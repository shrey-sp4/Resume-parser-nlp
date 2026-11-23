"""The fresh set: a second once-only held-out set of resumes, scored once on frozen code.

It lives outside the repo and outside RESUME_DATA_DIR, in four folders named by environment
variables:
    RESUME_FRESH_PDFS    the PDFs (only .pdf files directly inside the folder are read)
    RESUME_FRESH_GT      the ground truth, GT v2 (only .json files directly inside are read);
                         each GT file is paired with the PDF its resume_file names; this folder
                         may sit inside RESUME_FRESH_PDFS
    RESUME_FRESH_BACKUP  a backup of the ground truth: must exist, is never read
    RESUME_FRESH_OUT     everything the run writes, and nothing else:
                           fresh_runs.log     one JSON line for the run
                           fresh_report.txt   the aggregate report, as printed
                           fresh_report.json  the same numbers as JSON
The run log is mirrored to RESUME_DATA_DIR/reports/fresh_runs.log; an entry in either log
refuses the run. RESUME_DATA_DIR also keeps the usual private salt, as for every group.

    python eval/fresh.py check
        Layout only, the pipeline is not run: folder rules, PDF and GT counts, pairing by
        resume_file, resume_file values ending in .pdf (pattern only), the GT v2 schema, no
        fresh file name or resume ID in the repo's files, no PDF identical to one in another
        set, and the run logs. Prints PASS or FAIL and counts only.
    python eval/fresh.py run --commit <hash> [--parser-commit <hash>]
        The one run, ever. Refused unless HEAD is the commit named (7 to 40 hex characters),
        the git tree is clean (untracked files included), the layout check passes and no run
        is logged; with --parser-commit, also refused when src/, config/ or schema/ differ
        from that commit. The log line is written before any work, so a crashed run counts.
        There is no way to run it a second time.
    python eval/fresh.py rehearse [--seeds 1000-1009] [--keep]
        Dress rehearsal: the full check and run on synthetic CVs from the synthetic sweep
        generator (scripts/synthetic_sweep_gen.py) with their generator GT, in temporary folders
        for all four fresh folders AND for RESUME_DATA_DIR, so no real folder or log is touched.
        The git checks are shown but not enforced. The temporary folder is removed afterwards
        unless --keep is given.

Aggregates only. The run keeps everything in memory: no main JSON, sidecar, stage file, index
or per-item file is written anywhere, and nothing names a resume (no file name, stem, resume
ID or path) on the console or in the reports. Output of the parser itself is swallowed during
the run. An exception is reported as its class name and a count only, never its message or a
traceback. Reported: entry P / R / F1 with 95% CIs, field accuracies, name exact and normalised
(eval/evaluate.py), the funnel tables (eval/funnel.py), decision counts, and how many wrong
items fell in resumes the parser flagged (NEEDS_REVIEW or FAIL), with the aggregate
check-validity table.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "eval")]

import check_no_data  # noqa: E402
import common  # noqa: E402
import evaluate as ev  # noqa: E402
import funnel  # noqa: E402

from resume_parser import final as final_mod  # noqa: E402
from resume_parser.schema import CONFIG_DIR, REPO_ROOT, DataDirError, data_root, schema_errors  # noqa: E402

GROUP = "fresh"
TOOL = "fresh_run"
ENV_PDFS, ENV_GT, ENV_BACKUP, ENV_OUT = "RESUME_FRESH_PDFS", "RESUME_FRESH_GT", "RESUME_FRESH_BACKUP", "RESUME_FRESH_OUT"
ENVS = (ENV_PDFS, ENV_GT, ENV_BACKUP, ENV_OUT)
LOG_FILE = "fresh_runs.log"
REPORT_TXT, REPORT_JSON = "fresh_report.txt", "fresh_report.json"
OUT_FILES = (LOG_FILE, REPORT_TXT, REPORT_JSON)
PARSER_PATHS = ("src", "config", "schema")
FLAGGED = ("NEEDS_REVIEW", "FAIL")
DATA_SUBDIRS = ("pdfs", "ground_truth_v2", "outputs", "review_queue", "reports")


class FreshError(RuntimeError):
    """A refusal or a problem with the fresh folders. Messages never show a path, file name,
    stem, resume ID or any value read from a file."""


# ---------------------------------------------------------------- folders


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _overlap(a: Path, b: Path) -> bool:
    return _inside(a, b) or _inside(b, a)


def folders() -> tuple[dict[str, Path | None], list[str]]:
    """The four fresh folders (resolved, None when unset) and the folder rules they break, as
    messages that name only environment variables."""
    paths: dict[str, Path | None] = {}
    problems: list[str] = []
    for env in ENVS:
        value = os.environ.get(env, "").strip()
        paths[env] = Path(value).resolve() if value else None
        if not value:
            problems.append(f"{env} is not set")
    for env in (ENV_PDFS, ENV_GT, ENV_BACKUP):
        if paths[env] is not None and not paths[env].is_dir():
            problems.append(f"{env} is not an existing folder")
    out = paths[ENV_OUT]
    if out is not None and not out.is_dir() and not out.parent.is_dir():
        problems.append(f"{ENV_OUT} is not a folder and its parent folder does not exist")
    try:
        root = data_root()
    except DataDirError:
        root = None
        problems.append("RESUME_DATA_DIR is not set or not a folder (the run log is mirrored there)")
    finals = [Path(v).resolve() for v in (os.environ.get(e, "").strip()
                                          for e in (final_mod.ENV_PDFS, final_mod.ENV_GT, final_mod.ENV_OUT)) if v]
    for env, path in paths.items():
        if path is None:
            continue
        if _overlap(path, REPO_ROOT):
            problems.append(f"{env} is inside the repo or contains it")
        if root is not None and _overlap(path, root):
            problems.append(f"{env} is inside RESUME_DATA_DIR or contains it")
        if any(_overlap(path, f) for f in finals):
            problems.append(f"{env} overlaps a folder of the final set")
    # the backup and the output folder stand apart from every other fresh folder (the GT folder
    # may sit inside the PDF folder)
    pairs = set()
    for env in (ENV_BACKUP, ENV_OUT):
        for other in ENVS:
            if other != env and paths[env] is not None and paths[other] is not None \
                    and _overlap(paths[env], paths[other]):
                pairs.add(tuple(sorted((env, other))))
    problems += [f"{a} and {b} overlap" for a, b in sorted(pairs)]
    return paths, problems


def _files(folder: Path, suffix: str) -> list[Path]:
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == suffix), key=lambda p: p.name)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _log_paths(paths: dict[str, Path | None]) -> list[Path]:
    return [paths[ENV_OUT] / LOG_FILE, data_root() / "reports" / LOG_FILE]


def _log_entries(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for x in path.read_text(encoding="utf-8").splitlines() if x.strip())


def _repo_files() -> list[str] | None:
    done = _git("ls-files", "-z")
    if done.returncode:
        return None
    return [p for p in done.stdout.split("\0") if p]


def layout(paths: dict[str, Path | None]) -> dict:
    """The layout check on folders that keep the folder rules. Returns counts (and, under the
    private key "_pairs", the (GT record, PDF path) pairs the run uses; never printed)."""
    pdfs = _files(paths[ENV_PDFS], ".pdf")
    gts = _files(paths[ENV_GT], ".json")
    by_name = {p.name: p for p in pdfs}
    used: Counter = Counter()
    pairs, gt_no_pdf, bad_suffix, invalid = [], 0, 0, 0
    for path in gts:
        try:
            gt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            invalid += 1
            gt_no_pdf += 1
            continue
        if not isinstance(gt, dict) or schema_errors(gt, "resume.schema.json"):
            invalid += 1
        value = gt.get("resume_file") if isinstance(gt, dict) else None
        value = value if isinstance(value, str) else ""
        if not value.lower().endswith(".pdf"):
            bad_suffix += 1
        if value in by_name:
            used[value] += 1
            pairs.append((gt, by_name[value]))
        else:
            gt_no_pdf += 1

    markers: set[str] = set()
    for path in (*pdfs, *gts):
        check_no_data._add_marker(markers, path.name)
    repo = _repo_files()
    in_repo = None if repo is None else sum(
        "guarded-group" in m for m in check_no_data.find_problems(repo, REPO_ROOT, markers))

    others: set[str] = set()
    try:
        others |= {_sha256(p) for p in (data_root() / "pdfs").rglob("*.pdf") if p.is_file()}
    except DataDirError:
        pass
    final_pdfs = os.environ.get(final_mod.ENV_PDFS, "").strip()
    if final_pdfs and Path(final_pdfs).is_dir():
        others |= {_sha256(p) for p in _files(Path(final_pdfs), ".pdf")}
    duplicates = sum(_sha256(p) in others for p in pdfs)
    earlier = sum(_log_entries(p) for p in _log_paths(paths))

    res = {
        "pdfs": len(pdfs), "gt_files": len(gts),
        "pdfs_without_gt": sum(1 for n in by_name if used[n] == 0),
        "pdfs_with_several_gt": sum(1 for n in by_name if used[n] > 1),
        "gt_without_pdf": gt_no_pdf,
        "resume_file_not_pdf": bad_suffix,
        "gt_schema_failures": invalid,
        "repo_files_naming_fresh": in_repo,
        "pdfs_identical_to_another_set": duplicates,
        "runs_logged": earlier,
    }
    res["ok"] = bool(
        res["pdfs"] and res["pdfs"] == res["gt_files"] and in_repo == 0
        and not any(res[k] for k in ("pdfs_without_gt", "pdfs_with_several_gt", "gt_without_pdf",
                                     "resume_file_not_pdf", "gt_schema_failures", "pdfs_identical_to_another_set")))
    res["_pairs"] = pairs
    return res


def print_check(problems: list[str], res: dict | None) -> None:
    ok = not problems and res is not None and res["ok"]
    print(f"fresh check: {'PASS' if ok else 'FAIL'}")
    print(f"  folder rules broken: {len(problems)}")
    for message in problems:
        print(f"    {message}")
    if res is None:
        print("  (files not checked until the folder rules hold)")
        return
    rows = (("PDFs", "pdfs"), ("GT files", "gt_files"),
            ("PDFs with no GT file", "pdfs_without_gt"),
            ("PDFs named by more than one GT file", "pdfs_with_several_gt"),
            ("GT files with no PDF", "gt_without_pdf"),
            ("resume_file values not ending in .pdf", "resume_file_not_pdf"),
            ("GT files failing the GT v2 schema (or not JSON)", "gt_schema_failures"),
            ("repo files naming a fresh file or resume ID", "repo_files_naming_fresh"),
            ("PDFs identical to a PDF of another set", "pdfs_identical_to_another_set"),
            ("runs logged so far", "runs_logged"))
    for label, key in rows:
        value = res[key]
        print(f"  {label + ':':<50}{'could not check' if value is None else value}")
    if res["pdfs"] != res["gt_files"]:
        print("  the PDF and GT counts differ")


def cmd_check(_args=None) -> int:
    paths, problems = folders()
    res = layout(paths) if not problems else None
    print_check(problems, res)
    return 0 if not problems and res["ok"] else 1


# ---------------------------------------------------------------- git


def _git(*args: str) -> subprocess.CompletedProcess:
    """Read-only git commands on this repo (no optional index lock)."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def git_state(parser_commit: str | None = None) -> dict:
    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--untracked-files=all")
    if head.returncode or status.returncode:
        raise FreshError("git could not read HEAD or the tree status.")
    state = {"head": head.stdout.strip(), "dirty": sum(1 for x in status.stdout.splitlines() if x.strip()),
             "parser_commit": None, "parser_unchanged": None}
    if parser_commit:
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", parser_commit.strip()):
            raise FreshError("--parser-commit must be 7 to 40 hex characters.")
        diff = _git("diff", "--quiet", parser_commit.strip(), "HEAD", "--", *PARSER_PATHS)
        if diff.returncode not in (0, 1):
            raise FreshError("git could not compare HEAD with --parser-commit.")
        state["parser_commit"] = parser_commit.strip().lower()
        state["parser_unchanged"] = diff.returncode == 0
    return state


