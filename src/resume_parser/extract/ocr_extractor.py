"""OCR for pages with no text layer (stage 1, last method). Optional dependency.

Needs the pytesseract package and the tesseract program. When either is missing
the method reports "ocr_unavailable". The OCR itself is not built yet.
"""

from __future__ import annotations

import importlib.util
import shutil

from resume_parser.schema import Method, MethodOutput, Reason, not_implemented


def ocr_available() -> bool:
    return importlib.util.find_spec("pytesseract") is not None and shutil.which("tesseract") is not None


def ocr_page(payload) -> MethodOutput:
    if not ocr_available():
        return MethodOutput(None, (Reason("ocr_unavailable"),))
    return not_implemented()


OCR_TESSERACT = Method("ocr_tesseract", ocr_page, stub=True)
