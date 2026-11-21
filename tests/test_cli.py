"""Command line: data folder checks and validate-gt output (resume IDs only)."""

from __future__ import annotations

import json

import pytest

from resume_parser.cli import main
from resume_parser.schema import resume_id_for
from test_schema import VALID_MAIN


def test_help_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "validate-gt" in capsys.readouterr().out


def test_unset_data_dir_gives_one_line_message(monkeypatch, capsys):
    monkeypatch.delenv("RESUME_DATA_DIR")
    assert main(["run", "--allow-stubs"]) == 2
    err = capsys.readouterr().err.strip()
    assert err.count("\n") == 0 and "RESUME_DATA_DIR" in err


def test_missing_data_dir_gives_one_line_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("RESUME_DATA_DIR", str(tmp_path / "nowhere"))
    assert main(["validate-gt"]) == 2
    assert "RESUME_DATA_DIR" in capsys.readouterr().err


def test_validate_gt_prints_ids_only(data_dir, capsys):
    gt = data_dir / "ground_truth_v2"
    good = dict(VALID_MAIN, candidate_name="Secret Person")
    bad = dict(VALID_MAIN, candidate_name="Other Secret", notes=None)
    (gt / "jane_secret_cv.json").write_text(json.dumps(good), encoding="utf-8")
    (gt / "john_secret_cv.json").write_text(json.dumps(bad), encoding="utf-8")
    (gt / "_template.json").write_text("{}", encoding="utf-8")

    assert main(["validate-gt"]) == 1
    out = capsys.readouterr().out
    assert f"{resume_id_for('jane_secret_cv.json')} OK" in out
    assert f"{resume_id_for('john_secret_cv.json')} INVALID" in out
    assert "notes: type" in out
    assert "checked 2 file(s), 1 invalid" in out
    assert "secret" not in out.lower()
    assert "template" not in out


# ---------------------------------------------------------------- stubs guard


def test_run_refuses_while_stubs_are_left(data_dir, capsys):
    import make_synthetic_resumes as synth
    from resume_parser.pipeline import unimplemented_methods

    synth.build_all(data_dir / "pdfs")
    assert main(["run"]) == 2
    captured = capsys.readouterr()
    stubs = unimplemented_methods()
    assert stubs
    for name in stubs:
        assert f"  {name}\n" in captured.err
    assert "--allow-stubs" in captured.err
    assert captured.out == ""  # nothing processed
    assert list((data_dir / "outputs").iterdir()) == []
    assert not (data_dir / "reports" / "run_summary.json").exists()


def test_run_with_allow_stubs_processes(data_dir, capsys):
    import make_synthetic_resumes as synth

    synth.build_all(data_dir / "pdfs")
    assert main(["run", "--allow-stubs"]) == 0
    assert "--allow-stubs given" in capsys.readouterr().err
    assert (data_dir / "outputs" / "run_index.json").is_file()
    assert (data_dir / "reports" / "run_summary.json").is_file()


def test_run_without_flag_works_once_no_stubs_are_left(data_dir, monkeypatch, capsys):
    import make_synthetic_resumes as synth
    from resume_parser import pipeline

    monkeypatch.setattr(pipeline, "unimplemented_methods", lambda: [])
    synth.build_all(data_dir / "pdfs")
    assert main(["run"]) == 0
    assert (data_dir / "outputs" / "run_index.json").is_file()


# ---------------------------------------------------------------- saved stage files

STAGE_FILE_NAMES = ["stage1_lines.json", "stage1_methods.json", "stage3_sections.json",
                    "stage4_canonical.json", "stage5_entities.json"]


def _files_outside(tmp_path, data_dir) -> set:
    return {p for p in tmp_path.rglob("*") if p.is_file() and data_dir not in p.parents}