def require_frozen(state: dict, commit: str | None) -> None:
    c = (commit or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", c):
        raise FreshError("--commit must be 7 to 40 hex characters.")
    if not state["head"].lower().startswith(c):
        raise FreshError("HEAD is not the commit named by --commit; nothing was run.")
    if state["dirty"]:
        raise FreshError(f"the git tree is not clean ({state['dirty']} changed or untracked path(s)); "
                         "commit first; nothing was run.")
    if state["parser_unchanged"] is False:
        raise FreshError("src/, config/ or schema/ differ from --parser-commit; nothing was run.")


# ---------------------------------------------------------------- the run


@contextlib.contextmanager
def _silenced():
    """Swallow everything the parser prints, warns or logs while it runs."""
    sink = io.StringIO()
    before = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with warnings.catch_warnings(), contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            warnings.simplefilter("ignore")
            yield
    finally:
        logging.disable(before)


def _roundtrip(obj):
    from resume_parser.writer import dump_json

    return json.loads(dump_json(obj, sort_keys=True))


def run_pipeline(pdf: Path):
    from resume_parser.pipeline import run_pipeline as run

    return run(pdf)


def from_record(record) -> tuple[dict | None, dict, set, str]:
    """What the final run would write and read back, built in memory: the prediction (schema
    checked, as write_resume does), the funnel's stage view, the checks that fired and the
    decision."""
    from resume_parser.writer import _checked, build_stage_file

    pred = _roundtrip(_checked(record.main, "resume.schema.json")) if record.main is not None else None
    sidecar = _roundtrip(_checked(record.sidecar, "meta.schema.json"))
    rec = {s: _roundtrip(build_stage_file(record, s)) if s in record.results else None for s in funnel.STAGE_FILES}
    stage1 = record.results.get("stage1")
    methods = {"methods": _roundtrip(stage1.extra["outputs"])} if stage1 is not None and stage1.extra.get("outputs") else {}
    st = funnel.stages_from(rec, methods, sidecar.get("provenance") or {})
    return pred, st, funnel.checks_from(sidecar), sidecar["decision"]


def _clean(obj):
    """JSON-safe copy: NaN becomes null, tuples become lists."""
    if isinstance(obj, float):
        return None if math.isnan(obj) else obj
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def score(pairs: list) -> dict:
    """Run the parser on every pair in memory and return the aggregates only."""
    empty = funnel.stages_from({s: None for s in funnel.STAGE_FILES}, {}, {})
    rows, items, fps, checks, methods5 = [], [], [], [], []
    incomplete = 0
    decisions: Counter = Counter()
    errors: Counter = Counter()
    lost = {"gt_items_lost": 0, "gt_items_lost_in_flagged": 0, "false_positives": 0,
            "false_positives_in_flagged": 0, "resumes_with_a_loss": 0, "resumes_with_a_loss_flagged": 0}
    for k, (gt, pdf) in enumerate(pairs, 1):
        pred, st, fired, decision = None, empty, None, None
        try:
            with _silenced():
                pred, st, fired, decision = from_record(run_pipeline(pdf))
        except Exception as exc:  # counted by class name only, never shown
            errors[type(exc).__name__] += 1
            pred, st, fired, decision = None, empty, None, None
        rows.append(ev.score_resume(gt, pred))
        incomplete += not st["complete"]
        it, fp = funnel.diagnose_resume(gt, pred, st, f"x_{k:04d}")
        items += it
        fps += fp
        per_unit = (st["records"]["stage5"] or {}).get("per_unit") or {}
        methods5 += [(sec, (per_unit.get(sec) or {}).get("method_used") or "none")
                     for sec in ("personal", "education", "experience", "publications")]
        losses = [i for i in it if i["lost_at"] != "ok"]
        if fired is not None:
            checks.append((fired, {(i["lost_at"], i["section"]) for i in losses}))
        decisions[decision or "no decision (error)"] += 1
        flagged = decision in FLAGGED
        lost["gt_items_lost"] += len(losses)
        lost["gt_items_lost_in_flagged"] += len(losses) if flagged else 0
        lost["false_positives"] += len(fp)
        lost["false_positives_in_flagged"] += len(fp) if flagged else 0
        lost["resumes_with_a_loss"] += bool(losses or fp)
        lost["resumes_with_a_loss_flagged"] += bool(losses or fp) and flagged
    return {"eval": ev.aggregate(rows),
            "funnel": {GROUP: {"items": items, "fps": fps, "n": len(pairs), "checks": checks,
                               "incomplete": incomplete, "stage5_methods": methods5}},
            "decisions": decisions, "errors": errors, "caught": lost}


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f}%" if b else "n/a"


