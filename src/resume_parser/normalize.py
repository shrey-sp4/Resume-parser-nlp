"""Stage 4: normalization layer. Builds the Canonical Document read by stage 5.

Method chain: exact_alias_map (config/section_aliases.json), fuzzy_alias_match
(rapidfuzz), keep_as_other_with_warning. All stubs for now.
"""

from __future__ import annotations

from pathlib import Path

from resume_parser.pipeline import run_with_fallbacks
from resume_parser.schema import Context, Method, MethodOutput, StageResult, not_implemented


def exact_alias_map(blocks) -> MethodOutput:
    return not_implemented()


def fuzzy_alias_match(blocks) -> MethodOutput:
    return not_implemented()


def keep_as_other_with_warning(blocks) -> MethodOutput:
    return not_implemented()


METHODS = (
    Method("exact_alias_map", exact_alias_map, stub=True),
    Method("fuzzy_alias_match", fuzzy_alias_match, stub=True),
    Method("keep_as_other_with_warning", keep_as_other_with_warning, warn_code="heading_kept_as_other", stub=True),
)


def check_canonical(document) -> float:
    """Every line in exactly one section, order kept, no loss or duplication.

    Placeholder: returns 0.0 until the normalizer exists.
    """
    return 0.0


def run_normalize(pdf_path: Path, ctx: Context) -> StageResult:
    return run_with_fallbacks(
        "stage4",
        METHODS,
        check_canonical,
        ctx.thresholds["accept_threshold"]["stage4"],
        payload=ctx.results["stage3"].data,
    )
