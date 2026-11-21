"""Loss funnel: each deliberately removed item is assigned to the right stage, false
positives get the right cause, methods are reported, and test_frozen runs once only."""

from __future__ import annotations

import json
import re

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
    assert items[("experience", 1)]["cause"] == "wrong fields: dates"

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
    assert "school-level" in out.split("FALSE-POSITIVE CAUSES")[1]  # the education group


def test_resume_without_stage_files_is_reported(data_dir, capsys):
    gts = write_ground_truth(data_dir)
    for stem, gt in gts.items():
        write_prediction(data_dir, stem, gt)
    assert funnel.main([]) == 0
    assert "--save-stages" in capsys.readouterr().out
    assert all(r["lost_at"] == "S1 text" for r in _items(data_dir).values())


def test_runs_on_stage_files_saved_by_the_pipeline(data_dir, capsys):
    """Real stage files from the pipeline: every GT item of the fake resumes is found in the
    stage 1 lines and the right section, and every section is extracted."""
    synth.build_all(data_dir / "pdfs")
    assert cli_main(["run", "--allow-stubs", "--save-stages"]) == 0
    write_ground_truth(data_dir)
    assert funnel.main([]) == 0
    assert "--save-stages" not in capsys.readouterr().out  # the files are there
    for group in ("dev", "tuned"):
        items = _items(data_dir, group).values()
        assert items and all(r["s1_ge95"] for r in items), [r["lost_at"] for r in items]
        built = [r for r in items if r["section"] in ("name", "education", "experience", "publications")]
        assert built and all(r["lost_at"] in ("ok", "S5 fields") for r in built),             [(r["section"], r["idx"], r["lost_at"], r["cause"]) for r in built]


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


# ---------------------------------------------------------------- what the funnel prints

LOST_ROW = re.compile(
    r"^r_[0-9a-f]{8}\s+(name|education|experience|publications)\s+\d+\s+"
    r"(garbled|glued words|table cell|wrong order|missing text|other)\s+(\S+)$"
)


def _gt_strings() -> set[str]:
    strings = set()
    for gt in synth.synthetic_ground_truth().values():
        strings.add(gt["candidate_name"])
        for sec in ("education", "experience", "publications"):
            for e in gt[sec]:
                strings |= {v for k, v in e.items() if k not in ("degree_level", "experience_type", "type", "year")
                            and len(v) > 6}
    return strings


def test_lost_items_print_only_ids_sections_indexes_causes_and_methods(data_dir, capsys):
    _setup(data_dir)
    assert funnel.main(["--lost-items"]) == 0
    out = capsys.readouterr().out
    header, *rows = out.strip().splitlines()
    assert header.split() == ["resume_id", "section", "index", "cause", "recoverable_by"]
    assert rows, "the removed publication must be listed"
    assert all(LOST_ROW.match(r) for r in rows), rows
    assert f"{resume_id_for(DEV)} " in rows[0] and "publications" in rows[0]


def test_funnel_output_has_no_text_names_or_stems(data_dir, capsys):
    _setup(data_dir)
    assert funnel.main([]) == 0
    assert funnel.main(["--lost-items"]) == 0
    out = capsys.readouterr().out
    for s in _gt_strings():
        assert s not in out, s
    assert "synthetic_" not in out and ".pdf" not in out and ".json" not in out


def test_s1_causes(data_dir, capsys):
    """Words out of order are 'wrong order'; a title of short words with its spaces lost is
    'glued words' (with only a few spaces lost, fuzzy matching still finds it)."""
    from eval_helpers import gt_all

    gts = gt_all()
    gts[DEV]["publications"][1]["title"] = "To be or not to be is up to us as we go on"
    write_ground_truth(data_dir, gts)
    for stem, gt in gts.items():
        write_prediction(data_dir, stem, gt)
        write_stage_files(data_dir, stem, gt)
    gt = gts[DEV]
    folder = write_stage_files(data_dir, DEV, gt, drop={("publications", 0), ("publications", 1)})
    path = folder / "stage1_lines.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    words = gt["publications"][0]["title"].split()
    record["data"]["page_001"]["lines"] += [{"text": " ".join(reversed(words))},
                                            {"text": "".join(gt["publications"][1]["title"].split())}]
    path.write_text(json.dumps(record), encoding="utf-8")
    assert funnel.main([]) == 0
    items = _items(data_dir)
    assert items[("publications", 0)]["cause"] == "wrong order"
    assert items[("publications", 1)]["cause"] == "glued words"


# ---------------------------------------------------------------- S4 rows and check validity

S4_ROW = re.compile(
    r"^r_[0-9a-f]{8}\s+(name|education|experience|publications)\s+\d+\s+"
    r"(wrong section: (personal|education|experience|publications|other|unassigned)|in text but in no section)\s+"
    r"stage3_\S+_/_stage4_\S+$"
)


def test_lost_items_for_s4_print_ids_causes_and_methods_only(data_dir, capsys):
    _setup(data_dir)
    assert funnel.main(["--lost-items", "--stage", "S4"]) == 0
    out = capsys.readouterr().out
    header, *rows = out.strip().splitlines()
    assert header.split() == ["resume_id", "section", "index", "cause", "method"]
    assert rows and all(S4_ROW.match(r) for r in rows), rows
    assert any(" education " in r and "wrong section: other" in r for r in rows)
    for s in _gt_strings():
        assert s not in out


def test_check_validity_counts():
    fired_loss = ({("stage1", "below_accept_threshold"), ("stage4", "missing_target_section:education")},
                  {("S1 text", "publications"), ("S4 section", "education")})
    fired_no_loss = ({("stage1", "below_accept_threshold")}, set())
    missed = (set(), {("S1 text", "name")})
    rows = funnel.check_validity([fired_loss, fired_no_loss, missed])
    assert rows[("stage1", "below_accept_threshold")] == {"fired": 2, "fired_with_loss": 1, "missed": 1}
    assert rows[("stage4", "missing_target_section:education")] == {"fired": 1, "fired_with_loss": 1, "missed": 0}
    assert rows[("stage1", "status WARN")] == {"fired": 0, "fired_with_loss": 0, "missed": 2}


def test_check_validity_table_is_printed_from_sidecars(data_dir, capsys):
    synth.build_all(data_dir / "pdfs")
    assert cli_main(["run", "--allow-stubs", "--save-stages"]) == 0
    write_ground_truth(data_dir)
    capsys.readouterr()
    assert funnel.main([]) == 0
    out = capsys.readouterr().out
    table = out.split("=== CHECK VALIDITY")[1]
    assert "fired/+loss/missed" in table and "not_implemented" not in table  # every stage is built
    assert "test_frozen" not in table


def test_header_label_needs_an_entry_without_degree_institution_or_year():
    gt_all = {"education": [], "experience": [], "publications": []}
    real = {"degree": "M.Sc.", "stream": "", "institution": "Lakeside University", "dates": ""}
    header = {"degree": "", "stream": "Board University Year", "institution": "", "dates": ""}
    assert funnel.classify_fp("education", real, gt_all, set(), []) != "table header or skills line"
    assert funnel.classify_fp("education", header, gt_all, set(), []) == "table header or skills line"