def report(scored: dict, state: dict, n: int) -> tuple[str, dict]:
    """The aggregate report as text and as JSON. Nothing in it names a resume."""
    d, e, c = scored["decisions"], scored["errors"], scored["caught"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print("=== FRESH SET: the one run (aggregates only) ===")
        print(f"  commit: {state['head']}")
        if state["parser_commit"]:
            print(f"  parser commit: {state['parser_commit']} (src/, config/, schema/ unchanged: "
                  f"{'yes' if state['parser_unchanged'] else 'NO'})")
        print(f"  resumes: {n}")
        print(f"  errors: {sum(e.values())}" + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(e.items())) + ")" if e else ""))
        print("  decisions: " + ", ".join(f"{k} {d.get(k, 0)}" for k in ("ACCEPT", "NEEDS_REVIEW", "FAIL"))
              + (f", no decision (error) {d['no decision (error)']}" if d.get("no decision (error)") else ""))
        print("  wrong items in resumes the parser flagged (decision NEEDS_REVIEW or FAIL):")
        print(f"    GT items lost (any stage):  {c['gt_items_lost_in_flagged']} of {c['gt_items_lost']} "
              f"({_pct(c['gt_items_lost_in_flagged'], c['gt_items_lost'])})")
        print(f"    false-positive entries:     {c['false_positives_in_flagged']} of {c['false_positives']} "
              f"({_pct(c['false_positives_in_flagged'], c['false_positives'])})")
        print(f"    resumes with any loss or false positive: {c['resumes_with_a_loss']}, "
              f"of which flagged: {c['resumes_with_a_loss_flagged']}")
        ev.print_group(GROUP, scored["eval"], None)
        print()
        funnel.print_report(scored["funnel"], validity_groups=[GROUP])
    fun = scored["funnel"][GROUP]
    validity = funnel.check_validity(fun["checks"])
    data = {
        "group": GROUP, "commit": state["head"], "parser_commit": state["parser_commit"],
        "parser_unchanged": state["parser_unchanged"], "resumes": n,
        "errors": dict(sorted(e.items())), "decisions": dict(sorted(d.items())), "flag_catch": c,
        "evaluate": scored["eval"],
        "funnel": {sec: dict(zip(("rows", "extra"), funnel.funnel(fun["items"], sec))) for sec in funnel.SECTIONS},
        "funnel_stages": funnel.STAGES,
        "s1_causes": {cause: sum(1 for i in fun["items"] if i["lost_at"] == "S1 text" and i["cause"] == cause)
                      for cause in funnel.S1_CAUSES},
        "check_validity": {f"{s}|{k}": v for (s, k), v in validity.items()},
    }
    return buf.getvalue(), _clean(data)


