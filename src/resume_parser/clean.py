"""Stage 2: cleaning of the stage 1 lines.

Methods, in order:
  full_clean          everything below
  conservative_clean  glyphs and whitespace only
  raw_lines           the stage 1 lines unchanged

full_clean:
- removes repeated page headers and footers, only on documents of 3 or more pages: the
  same text (digits masked, so page numbers match) in the same vertical band (within
  header_footer_band_pt) near the top or bottom of the page, on at least half the pages;
- strips bullet and icon glyphs, (cid:N) codes and replacement characters;
- normalises unicode (NFKC) and whitespace;
- joins a wrapped line to the line above, fixing a hyphen at the line end, only inside
  paragraph blocks: neither line in a table cell, a small vertical gap, the same left edge,
  and the next line starting with a lowercase letter. Wrapped citations are left to stage 5.

Check: letters and digits kept, as a share of the original letters and digits minus those
in removed headers, footers and glyphs. Below the accept threshold (0.95) the next method
is tried. Every line keeps its id, page and bbox; a joined line keeps the first line's id
and lists the others in joined_ids.
"""

from __future__ import annotations

import math
import re
import unicodedata
from pathlib import Path

from resume_parser.pipeline import run_with_fallbacks
from resume_parser.schema import Context, Method, MethodOutput, StageResult

_CID = re.compile(r"\(cid:\d+\)")
_BULLET_CHARS = "•▪●◦■□►▸▹➢➤✓✔✗❖◆◇◊‣⁃∙·○◉*�"
_LEADING_BULLET = re.compile(
    r"^\s*(?:[" + re.escape(_BULLET_CHARS) + r"]+|[-–—](?=\s)|o(?=\s+[A-Z]))\s*"
)


