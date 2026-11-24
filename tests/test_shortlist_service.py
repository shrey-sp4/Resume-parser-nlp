"""The UI service layer (src/shortlisting/service.py) and a smoke test of app/streamlit_app.py, on
invented data and the synthetic PDFs of scripts/make_synthetic_resumes.py only."""

from __future__ import annotations

import csv
import json

import pytest

import check_no_data
import make_synthetic_resumes as synth
from resume_parser.schema import REPO_ROOT
from shortlisting import service
from shortlisting.features import FORBIDDEN_FIELDS
from test_shortlisting import FakeInstitutions, FakeVenues

APP = REPO_ROOT / "app" / "streamlit_app.py"


@pytest.fixture
def env(data_dir, tmp_path, monkeypatch):
    monkeypatch.delenv(service.ENV_WORK_DIR, raising=False)
    monkeypatch.setattr(service, "load_indexes", lambda: (FakeVenues(), FakeInstitutions()))
    pdf_dir = tmp_path / "pdfs"
    synth.build_all(pdf_dir)
    return {"data": data_dir, "pdfs": pdf_dir, "tmp": tmp_path}


def profile(**kw):
    return service.build_profile({"mode": "default", "keywords": "invented, research", **kw})


def test_work_dir_default_and_repo_refusal(env, monkeypatch):
    assert service.resolve_work_dir() == (env["data"] / "shortlist_runs").resolve()
    with pytest.raises(service.ServiceError, match="outside the repo"):
        service.resolve_work_dir(REPO_ROOT / "runs")
    monkeypatch.setenv(service.ENV_WORK_DIR, str(REPO_ROOT / "app"))
    with pytest.raises(service.ServiceError, match="outside the repo"):
        service.resolve_work_dir()
    monkeypatch.setenv(service.ENV_WORK_DIR, str(env["tmp"] / "elsewhere"))
    assert service.resolve_work_dir() == (env["tmp"] / "elsewhere").resolve()


def test_uploads_are_copied_only_into_the_work_dir(env):
    paths = service.save_uploads([("../../evil name.pdf", b"%PDF-1.4"), ("plain", b"%PDF-1.4")])
    folder = (env["data"] / "shortlist_runs" / "uploads").resolve()
    assert [p.parent for p in paths] == [folder, folder]
    assert [p.name for p in paths] == ["evil name.pdf", "plain.pdf"]


def test_run_parses_caches_scores_and_reports_progress(env, monkeypatch):
    pdfs = service.find_pdfs(env["pdfs"])
    assert len(pdfs) == 3
    calls = []
    run = service.run_shortlist(profile(), pdfs, progress_callback=lambda d, t, s: calls.append((d, t, s)), stability=False)
    assert len(run.results) == 3 and sorted(r["file_name"] for r in run.results) == sorted(p.name for p in pdfs)
    assert calls[-1] == (4, 4, "scoring") and [c[0] for c in calls] == [1, 2, 3, 4]
    assert sum(len(v) for v in run.by_tier.values()) == 3
    cached = sorted((run.work_dir / "cache").glob("*.json"))
    assert [p.stem for p in cached] == sorted(service.file_hash(p) for p in pdfs)
    # a second run reads the cache and never runs the parser
    import resume_parser.pipeline as pipeline
    monkeypatch.setattr(pipeline, "run_pipeline", lambda pdf: (_ for _ in ()).throw(AssertionError("parser ran")))
    again = service.run_shortlist(profile(), pdfs, stability=False)
    assert [r["resume_id"] for r in again.results] == [r["resume_id"] for r in run.results]


def test_a_parser_crash_is_unreadable_with_plain_wording(env, monkeypatch):
    import resume_parser.pipeline as pipeline

    def boom(pdf):
        raise ValueError("secret path and text")

    monkeypatch.setattr(pipeline, "run_pipeline", boom)
    run = service.run_shortlist(profile(), service.find_pdfs(env["pdfs"])[:1], stability=False)
    assert [r["tier"] for r in run.unreadable] == ["Unreadable"]
    row = service.table_rows(run.unreadable)[0]
    assert row["reason"] == service.UNREADABLE_TEXT and "secret" not in json.dumps(row)


