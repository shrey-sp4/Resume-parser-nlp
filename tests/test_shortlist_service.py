"""The UI service layer (src/shortlisting/service.py) and a smoke test of app/streamlit_app.py, on
invented data and the synthetic PDFs of scripts/make_synthetic_resumes.py only."""

from __future__ import annotations

import csv
import hashlib
import json

import pytest

import check_no_data
import make_synthetic_resumes as synth
from resume_parser.schema import REPO_ROOT
from resume_parser.writer import dump_json
from shortlisting import service
from shortlisting.features import FORBIDDEN_FIELDS
from test_shortlisting import FakeInstitutions, FakeVenues

APP = REPO_ROOT / "app" / "streamlit_app.py"
STEPS = [label for label, _ in service.PARSE_STEPS]
# words about how the system works inside; none may reach the recruiter
JARGON = ("parser", "flag", "entry", "entries", "stability", "rank range", "bm25", "tf-idf", "tfidf", "cache",
          "hash", "config", "editorial", "override", "certainty", "no_entries_of_type", "needs_review", "low_score",
          "tier 1", "tier 2", "tier 3", "unlisted", "sidecar", "reject", "failed")


@pytest.fixture
def env(data_dir, tmp_path, monkeypatch):
    monkeypatch.delenv(service.ENV_WORK_DIR, raising=False)
    monkeypatch.setattr(service, "load_indexes", lambda: (FakeVenues(), FakeInstitutions()))
    pdf_dir = tmp_path / "pdfs"
    synth.build_all(pdf_dir)
    return {"data": data_dir, "pdfs": pdf_dir, "tmp": tmp_path}


def profile(**kw):
    return service.build_profile({"mode": "default", "keywords": "invented, research", **kw})


def no_jargon(text: str) -> list[str]:
    low = text.lower()
    return [w for w in JARGON if w in low]


def test_work_dir_default_and_repo_refusal(env, monkeypatch):
    assert service.resolve_work_dir() == (env["data"] / "shortlist_runs").resolve()
    with pytest.raises(service.ServiceError, match="outside"):
        service.resolve_work_dir(REPO_ROOT / "runs")
    monkeypatch.setenv(service.ENV_WORK_DIR, str(REPO_ROOT / "app"))
    with pytest.raises(service.ServiceError, match="outside"):
        service.resolve_work_dir()
    monkeypatch.setenv(service.ENV_WORK_DIR, str(env["tmp"] / "elsewhere"))
    assert service.resolve_work_dir() == (env["tmp"] / "elsewhere").resolve()


def test_uploads_are_copied_only_into_the_work_dir(env):
    paths = service.save_uploads([("../../evil name.pdf", b"%PDF-1.4"), ("plain", b"%PDF-1.4")])
    folder = (env["data"] / "shortlist_runs" / "uploads").resolve()
    assert [p.parent for p in paths] == [folder, folder]
    assert [p.name for p in paths] == ["evil name.pdf", "plain.pdf"]


def test_progress_events_in_order_then_already_read(env, monkeypatch):
    pdfs = service.find_pdfs(env["pdfs"])
    assert len(pdfs) == 3
    events = []
    run = service.run_shortlist(profile(), pdfs, progress_callback=events.append)
    assert len(run.results) == 3 and sorted(r["file_name"] for r in run.results) == sorted(p.name for p in pdfs)
    for k, pdf in enumerate(pdfs, 1):
        mine = [(e["step"], e["state"]) for e in events if e["cv"] == k]
        assert all(e["file"] == pdf.name and e["total"] == 3 for e in events if e["cv"] == k)
        expected = [x for s in STEPS for x in ((s, "running"), (s, "done"))]
        assert mine[:-1] == expected and mine[-1][1] in ("finished", "could_not_read")
    assert [e["cv"] for e in events][-2:] == [None, None]
    assert [(e["step"], e["state"]) for e in events[-2:]] == [("Scoring", "running"), ("Scoring", "done")]
    # CVs are read one after the other
    order = [e["cv"] for e in events if e["cv"] is not None]
    assert order == sorted(order)
    cached = sorted((run.work_dir / "cache").glob("*.json"))
    assert [p.stem for p in cached] == sorted(service.file_hash(p) for p in pdfs)

    # a second run uses the saved readings and never runs the parser
    import resume_parser.pipeline as pipeline
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    events2 = []
    again = service.run_shortlist(profile(), pdfs, progress_callback=events2.append)
    assert [r["resume_id"] for r in again.results] == [r["resume_id"] for r in run.results]
    per_cv = [(e["step"], e["state"]) for e in events2 if e["cv"] is not None]
    assert all(s == ("Already read (saved)", "saved") for s in per_cv if s[1] != "could_not_read") and len(per_cv) >= 3
    board = service.ProgressBoard([p.name for p in pdfs])
    for e in events2[:-2]:
        board.update(e)
    assert {r["Stage"] for r in board.rows()} <= {service.SAVED, service.COULD_NOT_READ}
    for e in events2[-2:]:
        board.update(e)
    assert board.bar()[0] == 1.0


