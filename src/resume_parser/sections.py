"""Stage 3: section detection (where each section starts and ends, raw labels).

Input: the stage 2 lines. Output: blocks in reading order. The first block (before any
heading) is the personal block; every other block starts at a heading line.

Heading signals (thresholds.json, stage3_sections, to calibrate):
- vocabulary: the whole line, normalised, is an alias in config/section_aliases.json, or
  has fuzz.ratio of at least stage4_normalize.fuzzy_min with one (a misspelt heading).
  Table cells may be vocabulary headings (CVs laid out as a two-column table), but only by an
  exact alias: header cells such as "Degree" are too close to aliases for fuzzy matching.
- look: a short line (at most heading_max_words words and heading_max_chars characters, no
  full stop at the end, not a table cell, not itself a date, degree or citation line) with at
  least look_signals_min of: font at least font_ratio x the body font size, bold share at
  least bold_min, all capitals (caps_min of its letters), white space above of at least
  space_above_ratio x its line height; one of them must be the larger font or the capitals,
  so a bold entry title at body size (a job title) is not taken for a heading. Look headings
  are never taken from the first name_zone_lines lines of page 1, so the name line stays in
  the personal block.

Inline headings: a line (not a table cell) that starts with an alias followed by a colon or
a spaced dash, with text after it ("Experience: 25 years ..."), is split first into a heading
line (id "<id>_h", inline_heading True) and a content line that keeps the original id.

Methods, in order (the first whose score reaches the accept threshold wins):
  heading_vocabulary  split at vocabulary headings only
  heading_look        vocabulary and look headings
  content_patterns    as heading_look, and a block whose heading is unknown gets a guess from
                      its content (citations -> publications, degree words -> education, date
                      ranges -> experience)

Score (label-free): 0 when no education, experience or publications block is found;
otherwise 1 - the share of target-looking lines that sit where they do not belong: a citation
outside publications, a degree line outside education, a date range outside education,
experience and publications. Lines in the first personal_free_lines lines of the personal
block are never counted. A method that does not split at look headings (heading_vocabulary)
has its score halved while unknown heading-like lines remain, so it cannot be accepted when
the document has headings it does not know.

Unknown heading-like lines (look headings that are not aliases) are listed by id.
"""

from __future__ import annotations

import re
import statistics
from pathlib import Path

from resume_parser.headings import content_guess, exact_alias, fuzzy_alias, line_kind
from resume_parser.pipeline import escalate, run_with_fallbacks
from resume_parser.schema import Context, Method, MethodOutput, Reason, StageResult, Status

TARGET_BLOCKS = ("education", "experience", "publications")
# where each kind of target-looking line belongs
BELONGS = {"citation": {"publications"}, "degree": {"education"},
           "date": {"education", "experience", "publications"}}


# ---------------------------------------------------------------- heading signals


def _body_size(lines: list[dict]) -> float:
    sizes = []
    for line in lines:
        sizes += [float(line.get("font_size") or 0.0)] * max(1, len(line["text"]))
    sizes = [s for s in sizes if s > 0]
    return float(statistics.median(sizes)) if sizes else 0.0


def look_signals(line: dict, prev: dict | None, body: float, p: dict) -> int:
    """Number of look signals, or 0 when neither the larger font nor the capitals is there."""
    letters = [ch for ch in line["text"] if ch.isalpha()]
    caps = len(letters) >= 3 and sum(ch.isupper() for ch in letters) / len(letters) >= p["caps_min"]
    big = body > 0 and (line.get("font_size") or 0.0) >= p["font_ratio"] * body
    bold = (line.get("bold") or 0.0) >= p["bold_min"]
    space = False
    if prev is not None and prev["page"] == line["page"]:
        height = max(1.0, line["bbox"][3] - line["bbox"][1])
        space = line["bbox"][1] - prev["bbox"][3] >= p["space_above_ratio"] * height
    if not (big or caps):
        return 0
    return int(caps) + int(big) + int(bold) + int(space)


def is_look_heading(line: dict, prev: dict | None, body: float, p: dict, index_on_page1: int | None) -> bool:
    text = line["text"].strip()
    if line.get("in_table") or not any(ch.isalpha() for ch in text):
        return False
    if index_on_page1 is not None and index_on_page1 < p["name_zone_lines"]:
        return False
    if len(text) > p["heading_max_chars"] or len(text.split()) > p["heading_max_words"] or text.endswith("."):
        return False
    if line_kind(text) is not None:
        return False
    return look_signals(line, prev, body, p) >= p["look_signals_min"]


def find_headings(lines: list[dict], p: dict, use_look: bool) -> tuple[dict[int, tuple], list[str]]:
    """{line index: (guess, label, source)} and the ids of unknown heading-like lines."""
    body = _body_size(lines)
    headings: dict[int, tuple] = {}
    unknown: list[str] = []
    page1_index = 0
    for i, line in enumerate(lines):
        prev = lines[i - 1] if i else None
        on_page1 = line["page"] == 1
        short = len(line["text"].split()) <= p["heading_max_words"] and len(line["text"]) <= p["heading_max_chars"]
        alias = exact_alias(line["text"]) if short else None
        # fuzzy only outside tables: header cells such as "Degree" are too close to aliases
        fuzzy = None if alias or not short or line.get("in_table") or line_kind(line["text"])             else fuzzy_alias(line["text"], p["fuzzy_min"])
        if alias:
            headings[i] = (alias[0], alias[1], "alias")
        elif fuzzy:
            headings[i] = (fuzzy[0], fuzzy[1], "fuzzy")
        elif is_look_heading(line, prev, body, p, page1_index if on_page1 else None):
            unknown.append(line["id"])
            if use_look:
                headings[i] = (None, None, "look")
        if on_page1:
            page1_index += 1
    return headings, unknown


