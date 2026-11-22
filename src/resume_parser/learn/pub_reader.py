"""A learned reader for publication fields (title, venue, year, type). Experimental: no pipeline
stage uses it; eval/pub_reader_loo.py compares it with the rules.

It reads one publication entry, as split by stage 5 (the splitting is not learned):
  tokens    the entry text cleaned the way publications.parse_entry cleans it (list marker, links
            and DOIs removed), cut into letter runs, digit runs and single punctuation marks; each
            token keeps its character span, so every value is a piece of the text as printed
  features  SHAPES ONLY, never word identities: token shape (Xx, XX, x, d4, punctuation kind),
            length bucket, relative position, the shapes of 2 neighbours on each side, year-like
            and plausible-year flags, initials, commas/periods/colons seen so far, inside quotes
            or brackets, bold share of the line, first token of a line, and membership flags from
            config/publications.json (journal, conference, publisher, weak words, cues). Every
            feature value comes from a fixed alphabet (FEATURE_VALUES), so no word can enter a
            model. There is no italic flag in the line info, so italics are not used.
  tagger    DictVectorizer + LogisticRegression over AUTHOR, TITLE, VENUE, YEAR, OTHER. Decoding:
            title = longest TITLE run, venue = longest VENUE run (with the rules' trimming of a
            leading "In" and trailing volume or page numbers), year = the most likely YEAR token
            that is a plausible year (1950 to current_year + year_max_offset). Confidence = the
            mean probability of the chosen run; an empty value has confidence 0.
  type      DictVectorizer + LogisticRegression on entry-level flags (vocabulary words in the
            entry and in the rules' venue, sub-heading type, section-heading type, DOI, volume or
            pages, acronym next to a year, sizes). Confidence = top class probability.

Weak labels come from ground truth: for a GT publication matched to an entry, the GT title and
venue are found as token spans by fuzz.ratio (at least LABEL_MIN), and the GT year as tokens equal
to it; tokens before the title are AUTHOR, the rest OTHER. An entry is skipped (with a reason)
when the title, a GT venue or a GT year is not found, or when a second span that does not overlap
scores within AMBIGUOUS_GAP of the best.

Models hold only DictVectorizer vocabularies over these shape features and the fitted
coefficients. They are saved only under RESUME_DATA_DIR/models/, never in the repo.
"""

from __future__ import annotations

import pickle
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from resume_parser.entities import publications as pub
from resume_parser.schema import REPO_ROOT, DataDirError, data_root, load_config, require_inside_data_root

LABELS = ("AUTHOR", "TITLE", "VENUE", "YEAR", "OTHER")
TYPES = ("journal", "conference", "other")
LABEL_MIN = 85.0  # fuzz.ratio for a GT title or venue to be found as a span
AMBIGUOUS_GAP = 2.0
RANDOM_STATE = 0
MODEL_FILE = "pub_reader.pkl"

# ---------------------------------------------------------------- the fixed feature alphabet

LETTER_SHAPES = ("X", "x", "Xx", "XX", "xx", "xX", "XxX")
DIGIT_SHAPES = ("d1", "d2", "d3", "d4", "d5")
PUNCT_SHAPES = ("p_comma", "p_period", "p_colon", "p_semi", "p_quote", "p_open", "p_close", "p_dash",
                "p_slash", "p_amp", "p_other")
EDGE_SHAPES = ("BOS", "EOS")
SHAPES = LETTER_SHAPES + DIGIT_SHAPES + PUNCT_SHAPES + EDGE_SHAPES
LEN_BUCKETS = ("1", "2", "3", "4-6", "7-10", "11+")
COUNT_BUCKETS = ("0", "1", "2", "3-4", "5-8", "9+")
SIZE_BUCKETS = ("1-10", "11-20", "21-40", "41-80", "81+")
POS_BUCKETS = tuple(str(i) for i in range(10))
BOLD_BUCKETS = ("none", "some", "most")
SLOT_TYPES = ("journal", "conference", "other", "none")
FEATURE_VALUES = frozenset(SHAPES + LEN_BUCKETS + COUNT_BUCKETS + SIZE_BUCKETS + POS_BUCKETS + BOLD_BUCKETS
                           + SLOT_TYPES)
