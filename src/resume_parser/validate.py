"""Stage 6: validation and scoring. Single method: validate_and_score.

Decision for now: ACCEPT when no earlier stage raised a flag, otherwise
NEEDS_REVIEW. Section scores against config/thresholds.json come later.
"""

from __future__ import annotations

from pathlib import Path

from resume_parser.pipeline import run_with_fallbacks
from resume_parser.schema import Context, Method, MethodOutput, StageResult
from resume_parser.scoring import section_scores


def validate_and_score(ctx: Context) -> MethodOutput:
    decision = "NEEDS_REVIEW" if ctx.flags else "ACCEPT"
    return MethodOutput({"decision": decision, "section_scores": section_scores(ctx)})


def check_decision(data: dict) -> float:
    return 1.0 if data.get("decision") in ("ACCEPT", "NEEDS_REVIEW") else 0.0


def run_validate(pdf_path: Path, ctx: Context) -> StageResult:
    return run_with_fallbacks(
        "stage6",
        (Method("validate_and_score", validate_and_score),),
        check_decision,
        ctx.thresholds["accept_threshold"]["stage6"],
        payload=ctx,
    )
