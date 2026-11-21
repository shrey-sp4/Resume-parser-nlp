"""Stage 0: intake and triage.

Opens the PDF (pdfplumber first, pdfminer as fallback) and builds a profile of
each page: TEXT, IMAGE_ONLY or MIXED, plus table_present and two_column_hint.

Gate: FAIL "unreadable" only when no opener can read the file. Low or no text
gives WARN "needs_ocr"; stage 1 then decides whether the pages are usable.
"""

from __future__ import annotations

from pathlib import Path

from resume_parser.pipeline import escalate, run_with_fallbacks
from resume_parser.schema import Context, Method, MethodOutput, Reason, StageResult, Status


# ---------------------------------------------------------------- page profile


def _image_share(boxes: list[tuple[float, float, float, float]], width: float, height: float) -> float:
    """Share of the page covered by image boxes (x0, y0, x1, y1), clipped and capped at 1."""
    page_area = width * height
    if page_area <= 0:
        return 0.0
    covered = 0.0
    for x0, y0, x1, y1 in boxes:
        w = max(0.0, min(x1, width) - max(x0, 0.0))
        h = max(0.0, min(y1, height) - max(y0, 0.0))
        covered += w * h
    return round(min(1.0, covered / page_area), 4)


def _segments_from_words(words: list[tuple[float, float, float]], gap: float) -> list[tuple[float, float]]:
    """Group words (x0, x1, top) into line segments, split where the gap is wide."""
    rows: dict[int, list[tuple[float, float]]] = {}
    for x0, x1, top in words:
        rows.setdefault(round(top), []).append((x0, x1))
    segments = []
    for key in sorted(rows):
        spans = sorted(rows[key])
        start, end = spans[0]
        for x0, x1 in spans[1:]:
            if x0 - end > gap:
                segments.append((start, end))
                start, end = x0, x1
            else:
                end = max(end, x1)
        segments.append((start, end))
    return segments


def two_column_hint(segments: list[tuple[float, float]], width: float, params: dict) -> bool:
    """True when few text segments cross the centre strip and both halves have text."""
    if not segments or width <= 0:
        return False
    lo, hi = (v * width for v in params["center_strip"])
    total = len(segments)
    crossing = sum(1 for x0, x1 in segments if x0 < lo and x1 > hi)
    left = sum(1 for _x0, x1 in segments if x1 <= lo)
    right = sum(1 for x0, _x1 in segments if x0 >= hi)
    return (
        crossing / total <= params["two_column_max_crossing_share"]
        and left / total >= params["two_column_min_half_share"]
        and right / total >= params["two_column_min_half_share"]
    )


def classify_page(text_chars: int, image_share: float, params: dict) -> str:
    """TEXT, IMAGE_ONLY or MIXED. A blank page counts as IMAGE_ONLY (no text layer)."""
    if text_chars < params["min_text_chars"]:
        return "IMAGE_ONLY"
    if image_share >= params["mixed_image_share"]:
        return "MIXED"
    return "TEXT"


def _page_profile(number, text_chars, image_share, table_present, two_col, params) -> dict:
    return {
        "page": number,
        "text_chars": text_chars,
        "image_area_share": image_share,
        "profile": classify_page(text_chars, image_share, params),
        "table_present": table_present,
        "two_column_hint": two_col,
    }


# ---------------------------------------------------------------- openers


def open_with_pdfplumber(pdf_path: Path, params: dict) -> MethodOutput:
    try:
        import pdfplumber

        pages = []
        with pdfplumber.open(pdf_path) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                width, height = float(page.width), float(page.height)
                text_chars = sum(1 for ch in page.chars if not ch.get("text", "").isspace())
                boxes = [
                    (float(im["x0"]), float(im["top"]), float(im["x1"]), float(im["bottom"]))
                    for im in page.images
                ]
                words = [
                    (float(w["x0"]), float(w["x1"]), float(w["top"]))
                    for w in page.extract_words()
                ]
                segments = _segments_from_words(words, params["segment_gap_pt"])
                pages.append(
                    _page_profile(
                        number,
                        text_chars,
                        _image_share(boxes, width, height),
                        bool(page.find_tables()),
                        two_column_hint(segments, width, params),
                        params,
                    )
                )
    except Exception as exc:  # any failure to open or read means: try the next opener
        return MethodOutput(None, (Reason("open_failed", f"pdfplumber: {type(exc).__name__}"),))
    if not pages:
        return MethodOutput(None, (Reason("no_pages", "pdfplumber"),))
    return MethodOutput({"opened_with": "pdfplumber", "pages": pages})


def _walk(obj):
    yield obj
    if hasattr(obj, "__iter__") and not isinstance(obj, str):
        for child in obj:
            yield from _walk(child)


def open_with_pdfminer(pdf_path: Path, params: dict) -> MethodOutput:
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LAParams, LTChar, LTImage, LTTextLine

        pages = []
        for number, layout in enumerate(extract_pages(str(pdf_path), laparams=LAParams()), start=1):
            width, height = float(layout.width), float(layout.height)
            text_chars = 0
            boxes = []
            segments = []
            for item in _walk(layout):
                if isinstance(item, LTChar) and not item.get_text().isspace():
                    text_chars += 1
                elif isinstance(item, LTImage):
                    x0, y0, x1, y1 = item.bbox
                    boxes.append((x0, height - y1, x1, height - y0))
                elif isinstance(item, LTTextLine):
                    segments.append((item.x0, item.x1))
            pages.append(
                _page_profile(
                    number,
                    text_chars,
                    _image_share(boxes, width, height),
                    None,  # pdfminer has no table finder
                    two_column_hint(segments, width, params),
                    params,
                )
            )
    except Exception as exc:
        return MethodOutput(None, (Reason("open_failed", f"pdfminer: {type(exc).__name__}"),))
    if not pages:
        return MethodOutput(None, (Reason("no_pages", "pdfminer"),))
    return MethodOutput({"opened_with": "pdfminer", "pages": pages})


def check_intake(data: dict) -> float:
    """1.0 when the file opened and has pages."""
    return 1.0 if data.get("pages") else 0.0


# ---------------------------------------------------------------- stage


def run_intake(pdf_path: Path, ctx: Context) -> StageResult:
    params = ctx.thresholds["page_profile"]
    methods = (
        Method("open_pdfplumber", lambda p: open_with_pdfplumber(p, params)),
        Method("open_pdfminer", lambda p: open_with_pdfminer(p, params)),
    )
    result = run_with_fallbacks(
        "stage0",
        methods,
        check_intake,
        ctx.thresholds["accept_threshold"]["stage0"],
        payload=Path(pdf_path),
        fail_if_unusable=True,
        unusable_code="unreadable",
    )
    if result.status is Status.FAIL:
        return result

    pages = result.data["pages"]
    total_chars = sum(p["text_chars"] for p in pages)
    avg = round(total_chars / len(pages), 2)
    counts = {kind: sum(p["profile"] == kind for p in pages) for kind in ("TEXT", "IMAGE_ONLY", "MIXED")}
    result.metrics.update(
        {
            "page_count": len(pages),
            "avg_text_chars_per_page": avg,
            "profile_counts": counts,
            "pages": pages,
        }
    )
    if counts["IMAGE_ONLY"] or avg < params["low_text_chars_per_page"]:
        detail = f"{counts['IMAGE_ONLY']} image-only page(s), {avg} text chars per page"
        result = escalate(result, Status.WARN, Reason("needs_ocr", detail, "document"))
    return result
