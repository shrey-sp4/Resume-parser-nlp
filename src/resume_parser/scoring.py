"""Per-section scores for stage 6, label-free: the check score of the method stage 5 chose.

- personal (the name): the personal unit (0.5 for a valid name + 0.5 for an email or phone)
- education, experience, publications: their stage 5 units
A section that stage 4 did not find gets None; a found section whose methods all gave no
output gets 0.0.
"""

from __future__ import annotations

from resume_parser.schema import TARGET_SECTIONS, Context


def found_sections(ctx: Context) -> set[str]:
    stage4 = ctx.results.get("stage4")
    data = stage4.data if stage4 is not None and isinstance(stage4.data, dict) else {}
    return {s["name"] for s in data.get("sections", []) if s.get("lines")}


def section_scores(ctx: Context) -> dict[str, float | None]:
    stage5 = ctx.results.get("stage5")
    found = found_sections(ctx)
    scores: dict[str, float | None] = {}
    for section in TARGET_SECTIONS:
        unit = stage5.per_unit.get(section) if stage5 is not None else None
        if section != "personal" and section not in found:
            scores[section] = None
            continue
        if unit is None:
            scores[section] = None
            continue
        chosen = next((a for a in unit.methods_tried if a.method == unit.method_used), None)
        scores[section] = round(chosen.score, 4) if chosen is not None and chosen.score is not None else 0.0
    return scores
