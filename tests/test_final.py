"""The final set: folders outside the repo and the data folder, the strictest guard (every run
logged, a second run refused without --allow-rerun), aggregates only, and never part of the
development tools. Synthetic resumes only, in temporary folders."""

from __future__ import annotations

import json
import re

import pytest

import calibrate_thresholds
import check_no_data
import end_to_end
import evaluate
import funnel
import make_synthetic_resumes as synth
from resume_parser import final
from resume_parser.cli import main as cli_main
from resume_parser.schema import REPO_ROOT, DataDirError, resume_id_for

ID = re.compile(r"\b[re]_[0-9a-f]{8}\b")


@pytest.fixture
def final_dirs(tmp_path, monkeypatch, data_dir):
    pdfs, gt, out = tmp_path / "final_pdfs", tmp_path / "final_gt", tmp_path / "final_out"
    synth.build_all(pdfs)
    gt.mkdir()
    for stem, record in synth.synthetic_ground_truth().items():
        (gt / f"{stem}.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setenv(final.ENV_PDFS, str(pdfs))
    monkeypatch.setenv(final.ENV_GT, str(gt))
    monkeypatch.setenv(final.ENV_OUT, str(out))
    return {"pdfs": pdfs, "gt": gt, "out": out}


def _stems():
    return sorted(synth.synthetic_ground_truth())


def _log(out):
    path = out / final.LOG_FILE
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def _no_names(text: str) -> None:
    for stem in _stems():
        assert stem not in text


# ---------------------------------------------------------------- folders


def test_folders_outside_the_repo_and_the_data_folder_only(final_dirs, data_dir, monkeypatch, tmp_path):
    assert final.final_pdfs_dir() == final_dirs["pdfs"].resolve()
    for value, message in ((str(REPO_ROOT / "docs"), "outside the repo"),
                           (str(data_dir / "pdfs"), "outside RESUME_DATA_DIR"),
                           (str(tmp_path), "outside RESUME_DATA_DIR"),  # a folder above the data folder
                           (str(tmp_path / "missing"), "existing folder")):
        monkeypatch.setenv(final.ENV_PDFS, value)
        with pytest.raises(final.FinalError, match=message):
            final.final_pdfs_dir()
    monkeypatch.delenv(final.ENV_PDFS)
    with pytest.raises(final.FinalError, match="not set"):
        final.final_pdfs()


def test_only_files_directly_inside_the_folders_are_read(final_dirs):
    (final_dirs["pdfs"] / "sub").mkdir()
    (final_dirs["pdfs"] / "sub" / "nested_resume.pdf").write_bytes(b"%PDF-1.4")
    (final_dirs["pdfs"] / "notes.txt").write_text("x", encoding="utf-8")
    (final_dirs["gt"] / "sub").mkdir()
    (final_dirs["gt"] / "sub" / "nested_resume.json").write_text("{}", encoding="utf-8")
    assert [p.stem for p in final.final_pdfs()] == _stems()
    assert final.final_stems() == _stems()


def test_writes_outside_the_final_output_folder_are_refused(final_dirs, tmp_path):
    assert final.require_inside_final_out(final_dirs["out"] / "outputs" / "x.json")
    with pytest.raises(final.FinalError):
        final.require_inside_final_out(tmp_path / "elsewhere" / "x.json")
    with pytest.raises(DataDirError):
        final.require_inside_output_root(REPO_ROOT / "outputs")


# ---------------------------------------------------------------- validate-final


def test_validate_final_counts_and_ids_only(final_dirs, capsys):
    assert cli_main(["validate-final"]) == 0
    out = capsys.readouterr().out
    assert "3 PDF(s), 3 GT file(s)" in out and "failing the GT v2 schema: 0" in out
    a, b = _stems()[0], _stems()[1]
    record = json.loads((final_dirs["gt"] / f"{a}.json").read_text(encoding="utf-8"))
    (final_dirs["gt"] / f"{a}.json").write_text(json.dumps(dict(record, resume_file="other.pdf")), encoding="utf-8")
    broken = json.loads((final_dirs["gt"] / f"{b}.json").read_text(encoding="utf-8"))
    broken.pop("candidate_name")
    (final_dirs["gt"] / f"{b}.json").write_text(json.dumps(broken), encoding="utf-8")
    assert cli_main(["validate-final"]) == 1
    out = capsys.readouterr().out
    assert f"names no PDF in the folder: 1 ({resume_id_for(a + '.json')})" in out
    assert f"failing the GT v2 schema: 1 ({resume_id_for(b + '.json')})" in out
    assert "PDFs with no GT file: 1" in out
    _no_names(out)
    assert not final_dirs["out"].joinpath(final.LOG_FILE).exists()  # never a run


# ---------------------------------------------------------------- run, evaluate, funnel


def test_run_final_writes_only_under_its_folder_and_refuses_a_second_run(final_dirs, data_dir, capsys):
    assert cli_main(["run", "--groups", "final"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("final: 3 processed, 0 error(s)") and not ID.search(out)
    _no_names(out)
    written = final_dirs["out"]
    assert sorted(p.name for p in (written / "outputs").glob("*.meta.json")) == [f"{s}.meta.json" for s in _stems()]
    assert (written / "outputs" / "run_index.json").is_file() and len(list((written / "stages").iterdir())) == 3
    summary = json.loads((written / final.SUMMARY_FILE).read_text(encoding="utf-8"))
    assert "flagged_resumes" not in summary and summary["resumes_total"] == 3
    for folder in ("outputs", "review_queue", "reports"):  # nothing in the data folder
        assert not any((data_dir / folder).rglob("*.json")) and not any((data_dir / folder).rglob("*.txt"))
    assert [e["run"] for e in _log(written)] == [1]

    assert cli_main(["run", "--groups", "final"]) == 2
    assert "refusing" in capsys.readouterr().err
    assert len(_log(written)) == 1
    assert cli_main(["run", "--groups", "final", "--allow-rerun", "checked a fix"]) == 0
    assert [(e["run"], e["rerun_reason"]) for e in _log(written)] == [(1, ""), (2, "checked a fix")]
    assert {"script_sha256", "code_sha256", "settings_sha256", "pipeline_thresholds_sha256", "utc"} <= set(_log(written)[0])


def test_final_never_mixes_with_other_groups(final_dirs, capsys):
    assert cli_main(["run", "--groups", "final,dev"]) == 2
    assert cli_main(["check-determinism", "--groups", "final"]) == 2
    assert "final is not allowed here" in capsys.readouterr().err
    assert cli_main(["run", "--allow-rerun", "x"]) == 2  # --allow-rerun is only for final
    assert not final_dirs["out"].joinpath(final.LOG_FILE).exists()


def test_evaluate_and_funnel_final_are_aggregates_only_and_guarded(final_dirs, data_dir, capsys):
    assert cli_main(["run", "--groups", "final"]) == 0
    capsys.readouterr()
    for tool, module in (("evaluate", evaluate), ("funnel", funnel)):
        assert module.main(["--final"]) == 0
        out = capsys.readouterr().out
        assert "final" in out and not ID.search(out), tool
        _no_names(out)
        assert module.main(["--final"]) == 2, tool  # a second run is refused
        assert "refusing" in capsys.readouterr().err
        assert module.main(["--final", "--allow-rerun", "after a fix"]) == 0, tool
        capsys.readouterr()
        assert [e["run"] for e in _log(final_dirs["out"]) if e["tool"] == tool] == [1, 2]
    assert evaluate.main(["--final", "--validation"]) == 2
    assert not (data_dir / "reports" / "funnel").exists()  # no per-item file
    funnel.main(["--final", "--allow-rerun", "look"])
    out = capsys.readouterr().out
    assert "LARGEST LOSS POINTS" not in out and "METHOD THAT HANDLED" not in out


def test_development_tools_never_see_final(final_dirs):
    with pytest.raises(SystemExit):
        calibrate_thresholds.main(["--final"])
    assert "final" not in end_to_end.GROUPS
    assert "final" not in evaluate.common.DEV_GROUPS


# ---------------------------------------------------------------- the repo check


def test_data_check_refuses_final_ids_and_stems_in_the_repo(final_dirs, tmp_path):
    markers = check_no_data.final_markers()
    stem = _stems()[0]
    assert stem in markers and resume_id_for(stem) in markers
    assert markers <= check_no_data.guarded_markers()
    (tmp_path / "notes.md").write_text(f"see {resume_id_for(stem)}\n", encoding="utf-8")
    problems = check_no_data.find_problems(["notes.md"], tmp_path, markers)
    assert problems == ["notes.md:1: guarded-group resume ID or file name in file contents"]
