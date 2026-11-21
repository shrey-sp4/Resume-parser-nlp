"""Stage 4: normalization layer. Builds the Canonical Document, the only thing stage 5 reads.

Each stage 3 block is one unit ("block_001", ...) and gets its own method choice:
  exact_alias_map             the normalised heading is an alias (the first block, which has
                              no heading, is the personal block)                 confidence 1.0
  fuzzy_alias_match           fuzz.ratio of the whole normalised heading against every alias
                              is at least fuzzy_min (thresholds.json)            confidence ratio/100
  content_guess               the block's content: citations -> publications, degree words ->
                              education, date ranges -> experience; always WARN
                              section_from_content                               confidence share
  keep_as_other_with_warning  "other", keeping the heading as original_name; always WARN
                              heading_kept_as_other                              confidence 0
The first method reaching the accept threshold wins, otherwise the most confident one.

Canonical Document:
  {"doc_id", "sections": [{"block_id", "name", "heading_raw", "original_name", "heading_line",
   "lines"}], "unassigned": []}
Lines are the stage 2 lines unchanged (id, page, bbox and the rest kept).

Document checks, added as reasons:
- every stage 2 line is in exactly one section (as a heading or a line; an inline heading
  split off a line counts as part of that line), in order; otherwise WARN
  lines_lost_or_duplicated
- section names are only personal, education, experience, publications, other
- target-looking lines (dates, degrees, citations) in "other" or unassigned at most leak_max of
  all target-looking lines; otherwise WARN target_lines_in_other
- each of personal, education, experience, publications that is missing gets the code
  missing_target_section:<name>: a WARN (and a flag) for education and experience,
  information only for personal and publications
"""

from __future__ import annotations

from pathlib import Path

from resume_parser.headings import TARGETS, content_guess, exact_alias, fuzzy_alias, line_kind, norm_heading
from resume_parser.pipeline import escalate, run_with_fallbacks
from resume_parser.schema import (
    CANONICAL_SECTIONS,
    Context,
    Method,
    MethodOutput,
    Reason,
    StageResult,
    Status,
    Unit,
    resume_id_for,
)


def _mapping(name: str, original: str | None, confidence: float, source: str) -> dict:
    return {"name": name, "original_name": original if name == "other" else None,
            "confidence": round(float(confidence), 4), "source": source}


def exact_alias_map(payload) -> MethodOutput:
    block, _p = payload
    if block["heading_line"] is None:
        return MethodOutput(_mapping("personal", None, 1.0, "position"))
    found = exact_alias(block["heading_raw"])
    if not found:
        return MethodOutput(None)
    return MethodOutput(_mapping(found[0], found[1], 1.0, "alias"))


def fuzzy_alias_match(payload) -> MethodOutput:
    block, p = payload
    if block["heading_line"] is None:
        return MethodOutput(None)
    found = fuzzy_alias(block["heading_raw"], p["fuzzy_min"])
    if not found:
        return MethodOutput(None)
    return MethodOutput(_mapping(found[0], found[1], found[2] / 100, "fuzzy"))


def content_guess_method(payload) -> MethodOutput:
    block, p = payload
    found = content_guess(block["lines"], p["content_min_share"])
    if not found:
        return MethodOutput(None)
    return MethodOutput(_mapping(found[0], None, found[1], "content"))


def keep_as_other_with_warning(payload) -> MethodOutput:
    block, _p = payload
    original = norm_heading(block["heading_raw"]) if block["heading_raw"] else "unknown"
    return MethodOutput(_mapping("other", original or "unknown", 0.0, "kept"))


METHODS = (
    Method("exact_alias_map", exact_alias_map),
    Method("fuzzy_alias_match", fuzzy_alias_match),
    Method("content_guess", content_guess_method, warn_code="section_from_content"),
    Method("keep_as_other_with_warning", keep_as_other_with_warning, warn_code="heading_kept_as_other"),
)


WARN_IF_MISSING = ("education", "experience")


def check_mapping(data: dict) -> float:
    return data["confidence"]


# ---------------------------------------------------------------- the document


def build_document(doc_id: str, blocks: list[dict], mappings: dict[str, dict]) -> dict:
    sections = []
    for block in blocks:
        m = mappings[block["block_id"]]
        sections.append({
            "block_id": block["block_id"],
            "name": m["name"],
            "heading_raw": block["heading_raw"],
            "original_name": m["original_name"],
            "heading_line": block["heading_line"],
            "lines": block["lines"],
        })
    return {"doc_id": doc_id, "sections": sections, "unassigned": []}


def document_reasons(document: dict, stage2_lines: list[dict], p: dict) -> list[tuple[Status, Reason]]:
    out: list[tuple[Status, Reason]] = []
    seen = []
    for sec in document["sections"]:
        # an inline heading is part of its content line, which keeps the original id
        if sec["heading_line"] is not None and not sec["heading_line"].get("inline_heading"):
            seen.append(sec["heading_line"]["id"])
        seen += [line["id"] for line in sec["lines"]]
    seen += [line["id"] for line in document["unassigned"]]
    if seen != [line["id"] for line in stage2_lines]:
        out.append((Status.WARN, Reason("lines_lost_or_duplicated", section="document")))
    bad_names = sorted({s["name"] for s in document["sections"]} - set(CANONICAL_SECTIONS))
    if bad_names:
        out.append((Status.WARN, Reason("unknown_section_name", ",".join(bad_names), "document")))

    target_lines = in_other = 0
    for sec in document["sections"]:
        for line in sec["lines"]:
            if line_kind(line["text"]) is not None:
                target_lines += 1
                in_other += sec["name"] == "other"
    in_other += sum(line_kind(line["text"]) is not None for line in document["unassigned"])
    if target_lines and in_other / target_lines > p["leak_max"]:
        out.append((Status.WARN, Reason("target_lines_in_other", f"{in_other} of {target_lines}", "other")))

    present = {s["name"] for s in document["sections"]}
    for name in TARGETS:
        if name not in present:
            status = Status.WARN if name in WARN_IF_MISSING else Status.OK
            out.append((status, Reason(f"missing_target_section:{name}", section=name)))
    return out


def run_normalize(pdf_path: Path, ctx: Context) -> StageResult:
    p = {**ctx.thresholds["stage4_normalize"],
         "content_min_share": ctx.thresholds["stage3_sections"]["content_min_share"]}
    stage3 = ctx.results["stage3"].data or {}
    blocks = stage3.get("blocks") or []
    units = [Unit(b["block_id"], (b, p), section=None) for b in blocks]
    result = run_with_fallbacks(
        "stage4",
        METHODS,
        check_mapping,
        ctx.thresholds["accept_threshold"]["stage4"],
        units=units,
    )
    document = build_document(resume_id_for(ctx.resume_file), blocks, result.data)
    stage2_lines = (ctx.results["stage2"].data or {}).get("lines") or []
    result.data = document
    result.metrics.update({
        "sections": len(document["sections"]),
        "by_name": {n: sum(s["name"] == n for s in document["sections"]) for n in CANONICAL_SECTIONS},
    })
    for status, reason in document_reasons(document, stage2_lines, p):
        result = escalate(result, status, reason)
    return result
