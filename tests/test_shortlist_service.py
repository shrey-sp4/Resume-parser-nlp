"""The UI service layer (src/shortlisting/service.py) and a smoke test of app/streamlit_app.py, on
invented data and the synthetic PDFs of scripts/make_synthetic_resumes.py only."""

from __future__ import annotations

import csv
import hashlib
import json
import re

import pytest

import check_no_data
import make_synthetic_resumes as synth
from resume_parser.schema import REPO_ROOT
from resume_parser.writer import dump_json
from shortlisting import service
from shortlisting.features import FORBIDDEN_FIELDS
from shortlisting.scorer import Candidate, Shortlister
from test_shortlisting import FakeInstitutions, FakeVenues

APP = REPO_ROOT / "app" / "streamlit_app.py"
PARSE = [label for label, _ in service.PARSE_STEPS]
# words about how the system works inside; none may reach the recruiter
JARGON = ("parser", "flag", "entry", "entries", "stability", "rank range", "bm25", "tf-idf", "tfidf", "cache",
          "hash", "config", "editorial", "override", "certainty", "confidence", "no_entries_of_type", "needs_review",
          "low_score", "tier 1", "tier 2", "tier 3", "unlisted", "sidecar", "reject", "failed")
REMOVED = ("suggest", "job_ad", "job ad", "advertisement", "saved requirements", "save_profile", "load_profile",
           "list_profiles", "profile_name", "profile_pick")


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
    low = re.sub(r"<style>.*?</style>", "", text, flags=re.S).lower()
    return [w for w in JARGON if w in low]


def states(panel: str) -> list[str]:
    """The state of each of the eight step boxes in a panel's HTML."""
    pairs = re.findall(r'data-state="(\w+)" data-step="(\d+)"', panel)
    assert len(pairs) == len(service.STEPS)
    return [s for s, _ in sorted(pairs, key=lambda p: int(p[1]))]


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


def test_removed_items_are_gone():
    src = (APP.read_text(encoding="utf-8") + (REPO_ROOT / "src/shortlisting/service.py").read_text(encoding="utf-8")).lower()
    assert [w for w in REMOVED if w in src] == []
    for name in ("suggest_keywords", "save_profile", "load_profile", "list_profiles", "ProgressBoard"):
        assert not hasattr(service, name)


def test_step_events_in_order_then_already_read(env, monkeypatch):
    pdfs = service.find_pdfs(env["pdfs"])
    assert len(pdfs) == 3
    events = []
    run = service.run_shortlist(profile(), pdfs, progress_callback=events.append)
    assert len(run.results) == 3
    seq = [(e["cv"], e["step"], e["state"]) for e in events]
    expected = []
    for k in (1, 2, 3):
        expected += [(k, s, st) for s in PARSE for st in ("running", "done")]
    for step in (service.LOOKUPS, service.YEARS):
        expected += [(k, step, st) for k in (1, 2, 3) for st in ("running", "done")]
    expected += [(None, s, st) for s in (service.SCORING, service.SORTING) for st in ("running", "done")]
    # a CV the parser marks unreadable adds its could-not-read event after its reading steps
    assert [x for x in seq if x[2] != "could_not_read"] == expected
    assert [s for s, _ in service.STEPS] == [
        "Reading the PDF", "Finding sections", "Reading education, experience and publications", "Checking the reading",
        "Looking up journal, conference and institution rankings", "Working out experience years", "Scoring",
        "Sorting into tiers"]
    assert sorted(p.stem for p in (run.work_dir / "cache").glob("*.json")) == sorted(service.file_hash(p) for p in pdfs)

    # a second run uses the saved readings and never runs the parser
    import resume_parser.pipeline as pipeline
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    events2 = []
    again = service.run_shortlist(profile(), pdfs, progress_callback=events2.append)
    assert [r["resume_id"] for r in again.results] == [r["resume_id"] for r in run.results]
    first = [e for e in events2 if e["cv"] is not None and e["step"] not in (service.LOOKUPS, service.YEARS)]
    assert {(e["step"], e["state"]) for e in first} <= {(service.SAVED, "saved"), (service.COULD_NOT_READ, "could_not_read")}
    board = service.StatusBoard(3)
    board.update(events2[0])
    assert board.status == "Already read (saved) · CV 1 of 3" and board.box_states()[:4] == ["done"] * 4


