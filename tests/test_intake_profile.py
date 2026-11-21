"""Stage 0 page profile: TEXT, IMAGE_ONLY and MIXED pages are told apart."""

from __future__ import annotations

import make_synthetic_resumes as synth
from resume_parser.intake import classify_page, open_with_pdfminer, run_intake
from resume_parser.schema import Status, load_thresholds


def _profile(path, ctx):
    result = run_intake(path, ctx)
    return result, result.metrics["pages"][0]


def test_text_page(tmp_path, ctx):
    result, page = _profile(synth.make_text_pdf(tmp_path / "text.pdf"), ctx)
    assert page["profile"] == "TEXT"
    assert page["image_area_share"] == 0.0
    assert result.method_used == "open_pdfplumber"
    assert result.status is Status.OK


def test_image_only_page(tmp_path, ctx):
    result, page = _profile(synth.make_image_only_pdf(tmp_path / "scan.pdf"), ctx)
    assert page["profile"] == "IMAGE_ONLY"
    assert page["text_chars"] == 0
    assert page["image_area_share"] > 0.5
    assert result.status is Status.WARN
    assert [r.code for r in result.reasons] == ["needs_ocr"]


def test_mixed_page(tmp_path, ctx):
    _result, page = _profile(synth.make_mixed_pdf(tmp_path / "mixed.pdf"), ctx)
    assert page["profile"] == "MIXED"
    assert page["text_chars"] > 100
    assert 0.1 <= page["image_area_share"] < 0.5


def test_table_is_detected(synthetic_pdfs, ctx):
    pages = run_intake(synthetic_pdfs[1], ctx).metrics["pages"]
    assert pages[0]["table_present"] is True
    pages = run_intake(synthetic_pdfs[0], ctx).metrics["pages"]
    assert pages[0]["table_present"] is False


def test_pdfminer_fallback_gives_same_profiles(tmp_path):
    params = load_thresholds()["page_profile"]
    for make, expected in [
        (synth.make_text_pdf, "TEXT"),
        (synth.make_image_only_pdf, "IMAGE_ONLY"),
        (synth.make_mixed_pdf, "MIXED"),
    ]:
        out = open_with_pdfminer(make(tmp_path / f"{expected}.pdf"), params)
        assert out.data["pages"][0]["profile"] == expected


def test_blank_page_counts_as_image_only():
    params = load_thresholds()["page_profile"]
    assert classify_page(0, 0.0, params) == "IMAGE_ONLY"