def test_tables_cards_and_export_hold_no_personal_fields(env):
    run = service.run_shortlist(profile(), service.find_pdfs(env["pdfs"]), stability=True)
    for r in run.results:
        row = service.table_rows([r])[0]
        assert set(row) == {"rank", "file", "score", "certainty", "parser flag", "reason"}
        for word in ("reject", "fail"):
            assert word not in row["reason"].lower()
        c = service.card(r)
        assert {comp["name"] for comp in c["components"]} == {"Education", "Experience", "Publications", "Topic fit"}
        assert c["rank_range"] is not None
    blob = json.dumps([service.card(r) for r in run.results]) + json.dumps(service.table_rows(run.results))
    for key in set(FORBIDDEN_FIELDS) - {"name"}:  # "name" is also the card's own component label key
        assert f'"{key}"' not in blob
    for gt in synth.synthetic_ground_truth().values():  # the synthetic CVs' printed names never appear
        assert gt["candidate_name"] and gt["candidate_name"] not in blob
    for label, column in (("resume_id", "r_"), ("file_name", "synthetic_")):
        path = service.export_csv(run, label)
        assert path.parent == (run.work_dir / "exports") and not str(path).startswith(str(REPO_ROOT))
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        assert len(rows) == 3 and all(r["file"].startswith(column) for r in rows)


def test_profile_from_the_form():
    p = service.build_profile({"mode": "education", "institution_tier_cutoff": "Tier 2", "venue_tier_cutoff": "A",
                               "keywords": "quantum widgets, gadgets", "synonyms": "quantum widgets: qubit gadgets\nnoise"})
    assert (p.institution_tier_cutoff, p.venue_tier_cutoff) == ("Tier 2", None)  # each cutoff only in its own mode
    assert p.keywords == ["quantum widgets", "gadgets"] and p.synonyms == {"quantum widgets": ["qubit gadgets"]}
    assert service.build_profile({"mode": "experience", "experience_kind": "industry", "experience_years": "10"}).experience_years == 10
    assert service.field_options()[0] == (None, "None") and len(service.field_options()) == 12
    assert "reason" not in service.MODE_SENTENCES and set(service.MODE_SENTENCES) == set(service.MODE_LABELS)


def test_editorial_override_is_marked_on_the_card(env):
    from shortlisting.scorer import Candidate, Shortlister

    cand = Candidate("r_1", {"education": [{"degree": "D", "degree_level": "doctorate", "stream": "", "institution": "Override University"}]},
                     "ACCEPT")
    result = Shortlister(profile(), FakeVenues(), FakeInstitutions()).score_pool([cand])[0]
    c = service.card(result)
    edu = next(x for x in c["components"] if x["name"] == "Education")
    assert edu["editorial_override"] and any("editorial override" in e for e in edu["evidence"])


def test_streamlit_is_imported_only_by_the_app():
    for path in (REPO_ROOT / "src").rglob("*.py"):
        assert "import streamlit" not in path.read_text(encoding="utf-8"), path.name
    assert "import streamlit" in APP.read_text(encoding="utf-8")


def test_no_data_in_the_new_files():
    files = ["app/streamlit_app.py", "src/shortlisting/service.py", "tests/test_shortlist_service.py"]
    assert check_no_data.find_problems(files, REPO_ROOT) == []


def test_ui_smoke_on_three_synthetic_pdfs(env):
    testing = pytest.importorskip("streamlit.testing.v1")
    at = testing.AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception
    assert at.title[0].value == "Candidate shortlisting"
    assert [r.label for r in at.radio][:2] == ["Where are the CVs?", "What matters most?"]
    assert at.button(key="run").disabled  # nothing chosen yet
    at.text_input(key="folder").input(str(env["pdfs"])).run()
    assert any("3" in m.value and "PDF" in m.value for m in at.markdown)
    at.radio(key="mode").set_value("experience").run()
    assert at.radio(key="kind").value == "academic"  # experience fields appear only in this mode
    at.text_area(key="keywords").input("invented, research").run()
    at.button(key="run").click().run(timeout=240)
    assert not at.exception
    assert any(service.NOTE == i.value for i in at.info)
    assert len(at.tabs) == 4 and sum(int(t.label.split("(")[1].rstrip(")")) for t in at.tabs) == 3
    for word in ("reject", "failed"):
        assert all(word not in str(m.value).lower() for m in at.markdown)
