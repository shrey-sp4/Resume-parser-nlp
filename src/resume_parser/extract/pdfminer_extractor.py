"""pdfminer.six layout analysis (stage 1 fallback). Not built yet: stub."""

from __future__ import annotations

from resume_parser.schema import Method, MethodOutput, not_implemented


def layout_lines(payload) -> MethodOutput:
    """Lines from pdfminer's layout analysis for one page."""
    return not_implemented()


LAYOUT = Method("pdfminer_layout", layout_lines, stub=True)
