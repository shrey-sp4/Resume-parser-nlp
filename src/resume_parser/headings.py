"""Section headings and content patterns, shared by stage 3 (sections) and stage 4 (normalize).

Headings are compared in a normalised form: unicode NFKC, lower case, leading numbering
("2.", "iv)", "A.") removed, a trailing colon or dash and a trailing count such as "(5)"
removed, spaces collapsed. The alias map is config/section_aliases.json: four canonical
lists, plus "other" split into labels (skills, awards, ...) that become original_name.

Content patterns mark lines that look like they belong to a target section: citations
(publications), degree words (education) and date ranges (experience).
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from rapidfuzz import fuzz

from resume_parser.schema import load_config

TARGETS = ("personal", "education", "experience", "publications")

_NUMBERING = re.compile(r"^\s*(?:\d{1,2}|[ivx]{1,4}|[a-h])\s*[.)\]]\s*", re.IGNORECASE)
_TRAILING = re.compile(r"(?:\s*\(\s*\d+\s*\))?\s*[:\-–—]*\s*$")


def norm_heading(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).strip()
    text = _NUMBERING.sub("", text)
    text = _TRAILING.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" *•·.\t").lower()
    return text


@lru_cache(maxsize=1)
def alias_index() -> dict[str, tuple[str, str]]:
    """normalised alias -> (canonical name, original label)."""
    cfg = load_config("section_aliases.json")
    index: dict[str, tuple[str, str]] = {}
    for name in TARGETS:
        for alias in cfg.get(name, []):
            index.setdefault(norm_heading(alias), (name, name))
    for label, aliases in sorted((cfg.get("other") or {}).items()):
        for alias in aliases:
            index.setdefault(norm_heading(alias), ("other", label))
    index.pop("", None)
    return index


def exact_alias(text: str) -> tuple[str, str] | None:
    return alias_index().get(norm_heading(text))


def fuzzy_alias(text: str, minimum: float) -> tuple[str, str, float] | None:
    """Best alias by fuzz.ratio on the whole normalised heading, if at least minimum (0-100).
    Ties go to the alias that sorts first, so the result does not depend on dict order."""
    key = norm_heading(text)
    if not key:
        return None
    best = None
    for alias in sorted(alias_index()):
        score = fuzz.ratio(key, alias)
        if score >= minimum and (best is None or score > best[2]):
            best = (*alias_index()[alias], round(score, 2))
    return best


# ---------------------------------------------------------------- content patterns

_YEAR = r"(?:19|20)\d{2}"
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE_RANGE = re.compile(
    rf"(?:{_MONTH}\s*)?{_YEAR}\s*(?:-|–|—|to|till)\s*(?:(?:{_MONTH}\s*)?{_YEAR}|present|date|now|current|till date|today)",
    re.IGNORECASE,
)
DEGREE = re.compile(
    r"\b(?:ph\.?\s?d|d\.?\s?phil|m\.?\s?phil|m\.?\s?sc|b\.?\s?sc|m\.?\s?tech|b\.?\s?tech|m\.?\s?e\.|b\.?\s?e\.|"
    r"mba|m\.?\s?com|b\.?\s?com|m\.?\s?a\.|b\.?\s?a\.|bachelor|master|doctor of|post\s?graduate|diploma)",
    re.IGNORECASE,
)
CITATION = re.compile(
    r"\b(?:vol\.?|volume|pp\.?|doi|issn|isbn|journal|proceedings|conference|transactions|et al|springer|"
    r"elsevier|ieee|wiley)\b|\d+\s*\(\s*\d+\s*\)",
    re.IGNORECASE,
)
KIND_TO_SECTION = {"citation": "publications", "degree": "education", "date": "experience"}


def line_kind(text: str) -> str | None:
    """"citation", "degree", "date" or None. A citation needs some length, so a short line
    with the word journal is not one."""
    if len(text) >= 40 and CITATION.search(text):
        return "citation"
    if DEGREE.search(text):
        return "degree"
    if DATE_RANGE.search(text):
        return "date"
    return None


def content_guess(lines: list[dict], min_share: float) -> tuple[str, float] | None:
    """(section, share) from the dominant kind of line, when it covers min_share of the lines."""
    if not lines:
        return None
    kinds = [line_kind(line["text"]) for line in lines]
    counts = {k: kinds.count(k) for k in KIND_TO_SECTION}
    kind = max(counts, key=lambda k: (counts[k], -list(KIND_TO_SECTION).index(k)))
    share = counts[kind] / len(lines)
    if counts[kind] and share >= min_share:
        return KIND_TO_SECTION[kind], round(share, 4)
    return None
