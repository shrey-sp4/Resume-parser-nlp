"""The experience calculator (src/shortlisting/experience.py) on the development resumes: ground
truth versus parser output, counts only.

    python eval/experience_check.py [--today YYYY-MM-DD]

Development groups only (dev, tuned, dev2); validation, test_frozen, final and fresh are never
read. For academic and industry and the tiers 2, 5 and 10 it runs the calculator on the GT
experience entries and on the parser's main JSON of the same resume (the statement fallback reads
the saved stage 1 lines), and prints how many resumes get each status from each, how many
agree, and the causes behind the disagreements, found by pairing GT and parser entries with the
evaluator's own matching (eval/evaluate.py):
  type wrong     a paired entry counts for the type on one side only
  date missing   a paired entry has a worse date on the parser side (full > year only > start only > missing)
  date wrong     a paired entry has usable dates on both sides that read differently
  extra entry    a parser entry of the type with no GT partner
  missed entry   a GT entry of the type with no parser partner
Nothing is written; no resume text, name or ID is printed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "eval")]

import common  # noqa: E402
import evaluate as ev  # noqa: E402
from common import EvalError  # noqa: E402

from resume_parser.schema import DataDirError, read_json, resume_id_for  # noqa: E402
from shortlisting.experience import KINDS, TIERS, compute_experience, read_entry  # noqa: E402

STATUSES = ("meets", "likely_meets", "does_not_meet", "unknown")
QUALITY_RANK = {"full": 3, "year_only": 2, "start_only": 1, "missing": 0}
CAUSES = ("type wrong", "date missing", "date wrong", "extra entry", "missed entry")


def stage1_text(stem: str) -> str:
    path = common.stages_dir() / resume_id_for(stem) / "stage1_lines.json"
    if not path.is_file():
        return ""
    data = (read_json(path) or {}).get("data") or {}
    return "\n".join(str(line.get("text", "")) for page in sorted(data) for line in (data[page] or {}).get("lines") or [])


def causes(gt_exp: list, pred_exp: list, kind: str, today: dt.date) -> set[str]:
    pairs, _ = ev.match(gt_exp, pred_exp, "experience")
    found, g_used, p_used = set(), set(), set()
    for gi, pi, _score in pairs:
        g_used.add(gi)
        p_used.add(pi)
        g, p = gt_exp[gi], pred_exp[pi]
        g_k, p_k = g.get("experience_type") == kind, p.get("experience_type") == kind
        if g_k != p_k:
            found.add("type wrong")
            continue
        if not g_k:
            continue
        gq, _, gspan = read_entry(g.get("dates") or "", today)
        pq, _, pspan = read_entry(p.get("dates") or "", today)
        if QUALITY_RANK[pq] < QUALITY_RANK[gq]:
            found.add("date missing")
        elif gspan and pspan and (gspan.start, gspan.end) != (pspan.start, pspan.end):
            found.add("date wrong")
    if any(i not in p_used and e.get("experience_type") == kind for i, e in enumerate(pred_exp)):
        found.add("extra entry")
    if any(i not in g_used and e.get("experience_type") == kind for i, e in enumerate(gt_exp)):
        found.add("missed entry")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python eval/experience_check.py", description=__doc__.splitlines()[0])
    ap.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today(), help="the date 'Present' ends on")
    a = ap.parse_args(argv)
    try:
        groups = common.load_groups()
        stems = sorted({s for g in common.DEV_GROUPS for s in groups[g]})
        missing_pred = 0
        rows = []
        for stem in stems:
            gt = read_json(common.gt_dir() / f"{stem}.json")
            pred_path = common.outputs_dir() / f"{stem}.json"
            pred = read_json(pred_path) if pred_path.is_file() else {}
            missing_pred += not pred_path.is_file()
            text = stage1_text(stem)
            for kind in KINDS:
                why = causes(gt.get("experience") or [], pred.get("experience") or [], kind, a.today)
                for tier in TIERS:
                    g = compute_experience(gt, kind, tier, today=a.today, text=text)
                    p = compute_experience(pred, kind, tier, today=a.today, text=text)
                    rows.append((kind, tier, g, p, why))
    except (EvalError, DataDirError) as exc:
        print(f"experience_check: {exc}", file=sys.stderr)
        return 2

    print(f"=== EXPERIENCE CALCULATOR: development GT vs parser ({len(stems)} resumes; today {a.today}) ===")
    if missing_pred:
        print(f"  {missing_pred} resume(s) have no parser output (read as no entries)")
    head = "".join(f"{s:>15}" for s in STATUSES)
    print(f"\n  {'kind':<10}{'tier':>5}  {'side':<7}{head}{'agree':>8}")
    for kind in KINDS:
        for tier in TIERS:
            sel = [r for r in rows if r[0] == kind and r[1] == tier]
            g = Counter(r[2]["status"] for r in sel)
            p = Counter(r[3]["status"] for r in sel)
            agree = sum(r[2]["status"] == r[3]["status"] for r in sel)
            print(f"  {kind:<10}{tier:>5}  {'GT':<7}" + "".join(f"{g[s]:>15}" for s in STATUSES) + f"{agree:>5}/{len(sel)}")
            print(f"  {'':<10}{'':>5}  {'parser':<7}" + "".join(f"{p[s]:>15}" for s in STATUSES))
    dis = [r for r in rows if r[2]["status"] != r[3]["status"]]
    print(f"\n  disagreements: {len(dis)} of {len(rows)} (resume x kind x tier)")
    print(f"  {'cause (a case can have several)':<36}{'cases':>7}{'resume x kind':>15}")
    for cause in CAUSES + ("none found (entries agree)",):
        hit = [r for r in dis if (cause in r[4]) or (cause.startswith("none") and not r[4])]
        print(f"  {cause:<36}{len(hit):>7}{len({(id(r[4]), r[0]) for r in hit}):>15}")
    src = Counter((r[2]["source"], r[3]["source"]) for r in dis)
    print("  source of the two results in the disagreements (GT, parser): " + ", ".join(f"{k[0]}/{k[1]} {v}" for k, v in sorted(src.items())))
    unk = Counter((side, r[i]["reason"]) for r in rows for side, i in (("GT", 2), ("parser", 3)) if r[i]["status"] == "unknown")
    print("  why unknown (all cases): " + ", ".join(f"{s} {k} {v}" for (s, k), v in sorted(unk.items())))
    pairs = Counter(f"{r[2]['status']} -> {r[3]['status']}" for r in dis)
    print("  GT status -> parser status: " + ", ".join(f"{k} {v}" for k, v in pairs.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
