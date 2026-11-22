"""Score predictions against ground truth, per resume and per field (docs/design.md, section 6).

    python eval/evaluate.py                       dev, tuned, dev2: per-resume rows and aggregates
    python eval/evaluate.py --calibrate           best-pair similarity histograms (dev + tuned)
    python eval/evaluate.py --json reports/x.json also write results (relative to RESUME_DATA_DIR;
                                                  any folder outside it is refused)
    python eval/evaluate.py --fingerprint         print the evaluator hashes and exit
    python eval/evaluate.py --validation          ALSO validation, aggregates only; every run is
                                                  logged (reports/validation_runs.log), and from
                                                  the 4th run on a warning is printed
    python eval/evaluate.py --test-frozen         ALSO test_frozen, aggregates only, once;
                                                  a second run needs --allow-rerun "<reason>"
    python eval/evaluate.py --final               ONLY the final set (outside RESUME_DATA_DIR,
                                                  resume_parser/final.py), aggregates only, logged
                                                  in RESUME_FINAL_OUT/final_runs.log; a second run
                                                  needs --allow-rerun "<reason>"

Ground truth: RESUME_DATA_DIR/ground_truth_v2/<stem>.json. Predictions: RESUME_DATA_DIR/outputs/
<stem>.json, in the same format, so both are read with the same code. A missing prediction is
scored as an empty one (every GT entry is a miss).

Entry matching
  education      one-to-one fuzzy matching; pair score = min(degree+stream, institution)
  experience     same; pair score = min(job_title, institution)
  publications   pair score = partial ratio of GT title and predicted title (full ratio for
                 GT titles shorter than MIN_PUB_TITLE_CHARS)
  A pair counts only at or above THRESHOLDS. A GT component that is empty is left out of the
  pair score (not treated as a mismatch). Different degree levels never match.

Field checks on matched pairs. A GT field that is EMPTY is skipped: it is not in the
denominator, and the number skipped is reported. "pred when GT empty" (the parser gave a value
although the GT has none) is reported separately and never scored.
  name             exact (outer whitespace stripped); name_norm: no Dr./Prof., case, punctuation
  edu_institution  normalised exact (the GT may add a location after a comma)
  exp_institution  same
  exp_dates        exact as printed
  pub_year         exact as printed
  pub_venue        partial ratio >= VENUE_MIN
  pub_type         equal
  Further rows, kept apart so the rows above stay comparable with earlier runs:
  edu_degree_level equal; edu_stream normalised exact; exp_job_title normalised exact;
  exp_experience_type equal; exp_dates_spacing: exp_dates once the spaces around dashes are
  removed (a reading aid next to exp_dates; the funnel and field_errors use exp_dates).
  pub counts       predicted entries per type vs GT stated_totals, where the GT has one
  CIs              95% percentile bootstrap over resumes (micro-aggregated), fixed seed

Console rows show resume IDs only. Files written here use the salted eval IDs.
validation and test_frozen are never shown or stored per resume.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Callable

import numpy as np
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from common import EvalError  # noqa: E402

from resume_parser.schema import DataDirError, data_root, read_json, require_inside_data_root  # noqa: E402

TOOL = "evaluate"

# ---- frozen settings (hashed into every test_frozen log line) ----------------
EVALUATOR_VERSION = "rev2"
VENUE_MIN = 85.0
THRESHOLDS = {"education": 70.0, "experience": 70.0, "publications": 85.0}
BOOTSTRAP = {"n": 10000, "seed": 0, "alpha": 0.05}
MIN_PUB_TITLE_CHARS = 15  # shorter GT titles use the full ratio, not partial (avoids trivial hits)
GENERIC_INST_WORDS = frozenset(
    "university universities institute institution of the and for in at dept department "
    "school college faculty centre center india indian national deemed technology science "
    "sciences engineering information management research studies".split()
)
# degree-level canonicaliser used only for matching (checked in order; first hit wins)
DEGREE_LEVEL_PATTERNS = (
    ("postdoc", r"\bpost ?doc"),
    ("doctorate", r"\bph ?d\b|\bdoctor(ate)? of\b|\bd ?phil\b"),
    ("mphil", r"\bm ?phil\b"),
    ("masters", r"\bmaster|\bm ?(sc|tech|e|a|s|com|ca|ba|pharm|stat)\b|\bmba\b|\bpgd"),
    ("bachelors", r"\bbachelor|\bb ?(sc|tech|e|a|s|com|ca|pharm|stat)\b"),
)
# ------------------------------------------------------------------------------

SECTIONS = ("education", "experience", "publications")
OLD_FIELD_CHECKS = ("name", "name_norm", "edu_institution", "exp_institution", "exp_dates",
                    "pub_year", "pub_venue", "pub_type")
NEW_FIELD_CHECKS = ("edu_degree_level", "edu_stream", "exp_job_title", "exp_experience_type", "exp_dates_spacing")
FIELD_CHECKS = OLD_FIELD_CHECKS + NEW_FIELD_CHECKS
MATCH_COMPONENTS = ("edu_stream", "edu_institution", "exp_job_title", "exp_institution")
COUNT_KEYS = ("tp", "fp", "fn", "n_gt", "n_pred")


def settings() -> dict:
    return {
        "EVALUATOR_VERSION": EVALUATOR_VERSION, "VENUE_MIN": VENUE_MIN, "THRESHOLDS": THRESHOLDS,
        "BOOTSTRAP": BOOTSTRAP, "MIN_PUB_TITLE_CHARS": MIN_PUB_TITLE_CHARS,
        "GENERIC_INST_WORDS": sorted(GENERIC_INST_WORDS),
        "DEGREE_LEVEL_PATTERNS": [list(p) for p in DEGREE_LEVEL_PATTERNS],
        "FIELD_CHECKS": list(FIELD_CHECKS),
    }


# ---- normalisation and similarity --------------------------------------------


def norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _tsr(a, b) -> float | None:
    a, b = norm(a), norm(b)
    if not a and not b:
        return None
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(a, b)


def _min_present(*xs) -> float:
    """Pair score = weakest component: an entry matches only if EVERY field the GT has
    (degree/role AND institution) matches. A missing GT field is not compared."""
    xs = [x for x in xs if x is not None]
    return min(xs) if xs else 0.0


def _inst_norm(s) -> str:
    return " ".join(t for t in norm(s).split() if t not in GENERIC_INST_WORDS and not t.isdigit())


def _first_segment(s: str) -> str:
    return re.split(r"[,(;]| - ", str(s), maxsplit=1)[0]


def inst_sim(g, p) -> float | None:
    """Institution similarity on distinctive tokens only (generic words such as
    'university of' would otherwise make different institutions look alike)."""
    if not norm(g):
        return None

    def one(gs):
        a, b = _inst_norm(gs), _inst_norm(p)
        if not a:  # only generic words: compare the full strings
            return _tsr(gs, p)
        return fuzz.token_set_ratio(a, b) if b else 0.0

    # the GT often appends a location ("X University, Town, Country"); also try the
    # first segment alone, which is normally the institution name
    first = _first_segment(g)
    return max(one(g), one(first) if norm(first) else 0.0)


def degree_level(s):
    s = norm(s)
    for lvl, pat in DEGREE_LEVEL_PATTERNS:
        if re.search(pat, s):
            return lvl
    return None


def _join(*parts) -> str:
    return " ".join(x for x in parts if x)


def score_edu(g, p) -> float:
    deg = _tsr(_join(g.get("degree"), g.get("stream")), _join(p.get("degree"), p.get("stream")))
    lg, lp = degree_level(g.get("degree")), degree_level(p.get("degree"))
    if lg and lp and lg != lp:
        deg = 0.0  # e.g. B.Sc. vs M.Sc.: different entries however similar the strings
    return _min_present(deg, inst_sim(g.get("institution"), p.get("institution")))


def score_exp(g, p) -> float:
    title = _tsr(g.get("job_title"), p.get("job_title")) if norm(g.get("job_title")) else None
    return _min_present(title, inst_sim(g.get("institution"), p.get("institution")))


def score_pub(g, p) -> float:
    t, c = norm(g.get("title")), norm(p.get("title"))
    if not t or not c:
        return 0.0
    if len(t) < MIN_PUB_TITLE_CHARS:
        return fuzz.ratio(t, c)
    return fuzz.partial_ratio(t, c)


SCORERS = {"education": score_edu, "experience": score_exp, "publications": score_pub}


def match(gt: list, pred: list, section: str):
    """One-to-one assignment maximising total above-threshold similarity.
    Returns (pairs [(gi, pi, score)], score_matrix)."""
    thr = THRESHOLDS[section]
    if not gt or not pred:
        return [], np.zeros((len(gt), len(pred)))
    S = np.array([[SCORERS[section](g, p) for p in pred] for g in gt], dtype=float)
    W = np.where(S >= thr, S, 0.0)
    rows, cols = linear_sum_assignment(-W)
    return [(int(r), int(c), float(S[r, c])) for r, c in zip(rows, cols) if W[r, c] > 0], S


# ---- field checks ----------------------------------------------------------------


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def norm_name(s) -> str:
    return norm(re.sub(r"^\s*(dr|prof|mr|ms|mrs)\b\.?\s*", "", str(s or ""), flags=re.I))


def _inst_exact(gs: str, ps: str) -> bool:
    ps = norm(ps)
    return bool(ps) and ps in (norm(gs), norm(_first_segment(gs)))


def _venue_ok(gv: str, pv: str) -> bool:
    return bool(norm(gv)) and bool(norm(pv)) and fuzz.partial_ratio(norm(gv), norm(pv)) >= VENUE_MIN


def _equal(g: str, p: str) -> bool:
    return g == p


def _norm_equal(g: str, p: str) -> bool:
    return bool(norm(p)) and norm(g) == norm(p)


def _dates_spacing_equal(g: str, p: str) -> bool:
    """Dates equal once the spaces around dashes are removed and other spaces collapsed
    ("Jul 2019 - Jun 2021" = "Jul 2019-Jun 2021"). Reported next to exp_dates, never instead."""
    def squash(s):
        return re.sub(r"\s+", " ", re.sub(r"\s*([-–—])\s*", r"\1", s)).strip()
    return bool(p) and squash(g) == squash(p)


# (check name, entry key, compare(gt value, pred value)); both values are stripped strings
FIELD_RULES: dict[str, tuple[tuple[str, str, Callable[[str, str], bool]], ...]] = {
    "education": (
        ("edu_institution", "institution", _inst_exact),
        ("edu_degree_level", "degree_level", _equal),
        ("edu_stream", "stream", _norm_equal),
    ),
    "experience": (
        ("exp_institution", "institution", _inst_exact),
        ("exp_dates", "dates", _equal),
        ("exp_job_title", "job_title", _norm_equal),
        ("exp_experience_type", "experience_type", _equal),
    ),
    "publications": (
        ("pub_year", "year", _equal),
        ("pub_venue", "venue", _venue_ok),
        ("pub_type", "type", _equal),
    ),
}


def field_errors(section: str, g: dict, p: dict) -> list[str]:
    """Entry keys that are wrong on one matched pair (empty GT fields are not checked).
    The funnel uses this too, so both tools judge fields the same way."""
    bad = []
    for _, key, ok in FIELD_RULES[section]:
        gv, pv = _s(g.get(key)), _s(p.get(key))
        if gv and not ok(gv, pv):
            bad.append(key)
    return bad


def _new_field() -> dict:
    return {"correct": 0, "n": 0, "skipped_gt_empty": 0, "pred_when_gt_empty": 0}


def _check(acc: dict, gt_val: str, pred_has_value: bool, is_correct: Callable[[], bool]) -> None:
    """One field check. GT empty -> skipped (and count a prediction made anyway)."""
    if not _s(gt_val):
        acc["skipped_gt_empty"] += 1
        if pred_has_value:
            acc["pred_when_gt_empty"] += 1
        return
    acc["n"] += 1
    acc["correct"] += bool(is_correct())


# ---- loading ----------------------------------------------------------------------


def empty_prediction() -> dict:
    return {"candidate_name": "", "education": [], "experience": [], "publications": [],
            "stated_totals": {"journal": None, "conference": None, "total": None}}


def load_gt(stem: str) -> dict:
    return read_json(common.gt_dir() / f"{stem}.json")


def load_pred(stem: str) -> dict | None:
    """The prediction, or None when outputs/ has no main JSON for this resume."""
    path = common.outputs_dir() / f"{stem}.json"
    return read_json(path) if path.is_file() else None


# ---- per-resume scoring -----------------------------------------------------------


def score_resume(gt: dict, pred: dict | None) -> dict:
    missing = pred is None
    pr = empty_prediction() if missing else pred
    F = {k: _new_field() for k in FIELD_CHECKS}
    comp = {k: {"used": 0, "skipped_gt_empty": 0} for k in MATCH_COMPONENTS}
    r: dict = {"fields": F, "match_components": comp, "missing_prediction": missing}

    pname, gname = _s(pr.get("candidate_name")), _s(gt.get("candidate_name"))
    _check(F["name"], gname, bool(pname), lambda: pname == gname)
    _check(F["name_norm"], gname, bool(pname), lambda: bool(pname) and norm_name(pname) == norm_name(gname))

    for sec in SECTIONS:
        g, p = gt.get(sec) or [], pr.get(sec) or []
        pairs, _ = match(g, p, sec)
        r[sec] = {"tp": len(pairs), "fp": len(p) - len(pairs), "fn": len(g) - len(pairs),
                  "n_gt": len(g), "n_pred": len(p)}
        for e in g:
            for name in MATCH_COMPONENTS:
                prefix, key = name.split("_", 1)
                if {"edu": "education", "exp": "experience"}[prefix] == sec:
                    comp[name]["used" if _s(e.get(key)) else "skipped_gt_empty"] += 1
        for gi, pi, _ in pairs:
            for check, key, ok in FIELD_RULES[sec]:
                gv, pv = _s(g[gi].get(key)), _s(p[pi].get(key))
                _check(F[check], gv, bool(pv), lambda gv=gv, pv=pv, ok=ok: ok(gv, pv))
            if sec == "experience":  # a reading aid only; field_errors and the funnel use exp_dates
                gv, pv = _s(g[gi].get("dates")), _s(p[pi].get("dates"))
                _check(F["exp_dates_spacing"], gv, bool(pv), lambda gv=gv, pv=pv: _dates_spacing_equal(gv, pv))

    st = gt.get("stated_totals") or {}
    pubs = pr.get("publications") or []
    pc = {"journal": sum(x.get("type") == "journal" for x in pubs),
          "conference": sum(x.get("type") == "conference" for x in pubs),
          "total": len(pubs)}
    gt_pubs = gt.get("publications") or []
    r["pub_counts"] = {
        k: {"stated": st[k], "pred": pc[k],
            "gt_entries": len(gt_pubs) if k == "total" else sum(x.get("type") == k for x in gt_pubs)}
        for k in ("journal", "conference", "total")
        if isinstance(st.get(k), (int, float)) and st.get(k)
    }
    return r


# ---- aggregation -------------------------------------------------------------------


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    rc = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * rc / (p + rc) if p + rc else 0.0
    return p, rc, f


# One count vector per resume, so a bootstrap sample is a sum of rows.
_COLUMNS = [(sec, c) for sec in SECTIONS for c in ("tp", "fp", "fn")] + \
           [(k, c) for k in FIELD_CHECKS for c in ("correct", "n")]


def _vector(r: dict) -> list[int]:
    return [r[a][c] if a in SECTIONS else r["fields"][a][c] for a, c in _COLUMNS]


def _div(a, b):
    """a / b, with 0.0 where b is 0 (works on numbers and arrays)."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    return np.divide(a, b, out=np.zeros_like(a), where=b != 0)


