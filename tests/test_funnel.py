"""Loss funnel: each deliberately removed item is assigned to the right stage, false
positives get the right cause, methods are reported, and test_frozen runs once only."""

from __future__ import annotations

import json

import pytest

import funnel
import make_synthetic_resumes as synth
from eval_helpers import person_names, write_ground_truth, write_prediction, write_stage_files
from resume_parser.cli import main as cli_main
from resume_parser.schema import resume_id_for

DEV = "synthetic_a_simple"


def _items(data_dir, group="dev") -> dict:
    path = data_dir / "reports" / "funnel" / f"items_{group}.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    return {(r["section"], r["idx"]): r for r in rows}


def _fps(data_dir, group="dev") -> list[dict]:
    path = data_dir / "reports" / "funnel" / f"fps_{group}.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _setup(data_dir):
    """Every resume perfect, then resume a (dev) damaged on purpose, one loss per stage."""
    gts = write_ground_truth(data_dir)
    for stem, gt in gts.items():
        write_prediction(data_dir, stem, gt)
        write_stage_files(data_dir, stem, gt)

    gt = gts[DEV]
    write_stage_files(data_dir, DEV, gt, drop={("publications", 1)}, move_to_other={("education", 2)})
    pred = json.loads(json.dumps(gt))
    del pred["publications"][1]                        # gone from the lines too -> S1
    del pred["experience"][2]                          # never predicted -> S5 entry
    pred["experience"][1]["dates"] = "2021 - 2023"     # wrong field -> S5 fields
    pred["publications"][0]["title"] = "Modelling gene networks with"  # cut short
    pred["education"].append({"degree": "Class XII", "degree_level": "other", "stream": "",
                              "institution": "Fakeville Public School"})  # school-level false positive
    write_prediction(data_dir, DEV, pred)
    return gts


def test_each_removed_item_is_lost_at_the_right_stage(data_dir, capsys):
    _setup(data_dir)
    assert funnel.main([]) == 0
    items = _items(data_dir)

    assert items[("publications", 1)]["lost_at"] == "S1 text"
    assert items[("education", 2)]["lost_at"] == "S4 section"
    assert items[("education", 2)]["cause"] == "wrong section: other"
    assert items[("experience", 2)]["lost_at"] == "S5 entry"
    assert items[("experience", 1)]["lost_at"] == "S5 fields"
    assert items[("experience", 1)]["cause"] == "wrong: dates"

    cut = items[("publications", 0)]
    assert cut["lost_at"] == "ok" and cut["completeness"] == "truncated" and cut["cut_at"] == "S5 entry"

    untouched = [("name", 0), ("education", 0), ("education", 1), ("experience", 0)]
    assert all(items[k]["lost_at"] == "ok" and items[k]["completeness"] == "complete" for k in untouched)
    # tuned is perfect
    assert all(r["lost_at"] == "ok" for r in _items(data_dir, "tuned").values())


def test_false_positive_cause_and_methods_are_reported(data_dir, capsys):
    _setup(data_dir)
    assert funnel.main([]) == 0
    out = capsys.readouterr().out
    assert [(f["section"], f["cause"]) for f in _fps(data_dir)] == [("education", "school-level entry")]

    items = _items(data_dir)
    assert items[("experience", 2)]["method"] == "stage5 anchor_split"
    assert items[("publications", 1)]["method"] == "stage1 pdfplumber_words_tables"
    assert items[("education", 2)]["method"] == "stage3 heading_vocabulary / stage4 exact_alias_map"
    method_lines = out.split("=== METHOD THAT HANDLED EACH LOST ITEM ===")[1].split("===")[0]
    assert "S5 entry" in method_lines and "stage5 anchor_split" in method_lines
    assert "school-level entry" in out


def test_resume_without_stage_files_is_reported(data_dir, capsys):
    gts = write_ground_truth(data_dir)
    for stem, gt in gts.items():
        write_prediction(data_dir, stem, gt)
    assert funnel.main([]) == 0
    assert "--save-stages" in capsys.readouterr().out
    assert all(r["lost_at"] == "S1 text" for r in _items(data_dir).values())


def test_runs_on_stage_files_saved_by_the_pipeline(data_dir, capsys):
    """The stubs save empty stage data; the funnel must read those files without failing."""
    synth.build_all(data_dir / "pdfs")
    assert cli_main(["run", "--allow-stubs", "--save-stages"]) == 0
    write_ground_truth(data_dir)
    assert funnel.main([]) == 0
    assert "--save-stages" not in capsys.readouterr().out  # the files are there
    assert all(r["lost_at"] == "S1 text" for r in _items(data_dir).values())


# ---------------------------------------------------------------- test_frozen guard


def _log(data_dir) -> list[dict]:
    path = data_dir / "reports" / "test_frozen_runs.log"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def test_test_frozen_only_with_the_flag_and_only_once(data_dir, capsys):
    _setup(data_dir)
    assert funnel.main([]) == 0
    assert "test_frozen" not in capsys.readouterr().out
    assert _log(data_dir) == []

    assert funnel.main(["--test-frozen"]) == 0
    out = capsys.readouterr().out
    assert "test_frozen" in out and "aggregates only" in out
    assert resume_id_for("synthetic_c_variants") not in out
    assert not (data_dir / "reports" / "funnel" / "items_test_frozen.jsonl").exists()
    (entry,) = _log(data_dir)
    assert entry["tool"] == "funnel" and entry["run"] == 1 and entry["pipeline_thresholds_sha256"]

    assert funnel.main(["--test-frozen"]) == 2
    assert "already been run" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        funnel.main(["--test-frozen", "--allow-rerun", "please"])
    assert len(_log(data_dir)) == 1


def test_evaluate_and_funnel_keep_separate_counters(data_dir, capsys):
    import evaluate

    _setup(data_dir)
    assert evaluate.main(["--test-frozen"]) == 0
    assert funnel.main(["--test-frozen"]) == 0  # the evaluator's run does not use up the funnel's
    assert [(e["tool"], e["run"]) for e in _log(data_dir)] == [("evaluate", 1), ("funnel", 1)]


def test_no_names_in_any_output(data_dir, capsys):
    _setup(data_dir)
    assert funnel.main(["--test-frozen"]) == 0
    out = capsys.readouterr().out
    written = "".join(p.read_text(encoding="utf-8") for p in (data_dir / "reports" / "funnel").glob("*"))
    for text in (out, written):
        for name in person_names():
            assert name not in text
        assert "synthetic_" not in text
        assert "Northfield" not in text and "Modelling gene" not in text  # no resume text either