def _write_inside(folder: Path, name: str, text: str, mode: str = "w") -> None:
    path = (folder / name).resolve()
    if path.parent != folder.resolve() or name not in OUT_FILES + (LOG_FILE,):
        raise FreshError("refusing to write anything but the fresh log and reports.")
    with path.open(mode, encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def run_fresh(commit: str | None, parser_commit: str | None, enforce_git: bool = True) -> int:
    state = git_state(parser_commit)
    print(f"commit: {state['head']}")
    if enforce_git:
        require_frozen(state, commit)
    else:
        print(f"rehearsal: git checks shown, not enforced (tree clean: {'yes' if not state['dirty'] else 'no'}"
              + (f"; src/, config/, schema/ unchanged since --parser-commit: "
                 f"{'yes' if state['parser_unchanged'] else 'no'}" if parser_commit else "") + ")")
    paths, problems = folders()
    res = layout(paths) if not problems else None
    if problems or not res["ok"]:
        print_check(problems, res)
        raise FreshError("the layout check fails; nothing was run.")
    if res["runs_logged"]:
        raise FreshError("refusing to run: the fresh set has already been run (it runs once, ever).")

    out = paths[ENV_OUT]
    out.mkdir(exist_ok=True)
    mirror = data_root() / "reports"
    mirror.mkdir(exist_ok=True)
    pairs = res.pop("_pairs")
    entry = {
        "tool": TOOL, "run": 1, "group": GROUP,
        "utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "commit": state["head"], "tree_clean": not state["dirty"], "git_enforced": enforce_git,
        "parser_commit": state["parser_commit"], "parser_unchanged": state["parser_unchanged"],
        "resumes": len(pairs),
        "code_sha256": final_mod.code_sha256(),
        "script_sha256": _sha256(Path(__file__)),
        "evaluate_sha256": _sha256(Path(ev.__file__)),
        "funnel_sha256": _sha256(Path(funnel.__file__)),
        "common_sha256": _sha256(Path(common.__file__)),
        "pipeline_thresholds_sha256": _sha256(CONFIG_DIR / "thresholds.json"),
    }
    line = json.dumps(entry, sort_keys=True) + "\n"
    _write_inside(out, LOG_FILE, line, "a")  # before any work, so a crashed run still counts
    _write_inside(mirror, LOG_FILE, line, "a")

    text, data = report(score(pairs), state, len(pairs))
    _write_inside(out, REPORT_TXT, text)
    _write_inside(out, REPORT_JSON, json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(text, end="")
    print(f"\nfresh: report written to RESUME_FRESH_OUT ({REPORT_TXT}, {REPORT_JSON}); the run is logged")
    return 0


def cmd_run(args) -> int:
    return run_fresh(args.commit, args.parser_commit)


# ---------------------------------------------------------------- dress rehearsal


def _seeds(text: str) -> list[int]:
    m = re.fullmatch(r"(\d+)-(\d+)", text.strip())
    if not m or int(m.group(2)) < int(m.group(1)):
        raise FreshError("--seeds takes a range like 1000-1009.")
    return list(range(int(m.group(1)), int(m.group(2)) + 1))


def _tree(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")) if folder.is_dir() else []


def cmd_rehearse(args) -> int:
    import synthetic_sweep_gen as gen

    seeds = _seeds(args.seeds)
    base = Path(tempfile.mkdtemp(prefix="fresh_rehearsal_")).resolve()
    if _overlap(base, REPO_ROOT):
        raise FreshError("the temporary folder is inside the repo.")
    data, pdfs, out, backup = base / "data", base / "pdfs", base / "out", base / "gt_backup"
    gt = pdfs / "gt"  # the GT folder inside the PDF folder, as in the real layout
    for sub in DATA_SUBDIRS:
        (data / sub).mkdir(parents=True)
    gt.mkdir(parents=True)
    backup.mkdir()
    for seed in seeds:
        pdf, record = gen.build(gen.combo(seed), pdfs)
        (gt / f"{pdf.stem}.json").write_text(json.dumps(record), encoding="utf-8")
    for path in gt.iterdir():
        shutil.copy2(path, backup / path.name)

    keys = (*ENVS, "RESUME_DATA_DIR", final_mod.ENV_PDFS, final_mod.ENV_GT, final_mod.ENV_OUT)
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in (final_mod.ENV_PDFS, final_mod.ENV_GT, final_mod.ENV_OUT):
            os.environ.pop(k, None)
        os.environ.update({ENV_PDFS: str(pdfs), ENV_GT: str(gt), ENV_BACKUP: str(backup), ENV_OUT: str(out),
                           "RESUME_DATA_DIR": str(data)})
        print(f"rehearsal: {len(seeds)} synthetic CV(s) in a temporary folder; every fresh folder and "
              "RESUME_DATA_DIR point there\n")
        check_rc = cmd_check()
        print()
        run_rc = run_fresh(None, args.parser_commit, enforce_git=False)
        try:
            run_fresh(None, args.parser_commit, enforce_git=False)
            again = "NOT refused"
        except FreshError:
            again = "refused"
        print("\nrehearsal summary:")
        print(f"  layout check exit code: {check_rc}; run exit code: {run_rc}; a second run: {again}")
        print(f"  output folder holds: {', '.join(_tree(out))}")
        print(f"  temporary data folder files: {', '.join(p for p in _tree(data) if (data / p).is_file())}")
        return 0 if check_rc == 0 and run_rc == 0 and again == "refused" and _tree(out) == sorted(OUT_FILES) else 1
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if args.keep:
            print("  (temporary folder kept)")
        else:
            shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------- command line


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python eval/fresh.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="layout only: counts, pairing, schema, folder rules").set_defaults(func=cmd_check)
    run = sub.add_parser("run", help="the one run, on the commit named")
    run.add_argument("--commit", required=True, help="the commit HEAD must be (7 to 40 hex characters)")
    run.add_argument("--parser-commit", help="also refuse when src/, config/ or schema/ differ from this commit")
    run.set_defaults(func=cmd_run)
    reh = sub.add_parser("rehearse", help="the full check and run on synthetic CVs in temporary folders")
    reh.add_argument("--seeds", default="1000-1009", help="synthetic sweep seeds, as a range (default 1000-1009)")
    reh.add_argument("--parser-commit", help="also show whether src/, config/, schema/ differ from this commit")
    reh.add_argument("--keep", action="store_true", help="keep the temporary folder")
    reh.set_defaults(func=cmd_rehearse)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FreshError, DataDirError) as exc:
        print(f"fresh: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # never a message or traceback: they could name a file
        print(f"fresh: stopped by {type(exc).__name__} (no further detail is shown)", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
