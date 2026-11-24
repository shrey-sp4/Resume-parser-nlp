"""Venue tiers: journals from the SCImago journal list, conferences from the ICORE list.

Tier rule (config/tiers.json): A = SCImago Q1 or ICORE A*/A; B = Q2 or ICORE B; C = Q3/Q4 or
ICORE C; anything else is unknown (None), which is neutral, never low. Journal tiers apply only to
entries typed journal, ICORE only to entries typed conference, and nothing to other types.

Journals (rows typed journal, trade journal or book series), first hit wins:
  issn     an ISSN written in the venue string
  exact    a normalised form of the venue (abbreviations expanded, a parenthesised part kept or
           dropped) equals a normalised SCImago title
  acronym  a standalone token with at least two capitals named in config/venue_acronyms.json
  fuzzy    text.FuzzyMatcher (score, token guard, margin, no fuzzy for short names)
  A title listed more than once with different quartiles is ambiguous (unknown).
  With a role field (config/venue_fields.json), the tier is the best quartile among the
  journal's categories in that field; when none of them is in the field, the best overall
  quartile, marked out_of_field. With no field, the best overall quartile.
Conferences (ICORE): years, ordinals, "proceedings", publisher words (IEEE, ACM, ...) and a
parenthesised part are removed; then exact title, title alias, acronym (ICORE's own, plus the
aliases in config/venue_acronyms.json; an acronym ICORE lists more than once is resolved by
shared title words, else by agreement of the ranks, else ambiguous) and fuzzy title. A
workshop, companion, adjunct, doctoral, tutorial, poster or demo venue never takes the rank of a
main conference whose title lacks that word.

When the whole venue string finds nothing, each comma-separated part of at least two content words
is tried on its own ("Name, City, Country"; "Journal, Publisher"), and the result says from_part.
A journal that still finds nothing gets three more rules, in this order, each refused when it
fits more than one title: prefix (a dotted abbreviation the table does not know, such as
"Fict.", stands for a title word it begins; same number of words, in order), colon (the venue
is the main title before a colon of one journal) and publisher (publisher words such as
Elsevier or Springer dropped when 3+ content words remain). The method says which rule matched.

Output per venue: tier, source, matched_title, match_score, method, categories, field,
field_categories, out_of_field, quartile or core_rank, certainty (high: issn, exact, acronym;
medium: fuzzy; none: no tier) and reason.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from functools import lru_cache
from pathlib import Path

from resume_parser.schema import load_config
from shortlisting.tiers.lexicons import LexiconError, lexicon_file, tier_settings
from shortlisting.tiers.text import STOPWORDS, FuzzyMatcher, ascii_lower, content_tokens, normalise, variants

_CAT = re.compile(r"^(.*?)(?:\s*\((Q[1-4])\))?$")
_ISSN = re.compile(r"\b(\d{4})-?(\d{3}[\dXx])\b")
_ACRONYM_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9&/\-]*[A-Za-z0-9]|[A-Za-z]")
_PAREN = re.compile(r"\([^)]*\)")
CONF_DROP = frozenset({"proceedings", "proc", "annual", "ieee", "acm", "cvf", "usenix", "siam", "ifip", "springer",
                       "lncs", "edition"})
GENERIC_CONF_WORDS = frozenset({"international", "conference", "symposium", "workshop", "annual", "meeting", "joint",
                                "national", "congress", "forum", "acm", "ieee"})
PUBLISHER_WORDS = re.compile(r"\b(springer|elsevier|wiley|taylor|francis|sage|iop|aip|rsc|mdpi|hindawi|inderscience|emerald|igi|crc|"
                             r"publishing|publications|publisher|press|online)\b", re.IGNORECASE)
SATELLITE_WORDS = frozenset({"workshop", "workshops", "companion", "adjunct", "doctoral", "consortium", "tutorial",
                             "tutorials", "poster", "posters", "demo", "demos"})
_ORDINAL = re.compile(r"^\d+(st|nd|rd|th)$")
_YEAR = re.compile(r"^(19|20)\d\d$")
TIERED_TYPES = ("journal", "conference")


@dataclass(frozen=True)
class Journal:
    title: str
    norm: str
    type: str
    issns: tuple
    best_quartile: str | None
    categories: tuple  # ((name, quartile or None), ...)
    areas: tuple


@dataclass(frozen=True)
class Conference:
    title: str
    norm: str
    acronym: str
    source: str
    rank: str


@dataclass
class VenueResult:
    tier: str | None = None
    source: str | None = None
    matched_title: str | None = None
    match_score: float | None = None
    method: str | None = None
    categories: list = dc_field(default_factory=list)
    field: str | None = None
    field_categories: list = dc_field(default_factory=list)
    out_of_field: bool | None = None
    quartile: str | None = None
    core_rank: str | None = None
    certainty: str = "none"
    reason: str = "no_match"
    from_part: bool = False  # matched on one comma-separated part of the venue string

    def as_dict(self) -> dict:
        return dict(self.__dict__)


# ---------------------------------------------------------------- loading


def load_scimago(path: Path) -> list[Journal]:
    """The SCImago journal list (';' separated; columns read by name, the first of a repeated
    name; decimal commas are not used here)."""
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    if not rows:
        raise LexiconError("the SCImago file is empty.")
    head = rows[0]
    try:
        ti, ty, iss, bq, ca, ar = (head.index(n) for n in ("Title", "Type", "Issn", "SJR Best Quartile", "Categories", "Areas"))
    except ValueError:
        raise LexiconError("the SCImago file does not have the expected columns.") from None
    out = []
    for r in rows[1:]:
        if len(r) < len(head):
            continue
        cats = []
        for part in (x.strip() for x in r[ca].split(";")):
            if part:
                m = _CAT.match(part)
                cats.append((m.group(1).strip(), m.group(2)))
        issns = tuple(x.strip().upper() for x in r[iss].split(",") if len(x.strip()) == 8)
        q = r[bq].strip()
        out.append(Journal(r[ti].strip(), normalise(r[ti]), r[ty].strip().lower(), issns, q if q in ("Q1", "Q2", "Q3", "Q4") else None,
                           tuple(cats), tuple(a.strip() for a in r[ar].split(";") if a.strip())))
    return out


def conference_norm(text: str, abbreviations: dict | None = None) -> list[str]:
    """Normalised forms of a conference name for matching (see the module docstring)."""
    base = _PAREN.sub(" ", str(text or ""))
    forms = variants(base, abbreviations) if abbreviations else [normalise(base)]
    out = []
    for f in forms:
        toks = [t for t in f.split() if t not in CONF_DROP and not _YEAR.match(t) and not _ORDINAL.match(t)]
        while toks and toks[0] in ("of", "in", "on"):
            toks.pop(0)
        form = " ".join(toks)
        if form and form not in out:
            out.append(form)
    return out


def load_core(path: Path) -> list[Conference]:
    """The ICORE export: no header; id, title, acronym, source, rank, ... (quoted where needed)."""
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    out = []
    for r in rows:
        if len(r) < 5 or not r[1].strip():
            continue
        forms = conference_norm(r[1])
        out.append(Conference(r[1].strip(), forms[0] if forms else "", r[2].strip(), r[3].strip(), r[4].strip()))
    if not out:
        raise LexiconError("the ICORE file has no rows.")
    return out


# ---------------------------------------------------------------- the index


def ckey(norm: str) -> str:
    """The lookup key of a normalised name: its content tokens (stop words dropped, so
    'Trans. Signal Process.' meets 'Transactions on Signal Processing')."""
    return " ".join(content_tokens(norm))


class VenueIndex:
    def __init__(self, journals: list[Journal], conferences: list[Conference], fields: dict | None = None,
                 abbreviations: dict | None = None, acronyms: dict | None = None, settings: dict | None = None,
                 scimago_label: str = "scimago", strict_fields: bool = True):
        self.settings = settings if settings is not None else tier_settings()
        self.abbr = abbreviations if abbreviations is not None else load_config("venue_abbreviations.json")
        acr = acronyms if acronyms is not None else load_config("venue_acronyms.json")
        self.fields = (fields if fields is not None else load_config("venue_fields.json"))["fields"]
        self.scimago_label = scimago_label
        jtypes = set(self.settings["scimago_journal_types"])
        self.journals = [j for j in journals if j.type in jtypes]
        self.by_title: dict[str, list[Journal]] = {}
        self.by_issn: dict[str, list[Journal]] = {}
        for j in self.journals:
            self.by_title.setdefault(ckey(j.norm), []).append(j)
            for i in j.issns:
                self.by_issn.setdefault(i, []).append(j)
        self.journal_acronyms = {k.upper(): ckey(normalise(v)) for k, v in acr.get("journals", {}).items()}
        fuzzy = self.settings["venue_fuzzy"]
        self.journal_matcher = FuzzyMatcher(self.by_title, fuzzy)
        # for the journal rules (prefix, colon, publisher), tried only when nothing else matched
        self.journal_rules = bool(self.settings.get("journal_rules", True))
        self.by_length: dict[int, list[str]] = {}
        for key in self.by_title:
            self.by_length.setdefault(len(key.split()), []).append(key)
        self.by_main: dict[str, list[Journal]] = {}
        for j in self.journals:
            if ":" in j.title:
                main = ckey(normalise(j.title.split(":")[0]))
                if main and main not in self.by_title:
                    self.by_main.setdefault(main, []).append(j)

        self.conferences = conferences
        self.conf_by_title: dict[str, list[Conference]] = {}
        self.conf_by_acronym: dict[str, list[Conference]] = {}
        for c in conferences:
            if c.norm:
                self.conf_by_title.setdefault(ckey(c.norm), []).append(c)
            if c.acronym:
                self.conf_by_acronym.setdefault(c.acronym.upper(), []).append(c)
        self.conf_aliases = {k.upper(): v.upper() for k, v in acr.get("conference_aliases", {}).items()}
        self.conf_title_aliases = {}
        for k, v in acr.get("conference_title_aliases", {}).items():
            for form in conference_norm(k):
                self.conf_title_aliases[ckey(form)] = v.upper()
        self.conf_matcher = FuzzyMatcher(self.conf_by_title, fuzzy)
        unknown = self.unknown_field_names()
        if strict_fields and unknown:
            raise LexiconError(f"config/venue_fields.json names {len(unknown)} categor(ies) or area(s) "
                               "the SCImago list does not have.")

    # ---- fields

    def unknown_field_names(self) -> list[str]:
        cats = {n for j in self.journals for n, _ in j.categories}
        areas = {a for j in self.journals for a in j.areas}
        bad = []
        for f in self.fields.values():
            bad += [c for c in f.get("categories", []) if c not in cats]
            bad += [a for a in f.get("areas", []) if a not in areas]
        return sorted(set(bad))

    def _journal_result(self, j: Journal, method: str, score: float, field_name: str | None) -> VenueResult:
        res = VenueResult(source=self.scimago_label, matched_title=j.title, match_score=round(score, 1), method=method,
                          categories=[{"name": n, "quartile": q} for n, q in j.categories], field=field_name,
                          certainty="high" if method in ("issn", "exact", "acronym") else "medium")
        quartiles = [q for _, q in j.categories if q]
        overall = j.best_quartile or (min(quartiles) if quartiles else None)
        q = overall
        if field_name:
            spec = self.fields.get(field_name)
            if spec is None:
                raise KeyError(f"unknown field {field_name!r} (config/venue_fields.json)")
            cats, areas = set(spec.get("categories", [])), set(spec.get("areas", []))
            in_field = [(n, qq) for n, qq in j.categories if n in cats or (areas and set(j.areas) & areas)]
            res.field_categories = [n for n, _ in in_field]
            fq = [qq for _, qq in in_field if qq]
            if fq:
                q, res.out_of_field = min(fq), False
            else:
                res.out_of_field = True
        res.quartile = q
        res.tier = self.settings["quartile_tier"].get(q) if q else None
        res.reason = "ok" if res.tier else "no_quartile"
        if not res.tier:
            res.certainty = "none"
        return res

    def _from_journals(self, js: list[Journal], method: str, score: float, field_name: str | None) -> VenueResult:
        results = [self._journal_result(j, method, score, field_name) for j in js]
        if len({r.tier for r in results}) == 1:
            return results[0]
        return VenueResult(method=method, match_score=score, reason="ambiguous_duplicate_title")

    def _acronyms_in(self, text: str) -> list[str]:
        toks = _ACRONYM_TOKEN.findall(str(text or ""))
        words = [t for t in toks if t.isalpha() and len(t) > 1]
        if len(words) >= 3 and all(t.isupper() for t in words) and len(toks) > 1:
            return []  # a title written in capitals: its words are not acronyms
        return [t.upper() for t in toks if sum(ch.isupper() for ch in t) >= 2]

    def journal_tier(self, venue: str, field_name: str | None = None) -> VenueResult:
        for a, b in _ISSN.findall(str(venue or "")):
            js = self.by_issn.get((a + b).upper())
            if js:
                return self._from_journals(js, "issn", 100.0, field_name)
        forms = variants(venue, self.abbr)
        stripped = _PAREN.sub(" ", str(venue or ""))
        if stripped != venue:
            forms += [f for f in variants(stripped, self.abbr) if f not in forms]
        forms = list(dict.fromkeys(ckey(f) for f in forms))
        for f in forms:
            if f in self.by_title:
                return self._from_journals(self.by_title[f], "exact", 100.0, field_name)
        hits = {self.journal_acronyms[a] for a in self._acronyms_in(venue) if a in self.journal_acronyms}
        if len(hits) == 1:
            title = hits.pop()
            if title in self.by_title:
                return self._from_journals(self.by_title[title], "acronym", 100.0, field_name)
        elif len(hits) > 1:
            return VenueResult(method="acronym", reason="ambiguous_acronym")
        fr = self.journal_matcher.match(forms)
        if fr.key is None:
            return VenueResult(match_score=fr.score or None, reason=fr.reason if fr.reason != "fuzzy" else "no_match")
        return self._from_journals(self.by_title[fr.key], "fuzzy", fr.score, field_name)

    def _conf_result(self, cs: list[Conference], method: str, score: float, query_tokens: list[str]) -> VenueResult:
        if len(cs) > 1:  # e.g. an acronym ICORE lists more than once: shared title words decide
            words = set(query_tokens) - GENERIC_CONF_WORDS
            shared = sorted(((len(words & (set(content_tokens(c.norm)) - GENERIC_CONF_WORDS)), k) for k, c in enumerate(cs)),
                            reverse=True)
            if shared[0][0] > 0 and shared[0][0] > shared[1][0]:  # the one sharing the most title words
                cs = [cs[shared[0][1]]]
            elif len({self.settings["core_rank_tier"].get(c.rank) for c in cs}) != 1:
                return VenueResult(method=method, match_score=score, reason="ambiguous_acronym")
        c = cs[0]
        side = SATELLITE_WORDS & set(query_tokens)
        if side - set(content_tokens(c.norm)):  # a workshop never takes its main conference's rank
            return VenueResult(method=method, match_score=score, reason="satellite_event_not_ranked")
        tier = self.settings["core_rank_tier"].get(c.rank)
        return VenueResult(tier=tier, source=c.source.lower() or "core", matched_title=c.title, match_score=round(score, 1),
                           method=method, core_rank=c.rank,
                           certainty=("high" if method in ("exact", "acronym", "title_alias") else "medium") if tier else "none",
                           reason="ok" if tier else "core_rank_not_tiered")

    def conference_tier(self, venue: str) -> VenueResult:
        forms = list(dict.fromkeys(ckey(f) for f in conference_norm(venue, self.abbr)))
        qtok = forms[0].split() if forms else []
        for f in forms:
            if f in self.conf_by_title:
                return self._conf_result(self.conf_by_title[f], "exact", 100.0, qtok)
        for f in forms:
            if f in self.conf_title_aliases and self.conf_title_aliases[f] in self.conf_by_acronym:
                return self._conf_result(self.conf_by_acronym[self.conf_title_aliases[f]], "title_alias", 100.0, qtok)
        found = []
        for a in self._acronyms_in(venue):
            a = self.conf_aliases.get(a, a)
            if a in self.conf_by_acronym and a not in found:
                found.append(a)
        if len(found) == 1:
            return self._conf_result(self.conf_by_acronym[found[0]], "acronym", 100.0, qtok)
        if len(found) > 1:
            return VenueResult(method="acronym", reason="ambiguous_acronym")
        fr = self.conf_matcher.match(forms)
        if fr.key is None:
            return VenueResult(match_score=fr.score or None, reason=fr.reason if fr.reason != "fuzzy" else "no_match")
        return self._conf_result(self.conf_by_title[fr.key], "fuzzy", fr.score, qtok)

    def tier(self, venue: str, entry_type: str | None, field_name: str | None = None) -> dict:
        """The tier result for one publication venue, as a dict (see the module docstring)."""
        if (entry_type or "") not in TIERED_TYPES:
            return VenueResult(reason="type_not_tiered").as_dict()
        if not str(venue or "").strip():
            return VenueResult(reason="no_venue").as_dict()
        res = self._one(venue, entry_type, field_name)
        if res.method is None and "," in venue:
            # "Conference name, City, Country" or "Journal, Publisher": each comma-separated part of at
            # least two content words is tried on its own (a one-word part such as a city never is)
            for part in (p.strip() for p in venue.split(",")):
                if len(content_tokens(normalise(part))) >= 2:
                    alt = self._one(part, entry_type, field_name)
                    if alt.method is not None:
                        alt.from_part = True
                        return alt.as_dict()
        if entry_type == "journal" and res.method is None and self.journal_rules:
            # only for a journal nothing above matched, so no earlier match can change
            extra, refused = self.journal_rule_match(venue, field_name)
            if extra is not None:
                return extra.as_dict()
            if refused:
                res.reason = refused
        return res.as_dict()

    # ---- the three journal rules, tried in this order when nothing else matched

    def _prefix_tokens(self, venue: str) -> list[tuple[list[str], bool]]:
        """Content tokens with their expansions, each marked: written as a dotted abbreviation that
        the abbreviation table does not resolve?"""
        always, with_stop = self.abbr.get("always", {}), self.abbr.get("with_stop", {})
        s = _PAREN.sub(" ", ascii_lower(venue)).replace("&", " and ")
        out = []
        for tok in re.findall(r"[a-z0-9]+\.?", s):
            word, dot = tok.rstrip("."), tok.endswith(".")
            if word in STOPWORDS:
                continue
            exp = always.get(word) or (with_stop.get(word) if dot else None)
            opts = [exp] if isinstance(exp, str) else list(exp) if exp else [word]
            out.append((opts, dot and not exp))
        return out

    def prefix_rule(self, venue: str, field_name: str | None) -> tuple[VenueResult | None, str | None]:
        """(a) A dotted abbreviation the table does not know stands for any title word it is a prefix
        of (3+ characters); every other token must be equal; same number of content tokens, in
        order; exactly one title."""
        toks = self._prefix_tokens(venue)
        if not any(dotted for _, dotted in toks):
            return None, None
        hits = []
        for key in self.by_length.get(len(toks), []):
            used = False
            for (opts, dotted), word in zip(toks, key.split()):
                if word in opts:
                    continue
                if dotted and any(len(a) >= 3 and not a.isdigit() and word.startswith(a) for a in opts):
                    used = True
                    continue
                break
            else:
                if used:
                    hits.append(key)
        if not hits:
            return None, None
        if len(hits) > 1:
            return None, "ambiguous_prefix"
        return self._from_journals(self.by_title[hits[0]], "prefix", 100.0, field_name), None

    def colon_rule(self, venue: str, field_name: str | None) -> tuple[VenueResult | None, str | None]:
        """(b) The venue equals the main title before a colon ("Journal of X: Subtitle") of exactly
        one journal title (that main title not being a full title itself)."""
        forms = {ckey(f) for b in {venue, _PAREN.sub(" ", venue)} for f in variants(b, self.abbr)}
        js = {id(j): j for f in forms for j in self.by_main.get(f, [])}
        if not js:
            return None, None
        if len({j.title for j in js.values()}) > 1:
            return None, "ambiguous_colon"
        return self._from_journals(list(js.values()), "colon", 100.0, field_name), None

    def publisher_rule(self, venue: str, field_name: str | None) -> tuple[VenueResult | None, str | None]:
        """(c) Publisher words (Elsevier, Springer, ...) dropped, when 3+ content words remain, and
        the rest matched by the usual methods (ISSN, exact, acronym, guarded fuzzy)."""
        if not PUBLISHER_WORDS.search(venue):
            return None, None
        rest = " ".join(PUBLISHER_WORDS.sub(" ", venue).split())
        if len(content_tokens(normalise(rest))) < 3:
            return None, None
        res = self.journal_tier(rest, field_name)
        if res.reason.startswith("ambiguous"):  # fits more than one title: refused
            return None, "ambiguous_publisher"
        if res.method is None:
            return None, None
        res.method = "publisher"
        res.certainty = "medium" if res.tier else "none"
        return res, None

    def journal_rule_match(self, venue: str, field_name: str | None) -> tuple[VenueResult | None, str | None]:
        """The first of the three journal rules that matches, else (None, the first refusal)."""
        refused = None
        for rule in (self.prefix_rule, self.colon_rule, self.publisher_rule):
            res, why = rule(venue, field_name)
            if res is not None:
                return res, None
            refused = refused or why
        return None, refused

    def _one(self, venue: str, entry_type: str, field_name: str | None) -> VenueResult:
        if entry_type == "journal":
            return self.journal_tier(venue, field_name)
        res = self.conference_tier(venue)
        res.field = field_name
        return res


@lru_cache(maxsize=2)
def _load(scimago: str, core: str, mtimes: tuple) -> VenueIndex:
    label = "scimago" + (re.search(r"(19|20)\d\d", Path(scimago).name) or [""])[0]
    return VenueIndex(load_scimago(Path(scimago)), load_core(Path(core)), scimago_label=label)


def load_index() -> VenueIndex:
    """The index over the lexicon files named in config/tiers.json (cached per file version)."""
    s, c = lexicon_file("scimago"), lexicon_file("core")
    return _load(str(s), str(c), (s.stat().st_mtime_ns, c.stat().st_mtime_ns))
