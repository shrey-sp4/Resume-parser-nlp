"""Stage-by-stage loss funnel: where is each ground-truth item lost? (docs/design.md, section 6)

    python eval/funnel.py                dev + tuned (per-item files under reports/funnel/)
    python eval/funnel.py --test-frozen  ALSO test_frozen, aggregates only; ONCE ever
    python eval/funnel.py --lost-items   list the items lost at S1 (dev + tuned only):
                                         resume ID, section, index, cause, recoverable_by
    python eval/funnel.py --lost-items --stage S4
                                         the same for S4 (or S5), with the method instead

Read-only: never runs or changes the parser, never edits ground truth. Needs the stage files
from "python -m resume_parser.cli run --save-stages" (RESUME_DATA_DIR/reports/stages/<resume_id>/)
and the predictions in RESUME_DATA_DIR/outputs/. Entry matching and field checks are the
evaluator's (eval/evaluate.py), so the two tools always agree.

GT items: name (candidate_name), education (degree + institution), experience
(job_title + institution), publications (title). Empty GT fields are skipped; an item "is found"
at a stage only if ALL its non-empty key fields are found there. Items with no key field text
at all are left out.

Funnel (each item stops at the FIRST stage it fails):
  S1 text     key fields fuzzy-found in the stage 1 lines (>= 85; >= 95 is also shown)
  S4 section  found in the right canonical section of the stage 4 Canonical Document
              (name -> personal); else "wrong section: X" or "in text but in no section"
  S5 entry    a predicted entry matches (evaluator rules, one-to-one, same thresholds)
  S5 fields   every checkable field on the matched pair is correct (evaluator checks)
Items lost at S1 get one cause, the first that fits:
  garbled       the page that matches best has more than garbled_max garbled characters
  glued words   found once all spaces are removed (words glued together or split apart)
  table cell    every word is there, and all of them are in table-cell lines
  wrong order   every word is there, but not together (reading order, columns)
  missing text  some words are not in the stage 1 lines at all
  other         anything else (for example a near match: a GT typo or a paraphrase)
and "recoverable_by": the stage 1 methods whose own lines do contain it (stage1_methods.json).

Completeness (matched items, separate from the funnel): is the prediction a TRUNCATED version
of the GT string? If so, where was it cut: not whole in the stage 1 lines (S1), whole in the
lines but not in the section (S4), or whole in the section but cut in the entry (S5).

Each lost item also gets the method that handled it, from the stage records: the stage 1
method of the page that matches it best (S1), the stage 3 and stage 4 methods (S4), or the
stage 5 method of its section (S5).

False positives (unmatched predictions) get the FIRST matching cause, in this order:
school-level entry; table header or skills line; non-paper publication; duplicate of an
already-matched GT item; wrapped-line fragment; bleed from another section; other. The rules
are defined once below (from category definitions, not fitted per resume) and are heuristics.

Check validity (dev + tuned only): for every label-free check that fired (each stage's
reason codes, and each stage's WARN/FAIL status) the table counts, per group, the resumes it
fired on, how many of those had a real loss at the funnel step that stage feeds (stages 0-1:
S1; stages 2-4: S4; stage 5: S5; for missing_target_section:<name> only that section's S4
losses), and how many resumes with such a loss it did not fire on. This shows whether a check
is worth trusting before its threshold is calibrated.

Privacy: console output shows counts only; --lost-items shows resume IDs, never text. Per-item and false-positive files are keyed by the
salted eval ID and hold no resume text. test_frozen: aggregates only, nothing per item.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import evaluate as ev  # noqa: E402
from common import EvalError  # noqa: E402

from resume_parser.extract.lines import garbled_rate  # noqa: E402
from resume_parser.schema import DataDirError, load_thresholds, read_json, resume_id_for  # noqa: E402

TOOL = "funnel"
STAGE_FILES = {
    "stage1": "stage1_lines.json",
    "stage3": "stage3_sections.json",
    "stage4": "stage4_canonical.json",
    "stage5": "stage5_entities.json",
}
METHODS_FILE = "stage1_methods.json"
S1_CAUSES = ("garbled", "glued words", "table cell", "wrong order", "missing text", "other")

FOUND_LO, FOUND_HI = 85.0, 95.0
SECTIONS = ("name", "education", "experience", "publications")
TARGET = {"name": {"personal"}, "education": {"education"},
          "experience": {"experience"}, "publications": {"publications"}}
STAGE5_UNIT = {"name": "personal", "education": "education",
               "experience": "experience", "publications": "publications"}
KEY_FIELDS = {"education": ("degree", "institution"), "experience": ("job_title", "institution"),
              "publications": ("title",)}
MAIN_FIELD = {"name": "candidate_name", "education": "institution", "experience": "institution",
              "publications": "title"}
_Q = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-",
                    "—": "-", " ": " ", "&": " and "})


def settings() -> dict:
    return {"FOUND_LO": FOUND_LO, "FOUND_HI": FOUND_HI, "evaluator": ev.settings()}


# ---- normalisation / fuzzy search -------------------------------------------------


def snorm(s) -> str:
    """Search form: no case, accents, quote/dash forms or punctuation."""
    s = str(s or "").translate(_Q)
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def find(needle_n: str, hay_n: str) -> float | None:
    if not needle_n:
        return None
    if not hay_n:
        return 0.0
    if needle_n in hay_n:
        return 100.0
    return fuzz.partial_ratio(needle_n, hay_n)


def find_all(fields_n: list, hay_n: str) -> float | None:
    scores = [find(f, hay_n) for f in fields_n if f]
    return min(scores) if scores else None


# ---- stage files ----------------------------------------------------------------------


def _line_text(lines) -> str:
    return "\n".join(str(line.get("text", "")) for line in lines or [] if isinstance(line, dict))


def load_stages(stem: str) -> dict:
    """The saved stage files of one resume: stage records, page texts and section texts."""
    folder = common.stages_dir() / resume_id_for(stem)
    rec = {s: read_json(folder / n) if (folder / n).is_file() else None for s, n in STAGE_FILES.items()}
    pages: dict[str, str] = {}
    table_words: set[str] = set()
    data1 = (rec["stage1"] or {}).get("data") or {}
    for page_id in sorted(data1):
        lines = (data1[page_id] or {}).get("lines") or []
        pages[page_id] = _line_text(lines)
        table_words |= set(snorm(_line_text([x for x in lines if x.get("in_table")])).split())
    by_method: dict[str, list[str]] = defaultdict(list)
    methods_rec = read_json(folder / METHODS_FILE) if (folder / METHODS_FILE).is_file() else {}
    for page_id in sorted(methods_rec.get("methods") or {}):
        for method, data in sorted((methods_rec["methods"][page_id] or {}).items()):
            by_method[method].append(_line_text((data or {}).get("lines")))
    sections: dict[str, list[str]] = defaultdict(list)
    blocks: list[tuple[str, str]] = []
    data4 = (rec["stage4"] or {}).get("data") or {}
    for sec in data4.get("sections") or []:
        sections[str(sec.get("name") or "unassigned")].append(_line_text(sec.get("lines")))
        blocks.append((str(sec.get("block_id") or ""), snorm(_line_text(sec.get("lines")))))
    if data4.get("unassigned"):
        sections["unassigned"].append(_line_text(data4["unassigned"]))
    return {
        "records": rec,
        "complete": rec["stage1"] is not None and rec["stage4"] is not None,
        "pages_raw": pages,
        "pages_n": {p: snorm(t) for p, t in pages.items()},
        "text_n": snorm("\n".join(pages.values())),
        "table_words": table_words,
        "methods_n": {m: snorm("\n".join(v)) for m, v in sorted(by_method.items())},
        "secs_n": {k: snorm("\n".join(v)) for k, v in sorted(sections.items())},
        "blocks_n": blocks,
    }


def _method(rec: dict | None, unit: str | None = None) -> str:
    if rec is None:
        return "none"
    if unit is not None:
        return (rec.get("per_unit", {}).get(unit) or {}).get("method_used") or "none"
    return rec.get("method_used") or "none"


def method_for(sec: str, lost_at: str, fields_n: list, st: dict) -> str | None:
    rec = st["records"]
    if lost_at == "S1 text":
        scores = {p: find_all(fields_n, t) or 0.0 for p, t in st["pages_n"].items()}
        if not scores:
            return "stage1 none"
        page = max(scores, key=lambda p: (scores[p], -list(scores).index(p)))
        return f"stage1 {_method(rec['stage1'], page)}"
    if lost_at == "S4 section":
        # the block that holds the item best (stage 4 chooses its method per block)
        scores = [(find_all(fields_n, text) or 0.0, -i, block) for i, (block, text) in enumerate(st["blocks_n"])]
        block = max(scores)[2] if scores else None
        stage4 = _method(rec["stage4"], block) if block and (rec["stage4"] or {}).get("per_unit") else \
            _method(rec["stage4"])
        return f"stage3 {_method(rec['stage3'])} / stage4 {stage4}"
    if lost_at in ("S5 entry", "S5 fields"):
        return f"stage5 {_method(rec['stage5'], STAGE5_UNIT[sec])}"
    return None


# ---- S1 loss causes ----------------------------------------------------------------------


def _garbled_max() -> float:
    return float(load_thresholds()["stage1_checks"]["garbled_max"])


def s1_cause(fields_n: list, st: dict) -> str:
    """Why an item is not in the stage 1 lines (the categories in the module docstring)."""
    scores = {p: find_all(fields_n, t) or 0.0 for p, t in st["pages_n"].items()}
    if scores:
        best = max(scores, key=lambda p: (scores[p], -list(scores).index(p)))
        if garbled_rate(st["pages_raw"][best]) > _garbled_max():
            return "garbled"
    squashed = st["text_n"].replace(" ", "")
    if squashed and all((find(f.replace(" ", ""), squashed) or 0.0) >= FOUND_LO for f in fields_n):
        return "glued words"
    words = set(st["text_n"].split())
    needed = {w for f in fields_n for w in f.split()}
    if needed <= words:
        return "table cell" if needed <= st["table_words"] else "wrong order"
    s1 = find_all(fields_n, st["text_n"])
    return "other" if s1 is not None and s1 >= 75 else "missing text"


# ---- completeness ---------------------------------------------------------------------


def pred_main(sec: str, p) -> str:
    return ev._s(p.get(MAIN_FIELD[sec]))


def completeness(sec: str, g_main: str, p_main: str) -> str:
    """complete / truncated / over_long / differs (main string, search-normalised)."""
    gn, pn = snorm(g_main), snorm(p_main)
    if not gn:
        return "n/a"
    if sec == "publications":
        if gn in pn:
            return "complete"
        toks = gn.split()
        k = 0  # longest GT-title prefix present in the prediction
        while k < len(toks) and " ".join(toks[:k + 1]) in pn:
            k += 1
        return "truncated" if 0.5 <= k / len(toks) < 1 else "differs"
    first = snorm(re.split(r"[,(;]| - ", g_main, maxsplit=1)[0])
    if pn in (gn, first):
        return "complete"
    if pn and pn in gn and len(pn) < len(gn):
        return "truncated"
    if gn in pn or (first and first in pn):
        return "over_long"
    return "differs"


# ---- false-positive causes ----------------------------------------------------------------
SCHOOL = re.compile(r"\b(?:10th|12th|xth|xiith|ssc|hsc|s s c|h s c|secondary|sr sec|sen sec|matric\w*|"
                    r"intermediate|10 2|class x|class xii|cbse|icse|high school|higher school)\b")
HEADER_WORDS = {"board", "university", "year", "percentage", "cgpa", "gpa", "passing", "institution",
                "institute", "degree", "course", "examination", "division", "grade", "school", "college",
                "level", "marks", "specialization", "month"}
SKILLS = re.compile(r"\b(?:tools|languages|skills|software|programming)\b")
NON_PAPER = re.compile(r"\b(?:presented|presentation|poster|oral|talk|seminar|webinar|newspaper|magazine|"
                       r"souvenir|newsletter|article titled|blog|attended|participated)\b")
CITATION = re.compile(r"\b(?:vol|volume|pp|pages|doi|issn|isbn)\b|\d+\s*\(\s*\d+\s*\)", re.IGNORECASE)
VENUE_WORDS = re.compile(r"\b(?:journal|conference|proceedings|symposium|international|national|transactions|"
                         r"letters|review|press|publisher|springer|elsevier|ieee|wiley)\b")
BLEED_WORDS = re.compile(r"\b(?:developed|project|workshop|attended|experience|responsib\w+|teaching|"
                         r"languages?|hobbies|interests?)\b")
FP_CAUSES = ("school-level entry", "table header or skills line", "non-paper publication",
             "duplicate of an already-matched GT item", "wrapped-line fragment", "bleed from another section",
             "other: garbled version of a missed GT entry", "other: citation-like, not in GT", "other")


def fp_text(sec: str, p) -> str:
    keys = ("title", "venue", "year") if sec == "publications" else \
        ("degree", "stream", "job_title", "institution", "dates")
    return " ".join(ev._s(p.get(k)) for k in keys)


def classify_fp(sec: str, p, gt_all: dict, matched_gt: set, matched_pred_texts: list) -> str:
    t = snorm(fp_text(sec, p))
    raw = fp_text(sec, p)
    if sec == "education" and SCHOOL.search(snorm(ev._s(p.get("degree")) + " " + ev._s(p.get("stream")))):
        return "school-level entry"
    if sec in ("education", "experience"):
        words = set(t.split())
        if (len(words & HEADER_WORDS) >= 2 and not re.search(r"\b(?:19|20)\d{2}\b", t)) \
                or (SKILLS.search(t) and len(t) < 80) or (len(t) < 25 and not re.search(r"\d", t)):
            return "table header or skills line"
    if sec == "publications" and NON_PAPER.search(t) and not CITATION.search(raw):
        return "non-paper publication"
    # wrapped-line split / duplicate: would match a GT entry another prediction already took,
    # or is a short fragment contained in a matched prediction's text
    g = gt_all[sec]
    for gi, ge in enumerate(g):
        if gi in matched_gt and ev.SCORERS[sec](ge, p) >= ev.THRESHOLDS[sec]:
            return "duplicate of an already-matched GT item"
    if len(t) < 60 and t and (sec == "publications" or any(t in m for m in matched_pred_texts)):
        return "wrapped-line fragment"
    # bleed: contains a GT item of ANOTHER section, or bullet/project/activity wording
    for other in ("education", "experience", "publications"):
        if other == sec:
            continue
        for ge in gt_all[other]:
            key = snorm(" ".join(ev._s(ge.get(k)) for k in KEY_FIELDS[other][-1:]))
            if len(key) >= 8 and (find(key, t) or 0.0) >= 90:
                return "bleed from another section"
    if sec in ("education", "experience") and BLEED_WORDS.search(t):
        return "bleed from another section"
    # "other", split into two recognisable sub-causes
    if sec in ("education", "experience"):
        comp = "degree" if sec == "education" else "job_title"
        pc = snorm(p.get(comp))
        for gi, ge in enumerate(g):
            if gi not in matched_gt and pc and snorm(ge.get(comp)) and \
                    fuzz.token_set_ratio(snorm(ge.get(comp)), pc) >= 80:
                return "other: garbled version of a missed GT entry"
    if sec == "publications" and (CITATION.search(raw) or
                                  (VENUE_WORDS.search(t) and re.search(r"\b(?:19|20)\d{2}\b", t))):
        return "other: citation-like, not in GT"
    return "other"


# ---- per-resume diagnosis ---------------------------------------------------------------


def diagnose_resume(gt: dict, pred: dict | None, st: dict, eid: str):
    pr = pred if pred is not None else ev.empty_prediction()
    text_n, secs_n = st["text_n"], st["secs_n"]
    gt_all = {s: gt.get(s) or [] for s in ("education", "experience", "publications")}
    items, fps = [], []

    def trace(sec, idx, fields, main_val, matched_pred, field_bad):
        fn = [snorm(v) for v in fields if ev._s(v)]
        fn = [f for f in fn if f]
        if not fn:
            return  # no key field text to look for
        it = {"eval_id": eid, "section": sec, "idx": idx, "gt_typo_band": False}
        s1 = find_all(fn, text_n)
        it["s1_score"] = s1
        it["s1_ge95"], it["s1_ge85"] = s1 is not None and s1 >= FOUND_HI, s1 is not None and s1 >= FOUND_LO
        sec_scores = {k: find_all(fn, v) or 0.0 for k, v in secs_n.items()}
        best = max(sec_scores, key=sec_scores.get) if sec_scores else None
        tgt = max((sec_scores.get(k, 0.0) for k in TARGET[sec]), default=0.0)
        it["best_section"], it["best_section_score"], it["target_score"] = best, sec_scores.get(best), tgt
        it["matched"], it["field_bad"] = matched_pred is not None, field_bad
        it["recoverable_by"] = []
        if not it["s1_ge85"]:
            it["lost_at"] = "S1 text"
            it["gt_typo_band"] = s1 is not None and s1 >= 75
            it["cause"] = s1_cause(fn, st)
            it["recoverable_by"] = [m for m, t in st["methods_n"].items() if (find_all(fn, t) or 0.0) >= FOUND_LO]
        elif tgt < FOUND_LO:
            it["lost_at"] = "S4 section"
            it["cause"] = (f"wrong section: {best}" if best and sec_scores[best] >= FOUND_LO
                           else "in text but in no section")
        elif matched_pred is None:
            it["lost_at"], it["cause"] = "S5 entry", "no matching predicted entry"
        elif field_bad:
            it["lost_at"], it["cause"] = "S5 fields", "wrong: " + ",".join(field_bad)
        else:
            it["lost_at"], it["cause"] = "ok", ""
        it["method"] = method_for(sec, it["lost_at"], fn, st)
        it["matched_despite_earlier_loss"] = matched_pred is not None and it["lost_at"] in ("S1 text", "S4 section")
        it["completeness"], it["cut_at"] = None, None
        if matched_pred is not None:
            c = completeness(sec, main_val, matched_pred if sec == "name" else pred_main(sec, matched_pred))
            it["completeness"] = c
            if c == "truncated":
                mn = snorm(main_val)
                if (find(mn, text_n) or 0.0) < FOUND_HI:
                    it["cut_at"] = "S1 lines"
                elif max((find(mn, secs_n.get(k, "")) or 0.0) for k in TARGET[sec]) < FOUND_HI:
                    it["cut_at"] = "S4 section"
                else:
                    it["cut_at"] = "S5 entry"
        items.append(it)

    # name
    gname, pname = ev._s(gt.get("candidate_name")), ev._s(pr.get("candidate_name"))
    if gname:
        m = pname if (pname and ev.norm_name(pname) == ev.norm_name(gname)) else None
        trace("name", 0, [gname], gname, m, [] if pname == gname else (["exact form"] if m else []))
    # entries
    for sec in ("education", "experience", "publications"):
        g, p = gt_all[sec], pr.get(sec) or []
        pairs, _ = ev.match(g, p, sec)
        by_g = {gi: pi for gi, pi, _ in pairs}
        for gi, ge in enumerate(g):
            pi = by_g.get(gi)
            mp = p[pi] if pi is not None else None
            trace(sec, gi, [ge.get(k) for k in KEY_FIELDS[sec]], ev._s(ge.get(MAIN_FIELD[sec])),
                  mp, ev.field_errors(sec, ge, mp) if mp is not None else [])
        matched_p = {pi for _, pi, _ in pairs}
        matched_texts = [snorm(fp_text(sec, p[pi])) for pi in sorted(matched_p)]
        for pi, pe in enumerate(p):
            if pi not in matched_p:
                fps.append({"eval_id": eid, "section": sec, "idx": pi,
                            "cause": classify_fp(sec, pe, gt_all, set(by_g), matched_texts)})
    return items, fps


# ---- check validity ------------------------------------------------------------------------
LOSS_FOR_STAGE = {
    "stage0": ("S1 text",), "stage1": ("S1 text",),
    "stage2": ("S4 section",), "stage3": ("S4 section",), "stage4": ("S4 section",),
    "stage5": ("S5 entry", "S5 fields"),
    "stage6": ("S1 text", "S4 section", "S5 entry", "S5 fields"),
    "stage7": ("S1 text", "S4 section", "S5 entry", "S5 fields"),
}
GT_SECTION_OF_TARGET = {"personal": "name", "education": "education", "experience": "experience",
                        "publications": "publications"}


def load_checks(stem: str) -> set[tuple[str, str]] | None:
    """(stage, check) pairs that fired for one resume, from its sidecar: every reason code,
    and "status WARN" / "status FAIL" for a stage that did not end OK. None without a sidecar."""
    path = common.outputs_dir() / f"{stem}.meta.json"
    if not path.is_file():
        return None
    fired = set()
    for stage, report in read_json(path)["stage_report"].items():
        if report["status"] != "OK":
            fired.add((stage, f"status {report['status']}"))
        for reason in report["reasons"]:
            fired.add((stage, reason["code"]))
    return fired


def _relevant_loss(stage: str, check: str, losses: set[tuple[str, str]]) -> bool:
    steps = LOSS_FOR_STAGE.get(stage, ())
    if check.startswith("missing_target_section:"):
        section = GT_SECTION_OF_TARGET.get(check.split(":", 1)[1])
        return any(step == "S4 section" and sec == section for step, sec in losses)
    return any(step in steps for step, _sec in losses)


def check_validity(per_resume: list[tuple[set, set]]) -> dict[tuple[str, str], dict]:
    """(stage, check) -> {"fired", "fired_with_loss", "missed"} over (fired, losses) pairs."""
    keys = {k for fired, _ in per_resume for k in fired}
    keys |= {(s, f"status {x}") for s in LOSS_FOR_STAGE for x in ("WARN", "FAIL")}
    rows = {}
    for stage, check in sorted(keys):
        fired = with_loss = missed = 0
        for f, losses in per_resume:
            hit = (stage, check) in f
            loss = _relevant_loss(stage, check, losses)
            fired += hit
            with_loss += hit and loss
            missed += loss and not hit
        rows[(stage, check)] = {"fired": fired, "fired_with_loss": with_loss, "missed": missed}
    return rows


def print_check_validity(results: dict) -> None:
    groups = [g for g in ("dev", "tuned") if g in results]
    tables = {g: check_validity(results[g]["checks"]) for g in groups}
    keys = sorted({k for t in tables.values() for k in t})
    print(f"  {'stage':<8}{'check':<40}" + "".join(f"{g + ' fired/+loss/missed':>28}" for g in groups))
    for key in keys:
        cells = []
        for g in groups:
            row = tables[g].get(key, {"fired": 0, "fired_with_loss": 0, "missed": 0})
            cells.append(f"{row['fired']:>8} {row['fired_with_loss']:>8} {row['missed']:>8}")
        if any(tables[g].get(key, {}).get("fired") or tables[g].get(key, {}).get("missed") for g in groups):
            print(f"  {key[0]:<8}{key[1][:39]:<40}" + "".join(f"{c:>28}" for c in cells))
    print("  (fired = resumes the check fired on; +loss = of those, resumes with a real loss at the step\n"
          "   that stage feeds; missed = resumes with such a loss where the check did not fire)")


# ---- aggregation / printing ---------------------------------------------------------------
STAGES = ["GT items", "S1 found in lines (>=85)", "S4 in right section", "S5 entry matched",
          "S5 fields correct"]
LOST_ORDER = ["S1 text", "S4 section", "S5 entry", "S5 fields"]


def funnel(items: list, sec: str):
    xs = [i for i in items if i["section"] == sec]
    n = len(xs)
    lost = Counter(i["lost_at"] for i in xs)
    kept, rows = n, [n]
    for st in LOST_ORDER:
        kept -= lost[st]
        rows.append(kept)
    extra = {"s1_ge95": sum(i["s1_ge95"] for i in xs),
             "s1_recoverable": sum(i["lost_at"] == "S1 text" and bool(i["recoverable_by"]) for i in xs),
             "matched_despite_earlier_loss": sum(i["matched_despite_earlier_loss"] for i in xs),
             "truncated": sum(i["completeness"] == "truncated" for i in xs),
             "matched": sum(i["matched"] for i in xs),
             "cut_at": dict(sorted(Counter(i["cut_at"] for i in xs if i["cut_at"]).items()))}
    return rows, extra


def pct(a, b) -> str:
    return f"{100 * a / b:5.1f}" if b else "  n/a"


def print_funnels(results: dict) -> None:
    groups = list(results)
    for sec in SECTIONS:
        print(f"\n--- {sec} " + "-" * (70 - len(sec)))
        print(f"  {'stage':<26}" + "".join(f"{g:>26}" for g in groups))
        print(f"  {'':<26}" + "".join(f"{'n   %GT  %prev':>26}" for _ in groups))
        fs = {g: funnel(results[g]["items"], sec) for g in groups}
        for k, st in enumerate(STAGES):
            cells = []
            for g in groups:
                rows, _ = fs[g]
                prev = rows[k - 1] if k else rows[0]
                cells.append(f"{rows[k]:>6} {pct(rows[k], rows[0])} {pct(rows[k], prev)}")
            print(f"  {st:<26}" + "".join(f"{c:>26}" for c in cells))
        for key, label in (("s1_ge95", "  (S1 at >=95)"),
                           ("s1_recoverable", "  S1 lost, other method has it"),
                           ("matched_despite_earlier_loss", "  matched despite S1/S4 miss"),
                           ("truncated", "  truncated among matched")):
            cells = []
            for g in groups:
                rows, ex = fs[g]
                base = ex["matched"] if key == "truncated" else rows[0]
                cells.append(f"{ex[key]:>6} {pct(ex[key], base)}      ")
            print(f"  {label:<26}" + "".join(f"{c:>26}" for c in cells))
        cut = {g: fs[g][1]["cut_at"] for g in groups}
        if any(cut.values()):
            print("  truncation cut at: " + " | ".join(f"{g}: {cut[g] or '-'}" for g in groups))


def loss_points(results: dict, rank_groups=("dev", "tuned")):
    keyed: dict = defaultdict(Counter)
    for g, r in results.items():
        for i in r["items"]:
            if i["lost_at"] == "ok":
                continue
            cause = i["cause"]
            if cause.startswith("wrong:"):
                cause = "field wrong: " + cause[len("wrong: "):]
            keyed[(i["section"], i["lost_at"], cause)][g] += 1
    return sorted(keyed.items(), key=lambda kv: (-sum(kv[1][g] for g in rank_groups), kv[0]))


def method_counts(results: dict):
    keyed: dict = defaultdict(Counter)
    for g, r in results.items():
        for i in r["items"]:
            if i["lost_at"] != "ok":
                keyed[(i["section"], i["lost_at"], i["method"])][g] += 1
    return sorted(keyed.items(), key=lambda kv: (SECTIONS.index(kv[0][0]), LOST_ORDER.index(kv[0][1]), kv[0][2]))


def print_fps(results: dict) -> None:
    groups = list(results)
    print(f"\n  {'section':<14}{'cause':<46}" + "".join(f"{g:>12}" for g in groups))
    for sec in ("education", "experience", "publications"):
        tot = {g: sum(1 for f in results[g]["fps"] if f["section"] == sec) for g in groups}
        for c in FP_CAUSES:
            cnt = {g: sum(1 for f in results[g]["fps"] if f["section"] == sec and f["cause"] == c) for g in groups}
            if any(cnt.values()):
                print(f"  {sec:<14}{c:<46}" + "".join(f"{cnt[g]:>12}" for g in groups))
        print(f"  {sec:<14}{'TOTAL':<46}" + "".join(f"{tot[g]:>12}" for g in groups))


def _jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)


# ---- command line ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python eval/funnel.py", description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--test-frozen", action="store_true",
                      help="also test_frozen, aggregates only; allowed once, logged")
    mode.add_argument("--lost-items", action="store_true",
                      help="list lost items for dev and tuned (resume IDs only)")
    ap.add_argument("--stage", choices=("S1", "S4", "S5"), default="S1",
                    help="with --lost-items: which funnel step (default S1)")
    return ap


LOST_AT_FOR_STEP = {"S1": ("S1 text",), "S4": ("S4 section",), "S5": ("S5 entry", "S5 fields")}


def print_lost_items(results: dict, ids: dict, step: str = "S1") -> None:
    """One row per lost item: resume ID, section, index, cause, and recoverable_by (S1) or the
    method that handled it (S4, S5). Never any text."""
    rid_of = {v["eval_id"]: v["resume_id"] for v in ids.values()}
    last = "recoverable_by" if step == "S1" else "method"
    print(f"{'resume_id':<12}{'section':<14}{'index':>6}  {'cause':<28}{last}")
    for g in ("dev", "tuned"):
        rows = sorted((rid_of[i["eval_id"]], i["section"], i["idx"], i["cause"],
                       ",".join(i["recoverable_by"]) if step == "S1" else (i["method"] or ""))
                      for i in results[g]["items"] if i["lost_at"] in LOST_AT_FOR_STEP[step])
        for rid, sec, idx, cause, last_value in rows:
            print(f"{rid:<12}{sec:<14}{idx:>6}  {cause:<28}{(last_value or '-').replace(' ', '_')}")


def run(a: argparse.Namespace) -> int:
    groups = common.load_groups()
    order = ["dev", "tuned"]
    if a.test_frozen:
        common.require_frozen_allowed(TOOL, rerun_allowed=False)
        common.log_frozen_run(TOOL, Path(__file__), settings())
        order.append("test_frozen")
    ids = common.ids([s for g in order for s in groups[g]])

    results = {}
    for g in order:
        items, fps, incomplete, checks = [], [], 0, []
        for stem in groups[g]:
            st = load_stages(stem)
            incomplete += not st["complete"]
            it, fp = diagnose_resume(ev.load_gt(stem), ev.load_pred(stem), st, ids[stem]["eval_id"])
            items += it
            fps += fp
            if g != "test_frozen":  # never looked at per resume for test_frozen
                fired = load_checks(stem)
                if fired is not None:
                    checks.append((fired, {(i["lost_at"], i["section"]) for i in it if i["lost_at"] != "ok"}))
        results[g] = {"items": items, "fps": fps, "incomplete": incomplete, "n": len(groups[g]),
                      "checks": checks}
        if g != "test_frozen":
            out = common.reports_dir() / "funnel"
            common.write_text_inside(out / f"items_{g}.jsonl", _jsonl(items))
            common.write_text_inside(out / f"fps_{g}.jsonl", _jsonl(fps))

    if a.lost_items:
        print_lost_items(results, ids, a.stage)
        return 0
    print("=== FUNNEL: where GT items are lost (first failing stage) ===")
    for g, r in results.items():
        note = "  (aggregates only)" if g == "test_frozen" else ""
        print(f"  {g}: {r['n']} resume(s){note}")
        if r["incomplete"]:
            print(f"    {r['incomplete']} resume(s) have no stage 1 or stage 4 file; "
                  "run 'python -m resume_parser.cli run --save-stages' first")
    print_funnels(results)
    print("\n=== ITEMS LOST AT S1, BY CAUSE ===")
    print(f"  {'cause':<16}" + "".join(f"{g:>12}" for g in results))
    for cause in S1_CAUSES:
        cnt = {g: sum(1 for i in r["items"] if i["lost_at"] == "S1 text" and i["cause"] == cause)
               for g, r in results.items()}
        print(f"  {cause:<16}" + "".join(f"{cnt[g]:>12}" for g in results))
    print("\n=== LARGEST LOSS POINTS (ranked by dev + tuned) ===")
    print(f"  {'#':>3} {'section':<13}{'stage':<12}{'cause':<52}" + "".join(f"{g:>12}" for g in results))
    for k, ((sec, st, cause), cnt) in enumerate(loss_points(results)[:15], 1):
        print(f"  {k:>3} {sec:<13}{st:<12}{cause[:51]:<52}" + "".join(f"{cnt[g]:>12}" for g in results))
    print("\n=== METHOD THAT HANDLED EACH LOST ITEM ===")
    print(f"  {'section':<13}{'stage':<12}{'method':<52}" + "".join(f"{g:>12}" for g in results))
    for (sec, st, method), cnt in method_counts(results):
        print(f"  {sec:<13}{st:<12}{method[:51]:<52}" + "".join(f"{cnt[g]:>12}" for g in results))
    print("\n=== FALSE-POSITIVE CAUSES (unmatched predictions) ===")
    print_fps(results)
    print("\n=== CHECK VALIDITY (dev + tuned): does a check fire where a real loss is? ===")
    print_check_validity(results)
    return 0


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return run(a)
    except (EvalError, DataDirError) as exc:
        print(f"funnel: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
