"""Build a fake data folder for the evaluator and funnel tests: ground truth, split,
predictions and saved stage files, all from the synthetic ground truth."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import make_synthetic_resumes as synth
from resume_parser.schema import resume_id_for

# dev = resume a, tuned = resume b (and anything else), test_frozen = resume c
SPLIT = {"dev": ["synthetic_a_simple.pdf"], "test_frozen": ["synthetic_c_variants.json"]}


def gt_all() -> dict[str, dict]:
    return copy.deepcopy(synth.synthetic_ground_truth())


def person_names() -> list[str]:
    return [gt["candidate_name"] for gt in synth.synthetic_ground_truth().values()]


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def write_ground_truth(data_dir: Path, gts: dict[str, dict] | None = None, split: dict | None = None) -> dict:
    gts = gts if gts is not None else gt_all()
    for stem, gt in gts.items():
        _dump(data_dir / "ground_truth_v2" / f"{stem}.json", gt)
    _dump(data_dir / "ground_truth_v2" / "_split.json", split if split is not None else SPLIT)
    return gts


def write_prediction(data_dir: Path, stem: str, pred: dict) -> None:
    _dump(data_dir / "outputs" / f"{stem}.json", pred)


# ---------------------------------------------------------------- stage files


def entry_line(section: str, e: dict) -> str:
    if section == "education":
        return f"{e['degree']} in {e['stream']}, {e['institution']}"
    if section == "experience":
        return f"{e['job_title']}, {e['institution']}, {e['dates']}"
    return f"{e['title']}. {e['venue']}, {e['year']}."


def write_stage_files(data_dir: Path, stem: str, gt: dict, drop=(), move_to_other=()) -> Path:
    """Stage 1, 3, 4 and 5 files as a perfect pipeline would save them.

    drop: (section, idx) items left out of the lines entirely.
    move_to_other: (section, idx) items kept in the lines but put in the "other" section.
    """
    sections = {"personal": [gt["candidate_name"]], "education": [], "experience": [],
                "publications": [], "other": []}
    for sec in ("education", "experience", "publications"):
        for idx, e in enumerate(gt[sec]):
            if (sec, idx) in drop:
                continue
            target = "other" if (sec, idx) in move_to_other else sec
            sections[target].append(entry_line(sec, e))
    all_lines = [t for name in sections for t in sections[name]]

    def record(stage, data, method_used, per_unit=None):
        return {"schema_version": "1.0", "resume_id": resume_id_for(stem), "stage": stage, "status": "OK",
                "method_used": method_used, "per_unit": per_unit or {}, "data": data}

    folder = data_dir / "reports" / "stages" / resume_id_for(stem)
    _dump(folder / "stage1_lines.json", record(
        "stage1", {"page_001": {"lines": [{"text": t} for t in all_lines]}}, "per_unit",
        {"page_001": {"method_used": "pdfplumber_words_tables", "status": "OK"}}))
    _dump(folder / "stage3_sections.json", record("stage3", {"blocks": []}, "heading_vocabulary"))
    _dump(folder / "stage4_canonical.json", record(
        "stage4",
        {"doc_id": resume_id_for(stem),
         "sections": [{"name": n, "heading_raw": n, "lines": [{"text": t} for t in ls]}
                      for n, ls in sections.items() if ls],
         "unassigned": []},
        "exact_alias_map"))
    _dump(folder / "stage5_entities.json", record(
        "stage5", None, "per_unit",
        {"personal": {"method_used": "top_lines", "status": "OK"},
         "education": {"method_used": "anchor_split", "status": "OK"},
         "experience": {"method_used": "anchor_split", "status": "OK"},
         "publications": {"method_used": "anchor_split", "status": "OK"}}))
    return folder
