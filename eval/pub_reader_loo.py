"""Rules against the learned publication-field reader, leave one resume out.

    python eval/pub_reader_loo.py [--groups dev,tuned,dev2] [--save-model]

Development groups only (dev, tuned, dev2); validation and test_frozen are refused. Reads the
saved stage files (cli run --save-stages) and the ground truth; changes nothing in the pipeline.

For each development resume: train the reader (src/resume_parser/learn/pub_reader.py) on the
weak labels of all the other resumes, then read every stage 5 publication entry of this one.
The entry splitting is the rules' own, so all systems see the same entries.

Systems, per field (title, venue, year, type):
  rules        the main JSON values
  learned      the reader's values
  combined@t   the learned value when its confidence is at least t, else the rules value;
               t = 0.8 is fixed in advance, the other values are a sweep chosen on the same data
               (optimistic)

Two views, judged with evaluate.py's own rules (title: score_pub >= 85; venue: partial ratio >=
VENUE_MIN; year and type: equal; a field empty in GT is not checked):
  same entries  the GT-entry pairs the rules match; correct title, venue, year, type, and all
                fields correct (title and no field error)
  re-matched    evaluate.match run again with each system's values: matched entries, P, R, F1,
                and matched entries with all fields correct

Counts and percentages only, per group and combined; never text, headings or file names.
--save-model trains on all the chosen groups and writes RESUME_DATA_DIR/models/pub_reader.pkl.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import evaluate as ev  # noqa: E402

from resume_parser.entities import publications as pub  # noqa: E402
from resume_parser.learn import pub_reader as pr  # noqa: E402
from resume_parser.schema import DataDirError, load_thresholds, read_json, resume_id_for  # noqa: E402

FIXED_T = 0.8
SWEEP = (0.5, 0.6, 0.7, 0.9, 0.95)
FIELDS = ("title", "venue", "year", "type")


# ---------------------------------------------------------------- data


def load_resume(stem: str, group: str, params: dict) -> dict:
    """{"group", "gt", "entries": [{"lines", "rules"}]} for one development resume."""
    gt = ev.load_gt(stem).get("publications") or []
    pred = (ev.load_pred(stem) or {}).get("publications") or []
    sdir = common.stages_dir() / resume_id_for(stem)
    entries = []
    if (sdir / "stage4_canonical.json").is_file() and (sdir / "stage5_entities.json").is_file():
        document = read_json(sdir / "stage4_canonical.json")["data"]
        sides = (read_json(sdir / "stage5_entities.json")["data"].get("publications") or {}).get("sides") or []
        by_id = {}
        for line in pub.publications_payload(document, params)["lines"]:
            by_id.setdefault(line["id"], line)
        if len(sides) != len(pred):
            raise common.EvalError(f"{resume_id_for(stem)}: stage 5 and main JSON disagree on publication count.")
        for side, rules in zip(sides, pred):
            entries.append({"lines": [by_id[i] for i in side["lines"] if i in by_id], "rules": rules})
    return {"group": group, "gt": gt, "entries": entries}


# ---------------------------------------------------------------- examples


def prepare(resume: dict, params: dict) -> dict:
    """Tokens, the rules' GT pairs, weak-label examples and skip reasons for one resume."""
    toks = [pr.tokenize(e) for e in resume["entries"]]
    pairs, _ = ev.match(resume["gt"], [e["rules"] for e in resume["entries"]], "publications")
    token_X, token_y, type_X, type_y, reasons = [], [], [], [], Counter()
    for gi, pi, _score in pairs:
        g, entry, tok = resume["gt"][gi], resume["entries"][pi], toks[pi]
        if g.get("type") in pr.TYPES:
            type_X.append(pr.type_features(entry, tok, params))
            type_y.append(g["type"])
        labels, reason = pr.weak_labels(tok, g)
        reasons[reason] += 1
        if labels:
            token_X.extend(pr.token_features(entry, tok, params))
            token_y.extend(labels)
    return {"toks": toks, "pairs": pairs, "token_X": token_X, "token_y": token_y, "type_X": type_X,
            "type_y": type_y, "reasons": reasons}


def train_on(prepared: list[dict]) -> dict:
    return pr.train([x for p in prepared for x in p["token_X"]], [y for p in prepared for y in p["token_y"]],
                    [x for p in prepared for x in p["type_X"]], [y for p in prepared for y in p["type_y"]])


# ---------------------------------------------------------------- scoring


def _field_ok(field: str, g: dict, p: dict) -> bool | None:
    """None when GT has no value (not checked)."""
    if field == "title":
        return ev.score_pub(g, p) >= ev.THRESHOLDS["publications"]
    rule = next(r for r in ev.FIELD_RULES["publications"] if r[1] == field)
    gv, pv = ev._s(g.get(field)), ev._s(p.get(field))
    return rule[2](gv, pv) if gv else None


def score(gt: list[dict], rule_pairs: list, values: list[dict]) -> Counter:
    c = Counter()
    for gi, pi, _ in rule_pairs:
        g, p = gt[gi], values[pi]
        c["pairs"] += 1
        for f in FIELDS:
            ok = _field_ok(f, g, p)
            if ok is not None:
                c[f"n_{f}"] += 1
                c[f"ok_{f}"] += ok
        c["all_ok"] += bool(_field_ok("title", g, p)) and not ev.field_errors("publications", g, p)
    pairs, _ = ev.match(gt, values, "publications")
    c["gt"] += len(gt)
    c["pred"] += len(values)
    c["matched"] += len(pairs)
    c["matched_fields_ok"] += sum(1 for gi, pi, _ in pairs if not ev.field_errors("publications", gt[gi], values[pi]))
    return c


