"""Stage 5: entity extraction, with the method chosen per section.

Reads only the Canonical Document from stage 4.

Units, in this order: personal, education, experience, publications.
- personal: top_lines, from_email, from_file_name (entities/personal.py).
- education: anchor_split, table_row_split, paragraph_split, whole_section_one_entry
  (entities/education.py).
- experience: date_anchor_split, table_row_split, paragraph_split, whole_section_one_entry
  (entities/experience.py).
- publications: marker_split, hanging_indent_split, paragraph_split, whole_section_one_entry
  (entities/publications.py).

What each unit returns as data (this is what writer.build_main and build_sidecar read):
- personal:      {"candidate_name": str, "personal_extra": {...}, "name_source": {page, lines}}
- education:     {"entries": [main-format education entries], "sides": [provenance per entry],
                  "excluded": [school-level entries], ...}
- experience:    {"entries": [main-format experience entries], "sides": [provenance per entry],
                  "parsed_dates": [{"start", "end"} per entry], ...}
- publications:  {"entries": [main-format publication entries], "sides": [provenance per entry],
                  "stated_totals": {"journal", "conference", "total"}, ...}
"""

from __future__ import annotations

from pathlib import Path

from resume_parser.entities import education as edu
from resume_parser.entities import experience as exp
from resume_parser.entities import publications as pub
from resume_parser.entities.personal import NAME_METHODS, check_personal, personal_payload
from resume_parser.pipeline import run_with_fallbacks
from resume_parser.schema import TARGET_SECTIONS, Context, StageResult, Unit

# every section brings its own chain; this one is only the stage-level default
SPLIT_METHODS = pub.SPLIT_METHODS


def run_entities(pdf_path: Path, ctx: Context) -> StageResult:
    document = ctx.results["stage4"].data or {"sections": []}
    by_section = ctx.thresholds["accept_threshold_by_section"]
    params = ctx.thresholds["stage5_entities"]
    stage4 = ctx.results["stage4"]
    by_content = frozenset(b for b, u in stage4.per_unit.items() if u.method_used == "content_guess")
    units = []
    for section in TARGET_SECTIONS:
        if section == "personal":
            payload, methods = personal_payload(document, ctx.resume_file, params), NAME_METHODS
            check = check_personal
        elif section == "education":
            payload, methods = edu.education_payload(document, params, by_content), edu.SPLIT_METHODS
            check = lambda data, p=params: edu.check_education(data, p)  # noqa: E731
        elif section == "experience":
            payload, methods = exp.experience_payload(document, params, by_content), exp.SPLIT_METHODS
            check = lambda data, p=params: exp.check_experience(data, p)  # noqa: E731
        else:
            payload, methods = pub.publications_payload(document, params), pub.SPLIT_METHODS
            check = lambda data, p=params: pub.check_publications(data, p)  # noqa: E731
        units.append(Unit(unit_id=section, payload=payload, methods=methods,
                          accept_threshold=by_section[section], section=section, check=check))
    return run_with_fallbacks(
        "stage5",
        SPLIT_METHODS,
        lambda data, p=params: pub.check_publications(data, p),
        ctx.thresholds["accept_threshold"]["stage5"],
        units=units,
    )
