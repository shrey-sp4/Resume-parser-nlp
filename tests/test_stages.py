"""Each real stage function returns a valid StageResult on a fake resume."""

from __future__ import annotations

from resume_parser import clean, normalize, sections
from resume_parser.entities import SPLIT_METHODS
from resume_parser.entities.personal import NAME_METHODS
from resume_parser.extract.base import METHODS as LAYOUT_METHODS
from resume_parser.pipeline import run_pipeline
from resume_parser.schema import STAGE_NAMES, StageResult, Status, schema_errors


def test_every_stage_returns_a_stage_result(synthetic_pdfs):
    record = run_pipeline(synthetic_pdfs[0])
    assert list(record.results) == list(STAGE_NAMES)
    for name, result in record.results.items():
        assert isinstance(result, StageResult)
        assert result.stage == name
        assert result.status in Status
        assert isinstance(result.metrics, dict)


def test_main_json_is_valid_and_has_empty_fields(synthetic_pdfs):
    record = run_pipeline(synthetic_pdfs[1])
    assert schema_errors(record.main, "resume.schema.json") == []
    assert list(record.main) == [
        "resume_file", "candidate_name", "education", "experience", "publications", "stated_totals", "notes"
    ]
    assert record.main["resume_file"] == "synthetic_b_table.pdf"
    assert record.main["education"] == []


def test_method_chains_are_registered_in_order():
    assert [m.name for m in LAYOUT_METHODS] == [
        "pdfplumber_words_tables", "pdfminer_layout", "pdfplumber_loose_spacing", "ocr_tesseract"
    ]
    assert [m.name for m in clean.METHODS] == ["full_clean", "conservative_clean", "raw_lines"]
    assert [m.name for m in sections.METHODS] == ["heading_vocabulary", "heading_look", "content_patterns"]
    assert [m.name for m in normalize.METHODS] == [
        "exact_alias_map", "fuzzy_alias_match", "keep_as_other_with_warning"
    ]
    assert [m.name for m in SPLIT_METHODS] == [
        "anchor_split", "table_row_split", "paragraph_split", "whole_section_one_entry"
    ]
    assert [m.name for m in NAME_METHODS] == ["top_lines", "from_email", "from_file_name"]


def test_stage5_chooses_per_section(synthetic_pdfs):
    stage5 = run_pipeline(synthetic_pdfs[0]).results["stage5"]
    assert list(stage5.per_unit) == ["personal", "education", "experience", "publications"]
    assert [a.method for a in stage5.per_unit["personal"].methods_tried] == [
        "top_lines", "from_email", "from_file_name"
    ]


def test_stub_flags_match_what_the_methods_return():
    """Every method marked stub returns not_implemented, and the guard lists exactly those."""
    from resume_parser.pipeline import method_chains, unimplemented_methods

    listed = []
    for stage, chain in method_chains().items():
        for method in chain:
            if method.stub:
                assert method.fn(None).implemented is False, f"{stage}: {method.name}"
                listed.append(f"{stage}: {method.name}")
    assert sorted(listed) == unimplemented_methods()
    assert "stage5.personal: top_lines" in listed


def test_stages_0_to_2_have_no_stubs_left():
    """Stages 0-2 run for real: no stub method, and OCR is optional, not a stub."""
    from resume_parser.pipeline import method_chains, optional_methods, unimplemented_methods

    assert not [m for m in unimplemented_methods() if m.split(":")[0] in ("stage0", "stage1", "stage2")]
    assert optional_methods() == ["stage1: ocr_tesseract"]
    assert all(not m.stub for s in ("stage1", "stage2") for m in method_chains()[s])
