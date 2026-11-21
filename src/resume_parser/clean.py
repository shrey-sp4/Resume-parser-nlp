"""Stage 2: cleaning (headers/footers, bullets, wrapped lines, hyphenation, unicode).

Method chain: full_clean, conservative_clean (glyphs and whitespace only),
raw_lines (no change). All stubs for now.
"""

from __future__ import annotations

from pathlib import Path

from resume_parser.pipeline import run_with_fallbacks
from resume_parser.schema import Context, Method, MethodOutput, StageResult, not_implemented


def full_clean(lines) -> MethodOutput:
    return not_implemented()


def conservative_clean(lines) -> MethodOutput:
    return not_implemented()


def raw_lines(lines) -> MethodOutput:
    return not_implemented()


METHODS = (
    Method("full_clean", full_clean, stub=True),
    Method("conservative_clean", conservative_clean, stub=True),
    Method("raw_lines", raw_lines, stub=True),
)


def check_clean(lines) -> float:
    """Share of letters and digits kept (excluding removed headers, footers, glyphs).

    Placeholder: returns 0.0 until the cleaners exist.
    """
    return 0.0


def run_clean(pdf_path: Path, ctx: Context) -> StageResult:
    return run_with_fallbacks(
        "stage2",
        METHODS,
        check_clean,
        ctx.thresholds["accept_threshold"]["stage2"],
        payload=ctx.results["stage1"].data,
    )
