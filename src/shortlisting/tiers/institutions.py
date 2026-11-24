"""Institution tiers: NIRF 2025 for Indian institutions, an editorial seed list for others.

NIRF (config/tiers.json "nirf"): the saved ranking pages of the included categories (Overall,
University, College, Research, Engineering, Management, Pharmacy, Medical, Dental, Law,
Architecture, Agriculture by default), each a ranked page (Institute ID, Name, City, State,
Score, Rank) plus rank-band pages (Name, City, State; the band in the page heading). The
"ALL" pages list participants, not ranks, and are never read. One institution is joined across
categories by its NIRF ID (IR-O-U-0456 and IR-E-U-0456 are the same, U-0456); a band row joins
the institution of the same name and state. Tier = the best rank over its COUNTED categories, a
band counting at its lower bound: 1-25 Tier 1, 26-100 Tier 2, 101-200 Tier 3; anything else
unlisted. Counted (config/tiers.json): Overall, University, College and Research always; a
discipline category (Engineering, Management, Pharmacy, Medical, Dental, Law, Architecture,
Agriculture) only when the role field maps to it (nirf.field_categories); with no field, only
the general four. The result names the category that gave the best rank (best_category), the
counted categories, and every category with its rank and whether it counted. An institution
ranked only in categories that do not count is unlisted (no_rank_in_counted_categories).
Global seed (config/institution_global_seed.json): an editorial list of non-Indian
universities, all Tier 1; it is not a ranking. Every other institution is unlisted.
Unlisted (and ambiguous) means review, never rejection: tier None, review True.
Editorial overrides (config/institution_overrides.json, optional): an institution named there
(by its name or an alias, after the usual alias table) takes the tier written there, for every
field, before NIRF and the seed list; its result says source, method and reason "editorial
override". Without the file there are no overrides.

Matching, on each normalised form of the name (abbreviations of
config/institution_aliases.json expanded; a parenthesised part kept or dropped; city synonyms
both ways): alias (config/institution_aliases.json), the IIT/NIT/IIM/IISER/IIIT/AIIMS/NLU
patterns, exact NIRF name (also with its city appended, as NIRF keeps the city in its own
column), exact seed name or alias, then fuzzy (order-sensitive ratio of at
least 90 with the token guard in order, a 3-point margin, no fuzzy for short names). When the
whole string finds nothing, each comma-separated part is tried ("Department of X, <institute>,
City"). A name NIRF lists for several campuses is resolved by a city or state named in the
string; otherwise by agreement of their tiers; otherwise it is ambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path

from resume_parser.schema import CONFIG_DIR, load_config, read_json
from shortlisting.tiers.lexicons import LexiconError, lexicon_file, tier_settings
from shortlisting.tiers.text import FuzzyMatcher, content_tokens, normalise, variants

PAGE = re.compile(r"^(?P<cat>[A-Za-z]+?)Ranking(?P<suffix>\d+|ALL)?$")
BAND = re.compile(r"Rank\s*-?\s*band\s*:\s*(\d+)\s*-\s*(\d+)", re.IGNORECASE)
NIRF_ID = re.compile(r"^IR-[A-Z]+-([A-Z]+-\d+)$")
_PAREN = re.compile(r"\([^)]*\)")


OVERRIDES_FILE = "institution_overrides.json"


def overrides_path() -> Path:
    return CONFIG_DIR / OVERRIDES_FILE


def load_overrides() -> dict:
    """config/institution_overrides.json, or no overrides when the file is absent."""
    path = overrides_path()
    return read_json(path) if path.is_file() else {"overrides": []}


def ckey(norm: str) -> str:
    return " ".join(content_tokens(norm))


@dataclass(frozen=True)
class NirfEntry:
    name: str
    city: str
    state: str
    category: str
    rank_low: int
    rank_label: str
    nirf_id: str | None  # the shared part of the NIRF ID (e.g. "U-0456"); None on band pages


@dataclass
class Institution:
    identity: str
    names: set = dc_field(default_factory=set)
    city: str = ""
    state: str = ""
    ranks: list = dc_field(default_factory=list)  # (rank_low, rank_label, category)


@dataclass
class InstitutionResult:
    tier: str | None = None
    status: str = "unlisted"  # listed, unlisted, ambiguous
    review: bool = True
    source: str | None = None
    matched_name: str | None = None
    match_score: float | None = None
    method: str | None = None
    best_rank: int | None = None
    best_rank_label: str | None = None
    best_category: str | None = None
    categories: list = dc_field(default_factory=list)
    certainty: str = "none"
    reason: str = "not_listed"
    field: str | None = None
    counted_categories: list = dc_field(default_factory=list)
    from_part: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


# ---------------------------------------------------------------- NIRF pages


class _Rows(HTMLParser):
    """Rows of the outermost tables (first text of each cell) and the page headings."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth, self.rows, self.row, self.cell = 0, [], None, None
        self.heads, self.in_head, self.head = [], False, ""

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
        elif self.depth == 1 and tag == "tr":
            self.row = []
        elif self.depth == 1 and tag == "td" and self.row is not None:
            self.cell = []
        elif tag in ("h1", "h2", "h3", "h4"):
            self.in_head, self.head = True, ""

    def handle_endtag(self, tag):
        if tag == "table":
            self.depth -= 1
        elif self.depth == 1 and tag == "td" and self.cell is not None:
            self.row.append(next((x for x in self.cell if x), ""))
            self.cell = None
        elif self.depth == 1 and tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None
        elif tag in ("h1", "h2", "h3", "h4") and self.in_head:
            self.heads.append(" ".join(self.head.split()))
            self.in_head = False

    def handle_data(self, data):
        if self.cell is not None and self.depth == 1:
            self.cell.append(" ".join(data.split()))
        if self.in_head:
            self.head += data