def test_status_line_bar_and_panel_state_per_event():
    board = service.StatusBoard(12)
    assert states(service.panel_html(board, "default")) == ["neutral"] * 8
    assert states(service.panel_html(None, "default")) == ["neutral"] * 8
    board.update({"cv": 3, "total": 12, "file": "c.pdf", "step": service.READ_PDF, "state": "running"})
    assert board.status == "Reading the PDF · CV 3 of 12"
    assert states(service.panel_html(board, "default")) == ["active"] + ["later"] * 7
    board.update({"cv": 3, "total": 12, "file": "c.pdf", "step": service.READ_PDF, "state": "done"})
    board.update({"cv": 3, "total": 12, "file": "c.pdf", "step": service.SECTIONS, "state": "running"})
    assert states(service.panel_html(board, "default")) == ["done", "active"] + ["later"] * 6
    board.update({"cv": 4, "total": 12, "file": "d.pdf", "step": service.COULD_NOT_READ, "state": "could_not_read"})
    assert board.status == "Could not read · CV 4 of 12"
    board.update({"cv": 5, "total": 12, "file": "e.pdf", "step": service.YEARS, "state": "running"})
    assert states(service.panel_html(board, "default")) == ["done"] * 5 + ["active", "later", "later"]
    board.update({"cv": None, "total": 12, "file": None, "step": service.SCORING, "state": "running"})
    assert board.status == "Scoring · 12 CVs"
    panel = service.panel_html(board, "default")
    assert "rp-active" in panel and "Education 25 · Experience 20 · Publications 25 · Topic 30" in panel
    board.update({"cv": None, "total": 12, "file": None, "step": service.SORTING, "state": "done"})
    assert board.finished and states(service.panel_html(board, "default", {"Shortlist": 5})) == ["done"] * 8
    assert service.strip_text(board).count("✓") == 8
    assert 0 < board.bar() <= 1.0


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
    assert [e["state"] for e in events if e["cv"] == 1 and e["step"] in PARSE + [service.COULD_NOT_READ]] == ["could_not_read"]
    assert any(e["cv"] == 2 and e["step"] == PARSE[-1] and e["state"] == "done" for e in events)
    crashed = next(r for r in run.results if r["file_name"] == pdfs[0].name)
    assert crashed["tier"] == "Unreadable"
    row = service.table_rows([crashed])[0]
    assert row["Reason"] == service.UNREADABLE_TEXT and "secret" not in json.dumps(row)


def test_unreadable_reason_only_when_clear():
    scanned = {"flags": [{"code": "needs_ocr"}, {"code": "no_text_layer"}], "decision_reasons": ["empty_name"]}
    assert service._unreadable_reason(scanned) == "It looks like a scanned image without text."
    assert service._unreadable_reason({"flags": [], "decision_reasons": ["empty_name", "low_score:name"]}) == ""
    assert service._unreadable_reason(None) == ""


