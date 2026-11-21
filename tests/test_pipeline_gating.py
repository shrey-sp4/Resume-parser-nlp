"""Gate rules: FAIL in stage 0 or 1 stops the resume; later WARN or FAIL is flagged."""

from __future__ import annotations

from pathlib import Path

import make_synthetic_resumes as synth
from conftest import fake_stage, fake_stages
from resume_parser.pipeline import default_stages, run_pipeline
from resume_parser.schema import Reason, StageResult, Status, schema_errors
from resume_parser.writer import write_resume


def test_fail_in_stage1_stops_the_run(tmp_path):
    stages = fake_stages({1: fake_stage("stage1", Status.FAIL, [Reason("no_text_layer")])})
    record = run_pipeline(tmp_path / "x.pdf", stages=stages)
    assert record.stopped_at == 1
    assert list(record.results) == ["stage0", "stage1"]
    assert record.main is None
    assert record.sidecar["decision"] == "NEEDS_REVIEW"
    assert record.flags == [{"section": "document", "code": "no_text_layer", "detail": "stage1"}]
    assert schema_errors(record.sidecar, "meta.schema.json") == []


def test_fail_in_stage0_stops_the_run(tmp_path):
    stages = fake_stages({0: fake_stage("stage0", Status.FAIL, [Reason("unreadable")])})
    record = run_pipeline(tmp_path / "x.pdf", stages=stages)
    assert record.stopped_at == 0
    assert list(record.results) == ["stage0"]


def test_warn_in_stage3_continues_and_is_flagged(tmp_path):
    reason = Reason("target_section_missing", "no publications heading", "publications")
    stages = fake_stages({3: fake_stage("stage3", Status.WARN, [reason])})
    record = run_pipeline(tmp_path / "x.pdf", stages=stages)
    assert record.stopped_at is None
    assert list(record.results) == [f"stage{i}" for i in range(8)]
    assert record.flags == [
        {"section": "publications", "code": "target_section_missing", "detail": "stage3: no publications heading"}
    ]
    assert record.sidecar["needs_review"] is True


def test_fail_after_stage1_does_not_stop(tmp_path):
    stages = fake_stages({5: fake_stage("stage5", Status.FAIL, [Reason("x", section="education")])})
    record = run_pipeline(tmp_path / "x.pdf", stages=stages)
    assert record.stopped_at is None
    assert "stage7" in record.results


def test_not_implemented_is_not_a_flag(synthetic_pdfs):
    record = run_pipeline(synthetic_pdfs[0])
    assert record.stopped_at is None
    assert record.flags == []
    assert record.sidecar["decision"] == "ACCEPT"
    assert record.results["stage5"].reasons[0].code == "not_implemented"


def test_invalid_main_json_writes_sidecar_only(synthetic_pdfs, data_dir):
    bad = {"education": {"entries": [{"degree": "PhD", "degree_level": "phd", "stream": "", "institution": ""}]}}

    def bad_stage5(pdf_path, ctx):
        return StageResult(stage="stage5", status=Status.OK, data=bad)

    stages = default_stages()
    stages[5] = ("stage5", bad_stage5)
    record = run_pipeline(synthetic_pdfs[0], stages=stages)
    assert record.main is None
    assert record.results["stage7"].status is Status.FAIL
    assert {"section": "document", "code": "main_json_invalid",
            "detail": "stage7: education/0/degree_level: enum"} in record.flags

    out = data_dir / "outputs"
    write_resume(record, out)
    stem = Path(record.resume_file).stem
    assert (out / f"{stem}.meta.json").is_file()
    assert not (out / f"{stem}.json").exists()


def test_unreadable_file_fails_stage0(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"this is not a pdf")
    record = run_pipeline(broken)
    assert record.stopped_at == 0
    codes = [r.code for r in record.results["stage0"].reasons]
    assert codes[0] == "unreadable"
    assert [a.method for a in record.results["stage0"].methods_tried] == ["open_pdfplumber", "open_pdfminer"]


def test_image_only_pdf_stops_at_stage1(tmp_path):
    record = run_pipeline(synth.make_image_only_pdf(tmp_path / "scan.pdf"))
    assert record.results["stage0"].status is Status.WARN
    assert record.stopped_at == 1
    assert "no_text_layer" in [f["code"] for f in record.flags]
    assert record.results["stage1"].per_unit["page_001"].methods_tried[0].method == "ocr_tesseract"
