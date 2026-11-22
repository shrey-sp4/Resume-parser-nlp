"""End-to-end report for the development groups dev, tuned and dev2 (never validation or
test_frozen), from the files a run leaves behind.

    python -m resume_parser.cli run --groups dev,tuned,dev2 --save-stages
    python eval/funnel.py
    python eval/end_to_end.py

Prints, per group:
  1. name, education, experience and publications P / R / F1 with 95% bootstrap CIs. The
     entry sections come from evaluate.aggregate; for the name, P = right names / names given
     and R = right names / GT names (right = name_norm equal), with the same bootstrap settings
  2. the funnel per section: GT items, found in stage 1 lines (S1), in the right section (S4),
     matched (S5), all fields correct
  3. items lost by funnel step, all sections together
  4. the check-validity table for every stage (funnel.check_validity)
  5. decisions (ACCEPT / NEEDS_REVIEW / FAIL) and, per decision, how many resumes have a real
     loss (recall or precision below 0.8 in a section, or a wrong name). The thresholds were
     chosen on these same resumes, so this table is optimistic
Counts, metrics and resume IDs only; never resume text or file names.
"""

from __future__ import annotations

import json
import sys
from collections import Counter

import numpy as np

import calibrate_thresholds as cal
import common
import evaluate as ev
import funnel
from resume_parser.schema import read_json, resume_id_for

GROUPS = common.DEV_GROUPS  # groups with no resumes are left out in main()
SECTIONS = ("name", "education", "experience", "publications")
FUNNEL_COLS = ("GT items", "S1 found", "S4 routed", "S5 matched", "fields correct")


def name_counts(gt: dict, pred: dict | None) -> tuple[int, int, int]:
    g = ev._s(gt.get("candidate_name"))
    p = ev._s((pred or {}).get("candidate_name"))
    right = bool(g) and bool(p) and ev.norm_name(p) == ev.norm_name(g)
    return int(right), int(bool(p) and not right), int(bool(g) and not right)


def name_prf(rows: list[tuple[int, int, int]]) -> tuple[dict, dict]:
    C = np.array(rows, dtype=float).reshape(len(rows), 3)

    def metrics(T):
        tp, fp, fn = T[..., 0], T[..., 1], T[..., 2]
        p, r = ev._div(tp, tp + fp), ev._div(tp, tp + fn)
        return {"P": p, "R": r, "F1": ev._div(2 * p * r, p + r)}

    point = {k: float(v) for k, v in metrics(C.sum(axis=0)).items()}
    rng = np.random.default_rng(ev.BOOTSTRAP["seed"])
    n = len(rows)
    draws = [rng.integers(0, n, n) for _ in range(ev.BOOTSTRAP["n"] if n else 0)]
    samples = metrics(np.stack([C[idx].sum(axis=0) for idx in draws])) if draws else {}
    lo, hi = 100 * ev.BOOTSTRAP["alpha"] / 2, 100 * (1 - ev.BOOTSTRAP["alpha"] / 2)
    ci = {k: (float(np.percentile(samples[k], lo)), float(np.percentile(samples[k], hi))) if draws
          else (float("nan"),) * 2 for k in point}
    return point, ci


def _pct(v: float) -> str:
    return "  n/a" if v != v else f"{100 * v:5.1f}"


def print_prf(groups: dict) -> None:
    print("=== 1. ENTRY MATCHING, P / R / F1 [95% CI] ===")
    print(f"  {'section':<14}" + " | ".join(f"{g:<64}" for g in GROUPS))
    for sec in SECTIONS:
        cells = []
        for g in GROUPS:
            point, ci = groups[g]["prf"][sec]
            cells.append("  ".join(f"{m} {_pct(point[m])} [{_pct(ci[m][0])},{_pct(ci[m][1])}]" for m in ("P", "R", "F1")))
        print(f"  {sec:<14}" + " | ".join(f"{c:<64}" for c in cells))


