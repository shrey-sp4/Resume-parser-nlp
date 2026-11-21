"""Field normalization for the values the GT format needs, and the text patterns behind them.

- degree and degree_level from config/degrees.json (longest match wins, kept as printed)
- institution from config/institutions.json (keywords, international patterns, a gazetteer)
- stream: the subject after "in", a dash, a colon or inside parentheses after the degree, or
  capitalised subject words right after it; in a table with no header row, the cell after the
  degree cell
- year and grade for the sidecar
- experience dates as printed (ranges, single dates, "Present"-style ends) and their parsed form
- experience_type from the institution: company endings -> industry, academic words or a known
  name -> academic, other industry words -> industry, otherwise other
- publication type is built with its stage
"""

from __future__ import annotations

import re
from functools import lru_cache

from resume_parser.schema import load_config

LEVELS = ("doctorate", "masters", "bachelors", "other")
YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
GRADE = re.compile(
    r"\b(?:c?gpa|cpi|sgpa|percentage|marks|grade|division)\b\s*[:\-]?\s*[\d.]+(?:\s*/\s*\d+)?\s*%?"
    r"|(?<!\d)\d{1,3}(?:\.\d+)?\s*%|\b(?:first|second|third)\s+(?:class|division)\b",
    re.IGNORECASE,
)
_SEPARATORS = re.compile(r",|;|\||\s[-–—]\s|\(|\)|:")


def _form_pattern(form: str) -> str:
    """Regex for one degree form: dots and spaces optional; short capitals as whole words."""
    letters = form.replace(".", "").replace(" ", "")
    if "." not in form and form.isupper() and len(letters) <= 3:
        return rf"(?-i:\b{re.escape(form)}\b)"
    parts = []
    for ch in form:
        if ch == ".":
            parts.append(r"\.?\s?")
        elif ch == " ":
            parts.append(r"\s+")
        else:
            parts.append(re.escape(ch))
    body = "".join(parts)
    return rf"(?<![A-Za-z]){body}(?![A-Za-z])"


@lru_cache(maxsize=1)
def _degree_table() -> list[tuple[str, re.Pattern]]:
    cfg = load_config("degrees.json")
    table = []
    for level in (*LEVELS, "school_level"):
        for form in cfg.get(level, []):
            table.append((level, re.compile(_form_pattern(form), re.IGNORECASE)))
    return table


def find_degree(text: str, include_school: bool = False) -> tuple[str, str, int, int] | None:
    """(degree as printed, level, start, end) of the longest degree form in the text, earliest
    on a tie. School-level forms only with include_school."""
    best = None
    for level, pat in _degree_table():
        if level == "school_level" and not include_school:
            continue
        for m in pat.finditer(text):
            printed = m.group(0).strip()
            if not printed:
                continue
            key = (len(printed), -m.start())
            if best is None or key > best[0]:
                best = (key, (printed.rstrip(" "), level, m.start(), m.start() + len(printed)))
    return best[1] if best else None


def degree_level(degree_text: str) -> str:
    found = find_degree(degree_text)
    return found[1] if found else "other"


def is_school_level(text: str) -> bool:
    """A school-level line: a school form and no higher degree."""
    if find_degree(text):
        return False
    school = find_degree(text, include_school=True)
    return school is not None and school[1] == "school_level"


@lru_cache(maxsize=1)
def _institution_patterns() -> tuple[re.Pattern, list[re.Pattern]]:
    cfg = load_config("institutions.json")
    words = [re.escape(w) for w in cfg.get("academic_keywords", []) + cfg.get("international_patterns", [])]
    keyword = re.compile(r"(?<![A-Za-z])(?:" + "|".join(words) + r")(?![A-Za-z])", re.IGNORECASE)
    names = [re.compile(r"(?<![A-Za-z])" + re.escape(n) + r"(?![A-Za-z])", re.IGNORECASE)
             for n in sorted(cfg.get("known_institutions", []), key=len, reverse=True)]
    return keyword, names


def has_institution(text: str) -> bool:
    keyword, names = _institution_patterns()
    return bool(keyword.search(text)) or any(p.search(text) for p in names)


