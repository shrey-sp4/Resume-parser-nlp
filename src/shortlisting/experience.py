"""Experience calculator for shortlisting: years of academic OR industry experience against a
tier of 2, 5 or 10 years. Rule-based; reads the parser's JSON (or GT v2, the same format).

Input: one resume as parsed (``experience``: a list of {job_title, institution, dates,
experience_type}); ``dates`` is the date range as printed and is read with the parser's own
reader (resume_parser.field_norm.parse_dates). ``experience_type`` is academic, industry or
other; "other" or a missing type counts as neither and is reported.

Per entry of the chosen type (date_quality):
  full      start and end with a month, or a start with a month and "Present"/"current"
            (ongoing ends at ``today``, a parameter so results are repeatable)
  year_only both ends given but at least one only as a year
  start_only a start and no end (not ongoing)
  missing   no usable date
Fully dated entries make an exact interval (an end month counts in full: Jan-Dec is 12 months).
A partial entry is read with its widest available dates (a year from 1 January to 31 December;
a start with no end as that year or month only); it never counts in the fully dated total.

Intervals of the chosen type are merged before summing, so overlapping jobs are not counted twice.
  years_fully_dated  merged years of the fully dated entries
  years_with_partial merged years of every entry with a usable date (partial ones widest)
Status, against the tier T:
  meets         years_fully_dated >= T (certainty high when every counted entry is fully dated,
                else medium: more entries can only add years)
  likely_meets  years_with_partial >= T only because partial entries are counted, or every entry
                is fully dated and years_fully_dated is within 0.5 below T (certainty medium)
  does_not_meet every counted entry fully dated and years_fully_dated < T - 0.5 (certainty high)
  unknown       no usable date and no text statement, or below T while some dates are missing
                or partial: missing data is never read as zero experience (certainty low)
Fallback, only when no entry of the chosen type has a usable date: a plain statement in the
resume text ("8 years of experience", "over 5 years of teaching experience"), counted only when
a word of the chosen type is near it; a statement with no type word has type unknown and gives
status unknown. Source text_statement, certainty low.

Entries of another type only (at least one experience entry, none of the chosen type, and no
statement of the chosen type): does_not_meet, certainty low, reason no_entries_of_type. No
experience entries at all (and no statement): unknown, reason no_experience_entries.

The result lists the entries (index, type, counted, years, date_quality), never their text, and a
reason: ok, no_experience_entries, no_entries_of_type, no_usable_dates,
below_tier_with_missing_or_partial_dates, text_statement or statement_type_not_chosen.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

from resume_parser.field_norm import parse_dates

KINDS = ("academic", "industry")
TIERS = (2, 5, 10)
NEAR_TIER = 0.5

# "8 years of experience", "over 5 years of teaching experience", "10+ yrs industry experience"
STATEMENT = re.compile(
    r"(?P<q>\b(?:over|more\s+than|nearly|almost|about|around|approximately|approx\.?|close\s+to)\s+)?"
    r"(?<![\d.])(?P<n>\d{1,2}(?:\.\d)?)\s*(?P<plus>\+)?\s*(?:years?|yrs?\.?)\s+(?:of\s+)?"
    r"(?P<mid>(?:[A-Za-z\-]+\s+){0,3}?)experience\b", re.IGNORECASE)
TYPE_WORDS = {
    "academic": re.compile(r"\b(academic|academia|teaching|research|postdoctoral|post-doctoral|faculty|university)\b", re.I),
    "industry": re.compile(r"\b(industry|industrial|corporate|company|companies)\b", re.I),
}
NEAR_CHARS = 40


@dataclass
class Span:
    start: _dt.date
    end: _dt.date  # exclusive


def _month_start(year: int, month: int) -> _dt.date:
    return _dt.date(year, month, 1)


def _next_month(d: _dt.date) -> _dt.date:
    return _dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _point(text: str | None) -> tuple[int | None, int | None]:
    if not text:
        return None, None
    y, _, m = text.partition("-")
    return int(y), (int(m) if m else None)


def read_entry(dates: str, today: _dt.date) -> tuple[str, Span | None, Span | None]:
    """(date_quality, exact span or None, widest span or None) for one entry's dates as printed."""
    p = parse_dates(dates or "", today.year)
    sy, sm = _point(p["start"])
    ey, em = _point(p["end"])
    if sy is None:
        return "missing", None, None
    start_lo = _month_start(sy, sm or 1)
    if p["ongoing"]:
        end = today if today > start_lo else start_lo
        quality = "full" if sm else "year_only"
        span = Span(start_lo, end)
        return quality, (span if quality == "full" else None), span
    if ey is None:  # a start only: that month or that year, at the widest
        end = _next_month(start_lo) if sm else _dt.date(sy + 1, 1, 1)
        return "start_only", None, Span(start_lo, min(end, max(today, start_lo)))
    end_hi = _next_month(_month_start(ey, em)) if em else _dt.date(ey + 1, 1, 1)
    if end_hi <= start_lo:
        return "missing", None, None  # an end before the start is not a usable date
    widest = Span(start_lo, min(end_hi, max(today, start_lo)))
    if sm and em:
        return "full", widest, widest
    return "year_only", None, widest