def _metrics(T) -> dict:
    """Metrics from column totals. T has one row per sample (or is one row of totals)."""
    T = np.asarray(T, dtype=float)
    col = {key: T[..., i] for i, key in enumerate(_COLUMNS)}
    out = {}
    for sec in SECTIONS:
        tp, fp, fn = col[(sec, "tp")], col[(sec, "fp")], col[(sec, "fn")]
        p, r = _div(tp, tp + fp), _div(tp, tp + fn)
        out[f"{sec}_P"], out[f"{sec}_R"], out[f"{sec}_F1"] = p, r, _div(2 * p * r, p + r)
    for k in FIELD_CHECKS:
        n = col[(k, "n")]
        out[f"{k}_acc"] = np.where(n > 0, _div(col[(k, "correct")], n), np.nan)
    return out


def aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    C = np.array([_vector(r) for r in rows], dtype=float).reshape(n, len(_COLUMNS))
    point = {k: float(v) for k, v in _metrics(C.sum(axis=0)).items()}
    # resample resumes with replacement; one draw of n indices per bootstrap sample
    rng = np.random.default_rng(BOOTSTRAP["seed"])
    draws = [rng.integers(0, n, n) for _ in range(BOOTSTRAP["n"] if n else 0)]
    samples = _metrics(np.stack([C[idx].sum(axis=0) for idx in draws])) if draws else {}
    lo, hi = 100 * BOOTSTRAP["alpha"] / 2, 100 * (1 - BOOTSTRAP["alpha"] / 2)
    ci = {}
    for k in point:
        v = samples[k][~np.isnan(samples[k])] if k in samples else np.array([])
        ci[k] = (float(np.percentile(v, lo)), float(np.percentile(v, hi))) if v.size else (float("nan"),) * 2
    counts = {sec: {c: sum(r[sec][c] for r in rows) for c in COUNT_KEYS} for sec in SECTIONS}
    fields = {k: {c: sum(r["fields"][k][c] for r in rows) for c in _new_field()} for k in FIELD_CHECKS}
    components = {k: {c: sum(r["match_components"][k][c] for r in rows) for c in ("used", "skipped_gt_empty")}
                  for k in MATCH_COMPONENTS}
    pcs = [r["pub_counts"] for r in rows if r["pub_counts"]]
    pub_counts = None
    if pcs:
        errs = [abs(v["pred"] - v["stated"]) / v["stated"] for pc in pcs for v in pc.values()]
        pub_counts = {"n_resumes": len(pcs), "n_comparisons": len(errs),
                      "mean_abs_rel_err": float(np.mean(errs)),
                      "within_10pct": int(sum(e <= 0.10 for e in errs))}
    return {"n": n, "missing_predictions": sum(r["missing_prediction"] for r in rows),
            "point": point, "ci": ci, "counts": counts, "fields": fields,
            "match_components": components, "pub_counts": pub_counts}


