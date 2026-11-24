"""Text helpers shared by the venue and institution tier lookups.

normalise      ASCII, lower case, "&" -> "and", punctuation -> space, "the" dropped,
               "(discontinued)" dropped, spaces collapsed
variants       the normalised forms of a name with its abbreviations expanded
               (config/venue_abbreviations.json style; an ambiguous one gives several)
content_tokens the tokens left after stop words
aligned        the token guard: every content token on each side lines up with exactly one
               token on the other side (equal; one an abbreviation prefix of the other, at
               least min_prefix characters; or, for tokens of 6+ characters, a per-token ratio
               of at least token_ratio). Single letters, numbers and roman numerals must be
               equal. So "Signal" never stands for "Image", "Letters" for "Transactions" or
               "A" for "B".
FuzzyMatcher   rapidfuzz token_sort_ratio (or ratio, with the guard in token order, when the
               settings say scorer "ratio" and ordered) over a fixed list of normalised keys, keeping only
               candidates at min_score or above that pass the token guard, and refusing a
               match when the second such candidate is within margin points (ambiguous).
               Short names (fewer than short_min_tokens content tokens or fewer than
               short_max_chars characters) are never fuzzy-matched.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz, process

STOPWORDS = frozenset({"of", "on", "and", "for", "in", "the", "an", "at", "to", "with", "de", "la", "et", "und", "fur"})
_ROMAN = re.compile(r"^(?=[ivxl]+$)l?x{0,3}(ix|iv|v?i{0,3})$")
_DISCONTINUED = re.compile(r"\(\s*discontinued\s*\)", re.IGNORECASE)
MAX_VARIANTS = 32


def ascii_lower(text: str) -> str:
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()


def normalise(text: str) -> str:
    s = _DISCONTINUED.sub(" ", ascii_lower(text)).replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(t for t in s.split() if t != "the")


def variants(text: str, abbreviations: dict) -> list[str]:
    """Normalised forms of text with abbreviations expanded: 'always' keys with or without a
    full stop, 'with_stop' keys only when written with one. At most MAX_VARIANTS forms; the
    first is the plain normalised text when no abbreviation fired."""
    always, with_stop = abbreviations.get("always", {}), abbreviations.get("with_stop", {})
    s = _DISCONTINUED.sub(" ", ascii_lower(text)).replace("&", " and ")
    options: list[list[str]] = []
    for tok in re.findall(r"[a-z0-9]+\.?", s):
        word, stop = tok.rstrip("."), tok.endswith(".")
        if word == "the":
            continue
        expansion = always.get(word) or (with_stop.get(word) if stop else None)
        if expansion is None:
            options.append([word])
        else:
            options.append([expansion] if isinstance(expansion, str) else list(expansion))
    out: list[str] = []
    for combo in itertools.islice(itertools.product(*options), MAX_VARIANTS):
        form = " ".join(combo)
        if form not in out:
            out.append(form)
    plain = normalise(text)
    return out if plain in out else [plain] + out[: MAX_VARIANTS - 1]


def content_tokens(norm: str) -> list[str]:
    return [t for t in norm.split() if t not in STOPWORDS]


def _strict(tok: str) -> bool:
    return len(tok) == 1 or tok.isdigit() or bool(_ROMAN.match(tok))


def tokens_compatible(a: str, b: str, min_prefix: int = 3, token_ratio: float = 85) -> bool:
    if a == b:
        return True
    if _strict(a) or _strict(b):
        return False
    short, long_ = sorted((a, b), key=len)
    if len(short) >= min_prefix and long_.startswith(short):
        return True
    return len(a) >= 6 and len(b) >= 6 and fuzz.ratio(a, b) >= token_ratio


def aligned(q_tokens: list[str], c_tokens: list[str], min_prefix: int = 3, token_ratio: float = 85,
            ordered: bool = False) -> bool:
    """One-to-one alignment of all content tokens (bipartite matching, small n); with ordered,
    token i must align with token i."""
    if len(q_tokens) != len(c_tokens):
        return False
    if ordered:
        return all(tokens_compatible(q, c, min_prefix, token_ratio) for q, c in zip(q_tokens, c_tokens))
    edges = [[j for j, c in enumerate(c_tokens) if tokens_compatible(q, c, min_prefix, token_ratio)] for q in q_tokens]
    owner = [-1] * len(c_tokens)

    def assign(i: int, seen: set) -> bool:
        for j in edges[i]:
            if j in seen:
                continue
            seen.add(j)
            if owner[j] < 0 or assign(owner[j], seen):
                owner[j] = i
                return True
        return False

    return all(assign(i, set()) for i in range(len(q_tokens)))


@dataclass
class FuzzyResult:
    key: str | None
    score: float
    reason: str  # "fuzzy", "no_candidate", "short_name", "rejected_by_token_guard", "ambiguous_margin"
    candidates_seen: int = 0
    rejected_by_guard: int = 0


class FuzzyMatcher:
    def __init__(self, keys, params: dict):
        self.keys = sorted(set(keys))
        self.p = params
        self.scorer = {"ratio": fuzz.ratio, "token_sort_ratio": fuzz.token_sort_ratio}[params.get("scorer", "token_sort_ratio")]
        self.ordered = bool(params.get("ordered", False))
        self._tokens = {k: content_tokens(k) for k in self.keys}

    def is_short(self, norm: str) -> bool:
        return len(norm) < self.p["short_max_chars"] or len(content_tokens(norm)) < self.p["short_min_tokens"]

    def match(self, queries: list[str]) -> FuzzyResult:
        long_queries = [q for q in queries if q and not self.is_short(q)]
        if not long_queries:
            return FuzzyResult(None, 0.0, "short_name")
        best: dict[str, float] = {}
        seen = rejected = 0
        for q in long_queries:
            qt = content_tokens(q)
            for key, score, _ in process.extract(q, self.keys, scorer=self.scorer, limit=10,
                                                 score_cutoff=self.p["min_score"]):
                seen += 1
                if aligned(qt, self._tokens[key], self.p["min_prefix"], self.p["token_ratio"], self.ordered):
                    best[key] = max(best.get(key, 0.0), float(score))
                else:
                    rejected += 1
        if not best:
            return FuzzyResult(None, 0.0, "rejected_by_token_guard" if seen else "no_candidate", seen, rejected)
        ranked = sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))
        if len(ranked) > 1 and ranked[1][1] >= ranked[0][1] - self.p["margin"]:
            return FuzzyResult(None, ranked[0][1], "ambiguous_margin", seen, rejected)
        return FuzzyResult(ranked[0][0], ranked[0][1], "fuzzy", seen, rejected)
