"""Shared helpers for eval/: data folders, groups, private IDs and the test_frozen guard.

Everything is read from and written to RESUME_DATA_DIR only:
    ground_truth_v2/<stem>.json      ground truth (GT v2, docs/design.md section 5.1)
    ground_truth_v2/_split.json      {"dev": [...], "test_frozen": [...]}; tuned = every other GT file
    outputs/<stem>.json              predictions (same format as the ground truth)
    reports/stages/<resume_id>/      saved stage files (cli run --save-stages)
    reports/test_frozen_runs.log     one JSON line per test_frozen run
    private/salt.txt, id_map.json    salt for eval IDs, and the map from IDs to file names

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

from resume_parser.schema import CONFIG_DIR, data_root, read_json, require_inside_data_root, resume_id_for

GROUPS = ("dev", "tuned", "test_frozen")
SPLIT_FILE = "_split.json"
FROZEN_LOG = "test_frozen_runs.log"


class EvalError(RuntimeError):
    """A problem with the inputs. main() prints it as one line and exits with code 2."""


# ---------------------------------------------------------------- folders


def gt_dir() -> Path:
    return data_root() / "ground_truth_v2"


def outputs_dir() -> Path:
    return data_root() / "outputs"


def reports_dir() -> Path:
    return data_root() / "reports"


def stages_dir() -> Path:
    return reports_dir() / "stages"


def private_dir() -> Path:
    return data_root() / "private"


def _stem(entry: str) -> str:
    name = Path(str(entry)).name
    for suffix in (".json", ".pdf"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


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
    folder = gt_dir()
    split_path = folder / SPLIT_FILE
    if not split_path.is_file():
        raise EvalError(f"ground_truth_v2/{SPLIT_FILE} is missing.")
    split = read_json(split_path)
    all_stems = sorted(p.stem for p in folder.glob("*.json") if not p.name.startswith("_"))
    known = set(all_stems)

    groups: dict[str, list[str]] = {}
    for name in ("dev", "test_frozen"):
        stems = sorted({_stem(s) for s in split.get(name, [])})
        missing = [s for s in stems if s not in known]
        if missing:
            ids = ", ".join(resume_id_for(s) for s in missing)
            raise EvalError(f"{SPLIT_FILE}: {name} lists {len(missing)} file(s) with no ground truth ({ids}).")
        groups[name] = stems
    both = set(groups["dev"]) & set(groups["test_frozen"])
    if both:
        raise EvalError(f"{SPLIT_FILE}: {len(both)} file(s) are in both dev and test_frozen.")
    taken = set(groups["dev"]) | set(groups["test_frozen"])
    groups["tuned"] = [s for s in all_stems if s not in taken]
    return {g: groups[g] for g in GROUPS}


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