VOCAB_FLAGS = ("journal", "conference", "publisher", "weak", "conference_cue", "other_cue")
TOKEN_KEYS = frozenset(
    ["s", "len", "pos", "s-2", "s-1", "s+1", "s+2", "s-1|s", "s|s+1", "ncomma", "nperiod", "ncolon", "bold",
     "ntok", "year", "plausible_year", "initial", "in_quotes", "in_brackets", "line_start", "after_year"]
    + [f"v_{f}" for f in VOCAB_FLAGS])
TYPE_KEYS = frozenset(
    ["sub", "sec", "ntok", "nyears", "rv_journal", "rv_conference", "rv_weak", "rv_empty", "has_doi", "has_url",
     "vol_pages", "acronym_year", "in_lead"] + [f"e_{f}" for f in VOCAB_FLAGS])

_TOKEN = re.compile(r"[^\W\d_]+|\d+|[^\w\s]|_")
_PUNCT = {",": "p_comma", ".": "p_period", ":": "p_colon", ";": "p_semi", "(": "p_open", "[": "p_open",
          "{": "p_open", ")": "p_close", "]": "p_close", "}": "p_close", "-": "p_dash", "–": "p_dash",
          "—": "p_dash", "/": "p_slash", "&": "p_amp"}
_QUOTES = set("\"“”‘’'`«»")
_IN_LEAD = re.compile(r"(?<![A-Za-z])In\s*:|(?<![A-Za-z])In\s+(?=[A-Z0-9])")


def _bucket(n: int, edges: tuple[int, ...], names: tuple[str, ...]) -> str:
    for edge, name in zip(edges, names):
        if n <= edge:
            return name
    return names[-1]


def shape(tok: str) -> str:
    """The shape class of one token; always a member of SHAPES."""
    if tok.isdigit():
        return DIGIT_SHAPES[min(len(tok), 5) - 1]
    if tok.isalpha():
        if len(tok) == 1:
            return "X" if tok.isupper() else "x"
        if tok.isupper():
            return "XX"
        if tok.islower():
            return "xx"
        if tok[0].isupper() and tok[1:].islower():
            return "Xx"
        return "xX" if tok[0].islower() else "XxX"
    if tok in _QUOTES:
        return "p_quote"
    return _PUNCT.get(tok, "p_other")


@lru_cache(maxsize=1)
def _vocab_patterns() -> dict[str, re.Pattern]:
    cfg, p = load_config("publications.json"), pub._patterns()
    return {"journal": p["journal"], "conference": p["conference"], "publisher": p["publisher"],
            "weak": pub._words_pattern(cfg.get("weak_type_words", [])), "conference_cue": p["conference_cue"],
            "other_cue": p["other_cue"]}


def _max_year(params: dict) -> int:
    return params["current_year"] + params["year_max_offset"]


def _plausible(tok: str, params: dict) -> bool:
    return len(tok) == 4 and tok.isdigit() and 1950 <= int(tok) <= _max_year(params)


# ---------------------------------------------------------------- one entry


def entry_text(lines: list[dict]) -> str:
    """The entry text the rules read fields from (publications.parse_entry), before field reading."""
    raw = pub._join([line["text"] for line in lines])
    text = pub._MARKER.sub("", raw, count=1).strip()
    text = re.sub(r"^\d{1,3}\s+(?=[A-Z])", "", text)
    return pub._DOI.sub(" ", pub._URL.sub(" ", text))