def parse_nirf_page(text: str, category: str) -> list[NirfEntry]:
    p = _Rows()
    p.feed(text)
    band = next((m for h in p.heads for m in [BAND.search(h)] if m), None)
    out: list[NirfEntry] = []
    if band:
        lo, hi = int(band.group(1)), int(band.group(2))
        for r in p.rows:
            if len(r) == 3 and r[0]:
                out.append(NirfEntry(r[0], r[1], r[2], category, lo, f"{lo}-{hi}", None))
        return out
    seen = set()
    for r in p.rows:
        # Institute ID, Name, City, State, [Score,] Rank; longer rows are repeated detail rows
        if len(r) not in (5, 6) or not NIRF_ID.match(r[0]) or r[0] in seen:
            continue
        rank = r[-1] if len(r) == 5 else r[5]
        if rank.isdigit():
            seen.add(r[0])
            out.append(NirfEntry(r[1], r[2], r[3], category, int(rank), rank, NIRF_ID.match(r[0]).group(1)))
    return out


def load_nirf(folder: Path, categories: list[str]) -> tuple[list[NirfEntry], list[str]]:
    """Entries of the included categories, and the included categories with no ranked page."""
    entries, found = [], set()
    for path in sorted(Path(folder).glob("*.html")):
        m = PAGE.match(path.stem)
        if not m or m.group("suffix") == "ALL" or m.group("cat") not in categories:
            continue
        rows = parse_nirf_page(path.read_text(encoding="utf-8", errors="replace"), m.group("cat"))
        entries += rows
        if m.group("suffix") is None and rows:
            found.add(m.group("cat"))
    if not entries:
        raise LexiconError("no NIRF ranking rows were found in the NIRF folder.")
    return entries, [c for c in categories if c not in found]


# ---------------------------------------------------------------- the index