def test_save_stages_writes_four_files_inside_the_data_folder_only(data_dir, tmp_path, capsys):
    import make_synthetic_resumes as synth

    synth.build_all(data_dir / "pdfs")
    before = _files_outside(tmp_path, data_dir)
    assert main(["run", "--allow-stubs", "--save-stages"]) == 0
    assert _files_outside(tmp_path, data_dir) == before

    stages = data_dir / "reports" / "stages"
    rid = resume_id_for("synthetic_a_simple.pdf")
    assert sorted(p.name for p in (stages / rid).iterdir()) == STAGE_FILE_NAMES
    assert sorted(p.name for p in stages.iterdir()) == sorted(
        resume_id_for(p.name) for p in (data_dir / "pdfs").glob("*.pdf"))
    record = json.loads((stages / rid / "stage1_lines.json").read_text(encoding="utf-8"))
    assert record["resume_id"] == rid and record["stage"] == "stage1"
    assert set(record) == {"schema_version", "resume_id", "stage", "status", "method_used", "per_unit", "data"}


def test_save_stages_removes_stale_files(data_dir, capsys):
    import make_synthetic_resumes as synth

    pdf = synth.make_image_only_pdf(data_dir / "pdfs" / "scan.pdf")  # stops at stage 1
    stale = data_dir / "reports" / "stages" / resume_id_for(pdf.name) / "stage4_canonical.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}", encoding="utf-8")
    assert main(["run", "--allow-stubs", "--save-stages"]) == 0
    assert sorted(p.name for p in stale.parent.iterdir()) == ["stage1_lines.json", "stage1_methods.json"]


def test_no_stage_files_without_the_flag(data_dir, capsys):
    import make_synthetic_resumes as synth

    synth.build_all(data_dir / "pdfs")
    assert main(["run", "--allow-stubs"]) == 0
    assert not (data_dir / "reports" / "stages").exists()


# ---------------------------------------------------------------- --groups


def _labelled(data_dir):
    """Fake PDFs plus ground truth: dev = resume a, tuned = resume b, test_frozen = resume c."""
    import make_synthetic_resumes as synth
    from eval_helpers import write_ground_truth

    synth.build_all(data_dir / "pdfs")
    write_ground_truth(data_dir)


def _processed(data_dir) -> set[str]:
    return {p.name[: -len(".meta.json")] for p in (data_dir / "outputs").glob("*.meta.json")}


def test_groups_default_to_dev_and_tuned(data_dir, capsys):
    _labelled(data_dir)
    assert main(["run", "--allow-stubs"]) == 0
    out = capsys.readouterr().out
    assert _processed(data_dir) == {"synthetic_a_simple", "synthetic_b_table"}
    assert "groups: dev 1, tuned 1" in out
    assert resume_id_for("synthetic_c_variants") not in out


def test_test_frozen_runs_only_when_named_and_is_not_shown_per_resume(data_dir, capsys):
    _labelled(data_dir)
    assert main(["run", "--allow-stubs", "--groups", "test_frozen"]) == 0
    out = capsys.readouterr().out
    assert _processed(data_dir) == {"synthetic_c_variants"}
    assert resume_id_for("synthetic_c_variants") not in out
    assert "test_frozen: 1 processed (not shown per resume)" in out


def test_one_test_frozen_pdf_is_refused_unless_named(data_dir, capsys):
    _labelled(data_dir)
    pdf = str(data_dir / "pdfs" / "synthetic_c_variants.pdf")
    assert main(["run", "--allow-stubs", pdf]) == 2
    assert "not in the groups" in capsys.readouterr().err
    assert _processed(data_dir) == set()
    assert main(["run", "--allow-stubs", "--groups", "dev,test_frozen", pdf]) == 0


def test_bad_group_names_and_missing_split_are_refused(data_dir, capsys):
    import make_synthetic_resumes as synth

    synth.build_all(data_dir / "pdfs")
    assert main(["run", "--allow-stubs", "--groups", "dev"]) == 2  # no _split.json yet
    _labelled(data_dir)
    assert main(["run", "--allow-stubs", "--groups", "dev,everything"]) == 2
    assert "--groups takes" in capsys.readouterr().err
    assert _processed(data_dir) == set()