# ---- printing ----------------------------------------------------------------------

FIELD_NOTES = {
    "name": "exact", "name_norm": "no Dr./case/punct.",
    "edu_institution": "normalised exact", "exp_institution": "normalised exact",
    "exp_dates": "exact as printed", "pub_year": "exact as printed",
    "pub_venue": f"partial ratio >= {VENUE_MIN:.0f}", "pub_type": "equal",
    "edu_degree_level": "equal", "edu_stream": "normalised exact",
    "exp_job_title": "normalised exact", "exp_experience_type": "equal",
    "exp_dates_spacing": "as printed, spaces around dashes ignored",
}


def fmt(point, ci, k) -> str:
    v = point.get(k)
    if v is None or v != v:
        return "   n/a"
    lo, hi = ci[k]
    return f"{100 * v:5.1f} [{100 * lo:5.1f}, {100 * hi:5.1f}]"


def print_group(name: str, res: dict, per_resume=None) -> None:
    print(f"\n=== {name}  (n={res['n']} resumes) ===")
    if name == "tuned":
        print("    (tuned-set numbers: the parser was developed on these resumes)")
    if name in common.GUARDED_GROUPS:
        print("    (aggregates only; per-resume results are not shown)")
    if res["missing_predictions"]:
        print(f"    {res['missing_predictions']} resume(s) have no prediction (scored as empty)")
    p, ci = res["point"], res["ci"]
    print(f"  entry matching {'':<7}{'value [95% CI]':>24}")
    for sec in SECTIONS:
        c = res["counts"][sec]
        print(f"  {sec}")
        for m in ("P", "R", "F1"):
            print(f"    {m:<20}{fmt(p, ci, sec + '_' + m):>24}")
        print(f"    gt={c['n_gt']} pred={c['n_pred']} tp={c['tp']} fp={c['fp']} fn={c['fn']}")
    for title, keys in (("field checks", OLD_FIELD_CHECKS), ("further field checks", NEW_FIELD_CHECKS)):
        print(f"\n  {title:<22}{'value [95% CI]':>24}{'n':>6}{'skip':>6}{'pred|GT empty':>15}")
        for k in keys:
            f = res["fields"][k]
            print(f"  {k:<22}{fmt(p, ci, k + '_acc'):>24}{f['n']:>6}{f['skipped_gt_empty']:>6}"
                  f"{f['pred_when_gt_empty']:>15}   ({FIELD_NOTES[k]}{', matched pairs' if k[:4] != 'name' else ''})")
    print("  (skip = GT field empty, excluded from the denominator; pred|GT empty = parser gave a value\n"
          "   anyway; reported only, not in any score)")
    mc = res["match_components"]
    print("  match components with empty GT (left out of the pair score): "
          + ", ".join(f"{k} {v['skipped_gt_empty']}/{v['used'] + v['skipped_gt_empty']}" for k, v in mc.items()))
    pc = res["pub_counts"]
    if pc:
        print(f"  pub count vs stated_totals: {pc['n_resumes']} resumes, {pc['n_comparisons']} counts, "
              f"mean |pred-stated|/stated = {100 * pc['mean_abs_rel_err']:.1f}%, "
              f"within 10%: {pc['within_10pct']}/{pc['n_comparisons']}")
    if per_resume:
        print(f"\n  {'id':<12}{'name':>5}{'edu P/R':>12}{'exp P/R':>12}{'pub P/R':>12}{'dates':>8}{'type':>8}")
        for rid, r in per_resume:
            def pr_(sec):
                pp, rr, _ = prf(r[sec]["tp"], r[sec]["fp"], r[sec]["fn"])
                return f"{pp:.2f}/{rr:.2f}"
            d, t, nm = r["fields"]["exp_dates"], r["fields"]["pub_type"], r["fields"]["name"]
            name_s = "-" if not nm["n"] else ("ok" if nm["correct"] else "X")
            missing = "  (no prediction)" if r["missing_prediction"] else ""
            print(f"  {rid:<12}{name_s:>5}{pr_('education'):>12}{pr_('experience'):>12}{pr_('publications'):>12}"
                  f"{d['correct']:>4}/{d['n']:<3}{t['correct']:>4}/{t['n']:<3}{missing}")
        rows_pc = [(rid, r["pub_counts"]) for rid, r in per_resume if r["pub_counts"]]
        if rows_pc:
            print(f"\n  pub counts   {'id':<12}{'field':<12}{'stated':>7}{'pred':>6}{'gt_entries':>11}")
            for rid, pc_ in rows_pc:
                for k, v in pc_.items():
                    print(f"               {rid:<12}{k:<12}{v['stated']:>7}{v['pred']:>6}{v['gt_entries']:>11}")


