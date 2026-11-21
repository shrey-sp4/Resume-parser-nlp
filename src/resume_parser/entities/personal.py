"""Personal information: name, email, phones, links. Name methods are stubs for now."""

from __future__ import annotations

from resume_parser.schema import Method, MethodOutput, not_implemented


def name_from_top_lines(payload) -> MethodOutput:
    """Top lines, no digits, 2 to 4 words, honorifics removed."""
    return not_implemented()


def name_from_email(payload) -> MethodOutput:
    """Name guessed from the local part of the email address."""
    return not_implemented()


def name_from_file_name(payload) -> MethodOutput:
    """Lowest confidence: name taken from the PDF file name."""
    return not_implemented()


NAME_METHODS = (
    Method("top_lines", name_from_top_lines, stub=True),
    Method("from_email", name_from_email, stub=True),
    Method("from_file_name", name_from_file_name, warn_code="name_from_file_name", stub=True),
)