class InstitutionIndex:
    def __init__(self, entries: list[NirfEntry], seed: dict | None = None, aliases: dict | None = None,
                 settings: dict | None = None, overrides: dict | None = None):
        self.settings = settings if settings is not None else tier_settings()
        nirf = self.settings["nirf"]
        self.edition = nirf.get("edition", "nirf")
        self.bands = [tuple(b) for b in nirf["tier_bands"]]
        self.always_counted = list(nirf.get("always_counted", nirf["categories_included"]))
        self.field_categories = {k: list(v) for k, v in nirf.get("field_categories", {}).items()}
        cfg = aliases if aliases is not None else load_config("institution_aliases.json")
        self.abbr = cfg.get("abbreviations", {})
        self.patterns = [(ckey(normalise(p["short"])), ckey(normalise(p["long"]))) for p in cfg.get("patterns", [])]
        self.synonyms = [[normalise(w) for w in group] for group in cfg.get("city_synonyms", [])]
        self.aliases = {ckey(normalise(k)): ckey(normalise(v)) for k, v in cfg.get("aliases", {}).items()}

        # one Institution per NIRF ID; band rows join the same name and state
        self.institutions: dict[str, Institution] = {}
        by_name_state: dict[tuple, str] = {}
        for e in sorted(entries, key=lambda e: e.nirf_id is None):
            key = (ckey(normalise(e.name)), ckey(normalise(e.state)))
            identity = e.nirf_id or by_name_state.get(key) or f"band:{key[0]}|{key[1]}"
            inst = self.institutions.setdefault(identity, Institution(identity, city=e.city, state=e.state))
            inst.names.add(e.name)
            inst.ranks.append((e.rank_low, e.rank_label, e.category))
            by_name_state.setdefault(key, identity)
        self.by_name: dict[str, set] = {}
        for inst in self.institutions.values():
            for n in inst.names:
                # the name, and the name with its city (NIRF often keeps the city in its own
                # column); a parenthesised part is dropped only together with the city added, so
                # "Indian Institute of Technology (X)" never becomes a bare generic name
                keys = {ckey(normalise(n)), ckey(normalise(f"{n} {inst.city}")),
                        ckey(normalise(f"{_PAREN.sub(' ', n)} {inst.city}"))}
                for k in keys:
                    self.by_name.setdefault(k, set()).add(inst.identity)

        seed = seed if seed is not None else load_config("institution_global_seed.json")
        self.seed_tier = seed.get("tier", "Tier 1")
        self.seed: dict[str, str] = {}
        for s in seed.get("institutions", []):
            for n in [s["name"], *s.get("aliases", [])]:
                self.seed.setdefault(ckey(normalise(n)), s["name"])
        # editorial overrides win over NIRF and the seed list, for every field, and always say so
        self.overrides: dict[str, dict] = {}
        for o in (overrides if overrides is not None else load_overrides()).get("overrides", []):
            for n in [o["name"], *o.get("aliases", [])]:
                self.overrides.setdefault(ckey(normalise(n)), o)
        fuzzy = self.settings["institution_fuzzy"]
        self.nirf_matcher = FuzzyMatcher(self.by_name, fuzzy)
        self.seed_matcher = FuzzyMatcher(self.seed, fuzzy)

    # ---- forms of a name

    def _synonym_forms(self, key: str) -> list[str]:
        out = [key]
        for group in self.synonyms:
            for form in list(out):
                toks = form.split()
                for w in group:
                    if w in toks:
                        out += [" ".join(o if t == w else t for t in toks) for o in group if o != w]
        return list(dict.fromkeys(out))

    def forms(self, text: str) -> list[tuple[str, str]]:
        """(method, key) pairs to look up, in order: aliases, patterns, the plain forms."""
        base = [text] + ([_PAREN.sub(" ", text)] if "(" in text else [])
        keys = list(dict.fromkeys(ckey(v) for b in base for v in variants(b, self.abbr)))
        keys = list(dict.fromkeys(k2 for k in keys for k2 in self._synonym_forms(k)))
        out = [("alias", self.aliases[k]) for k in keys if k in self.aliases]
        for k in keys:
            for short, long_ in self.patterns:
                if k.startswith(short + " "):
                    out += [("pattern", f) for f in self._synonym_forms(long_ + k[len(short):])]
        out += [("exact", k) for k in keys]
        return list(dict.fromkeys(out))

    # ---- results

    def _tier_of(self, rank: int) -> str | None:
        return next((label for lo, hi, label in self.bands if lo <= rank <= hi), None)

    def counted_categories(self, field_name: str | None) -> list[str]:
        """The NIRF categories whose ranks count: always_counted, plus the discipline categories
        the role field maps to (config/tiers.json nirf.field_categories)."""
        if not field_name:
            return list(self.always_counted)
        if field_name not in self.field_categories:
            raise KeyError(f"unknown field {field_name!r} (config/tiers.json nirf.field_categories)")
        return list(dict.fromkeys(self.always_counted + self.field_categories[field_name]))

    def _best(self, inst: Institution, counted: list[str]):
        ranks = [r for r in inst.ranks if r[2] in counted]
        return min(ranks, key=lambda r: (r[0], r[2])) if ranks else None

    def _nirf_result(self, identities: set, method: str, score: float, text: str, counted: list[str],
                     field_name: str | None = None) -> InstitutionResult:
        insts = [self.institutions[i] for i in sorted(identities)]
        tier_of = lambda i: self._tier_of(b[0]) if (b := self._best(i, counted)) else None  # noqa: E731
        if len(insts) > 1:  # several campuses share the name: a city or state in the text decides
            words = set(normalise(text).split())
            named = [i for i in insts if words & set(normalise(f"{i.city} {i.state}").split())]
            if len(named) == 1:
                insts = named
            elif len({tier_of(i) for i in insts}) != 1:
                return InstitutionResult(status="ambiguous", source=self.edition, method=method,
                                         match_score=round(score, 1), reason="ambiguous_campus")
        inst = insts[0]
        best = self._best(inst, counted)
        tier = self._tier_of(best[0]) if best else None
        return InstitutionResult(
            tier=tier, status="listed" if tier else "unlisted", review=tier is None, source=self.edition,
            matched_name=sorted(inst.names)[0], match_score=round(score, 1), method=method,
            best_rank=best[0] if best else None, best_rank_label=best[1] if best else None,
            best_category=best[2] if best else None, field=field_name, counted_categories=list(counted),
            categories=[{"category": c, "rank": label, "counted": c in counted}
                        for _, label, c in sorted(inst.ranks, key=lambda r: (r[0], r[2]))],
            certainty=("high" if method in ("alias", "pattern", "exact") else "medium") if tier else "none",
            reason="ok" if tier else ("ranked_beyond_200" if best else "no_rank_in_counted_categories"))

    def _override_result(self, key: str) -> InstitutionResult:
        o = self.overrides[key]
        return InstitutionResult(tier=o["tier"], status="listed", review=False, source="editorial override",
                                 matched_name=o["name"], match_score=100.0, method="editorial override",
                                 certainty="high", reason="editorial override")

    def _seed_result(self, key: str, method: str, score: float) -> InstitutionResult:
        return InstitutionResult(tier=self.seed_tier, status="listed", review=False, source="global_seed",
                                 matched_name=self.seed[key], match_score=round(score, 1), method=method,
                                 certainty="high" if method in ("alias", "exact") else "medium", reason="ok")

    def _one(self, text: str, context: str | None = None, counted: list[str] | None = None,
             field_name: str | None = None) -> InstitutionResult:
        """One string; context (the whole string, when text is one comma-separated part) is what a
        campus is looked for in."""
        context = context or text
        counted = counted if counted is not None else self.counted_categories(None)
        forms = self.forms(text)
        for _method, key in forms:
            if key in self.overrides:
                return self._override_result(key)
        for method, key in forms:
            if key in self.by_name:
                return self._nirf_result(self.by_name[key], method, 100.0, context, counted, field_name)
        for method, key in forms:
            if key in self.seed:
                return self._seed_result(key, method, 100.0)
        keys = list(dict.fromkeys(k for _, k in forms))
        fr = self.nirf_matcher.match(keys)
        if fr.key is not None:
            return self._nirf_result(self.by_name[fr.key], "fuzzy", fr.score, context, counted, field_name)
        fs = self.seed_matcher.match(keys)
        if fs.key is not None:
            return self._seed_result(fs.key, "fuzzy", fs.score)
        reasons = {fr.reason, fs.reason}
        for r in ("ambiguous_margin", "rejected_by_token_guard", "short_name"):
            if r in reasons:
                return InstitutionResult(reason=r)
        return InstitutionResult(reason="not_listed")

    def tier(self, text: str, field_name: str | None = None) -> dict:
        """The tier result for one institution string, as a dict (see the module docstring). With
        a role field, the NIRF discipline categories of that field count too."""
        counted = self.counted_categories(field_name)
        if not str(text or "").strip():
            return InstitutionResult(reason="no_institution", field=field_name).as_dict()
        res = self._one(text, None, counted, field_name)
        if res.method is None and "," in text:
            for part in (p.strip() for p in text.split(",")):
                if part:
                    alt = self._one(part, text, counted, field_name)
                    if alt.method is not None:
                        alt.from_part = True
                        res = alt
                        break
        res.field, res.counted_categories = field_name, list(counted)
        return res.as_dict()


@lru_cache(maxsize=2)
def _load(folder: str, mtime: int) -> tuple[InstitutionIndex, tuple]:
    settings = tier_settings()
    entries, missing = load_nirf(Path(folder), settings["nirf"]["categories_included"])
    return InstitutionIndex(entries, settings=settings), tuple(missing)


def load_index() -> InstitutionIndex:
    """The index over the NIRF folder named in config/tiers.json (cached per folder version)."""
    folder = lexicon_file("nirf")
    mtime = max([folder.stat().st_mtime_ns] + [p.stat().st_mtime_ns for p in folder.glob("*.html")])
    index, missing = _load(str(folder), mtime)
    index.missing_categories = list(missing)
    return index
