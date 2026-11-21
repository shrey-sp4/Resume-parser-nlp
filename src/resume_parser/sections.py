"""Stage 3: section detection (where each section starts and ends, raw labels).

Method chain: heading_vocabulary (config/section_aliases.json), heading_look
(font size, bold, caps, spacing), content_patterns (dates, degrees, citations).
All stubs for now.
"""

from __future__ import annotations

from pathlib import Path

from resume_parser.pipeline import run_with_fallbacks
from resume_parser.schema import Context, Method, MethodOutput, StageResult, not_implemented


def heading_vocabulary(lines) -> MethodOutput:
    return not_implemented()


def heading_look(lines) -> MethodOutput:
    return not_implemented()


def content_patterns(lines) -> MethodOutput:
    return not_implemented()


METHODS = (
    Method("heading_vocabulary", heading_vocabulary, stub=True),
    Method("heading_look", heading_look, stub=True),
    Method("content_patterns", content_patterns, stub=True),
)


def check_sections(blocks) -> float:
    """Personal block near the top, target sections found, few leaking lines.

    Placeholder: returns 0.0 until the detectors exist.
    """
    return 0.0


def run_sections(pdf_path: Path, ctx: Context) -> StageResult:
    return run_with_fallbacks(
        "stage3",
        METHODS,
        check_sections,
        ctx.thresholds["accept_threshold"]["stage3"],
        payload=ctx.results["stage2"].data,
    )
