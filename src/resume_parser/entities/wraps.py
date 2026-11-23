"""Wrapped lines inside a narrow column, joined before stage 5 reads the entries.

On pages that stage 1 reordered by columns (lines with column "left" or "right"), a date part,
the end of an institution or a venue often continues on the next line of the same column. Stage
5 would read that continuation as a separate line (a new entry, or a part left out). Here a
continuation line is joined to the line above it, in a copy of the stage 4 document, inside the
education, experience and publications sections only. Stages 1 to 4 and every other page stay as
they are: a line without a column field is never touched.

A line B joins the line A above it (or the line A has already absorbed) when all of these hold:
  - both carry column "left" or "right", the same column, on the same page; neither is a table
    cell and B is not the first line of a column
  - the vertical gap is at most column_wrap_gap_factor times the normal line gap of that column
    on that page (the median gap between its consecutive lines)
  - B does not start left of A (an outdent starts a new entry) and does not start with a list
    number or marker
and at least one of these:
  - A is unfinished: it ends with a comma, a dash, an open bracket or "&", or with a joining word
    (of, and, in, for, the, on, at, to)
  - B is a closing part: it starts in lower case, with a dash or a closing bracket, is only a year
    or a year range (or "Present"), or starts with pages or a volume
  - in publications, B starts with a venue lead (In, In:, Proc., Proceedings, Journal, Trans.)
The joined line keeps the first line's id and lists the others in joined_ids, as in stage 2.
"""

from __future__ import annotations

import copy
import re
import statistics

from resume_parser.entities.publications import _MARKER

SECTIONS = ("education", "experience", "publications")
COLUMNS = ("left", "right")
JOINING_WORDS = {"of", "and", "in", "for", "the", "on", "at", "to", "&"}
_UNFINISHED = re.compile(r"[,(\-–—&]$")
_CLOSING = re.compile(
    r"^(?:[-–—)\]]"
    r"|(?:19|20)\d{2}(?:\s*[-–—]\s*(?:(?:19|20)\d{2}|present|till date|now))?[\s.,;)]*$"
    r"|present\b|pp?\.\s*\d|vol\.?\s*\d)",
    re.IGNORECASE)
_VENUE_LEAD = re.compile(r"^(?:in\s*:|in\s+(?=[A-Z])|proc(?:eedings\b|\.)|journal\b|trans(?:actions\b|\.))",
                         re.IGNORECASE)
_MAX_GAP_PT = 40  # gaps above this are breaks between blocks, not line spacing


def _normal_gap(lines: list[dict], column: str, page: int) -> float | None:
    same = [l for l in lines if l.get("column") == column and l["page"] == page]
    gaps = [b["bbox"][1] - a["bbox"][3] for a, b in zip(same, same[1:])
            if 0 <= b["bbox"][1] - a["bbox"][3] < _MAX_GAP_PT]
    return statistics.median(gaps) if gaps else None


def is_continuation(a: dict, a_bottom: float, b: dict, normal_gap: float | None, section: str,
                    gap_factor: float) -> bool:
    """B continues A (see the module docstring). a_bottom: the bottom of the last line A absorbed."""
    if a.get("column") not in COLUMNS or b.get("column") != a.get("column"):
        return False
    if a["page"] != b["page"] or a.get("in_table") or b.get("in_table") or b.get("column_start"):
        return False
    gap = b["bbox"][1] - a_bottom
    if gap < 0 or (normal_gap is not None and gap > gap_factor * max(normal_gap, 1.0)):
        return False
    if b["bbox"][0] < a["bbox"][0] - 2:
        return False
    ta, tb = a["text"].rstrip(), b["text"].strip()
    if not ta or not tb or _MARKER.match(tb):
        return False
    unfinished = bool(_UNFINISHED.search(ta)) or ta.split()[-1].lower() in JOINING_WORDS
    closing = tb[:1].islower() or bool(_CLOSING.match(tb))
    venue = section == "publications" and bool(_VENUE_LEAD.match(tb))
    return unfinished or closing or venue


def join_column_wraps(document: dict, params: dict) -> dict:
    """A copy of the stage 4 document with column continuation lines joined (education,
    experience and publications sections). The input is not changed."""
    if not any(l.get("column") in COLUMNS for s in document.get("sections", []) for l in s["lines"]):
        return document
    factor = params["column_wrap_gap_factor"]
    doc = copy.deepcopy(document)
    for sec in doc.get("sections", []):
        if sec["name"] not in SECTIONS:
            continue
        out: list[dict] = []
        bottom: list[float] = []
        for line in sec["lines"]:
            if out and is_continuation(out[-1], bottom[-1], line,
                                       _normal_gap(sec["lines"], line.get("column"), line["page"]),
                                       sec["name"], factor):
                a = out[-1]
                a["text"] = a["text"].rstrip() + " " + line["text"].strip()
                a["joined_ids"] = list(a.get("joined_ids") or []) + [line["id"]] + list(line.get("joined_ids") or [])
                bottom[-1] = line["bbox"][3]
                continue
            out.append(dict(line))
            bottom.append(line["bbox"][3])
        sec["lines"] = out
    return doc
