"""Reading order on four invented page layouts (one column, two columns, a left sidebar,
tables), checked stage by stage:
  S1  the stage 1 lines hold the known text in reading order: headings and entries in the
      expected sequence, the wrapped parts of an entry on consecutive lines, and sidebar lines
      never in the middle of the main text
  S4  every entry's text lands in the stage 4 section of its kind, and no sidebar skill line
      sits in education, experience or publications
  S5  the main JSON entries match the ground truth (evaluator matching and field checks), with
      no extra entries, and the name is right
The PDFs are built in a temp folder at test time. A layout that fails today is marked xfail with
the first failing stage; no parser rule is changed for it."""

from __future__ import annotations

import pytest

import evaluate as ev
import make_synthetic_resumes as synth
from resume_parser.pipeline import run_pipeline

SECTIONS = ("education", "experience", "publications")
WIDTH = {"layout_one_column": 110, "layout_two_columns": 48, "layout_two_columns_unequal": 48,
         "layout_sidebar": 70, "layout_sidebar_right": 66, "layout_tables": None}


def _entries(kind):
    return {"education": synth.LAYOUT_EDU, "experience": synth.LAYOUT_EXP, "publications": synth.LAYOUT_PUB}[kind]


def _norm(text):
    return " ".join(str(text).split())


def stage_results(stem: str, folder) -> dict[str, bool]:
    record = run_pipeline(synth.LAYOUTS[stem](folder / f"{stem}.pdf"))
    lines = [_norm(l["text"]) for page in record.results["stage1"].data.values() for l in page["lines"]]
    width = WIDTH[stem]

    def idx(text):
        return next((i for i, line in enumerate(lines) if text in line), None)

    # S1: headings and entry starts in order; wrapped parts consecutive; sidebar not inside
    s1 = True
    if width is not None:
        order = []
        for heading, kind in (("EDUCATION", "education"), ("EXPERIENCE", "experience"), ("PUBLICATIONS", "publications")):
            order.append(idx(heading))
            for text, _gt in _entries(kind):
                parts = synth._wrapped(text, width)
                at = [idx(p) for p in parts]
                s1 &= None not in at and all(b == a + 1 for a, b in zip(at, at[1:]))
                order.append(at[0])
        s1 &= None not in order and order == sorted(order)
        if stem.startswith("layout_sidebar") and None not in order:
            side = [idx(s) for s in synth.LAYOUT_SKILLS] + [idx("CONTACT"), idx("SKILLS")]
            s1 &= all(i is not None and not (order[0] < i < order[-1]) for i in side)
    else:  # tables: one row of cells per entry, in row order
        rows = [idx(g["institution"]) for kind in ("education", "experience") for _, g in _entries(kind)]
        s1 &= None not in rows and rows == sorted(rows)

    # S4: entry text in the right section; skills outside the target sections
    s4 = True
    sections = record.results["stage4"].data["sections"]
    text_of = {sec["block_id"]: " ".join(_norm(l["text"]) for l in sec["lines"]) for sec in sections}
    for kind in SECTIONS:
        for _text, g in _entries(kind):
            key = g.get("institution") or g.get("title")
            homes = {sec["name"] for sec in sections if key in text_of[sec["block_id"]]}
            s4 &= kind in homes
    for skill in synth.LAYOUT_SKILLS:
        s4 &= not any(sec["name"] in SECTIONS and skill in text_of[sec["block_id"]] for sec in sections)

    # S5: every entry matched with correct fields, no extra entry, right name
    gt = synth.layout_ground_truth(stem)
    s5 = record.main is not None and record.main["candidate_name"] == gt["candidate_name"]
    for kind in SECTIONS:
        pred = record.main[kind] if record.main else []
        pairs, _ = ev.match(gt[kind], pred, kind)
        s5 &= len(pairs) == len(gt[kind]) == len(pred) and not any(
            ev.field_errors(kind, gt[kind][gi], pred[pi]) for gi, pi, _ in pairs)
    return {"S1": bool(s1), "S4": bool(s4), "S5": bool(s5)}


def first_failing(results: dict[str, bool]) -> str | None:
    return next((s for s in ("S1", "S4", "S5") if not results[s]), None)


# (layout, stage) pairs that fail today, with the reason; no parser rule is changed for them.
# The two-column S5 failures (entries wrapped in a narrow column) are fixed by entities/wraps.py.
KNOWN_FAILURES: dict = {}
_RESULTS: dict = {}