def systems(thresholds=(FIXED_T, *SWEEP)) -> list[str]:
    return ["rules", "learned"] + [f"combined@{t:g}" for t in thresholds]


def run_loo(resumes: list[dict], params: dict, thresholds=(FIXED_T, *SWEEP)) -> dict:
    """{"counts": {(group, system): Counter}, "labels": {group: Counter}}. Pure: no files written."""
    prepared = [prepare(r, params) for r in resumes]
    counts: dict = {}
    labels: dict = {}
    for k, resume in enumerate(resumes):
        g = resume["group"]
        lab = labels.setdefault(g, Counter())
        lab["gt"] += len(resume["gt"])
        lab["entries"] += len(resume["entries"])
        lab["matched"] += len(prepared[k]["pairs"])
        lab.update({f"label {r}": n for r, n in prepared[k]["reasons"].items()})
        lab["type examples"] += len(prepared[k]["type_y"])
        if not resume["entries"]:
            per = {s: [] for s in systems(thresholds)}
        else:
            models = train_on([p for j, p in enumerate(prepared) if j != k])
            rules = [e["rules"] for e in resume["entries"]]
            learned = [pr.read_entry(models, e, params) for e in resume["entries"]]
            per = {"rules": rules, "learned": [{f: x[f] for f in FIELDS} for x in learned]}
            for t in thresholds:
                per[f"combined@{t:g}"] = [pr.combine(r, x, t) for r, x in zip(rules, learned)]
        for name, values in per.items():
            c = score(resume["gt"], prepared[k]["pairs"], values) if values else Counter(gt=len(resume["gt"]))
            counts.setdefault((g, name), Counter()).update(c)
            counts.setdefault(("combined", name), Counter()).update(c)
    return {"counts": counts, "labels": labels}


# ---------------------------------------------------------------- printing


def _pct(a, b) -> str:
    return f"{100 * a / b:5.1f}" if b else "    -"


def _prf(c: Counter) -> tuple[str, str, str]:
    p = c["matched"] / c["pred"] if c["pred"] else 0.0
    r = c["matched"] / c["gt"] if c["gt"] else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return f"{100 * p:5.1f}", f"{100 * r:5.1f}", f"{100 * f:5.1f}"


def print_report(result: dict, groups: list[str], thresholds=(FIXED_T, *SWEEP)) -> None:
    print("=== WEAK LABELS (GT publications matched by the rules) ===")
    keys = sorted({k for c in result["labels"].values() for k in c})
    print("  " + f"{'':<28}" + "".join(f"{g:>10}" for g in groups))
    for key in ["gt", "entries", "matched"] + [k for k in keys if k.startswith("label")] + ["type examples"]:
        print("  " + f"{key:<28}" + "".join(f"{result['labels'].get(g, Counter())[key]:>10}" for g in groups))
    for g in groups + ["combined"]:
        print(f"\n=== {g}: same entries (the rules' GT pairs) | re-matched ===")
        print(f"  {'system':<16}{'pairs':>6}{'title':>12}{'venue':>12}{'year':>12}{'type':>12}{'all ok':>12}"
              f" | {'matched':>8}{'P':>7}{'R':>7}{'F1':>7}{'fields ok':>11}")
        for name in systems(thresholds):
            c = result["counts"].get((g, name), Counter())
            cells = [f"{c['ok_' + f]:>4} {_pct(c['ok_' + f], c['n_' + f])}" for f in FIELDS]
            p, r, f1 = _prf(c)
            print(f"  {name:<16}{c['pairs']:>6}" + "".join(f"{x:>12}" for x in cells)
                  + f"{c['all_ok']:>5} {_pct(c['all_ok'], c['pairs'])}"
                  + f" | {c['matched']:>8}{p:>7}{r:>7}{f1:>7}{c['matched_fields_ok']:>6} {_pct(c['matched_fields_ok'], c['matched'])}")
        c = result["counts"].get((g, "rules"), Counter())
        print(f"  checked per field (GT not empty): title {c['n_title']}, venue {c['n_venue']}, year {c['n_year']},"
              f" type {c['n_type']}; GT {c['gt']}, predictions {c['pred']}")
    print(f"\ncombined@{FIXED_T:g} was fixed in advance; the other thresholds are chosen on the same data (optimistic).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--groups", default="dev,tuned,dev2")
    ap.add_argument("--save-model", action="store_true", help="train on all chosen groups and save under models/")
    a = ap.parse_args(argv)
    groups = [g.strip() for g in a.groups.split(",") if g.strip()]
    try:
        bad = [g for g in groups if g not in common.DEV_GROUPS]
        if bad:
            raise common.EvalError(f"development groups only ({', '.join(common.DEV_GROUPS)}); refused: {', '.join(bad)}")
        params = load_thresholds()["stage5_entities"]
        split = common.load_groups()
        resumes = [load_resume(s, g, params) for g in groups for s in sorted(split[g])]
        result = run_loo(resumes, params)
        print_report(result, groups)
        if a.save_model:
            path = pr.save_model(train_on([prepare(r, params) for r in resumes]))
            print(f"\nmodel saved under RESUME_DATA_DIR/{path.parent.name}/{path.name}")
    except (common.EvalError, DataDirError) as exc:
        print(f"pub_reader_loo: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
