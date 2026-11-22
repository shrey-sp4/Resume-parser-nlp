"""The "final" group: a fresh set of resumes kept apart from everything used to build the parser.

It lives outside RESUME_DATA_DIR and outside the repo, in three folders named by environment
variables:
    RESUME_FINAL_PDFS   the PDFs (only .pdf files directly inside the folder are read)
    RESUME_FINAL_GT     the ground truth, GT v2 (only .json files directly inside are read);
                        each file is named like its PDF ("x.json" for "x.pdf")
    RESUME_FINAL_OUT    everything the tools write for it:
                          outputs/          main JSON, sidecar and run_index.json
                          stages/           saved stage files (the funnel reads them)
                          run_summary.json  aggregate counts of the run
                          final_runs.log    one JSON line per run of every tool

Resume IDs are the usual ones (resume_id_for); the private salt and ID map stay in
RESUME_DATA_DIR/private.

Guard (the strictest one): results are aggregates only, every run of cli run, evaluate.py and
funnel.py is logged before it starts, and a second run of a tool is refused unless
--allow-rerun "<reason>" is given (the reason is logged). The final group is never part of the
unknown-headings list, threshold calibration, check-determinism, end_to_end, the review queue or
any per-resume output or ranking, and nothing is ever chosen or tuned on it.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from pathlib import Path

from resume_parser.schema import CONFIG_DIR, REPO_ROOT, DataDirError, data_root

FINAL_GROUP = "final"
ENV_PDFS, ENV_GT, ENV_OUT = "RESUME_FINAL_PDFS", "RESUME_FINAL_GT", "RESUME_FINAL_OUT"
LOG_FILE = "final_runs.log"
SUMMARY_FILE = "run_summary.json"


class FinalError(RuntimeError):
    """A problem with the final folders or its guard. Messages never show a path or file name."""


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _folder(env: str, create: bool = False) -> Path:
    value = os.environ.get(env, "").strip()
    if not value:
        raise FinalError(f"{env} is not set.")
    path = Path(value).resolve()
    if _inside(path, REPO_ROOT):
        raise FinalError(f"{env} must be outside the repo.")
    try:
        root = data_root()
    except DataDirError:
        root = None
    if root is not None and (_inside(path, root) or _inside(root, path)):
        raise FinalError(f"{env} must be outside RESUME_DATA_DIR.")
    if create and not path.exists() and path.parent.is_dir():
        path.mkdir()
    if not path.is_dir():
        raise FinalError(f"{env} does not point to an existing folder.")
    return path


def final_pdfs_dir() -> Path:
    return _folder(ENV_PDFS)


def final_gt_dir() -> Path:
    return _folder(ENV_GT)


def final_out() -> Path:
    return _folder(ENV_OUT, create=True)


def final_pdfs() -> list[Path]:
    """The .pdf files directly inside RESUME_FINAL_PDFS (no subfolders), sorted by name."""
    folder = final_pdfs_dir()
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"), key=lambda p: p.name)


def final_gt_files() -> list[Path]:
    """The .json files directly inside RESUME_FINAL_GT (no subfolders), sorted by name."""
    folder = final_gt_dir()
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".json"), key=lambda p: p.name)


def final_stems() -> list[str]:
    return [p.stem for p in final_gt_files()]


def require_inside_final_out(path: Path) -> Path:
    """Refuse to write anywhere except under RESUME_FINAL_OUT."""
    root, resolved = final_out(), Path(path).resolve()
    if not _inside(resolved, root):
        raise FinalError("refusing to write outside RESUME_FINAL_OUT.")
    return resolved


def require_inside_output_root(path: Path) -> Path:
    """For the output writers: a path under RESUME_DATA_DIR, or under RESUME_FINAL_OUT when it
    is set. Anything else (the repo included) is refused."""
    resolved = Path(path).resolve()
    try:
        root = data_root()
        if _inside(resolved, root):
            return resolved
    except DataDirError:
        if not os.environ.get(ENV_OUT, "").strip():
            raise
    if os.environ.get(ENV_OUT, "").strip():
        try:
            return require_inside_final_out(resolved)
        except FinalError as exc:
            raise DataDirError(str(exc)) from None
    raise DataDirError("refusing to write outside RESUME_DATA_DIR.")


# ---------------------------------------------------------------- the guard


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_sha256() -> str:
    """One hash over every .py file of the package (path and content), so a run on changed
    code shows in the log without git."""
    h = hashlib.sha256()
    package = Path(__file__).resolve().parent
    for path in sorted(package.rglob("*.py")):
        h.update(path.relative_to(package).as_posix().encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


def _log_path() -> Path:
    return final_out() / LOG_FILE


def final_runs(tool: str) -> list[dict]:
    path = _log_path()
    if not path.is_file():
        return []
    return [e for e in (json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip())
            if e.get("tool") == tool]


def require_final_allowed(tool: str, rerun_reason: str | None) -> None:
    """Refuse a second run of a tool on final unless a written reason is given."""
    earlier = final_runs(tool)
    if earlier and not (rerun_reason or "").strip():
        raise FinalError(f"refusing to run {tool} on final again: it has run {len(earlier)} time(s); "
                         "pass --allow-rerun \"<reason>\" to run it anyway (the reason is logged).")


def log_final_run(tool: str, script_path: Path, settings: dict, rerun_reason: str | None = None) -> dict:
    """Check the guard, then append this run's line (before any work, so a crashed run counts)."""
    require_final_allowed(tool, rerun_reason)
    entry = {
        "tool": tool,
        "run": len(final_runs(tool)) + 1,
        "utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "script_sha256": _sha256_file(script_path),
        "code_sha256": code_sha256(),
        "settings_sha256": hashlib.sha256(json.dumps(settings, sort_keys=True, default=list).encode("utf-8")).hexdigest(),
        "pipeline_thresholds_sha256": _sha256_file(CONFIG_DIR / "thresholds.json"),
        "rerun_reason": (rerun_reason or "").strip(),
    }
    path = require_inside_final_out(_log_path())
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry
