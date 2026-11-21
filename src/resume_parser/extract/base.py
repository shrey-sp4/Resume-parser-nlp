"""Stage 1: layout extraction, with the method chosen per page.

Method chain for text and mixed pages: pdfplumber words+tables, pdfminer layout,
pdfplumber with looser word spacing, OCR. Image-only pages (from the stage 0
profile) only try OCR and must give output, otherwise the page is FAIL
"no_text_layer". The stage FAILs only when no page is usable.
"""

from __future__ import annotations

from pathlib import Path

from resume_parser.extract.ocr_extractor import OCR_TESSERACT
from resume_parser.extract.pdfminer_extractor import LAYOUT
from resume_parser.extract.pdfplumber_extractor import LOOSE_SPACING, WORDS_AND_TABLES
from resume_parser.pipeline import run_with_fallbacks
from resume_parser.schema import Context, StageResult, Unit

METHODS = (WORDS_AND_TABLES, LAYOUT, LOOSE_SPACING, OCR_TESSERACT)


def check_layout(lines) -> float:
    """Quality score for one page's lines (garbled chars, glued words, agreement).

    Placeholder: returns 0.0 until the extractors exist, so a new method cannot
    pass without a real check being written.
    """
    return 0.0


def run_layout(pdf_path: Path, ctx: Context) -> StageResult:
    pages = ctx.results["stage0"].metrics.get("pages", [])
    units = []
    for page in pages:
        image_only = page["profile"] == "IMAGE_ONLY"
        units.append(
            Unit(
                unit_id=f"page_{page['page']:03d}",
                payload=(Path(pdf_path), page["page"]),
                methods=(OCR_TESSERACT,) if image_only else None,
                require_output=image_only,
                section="document",
            )
        )
    return run_with_fallbacks(
        "stage1",
        METHODS,
        check_layout,
        ctx.thresholds["accept_threshold"]["stage1"],
        units=units,
        fail_if_unusable=True,
        unusable_code="no_text_layer",
    )
