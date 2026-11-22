"""Pick section_score_threshold in config/thresholds.json from dev+tuned ground truth (design 7).

    python eval/calibrate_thresholds.py            report only
    python eval/calibrate_thresholds.py --write    also write the chosen thresholds

Needs a run first: python -m resume_parser.cli run --groups dev,tuned --save-stages, then
python eval/funnel.py (for the "any item lost" table). test_frozen is never read.

For each critical section (name = the personal unit, education, experience, publications) the
label-free section score from the sidecar is compared with real correctness:
  real loss (used for the choice): recall below 0.8 or precision below 0.8 (an empty GT with
      predictions counts as precision 0; an empty GT and no predictions is no loss); for the
      name, name_norm wrong or empty
  for reading only: F1 below 1 (any unmatched GT entry or prediction), and any item lost at
      any funnel step (field errors included)
A section is flagged when its score is below the threshold, or when it is missing (score None).
Per candidate threshold (the distinct observed scores, plus 0, 1 and "flag all", which is shown but
never chosen as it sends every resume to review): share of
resumes flagged, catch rate (flagged among resumes with a real loss) and false alarms (share of
flagged resumes with no real loss). The choice: the smallest flagged share with a catch rate of
at least 90%; ties go to the lower threshold. Stability: leave one resume out, choose again on
the others, and count how often the same threshold comes back.

Only section_score_threshold is written. accept_threshold_by_section (which picks the stage 5
methods) is never changed, so stage 5 output stays the same. 25 resumes is a small sample.
Prints counts and thresholds only, never resume text or file names.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import common
import evaluate as ev
from resume_parser.schema import read_json, resume_id_for

ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS = ROOT / "config" / "thresholds.json"
SECTIONS = ("personal", "education", "experience", "publications")
LABEL = {"personal": "name", "education": "education", "experience": "experience", "publications": "publications"}
FUNNEL_SECTION = {"personal": "name", "education": "education", "experience": "experience",
                  "publications": "publications"}
TARGET_CATCH = 0.9
FLAG_ALL = 1.01  # a threshold above every possible score flags every resume


def _pr(tp: int, fp: int, fn: int) -> tuple[float, float]:
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return precision, recall


def collect(groups=("dev", "tuned")) -> list[dict]:
    """One row per resume: section scores and the three loss labels per section."""
    all_groups = common.load_groups()
    stems = [s for g in groups for s in all_groups[g]]
    eids = common.ids(stems)
    lost_items: dict[tuple[str, str], int] = {}
    for g in groups:
        path = common.reports_dir() / "funnel" / f"items_{g}.jsonl"
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                it = json.loads(line)
                if it["lost_at"] != "ok":
                    key = (it["eval_id"], it["section"])
                    lost_items[key] = lost_items.get(key, 0) + 1
    rows = []
    for stem in stems:
        meta_path = common.outputs_dir() / f"{stem}.meta.json"
        if not meta_path.is_file():
            continue
        scores = read_json(meta_path)["section_scores"]
        r = ev.score_resume(ev.load_gt(stem), ev.load_pred(stem))
        row = {"resume_id": resume_id_for(stem), "scores": scores, "loss": {}, "loss_f1": {}, "loss_any": {}}
        for sec in SECTIONS:
            if sec == "personal":
                f = r["fields"]["name_norm"]
                bad = f["n"] > 0 and f["correct"] < f["n"]
                row["loss"][sec] = row["loss_f1"][sec] = bad
            else:
                c = r[sec]
                p, rec = _pr(c["tp"], c["fp"], c["fn"])
                row["loss"][sec] = p < 0.8 or rec < 0.8
                row["loss_f1"][sec] = c["fp"] > 0 or c["fn"] > 0
            row["loss_any"][sec] = lost_items.get((eids[stem]["eval_id"], FUNNEL_SECTION[sec]), 0) > 0
        rows.append(row)
    return rows


def flagged(score, t: float) -> bool:
    return score is None or score < t


def table(rows: list[dict], sec: str, t: float, label: str = "loss") -> dict:
    n = len(rows)
    flags = [flagged(r["scores"][sec], t) for r in rows]
    losses = [r[label][sec] for r in rows]
    n_flag, n_loss = sum(flags), sum(losses)
    caught = sum(f and l for f, l in zip(flags, losses))
    false_alarm = sum(f and not l for f, l in zip(flags, losses))
    return {"threshold": t, "flagged": n_flag, "flagged_share": n_flag / n if n else 0.0,
            "losses": n_loss, "catch": caught / n_loss if n_loss else None,
            "false_alarm": false_alarm / n_flag if n_flag else None}


def candidates(rows: list[dict], sec: str) -> list[float]:
    seen = {r["scores"][sec] for r in rows if r["scores"][sec] is not None}
    return sorted(seen | {0.0, 1.0, FLAG_ALL})


def choose(rows: list[dict], sec: str, label: str = "loss") -> tuple[float, bool]:
    """(threshold, reached the target catch rate)."""
    # "flag all" is shown in the tables but never chosen: it would send every resume to review
    tabs = [table(rows, sec, t, label) for t in candidates(rows, sec) if t != FLAG_ALL]
    if not any(r[label][sec] for r in rows):
        return 0.0, True  # no real loss: flag only missing sections
    good = [x for x in tabs if x["catch"] is not None and x["catch"] >= TARGET_CATCH]
    if good:
        best = min(good, key=lambda x: (x["flagged_share"], x["threshold"]))
        return best["threshold"], True
    best = max(tabs, key=lambda x: (x["catch"] or 0.0, -x["flagged_share"], -x["threshold"]))
    return best["threshold"], False


def _fmt(v) -> str:
    return "  n/a" if v is None else f"{100 * v:5.1f}"


def print_section(rows: list[dict], sec: str, label: str, title: str) -> None:
    print(f"\n  {LABEL[sec]} - {title}")
    print(f"    {'threshold':>9}{'flagged':>9}{'flag %':>8}{'catch %':>9}{'false alarm %':>15}")
    for t in candidates(rows, sec):
        x = table(rows, sec, t, label)
        shown = "flag all" if t == FLAG_ALL else f"{t:.4f}"
        print(f"    {shown:>9}{x['flagged']:>9}{_fmt(x['flagged_share']):>8}{_fmt(x['catch']):>9}"
              f"{_fmt(x['false_alarm']):>15}")
    print(f"    resumes with a real loss: {sum(r[label][sec] for r in rows)} of {len(rows)}")


def stability(rows: list[dict], sec: str, chosen: float) -> dict:
    picks = [choose(rows[:i] + rows[i + 1:], sec)[0] for i in range(len(rows))]
    return {"same": sum(p == chosen for p in picks), "runs": len(picks), "min": min(picks), "max": max(picks)}


def combined(rows: list[dict], chosen: dict) -> dict:
    flags = [any(flagged(r["scores"][s], chosen[s]) for s in SECTIONS) for r in rows]
    losses = [any(r["loss"][s] for s in SECTIONS) for r in rows]
    n_flag, n_loss = sum(flags), sum(losses)
    caught = sum(f and l for f, l in zip(flags, losses))
    return {"flagged": n_flag, "flagged_share": n_flag / len(rows), "losses": n_loss,
            "catch": caught / n_loss if n_loss else None,
            "false_alarm": sum(f and not l for f, l in zip(flags, losses)) / n_flag if n_flag else None}


def write_thresholds(chosen: dict, reached: dict) -> None:
    cfg = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    cfg["section_score_threshold"] = {s: (1.0 if chosen[s] == FLAG_ALL else round(chosen[s], 4)) for s in SECTIONS}
    if chosen and any(chosen[s] == FLAG_ALL for s in SECTIONS):
        cfg["section_score_threshold"]["_note"] = "a value of 1.0 here came from 'flag all'"
    cfg["section_score_threshold"]["_comment"] = (
        "Stage 6: a section scoring below this goes to review. Chosen by eval/calibrate_thresholds.py on "
        "dev+tuned (25 resumes, a small sample): the smallest flagged share with a catch rate of at least "
        "90%, or the best catch rate where no threshold reaches 90% ("
        + (", ".join(LABEL[s] for s in SECTIONS if not reached[s]) or "none") + " on this run); a real loss is "
        "recall or precision below 0.8 (name: name_norm wrong or empty).")
    THRESHOLDS.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python eval/calibrate_thresholds.py", description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="write the chosen section_score_threshold values")
    a = ap.parse_args(argv)
    rows = collect()
    if not rows:
        print("calibrate_thresholds: no sidecars found; run the pipeline on dev and tuned first")
        return 2
    print(f"=== THRESHOLD CALIBRATION: dev+tuned, {len(rows)} resumes (a small sample) ===")
    chosen, reached_all = {}, {}
    for sec in SECTIONS:
        print_section(rows, sec, "loss", "real loss: recall or precision below 0.8 (name: name_norm wrong)")
        t, reached = choose(rows, sec)
        chosen[sec], reached_all[sec] = t, reached
        st = stability(rows, sec, t)
        shown = "flag all" if t == FLAG_ALL else f"{t:.4f}"
        note = "" if reached else f"  (no threshold reaches {100 * TARGET_CATCH:.0f}% catch; best shown)"
        print(f"    chosen: {shown}{note}")
        print(f"    leave-one-out: same threshold in {st['same']} of {st['runs']} runs; range {st['min']:.4f} "
              f"to {st['max']:.4f}")
    for label, title in (("loss_f1", "for reading: F1 below 1"),
                         ("loss_any", "for reading: any item lost at any funnel step, field errors included")):
        print(f"\n=== {title.upper()} ===")
        for sec in SECTIONS:
            print_section(rows, sec, label, title)
    c = combined(rows, chosen)
    print("\n=== RESUME LEVEL (flagged by any section's score, against a real loss in any section) ===")
    print(f"  flagged {c['flagged']} of {len(rows)} ({_fmt(c['flagged_share'])}%), resumes with a real loss "
          f"{c['losses']}, catch {_fmt(c['catch'])}%, false alarms {_fmt(c['false_alarm'])}%")
    print("  (other review reasons, such as whole_section_one_entry or a name fallback, come on top)")
    if a.write:
        write_thresholds(chosen, reached_all)
        print("\nwritten: section_score_threshold in config/thresholds.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
