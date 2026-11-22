"""The rotating hold-out (eval/folds.py): fixed folds stored as resume IDs in the data folder,
development groups only, aggregates and resume IDs only in the output. Synthetic data only."""

from __future__ import annotations

import json

import folds
import funnel
from eval_helpers import gt_all, write_ground_truth, write_prediction, write_stage_files
from resume_parser.schema import resume_id_for

VAL = "synthetic_c_variants"


def _setup(data_dir, bad: str | None = None):
    """Seven development resumes (copies of the fake ones under new names) and one validation
    resume. bad: a development stem whose prediction is empty, so it pulls its fold down."""
    base = gt_all()
    gts = {VAL: base[VAL]}
    for k in range(7):
        stem, src = f"fake_dev_{k}", list(base.values())[k % 3]
        gts[stem] = dict(src, resume_file=f"fake_dev_{k}.pdf")
    write_ground_truth(data_dir, gts, split={"dev": ["fake_dev_0.pdf", "fake_dev_1.pdf"],
                                             "dev2": ["fake_dev_2.json"], "validation": [f"{VAL}.json"]})
    for stem, gt in gts.items():
        pred = dict(gt, education=[], experience=[], publications=[]) if stem == bad else gt
        write_prediction(data_dir, stem, pred)
        write_stage_files(data_dir, stem, gt)
    return gts


def test_folds_are_fixed_and_hold_resume_ids_only(data_dir):
    _setup(data_dir)
    stems = folds.development_stems()
    assert VAL not in stems and len(stems) == 7
    a, b = folds.make_folds(stems), folds.make_folds(stems)
    assert a == b and len(a) == 3 and sorted(r for f in a for r in f) == sorted(resume_id_for(s) for s in stems)
    assert [len(f) for f in a] == [2, 2, 3]
    folds.load_or_make_folds(stems)
    text = folds.folds_path().read_text(encoding="utf-8")
    assert json.loads(text)["folds"] == a
    assert "fake_dev" not in text and resume_id_for(VAL) not in text


def test_saved_folds_must_match_the_development_resumes(data_dir, capsys):
    _setup(data_dir)
    folds.load_or_make_folds(folds.development_stems())
    saved = json.loads(folds.folds_path().read_text(encoding="utf-8"))
    saved["folds"][0] = saved["folds"][0][1:]
    folds.folds_path().write_text(json.dumps(saved), encoding="utf-8")
    assert folds.main([]) == 2 and "--rebuild" in capsys.readouterr().err
    funnel.main([])
    capsys.readouterr()
    assert folds.main(["--rebuild"]) == 0


def test_report_is_aggregates_and_ids_only_and_finds_the_resume_that_drags(data_dir, capsys):
    _setup(data_dir, bad="fake_dev_4")
    assert funnel.main([]) == 0
    capsys.readouterr()
    assert folds.main([]) == 0
    out = capsys.readouterr().out
    assert "NOT held-out scores" in out and "largest F1 spread" in out
    assert "fake_dev" not in out and resume_id_for(VAL) not in out
    for name in (g["candidate_name"] for g in gt_all().values()):
        assert name not in out
    result = folds.run()
    bad = resume_id_for("fake_dev_4")
    fold = next(f for f in result["folds"] if any(r == bad for sec in f["draggers"] for r, _ in f["draggers"][sec]))
    assert fold["draggers"]["publications"][0][0] == bad
    assert sum(f["funnel"]["experience"]["GT items"] for f in result["folds"]) == sum(
        len(g["experience"]) for s, g in _setup(data_dir, bad="fake_dev_4").items() if s != VAL)
