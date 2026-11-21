"""Evaluator: perfect and damaged predictions, missing predictions, the test_frozen guard,
and no names in any output. Synthetic data only, all inside the test's data folder."""

from __future__ import annotations

import json

import pytest

import evaluate
from eval_helpers import gt_all, person_names, write_ground_truth, write_prediction
from resume_parser.schema import read_json, resume_id_for

FROZEN_STEM = "synthetic_c_variants"


def _score(data_dir, capsys, *args):
    """Run the evaluator with --json and return (exit code, results, console text)."""
    code = evaluate.main(["--json", "reports/eval.json", *args])
    captured = capsys.readouterr()
    path = data_dir / "reports" / "eval.json"
    results = read_json(path) if path.is_file() else None
    return code, results, captured.out + captured.err


def _perfect_setup(data_dir):
    gts = write_ground_truth(data_dir)
    for stem, gt in gts.items():
        write_prediction(data_dir, stem, gt)
    return gts


def test_ground_truth_against_itself_scores_100(data_dir, capsys):
    _perfect_setup(data_dir)
    code, res, _ = _score(data_dir, capsys)
    assert code == 0
    for group in ("dev", "tuned"):
        point = res[group]["point"]
        assert point, group
        assert all(v == 1.0 for v in point.values()), {k: v for k, v in point.items() if v != 1.0}
        assert res[group]["fields"]["pub_venue"]["n"] > 0


def test_damaged_copy_scores_lower_in_the_expected_fields(data_dir, capsys):
    gts = _perfect_setup(data_dir)
    stem = "synthetic_a_simple"  # the only dev resume
    bad = json.loads(json.dumps(gts[stem]))
    bad["candidate_name"] = "Dr. " + bad["candidate_name"]
    bad["experience"][0]["dates"] = "2023 - now"
    del bad["publications"][1]
    bad["education"][1]["institution"] = "Lakeside College"
    write_prediction(data_dir, stem, bad)

    code, res, _ = _score(data_dir, capsys)
    assert code == 0
    p = res["dev"]["point"]
    assert p["name_acc"] == 0.0 and p["name_norm_acc"] == 1.0
    assert p["exp_dates_acc"] == pytest.approx(2 / 3)
    assert p["publications_R"] == 0.5 and p["publications_P"] == 1.0
    assert p["edu_institution_acc"] == pytest.approx(2 / 3)
    assert p["education_R"] == 1.0  # still matched, only the field is wrong
    damaged = {"name_acc", "exp_dates_acc", "publications_R", "publications_F1", "edu_institution_acc"}
    assert all(v == 1.0 for k, v in p.items() if k not in damaged)
    assert all(v == 1.0 for v in res["tuned"]["point"].values())


def test_missing_prediction_counts_every_entry_as_missed(data_dir, capsys):
    gts = _perfect_setup(data_dir)
    (data_dir / "outputs" / "synthetic_a_simple.json").unlink()
    code, res, out = _score(data_dir, capsys)
    assert code == 0
    dev = res["dev"]
    assert dev["missing_predictions"] == 1
    assert dev["counts"]["education"]["fn"] == len(gts["synthetic_a_simple"]["education"])
    assert dev["point"]["education_R"] == 0.0
    assert "no prediction" in out


# ---------------------------------------------------------------- test_frozen guard


def _log(data_dir) -> list[dict]:
    path = data_dir / "reports" / "test_frozen_runs.log"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def test_test_frozen_is_not_scored_without_the_flag(data_dir, capsys):
    _perfect_setup(data_dir)
    code, res, out = _score(data_dir, capsys)
    assert code == 0
    assert "test_frozen" not in res
    assert "=== test_frozen" not in out
    assert resume_id_for(FROZEN_STEM) not in out
    assert _log(data_dir) == []


def test_test_frozen_runs_once_then_needs_a_written_reason(data_dir, capsys):
    _perfect_setup(data_dir)

    code, res, out = _score(data_dir, capsys, "--test-frozen")
    assert code == 0
    assert "=== test_frozen" in out and "aggregates only" in out
    assert resume_id_for(FROZEN_STEM) not in out  # never per resume
    assert "per_resume" not in res["test_frozen"]
    (entry,) = _log(data_dir)
    assert entry["tool"] == "evaluate" and entry["run"] == 1 and entry["reason"] == ""
    for key in ("script_sha256", "common_sha256", "eval_settings_sha256", "pipeline_thresholds_sha256", "utc"):
        assert entry[key]

    code, _, out = _score(data_dir, capsys, "--test-frozen")
    assert code == 2 and "already been run" in out
    code, _, _ = _score(data_dir, capsys, "--test-frozen", "--allow-rerun", "  ")
    assert code == 2
    assert len(_log(data_dir)) == 1

    code, _, _ = _score(data_dir, capsys, "--test-frozen", "--allow-rerun", "fixed a bug in the date check")
    assert code == 0
    second = _log(data_dir)[1]
    assert second["run"] == 2 and second["reason"] == "fixed a bug in the date check"


def test_allow_rerun_needs_test_frozen(data_dir, capsys):
    _perfect_setup(data_dir)
    code, _, out = _score(data_dir, capsys, "--allow-rerun", "why")
    assert code == 2 and "--test-frozen" in out


def test_json_outside_the_data_folder_is_refused(data_dir, tmp_path, capsys):
    _perfect_setup(data_dir)
    assert evaluate.main(["--json", str(tmp_path / "elsewhere.json")]) == 2
    assert not (tmp_path / "elsewhere.json").exists()


def test_split_problems_give_one_line_messages(data_dir, capsys):
    write_ground_truth(data_dir, split={"dev": ["not_there"], "test_frozen": []})
    assert evaluate.main([]) == 2
    err = capsys.readouterr().err.strip()
    assert err.count("\n") == 0 and "no ground truth" in err and "not_there" not in err


# ---------------------------------------------------------------- privacy


def test_no_names_or_file_names_in_any_output(data_dir, capsys):
    gts = gt_all()
    gts["jane_secret_cv"] = dict(gts["synthetic_b_table"], resume_file="jane_secret_cv.pdf")
    write_ground_truth(data_dir, gts)
    for stem, gt in gts.items():
        write_prediction(data_dir, stem, gt)

    code, _, out = _score(data_dir, capsys, "--test-frozen")
    assert code == 0
    assert evaluate.main(["--calibrate"]) == 0
    out += capsys.readouterr().out
    written = "".join(p.read_text(encoding="utf-8") for p in (data_dir / "reports").rglob("*") if p.is_file())
    for text in (out, written):
        for name in person_names():
            assert name not in text
        assert "secret" not in text and "synthetic_" not in text
    assert resume_id_for("jane_secret_cv") in out  # tuned rows are shown by ID
    id_map = read_json(data_dir / "private" / "id_map.json")
    assert id_map[resume_id_for("jane_secret_cv")]["file"] == "jane_secret_cv"