# ---- calibration ---------------------------------------------------------------------


def calibrate(groups: dict) -> None:
    print("Best-pair similarity histograms (dev + tuned). For each GT entry: best score")
    print("over predicted entries. Bins of 10; a clear valley suggests the threshold.\n")
    for sec in SECTIONS:
        best: list[float] = []
        for g in ("dev", "tuned"):
            for stem in groups[g]:
                gt = load_gt(stem).get(sec) or []
                pr = (load_pred(stem) or empty_prediction()).get(sec) or []
                if gt and pr:
                    _, S = match(gt, pr, sec)
                    best += list(S.max(axis=1))
                else:
                    best += [0.0] * len(gt)
        h, _ = np.histogram(best, bins=range(0, 111, 10))
        print(f"{sec:<13} (thr={THRESHOLDS[sec]:.0f}, n={len(best)})")
        for i, c in enumerate(h):
            print(f"   {i * 10:>3}-{i * 10 + 9:<3} {c:>4} {'#' * min(int(c), 60)}")


# ---- command line ----------------------------------------------------------------------


def fingerprint() -> dict:
    return {"evaluator_version": EVALUATOR_VERSION,
            "script_sha256": common.sha256_file(Path(__file__)),
            "common_sha256": common.sha256_file(Path(common.__file__)),
            "eval_settings_sha256": common.sha256_settings(settings())}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python eval/evaluate.py", description=__doc__.splitlines()[0])
    ap.add_argument("--calibrate", action="store_true", help="similarity histograms (dev + tuned)")
    ap.add_argument("--json", type=Path, help="also write results here (relative to RESUME_DATA_DIR; outside it is refused)")
    ap.add_argument("--fingerprint", action="store_true", help="print the evaluator hashes and exit")
    ap.add_argument("--validation", action="store_true", help="also score validation (aggregates only, logged)")
    ap.add_argument("--test-frozen", action="store_true", help="also score test_frozen (aggregates only, logged)")
    ap.add_argument("--final", action="store_true", help="score only the final set (aggregates only, logged)")
    ap.add_argument("--allow-rerun", metavar="REASON", help="written reason for a second test_frozen or final run")
    return ap


