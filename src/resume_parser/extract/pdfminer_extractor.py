"""pdfminer.six layout analysis (stage 1 fallback).

Gives the same line structure as the pdfplumber methods (extract/lines.py), built from
pdfminer's text lines. Lines keep pdfminer's own reading order (text boxes in its layout
order), which differs from the pdfplumber top-to-bottom order on column layouts. Each
pdfminer line is split at wide gaps the same way as pdfplumber rows; row_id groups lines
on the same visual row. pdfminer has no table finder, so in_table is always False.
"""

from __future__ import annotations

from resume_parser.extract.lines import assign_rows, column_order, is_bold, make_line, number_lines, split_segments
from resume_parser.schema import Method, MethodOutput, Reason


def _walk_lines(obj):
    from pdfminer.layout import LTTextLine

    if isinstance(obj, LTTextLine):
        yield obj
        return
    if hasattr(obj, "__iter__"):
        for child in obj:
            yield from _walk_lines(child)


def _line_words(text_line, height: float) -> list[dict]:
    """Words of one pdfminer text line, from its characters (spaces end a word)."""
    from pdfminer.layout import LTChar

    words, chars = [], []

    def flush():
        if chars:
            words.append({
                "text": "".join(c.get_text() for c in chars),
                "x0": min(c.x0 for c in chars), "x1": max(c.x1 for c in chars),
                "top": height - max(c.y1 for c in chars), "bottom": height - min(c.y0 for c in chars),
                "size": sorted(c.size for c in chars)[len(chars) // 2],
                "bold": sum(is_bold(c.fontname) for c in chars) * 2 > len(chars),
            })
            chars.clear()

    for obj in text_line:
        if isinstance(obj, LTChar) and not obj.get_text().isspace():
            chars.append(obj)
        else:  # a space character or a virtual space between words
            flush()
    flush()
    return words


def layout_lines(source) -> MethodOutput:
    """Lines from pdfminer's layout analysis for one page."""
    layout = source.miner_page
    if layout is None:
        return MethodOutput(None, (Reason("open_failed", "pdfminer"),))
    try:
        height = float(layout.height)
        lines = []
        for text_line in _walk_lines(layout):
            words = _line_words(text_line, height)
            if not words:
                continue
            for seg in split_segments(words, source.params["segment_gap_ratio"]):
                lines.append(make_line(seg, source.page))
        lines = assign_rows(lines, source.page, source.params["row_tolerance_ratio"])
    except Exception as exc:
        return MethodOutput(None, (Reason("extract_failed", f"pdfminer_layout: {type(exc).__name__}"),))
    if not lines:
        return MethodOutput(None, (Reason("empty_page"),))
    width, height = source.size
    return MethodOutput({"method": "pdfminer_layout", "lines": number_lines(column_order(lines, source.params), source.page),
                         "width": width, "height": height})


LAYOUT = Method("pdfminer_layout", layout_lines)
