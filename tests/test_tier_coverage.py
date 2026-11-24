"""eval/tier_coverage.py on invented ground truth: development groups only, counts only, the
unmatched strings go to a private file and are never printed."""

from __future__ import annotations

import json

import pytest

import tier_coverage
from shortlisting.tiers import lexicons
from shortlisting.tiers.venues import VenueIndex, load_core, load_scimago
from shortlisting.tiers.institutions import InstitutionIndex, parse_nirf_page
from test_tiers_institutions import PAGES
from test_tiers_venues import ACRONYMS, FIELDS, write_lexicons

SECRET_DEV = "Journal of Secret Unlisted Studies"
SECRET_VALIDATION = "Journal of Imaginary Signal Processing Validation Only"
SECRET_INSTITUTION = "Freedonia College of Secret Studies"


def _gt(stem, pubs, edu=(), exp=()):
    return {"resume_file": f"{stem}.pdf", "candidate_name": "Invented Person",
            "education": [{"degree": "PhD", "degree_level": "doctorate", "stream": "Toys", "institution": i} for i in edu],
            "experience": [{"job_title": "Lecturer", "institution": i, "dates": "2020", "experience_type": t} for t, i in exp],
            "publications": [{"title": f"Paper {k}", "type": t, "year": "2020", "venue": v} for k, (t, v) in enumerate(pubs)],
            "stated_totals": {"journal": None, "conference": None, "total": None}, "notes": ""}


@pytest.fixture
def setup(data_dir, tmp_path, monkeypatch):
    gt = data_dir / "ground_truth_v2"
    files = {
        "synthetic_dev_one": [("journal", "J. Imaginary Signal Process."), ("journal", SECRET_DEV),
                              ("conference", "ICPL 2021"), ("other", "Some Book"), ("journal", "")],
        "synthetic_tuned_one": [("journal", "Review of Toy Physics B"), ("conference", "National Conference on Toy Things")],
        "synthetic_validation_one": [("journal", SECRET_VALIDATION)] * 5,
    }
    people = {"synthetic_dev_one": (["IIT Delhi", SECRET_INSTITUTION], [("academic", "Fictional College of Arts"), ("industry", "Toy Company Pvt Ltd")]),
              "synthetic_tuned_one": (["Stanford University"], [("academic", "Toyland University")]),
              "synthetic_validation_one": ([SECRET_INSTITUTION + " Validation"] * 3, [])}
    for stem, pubs in files.items():
        (gt / f"{stem}.json").write_text(json.dumps(_gt(stem, pubs, *people[stem])), encoding="utf-8")
    (gt / "_split.json").write_text(json.dumps({"dev": ["synthetic_dev_one"], "dev2": [],
                                                "validation": ["synthetic_validation_one"], "test_frozen": []}), encoding="utf-8")
    folder = write_lexicons(tmp_path / "lex")
    monkeypatch.setenv(lexicons.ENV_LEXICONS, str(folder))
    index = VenueIndex(load_scimago(folder / "scimagojr 2025.csv"), load_core(folder / "core" / "ICORE2026.csv"),
                       fields=FIELDS, acronyms=ACRONYMS)
    monkeypatch.setattr(tier_coverage, "load_index", lambda: index)
    entries = [e for stem, html in PAGES.items() if not stem.endswith("ALL")
               for e in parse_nirf_page(html, stem.replace("Ranking", "").rstrip("0123456789"))]
    institutions = InstitutionIndex(entries)
    monkeypatch.setattr(tier_coverage, "load_institution_index", lambda: institutions)
    return data_dir


def test_counts_only_development_only_unmatched_kept_private(setup, capsys):
    assert tier_coverage.main([]) == 0
    out = capsys.readouterr().out
    assert "development GT: 2 resumes" in out
    assert "journal: 4 entries, 3 with a venue" in out  # the validation resume is never read
    assert "mapped to a tier: 2 of 3" in out and "A 1" in out and "B 1" in out
    assert "conference: 2 entries, 2 with a venue" in out and "core_rank_not_tiered 1" in out
    for secret in (SECRET_DEV, SECRET_VALIDATION, "Imaginary", "synthetic_dev_one", "Invented Person"):
        assert secret not in out
    private = (setup / "reports" / "tiers" / tier_coverage.UNMATCHED_VENUES).read_text(encoding="utf-8")
    assert SECRET_DEV in private and SECRET_VALIDATION not in private
    assert "National Conference" not in private  # matched, only its rank is not tiered
    assert "education: 3 entries, 3 with an institution" in out and "mapped to a tier: 2 of 3" in out
    assert "experience academic: 2 entries" in out and "ambiguous 1" in out
    assert "experience industry: 1 entries" in out and "global_seed 1" in out
    for secret in (SECRET_INSTITUTION, "Toyland", "Toy Company"):
        assert secret not in out
    unlisted = (setup / "reports" / "tiers" / tier_coverage.UNMATCHED_INSTITUTIONS).read_text(encoding="utf-8")
    assert SECRET_INSTITUTION in unlisted and "Validation" not in unlisted and "Toy Company" not in unlisted


def test_a_field_can_be_given(setup, capsys):
    assert tier_coverage.main(["--field", "economics"]) == 0
    assert "field: economics" in capsys.readouterr().out
    assert tier_coverage.main(["--field", "no_such_field"]) == 2