def tokenize(entry: dict) -> dict:
    """{"text", "tokens": [(token, start, end)], "line_of": [line index per token]}. The line of a
    token is found by its relative position, as the cleaned text no longer lines up exactly."""
    lines = entry["lines"]
    text = entry_text(lines)
    tokens = [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(text)]
    lengths = [max(len(line["text"]), 1) for line in lines]
    total, cum, bounds = sum(lengths), 0, []
    for n in lengths:
        cum += n
        bounds.append(cum / total)
    line_of = []
    for _tok, start, _end in tokens:
        frac = start / max(len(text), 1)
        line_of.append(next((k for k, b in enumerate(bounds) if frac < b), len(lines) - 1))
    return {"text": text, "tokens": tokens, "line_of": line_of}


def token_features(entry: dict, tok: dict, params: dict) -> list[dict]:
    text, tokens, line_of = tok["text"], tok["tokens"], tok["line_of"]
    lines = entry["lines"]
    n = len(tokens)
    shapes = [shape(t) for t, _, _ in tokens]
    vocab = {name: [(m.start(), m.end()) for m in pat.finditer(text)] for name, pat in _vocab_patterns().items()}
    feats = []
    commas = periods = colons = quotes = depth = 0
    seen_year = False
    for i, (t, start, end) in enumerate(tokens):
        def sh(k):
            return "BOS" if k < 0 else "EOS" if k >= n else shapes[k]

        bold = float(lines[line_of[i]].get("bold") or 0.0)
        f = {"s": shapes[i], "len": _bucket(len(t), (1, 2, 3, 6, 10), LEN_BUCKETS),
             "pos": str(min(int(10 * i / max(n, 1)), 9)), "s-2": sh(i - 2), "s-1": sh(i - 1), "s+1": sh(i + 1),
             "s+2": sh(i + 2), "s-1|s": f"{sh(i - 1)}|{shapes[i]}", "s|s+1": f"{shapes[i]}|{sh(i + 1)}",
             "ncomma": _bucket(commas, (0, 1, 2, 4, 8), COUNT_BUCKETS),
             "nperiod": _bucket(periods, (0, 1, 2, 4, 8), COUNT_BUCKETS),
             "ncolon": _bucket(colons, (0, 1, 2, 4, 8), COUNT_BUCKETS),
             "bold": "none" if bold <= 0 else "some" if bold < 0.5 else "most",
             "ntok": _bucket(n, (10, 20, 40, 80), SIZE_BUCKETS)}
        year_like = len(t) == 4 and t.isdigit() and t[:2] in ("19", "20")
        flags = {"year": year_like, "plausible_year": _plausible(t, params),
                 "initial": len(t) == 1 and t.isupper() and i + 1 < n and tokens[i + 1][0] == ".",
                 "in_quotes": quotes % 2 == 1, "in_brackets": depth > 0,
                 "line_start": i == 0 or line_of[i] != line_of[i - 1], "after_year": seen_year}
        for name, spans in vocab.items():
            flags[f"v_{name}"] = any(a <= start and end <= b for a, b in spans)
        f.update({k: 1 for k, v in flags.items() if v})
        feats.append(f)
        commas += t == ","
        periods += t == "."
        colons += t == ":"
        quotes += t in _QUOTES
        depth = max(depth + (t in "([{") - (t in ")]}"), 0)
        seen_year = seen_year or year_like
    return feats


