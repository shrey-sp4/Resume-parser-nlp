"""Stage 1: layout extraction, with the method chosen per page.

Method chain for text and mixed pages:
  1. pdfplumber_words_tables   always runs
  2. pdfminer_layout           always runs (it is also the agreement reference for method 1)
  3. pdfplumber_loose_spacing  runs only when method 1 was not accepted
  4. ocr_tesseract             optional; runs only when nothing was accepted
Image-only pages (from the stage 0 profile) only try OCR and must give output, otherwise the
page is FAIL "no_text_layer". The stage FAILs only when no page is usable.

Page score (0..1), from label-free checks (thresholds.json, stage1_checks, to calibrate):
- 0 when more than garbled_max of the characters are garbled ((cid:N) or replacement
  characters; private-use icon glyphs do not count). An empty page gives no output at all.
- otherwise the word overlap with the other text method (order-insensitive; method 1 and 3
  are compared with method 2, method 2 with method 1), or 1.0 when the other method has
  nothing to compare with;
- halved when more than glued_max of the words look glued, so such a page cannot be
  accepted but still ranks against the other methods.
The separate values (garbled_rate, glued_rate, glued_words, overlap, words, lines) are
recorded with every attempt. Pages read column by column (extract/lines.py column_order) are
noted as columns_reordered, pages that only may have columns as possible_columns; these notes
go to the sidecar flags and never change a status, a decision or a score. The first method in order that reaches the accept threshold
wins; otherwise the best score wins with WARN; ties go to the earlier method.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

from resume_parser.extract.lines import garbled_rate, glued_words, possible_columns, text_of, word_overlap
from resume_parser.extract.ocr_extractor import OCR_TESSERACT
from resume_parser.extract.pdfminer_extractor import LAYOUT
from resume_parser.extract.pdfplumber_extractor import LOOSE_SPACING, WORDS_AND_TABLES
from resume_parser.extract.source import PageSource
from resume_parser.pipeline import run_with_fallbacks
from resume_parser.schema import AttemptStatus, Context, Method, Reason, StageResult, Unit


def _first_not_accepted(attempts) -> bool:
    return bool(attempts) and attempts[0].status is not AttemptStatus.ACCEPTED


def _none_accepted(attempts) -> bool:
    return not any(a.status is AttemptStatus.ACCEPTED for a in attempts)


METHODS = (
    WORDS_AND_TABLES,
    LAYOUT,
    Method(LOOSE_SPACING.name, LOOSE_SPACING.fn, run_if=_first_not_accepted),
    Method(OCR_TESSERACT.name, OCR_TESSERACT.fn, optional=True, run_if=_none_accepted),
)
EXTRACTORS = {m.name: m.fn for m in METHODS}
# the method each one is compared with for the word-overlap check
REFERENCE = {
    "pdfplumber_words_tables": "pdfminer_layout",
    "pdfminer_layout": "pdfplumber_words_tables",
    "pdfplumber_loose_spacing": "pdfminer_layout",
}


def page_score(data: dict, reference: dict | None, params: dict) -> tuple[float, dict]:
    """(score, details) for one method's lines on one page. See the module docstring."""
    text = text_of(data["lines"])
    garbled = garbled_rate(text)
    glued_rate, glued_n, words = glued_words(text, params)
    overlap = word_overlap(text, text_of(reference["lines"])) if reference else None
    if garbled > params["garbled_max"]:
        score = 0.0
    else:
        score = overlap if overlap is not None else 1.0
        if glued_rate > params["glued_max"]:
            score *= 0.5
    details = {"garbled_rate": garbled, "glued_rate": glued_rate, "glued_words": glued_n,
               "overlap": overlap, "words": words, "lines": len(data["lines"])}
    return score, details


def _page_check(source: PageSource, params: dict):
    def check(data: dict):
        ref_name = REFERENCE.get(data.get("method"))
        ref = source.get(ref_name).data if ref_name else None
        return page_score(data, ref, params)
    return check


def _cached(name: str):
    return lambda source: source.get(name)


def _open_pdfplumber(stack: ExitStack, pdf_path: Path):
    try:
        import pdfplumber

        return stack.enter_context(pdfplumber.open(pdf_path)).pages
    except Exception:  # stage 0 may have opened it with pdfminer only
        return None


def _open_pdfminer(pdf_path: Path):
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LAParams

        return list(extract_pages(str(pdf_path), laparams=LAParams()))
    except Exception:
        return None


def _glued_page_counts(result: StageResult, glued_max: float) -> dict:
    """How many pages trigger the glued-word check: for the chosen method, and per method."""
    chosen = 0
    by_method: dict[str, int] = {}
    for choice in result.per_unit.values():
        for a in choice.methods_tried:
            rate = dict(a.details).get("glued_rate")
            if rate is not None and rate > glued_max:
                by_method[a.method] = by_method.get(a.method, 0) + 1
                chosen += a.method == choice.method_used
    return {"pages_glued_over_max": chosen, "pages_glued_over_max_by_method": dict(sorted(by_method.items()))}


def run_layout(pdf_path: Path, ctx: Context) -> StageResult:
    params = {**ctx.thresholds["stage1_extract"], **ctx.thresholds["stage1_checks"]}
    pages = ctx.results["stage0"].metrics.get("pages", [])
    with ExitStack() as stack:
        plumber_pages = _open_pdfplumber(stack, Path(pdf_path))
        miner_pages = _open_pdfminer(Path(pdf_path))
        units = []
        for page in pages:
            n = page["page"]
            image_only = page["profile"] == "IMAGE_ONLY"
            source = PageSource(
                n,
                plumber_pages[n - 1] if plumber_pages and n <= len(plumber_pages) else None,
                miner_pages[n - 1] if miner_pages and n <= len(miner_pages) else None,
                params,
                EXTRACTORS,
            )
            chain = (OCR_TESSERACT,) if image_only else METHODS
            units.append(
                Unit(
                    unit_id=f"page_{n:03d}",
                    payload=source,
                    methods=tuple(Method(m.name, _cached(m.name), m.warn_code, m.stub, m.optional, m.run_if)
                                  for m in chain),
                    require_output=image_only,
                    section="document",
                    check=_page_check(source, params),
                )
            )
        result = run_with_fallbacks(
            "stage1",
            METHODS,
            lambda data: 0.0,  # every unit brings its own check
            ctx.thresholds["accept_threshold"]["stage1"],
            units=units,
            fail_if_unusable=True,
            unusable_code="no_text_layer",
            run_all=True,
        )
    result.metrics.update(_glued_page_counts(result, params["glued_max"]))
    result.metrics["notes"] = _column_notes(result, params)
    return result


def _column_notes(result: StageResult, params: dict) -> list[dict]:
    """Per page: columns_reordered when the chosen lines were read column by column,
    possible_columns when only the looser gutter test finds columns. They become sidecar flags
    (pipeline.flags_from) but never change a stage status, a decision or a score."""
    notes = []
    for key, page in sorted((result.data or {}).items()):
        lines = (page or {}).get("lines") or []
        if any("column" in line for line in lines):
            notes.append(Reason("columns_reordered", key).to_dict())
        elif lines and possible_columns(lines, params):
            notes.append(Reason("possible_columns", key).to_dict())
    return notes