def _alnum(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


def _is_icon(ch: str) -> bool:
    o = ord(ch)
    return unicodedata.category(ch) == "So" or 0xE000 <= o <= 0xF8FF or o >= 0xF0000 or ch == "�"


def strip_glyphs(text: str) -> tuple[str, int]:
    """Text without (cid:N), a leading bullet, icons and replacement characters, and the
    number of letters and digits that went with them."""
    removed = sum(_alnum(m) for m in _CID.findall(text))
    text = _CID.sub(" ", text)
    m = _LEADING_BULLET.match(text)
    if m:
        removed += _alnum(m.group(0))
        text = text[m.end():]
    kept = []
    for ch in text:
        if _is_icon(ch):
            removed += ch.isalnum()
            kept.append(" ")
        else:
            kept.append(ch)
    return "".join(kept), removed


def normalise_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _all_lines(pages: dict) -> list[dict]:
    lines = []
    for page_id in sorted(pages or {}):
        data = pages[page_id]
        if data:
            lines += [dict(line) for line in data["lines"]]
    return lines


def _page_heights(pages: dict) -> dict[int, float]:
    out = {}
    for data in (pages or {}).values():
        if data and data["lines"]:
            out[data["lines"][0]["page"]] = float(data.get("height") or 0.0)
    return out


def header_footer_ids(lines: list[dict], heights: dict[int, float], params: dict) -> set[str]:
    """Ids of lines repeated as page headers or footers (see the module docstring)."""
    n_pages = len(heights)
    if n_pages < params["header_footer_min_pages"]:
        return set()
    need = math.ceil(params["header_footer_min_page_share"] * n_pages)
    zone, band = params["header_footer_zone"], params["header_footer_band_pt"]
    groups: dict[str, list[dict]] = {}
    for line in lines:
        h = heights.get(line["page"]) or 0.0
        top, bottom = line["bbox"][1], line["bbox"][3]
        if h and not (top <= zone * h or bottom >= (1 - zone) * h):
            continue
        key = re.sub(r"\d+", "#", normalise_space(line["text"]).lower())
        if key:
            groups.setdefault(key, []).append(line)
    out = set()
    for members in groups.values():
        for line in members:
            pages = {m["page"] for m in members if abs(m["bbox"][1] - line["bbox"][1]) <= band}
            if len(pages) >= need:
                out.add(line["id"])
    return out


def _can_join(a: dict, b: dict, params: dict) -> bool:
    if a["in_table"] or b["in_table"] or a["page"] != b["page"] or a.get("row_id") == b.get("row_id"):
        return False
    gap = b["bbox"][1] - a["bbox"][3]
    if gap < -1.0 or gap > params["join_max_gap_ratio"] * (a["font_size"] or 1.0):
        return False
    if abs(b["bbox"][0] - a["bbox"][0]) > params["join_left_edge_pt"]:
        return False
    return bool(b["text"]) and b["text"][0].islower()


def join_wrapped(lines: list[dict], params: dict) -> tuple[list[dict], int]:
    """Join wrapped paragraph lines; returns the lines and how many joins were made."""
    out: list[dict] = []
    joins = 0
    for line in lines:
        prev = out[-1] if out else None
        last = prev.get("_last") if prev else None
        if prev is not None and _can_join(last, line, params):
            text = prev["text"]
            if len(text) > 1 and text.endswith("-") and text[-2].isalpha():
                text = text[:-1] + line["text"]  # hyphen at the line end
            else:
                text = text + " " + line["text"]
            x0, top, x1, bottom = prev["bbox"]
            bx0, btop, bx1, bbottom = line["bbox"]
            prev.update(text=text, bbox=[min(x0, bx0), min(top, btop), max(x1, bx1), max(bottom, bbottom)])
            prev["joined_ids"] = prev["joined_ids"] + [line["id"]]
            prev["_last"] = line
            joins += 1
        else:
            out.append(dict(line, _last=line))
    for line in out:
        del line["_last"]
    return out, joins


def _clean(pages: dict, params: dict, method: str, full: bool) -> MethodOutput:
    lines = _all_lines(pages)
    raw_alnum = sum(_alnum(line["text"]) for line in lines)
    drop = header_footer_ids(lines, _page_heights(pages), params) if full else set()
    removed_hf = sum(_alnum(line["text"]) for line in lines if line["id"] in drop)
    removed_glyphs = 0
    empty = 0
    cleaned = []
    for line in lines:
        if line["id"] in drop:
            continue
        text, removed = strip_glyphs(line["text"])
        removed_glyphs += removed
        if full:
            text = unicodedata.normalize("NFKC", text)
        text = normalise_space(text)
        if not text:
            empty += 1
            continue
        cleaned.append(dict(line, text=text, joined_ids=[]))
    joins = 0
    if full:
        cleaned, joins = join_wrapped(cleaned, params)
    return MethodOutput({
        "method": method,
        "lines": cleaned,
        "raw_alnum": raw_alnum,
        "removed_alnum": {"header_footer": removed_hf, "glyphs": removed_glyphs},
        "removed_lines": {"header_footer": len(drop), "empty": empty},
        "joins": joins,
    })


def full_clean(payload) -> MethodOutput:
    pages, params = payload
    return _clean(pages, params, "full_clean", full=True)


def conservative_clean(payload) -> MethodOutput:
    """Glyphs and whitespace only: no header/footer removal, no unicode change, no joins."""
    pages, params = payload
    return _clean(pages, params, "conservative_clean", full=False)


def raw_lines(payload) -> MethodOutput:
    pages, _params = payload
    lines = [dict(line, joined_ids=[]) for line in _all_lines(pages)]
    total = sum(_alnum(line["text"]) for line in lines)
    return MethodOutput({"method": "raw_lines", "lines": lines, "raw_alnum": total,
                         "removed_alnum": {"header_footer": 0, "glyphs": 0},
                         "removed_lines": {"header_footer": 0, "empty": 0}, "joins": 0})


METHODS = (
    Method("full_clean", full_clean),
    Method("conservative_clean", conservative_clean),
    Method("raw_lines", raw_lines),
)


def check_clean(data: dict):
    """Letters and digits kept, excluding removed headers, footers and glyphs (0..1)."""
    removed = data["removed_alnum"]
    expected = data["raw_alnum"] - removed["header_footer"] - removed["glyphs"]
    kept = sum(_alnum(line["text"]) for line in data["lines"])
    retention = min(1.0, kept / expected) if expected > 0 else 1.0
    details = {"retention": round(retention, 4), "raw_alnum": data["raw_alnum"], "kept_alnum": kept,
               "removed_header_footer": removed["header_footer"], "removed_glyphs": removed["glyphs"],
               "joins": data["joins"]}
    return retention, details


def run_clean(pdf_path: Path, ctx: Context) -> StageResult:
    params = ctx.thresholds["stage2_clean"]
    return run_with_fallbacks(
        "stage2",
        METHODS,
        check_clean,
        ctx.thresholds["accept_threshold"]["stage2"],
        payload=(ctx.results["stage1"].data, params),
    )
