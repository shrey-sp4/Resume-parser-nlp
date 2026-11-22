"""The validation group: aggregates only, every run logged, a warning from the 4th run on, never
in per-item files, lost-item lists, the unknown-headings file or the determinism check; and the
data check refuses its IDs in the repo. Synthetic data only."""

from __future__ import annotations

import json

import check_no_data
import evaluate
import funnel
from eval_helpers import gt_all, write_ground_truth, write_prediction, write_stage_files
from resume_parser.cli import main as cli_main
from resume_parser.groups import load_groups
from resume_parser.schema import resume_id_for

VAL = "synthetic_c_variants"
DEV2 = "synthetic_b_table"
SPLIT = {"dev": ["synthetic_a_simple.pdf"], "dev2": [f"{DEV2}.json"], "validation": [f"{VAL}.json"]}


def _setup(data_dir, stage_files=True):
    gts = write_ground_truth(data_dir, gt_all(), split=SPLIT)
    for stem, gt in gts.items():
        write_prediction(data_dir, stem, gt)
        if stage_files:
            write_stage_files(data_dir, stem, gt)
    return gts


def _log(data_dir) -> list[dict]:
    path = data_dir / "reports" / "validation_runs.log"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def test_groups_are_read_from_the_split(data_dir):
    _setup(data_dir, stage_files=False)
    groups = load_groups()
    assert groups["validation"] == [VAL] and groups["dev2"] == [DEV2] and groups["tuned"] == []
    assert groups["test_frozen"] == []


def test_evaluate_validation_is_aggregates_only_and_logged_every_run(data_dir, capsys):
    _setup(data_dir, stage_files=False)
    assert evaluate.main(["--json", "reports/eval.json"]) == 0
    out = capsys.readouterr().out
    assert "=== validation" not in out and "=== dev2" in out and _log(data_dir) == []

    for run in range(1, 5):
        assert evaluate.main(["--validation", "--json", "reports/eval.json"]) == 0
        out = capsys.readouterr().out
        assert "=== validation" in out and "aggregates only" in out
        assert resume_id_for(VAL) not in out
        results = json.loads((data_dir / "reports" / "eval.json").read_text(encoding="utf-8"))
        assert "per_resume" not in results["validation"]
        entry = _log(data_dir)[-1]
        assert entry["tool"] == "evaluate" and entry["run"] == run
        warned = "validation has been used 4 times; results are no longer a fair estimate"
        if run < 4:
            assert entry["warning"] == "" and "WARNING" not in out
        else:  # never blocked, but warned in the report and in the log line
            assert entry["warning"] == warned and f"WARNING: {warned}" in out
            assert results["validation"]["warning"] == warned


def test_funnel_validation_writes_nothing_per_item(data_dir, capsys):
    _setup(data_dir)
    assert funnel.main(["--validation"]) == 0
    out = capsys.readouterr().out
    assert "validation: 1 resume(s)  (aggregates only)" in out
    folder = data_dir / "reports" / "funnel"
    assert (folder / "items_dev2.jsonl").is_file() and not (folder / "items_validation.jsonl").exists()
    assert _log(data_dir)[-1]["tool"] == "funnel"
    assert funnel.main(["--lost-items", "--stage", "S4"]) == 0
    assert resume_id_for(VAL) not in capsys.readouterr().out


def test_cli_shows_validation_as_counts_only(data_dir, capsys):
    import make_synthetic_resumes as synth

    synth.build_all(data_dir / "pdfs")
    write_ground_truth(data_dir, gt_all(), split=SPLIT)
    assert cli_main(["run", "--save-stages", "--groups", "dev,validation"]) == 0
    out = capsys.readouterr().out
    assert "validation: 1 processed (not shown per resume)" in out
    assert resume_id_for(VAL) not in out
    review = (data_dir / "reports" / "stage3_unknown_headings.txt").read_text(encoding="utf-8")
    assert resume_id_for(VAL) not in review
    assert cli_main(["check-determinism", "--groups", "validation"]) == 2


def test_data_check_refuses_validation_ids_in_the_repo(data_dir, tmp_path, monkeypatch):
    _setup(data_dir, stage_files=False)
    monkeypatch.setenv("RESUME_DATA_DIR", str(data_dir))
    markers = check_no_data.guarded_markers()
    assert resume_id_for(VAL) in markers and resume_id_for(DEV2) not in markers
    (tmp_path / "notes.md").write_text(f"see {resume_id_for(VAL)}\n", encoding="utf-8")
    problems = check_no_data.find_problems(["notes.md"], tmp_path, markers)
    assert problems == ["notes.md:1: guarded-group resume ID or file name in file contents"]
    assert resume_id_for(VAL) not in problems[0]