def _results(stem, folder):
    if stem not in _RESULTS:
        _RESULTS[stem] = stage_results(stem, folder)
    return _RESULTS[stem]


@pytest.mark.parametrize("stem,stage", [
    pytest.param(stem, stage, marks=pytest.mark.xfail(strict=False, reason=KNOWN_FAILURES[(stem, stage)]))
    if (stem, stage) in KNOWN_FAILURES else (stem, stage)
    for stem in synth.LAYOUTS for stage in ("S1", "S4", "S5")
])
def test_layout_stage(stem, stage, tmp_path):
    assert _results(stem, tmp_path)[stage], (stem, stage)


def test_layout_content_is_invented_and_the_same_for_every_layout():
    stems = list(synth.LAYOUTS)
    assert len({str(synth.layout_ground_truth(s)["education"]) for s in stems}) == 1
    assert all(synth.layout_ground_truth(s)["resume_file"] == f"{s}.pdf" for s in stems)


# ---------------------------------------------------------------- layouts wrapped by the PDF library
# Different invented content, with line breaks made by the library's own text wrapping (no
# hand-placed breaks), so the wrapped-line rule is checked on breaks it was not written against.
# A stage that fails today is marked xfail with its reason; no rule is added to fit these pages.
AUTO_KNOWN_FAILURES = {
    ("auto_two_columns", "S5"): ("S5: two lines break in the middle of a name (an institution and a company name "
                                 "run on to the next line with no comma, dash or joining word), so the wrapped-line "
                                 "rule does not join them"),
}
_AUTO_RESULTS: dict = {}


def auto_stage_results(stem: str, folder) -> dict[str, bool]:
    record = run_pipeline(synth.AUTO_LAYOUTS[stem](folder / f"{stem}.pdf"))
    lines = [_norm(l["text"]) for page in record.results["stage1"].data.values() for l in page["lines"]]

    def idx(text):
        return next((i for i, line in enumerate(lines) if text in line), None)

    entries = {"education": synth.AUTO_EDU, "experience": synth.AUTO_EXP, "publications": synth.AUTO_PUB}
    # S1: headings and the start of every entry in order (the library chooses where lines break)
    order = []
    for heading, kind in (("EDUCATION", "education"), ("EXPERIENCE", "experience"), ("PUBLICATIONS", "publications")):
        order.append(idx(heading))
        order += [idx(" ".join(text.split()[:4])) for text, _gt in entries[kind]]
    s1 = None not in order and order == sorted(order)
    if stem == "auto_sidebar" and s1:
        side = [idx(s) for s in synth.AUTO_SKILLS] + [idx("CONTACT"), idx("SKILLS")]
        s1 = all(i is not None and not (order[0] < i < order[-1]) for i in side)
    # S4: entry text in the right section
    sections = record.results["stage4"].data["sections"]
    text_of = {sec["block_id"]: " ".join(_norm(l["text"]) for l in sec["lines"]) for sec in sections}
    s4 = True
    for kind in SECTIONS:
        for _text, g in entries[kind]:
            key = " ".join((g.get("institution") or g.get("title")).split()[:3])
            s4 &= kind in {sec["name"] for sec in sections if key in text_of[sec["block_id"]]}
    # S5: every entry matched with correct fields, no extra entry, right name
    gt = synth.auto_ground_truth(stem)
    s5 = record.main is not None and record.main["candidate_name"] == gt["candidate_name"]
    for kind in SECTIONS:
        pred = record.main[kind] if record.main else []
        pairs, _ = ev.match(gt[kind], pred, kind)
        s5 &= len(pairs) == len(gt[kind]) == len(pred) and not any(
            ev.field_errors(kind, gt[kind][gi], pred[pi]) for gi, pi, _ in pairs)
    return {"S1": bool(s1), "S4": bool(s4), "S5": bool(s5)}


@pytest.mark.parametrize("stem,stage", [
    pytest.param(stem, stage, marks=pytest.mark.xfail(strict=False, reason=AUTO_KNOWN_FAILURES[(stem, stage)]))
    if (stem, stage) in AUTO_KNOWN_FAILURES else (stem, stage)
    for stem in synth.AUTO_LAYOUTS for stage in ("S1", "S4", "S5")
])
def test_auto_wrapped_layout_stage(stem, stage, tmp_path):
    if stem not in _AUTO_RESULTS:
        _AUTO_RESULTS[stem] = auto_stage_results(stem, tmp_path)
    assert _AUTO_RESULTS[stem][stage], (stem, stage)
