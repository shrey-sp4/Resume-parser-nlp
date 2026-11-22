"""Stage 6: validation and scoring. Single method: validate_and_score.

The decision per resume comes from the four critical sections (name, education, experience,
publications) and is never silent:
  FAIL          stage 0 or 1 failed (the pipeline stopped; set when the sidecar is built, as
                stage 6 does not run then), or the name is empty (empty_name; the main JSON is
                still written)
  NEEDS_REVIEW  any of these reasons, for <s> in name, education, experience, publications:
                  missing_section:<s>          stage 4 found no such section (not for the name)
                  low_score:<s>                section score below section_score_threshold[s]
                  whole_section_one_entry:<s>  stage 5 fell back to one entry for the section
                  name_from_email, name_from_file_name   the name came from a fallback
                  no_entries:<s>               the section has lines but no entry
  ACCEPT        otherwise
Flags from earlier stages stay in the sidecar but do not decide on their own. Section scores
are in scoring.py; thresholds in config/thresholds.json (section_score_threshold, calibrated by
eval/calibrate_thresholds.py).
"""

from __future__ import annotations

from pathlib import Path

from resume_parser.schema import (
    Attempt,
    AttemptStatus,
    Context,
    Reason,
    StageResult,
    Status,
)
from resume_parser.scoring import found_sections, section_scores

ENTRY_SECTIONS = ("education", "experience", "publications")
NAME = "name"  # the personal unit is reported as the name


def _label(section: str) -> str:
    return NAME if section == "personal" else section


def decide(ctx: Context) -> tuple[str, list[Reason], dict]:
    """(decision, reasons, section scores)."""
    scores = section_scores(ctx)
    thresholds = ctx.thresholds["section_score_threshold"]
    stage5 = ctx.results.get("stage5")
    data = stage5.data if stage5 is not None and isinstance(stage5.data, dict) else {}
    units = stage5.per_unit if stage5 is not None else {}
    found = found_sections(ctx)
    reasons: list[Reason] = []

    personal = data.get("personal") if isinstance(data.get("personal"), dict) else {}
    if not (personal.get("candidate_name") or "").strip():
        reasons.append(Reason("empty_name", "", "personal"))
    name_method = units["personal"].method_used if "personal" in units else None
    if name_method in ("from_email", "from_file_name"):
        reasons.append(Reason(f"name_{name_method}", "", "personal"))

    for section, score in scores.items():
        label = _label(section)
        if section in ENTRY_SECTIONS and section not in found:
            reasons.append(Reason(f"missing_section:{label}", "", section))
            continue
        if score is not None and score < thresholds[section]:
            reasons.append(Reason(f"low_score:{label}", f"{score:.4f} < {thresholds[section]}", section))
        unit = units.get(section)
        if section in ENTRY_SECTIONS and unit is not None:
            if unit.method_used == "whole_section_one_entry":
                reasons.append(Reason(f"whole_section_one_entry:{label}", "", section))
            sec_data = data.get(section) if isinstance(data.get(section), dict) else {}
            if sec_data.get("lines_total", 0) > 0 and not sec_data.get("entries"):
                reasons.append(Reason(f"no_entries:{label}", "", section))

    if any(r.code == "empty_name" for r in reasons):
        decision = "FAIL"
    elif reasons:
        decision = "NEEDS_REVIEW"
    else:
        decision = "ACCEPT"
    reasons.sort(key=lambda r: r.code)
    return decision, reasons, scores


STATUS_OF = {"ACCEPT": Status.OK, "NEEDS_REVIEW": Status.WARN, "FAIL": Status.FAIL}


def run_validate(pdf_path: Path, ctx: Context) -> StageResult:
    decision, reasons, scores = decide(ctx)
    attempt = Attempt("validate_and_score", 1.0, AttemptStatus.ACCEPTED, tuple(reasons))
    return StageResult(
        stage="stage6",
        status=STATUS_OF[decision],
        data={"decision": decision, "decision_reasons": [r.code for r in reasons], "section_scores": scores},
        metrics={"reasons": len(reasons)},
        reasons=list(reasons),
        method_used="validate_and_score",
        methods_tried=[attempt],
    )