def type_features(entry: dict, tok: dict, params: dict) -> dict:
    text, rules = tok["text"], entry.get("rules") or {}
    venue = rules.get("venue") or ""
    pats = _vocab_patterns()
    years = [t for t, _, _ in tok["tokens"] if _plausible(t, params)]
    first = entry["lines"][0] if entry["lines"] else {}
    f = {"sub": first.get("pub_sub_type") or "none", "sec": first.get("pub_sec_type") or "none",
         "ntok": _bucket(len(tok["tokens"]), (10, 20, 40, 80), SIZE_BUCKETS),
         "nyears": _bucket(len(years), (0, 1, 2, 4, 8), COUNT_BUCKETS)}
    acronym = bool(re.fullmatch(r"[A-Z]{3,8}", venue)) and bool(
        re.search(rf"\b{re.escape(venue)}\s*['’]?\s*(?:(?:19|20)\d{{2}}|\d{{2}})\b", text)) if venue else False
    flags = {"rv_journal": bool(venue) and bool(pats["journal"].search(venue)),
             "rv_conference": bool(venue) and bool(pats["conference"].search(venue)),
             "rv_weak": bool(venue) and bool(pats["weak"].search(venue)), "rv_empty": not venue,
             "has_doi": bool(pub._DOI.search(pub._join([l["text"] for l in entry["lines"]]))),
             "has_url": bool(pub._URL.search(pub._join([l["text"] for l in entry["lines"]]))),
             "vol_pages": bool(re.search(r"\bvol(?:ume)?\.?\s*\d|\bpp?\.?\s*\d|\d+\s*\(\s*\d+\s*\)", text, re.I)),
             "acronym_year": acronym, "in_lead": bool(_IN_LEAD.search(text))}
    for name, pat in pats.items():
        flags[f"e_{name}"] = bool(pat.search(text))
    f.update({k: 1 for k, v in flags.items() if v})
    return f


# ---------------------------------------------------------------- weak labels


def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _find_span(tok: dict, target: str, blocked: set[int]) -> tuple[str, tuple[int, int] | None]:
    """("ok", (first, last) token index) or (reason, None)."""
    goal = _norm(target)
    n_goal = len(goal.split())
    words = [i for i, (t, _, _) in enumerate(tok["tokens"]) if t.isalnum() and i not in blocked]
    found = []
    for a in range(len(words)):
        for length in range(max(1, int(0.7 * n_goal)), int(1.3 * n_goal) + 2):
            b = a + length - 1
            if b >= len(words):
                break
            i, j = words[a], words[b]
            if any(k in blocked for k in range(i, j + 1)):
                break
            score = fuzz.ratio(_norm(tok["text"][tok["tokens"][i][1]:tok["tokens"][j][2]]), goal)
            if score >= LABEL_MIN:
                found.append((score, i, j))
    if not found:
        return "not found", None
    found.sort(key=lambda x: (-x[0], x[1], x[2]))
    best = found[0]
    for score, i, j in found[1:]:
        if (j < best[1] or i > best[2]) and score >= best[0] - AMBIGUOUS_GAP:
            return "ambiguous", None
    return "ok", (best[1], best[2])


def weak_labels(tok: dict, gt: dict) -> tuple[list[str] | None, str]:
    """(token labels, "ok") or (None, skip reason)."""
    n = len(tok["tokens"])
    if not _norm(gt.get("title")):
        return None, "title not found"
    status, title = _find_span(tok, gt["title"], set())
    if not title:
        return None, f"title {status}"
    labels = ["OTHER"] * n
    for k in range(title[0], title[1] + 1):
        labels[k] = "TITLE"
    for k in range(title[0]):
        labels[k] = "AUTHOR"
    if _norm(gt.get("venue")):
        status, venue = _find_span(tok, gt["venue"], set(range(title[0], title[1] + 1)))
        if not venue:
            return None, f"venue {status}"
        for k in range(venue[0], venue[1] + 1):
            labels[k] = "VENUE"
    year = str(gt.get("year") or "").strip()
    if year:
        hits = [k for k, (t, _, _) in enumerate(tok["tokens"]) if t == year and labels[k] != "TITLE"]
        if not hits:
            return None, "year not found"
        for k in hits:
            labels[k] = "YEAR"
    return labels, "ok"


# ---------------------------------------------------------------- training and reading


