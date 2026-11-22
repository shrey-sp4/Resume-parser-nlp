"""Shared helpers for eval/: data folders, groups, private IDs and the validation and test_frozen guards.

Everything is read from and written to RESUME_DATA_DIR only:
    ground_truth_v2/<stem>.json      ground truth (GT v2, docs/design.md section 5.1)
    ground_truth_v2/_split.json      {"dev", "dev2", "validation", "test_frozen": [...]}; tuned = every
                                     other GT file (validation and test_frozen are guarded)
                                     (read by resume_parser.groups, shared with cli run --groups)
    outputs/<stem>.json              predictions (same format as the ground truth)
    reports/stages/<resume_id>/      saved stage files (cli run --save-stages)
    reports/test_frozen_runs.log     one JSON line per test_frozen run
    reports/validation_runs.log      one JSON line per validation run (a warning from the 4th on)
    private/salt.txt, id_map.json    salt for eval IDs, and the map from IDs to file names

The final set lives outside RESUME_DATA_DIR (resume_parser/final.py). use_final() points
gt_dir, outputs_dir and stages_dir at its folders for one evaluate.py --final or funnel.py
--final run; its log is RESUME_FINAL_OUT/final_runs.log.

Two kinds of ID:
- resume_id ("r_" + sha256(stem)[:8]) is the pipeline's ID. Console tables show it, so a row
  can be found in outputs/ and the sidecars.
- eval_id ("e_" + sha256(salt + stem)[:8]) is used in files that eval/ writes (--json results,
  funnel item files). Without the private salt it cannot be traced back to a file name.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import secrets
from pathlib import Path

from resume_parser import final as final_mod
from resume_parser import groups as groups_mod
from resume_parser.schema import CONFIG_DIR, data_root, read_json, require_inside_data_root, resume_id_for

GROUPS = groups_mod.GT_GROUPS
DEV_GROUPS = groups_mod.DEV_GROUPS
GUARDED_GROUPS = groups_mod.GUARDED_GROUPS
AGGREGATE_ONLY_GROUPS = groups_mod.AGGREGATE_ONLY_GROUPS  # guarded groups and final
FINAL_GROUP = final_mod.FINAL_GROUP
_FINAL = {"on": False}
FROZEN_LOG = "test_frozen_runs.log"


class EvalError(RuntimeError):
    """A problem with the inputs. main() prints it as one line and exits with code 2."""


# ---------------------------------------------------------------- folders


def use_final(on: bool = True) -> None:
    """Point gt_dir, outputs_dir and stages_dir at the final folders for one final run; the
    tools switch it off again when the run ends (use_final(False)), whatever happened."""
    _FINAL["on"] = on


def final_on() -> bool:
    return _FINAL["on"]


def gt_dir() -> Path:
    return final_mod.final_gt_dir() if _FINAL["on"] else data_root() / "ground_truth_v2"


def outputs_dir() -> Path:
    return final_mod.final_out() / "outputs" if _FINAL["on"] else data_root() / "outputs"


def reports_dir() -> Path:
    return data_root() / "reports"


def stages_dir() -> Path:
    return final_mod.final_out() / "stages" if _FINAL["on"] else reports_dir() / "stages"


def final_groups() -> dict[str, list[str]]:
    """{"final": stems of the GT files directly inside RESUME_FINAL_GT}."""
    try:
        return {FINAL_GROUP: final_mod.final_stems()}
    except final_mod.FinalError as exc:
        raise EvalError(str(exc)) from None


def log_final_run(tool: str, script_path: Path, settings: dict, rerun_reason: str | None) -> dict:
    try:
        return final_mod.log_final_run(tool, script_path, settings, rerun_reason)
    except final_mod.FinalError as exc:
        raise EvalError(str(exc)) from None


def private_dir() -> Path:
    return data_root() / "private"


def write_text_inside(path: Path, text: str) -> Path:
    """Write a UTF-8 file with \\n newlines, only under RESUME_DATA_DIR."""
    path = require_inside_data_root(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


# ---------------------------------------------------------------- groups


def load_groups() -> dict[str, list[str]]:
    """Stems per group, each list sorted. tuned is every GT file not in dev or test_frozen."""
    try:
        return groups_mod.load_groups()
    except groups_mod.GroupError as exc:
        raise EvalError(str(exc)) from None


# ---------------------------------------------------------------- IDs


def _salt() -> str:
    path = private_dir() / "salt.txt"
    if path.is_file():
        salt = path.read_text(encoding="utf-8").strip()
        if salt:
            return salt
    salt = secrets.token_hex(16)
    write_text_inside(path, salt + "\n")
    return salt


def eval_id_for(stem: str, salt: str) -> str:
    return "e_" + hashlib.sha256((salt + stem).encode("utf-8")).hexdigest()[:8]


def ids(stems: list[str]) -> dict[str, dict[str, str]]:
    """stem -> {"resume_id", "eval_id"}. Adds the stems to the private ID map."""
    salt = _salt()
    out = {s: {"resume_id": resume_id_for(s), "eval_id": eval_id_for(s, salt)} for s in sorted(set(stems))}
    map_path = private_dir() / "id_map.json"
    id_map = read_json(map_path) if map_path.is_file() else {}
    for stem, v in out.items():
        id_map[v["resume_id"]] = {"eval_id": v["eval_id"], "file": stem}
    write_text_inside(map_path, json.dumps(id_map, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return out


# ---------------------------------------------------------------- test_frozen guard


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_settings(settings: dict) -> str:
    text = json.dumps(settings, sort_keys=True, ensure_ascii=False, default=list)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _log_path() -> Path:
    return reports_dir() / FROZEN_LOG


def frozen_runs(tool: str) -> list[dict]:
    """Earlier test_frozen runs of one tool, from the log."""
    path = _log_path()
    if not path.is_file():
        return []
    runs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            if entry.get("tool") == tool:
                runs.append(entry)
    return runs


def require_frozen_allowed(tool: str, rerun_reason: str | None = None, rerun_allowed: bool = True) -> None:
    """Refuse a second test_frozen run unless reruns are allowed and a reason is written."""
    earlier = frozen_runs(tool)
    if not earlier:
        return
    if not rerun_allowed:
        raise EvalError(f"refusing --test-frozen: {tool} has already been run on test_frozen once.")
    if not (rerun_reason or "").strip():
        raise EvalError(
            f"refusing --test-frozen: {tool} has already been run on test_frozen "
            f"{len(earlier)} time(s); pass --allow-rerun \"<reason>\" to run it again."
        )


# ---------------------------------------------------------------- validation guard

VALIDATION_LOG = "validation_runs.log"
VALIDATION_WARN_FROM = 4  # from this run of a tool on, its validation results are no longer fair


def validation_runs(tool: str) -> list[dict]:
    """Earlier validation runs of one tool, from reports/validation_runs.log."""
    path = reports_dir() / VALIDATION_LOG
    if not path.is_file():
        return []
    return [e for e in (json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip())
            if e.get("tool") == tool]


def validation_warning(run: int) -> str:
    if run < VALIDATION_WARN_FROM:
        return ""
    return f"validation has been used {run} times; results are no longer a fair estimate"


def log_validation_run(tool: str, script_path: Path, settings: dict) -> dict:
    """Append one line for this validation run (never refused). From the 4th run of a tool on
    the line, and every report of that run, carries a warning."""
    run = len(validation_runs(tool)) + 1
    entry = {
        "tool": tool,
        "run": run,
        "utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "script_sha256": sha256_file(script_path),
        "common_sha256": sha256_file(Path(__file__)),
        "eval_settings_sha256": sha256_settings(settings),
        "pipeline_thresholds_sha256": sha256_file(CONFIG_DIR / "thresholds.json"),
        "warning": validation_warning(run),
    }
    path = require_inside_data_root(reports_dir() / VALIDATION_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def log_frozen_run(tool: str, script_path: Path, settings: dict, reason: str | None = None) -> dict:
    """Append one line for this run. Called before scoring, so a crashed run still counts."""
    entry = {
        "tool": tool,
        "run": len(frozen_runs(tool)) + 1,
        "utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "script_sha256": sha256_file(script_path),
        "common_sha256": sha256_file(Path(__file__)),
        "eval_settings_sha256": sha256_settings(settings),
        "pipeline_thresholds_sha256": sha256_file(CONFIG_DIR / "thresholds.json"),
        "reason": (reason or "").strip(),
    }
    path = require_inside_data_root(_log_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry
