"""Rotating hold-out over the development resumes: how much do the scores vary between folds?

    python eval/folds.py [--rebuild]

The development resumes (dev, tuned, dev2) are cut into 3 fixed folds: sorted by resume ID,
shuffled with random.Random(0), then split into equal parts. The folds are stored, as resume IDs
only, in RESUME_DATA_DIR/reports/folds/folds.json and reused; --rebuild makes them again (needed
when the development groups change). Nothing about the folds is written in the repo.

For each fold: P/R/F1 with 95% CIs per section (evaluate.aggregate, same bootstrap), name
accuracy (exact and normalised), the spread across folds, the resumes whose removal raises the
fold's F1 most (leave one out inside the fold), and the experience and education funnel (from the
item files of the last eval/funnel.py run).

The rules were tuned on all of these resumes, so these are NOT held-out scores. They show which
sections vary most between folds and which resumes pull a fold down.

Development groups only: validation, test_frozen and final are never loaded. Output is aggregate
numbers and resume IDs only.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import evaluate as ev  # noqa: E402
from common import EvalError  # noqa: E402

from resume_parser.schema import DataDirError, read_json, resume_id_for  # noqa: E402

N_FOLDS = 3
SEED = 0
SECTIONS = ("publications", "education", "experience")
FUNNEL_SECTIONS = ("experience", "education")
STAGES = ("S1 text", "S4 section", "S5 entry", "S5 fields")
TOP = 3


def folds_path() -> Path:
    return common.reports_dir() / "folds" / "folds.json"


def development_stems() -> list[str]:
    groups = common.load_groups()
    return sorted({s for g in common.DEV_GROUPS for s in groups[g]})


def make_folds(stems: list[str], n_folds: int = N_FOLDS, seed: int = SEED) -> list[list[str]]:
    """Resume IDs per fold: sorted by ID, shuffled with random.Random(seed), cut into equal parts."""
    ids = sorted(resume_id_for(s) for s in stems)
    random.Random(seed).shuffle(ids)
    n = len(ids)
    return [sorted(ids[k * n // n_folds:(k + 1) * n // n_folds]) for k in range(n_folds)]


def load_or_make_folds(stems: list[str], rebuild: bool = False) -> list[list[str]]:
    path = folds_path()
    wanted = {resume_id_for(s) for s in stems}
    if path.is_file() and not rebuild:
        saved = read_json(path)
        folds = saved["folds"]
        if {r for f in folds for r in f} != wanted or saved.get("seed") != SEED:
            raise EvalError("the saved folds do not match the development resumes; run with --rebuild.")
        return folds
    folds = make_folds(stems)
    common.write_text_inside(path, json.dumps({"seed": SEED, "n_folds": N_FOLDS, "folds": folds}, indent=1) + "\n")
    return folds


def point_metrics(rows: list[dict]) -> dict:
    """Point metrics only (no bootstrap), for the leave-one-out search."""
    C = np.array([ev._vector(r) for r in rows], dtype=float).reshape(len(rows), len(ev._COLUMNS))
    return {k: float(v) for k, v in ev._metrics(C.sum(axis=0)).items()}


def draggers(rows_by_rid: dict[str, dict], section: str, top: int = TOP) -> list[tuple[str, float]]:
    """(resume ID, F1 gain in points when it is left out), largest gain first."""
    rids = sorted(rows_by_rid)
    base = point_metrics([rows_by_rid[r] for r in rids])[f"{section}_F1"]
    gains = []
    for r in rids:
        rest = [rows_by_rid[x] for x in rids if x != r]
        if rest:
            gains.append((r, 100 * (point_metrics(rest)[f"{section}_F1"] - base)))
    gains.sort(key=lambda x: (-x[1], x[0]))
    return [g for g in gains[:top] if g[1] > 0]


def funnel_counts(fold_eids: set[str]) -> dict:
    """{section: {"GT items", "S1 found", "S4 routed", "S5 matched", "fields correct", lost per stage}}."""
    out = {sec: {"GT items": 0, **{f"lost at {s}": 0 for s in STAGES}} for sec in FUNNEL_SECTIONS}
    for g in common.DEV_GROUPS:
        path = common.reports_dir() / "funnel" / f"items_{g}.jsonl"
        if not path.is_file():
            raise EvalError("no funnel item files; run 'python eval/funnel.py' first.")
        for line in path.read_text(encoding="utf-8").splitlines():
            it = json.loads(line)
            if it["section"] not in out or it["eval_id"] not in fold_eids:
                continue
            c = out[it["section"]]
            c["GT items"] += 1
            if it["lost_at"] in STAGES:
                c[f"lost at {it['lost_at']}"] += 1
    for c in out.values():
        c["S1 found"] = c["GT items"] - c["lost at S1 text"]
        c["S4 routed"] = c["S1 found"] - c["lost at S4 section"]
        c["S5 matched"] = c["S4 routed"] - c["lost at S5 entry"]
        c["fields correct"] = c["S5 matched"] - c["lost at S5 fields"]
    return out


def run(rebuild: bool = False) -> dict:
    stems = development_stems()
    folds = load_or_make_folds(stems, rebuild)
    stem_of = {resume_id_for(s): s for s in stems}
    ids = common.ids(stems)
    rows = {resume_id_for(s): ev.score_resume(ev.load_gt(s), ev.load_pred(s)) for s in stems}
    result = {"folds": []}
    for k, fold in enumerate(folds, 1):
        res = ev.aggregate([rows[r] for r in fold])
        result["folds"].append({
            "fold": k, "n": len(fold), "point": res["point"], "ci": res["ci"],
            "draggers": {sec: draggers({r: rows[r] for r in fold}, sec) for sec in SECTIONS},
            "funnel": funnel_counts({ids[stem_of[r]]["eval_id"] for r in fold}),
        })
    spread = {}
    for sec in SECTIONS:
        for m in ("P", "R", "F1"):
            vals = [100 * f["point"][f"{sec}_{m}"] for f in result["folds"]]
            spread[f"{sec}_{m}"] = (min(vals), max(vals), max(vals) - min(vals))
    for key in ("name_acc", "name_norm_acc"):
        vals = [100 * f["point"][key] for f in result["folds"]]
        spread[key] = (min(vals), max(vals), max(vals) - min(vals))
    result["spread"] = spread
    result["largest_spread"] = max(SECTIONS, key=lambda s: spread[f"{s}_F1"][2])
    return result


def print_report(result: dict) -> None:
    print("=== ROTATING HOLD-OUT: 3 fixed folds of the development resumes (seed 0) ===")
    print("NOTE: the rules were tuned on all of these resumes; these are NOT held-out scores.")
    print("      They show which sections vary most between folds and which resumes pull a fold down.\n")
    for f in result["folds"]:
        p, ci = f["point"], f["ci"]
        print(f"fold {f['fold']} (n={f['n']})")
        for sec in SECTIONS:
            cells = "  ".join(f"{m} {100 * p[f'{sec}_{m}']:5.1f} [{100 * ci[f'{sec}_{m}'][0]:5.1f},{100 * ci[f'{sec}_{m}'][1]:5.1f}]"
                              for m in ("P", "R", "F1"))
            print(f"  {sec:<13}{cells}")
        print(f"  {'name':<13}exact {100 * p['name_acc']:5.1f}   normalised {100 * p['name_norm_acc']:5.1f}")
    print("\nspread across folds (min / max / range)")
    for key, (lo, hi, rng) in result["spread"].items():
        print(f"  {key:<18}{lo:6.1f} {hi:6.1f} {rng:6.1f}")
    print(f"\nlargest F1 spread: {result['largest_spread']}")
    print(f"\nresumes that pull a fold down (F1 gain in points when left out, top {TOP})")
    for f in result["folds"]:
        for sec in SECTIONS:
            items = ", ".join(f"{r} +{g:.1f}" for r, g in f["draggers"][sec]) or "-"
            print(f"  fold {f['fold']} {sec:<13}{items}")
    print("\nfunnel per fold (items)")
    cols = ("GT items", "S1 found", "S4 routed", "S5 matched", "fields correct",
            "lost at S1 text", "lost at S4 section", "lost at S5 entry", "lost at S5 fields")
    print(f"  {'fold section':<18}" + "".join(f"{c:>19}" for c in cols))
    for f in result["folds"]:
        for sec in FUNNEL_SECTIONS:
            print(f"  {str(f['fold']) + ' ' + sec:<18}" + "".join(f"{f['funnel'][sec][c]:>19}" for c in cols))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rebuild", action="store_true", help="make the folds again (development groups changed)")
    a = ap.parse_args(argv)
    try:
        print_report(run(a.rebuild))
    except (EvalError, DataDirError) as exc:
        print(f"folds: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