def print_funnel(groups: dict) -> None:
    print("\n=== 2. FUNNEL PER SECTION (items) ===")
    print(f"  {'section':<14}{'group':<8}" + "".join(f"{c:>16}" for c in FUNNEL_COLS))
    for sec in SECTIONS:
        for g in GROUPS:
            xs = [i for i in groups[g]["items"] if i["section"] == sec]
            row = (len(xs), sum(i["s1_ge85"] for i in xs),
                   sum(i["lost_at"] not in ("S1 text", "S4 section") for i in xs),
                   sum(i["matched"] for i in xs), sum(i["lost_at"] == "ok" for i in xs))
            print(f"  {sec:<14}{g:<8}" + "".join(f"{v:>16}" for v in row))
    print("\n=== 3. ITEMS LOST BY FUNNEL STEP (all sections) ===")
    for g in GROUPS:
        c = Counter(i["lost_at"] for i in groups[g]["items"])
        print(f"  {g:<8}" + "  ".join(f"{step}: {c.get(step, 0)}" for step in funnel.LOST_ORDER)
              + f"  ok: {c.get('ok', 0)}  (of {len(groups[g]['items'])})")


def print_decisions(groups: dict) -> None:
    print("\n=== 5. DECISIONS AND REAL LOSSES (thresholds chosen on these resumes: optimistic) ===")
    print(f"  {'group':<8}{'decision':<14}{'resumes':>9}{'with a real loss':>18}{'without':>9}")
    for g in GROUPS:
        for d in ("ACCEPT", "NEEDS_REVIEW", "FAIL"):
            rows = [r for r in groups[g]["decisions"] if r[0] == d]
            loss = sum(r[1] for r in rows)
            print(f"  {g:<8}{d:<14}{len(rows):>9}{loss:>18}{len(rows) - loss:>9}")
    print("  (real loss: recall or precision below 0.8 in any section, or a wrong name)")


def main() -> int:
    global GROUPS
    all_groups = common.load_groups()
    GROUPS = tuple(g for g in common.DEV_GROUPS if all_groups[g])
    eids = common.ids([s for g in GROUPS for s in all_groups[g]])
    loss_by_rid = {r["resume_id"]: any(r["loss"].values()) for r in cal.collect(GROUPS)}
    groups: dict = {}
    for g in GROUPS:
        stems = all_groups[g]
        scored = [ev.score_resume(ev.load_gt(s), ev.load_pred(s)) for s in stems]
        agg = ev.aggregate(scored)
        prf = {sec: ({m: agg["point"][f"{sec}_{m}"] for m in ("P", "R", "F1")},
                     {m: agg["ci"][f"{sec}_{m}"] for m in ("P", "R", "F1")}) for sec in SECTIONS[1:]}
        prf["name"] = name_prf([name_counts(ev.load_gt(s), ev.load_pred(s)) for s in stems])
        path = common.reports_dir() / "funnel" / f"items_{g}.jsonl"
        if not path.is_file():
            print("end_to_end: run eval/funnel.py first (no funnel item files)")
            return 2
        items = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        checks, decisions = [], []
        for s in stems:
            fired = funnel.load_checks(s)
            losses = {(i["lost_at"], i["section"]) for i in items
                      if i["eval_id"] == eids[s]["eval_id"] and i["lost_at"] != "ok"}
            if fired is not None:
                checks.append((fired, losses))
            meta = common.outputs_dir() / f"{s}.meta.json"
            if meta.is_file():
                rid = resume_id_for(s)
                decisions.append((read_json(meta)["decision"], loss_by_rid.get(rid, False)))
        groups[g] = {"prf": prf, "items": items, "checks": checks, "decisions": decisions}
    print("END-TO-END REPORT: " + ", ".join(f"{g} {len(all_groups[g])}" for g in GROUPS) + " resumes "
          "(a small sample; tuned was used to develop the parser)\n")
    print_prf(groups)
    print_funnel(groups)
    print("\n=== 4. CHECK VALIDITY (every stage): does a check fire where a real loss is? ===")
    funnel.print_check_validity({g: {"checks": groups[g]["checks"]} for g in GROUPS})
    print_decisions(groups)
    return 0


if __name__ == "__main__":
    sys.exit(main())
