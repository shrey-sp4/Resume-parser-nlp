"""The fake resume builder makes 3 valid, byte-stable PDFs."""

from __future__ import annotations

import pdfplumber

import make_synthetic_resumes as synth


def test_three_valid_pdfs_are_created(tmp_path):
    paths = synth.build_all(tmp_path / "out")
    assert [p.name for p in paths] == [
        "synthetic_a_simple.pdf",
        "synthetic_b_table.pdf",
        "synthetic_c_variants.pdf",
    ]
    for path in paths:
        assert path.read_bytes().startswith(b"%PDF")
        with pdfplumber.open(path) as pdf:
            assert len(pdf.pages) >= 1
            text = pdf.pages[0].extract_text() or ""
            assert len(text.replace(" ", "")) > 200


def test_builds_are_byte_identical(tmp_path):
    first = [p.read_bytes() for p in synth.build_all(tmp_path / "one")]
    second = [p.read_bytes() for p in synth.build_all(tmp_path / "two")]
    assert first == second


def test_cli_entry_point(tmp_path, capsys):
    assert synth.main([str(tmp_path / "cli")]) == 0
    assert len(list((tmp_path / "cli").glob("*.pdf"))) == 3