def span_months(s: Span) -> float:
    """Calendar months from start to end (Jan 2015 to Jan 2016 is 12), days as a fraction of a month,
    so month-exact boundaries do not drift with leap years."""
    return (s.end.year - s.start.year) * 12 + (s.end.month - s.start.month) + (s.end.day - s.start.day) / 30.4375


def span_years(s: Span) -> float:
    return round(span_months(s) / 12, 2)


def merged_years(spans: list[Span]) -> float:
    """Years covered by the union of the spans (overlaps counted once)."""
    total = 0.0
    cur: Span | None = None
    for s in sorted(spans, key=lambda s: (s.start, s.end)):
        if cur is None or s.start > cur.end:
            if cur is not None:
                total += span_months(cur)
            cur = Span(s.start, s.end)
        else:
            cur.end = max(cur.end, s.end)
    if cur is not None:
        total += span_months(cur)
    return round(total / 12, 2)


def find_statement(text: str, kind: str) -> dict | None:
    """The first plain experience statement in text, with its type: kind when a word of that type
    is near, 'other' when only the other type's word is, else 'unknown'."""
    other = next(k for k in KINDS if k != kind)
    found = None
    for m in STATEMENT.finditer(text or ""):
        window = text[max(0, m.start() - NEAR_CHARS): m.end() + NEAR_CHARS]
        near = m.group("mid") + " " + window
        typ = kind if TYPE_WORDS[kind].search(near) else (other if TYPE_WORDS[other].search(near) else "unknown")
        q = (m.group("q") or "").strip().lower()
        item = {"years": float(m.group("n")), "type": typ,
                "qualifier": "at_least" if (m.group("plus") or q in ("over", "more than")) else
                             ("about" if q else "exact")}
        if typ == kind:
            return item
        found = found or item
    return found


def _status(years_full: float, years_any: float, all_full: bool, tier: float) -> tuple[str, str]:
    if years_full >= tier:
        return "meets", "high" if all_full else "medium"
    if all_full:
        if years_full >= tier - NEAR_TIER:
            return "likely_meets", "medium"
        return "does_not_meet", "high"
    if years_any >= tier:
        return "likely_meets", "medium"
    return "unknown", "low"  # below the tier, but some dates are missing or partial


def compute_experience(resume: dict, kind: str, tier: int, today: _dt.date | None = None,
                       text: str | None = None) -> dict:
    """The experience result for one resume (see the module docstring). ``text`` is the resume
    text, used only for the statement fallback."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}")
    today = today or _dt.date.today()
    entries, exact, widest = [], [], []
    unknown_type = 0
    for i, e in enumerate(resume.get("experience") or []):
        typ = e.get("experience_type") if e.get("experience_type") in KINDS else "unknown"
        unknown_type += typ == "unknown"
        quality, span, wide = read_entry(e.get("dates") or "", today)
        counted = typ == kind
        if counted:
            if span:
                exact.append(span)
            if wide:
                widest.append(wide)
        years = span_years(wide) if wide else None
        entries.append({"index": i, "type": typ, "counted": counted, "years": years, "date_quality": quality})
    counted = [e for e in entries if e["counted"]]
    result = {"kind": kind, "tier": tier, "today": today.isoformat(), "entries": entries,
              "entries_counted": len(counted), "entries_unknown_type": unknown_type,
              "years": None, "years_fully_dated": None, "years_with_partial": None,
              "status": "unknown", "certainty": "low", "source": "none",
              "reason": "no_experience_entries" if not entries else
                        ("no_entries_of_type" if not counted else "no_usable_dates")}
    if widest:
        years_full = merged_years(exact)
        years_any = merged_years(widest)
        all_full = all(e["date_quality"] == "full" for e in counted)
        status, certainty = _status(years_full, years_any, all_full, tier)
        result.update(years=years_full if all_full else years_any, years_fully_dated=years_full,
                      years_with_partial=years_any, status=status, certainty=certainty, source="entries",
                      reason="below_tier_with_missing_or_partial_dates" if status == "unknown" else "ok")
        return result
    statement = find_statement(text or "", kind)
    if statement:
        result["source"] = "text_statement"
        result["statement"] = statement
        result["reason"] = "text_statement" if statement["type"] == kind else "statement_type_not_chosen"
        if statement["type"] == kind:
            y = statement["years"]
            result["years"] = y
            if y >= tier:
                result["status"] = "meets"
            elif statement["qualifier"] != "exact" and y >= tier - 1 or y >= tier - NEAR_TIER:
                result["status"] = "likely_meets"
            else:
                result["status"] = "does_not_meet"
            result["certainty"] = "low"
            return result
    if entries and not counted:
        # experience is listed, none of it of the chosen type: low-certainty does_not_meet
        result.update(status="does_not_meet", certainty="low", source="entries", years=0.0,
                      reason="no_entries_of_type")
    return result
