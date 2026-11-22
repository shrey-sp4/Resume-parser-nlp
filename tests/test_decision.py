"""Stages 6 and 7: the decision per resume, its reason codes, the run index, the review queue,
determinism and schema validity. Synthetic data only."""

from __future__ import annotations

import copy
import re

import make_synthetic_resumes as synth
from make_synthetic_resumes import EDU_LINES, EXP_LINES, PERSONAL_TOP, PUB_LINES
from resume_parser.cli import main as cli_main
from resume_parser.pipeline import run_pipeline, unimplemented_methods
from resume_parser.schema import (
    Attempt,
    AttemptStatus,
    Context,
    StageResult,
    Status,
    UnitChoice,
    load_thresholds,
    read_json,
    schema_errors,
)
from resume_parser.validate import decide

FULL = (PERSONAL_TOP + [("heading", "EDUCATION")] + EDU_LINES + [("heading", "WORK EXPERIENCE")] + EXP_LINES
        + [("heading", "PUBLICATIONS")] + PUB_LINES)


def _pdf(tmp_path, lines, name="cv_test.pdf"):
    return synth.make_lines_pdf(tmp_path / name, lines)


def _valid(record):
    assert schema_errors(record.sidecar, "meta.schema.json") == []
    if record.main is not None:
        assert schema_errors(record.main, "resume.schema.json") == []
        assert list(record.main) == ["resume_file", "candidate_name", "education", "experience", "publications",
                                     "stated_totals", "notes"]  # the GT v2 key order


# ---------------------------------------------------------------- end to end, one path each


def test_accept(tmp_path):
    record = run_pipeline(_pdf(tmp_path, FULL))
    assert (record.sidecar["decision"], record.sidecar["decision_reasons"]) == ("ACCEPT", [])
    assert all(v is not None for v in record.sidecar["section_scores"].values())
    _valid(record)