def _clean_segment(seg: str) -> str:
    seg = YEAR.sub(" ", GRADE.sub(" ", seg))
    seg = re.sub(r"^\s*(?:from|at|in)\s+", "", seg, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", seg).strip(" .-–—,;:")


def find_institution(text: str, skip: tuple[int, int] | None = None) -> str:
    """The first segment (cut at commas, dashes, parentheses, bars) that holds an institution
    keyword or a known name, cleaned of years and grades, as printed. skip: a degree span to
    leave out."""
    if skip:
        text = text[: skip[0]] + " " * (skip[1] - skip[0]) + text[skip[1]:]
    for seg in _SEPARATORS.split(text):
        if seg and has_institution(seg):
            # "Physics from IIT Bombay": keep the part after the last "from" / "at"
            parts = re.split(r"\s(?:from|at)\s", seg, flags=re.IGNORECASE)
            tail = [p for p in parts if has_institution(p)]
            cleaned = _clean_segment(tail[-1] if tail else seg)
            if cleaned:
                return cleaned
    return ""


_STREAM_START = re.compile(r"^\s*(?:(?:in|of)\s+|[-–—:]\s*|\(\s*)", re.IGNORECASE)
_STREAM_STOP = re.compile(r"[,;|)(]|\s[-–—]\s|(?<!\d)(?:19|20)\d{2}(?!\d)")


def find_stream(text: str, degree_end: int) -> str:
    """The subject after the degree: after "in"/"of", a dash, a colon or an opening parenthesis.
    It stops at a separator, a year, a grade or an institution keyword."""
    rest = text[degree_end:]
    m = _STREAM_START.match(rest)
    if not m:
        return _plain_stream(rest)
    rest = rest[m.end():]
    cut = _STREAM_STOP.search(rest)
    stream = rest[: cut.start()] if cut else rest
    keyword, _names = _institution_patterns()
    k = keyword.search(stream)
    if k:
        stream = stream[: k.start()]
    stream = GRADE.sub(" ", stream)
    stream = re.sub(r"\s+(?:from|at)\s*$", "", stream.strip(), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", stream).strip(" .-–—,;:")


_PLAIN_STOP = re.compile(r"[,;|)(:]|\s[-–—]\s|\s(?:from|at)\s|(?<!\d)(?:19|20)\d{2}(?!\d)", re.IGNORECASE)
_FIRM_STOP = re.compile(r",|\s(?:from|at)\s|(?<!\d)(?:19|20)\d{2}(?!\d)", re.IGNORECASE)
_SUBJECT_WORD = re.compile(r"^(?:[A-Z][A-Za-z&.'\-]*|and|of|&|in)$")


def _plain_stream(rest: str) -> str:
    """A subject written right after the degree with only a space ("B.Tech. Computer Science,
    ..."): capitalised words up to a comma, "from"/"at", a separator or a year; at most 5
    words; none when an institution word comes first (the words start an institution name).
    Also none when more capitalised words follow on the line before the next comma, "from"/"at",
    year or institution word: then the words may belong to a longer name, so the stream is
    left empty rather than guessed."""
    if not rest[:1].isspace():
        return ""
    cut = _PLAIN_STOP.search(rest)
    phrase = (rest[: cut.start()] if cut else rest).strip()
    if cut and not _FIRM_STOP.fullmatch(cut.group(0)):
        after = rest[cut.end():]
        firm = _FIRM_STOP.search(after)
        window = after[: firm.start()] if firm else after
        keyword, _names = _institution_patterns()
        k = keyword.search(window)
        if k:
            window = window[: k.start()]
        if re.search(r"(?<![A-Za-z])[A-Z][a-z]", window):
            return ""
    return phrase.strip(" .-–—,;:") if is_subject_phrase(phrase) else ""


def is_subject_phrase(phrase: str, max_words: int = 5) -> bool:
    """1 to max_words capitalised subject words (plus "and", "of", "in", "&"), with no digits,
    grade or institution word."""
    words = phrase.split()
    if not words or len(words) > max_words or not words[0][:1].isupper():
        return False
    if re.search(r"\d", phrase) or has_institution(phrase) or GRADE.search(phrase):
        return False
    return all(_SUBJECT_WORD.match(w) for w in words)


def stream_from_next_cell(cell_text: str) -> str:
    """The stream in a table without a header row: the cell right after the degree cell, or its
    part before the first comma or semicolon, when that is a subject phrase."""
    first = re.split(r"[,;]", cell_text, maxsplit=1)[0].strip(" .-–—:")
    return first if is_subject_phrase(first, max_words=6) else ""


def find_years(text: str) -> list[str]:
    return YEAR.findall(text)


def find_grade(text: str) -> str:
    m = GRADE.search(text)
    return m.group(0).strip() if m else ""


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
# one point in time: "Jul 2023", "July, 2023", "07/2023", "2023", "Jul'19", "Jul 19", "'19"
# also "01/07/2019", "01-07-2019", "01.07.2019", "July 1, 2019", "1st July 2019"
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_POINT = (rf"(?:(?:(?<!\d){_DAY}\s+{_MONTH}[\s,.\-]*|{_MONTH}\s+{_DAY},?\s+|{_MONTH}[\s,.\-]*"
          rf"|(?<!\d)\d{{1,2}}\s*[/.\-]\s*\d{{1,2}}\s*[/.\-]\s*|(?<!\d)\d{{1,2}}\s*[/.\-]\s*)?(?<!\d)(?:19|20)\d{{2}}(?!\d)"
          rf"|{_MONTH}\s*['’`]\s*\d{{2}}(?!\d)|(?<![A-Za-z]){_MONTH}\s+\d{{2}}(?![\d/:,])|['’`]\d{{2}}(?!\d))")
_PRESENT = r"(?:present|current|till\s+date|till\s+now|to\s+date|todate|tilldate|date|now|ongoing|continuing|today)"
_RANGE_SEP = r"\s*(?:-|–|—|−|~|\bto\b|\btill\b|\buntil\b)\s*"
# a range ends in another point, a "Present"-style word, or two digits after a year ("2019-20")
DATE_SPAN = re.compile(rf"(?<![A-Za-z]){_POINT}(?:{_RANGE_SEP}(?:{_POINT}|{_PRESENT}\b|(?<=[-–—])\d{{2}}(?![\d/])))?",
                       re.IGNORECASE)
_POINT_ONLY = re.compile(rf"^{_POINT}$", re.IGNORECASE)
_PRESENT_ONLY = re.compile(rf"^{_PRESENT}$", re.IGNORECASE)


def find_dates(text: str) -> tuple[str, int, int] | None:
    """(dates as printed, start, end): the first date range, else the first single date with a
    4-digit year or a month. A bare 2-digit number is never a date."""
    best = None
    for m in DATE_SPAN.finditer(text):
        span = m.group(0).strip()
        has_range = bool(re.search(_RANGE_SEP + r"\S", span, re.IGNORECASE)) and not _POINT_ONLY.match(span)
        strong = has_range or YEAR.search(span) or re.match(_MONTH, span, re.IGNORECASE)
        if not strong:
            continue
        if has_range:
            return span, m.start(), m.start() + len(m.group(0).rstrip())
        if best is None:
            best = (span, m.start(), m.start() + len(m.group(0).rstrip()))
    return best


def _parse_point(text: str, current_year: int) -> tuple[str | None, int | None]:
    t = text.strip().lower()
    if not t or _PRESENT_ONLY.match(t):
        return None, None
    month = next((k + 1 for k, m in enumerate(_MONTHS) if re.search(rf"(?<![a-z]){m}", t)), None)
    y4 = YEAR.search(t)
    if y4:
        year = int(y4.group(0))
        nums = re.match(r"^(\d{1,2})\s*[/.\-]\s*(?:(\d{1,2})\s*[/.\-])?", t)
        if month is None and nums:
            m = int(nums.group(2) or nums.group(1))  # dd/mm/yyyy: the second number is the month
            month = m if 1 <= m <= 12 else None
    else:
        y2 = re.search(r"(\d{2})(?!\d)", t)
        if not y2:
            return None, None
        yy = int(y2.group(1))
        year = 2000 + yy if 2000 + yy <= current_year + 6 else 1900 + yy
    return (f"{year:04d}-{month:02d}" if month else f"{year:04d}"), year


def parse_dates(printed: str, current_year: int) -> dict:
    """{"start", "end", "start_year", "end_year", "ongoing"} from dates as printed. "Present"-style
    ends give end None and ongoing True; a single date gives start only."""
    text = (printed or "").strip()
    first = re.match(rf"^{_POINT}", text, re.IGNORECASE)  # "01-07-2019 - ..." : the whole first date
    if first and first.end() < len(text):
        sep = re.match(rf"^{_RANGE_SEP}", text[first.end():], re.IGNORECASE)
        parts = [text[: first.end()], text[first.end() + sep.end():]] if sep else [text]
    else:
        parts = [text]
    start, start_year = _parse_point(parts[0], current_year)
    end = end_year = None
    ongoing = False
    if len(parts) == 2:
        if _PRESENT_ONLY.match(parts[1].strip()):
            ongoing = True
        else:
            end, end_year = _parse_point(parts[1], current_year)
    return {"start": start, "end": end, "start_year": start_year, "end_year": end_year, "ongoing": ongoing}


@lru_cache(maxsize=1)
def _industry_patterns() -> tuple[re.Pattern, re.Pattern]:
    words = load_config("institutions.json").get("industry_keywords", [])
    suffix_words = {"pvt", "private limited", "ltd", "limited", "inc", "llc", "llp", "corporation", "corp"}

    def pat(ws):
        return re.compile(r"(?<![A-Za-z])(?:" + "|".join(re.escape(w) for w in ws) + r")(?![A-Za-z])", re.IGNORECASE)
    return pat(sorted(w for w in words if w in suffix_words) or ["pvt"]), pat(sorted(w for w in words if w not in suffix_words) or ["labs"])


def experience_type(institution_text: str) -> str:
    """academic / industry / other from the institution as printed. A company ending (Pvt, Ltd,
    Inc, LLC, Corp) means industry; then an academic keyword or a known institution means academic;
    then another industry word (Technologies, Labs, Solutions, ...) means industry; otherwise other."""
    text = institution_text or ""
    suffix, industry = _industry_patterns()
    if suffix.search(text):
        return "industry"
    if has_institution(text):
        return "academic"
    if industry.search(text):
        return "industry"
    return "other"


def publication_type(heading: str, venue: str) -> str:
    raise NotImplementedError("built together with publication extraction")
