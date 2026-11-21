"""Per-section scores: weighted share of the section's checks that passed.

Not built yet. Scores are None until the stage 5 checks exist.
"""

from __future__ import annotations

from resume_parser.schema import TARGET_SECTIONS, Context


def section_scores(ctx: Context) -> dict[str, float | None]:
    return {section: None for section in TARGET_SECTIONS}
