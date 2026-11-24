"""Rank a folder of parser outputs against a requirement profile and write a CSV.

    python -m shortlisting.cli --profile profile.json --outputs <folder> --out <file.csv>
                               [--stages <folder>] [--today YYYY-MM-DD] [--no-stability]

<folder> holds the parser's <stem>.json main files and <stem>.meta.json sidecars (decision and
flags). --stages (optional) is the saved stage folder (<resume_id>/stage1_lines.json), used for
the whole-text fallbacks. The CSV (outside the repo; a path inside it is refused) has one row per
resume: rank, resume_id, tier, score, certainty, the component values, eligibility, dropped
components, rank range and the tier reasons. Resume IDs only: no name, file name or text. The
console shows counts only.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

from resume_parser.schema import REPO_ROOT, read_json, resume_id_for
from shortlisting.profile import RequirementProfile
from shortlisting.scorer import COMPONENTS, Candidate, Shortlister

COLUMNS = ["rank", "resume_id", "tier", "score", "certainty", "usable_weight_share", *COMPONENTS,
           "eligibility", "dropped_components", "rank_range", "parse_decision", "tier_reasons"]


def stage_text(stages: Path | None, resume_id: str) -> str | None:
    if stages is None:
        return None
    path = stages / resume_id / "stage1_lines.json"
    if not path.is_file():
        return None
    data = (read_json(path) or {}).get("data") or {}
    return "\n".join(str(x.get("text", "")) for p in sorted(data) for x in (data[p] or {}).get("lines") or [])


def load_candidates(folder: Path, stages: Path | None = None) -> list[Candidate]:
    out = []
    for meta in sorted(folder.glob("*.meta.json")):
        stem = meta.name[: -len(".meta.json")]
        side = read_json(meta)
        main = folder / f"{stem}.json"
        resume = read_json(main) if main.is_file() else {}
        rid = resume_id_for(stem)
        flags = sorted({f.get("code", "") if isinstance(f, dict) else str(f) for f in side.get("flags") or []})
        out.append(Candidate(rid, resume, side.get("decision"), flags, stage_text(stages, rid)))
    return out


def row(r: dict) -> dict:
    comp = r["components"]
    return {"rank": r["rank"] or "", "resume_id": r["resume_id"], "tier": r["tier"],
            "score": "" if r["score"] is None else f"{r['score']:.3f}", "certainty": r["certainty"]["level"],
            "usable_weight_share": r["certainty"]["usable_weight_share"],
            **{n: "" if comp[n]["value"] is None else f"{comp[n]['value']:.3f}" for n in COMPONENTS},
            "eligibility": "; ".join(f"{k}={v['result']}" for k, v in r["eligibility"].items()),
            "dropped_components": "; ".join(d["component"] for d in r["dropped_components"]),
            "rank_range": "" if not r["rank_range"] else f"{r['rank_range'][0]}-{r['rank_range'][1]}",
            "parse_decision": r["parse_decision"] or "", "tier_reasons": "; ".join(r["tier_reasons"])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m shortlisting.cli", description=__doc__.splitlines()[0])
    ap.add_argument("--profile", type=Path, required=True)
    ap.add_argument("--outputs", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--stages", type=Path)
    ap.add_argument("--today", type=dt.date.fromisoformat)
    ap.add_argument("--no-stability", action="store_true")
    ap.add_argument("--cards", type=Path, help="also write the explanation cards (JSON) here, outside the repo")
    a = ap.parse_args(argv)
    for target in [a.out] + ([a.cards] if a.cards else []):
        resolved = target.resolve()
        if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
            print("shortlisting: refusing to write inside the repo.", file=sys.stderr)
            return 2
    if not a.outputs.is_dir():
        print("shortlisting: the outputs folder does not exist.", file=sys.stderr)
        return 2
    try:
        profile = RequirementProfile.from_json(a.profile)
    except (ValueError, OSError, TypeError) as exc:
        print(f"shortlisting: bad profile ({exc})", file=sys.stderr)
        return 2
    candidates = load_candidates(a.outputs, a.stages)
    results = Shortlister(profile, today=a.today).rank(candidates, stability=not a.no_stability)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in results:
            w.writerow(row(r))
    if a.cards:
        a.cards.write_text(json.dumps(results, indent=1, sort_keys=True), encoding="utf-8")
    tiers = Counter(r["tier"] for r in results)
    print(f"shortlisting: {len(results)} resume(s) ranked; " + ", ".join(f"{t} {tiers[t]}" for t in ("Shortlist", "Review", "Hold", "Unreadable")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
