"""Funnel report for a run: RESUME_DATA_DIR/reports/run_summary.json.

Built from every sidecar in outputs/, so it always matches run_index.json.
Shows resume IDs only, never names or file names. Keys are sorted.

Contents:
- stages: per stage, how many resumes entered it, and how many ended OK, WARN,
  FAIL or stopped there, and how many choices were passed as OK only because
  the methods are not built yet (not_implemented: pages in stage 1, sections in
  stage 5, one per resume in other stages)
- method_usage: per stage, how often each method was chosen (per unit for
  stages that choose per page or per section)
- fallbacks_used: per stage, choices where the chosen method was not the first
  implemented one
- top_reason_codes: the most common flag codes
- flagged_resumes: resume IDs with the section and code of each flag
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from resume_parser.pipeline import GATED_STAGES
from resume_parser.schema import SCHEMA_VERSION, STAGE_NAMES, read_json, require_inside_data_root
from resume_parser.writer import _write, dump_json

SUMMARY_FILE = "run_summary.json"
TOP_CODES = 10


def _choices(stage: dict) -> list[tuple[str | None, list[dict], str]]:
    """(method_used, methods_tried, status) for each choice in a stage report."""
    if stage["per_unit"]:
        return [
            (u["method_used"], u["methods_tried"], u["status"])
            for _, u in sorted(stage["per_unit"].items())
        ]
    return [(stage["method_used"], stage["methods_tried"], stage["status"])]


def _is_fallback(method_used: str | None, tried: list[dict]) -> bool:
    if method_used is None:
        return False
    implemented = [a["method"] for a in tried if a["status"] not in ("NOT_IMPLEMENTED", "SKIPPED")]
    return bool(implemented) and implemented[0] != method_used


def build_run_summary(out_dir: Path) -> dict:
    sidecars = [read_json(p) for p in sorted(Path(out_dir).glob("*.meta.json"))]
    sidecars.sort(key=lambda m: m["resume_id"])

    stages = {
        name: {"entered": 0, "ok": 0, "warn": 0, "fail": 0, "stopped": 0, "not_implemented": 0}
        for name in STAGE_NAMES
    }
    usage: dict[str, Counter] = {name: Counter() for name in STAGE_NAMES}
    fallbacks = {name: 0 for name in STAGE_NAMES}
    codes: Counter = Counter()
    decisions: Counter = Counter()
    flagged = []

    for meta in sidecars:
        decisions[meta["decision"]] += 1
        for name, stage in meta["stage_report"].items():
            counts = stages[name]
            counts["entered"] += 1
            counts[stage["status"].lower()] += 1
            if stage["status"] == "FAIL" and STAGE_NAMES.index(name) in GATED_STAGES:
                counts["stopped"] += 1
            for method_used, tried, status in _choices(stage):
                usage[name][method_used or "none"] += 1
                fallbacks[name] += _is_fallback(method_used, tried)
                # No method chosen and still OK only happens when stubs were left.
                counts["not_implemented"] += method_used is None and status == "OK"
        for flag in meta["flags"]:
            codes[flag["code"]] += 1
        if meta["flags"]:
            flagged.append(
                {
                    "resume_id": meta["resume_id"],
                    "flags": [{"section": f["section"], "code": f["code"]} for f in meta["flags"]],
                }
            )

    top = sorted(codes.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_CODES]
    return {
        "schema_version": SCHEMA_VERSION,
        "resumes_total": len(sidecars),
        "decisions": {d: decisions.get(d, 0) for d in ("ACCEPT", "NEEDS_REVIEW", "FAIL")},
        "stages": stages,
        "method_usage": {name: dict(sorted(c.items())) for name, c in usage.items()},
        "fallbacks_used": {**fallbacks, "total": sum(fallbacks.values())},
        "top_reason_codes": [{"code": code, "count": n} for code, n in top],
        "flagged_resumes": flagged,
    }


def write_run_summary(out_dir: Path, reports_dir: Path) -> dict:
    reports_dir = require_inside_data_root(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    summary = build_run_summary(out_dir)
    _write(reports_dir / SUMMARY_FILE, dump_json(summary, sort_keys=True))
    return summary
