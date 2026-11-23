"""Synthetic sweep: many varied invented CVs through the whole pipeline, scored stage by stage.

    python eval/synthetic_sweep.py [--n 300] [--start 0] [--json out.json]

The CVs come from scripts/synthetic_sweep_gen.py (seeded combinations of layout factors, all
content invented); PDFs are built in a temporary folder and deleted afterwards. Nothing real is
read. For each CV and each section (education, experience, publications) and the name:
  S1   the heading and every entry's key text are in the stage 1 lines, heading first, entries
       in order
  S4   every entry's key text lands in a stage 4 section of its kind
  S5m  every GT entry is matched (evaluator matching), with no extra predicted entry
  S5f  and every matched pair has no field error (evaluator field checks)
  name the candidate name matches (evaluator normalised check)
A failing section gets its first failing stage and one cause. The report prints counts only:
pass rates by stage, a table of pass rates per factor value, and the most common failure
patterns with the factor values over-represented in them.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "eval")]

import evaluate as ev  # noqa: E402
import synthetic_sweep_gen as gen  # noqa: E402
from resume_parser.pipeline import run_pipeline  # noqa: E402

SECTIONS = ("education", "experience", "publications")
STAGES = ("S1", "S4", "S5m", "S5f")
HEADER_FOOTER = ("curriculum vitae of", "page 1 of", "page 2 of", "page 3 of")


def _n(text) -> str:
    return " ".join(str(text).lower().split())


def score(spec: gen.Spec, folder: Path) -> dict:
    pdf, gt = gen.build(spec, folder)
    record = run_pipeline(pdf)
    keys = gen.entry_keys(spec)
    s1_lines = [_n(l["text"]) for p in record.results["stage1"].data.values() for l in (p or {}).get("lines") or []]
    # a key may wrap over two lines: search the joined text and map back to the starting line
    joined, starts = "", []
    for line in s1_lines:
        starts.append(len(joined))
        joined += line + " "

    def idx(text):
        pos = joined.find(_n(text))
        if pos < 0:
            return None
        return max(i for i, s in enumerate(starts) if s <= pos)

    sections4 = record.results["stage4"].data["sections"] if record.results.get("stage4") else []
    text4 = [(s["name"], " ".join(_n(l["text"]) for l in s["lines"])) for s in sections4]
    main = record.main or {}
    out = {"seed": spec.seed, "factors": spec.factors, "sections": {}}
    for kind in SECTIONS:
        res = {}
        heading = gen.HEADINGS[kind][gen.ALIAS_INDEX[spec.factors["heading_alias"]]]
        at = [idx(k) for k in keys[kind]]
        h = idx(heading)
        res["S1"] = h is not None and None not in at and all(h < a for a in at) and at == sorted(at)
        homes = [{name for name, txt in text4 if _n(k) in txt} for k in keys[kind]]
        res["S4"] = all(kind in hm for hm in homes)
        pred = main.get(kind) or []
        pairs, _ = ev.match(gt[kind], pred, kind)
        res["S5m"] = len(pairs) == len(gt[kind]) == len(pred)
        errors = sorted({e for gi, pi, _ in pairs for e in ev.field_errors(kind, gt[kind][gi], pred[pi])})
        res["S5f"] = res["S5m"] and not errors
        # cumulative: a stage passes only if the earlier ones passed
        ok = True
        for st in STAGES:
            ok = ok and res[st]
            res[st] = ok
        first = next((st for st in STAGES if not res[st]), None)
        cause = None
        if first == "S1":
            cause = "text missing from stage 1" if (h is None or None in at) else "text out of order"
        elif first == "S4":
            if not any(name == kind for name, _ in text4):
                cause = "heading not recognised"
            else:
                others = sorted({n for hm in homes if kind not in hm for n in hm} or {"no section"})
                cause = "section ended early or late (entries in " + ",".join(others) + ")"
        elif first == "S5m":
            blob = " ".join(_n(json.dumps(p)) for p in pred)
            if any(hf in blob for hf in HEADER_FOOTER):
                cause = "header/footer text read as content"
            elif len(pred) > len(gt[kind]):
                cause = "entries split (extra entries)"
            elif len(pred) < len(gt[kind]):
                cause = "entries merged (fewer entries)"
            else:
                cause = "entries matched wrongly"
        elif first == "S5f":
            if kind == "publications" and {"title", "venue"} & set(errors):
                cause = "title/venue boundary wrong (" + ",".join(errors) + ")"
            else:
                cause = "field wrong: " + ",".join(errors)
        res["first_fail"], res["cause"] = first, cause
        out["sections"][kind] = res
    pname = ev._s(main.get("candidate_name"))
    out["name"] = bool(pname) and ev.norm_name(pname) == ev.norm_name(gt["candidate_name"])
    out["name_cause"] = None if out["name"] else ("name empty" if not pname else "name wrong")
    out["pages"] = len(record.results["stage1"].data)
    return out


def report(rows: list[dict]) -> None:
    n = len(rows)
    print(f"=== SYNTHETIC SWEEP: {n} combinations ===\n")
    print("1. pass rate by stage (cumulative: a stage counts only when the earlier ones passed)")
    print(f"   {'section':<14}" + "".join(f"{s:>8}" for s in STAGES))
    for kind in SECTIONS:
        print(f"   {kind:<14}" + "".join(f"{100 * sum(r['sections'][kind][s] for r in rows) / n:>7.1f}%" for s in STAGES))
    print(f"   {'name':<14}{100 * sum(r['name'] for r in rows) / n:>7.1f}%")

    print("\n2. factor table: pass rate per factor value (education | experience | publications as "
          "S1/S4/S5m/S5f; name)")
    for factor, values in gen.FACTORS.items():
        print(f"   {factor}")
        for v in values:
            sel = [r for r in rows if r["factors"][factor] == v]
            if not sel:
                continue
            cells = []
            for kind in SECTIONS:
                cells.append("/".join(f"{100 * sum(r['sections'][kind][s] for r in sel) / len(sel):.0f}" for s in STAGES))
            name = 100 * sum(r["name"] for r in sel) / len(sel)
            print(f"      {v:<20} n={len(sel):>3}  " + " | ".join(f"{c:<15}" for c in cells) + f" | name {name:.0f}")

    print("\n3. failure patterns (section, first failing stage, cause), with over-represented factor values")
    patterns = defaultdict(list)
    for r in rows:
        for kind in SECTIONS:
            s = r["sections"][kind]
            if s["first_fail"]:
                patterns[(kind, s["first_fail"], s["cause"])].append(r)
        if not r["name"]:
            patterns[("name", "S5", r["name_cause"])].append(r)
    overall = {f: Counter(r["factors"][f] for r in rows) for f in gen.FACTORS}
    for (kind, st, cause), sel in sorted(patterns.items(), key=lambda kv: -len(kv[1]))[:10]:
        lifts = []
        for f in gen.FACTORS:
            for v, c in Counter(r["factors"][f] for r in sel).items():
                share_in, share_all = c / len(sel), overall[f][v] / n
                if c >= 3 and share_in >= 0.5:
                    lifts.append((share_in / share_all, f, v, c))
        lifts.sort(reverse=True)
        top = ", ".join(f"{f}={v} ({c}/{len(sel)}, x{lift:.1f})" for lift, f, v, c in lifts[:4])
        print(f"   {len(sel):>4} combos  {kind:<13}{st:<5} {cause}\n         over-represented: {top or '-'}")
    print(f"\n   all patterns: {len(patterns)}; combos passing every section and the name: "
          f"{sum(all(r['sections'][k]['S5f'] for k in SECTIONS) and r['name'] for r in rows)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--json", type=Path, help="also write every combination's result here (synthetic only)")
    a = ap.parse_args(argv)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for seed in range(a.start, a.start + a.n):
            try:
                rows.append(score(gen.combo(seed), Path(tmp)))
            except Exception as exc:  # a crash is a finding, not a reason to stop
                print(f"seed {seed}: {type(exc).__name__}: {exc}")
    report(rows)
    if a.json:
        a.json.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
