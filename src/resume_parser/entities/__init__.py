"""Stage 5: entity extraction, with the method chosen per section.

Reads only the Canonical Document from stage 4.

Units, in this order: personal, education, experience, publications.
- personal: top_lines, from_email, from_file_name (entities/personal.py).
- education: anchor_split, table_row_split, paragraph_split, whole_section_one_entry
  (entities/education.py).
- experience, publications: the same split method names, still stubs.

What each unit returns as data (this is what writer.build_main and build_sidecar read):
- personal:      {"candidate_name": str, "personal_extra": {...}, "name_source": {page, lines}}
- education:     {"entries": [main-format education entries], "sides": [provenance per entry],
                  "excluded": [school-level entries], ...}
- experience:    {"entries": [main-format experience entries]}
- publications:  {"entries": [...], "stated_totals": {"journal", "conference", "total"}}
"""

from __future__ import annotations

from pathlib import Path

from resume_parser.entities import education as edu
from resume_parser.entities.personal import NAME_METHODS, check_personal, personal_payload
from resume_parser.pipeline import run_with_fallbacks
from resume_parser.schema import (
    TARGET_SECTIONS,
    Context,
    Method,
    MethodOutput,
    StageResult,
    Unit,
    not_implemented,
)


def anchor_split(payload) -> MethodOutput:
    """Split on anchors: degree words, date ranges, numbering, years."""
    return not_implemented()


def table_row_split(payload) -> MethodOutput:
    """One entry per table row."""
    return not_implemented()


def paragraph_split(payload) -> MethodOutput:
    """Split on blank lines."""
    return not_implemented()


def whole_section_one_entry(payload) -> MethodOutput:
    """Last resort: the whole section as one entry."""
    return not_implemented()


SPLIT_METHODS = (
    Method("anchor_split", anchor_split, stub=True),
    Method("table_row_split", table_row_split, stub=True),
    Method("paragraph_split", paragraph_split, stub=True),
    Method("whole_section_one_entry", whole_section_one_entry, warn_code="whole_section_one_entry", stub=True),
)


def check_entries(data) -> float:
    """Coverage and per-section entry checks (design section 4, stage 5).

    Placeholder: returns 0.0 until the splitters exist.
    """
    return 0.0


def run_entities(pdf_path: Path, ctx: Context) -> StageResult:
    document = ctx.results["stage4"].data or {"sections": []}
    by_section = ctx.thresholds["accept_threshold_by_section"]
    params = ctx.thresholds["stage5_entities"]
    units = []
    for section in TARGET_SECTIONS:
        if section == "personal":
            payload, methods = personal_payload(document, ctx.resume_file, params), NAME_METHODS
            check = check_personal
        elif section == "education":
            stage4 = ctx.results["stage4"]
            by_content = frozenset(b for b, u in stage4.per_unit.items() if u.method_used == "content_guess")
            payload, methods = edu.education_payload(document, params, by_content), edu.SPLIT_METHODS
            check = lambda data, p=params: edu.check_education(data, p)  # noqa: E731
        else:
            payload = {"section": section, "document": document, "resume_file": ctx.resume_file}
            methods, check = SPLIT_METHODS, None
        units.append(Unit(unit_id=section, payload=payload, methods=methods,
                          accept_threshold=by_section[section], section=section, check=check))
    return run_with_fallbacks(
        "stage5",
        SPLIT_METHODS,
        check_entries,
        ctx.thresholds["accept_threshold"]["stage5"],
        units=units,
    )