# ---------------------------------------------------------------- inline headings

_INLINE = re.compile(r"^(?P<head>[^:]{2,60}?)\s*(?::|\s[-–—](?=\s))\s*(?P<rest>\S.*)$")


def split_inline_headings(lines: list[dict]) -> list[dict]:
    """Split "Alias: content" lines into a heading line and a content line (see above)."""
    out = []
    for line in lines:
        m = None if line.get("in_table") else _INLINE.match(line["text"])
        if m and exact_alias(m.group("head")):
            out.append(dict(line, id=f"{line['id']}_h", text=m.group("head").strip(), inline_heading=True))
            out.append(dict(line, text=m.group("rest").strip()))
        else:
            out.append(line)
    return out


# ---------------------------------------------------------------- blocks


def build_blocks(lines: list[dict], headings: dict[int, tuple], p: dict, use_content: bool) -> list[dict]:
    blocks = [{"heading_raw": None, "heading_line": None, "guess": "personal", "label": "personal",
               "guess_source": "position", "lines": []}]
    for i, line in enumerate(lines):
        if i in headings:
            guess, label, source = headings[i]
            blocks.append({"heading_raw": line["text"], "heading_line": line, "guess": guess, "label": label,
                           "guess_source": source, "lines": []})
        else:
            blocks[-1]["lines"].append(line)
    if use_content:
        for block in blocks[1:]:
            if block["guess"] is None:
                found = content_guess(block["lines"], p["content_min_share"])
                if found:
                    block.update(guess=found[0], label=found[0], guess_source="content")
    for n, block in enumerate(blocks, start=1):
        block["block_id"] = f"block_{n:03d}"
    return [{k: b[k] for k in sorted(b)} for b in blocks]


def _detect(payload, method: str, use_look: bool, use_content: bool) -> MethodOutput:
    lines, p = payload
    if not lines:
        return MethodOutput(None)
    lines = split_inline_headings(lines)
    headings, unknown = find_headings(lines, p, use_look)
    blocks = build_blocks(lines, headings, p, use_content)
    return MethodOutput({"method": method, "blocks": blocks, "unknown_headings": unknown,
                         "uses_look": use_look, "personal_free_lines": p["personal_free_lines"]})


def heading_vocabulary(payload) -> MethodOutput:
    return _detect(payload, "heading_vocabulary", use_look=False, use_content=False)


def heading_look(payload) -> MethodOutput:
    return _detect(payload, "heading_look", use_look=True, use_content=False)


def content_patterns(payload) -> MethodOutput:
    return _detect(payload, "content_patterns", use_look=True, use_content=True)


METHODS = (
    Method("heading_vocabulary", heading_vocabulary),
    Method("heading_look", heading_look),
    Method("content_patterns", content_patterns),
)


def leak_counts(blocks: list[dict], personal_free_lines: int) -> tuple[int, int]:
    """(target-looking lines where they do not belong, all target-looking lines)."""
    leaked = total = 0
    for n, block in enumerate(blocks):
        for k, line in enumerate(block["lines"]):
            kind = line_kind(line["text"])
            if kind is None or (n == 0 and k < personal_free_lines):
                continue
            total += 1
            leaked += block["guess"] not in BELONGS[kind]
    return leaked, total


def check_sections(data: dict):
    blocks = data["blocks"]
    found = sorted({b["guess"] for b in blocks if b["guess"] in TARGET_BLOCKS})
    leaked, total = leak_counts(blocks, data["personal_free_lines"])
    leak_rate = round(leaked / total, 4) if total else 0.0
    score = (1.0 - leak_rate) if found else 0.0
    if data["unknown_headings"] and not data["uses_look"]:
        score *= 0.5  # headings it cannot see are still there
    details = {"blocks": len(blocks), "targets_found": len(found), "target_lines": total,
               "leaked_lines": leaked, "leak_rate": leak_rate, "unknown_headings": len(data["unknown_headings"])}
    return score, details


def run_sections(pdf_path: Path, ctx: Context) -> StageResult:
    p = {**ctx.thresholds["stage3_sections"], "fuzzy_min": ctx.thresholds["stage4_normalize"]["fuzzy_min"]}
    stage2 = ctx.results["stage2"].data or {}
    result = run_with_fallbacks(
        "stage3",
        METHODS,
        check_sections,
        ctx.thresholds["accept_threshold"]["stage3"],
        payload=(stage2.get("lines") or [], p),
    )
    data = result.data
    if data:
        result.metrics.update({
            "blocks": len(data["blocks"]),
            "headings_alias": sum(b["guess_source"] in ("alias", "fuzzy") for b in data["blocks"]),
            "headings_look": sum(b["guess_source"] in ("look", "content") for b in data["blocks"]),
            "unknown_headings": len(data["unknown_headings"]),
        })
        if not any(b["guess"] in TARGET_BLOCKS for b in data["blocks"]):
            result = escalate(result, Status.WARN, Reason("no_target_section", section="document"))
    return result