def train(token_X: list[dict], token_y: list[str], type_X: list[dict], type_y: list[str]) -> dict:
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression

    models = {}
    for key, X, y in (("tag", token_X, token_y), ("type", type_X, type_y)):
        vec = DictVectorizer(sparse=True, sort=True)
        clf = LogisticRegression(max_iter=2000, random_state=RANDOM_STATE)
        clf.fit(vec.fit_transform(X), y)
        models[key] = (vec, clf)
    return models


def _runs(labels: list[str], label: str) -> list[tuple[int, int]]:
    runs, start = [], None
    for k, lab in enumerate(labels + [""]):
        if lab == label and start is None:
            start = k
        elif lab != label and start is not None:
            runs.append((start, k - 1))
            start = None
    return runs


def _longest(labels, proba, col, label) -> tuple[tuple[int, int] | None, float]:
    runs = _runs(labels, label)
    if not runs:
        return None, 0.0
    scored = [((j - i + 1), sum(proba[k][col] for k in range(i, j + 1)) / (j - i + 1), -i, (i, j)) for i, j in runs]
    best = max(scored)
    return best[3], float(best[1])


def _cut(tok: dict, span: tuple[int, int] | None) -> str:
    if not span:
        return ""
    text = tok["text"][tok["tokens"][span[0]][1]:tok["tokens"][span[1]][2]]
    return re.sub(r"\s+", " ", text).strip(" ,;:.\"“”‘’'")


def read_entry(models: dict, entry: dict, params: dict) -> dict:
    """{"title", "venue", "year", "type", "conf": {field: confidence}} for one entry."""
    tok = tokenize(entry)
    out = {"title": "", "venue": "", "year": "", "type": "", "conf": {"title": 0.0, "venue": 0.0, "year": 0.0,
                                                                     "type": 0.0}}
    if tok["tokens"]:
        vec, clf = models["tag"]
        proba = clf.predict_proba(vec.transform(token_features(entry, tok, params)))
        classes = list(clf.classes_)
        labels = [classes[max(range(len(classes)), key=lambda c: row[c])] for row in proba]
        for field, label in (("title", "TITLE"), ("venue", "VENUE")):
            if label in classes:
                span, conf = _longest(labels, proba, classes.index(label), label)
                value = _cut(tok, span)
                if field == "venue" and value:
                    value = pub._TRAIL_NUMBERS.sub("", pub._LEAD_IN.sub("", value)).strip(" ,;:.")
                out[field], out["conf"][field] = value, (conf if value else 0.0)
        if "YEAR" in classes:
            col = classes.index("YEAR")
            years = [(proba[k][col], -k, t) for k, (t, _, _) in enumerate(tok["tokens"])
                     if labels[k] == "YEAR" and _plausible(t, params)]
            if years:
                best = max(years)
                out["year"], out["conf"]["year"] = best[2], float(best[0])
    vec, clf = models["type"]
    proba = clf.predict_proba(vec.transform([type_features(entry, tok, params)]))[0]
    k = max(range(len(proba)), key=lambda c: proba[c])
    out["type"], out["conf"]["type"] = str(clf.classes_[k]), float(proba[k])
    return out


def combine(rules: dict, learned: dict, threshold: float) -> dict:
    """The learned value of a field when its confidence is at least threshold, else the rules value."""
    return {f: (learned[f] if learned["conf"][f] >= threshold else rules.get(f, "") or "")
            for f in ("title", "venue", "year", "type")}


# ---------------------------------------------------------------- saving


def model_path() -> Path:
    return data_root() / "models" / MODEL_FILE


def save_model(models: dict, path: Path | None = None) -> Path:
    """Write the models under RESUME_DATA_DIR/models/ only; any other place is refused."""
    target = Path(path) if path is not None else model_path()
    resolved = require_inside_data_root(target)
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise DataDirError("refusing to write a model inside the repo.")
    if resolved.parent.name != "models" or resolved.parent.parent != data_root():
        raise DataDirError("models are written only to RESUME_DATA_DIR/models/.")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(pickle.dumps(models))
    return resolved
