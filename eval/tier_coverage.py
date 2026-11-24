"""Coverage of the tier lookups on the development ground truth: counts only.

    python eval/tier_coverage.py                    venues and institutions
    python eval/tier_coverage.py --field economics  venue and institution tiers for that role field
                                                    (config/venue_fields.json, config/tiers.json nirf.field_categories)

Development groups only (dev, tuned, dev2); validation, test_frozen, final and fresh are never read.
Reads the GT publication venue and type fields (and, for institutions, the education and
experience institution fields) and prints counts and shares only: never a venue, institution,
resume name or file name. The strings that found no tier are written, sorted and de-duplicated,
to RESUME_DATA_DIR/reports/tiers/ (unmatched_venues.txt, unmatched_institutions.txt) for private
review.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "eval")]

import common  # noqa: E402
from common import EvalError  # noqa: E402

from resume_parser.schema import DataDirError, read_json  # noqa: E402
from shortlisting.tiers.lexicons import LexiconError  # noqa: E402
from shortlisting.tiers.institutions import load_index as load_institution_index  # noqa: E402
from shortlisting.tiers.venues import load_index  # noqa: E402

UNMATCHED_VENUES = "unmatched_venues.txt"
UNMATCHED_INSTITUTIONS = "unmatched_institutions.txt"
NO_TIER = {"no_match", "no_candidate", "short_name", "rejected_by_token_guard", "ambiguous_margin",
           "ambiguous_duplicate_title", "ambiguous_acronym", "ambiguous_prefix", "ambiguous_colon", "ambiguous_publisher"}


def development_gt() -> list[dict]:
    groups = common.load_groups()
    stems = sorted({s for g in common.DEV_GROUPS for s in groups[g]})
    return [read_json(common.gt_dir() / f"{s}.json") for s in stems]


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:5.1f}%" if b else "   n/a"


def venue_coverage(gts: list[dict], field_name: str | None = None) -> tuple[dict, list[str]]:
    index = load_index()
    by_type: dict[str, Counter] = {}
    unmatched: set[str] = set()
    for gt in gts:
        for pub in gt.get("publications") or []:
            ty = pub.get("type") or "none"
            venue = str(pub.get("venue") or "").strip()
            c = by_type.setdefault(ty, Counter())
            c["entries"] += 1
            if not venue:
                c["no venue"] += 1
                continue
            c["with venue"] += 1
            res = index.tier(venue, ty, field_name)
            c[f"tier {res['tier'] or 'unknown'}"] += 1
            c[f"method {res['method'] or 'none'}"] += 1
            c[f"certainty {res['certainty']}"] += 1
            c[f"reason {res['reason']}"] += 1
            if res["out_of_field"]:
                c["out of field"] += 1
            if res["from_part"]:
                c["from part"] += 1
            if ty in ("journal", "conference") and res["reason"] in NO_TIER:
                unmatched.add(f"{ty}\t{res['reason']}\t{venue}")
    return by_type, sorted(unmatched)


def print_venues(by_type: dict, n_resumes: int, field_name: str | None) -> None:
    print(f"=== VENUE TIERS (development GT: {n_resumes} resumes; field: {field_name or 'none'}) ===")
    for ty in ("journal", "conference", "other", "none"):
        c = by_type.get(ty)
        if not c:
            continue
        wv = c["with venue"]
        tiered = sum(c[f"tier {t}"] for t in ("A", "B", "C"))
        print(f"\n  {ty}: {c['entries']} entries, {wv} with a venue ({_pct(wv, c['entries'])})")
        if ty not in ("journal", "conference"):
            print("    not tiered (only journal and conference entries are)")
            continue
        print(f"    mapped to a tier: {tiered} of {wv} ({_pct(tiered, wv)})")
        print("    tiers:     " + ", ".join(f"{t} {c[f'tier {t}']} ({_pct(c[f'tier {t}'], wv).strip()})" for t in ("A", "B", "C", "unknown")))
        print("    method:    " + ", ".join(f"{m} {c[f'method {m}']}" for m in ("issn", "exact", "title_alias", "acronym", "fuzzy", "prefix", "colon", "publisher", "none") if c[f"method {m}"]))
        print("    certainty: " + ", ".join(f"{k} {c[f'certainty {k}']}" for k in ("high", "medium", "none") if c[f"certainty {k}"]))
        print(f"    matched on one comma-separated part: {c['from part']}")
        reasons = sorted((k[7:], v) for k, v in c.items() if k.startswith("reason ") and k != "reason ok")
        print("    no tier, by reason: " + (", ".join(f"{k} {v}" for k, v in reasons) or "none"))
        if field_name:
            print(f"    out of field (best overall quartile used): {c['out of field']}")


def institution_coverage(gts: list[dict], field_name: str | None = None) -> tuple[dict, list[str]]:
    """Education institutions and the institutions of academic experience entries (industry and
    other experience is counted, not tiered: NIRF and the seed list rank academic institutions)."""
    index = load_institution_index()
    by_kind: dict[str, Counter] = {}
    unmatched: set[str] = set()
    for gt in gts:
        items = [("education", e.get("institution")) for e in gt.get("education") or []]
        items += [("experience " + (e.get("experience_type") or "other"), e.get("institution")) for e in gt.get("experience") or []]
        for kind, inst in items:
            c = by_kind.setdefault(kind, Counter())
            c["entries"] += 1
            inst = str(inst or "").strip()
            if not inst:
                c["no institution"] += 1
                continue
            c["with institution"] += 1
            if kind not in ("education", "experience academic"):
                continue
            res = index.tier(inst, field_name)
            c[f"tier {res['tier'] or 'none'}"] += 1
            c[f"status {res['status']}"] += 1
            c[f"source {res['source'] or 'none'}"] += 1
            c[f"method {res['method'] or 'none'}"] += 1
            c[f"reason {res['reason']}"] += 1
            c["from part"] += res["from_part"]
            if res["tier"] is None:
                unmatched.add(f"{kind}\t{res['reason']}\t{inst}")
    return by_kind, sorted(unmatched)


def print_institutions(by_kind: dict, n_resumes: int, field_name: str | None = None) -> None:
    print(f"\n=== INSTITUTION TIERS (development GT: {n_resumes} resumes; NIRF 2025 + global seed; "
          f"field: {field_name or 'none (general NIRF categories only)'}) ===")
    for kind in ("education", "experience academic", "experience industry", "experience other"):
        c = by_kind.get(kind)
        if not c:
            continue
        wi = c["with institution"]
        print(f"\n  {kind}: {c['entries']} entries, {wi} with an institution ({_pct(wi, c['entries'])})")
        if kind not in ("education", "experience academic"):
            print("    not tiered (NIRF and the seed list cover academic institutions)")
            continue
        tiered = sum(c[f"tier Tier {k}"] for k in (1, 2, 3))
        print(f"    mapped to a tier: {tiered} of {wi} ({_pct(tiered, wi)})")
        print("    tiers:   " + ", ".join(f"Tier {k} {c[f'tier Tier {k}']} ({_pct(c[f'tier Tier {k}'], wi).strip()})" for k in (1, 2, 3))
              + f", unlisted {c['status unlisted']}, ambiguous {c['status ambiguous']}  (unlisted and ambiguous: review)")
        print("    source:  " + ", ".join(f"{s} {c[f'source {s}']}" for s in ("nirf2025", "global_seed", "editorial override") if c[f"source {s}"]))
        print("    method:  " + ", ".join(f"{m} {c[f'method {m}']}" for m in ("alias", "pattern", "exact", "fuzzy", "none") if c[f"method {m}"]))
        print(f"    matched on one comma-separated part: {c['from part']}")
        reasons = sorted((k[7:], v) for k, v in c.items() if k.startswith("reason ") and k != "reason ok")
        print("    no tier, by reason: " + (", ".join(f"{k} {v}" for k, v in reasons) or "none"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python eval/tier_coverage.py", description=__doc__.splitlines()[0])
    ap.add_argument("--field", help="role field for the venue tiers (a key of config/venue_fields.json)")
    a = ap.parse_args(argv)
    try:
        gts = development_gt()
        by_type, unmatched = venue_coverage(gts, a.field)
        print_venues(by_type, len(gts), a.field)
        out = common.reports_dir() / "tiers" / UNMATCHED_VENUES
        common.write_text_inside(out, "".join(line + "\n" for line in unmatched))
        print(f"\n  {len(unmatched)} distinct unmatched journal/conference venue(s) written to "
              f"RESUME_DATA_DIR/reports/tiers/{UNMATCHED_VENUES} (not printed)")
        by_kind, unlisted = institution_coverage(gts, a.field)
        print_institutions(by_kind, len(gts), a.field)
        out = common.reports_dir() / "tiers" / UNMATCHED_INSTITUTIONS
        common.write_text_inside(out, "".join(line + "\n" for line in unlisted))
        print(f"\n  {len(unlisted)} distinct institution(s) with no tier written to "
              f"RESUME_DATA_DIR/reports/tiers/{UNMATCHED_INSTITUTIONS} (not printed)")
    except (EvalError, DataDirError, LexiconError, KeyError) as exc:
        print(f"tier_coverage: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
