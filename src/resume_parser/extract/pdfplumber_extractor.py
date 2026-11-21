"""pdfplumber-based line extraction (stage 1). Not built yet: both methods are stubs.

Each method takes (pdf_path, page_number) and will return the page's lines with
text, bounding box, font size, bold, table cell and reading-order index.
"""

from __future__ import annotations

from resume_parser.schema import Method, MethodOutput, not_implemented


def words_and_tables(payload) -> MethodOutput:
    """Main method: words with coordinates, tables read cell by cell."""
    return not_implemented()


def loose_spacing(payload) -> MethodOutput:
    """Same as words_and_tables with a looser word-spacing tolerance (for glued words)."""
    return not_implemented()


WORDS_AND_TABLES = Method("pdfplumber_words_tables", words_and_tables, stub=True)
LOOSE_SPACING = Method("pdfplumber_loose_spacing", loose_spacing, stub=True)
