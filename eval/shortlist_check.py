"""The shortlisting scorer on the development resumes: rankings from the ground truth versus the
parser output, aggregates only.

    python eval/shortlist_check.py [--today YYYY-MM-DD]

Development groups only (dev, tuned, dev2); validation, test_frozen, final and fresh are never
read, and nothing is tuned here. Three invented requirement profiles (computing, economics,
design), each in the four modes. Per mode and profile: Spearman rank correlation of the GT-based
and parser-based scores, overlap of the two top-5 lists, tier agreement (with the configured
parse_decision_forces_review, and as if it were true), GT-Shortlist candidates by parser tier,
parser-Shortlist candidates that are GT-Hold, and the component behind each GT-Shortlist that the
parser side holds, and the share of candidates in each
tier. A FAIL parse has no score and is left out of the correlation. Nothing is written; no resume
text, name, venue or institution is printed.
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
from common import EvalError  # noqa: E402
from scipy.stats import spearmanr  # noqa: E402

from experience_check import stage1_text  # noqa: E402
from resume_parser.schema import DataDirError, load_config, read_json, resume_id_for  # noqa: E402
from shortlisting.profile import MODES, RequirementProfile  # noqa: E402
from shortlisting.scorer import TIER_ORDER, Candidate, Shortlister  # noqa: E402
from shortlisting.tiers.institutions import load_index as load_institutions  # noqa: E402
from shortlisting.tiers.venues import load_index as load_venues  # noqa: E402

PROFILES = {
    "computing": dict(field="ai_ml_data_science", min_degree_level="masters", experience_kind="academic", experience_years=5,
                      institution_tier_cutoff="Tier 2", venue_tier_cutoff="B",
                      keywords=["machine learning", "deep learning", "neural networks", "computer vision", "language processing"],
                      synonyms={"deep learning": ["neural network"], "computer vision": ["image recognition"]}),
    "economics": dict(field="economics", min_degree_level="masters", experience_kind="academic", experience_years=5,
                      institution_tier_cutoff="Tier 2", venue_tier_cutoff="B",
                      keywords=["econometrics", "development economics", "labour markets", "public policy", "finance"],
                      synonyms={"labour markets": ["labor market", "employment"]}),
    "design": dict(field="humanities_social_sciences_design", min_degree_level="masters", experience_kind="academic",
                   experience_years=2, institution_tier_cutoff="Tier 2", venue_tier_cutoff="B",
                   keywords=["interaction design", "user experience", "visual communication", "design research", "usability"],
                   synonyms={"user experience": ["ux", "user centred"]}),
}


def candidates(stems: list[str]) -> tuple[list[Candidate], list[Candidate]]:
    gt, parsed = [], []
    for stem in stems:
        rid, text = resume_id_for(stem), stage1_text(stem)
        gt.append(Candidate(rid, read_json(common.gt_dir() / f"{stem}.json"), None, [], text))
        main, meta = common.outputs_dir() / f"{stem}.json", common.outputs_dir() / f"{stem}.meta.json"
        resume = read_json(main) if main.is_file() else {}
        side = read_json(meta) if meta.is_file() else {}
        flags = sorted({f.get("code", "") for f in side.get("flags") or [] if isinstance(f, dict)})
        parsed.append(Candidate(rid, resume, side.get("decision"), flags, text))
    return gt, parsed


def hold_cause(g: dict, p: dict) -> list[str]:
    """Why a GT-Shortlist candidate is Hold from the parser: the eligibility checks that fail, or
    else the component whose weighted contribution fell most (marked when unusable on the parser side)."""
    fails = [k for k, v in p["eligibility"].items() if v["result"] == "fail"]
    if fails:
        return [f"eligibility fail: {k}" for k in fails]
    drop = {}
    for n in g["components"]:
        gv = g["components"][n]["value"] or 0.0
        pv = p["components"][n]["value"] or 0.0
        drop[n] = g["weights_used"].get(n, 0) * gv - p["weights_used"].get(n, 0) * pv
    n = max(drop, key=lambda k: (drop[k], k))
    return [n + (" (unusable from the parser)" if p["components"][n]["value"] is None else "")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python eval/shortlist_check.py", description=__doc__.splitlines()[0])
    ap.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    a = ap.parse_args(argv)
    try:
        groups = common.load_groups()
        stems = sorted({s for g in common.DEV_GROUPS for s in groups[g]})
        gt, parsed = candidates(stems)
        venues, institutions = load_venues(), load_institutions()
    except (EvalError, DataDirError) as exc:
        print(f"shortlist_check: {exc}", file=sys.stderr)
        return 2
    cfg = load_config("shortlisting.json")
    forced_cfg = dict(cfg, parse_decision_forces_review=True)
    n = len(stems)
    print(f"=== SHORTLISTING: GT-based vs parser-based ranking ({n} development resumes; today {a.today}; "
          f"parse_decision_forces_review={cfg['parse_decision_forces_review']}) ===")
    print("  parse decisions: " + ", ".join(f"{d} {sum(c.decision == d for c in parsed)}" for d in ("ACCEPT", "NEEDS_REVIEW", "FAIL")))
    print(f"  {'profile':<11}{'mode':<14}{'spearman':>9}{'top5':>6}{'tier agree':>12}{'(forced)':>10}  "
          f"{'GT S/R/H/U':<12}{'parser S/R/H/U':<16}{'GT-S -> parser S/R/H/U':<24}{'parser-S that are GT-H':>23}")
    all_causes, totals = Counter(), Counter()
    for pname, spec in PROFILES.items():
        for mode in MODES:
            profile = RequirementProfile(mode=mode, **spec)
            sides = []
            for pool, conf in ((gt, cfg), (parsed, cfg), (parsed, forced_cfg)):
                s = Shortlister(profile, venues, institutions, today=a.today, config=conf)
                sides.append({r["resume_id"]: r for r in s.rank(pool, stability=False)})
            g, p, forced = sides
            both = [rid for rid in g if g[rid]["score"] is not None and p[rid]["score"] is not None]
            rho = spearmanr([g[r]["score"] for r in both], [p[r]["score"] for r in both]).statistic if len(both) > 2 else float("nan")
            top = lambda side: {r for r, x in sorted(side.items(), key=lambda kv: (kv[1]["rank"] or 10 ** 9, kv[0]))[:5]}  # noqa: E731
            overlap = len(top(g) & top(p))
            agree = sum(g[r]["tier"] == p[r]["tier"] for r in g)
            agree_forced = sum(g[r]["tier"] == forced[r]["tier"] for r in g)
            shares = lambda side: "/".join(str(sum(x["tier"] == t for x in side.values())) for t in TIER_ORDER)  # noqa: E731
            gs = [r for r in g if g[r]["tier"] == "Shortlist"]
            cross = Counter(p[r]["tier"] for r in gs)
            ps_gh = sum(1 for r in p if p[r]["tier"] == "Shortlist" and g[r]["tier"] == "Hold")
            for r in gs:
                totals["GT-S"] += 1
                totals[f"GT-S -> parser {p[r]['tier']}"] += 1
                if p[r]["tier"] == "Hold":
                    all_causes.update(hold_cause(g[r], p[r]))
            totals["parser-S"] += sum(1 for r in p if p[r]["tier"] == "Shortlist")
            totals["parser-S that are GT-H"] += ps_gh
            print(f"  {pname:<11}{mode:<14}{rho:>9.2f}{overlap:>4}/5{agree:>8}/{n:<3}{agree_forced:>6}/{n:<3}  "
                  f"{shares(g):<12}{shares(p):<16}{'/'.join(str(cross[t]) for t in TIER_ORDER):<24}{ps_gh:>23}")
    print("  (S/R/H/U = Shortlist / Review / Hold / Unreadable; '(forced)' = tier agreement if NEEDS_REVIEW still forced Review)")
    print(f"\n  over all 12 rows: GT-Shortlist {totals['GT-S']} -> parser Shortlist {totals['GT-S -> parser Shortlist']}, "
          f"Review {totals['GT-S -> parser Review']}, Hold {totals['GT-S -> parser Hold']}, "
          f"Unreadable {totals['GT-S -> parser Unreadable']}; parser-Shortlist {totals['parser-S']}, "
          f"of which GT-Hold {totals['parser-S that are GT-H']}")
    print("  GT-Shortlist that is parser-Hold, by cause: " + ", ".join(f"{k} {v}" for k, v in all_causes.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())