def test_parser_output_is_identical_through_the_observed_stages(env):
    from resume_parser.pipeline import run_pipeline

    def digest(rec):
        blob = (dump_json(rec.main, sort_keys=False) if rec.main is not None else "null") + dump_json(rec.sidecar, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    for pdf in service.find_pdfs(env["pdfs"]):
        assert digest(service.run_pipeline_observed(pdf, lambda s, w: None)) == digest(run_pipeline(pdf))


def test_scores_and_tiers_match_the_scorer_rank(env):
    """The service's own steps (lookups, years, scoring, sorting) give exactly Shortlister.rank."""
    pdfs = service.find_pdfs(env["pdfs"])
    for mode in ("default", "education", "experience", "publications"):
        p = profile(mode=mode, min_degree_level="masters")
        run = service.run_shortlist(p, pdfs)
        cands = []
        for r, pdf in zip(sorted(run.results, key=lambda r: pdfs.index(run.paths[r["resume_id"]])), pdfs):
            main, side, text = service.parse_cached(pdf, run.work_dir / "cache")
            flags = sorted({f.get("code", "") for f in side.get("flags") or []})
            cands.append(Candidate(r["resume_id"], main or {}, side.get("decision"), flags, text))
        ref = Shortlister(p, FakeVenues(), FakeInstitutions()).rank(cands, stability=False)
        keep = ("resume_id", "tier", "score", "rank", "rank_range", "components", "eligibility", "tier_reasons")
        assert [{k: r[k] for k in keep} for r in run.results] == [{k: r[k] for k in keep} for r in ref]


def test_tables_cards_summary_and_export_are_plain_and_hold_no_personal_fields(env):
    run = service.run_shortlist(profile(), service.find_pdfs(env["pdfs"]))
    counts = {t: len(run.by_tier[t]) for t in service.TIERS}
    assert service.summary_line(run) == "3 CVs read: " + ", ".join(f"{counts[t]} {t}" for t in service.TIERS)
    for r in run.results:
        row = service.table_rows([r])[0]
        assert list(row) == ["Rank", "CV", "Score", "Based on", "Reason"]
        if r["score"] is not None:
            assert row["Score"] == f"{round(r['score'] * 100)}%" and re.fullmatch(r"\d{1,3}%", row["Score"])
        c = service.card(r)
        # the chips match the scorer's dropped components
        dropped = {service.AREAS[d["component"]] for d in r["dropped_components"]}
        assert {a for a, s in c["areas"] if s not in service.COUNTED} == dropped
        pubs = r["components"]["publications"]
        assert dict(c["areas"])["Publications"] == ("None found" if pubs["reason"] == "no_publication_entries" else
                                                    "Found" if pubs["value"] is not None else "Not found")
        assert c["based_on"] == f"{4 - len(dropped)} of 4"
        chips = service.chips_html(r)
        assert sorted(re.findall(r'data-area="([^"]+)" data-status="([^"]+)"', chips)) == sorted(c["areas"])
    blob = json.dumps([service.card(r) for r in run.results]) + json.dumps(service.table_rows(run.results))
    blob += "".join(service.chips_html(r) for r in run.results)
    blob += service.table_html(service.table_rows(run.results), ["Rank", "CV", "Score", "Based on", "Reason"])
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
        assert list(rows[0]) == ["Tier", "Rank", "CV", "Score", "Based on", "Areas found", "Reason"]
        assert no_jargon(path.read_text(encoding="utf-8")) == []


def test_table_headers_have_short_hints():
    table = service.table_html([{"Rank": 1, "CV": "<x>.pdf", "Score": "87%", "Based on": "3 of 4", "Reason": "r"}],
                               ["Rank", "CV", "Score", "Based on", "Reason"])
    hints = re.findall(r'class="rp-hint" title="([^"]+)">\?<', table)
    assert len(hints) == 5 and all(len(h.split()) <= 10 for h in hints)
    assert "&lt;x&gt;.pdf" in table and "<x>" not in table


def test_review_reason_names_the_missing_areas():
    base = {"tier": "Review", "score": 0.82, "dropped_components": [{"component": "experience", "reason": "no_experience_entries"},
                                                                   {"component": "publications", "reason": "x"}]}
    text = service.reason_text({**base, "tier_reasons": ["overall certainty low", "key component unusable: experience"]})
    assert text == "High match, but Experience and Publications were not found. Please check the CV."
    one = {**base, "score": 0.3, "dropped_components": [{"component": "education", "reason": "no_education_entries"}],
           "tier_reasons": ["key component unusable: education"]}
    assert service.reason_text(one) == "Low match, but Education was not found. Please check the CV."
    thin = {**base, "dropped_components": [], "tier_reasons": ["overall certainty low"]}
    assert service.reason_text(thin) == "High match, but little could be read clearly. Please check the CV."
    topic_off = {"dropped_components": [{"component": "topic_fit", "reason": "no_keywords"}], "tier": "Hold"}
    assert dict(service.areas(topic_off))["Topic match"] == "Not used"


def test_an_area_counted_with_none_in_the_cv_says_none_found():
    """Zero publications are counted in the score (as 0), so the chip says None found, the area
    still counts in Based on and in the CSV, and Not found stays for dropped areas."""
    cand = Candidate("r_1", {"education": [{"degree": "D", "degree_level": "doctorate", "stream": "",
                                            "institution": "Alpha University"}]}, "ACCEPT")
    r = Shortlister(profile(), FakeVenues(), FakeInstitutions()).score_pool([cand])[0]
    r.update(rank=1, file_name="x.pdf", degrees=["Doctorate"])
    assert r["components"]["publications"]["reason"] == "no_publication_entries"
    assert "publications" not in {d["component"] for d in r["dropped_components"]}
    found = dict(service.areas(r))
    assert found["Publications"] == "None found" and found["Education"] == "Found"
    assert found["Experience"] == "Not found"  # dropped from the score
    assert service.based_on(r) == f"{sum(1 for s in found.values() if s in service.COUNTED)} of 4"
    chips = service.chips_html(r)
    assert 'data-area="Publications" data-status="None found"' in chips and "rp-none" in chips
    assert "Publications · Found" not in chips
    run = service.ShortlistRun([{**r, "tier": "Review"}], REPO_ROOT.parent, {"Review": [r]})
    row = service.export_rows(run)[0]
    assert "Publications" in row["Areas found"].split(", ")
    assert "Experience" not in row["Areas found"]


def test_plain_reasons_for_each_tier_reason():
    codes = ["eligibility fail: min_degree_level", "eligibility unknown: institution_tier", "key component unusable: experience",
             "overall certainty low", "education: editorial override", "education: unlisted institution",
             "parse decision NEEDS_REVIEW", "score below the threshold", "no usable score", "score at or above the threshold"]
    for code in codes:
        text = service.reason_text({"tier": "Review", "score": 0.5, "dropped_components": [], "tier_reasons": [code]})
        assert text and no_jargon(text) == [], code
    assert service.reason_text({"tier": "Review", "tier_reasons": ["education: editorial override"]}) == \
        "Institution rank set by the university's list"


def test_sort_and_filter():
    rs = [{"resume_id": "r_1", "file_name": "b.pdf", "score": 0.5}, {"resume_id": "r_2", "file_name": "a.pdf", "score": 0.9},
          {"resume_id": "r_3", "file_name": "c.pdf", "score": None}]
    assert [r["resume_id"] for r in service.sort_and_filter(rs)] == ["r_2", "r_1", "r_3"]
    assert [r["resume_id"] for r in service.sort_and_filter(rs, "file")] == ["r_2", "r_1", "r_3"]
    assert [r["resume_id"] for r in service.sort_and_filter(rs, "score", 0.6)] == ["r_2"]


def test_profile_from_the_form_and_weights_text():
    p = service.build_profile({"mode": "education", "institution_tier_cutoff": "Tier 2", "venue_tier_cutoff": "A",
                               "keywords": "quantum widgets, gadgets", "synonyms": "quantum widgets: qubit gadgets\nnoise"})
    assert (p.institution_tier_cutoff, p.venue_tier_cutoff) == ("Tier 2", None)  # each cutoff only in its own mode
    assert p.keywords == ["quantum widgets", "gadgets"] and p.synonyms == {"quantum widgets": ["qubit gadgets"]}
    assert service.build_profile({"mode": "experience", "experience_kind": "industry", "experience_years": "10"}).experience_years == 10
    assert service.field_options()[0] == (None, "Any field") and len(service.field_options()) == 12
    assert service.weights_text("default") == "Education 25 · Experience 20 · Publications 25 · Topic 30"
    assert service.weights_text("experience") == "Education 15 · Experience 40 · Publications 15 · Topic 30"


def test_step_descriptions_are_short():
    for _, desc in (*service.STEPS, *service.INPUTS, *service.TIER_TEXT.items()):
        assert len(desc.split()) <= 8, desc


def test_clear_saved_results_only_removes_saved_readings(env):
    run = service.run_shortlist(profile(), service.find_pdfs(env["pdfs"])[:2])
    keep = run.work_dir / "cache" / "notes.txt"
    keep.write_text("kept", encoding="utf-8")
    assert service.clear_saved_results() == 2
    assert list((run.work_dir / "cache").glob("*.json")) == [] and keep.is_file()


def test_editorial_override_is_in_plain_words_on_the_card(env):
    cand = Candidate("r_1", {"education": [{"degree": "D", "degree_level": "doctorate", "stream": "", "institution": "Override University"}]},
                     "ACCEPT")
    result = Shortlister(profile(), FakeVenues(), FakeInstitutions()).score_pool([cand])[0]
    result.update(file_name="x.pdf", degrees=["Doctorate"])
    c = service.card(result)
    assert "Institution rank set by the university's list" in c["reason"]
    edu = next(x for x in c["components"] if x["name"] == "Education")
    assert "Degrees: Doctorate" in edu["found"] and no_jargon(json.dumps(c)) == []


def test_fonts_have_fallbacks_and_no_network():
    assert "Garamond, 'EB Garamond', Georgia, serif" in service.CSS
    assert "'Times New Roman', Times, serif" in service.CSS
    text = APP.read_text(encoding="utf-8") + service.CSS
    assert not re.search(r"https?://", text)
    assert "@import" not in text and "@font-face" not in text


def test_streamlit_is_imported_only_by_the_app():
    for path in (REPO_ROOT / "src").rglob("*.py"):
        assert "import streamlit" not in path.read_text(encoding="utf-8"), path.name
    assert "import streamlit" in APP.read_text(encoding="utf-8")


def test_no_data_in_the_new_files():
    files = ["app/streamlit_app.py", "src/shortlisting/service.py", "tests/test_shortlist_service.py"]
    assert check_no_data.find_problems(files, REPO_ROOT) == []


def _ui_text(at) -> str:
    """Every piece of text the app shows: elements, widget labels, hints and options."""
    parts = []
    for kind in ("title", "header", "subheader", "markdown", "caption", "info", "warning", "error", "success",
                 "exception"):
        parts += [str(e.value) for e in getattr(at, kind)]
    for kind in ("radio", "selectbox", "text_input", "text_area", "button", "slider", "select_slider"):
        for w in getattr(at, kind):
            parts += [str(getattr(w, "label", "")), str(getattr(w.proto, "placeholder", "")), str(w.help or "")]
            parts += [str(o) for o in getattr(w, "options", []) or []]
    parts += [e.label for e in at.expander] + [t.label for t in at.tabs]
    return "\n".join(parts)


def test_ui_smoke_on_three_synthetic_pdfs(env):
    testing = pytest.importorskip("streamlit.testing.v1")
    at = testing.AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception
    assert at.title[0].value == "Candidate shortlisting"
    assert [r.label for r in at.radio][:2] == ["Choose your CV files", "What matters most?"]
    assert at.text_area(key="keywords").proto.placeholder == "machine learning, signal processing, deep learning"
    assert at.text_area(key="synonyms").proto.placeholder == "machine learning: ML, deep learning"
    for kind in ("radio", "selectbox", "text_input", "text_area", "slider", "select_slider"):
        for w in getattr(at, kind):  # every input has a short hint
            assert w.help and len(w.help.split()) <= 10, w.label
    assert [w for w in REMOVED if w in _ui_text(at).lower()] == []
    panel = next(m.value for m in at.markdown if 'class="rp-panel"' in m.value)
    assert states(panel) == ["neutral"] * 8 and "Your CVs" in panel and "Your requirements" in panel
    assert at.button(key="run").disabled  # nothing chosen yet
    at.text_input(key="folder").input(str(env["tmp"] / "no such folder")).run()
    assert [e.value for e in at.error] == ["We could not find this folder. Please check the path."]
    at.text_input(key="folder").input(str(env["pdfs"])).run()
    at.radio(key="mode").set_value("experience").run()
    assert at.radio(key="kind").value == "academic"  # experience fields appear only in this mode
    at.text_area(key="keywords").input("invented, research").run()
    at.button(key="run").click().run(timeout=240)
    assert not at.exception and not at.info and not at.warning and not at.error
    text = _ui_text(at)
    run = at.session_state["shortlist_run"]
    assert service.summary_line(run) in [m.value for m in at.markdown]
    assert "Done · 3 CVs" in [m.value for m in at.markdown]
    assert len(at.tabs) == 4 and sum(int(t.label.split("(")[1].rstrip(")")) for t in at.tabs) == 3
    assert [c.value for c in at.caption][-1] == service.NOTE
    assert no_jargon(text) == [] and "%" in text
    strip = at.expander[0]
    assert strip.label.count("✓") == 8
    final = next(m.value for m in at.markdown if 'class="rp-panel"' in m.value)
    assert states(final) == ["done"] * 8 and "Experience 40" in final

    # the minimum-score filter
    at.slider(key="min_score").set_value(100).run()
    assert sum(int(t.label.split("(")[1].rstrip(")")) for t in at.tabs[:3]) == sum(
        1 for r in run.results if r["tier"] != "Unreadable" and (r["score"] or 0) >= 1.0)

    # clearing the saved results asks first
    at.button(key="clear").click().run()
    assert list((run.work_dir / "cache").glob("*.json")) and at.button(key="clear_yes")
    at.button(key="clear_yes").click().run()
    assert list((run.work_dir / "cache").glob("*.json")) == []
    assert at.button(key="clear")
