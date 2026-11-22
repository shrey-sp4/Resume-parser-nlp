"""Stage 7 (output) and the file writer.

Per resume, under RESUME_DATA_DIR/outputs/:
- <name>.json       main output, GT v2 format, keys in the order of design 5.1
- <name>.meta.json  sidecar, sorted keys
and once per run: run_index.json (sorted keys, resumes sorted by file name).
With cli run --save-stages, also the stage files under RESUME_DATA_DIR/reports/stages/
<resume_id>/ (they hold resume text, so they never leave the data folder).

Every file is checked against its schema before it is written. Files are
UTF-8 with "\n" line endings, so output is byte-identical on every OS.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from resume_parser.final import require_inside_output_root
from resume_parser.pipeline import run_with_fallbacks
from resume_parser.schema import (
    SCHEMA_VERSION,
    TARGET_SECTIONS,
    Context,
    Method,
    MethodOutput,
    PipelineRecord,
    Reason,
    StageResult,
    Status,
    read_json,
    require_inside_data_root,
    schema_errors,
)

MAIN_KEYS = (
    "resume_file",
    "candidate_name",
    "education",
    "experience",
    "publications",
    "stated_totals",
    "notes",
)
ENTRY_KEYS = {
    "education": ("degree", "degree_level", "stream", "institution"),
    "experience": ("job_title", "institution", "dates", "experience_type"),
    "publications": ("title", "type", "year", "venue"),
}
TOTALS_KEYS = ("journal", "conference", "total")
INDEX_FILE = "run_index.json"


# ---------------------------------------------------------------- building


def _ordered(obj: Any, keys: tuple[str, ...]) -> Any:
    """Known keys first in the given order, any unknown keys after, sorted.

    Unknown keys are kept so the schema check catches them.
    """
    if not isinstance(obj, dict):
        return obj
    out = {k: obj[k] for k in keys if k in obj}
    out.update({k: obj[k] for k in sorted(obj) if k not in keys})
    return out


def build_main(resume_file: str, entities: Any) -> dict:
    """Build the main JSON from the stage 5 data (see entities/__init__.py)."""
    entities = entities if isinstance(entities, dict) else {}

    def section(name: str) -> dict:
        value = entities.get(name)
        return value if isinstance(value, dict) else {}

    def entries(name: str) -> list:
        return [_ordered(e, ENTRY_KEYS[name]) for e in section(name).get("entries", [])]

    totals = section("publications").get("stated_totals")
    if totals is None:
        totals = {k: None for k in TOTALS_KEYS}

    main = {
        "resume_file": resume_file,
        "candidate_name": section("personal").get("candidate_name", ""),
        "education": entries("education"),
        "experience": entries("experience"),
        "publications": entries("publications"),
        "stated_totals": _ordered(totals, TOTALS_KEYS),
        "notes": "",
    }
    return _ordered(main, MAIN_KEYS)


def build_and_validate(ctx: Context) -> MethodOutput:
    stage5 = ctx.results.get("stage5")
    main = build_main(ctx.resume_file, stage5.data if stage5 else None)
    return MethodOutput({"main": main, "errors": schema_errors(main, "resume.schema.json")})


def check_main(data: dict) -> float:
    return 0.0 if data["errors"] else 1.0


def run_output(pdf_path: Path, ctx: Context) -> StageResult:
    """Stage 7: build the main JSON and check it. If it is invalid, only the sidecar is written."""
    result = run_with_fallbacks(
        "stage7",
        (Method("build_and_validate", build_and_validate),),
        check_main,
        ctx.thresholds["accept_threshold"]["stage7"],
        payload=ctx,
    )
    errors = result.data["errors"] if result.data else []
    result.metrics["schema_errors"] = len(errors)
    if errors:
        result.status = Status.FAIL
        result.reasons = [Reason("main_json_invalid", errors[0], "document")]
        result.data = {"main": None, "errors": errors}
    return result


def _stage5_sidecar(record: PipelineRecord) -> tuple[dict, dict, dict, dict]:
    """personal_extra, provenance, excluded and parsed_dates from the stage 5 data (empty when
    stage 5 did not run). Keys follow the main JSON: candidate_name, education[0], experience[0], ..."""
    stage5 = record.results.get("stage5")
    data = stage5.data if stage5 is not None and isinstance(stage5.data, dict) else {}
    personal = data.get("personal") if isinstance(data.get("personal"), dict) else {}
    education = data.get("education") if isinstance(data.get("education"), dict) else {}
    experience = data.get("experience") if isinstance(data.get("experience"), dict) else {}
    publications = data.get("publications") if isinstance(data.get("publications"), dict) else {}
    extra = {"emails": [], "phones": [], "phone_digits": [], "links": []}
    extra.update(personal.get("personal_extra") or {})
    provenance = {}
    source = personal.get("name_source") or {}
    if source.get("page"):
        provenance["candidate_name"] = {"page": source["page"], "lines": list(source["lines"]), "raw_text": ""}
    for i, side in enumerate(education.get("sides") or []):
        provenance[f"education[{i}]"] = dict(side)
    parsed_dates = {}
    for i, side in enumerate(experience.get("sides") or []):
        provenance[f"experience[{i}]"] = dict(side)
    for i, side in enumerate(publications.get("sides") or []):
        provenance[f"publications[{i}]"] = dict(side)
    for i, dates in enumerate(experience.get("parsed_dates") or []):
        parsed_dates[f"experience[{i}]"] = {"start": dates.get("start"), "end": dates.get("end")}
    excluded = {"school_level_education": [dict(s) for s in education.get("excluded") or []],
                "non_paper_publications": []}
    return extra, provenance, excluded, parsed_dates


def decision_of(record: PipelineRecord) -> tuple[str, list[str]]:
    """(decision, sorted reason codes). A stage 0 or 1 failure stops the pipeline before stage 6:
    FAIL with stage<N>_failed. An invalid main JSON (stage 7) adds main_json_invalid and is at
    least NEEDS_REVIEW. Without a stage 6 result (a partial pipeline in tests) any flag means
    NEEDS_REVIEW."""
    if record.stopped_at is not None:
        return "FAIL", [f"stage{record.stopped_at}_failed"]
    stage6 = record.results.get("stage6")
    data = stage6.data if stage6 is not None and isinstance(stage6.data, dict) else None
    if data is None:
        return ("NEEDS_REVIEW" if record.flags else "ACCEPT"), []
    decision, reasons = data["decision"], list(data.get("decision_reasons") or [])
    stage7 = record.results.get("stage7")
    if stage7 is not None and stage7.status is Status.FAIL:
        reasons.append("main_json_invalid")
        decision = "FAIL" if decision == "FAIL" else "NEEDS_REVIEW"
    return decision, sorted(set(reasons))


def build_sidecar(record: PipelineRecord) -> dict:
    """Sidecar for one resume (design 5.2). Keys are sorted when written."""
    decision, reasons = decision_of(record)
    stage6 = record.results.get("stage6")
    stage6_data = stage6.data if stage6 and isinstance(stage6.data, dict) else {}
    scores = stage6_data.get("section_scores") or {s: None for s in TARGET_SECTIONS}
    extra, provenance, excluded, parsed_dates = _stage5_sidecar(record)
    return {
        "schema_version": SCHEMA_VERSION,
        "resume_id": record.resume_id,
        "resume_file": record.resume_file,
        "decision": decision,
        "needs_review": decision != "ACCEPT",
        "decision_reasons": reasons,
        "flags": list(record.flags),
        "section_scores": dict(scores),
        "personal_extra": extra,
        "provenance": provenance,
        "excluded": excluded,
        "parsed_dates": parsed_dates,
        "stage_report": {name: result.to_report() for name, result in record.results.items()},
    }


# ---------------------------------------------------------------- writing


def dump_json(obj: Any, sort_keys: bool) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=sort_keys) + "\n"


def _checked(obj: Any, schema_name: str) -> Any:
    errors = schema_errors(obj, schema_name)
    if errors:
        raise ValueError(f"{schema_name}: {len(errors)} error(s), first: {errors[0]}")
    return obj


def _write(path: Path, text: str) -> None:
    with Path(path).open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_resume(record: PipelineRecord, out_dir: Path) -> None:
    """Write the main JSON (when there is one) and the sidecar for one resume."""
    out_dir = require_inside_output_root(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(record.resume_file).stem
    main_path = out_dir / f"{stem}.json"
    if record.main is not None:
        _write(main_path, dump_json(_checked(record.main, "resume.schema.json"), sort_keys=False))
    elif main_path.exists():
        main_path.unlink()  # stale main JSON from an earlier run of the same file
    sidecar = _checked(record.sidecar, "meta.schema.json")
    _write(out_dir / f"{stem}.meta.json", dump_json(sidecar, sort_keys=True))


def build_index(out_dir: Path) -> dict:
    """Index of every sidecar in the folder, sorted by resume file name."""
    resumes = []
    for path in sorted(Path(out_dir).glob("*.meta.json")):
        meta = read_json(path)
        resumes.append(
            {
                "decision": meta["decision"],
                "flags_count": len(meta["flags"]),
                "reasons": list(meta.get("decision_reasons") or []),
                "resume_file": meta["resume_file"],
                "resume_id": meta["resume_id"],
            }
        )
    resumes.sort(key=lambda r: r["resume_file"])
    return {"schema_version": SCHEMA_VERSION, "resumes": resumes}


def write_index(out_dir: Path) -> dict:
    out_dir = require_inside_output_root(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    index = _checked(build_index(out_dir), "run_index.schema.json")
    _write(out_dir / INDEX_FILE, dump_json(index, sort_keys=True))
    return index


REVIEW_QUEUE_FILE = "review_queue.json"


def build_review_queue(index: dict) -> dict:
    """Resumes that need a person (NEEDS_REVIEW or FAIL): resume IDs and reason codes only,
    sorted by resume ID, so the queue never shows a file name."""
    rows = [{"decision": r["decision"], "reasons": list(r["reasons"]), "resume_id": r["resume_id"]}
            for r in index["resumes"] if r["decision"] != "ACCEPT"]
    rows.sort(key=lambda r: r["resume_id"])
    return {"schema_version": SCHEMA_VERSION, "resumes": rows}


def write_review_queue(index: dict, queue_dir: Path) -> dict:
    """RESUME_DATA_DIR/review_queue/review_queue.json, rebuilt from the run index."""
    queue_dir = require_inside_data_root(queue_dir)
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue = _checked(build_review_queue(index), "review_queue.schema.json")
    _write(queue_dir / REVIEW_QUEUE_FILE, dump_json(queue, sort_keys=True))
    return queue


# ---------------------------------------------------------------- unknown headings

UNKNOWN_HEADINGS_FILE = "stage3_unknown_headings.txt"


def unknown_headings(record: PipelineRecord) -> list[str]:
    """Texts of the heading-like lines stage 3 did not find in the alias map."""
    stage3, stage2 = record.results.get("stage3"), record.results.get("stage2")
    if stage3 is None or not stage3.data or stage2 is None or not stage2.data:
        return []
    by_id = {line["id"]: line["text"] for line in stage2.data["lines"]}
    return [by_id[i] for i in stage3.data["unknown_headings"] if i in by_id]


def write_unknown_headings(rows: list[tuple[str, str]], reports_dir: Path) -> Path:
    """A review list of (resume_id, heading text), sorted, for adding aliases by hand. It holds
    resume text, so it is written only inside the data folder and never printed."""
    folder = require_inside_data_root(reports_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / UNKNOWN_HEADINGS_FILE
    _write(path, "".join(f"{rid}\t{text}\n" for rid, text in sorted(rows)))
    return path


# ---------------------------------------------------------------- saved stage files

# stage -> file name under RESUME_DATA_DIR/reports/stages/<resume_id>/
STAGE_FILES = {
    "stage1": "stage1_lines.json",
    "stage3": "stage3_sections.json",
    "stage4": "stage4_canonical.json",
    "stage5": "stage5_entities.json",
}
# every stage 1 method's lines per page, so the funnel can say which method had an item
METHODS_FILE = "stage1_methods.json"


def build_stage_file(record: PipelineRecord, stage: str) -> dict:
    """One saved stage: its status, the method choices and the data passed forward.

    The data holds resume text, so these files are only ever written inside the data folder.
    """
    result = record.results[stage]
    return {
        "schema_version": SCHEMA_VERSION,
        "resume_id": record.resume_id,
        "stage": stage,
        "status": result.status.value,
        "method_used": result.method_used,
        "per_unit": {
            unit: {"method_used": choice.method_used, "status": choice.status.value}
            for unit, choice in result.per_unit.items()
        },
        "data": result.data,
    }


def write_stage_files(record: PipelineRecord, stages_dir: Path) -> list[Path]:
    """Write the saved stage files of one resume (cli run --save-stages).

    Files from an earlier run are removed first, so a resume that now stops earlier leaves
    no stale stage files behind. A stage that did not run gets no file.
    """
    folder = require_inside_output_root(Path(stages_dir) / record.resume_id)
    folder.mkdir(parents=True, exist_ok=True)
    for name in (*STAGE_FILES.values(), METHODS_FILE):
        (folder / name).unlink(missing_ok=True)
    written = []
    for stage, name in STAGE_FILES.items():
        if stage in record.results:
            path = folder / name
            _write(path, dump_json(build_stage_file(record, stage), sort_keys=True))
            written.append(path)
    stage1 = record.results.get("stage1")
    if stage1 is not None and stage1.extra.get("outputs"):
        path = folder / METHODS_FILE
        methods = {"schema_version": SCHEMA_VERSION, "resume_id": record.resume_id, "stage": "stage1",
                   "methods": stage1.extra["outputs"]}
        _write(path, dump_json(methods, sort_keys=True))
        written.append(path)
    return written