def test_progress_board_rows_and_bar():
    board = service.ProgressBoard(["a.pdf", "b.pdf"])
    assert board.bar() == (0.0, "CV 1 of 2")
    board.update({"cv": 1, "total": 2, "file": "a.pdf", "step": STEPS[0], "state": "running"})
    assert board.rows()[0] == {"CV": "a.pdf", "Stage": STEPS[0], "Done": ""}
    board.update({"cv": 1, "total": 2, "file": "a.pdf", "step": STEPS[0], "state": "done"})
    board.update({"cv": 2, "total": 2, "file": "b.pdf", "step": service.COULD_NOT_READ, "state": "could_not_read"})
    assert board.bar()[1] == "CV 2 of 2"
    board.update({"cv": None, "total": 2, "step": "Scoring", "state": "running"})
    assert [r["Stage"] for r in board.rows()] == ["Scoring", "Could not read"]
    board.update({"cv": None, "total": 2, "step": "Scoring", "state": "done"})
    assert board.rows() == [{"CV": "a.pdf", "Stage": "Done", "Done": "✓"}, {"CV": "b.pdf", "Stage": "Could not read", "Done": ""}]


def test_a_crashed_cv_shows_could_not_read_and_the_run_continues(env, monkeypatch):
    import resume_parser.pipeline as pipeline

    real = pipeline.run_pipeline
    pdfs = service.find_pdfs(env["pdfs"])

    def sometimes(pdf, *a, **k):
        if pdf.name == pdfs[0].name:
            raise ValueError("secret path and text")
        return real(pdf, *a, **k)

    monkeypatch.setattr(pipeline, "run_pipeline", sometimes)
    events = []
    run = service.run_shortlist(profile(), pdfs, progress_callback=events.append)
    assert len(run.results) == 3
    assert [e["state"] for e in events if e["cv"] == 1] == ["could_not_read"]
    assert [e["state"] for e in events if e["cv"] == 2][-1] in ("finished", "could_not_read")
    crashed = next(r for r in run.results if r["file_name"] == pdfs[0].name)
    assert crashed["tier"] == "Unreadable"
    row = service.table_rows([crashed])[0]
    assert row["Reason"] == service.UNREADABLE_TEXT and "secret" not in json.dumps(row)


def test_unreadable_reason_only_when_clear():
    scanned = {"flags": [{"code": "needs_ocr"}, {"code": "no_text_layer"}], "decision_reasons": ["empty_name"]}
    assert service._unreadable_reason(scanned) == "It looks like a scanned image without text."
    assert service._unreadable_reason({"flags": [], "decision_reasons": ["empty_name", "low_score:name"]}) == ""
    assert service._unreadable_reason(None) == ""
    r = {"tier": "Unreadable", "unreadable_reason": "It looks like a scanned image without text."}
    assert service.reason_text(r) == service.UNREADABLE_TEXT + " It looks like a scanned image without text."


