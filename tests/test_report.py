"""The funnel report counts match what the run did."""

from __future__ import annotations

import make_synthetic_resumes as synth
from resume_parser.cli import main
from resume_parser.schema import read_json, resume_id_for


def _summary(data_dir):
    return read_json(data_dir / "reports" / "run_summary.json")


def test_report_counts_for_three_fake_resumes(data_dir, capsys):
    synth.build_all(data_dir / "pdfs")
    assert main(["run", "--allow-stubs"]) == 0
    s = _summary(data_dir)

    assert s["resumes_total"] == 3
    assert s["decisions"] == {"ACCEPT": 3, "NEEDS_REVIEW": 0}
    for i in range(8):
        counts = dict(s["stages"][f"stage{i}"])
        counts.pop("not_implemented")
        assert counts == {"entered": 3, "ok": 3, "warn": 0, "fail": 0, "stopped": 0}
    assert s["method_usage"]["stage0"] == {"open_pdfplumber": 3}
    assert s["method_usage"]["stage1"] == {"pdfplumber_words_tables": 3}  # one page each
    assert s["method_usage"]["stage2"] == {"full_clean": 3}
    assert s["method_usage"]["stage3"] == {"heading_vocabulary": 3}
    assert s["method_usage"]["stage4"] == {"exact_alias_map": 15}  # 5 blocks each
    # every stage 5 section is built
    assert s["method_usage"]["stage5"] == {"anchor_split": 2, "date_anchor_split": 3, "marker_split": 3,
                                           "table_row_split": 1, "top_lines": 3}
    assert s["method_usage"]["stage7"] == {"build_and_validate": 3}
    # the table resume: anchor_split leaves table sections to table_row_split
    assert s["fallbacks_used"]["total"] == 1 and s["fallbacks_used"]["stage5"] == 1
    assert s["top_reason_codes"] == []
    assert s["flagged_resumes"] == []

    index = read_json(data_dir / "outputs" / "run_index.json")
    assert [r["decision"] for r in index["resumes"]] == ["ACCEPT"] * 3


def _page_total(data_dir) -> int:
    sidecars = (data_dir / "outputs").glob("*.meta.json")
    return sum(read_json(p)["stage_report"]["stage0"]["metrics"]["page_count"] for p in sidecars)


def test_report_counts_units_passed_as_not_implemented(data_dir, capsys):
    synth.build_all(data_dir / "pdfs")
    assert main(["run", "--allow-stubs"]) == 0
    stages = _summary(data_dir)["stages"]
    passed = {name: counts["not_implemented"] for name, counts in stages.items()}
    assert passed == {
        "stage0": 0,
        "stage1": 0,  # built: every page is read
        "stage2": 0,  # built
        "stage3": 0,  # built
        "stage4": 0,  # built
        "stage5": 0,  # built
        "stage6": 0,
        "stage7": 0,
    }


def test_report_counts_with_a_stopped_resume(data_dir, capsys):
    synth.build_all(data_dir / "pdfs")
    synth.make_image_only_pdf(data_dir / "pdfs" / "synthetic_d_scan.pdf")
    assert main(["run", "--allow-stubs"]) == 0
    s = _summary(data_dir)

    assert s["resumes_total"] == 4
    assert s["decisions"] == {"ACCEPT": 3, "NEEDS_REVIEW": 1}
    assert s["stages"]["stage0"] == {
        "entered": 4, "ok": 3, "warn": 1, "fail": 0, "stopped": 0, "not_implemented": 0
    }
    # The scanned page FAILs, so it is not counted as passed.
    assert s["stages"]["stage1"] == {
        "entered": 4, "ok": 3, "warn": 0, "fail": 1, "stopped": 1, "not_implemented": 0
    }
    assert s["stages"]["stage2"]["entered"] == 3

    scan_id = resume_id_for("synthetic_d_scan.pdf")
    assert [f["resume_id"] for f in s["flagged_resumes"]] == [scan_id]
    flags = s["flagged_resumes"][0]["flags"]
    assert {"section": "document", "code": "needs_ocr"} in flags
    assert {"section": "document", "code": "no_text_layer"} in flags
    codes = {c["code"]: c["count"] for c in s["top_reason_codes"]}
    assert codes["needs_ocr"] == 1 and codes["no_text_layer"] == 1


def test_report_shows_ids_not_file_names(data_dir, capsys):
    synth.build_all(data_dir / "pdfs")
    main(["run", "--allow-stubs"])
    text = (data_dir / "reports" / "run_summary.json").read_text(encoding="utf-8")
    assert "synthetic_" not in text
