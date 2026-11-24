"""The shortlisting scorer on the development resumes: rankings from the ground truth versus the
parser output, aggregates only.

    python eval/shortlist_check.py [--today YYYY-MM-DD]

Development groups only (dev, tuned, dev2); validation, test_frozen, final and fresh are never
read, and nothing is tuned here. Three invented requirement profiles (computing, economics,
design), each in the four modes. Per mode and profile: Spearman rank correlation of the GT-based
and parser-based scores, overlap of the two top-5 lists, tier agreement (also with the parser's
NEEDS_REVIEW decision ignored, which isolates the content), and the share of candidates in each
tier. A FAIL parse has no score and is left out of the correlation. Nothing is written; no resume
text, name, venue or institution is printed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "eval")]

import common  # noqa: E402
from common import EvalError  # noqa: E402
from scipy.stats import spearmanr  # noqa: E402

from experience_check import stage1_text  # noqa: E402
from resume_parser.schema import DataDirError, read_json, resume_id_for  # noqa: E402
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


def candidates(stems: list[str]) -> tuple[list[Candidate], list[Candidate], list[Candidate]]:
    gt, parsed, parsed_plain = [], [], []
    for stem in stems:
        rid, text = resume_id_for(stem), stage1_text(stem)
        gt.append(Candidate(rid, read_json(common.gt_dir() / f"{stem}.json"), None, [], text))
        main, meta = common.outputs_dir() / f"{stem}.json", common.outputs_dir() / f"{stem}.meta.json"
        resume = read_json(main) if main.is_file() else {}
        side = read_json(meta) if meta.is_file() else {}
        flags = sorted({f.get("code", "") for f in side.get("flags") or [] if isinstance(f, dict)})
        parsed.append(Candidate(rid, resume, side.get("decision"), flags, text))
        decision = "FAIL" if side.get("decision") == "FAIL" else None  # NEEDS_REVIEW ignored
        parsed_plain.append(Candidate(rid, resume, decision, flags, text))
    return gt, parsed, parsed_plain


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python eval/shortlist_check.py", description=__doc__.splitlines()[0])
    ap.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    a = ap.parse_args(argv)
    try:
        groups = common.load_groups()
        stems = sorted({s for g in common.DEV_GROUPS for s in groups[g]})
        gt, parsed, plain = candidates(stems)
        venues, institutions = load_venues(), load_institutions()
    except (EvalError, DataDirError) as exc:
        print(f"shortlist_check: {exc}", file=sys.stderr)
        return 2
    n = len(stems)
    print(f"=== SHORTLISTING: GT-based vs parser-based ranking ({n} development resumes; today {a.today}) ===")
    print(f"  parse decisions: " + ", ".join(f"{d} {sum(c.decision == d for c in parsed)}" for d in ("ACCEPT", "NEEDS_REVIEW", "FAIL")))
    head = f"  {'profile':<11}{'mode':<14}{'spearman':>9}{'top5':>6}{'tier agree':>12}{'(no NR)':>9}  " \
           f"{'tier shares GT: S/R/H/U':<26}{'parser: S/R/H/U':<20}{'parser no NR: S/R/H/U'}"
    print(head)
    for pname, spec in PROFILES.items():
        for mode in MODES:
            profile = RequirementProfile(mode=mode, **spec)
            sides = []
            for pool in (gt, parsed, plain):
                s = Shortlister(profile, venues, institutions, today=a.today)
                sides.append({r["resume_id"]: r for r in s.rank(pool, stability=False)})
            g, p, q = sides
            both = [rid for rid in g if g[rid]["score"] is not None and p[rid]["score"] is not None]
            rho = spearmanr([g[r]["score"] for r in both], [p[r]["score"] for r in both]).statistic if len(both) > 2 else float("nan")
            top = lambda side: {r for r, x in sorted(side.items(), key=lambda kv: (kv[1]["rank"] or 10 ** 9, kv[0]))[:5]}  # noqa: E731
            overlap = len(top(g) & top(p))
            agree = sum(g[r]["tier"] == p[r]["tier"] for r in g)
            agree_plain = sum(g[r]["tier"] == q[r]["tier"] for r in g)
            shares = lambda side: "/".join(str(sum(x["tier"] == t for x in side.values())) for t in TIER_ORDER)  # noqa: E731
            print(f"  {pname:<11}{mode:<14}{rho:>9.2f}{overlap:>4}/5{agree:>8}/{n:<3}{agree_plain:>5}/{n:<3}  "
                  f"{shares(g):<26}{shares(p):<20}{shares(q)}")
    print("  (S/R/H/U = Shortlist / Review / Hold / Unreadable; '(no NR)' and 'parser no NR' ignore the parser's"
          " NEEDS_REVIEW decision)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