def run(a: argparse.Namespace) -> int:
    if a.fingerprint:
        print(json.dumps(fingerprint(), indent=1, sort_keys=True))
        return 0
    if a.final:
        if a.calibrate or a.json or a.validation or a.test_frozen:
            raise EvalError("--final runs on its own (no --calibrate, --json, --validation or --test-frozen).")
        return run_final(a)
    if a.allow_rerun is not None and not a.test_frozen:
        raise EvalError("--allow-rerun only makes sense with --test-frozen or --final.")
    json_path = None
    if a.json:
        # a relative path is taken inside the data folder; anything outside it is refused
        json_path = require_inside_data_root(a.json if a.json.is_absolute() else data_root() / a.json)
    groups = common.load_groups()
    if a.calibrate:
        calibrate(groups)
        return 0

    order = [g for g in common.DEV_GROUPS if groups[g]]
    warning = ""
    if a.validation:
        warning = common.log_validation_run(TOOL, Path(__file__), settings())["warning"]
        order.append("validation")
    if a.test_frozen:
        common.require_frozen_allowed(TOOL, a.allow_rerun, rerun_allowed=True)
        common.log_frozen_run(TOOL, Path(__file__), settings(), a.allow_rerun)
        order.append("test_frozen")

    ids = common.ids([s for g in order for s in groups[g]])
    results: dict = {"evaluator": fingerprint()}
    for g in order:
        per = [(s, score_resume(load_gt(s), load_pred(s))) for s in groups[g]]
        res = aggregate([r for _, r in per])
        guarded = g in common.GUARDED_GROUPS
        if g == "validation" and warning:
            print(f"\nWARNING: {warning}")
        print_group(g, res, None if guarded else [(ids[s]["resume_id"], r) for s, r in per])
        if not guarded:
            res["per_resume"] = {ids[s]["eval_id"]: r for s, r in per}
        elif g == "validation" and warning:
            res["warning"] = warning
        results[g] = res
    if json_path:
        common.write_text_inside(json_path, json.dumps(results, indent=1, sort_keys=True, default=float) + "\n")
    return 0


def run_final(a: argparse.Namespace) -> int:
    """The final set only: logged before scoring, a second run refused without --allow-rerun,
    aggregates only, nothing written."""
    groups = common.final_groups()
    common.log_final_run(TOOL, Path(__file__), settings(), a.allow_rerun)
    common.use_final()
    stems = groups[common.FINAL_GROUP]
    res = aggregate([score_resume(load_gt(s), load_pred(s)) for s in stems])
    print_group(common.FINAL_GROUP, res, None)
    return 0


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return run(a)
    except (EvalError, DataDirError) as exc:
        print(f"evaluate: {exc}", file=sys.stderr)
        return 2
    finally:
        common.use_final(False)


if __name__ == "__main__":
    sys.exit(main())