def test_parser_output_is_identical_through_the_observed_stages(env):
    from resume_parser.pipeline import run_pipeline

    def digest(rec):
        blob = (dump_json(rec.main, sort_keys=False) if rec.main is not None else "null") + dump_json(rec.sidecar, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    seen = []
    for pdf in service.find_pdfs(env["pdfs"]):
        assert digest(service.run_pipeline_observed(pdf, lambda s, w: seen.append((s, w)))) == digest(run_pipeline(pdf))
    assert seen[:2] == [("stage0", "start"), ("stage0", "end")]


def test_tables_cards_summary_and_export_are_plain_and_hold_no_personal_fields(env):
    run = service.run_shortlist(profile(), service.find_pdfs(env["pdfs"]))
    counts = {t: len(run.by_tier[t]) for t in service.TIERS}
    assert service.summary_line(run) == "3 CVs read: " + ", ".join(f"{counts[t]} {t}" for t in service.TIERS)
    for r in run.results:
        row = service.table_rows([r])[0]
        assert set(row) == {"Rank", "CV", "Score", "Confidence", "Reason"}
        assert row["Confidence"] in ("", "High confidence", "Medium confidence", "Low confidence")
        c = service.card(r)
        assert {comp["name"] for comp in c["components"]} <= {"Education", "Experience", "Publications", "Topic match"}
    blob = json.dumps([service.card(r) for r in run.results]) + json.dumps(service.table_rows(run.results))
    assert no_jargon(blob) == []
    for key in set(FORBIDDEN_FIELDS) - {"name"}:  # "name" is also the card's own component label key
        assert f'"{key}"' not in blob
    for gt in synth.synthetic_ground_truth().values():  # the synthetic CVs' printed names never appear
        assert gt["candidate_name"] and gt["candidate_name"] not in blob
    for label, column in (("resume_id", "r_"), ("file_name", "synthetic_")):
        path = service.export_csv(run, label)
        assert path.parent == (run.work_dir / "exports") and not str(path).startswith(str(REPO_ROOT))
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        assert len(rows) == 3 and all(r["CV"].startswith(column) for r in rows)
        assert set(rows[0]) == {"Tier", "Rank", "CV", "Score", "Confidence", "Reason"}
        assert no_jargon(path.read_text(encoding="utf-8")) == []


def test_sort_and_filter():
    rs = [{"resume_id": "r_1", "file_name": "b.pdf", "score": 0.5}, {"resume_id": "r_2", "file_name": "a.pdf", "score": 0.9},
          {"resume_id": "r_3", "file_name": "c.pdf", "score": None}]
    assert [r["resume_id"] for r in service.sort_and_filter(rs)] == ["r_2", "r_1", "r_3"]
    assert [r["resume_id"] for r in service.sort_and_filter(rs, "file")] == ["r_2", "r_1", "r_3"]
    assert [r["resume_id"] for r in service.sort_and_filter(rs, "score", 0.6)] == ["r_2"]


def test_plain_reasons_for_each_tier_reason():
    codes = ["eligibility fail: min_degree_level", "eligibility unknown: institution_tier", "key component unusable: experience",
             "overall certainty low", "education: editorial override", "education: unlisted institution",
             "parse decision NEEDS_REVIEW", "score below the threshold", "no usable score", "score at or above the threshold"]
    for code in codes:
        text = service.reason_text({"tier": "Review", "tier_reasons": [code]})
        assert text and no_jargon(text) == [], code
    assert service.reason_text({"tier": "Review", "tier_reasons": ["education: editorial override"]}) == \
        "Institution rank set by the university's list"


def test_profile_from_the_form_and_save_and_load(env):
    p = service.build_profile({"mode": "education", "institution_tier_cutoff": "Tier 2", "venue_tier_cutoff": "A",
                               "keywords": "quantum widgets, gadgets", "synonyms": "quantum widgets: qubit gadgets\nnoise"})
    assert (p.institution_tier_cutoff, p.venue_tier_cutoff) == ("Tier 2", None)  # each cutoff only in its own mode
    assert p.keywords == ["quantum widgets", "gadgets"] and p.synonyms == {"quantum widgets": ["qubit gadgets"]}
    assert service.build_profile({"mode": "experience", "experience_kind": "industry", "experience_years": "10"}).experience_years == 10
    assert service.field_options()[0] == (None, "Any field") and len(service.field_options()) == 12

    form = {"mode": "publications", "field": "economics", "min_degree_level": "masters", "experience_kind": "academic",
            "experience_years": 5, "institution_tier_cutoff": None, "venue_tier_cutoff": "B",
            "keywords": "invented topic", "synonyms": "invented topic: made up", "unknown_key": "dropped"}
    path = service.save_profile("Invented role / 1", form)
    assert path.parent == (env["data"] / "shortlist_runs" / "profiles").resolve() and path.suffix == ".json"
    assert service.list_profiles() == [path.stem]
    loaded = service.load_profile(path.stem)
    assert loaded == {k: form[k] for k in service.FORM_KEYS}
    with pytest.raises(service.ServiceError):
        service.save_profile("  ", form)
    with pytest.raises(service.ServiceError):
        service.load_profile("not saved")


def test_clear_saved_results_only_removes_saved_readings(env):
    run = service.run_shortlist(profile(), service.find_pdfs(env["pdfs"])[:2])
    keep = run.work_dir / "cache" / "notes.txt"
    keep.write_text("kept", encoding="utf-8")
    service.save_profile("kept", {"mode": "default"})
    assert service.clear_saved_results() == 2
    assert list((run.work_dir / "cache").glob("*.json")) == [] and keep.is_file() and service.list_profiles() == ["kept"]


def test_editorial_override_is_in_plain_words_on_the_card(env):
    from shortlisting.scorer import Candidate, Shortlister

    cand = Candidate("r_1", {"education": [{"degree": "D", "degree_level": "doctorate", "stream": "", "institution": "Override University"}]},
                     "ACCEPT")
    result = Shortlister(profile(), FakeVenues(), FakeInstitutions()).score_pool([cand])[0]
    result.update(file_name="x.pdf", degrees=["Doctorate"])
    c = service.card(result)
    assert "Institution rank set by the university's list" in c["reason"]
    edu = next(x for x in c["components"] if x["name"] == "Education")
    assert "Degrees: Doctorate" in edu["found"] and no_jargon(json.dumps(c)) == []


def test_streamlit_is_imported_only_by_the_app():
    for path in (REPO_ROOT / "src").rglob("*.py"):
        assert "import streamlit" not in path.read_text(encoding="utf-8"), path.name
    assert "import streamlit" in APP.read_text(encoding="utf-8")


def test_no_data_in_the_new_files():
    files = ["app/streamlit_app.py", "src/shortlisting/service.py", "tests/test_shortlist_service.py"]
    assert check_no_data.find_problems(files, REPO_ROOT) == []


def _ui_text(at) -> str:
    """Every piece of text the app shows: elements, widget labels and options, tables."""
    parts = []
    for kind in ("title", "header", "subheader", "markdown", "caption", "info", "warning", "error", "success",
                 "exception"):
        parts += [str(e.value) for e in getattr(at, kind)]
    for kind in ("radio", "selectbox", "text_input", "text_area", "button", "slider", "select_slider"):
        for w in getattr(at, kind):
            parts.append(str(getattr(w, "label", "")))
            parts.append(str(getattr(w.proto, "placeholder", "")))
            parts += [str(o) for o in getattr(w, "options", []) or []]
    parts += [e.label for e in at.expander] + [t.label for t in at.tabs]
    parts += [d.value.to_string() for d in at.dataframe]
    return "\n".join(parts)


def test_ui_smoke_on_three_synthetic_pdfs(env):
    testing = pytest.importorskip("streamlit.testing.v1")
    at = testing.AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception
    assert at.title[0].value == "Candidate shortlisting"
    assert [r.label for r in at.radio][:2] == ["Choose your CV files", "What matters most?"]
    assert at.radio(key="source").options == ["Folder with PDF files", "Upload PDF files"]
    assert at.text_area(key="keywords").proto.placeholder == "machine learning, signal processing, deep learning"
    assert at.text_area(key="synonyms").proto.placeholder == "machine learning: ML, deep learning"
    assert at.text_area(key="job_ad").proto.placeholder == "Paste the job description here"
    assert at.button(key="run").disabled  # nothing chosen yet
    at.text_input(key="folder").input(str(env["tmp"] / "no such folder")).run()
    assert [e.value for e in at.error] == ["We could not find this folder. Please check the path."]
    at.text_input(key="folder").input(str(env["pdfs"])).run()
    assert any("3" in m.value and "PDF" in m.value for m in at.markdown)
    at.radio(key="mode").set_value("experience").run()
    assert at.radio(key="kind").value == "academic"  # experience fields appear only in this mode
    at.text_area(key="keywords").input("invented, research").run()
    at.button(key="run").click().run(timeout=240)
    assert not at.exception and not at.info and not at.warning and not at.error
    text = _ui_text(at)
    run = at.session_state["shortlist_run"]
    assert service.summary_line(run) in [m.value for m in at.markdown]
    assert len(at.tabs) == 4 and sum(int(t.label.split("(")[1].rstrip(")")) for t in at.tabs) == 3
    assert [c.value for c in at.caption][-1] == service.NOTE
    assert no_jargon(text) == []

    # the minimum-score filter and the sort order
    at.slider(key="min_score").set_value(1.0).run()
    assert sum(int(t.label.split("(")[1].rstrip(")")) for t in at.tabs[:3]) == sum(
        1 for r in run.results if r["tier"] != "Unreadable" and (r["score"] or 0) >= 1.0)

    # clearing the saved results asks first
    at.button(key="clear").click().run()
    assert list((run.work_dir / "cache").glob("*.json")) and at.button(key="clear_yes")
    at.button(key="clear_yes").click().run()
    assert list((run.work_dir / "cache").glob("*.json")) == []
    assert at.button(key="clear")  # back to the single button


def test_ui_saves_and_loads_requirements(env):
    testing = pytest.importorskip("streamlit.testing.v1")
    at = testing.AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    at.radio(key="mode").set_value("publications").run()
    at.selectbox(key="venue_cut").set_value("B").run()
    at.text_area(key="keywords").input("invented topic").run()
    at.text_input(key="profile_name").input("invented role").run()
    at.button(key="save_profile").click().run()
    assert service.list_profiles() == ["invented role"]
    at.radio(key="mode").set_value("default").run()
    at.text_area(key="keywords").input("").run()
    at.selectbox(key="profile_pick").set_value("invented role").run()
    at.button(key="load_profile").click().run()
    assert not at.exception and not at.error
    assert at.radio(key="mode").value == "publications" and at.selectbox(key="venue_cut").value == "B"
    assert at.text_area(key="keywords").value == "invented topic"
