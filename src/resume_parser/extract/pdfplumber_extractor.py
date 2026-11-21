"""pdfplumber-based line extraction (stage 1).

words_and_tables: words with coordinates grouped into lines (see extract/lines.py);
tables found by find_tables are read cell by cell, and words inside a cell are not read
again in the normal line pass.

loose_spacing: the same, with a different word-spacing tolerance, for pages where words
are glued together or split apart. It tries a tight and a wide tolerance and keeps the one
with fewer glued words and more agreement with the pdfminer lines.

Each method takes a PageSource (extract/source.py) and returns
{"method", "lines", "width", "height"} (plus "variant" for loose_spacing).
"""

from __future__ import annotations

from resume_parser.extract.lines import glued_words, is_bold, text_of, word_overlap, words_to_lines
from resume_parser.schema import Method, MethodOutput, Reason


def _words(page, x_tolerance: float) -> list[dict]:
    raw = page.extract_words(
        x_tolerance=x_tolerance, y_tolerance=3, keep_blank_chars=False, use_text_flow=False,
        extra_attrs=["fontname", "size"],
    )
    return [
        {"text": w["text"], "x0": float(w["x0"]), "x1": float(w["x1"]), "top": float(w["top"]),
         "bottom": float(w["bottom"]), "size": float(w["size"]), "bold": is_bold(w.get("fontname"))}
        for w in raw
        if w["text"].strip()
    ]


def _tables(page) -> list:
    out = []
    for table in page.find_tables():
        rows = [[tuple(float(v) for v in cell) if cell else None for cell in row.cells] for row in table.rows]
        out.append((tuple(float(v) for v in table.bbox), rows))
    return out


def _extract(source, x_tolerance: float, method: str) -> MethodOutput:
    page = source.plumber_page
    if page is None:
        return MethodOutput(None, (Reason("open_failed", "pdfplumber"),))
    try:
        lines = words_to_lines(_words(page, x_tolerance), source.page, source.params, _tables(page))
    except Exception as exc:  # a broken page must not stop the other methods
        return MethodOutput(None, (Reason("extract_failed", f"{method}: {type(exc).__name__}"),))
    if not lines:
        return MethodOutput(None, (Reason("empty_page"),))
    width, height = source.size
    return MethodOutput({"method": method, "lines": lines, "width": width, "height": height})


def words_and_tables(source) -> MethodOutput:
    """Main method: words with coordinates, tables read cell by cell."""
    return _extract(source, source.params["x_tolerance"], "pdfplumber_words_tables")


def loose_spacing(source) -> MethodOutput:
    """words_and_tables with a tight and a wide word-spacing tolerance; keeps the variant
    with fewer glued words, then more word overlap with pdfminer_layout (tight on a tie)."""
    reference = source.get("pdfminer_layout").data
    ref_text = text_of(reference["lines"]) if reference else ""
    best, best_key = None, None
    for variant, tol in (("tight", source.params["tight_x_tolerance"]), ("wide", source.params["wide_x_tolerance"])):
        out = _extract(source, tol, "pdfplumber_loose_spacing")
        if out.data is None:
            continue
        text = text_of(out.data["lines"])
        overlap = word_overlap(text, ref_text) if ref_text else None
        key = (glued_words(text, source.params)[0], -(overlap or 0.0))
        if best_key is None or key < best_key:
            best, best_key = MethodOutput(dict(out.data, variant=variant)), key
    return best or MethodOutput(None, (Reason("empty_page"),))


WORDS_AND_TABLES = Method("pdfplumber_words_tables", words_and_tables)
LOOSE_SPACING = Method("pdfplumber_loose_spacing", loose_spacing)