def test_missing_section(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION")] + EDU_LINES + [("heading", "PUBLICATIONS")] + PUB_LINES
    record = run_pipeline(_pdf(tmp_path, lines))
    assert record.sidecar["decision"] == "NEEDS_REVIEW"
    assert "missing_section:experience" in record.sidecar["decision_reasons"]
    assert record.sidecar["section_scores"]["experience"] is None
    _valid(record)


def test_low_score_with_a_stricter_threshold(tmp_path):
    thresholds = copy.deepcopy(load_thresholds())
    thresholds["section_score_threshold"]["education"] = 1.01
    record = run_pipeline(_pdf(tmp_path, FULL), thresholds=thresholds)
    assert record.sidecar["decision"] == "NEEDS_REVIEW"
    assert record.sidecar["decision_reasons"] == ["low_score:education"]
    _valid(record)


def test_name_from_email(tmp_path):
    lines = [("contact", "mira.testwood@example.org | +00 0000 000 001")] + FULL[2:]
    record = run_pipeline(_pdf(tmp_path, lines))
    assert record.sidecar["decision"] == "NEEDS_REVIEW"
    assert "name_from_email" in record.sidecar["decision_reasons"]
    _valid(record)


def test_name_from_file_name(tmp_path):
    lines = [("contact", "+00 0000 000 001")] + FULL[2:]
    record = run_pipeline(_pdf(tmp_path, lines, "mira_testwood_cv.pdf"))
    assert record.sidecar["decision"] == "NEEDS_REVIEW"
    assert "name_from_file_name" in record.sidecar["decision_reasons"]
    _valid(record)


def test_empty_name_fails_but_keeps_the_main_json(tmp_path):
    lines = [("contact", "+00 0000 000 001")] + FULL[2:]
    record = run_pipeline(_pdf(tmp_path, lines, "cv.pdf"))
    assert record.sidecar["decision"] == "FAIL"
    assert "empty_name" in record.sidecar["decision_reasons"]
    assert record.main is not None and record.main["candidate_name"] == ""
    _valid(record)


def test_stage1_failure_fails_with_no_main_json(tmp_path):
    record = run_pipeline(synth.make_image_only_pdf(tmp_path / "scan.pdf"))
    assert record.sidecar["decision"] == "FAIL"
    assert record.sidecar["decision_reasons"] in (["stage0_failed"], ["stage1_failed"])
    assert record.main is None
    _valid(record)


# ---------------------------------------------------------------- the rule itself, one reason each


def _ctx(*, sections=("education", "experience", "publications"), methods=None, scores=None, entries=None,
         name="Mira Testwood") -> Context:
    methods = methods or {}
    scores = scores or {}
    entries = entries or {}
    per_unit, data = {}, {"personal": {"candidate_name": name}}
    for unit in ("personal", "education", "experience", "publications"):
        method = methods.get(unit, "top_lines" if unit == "personal" else "anchor_split")
        attempt = Attempt(method, scores.get(unit, 1.0), AttemptStatus.ACCEPTED)
        per_unit[unit] = UnitChoice(method, (attempt,), Status.OK)
        if unit != "personal":
            data[unit] = {"entries": [{}] * entries.get(unit, 1), "lines_total": 3 if unit in sections else 0}
    stage4 = StageResult("stage4", Status.OK, data={"sections": [{"name": s, "lines": [{"id": "x"}]} for s in sections]})
    stage5 = StageResult("stage5", Status.OK, data=data, per_unit=per_unit)
    return Context(resume_file="x.pdf", thresholds=load_thresholds(), results={"stage4": stage4, "stage5": stage5})


def _codes(ctx):
    decision, reasons, _scores = decide(ctx)
    return decision, [r.code for r in reasons]


def test_rule_accept():
    assert _codes(_ctx()) == ("ACCEPT", [])


def test_rule_each_review_reason():
    assert _codes(_ctx(sections=("education", "publications"))) == ("NEEDS_REVIEW", ["missing_section:experience"])
    assert _codes(_ctx(scores={"publications": 0.5})) == ("NEEDS_REVIEW", ["low_score:publications"])
    assert _codes(_ctx(scores={"personal": 0.5})) == ("NEEDS_REVIEW", ["low_score:name"])
    assert _codes(_ctx(methods={"experience": "whole_section_one_entry"})) == (
        "NEEDS_REVIEW", ["whole_section_one_entry:experience"])
    assert _codes(_ctx(methods={"personal": "from_email"})) == ("NEEDS_REVIEW", ["name_from_email"])
    assert _codes(_ctx(methods={"personal": "from_file_name"})) == ("NEEDS_REVIEW", ["name_from_file_name"])
    assert _codes(_ctx(entries={"education": 0})) == ("NEEDS_REVIEW", ["no_entries:education"])


def test_rule_empty_name_fails():
    assert _codes(_ctx(name="")) == ("FAIL", ["empty_name"])


# ---------------------------------------------------------------- run index, review queue, determinism


def test_index_and_review_queue_hold_ids_and_codes(data_dir, capsys):
    synth.build_all(data_dir / "pdfs")
    synth.make_lines_pdf(data_dir / "pdfs" / "synthetic_e_noexp.pdf",
                         PERSONAL_TOP + [("heading", "EDUCATION")] + EDU_LINES)
    synth.make_image_only_pdf(data_dir / "pdfs" / "synthetic_d_scan.pdf")
    assert unimplemented_methods() == []
    assert cli_main(["run"]) == 0  # no --allow-stubs needed: every method is built
    out = capsys.readouterr().out
    for line in out.strip().splitlines():
        assert re.match(r"^(r_[0-9a-f]{8} (ACCEPT|NEEDS_REVIEW|FAIL) flags=\d+ unknown_headings=\d+.*|done: .*)$",
                        line), line
    index = read_json(data_dir / "outputs" / "run_index.json")
    queue = read_json(data_dir / "review_queue" / "review_queue.json")
    assert schema_errors(index, "run_index.schema.json") == [] and schema_errors(queue, "review_queue.schema.json") == []
    decisions = sorted(r["decision"] for r in index["resumes"])
    assert decisions == ["ACCEPT", "ACCEPT", "ACCEPT", "FAIL", "NEEDS_REVIEW"]
    # the queue holds every resume that is not ACCEPT, sorted by ID, with the index's reasons
    expected = sorted((r["resume_id"], r["decision"], r["reasons"]) for r in index["resumes"] if r["decision"] != "ACCEPT")
    assert [(r["resume_id"], r["decision"], r["reasons"]) for r in queue["resumes"]] == expected
    reasons = {r["decision"]: r["reasons"] for r in queue["resumes"]}
    assert "missing_section:experience" in reasons["NEEDS_REVIEW"]
    assert reasons["FAIL"] in (["stage0_failed"], ["stage1_failed"])
    text = (data_dir / "review_queue" / "review_queue.json").read_text(encoding="utf-8")
    assert "synthetic" not in text and ".pdf" not in text  # IDs only, never file names
    for meta in (data_dir / "outputs").glob("*.meta.json"):
        assert schema_errors(read_json(meta), "meta.schema.json") == []


def test_two_runs_are_byte_identical(data_dir, capsys):
    synth.build_all(data_dir / "pdfs")
    assert cli_main(["check-determinism"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"check-determinism: 3 resume\(s\), \d+ file\(s\) compared, 0 differ", out)
    assert all(line.startswith(("check-determinism:", "  r_")) for line in out.strip().splitlines())
