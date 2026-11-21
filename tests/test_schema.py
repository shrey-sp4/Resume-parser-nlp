"""Schema files load, and accept valid examples and reject invalid ones."""

from __future__ import annotations

import copy

import pytest
from jsonschema import Draft202012Validator

from resume_parser.schema import SCHEMA_DIR, read_json, schema_errors

VALID_MAIN = {
    "resume_file": "xyz.pdf",
    "candidate_name": "Test Person",
    "education": [
        {"degree": "Ph.D.", "degree_level": "doctorate", "stream": "Physics", "institution": "Some University"}
    ],
    "experience": [
        {"job_title": "Lecturer", "institution": "Some College", "dates": "2019 - 2021", "experience_type": "academic"}
    ],
    "publications": [{"title": "A paper", "type": "journal", "year": "2020", "venue": "Some Journal"}],
    "stated_totals": {"journal": 1, "conference": None, "total": 1},
    "notes": "",
}

VALID_META = {
    "schema_version": "1.0",
    "resume_id": "r_0123abcd",
    "resume_file": "xyz.pdf",
    "decision": "NEEDS_REVIEW",
    "needs_review": True,
    "flags": [{"section": "education", "code": "UNASSIGNED_TARGET_LINES", "detail": ""}],
    "section_scores": {"personal": 1.0, "education": 0.9, "experience": None, "publications": 0.8},
    "personal_extra": {"emails": [], "phones": [], "phone_digits": [], "links": []},
    "provenance": {"education[0]": {"page": 1, "lines": ["p001_l0010", "p001_l0012"], "raw_text": "",
                                    "year": "2016-2021", "grade": None, "method": "anchor_split"}},
    "excluded": {"school_level_education": [], "non_paper_publications": []},
    "parsed_dates": {"experience[0]": {"start": "2016-07", "end": None}},
    "stage_report": {
        "stage1": {
            "status": "WARN",
            "metrics": {},
            "method_used": "per_unit",
            "methods_tried": [],
            "per_unit": {
                "page_001": {
                    "method_used": "pdfminer_layout",
                    "methods_tried": [
                        {"method": "pdfplumber_words_tables", "score": 0.5, "status": "BELOW_THRESHOLD", "reasons": [],
                         "details": {"overlap": 0.62, "glued_rate": 0.0}},
                        {"method": "pdfminer_layout", "score": 0.95, "status": "ACCEPTED", "reasons": [], "details": {}},
                    ],
                    "status": "OK",
                }
            },
            "reasons": [{"code": "no_text_layer", "detail": "page_002", "section": "document"}],
        }
    },
}

VALID_INDEX = {
    "schema_version": "1.0",
    "resumes": [{"decision": "ACCEPT", "flags_count": 0, "resume_file": "xyz.pdf", "resume_id": "r_0123abcd"}],
}


@pytest.mark.parametrize("name", ["resume.schema.json", "meta.schema.json", "run_index.schema.json"])
def test_schema_files_are_valid_schemas(name):
    Draft202012Validator.check_schema(read_json(SCHEMA_DIR / name))


def test_valid_examples_pass():
    assert schema_errors(VALID_MAIN, "resume.schema.json") == []
    assert schema_errors(VALID_META, "meta.schema.json") == []
    assert schema_errors(VALID_INDEX, "run_index.schema.json") == []


def _broken(base, change):
    obj = copy.deepcopy(base)
    change(obj)
    return obj


@pytest.mark.parametrize(
    "change",
    [
        lambda o: o.update(extra_key=1),
        lambda o: o.pop("notes"),
        lambda o: o["education"][0].update(degree_level="phd"),
        lambda o: o["education"][0].update(year="2019"),
        lambda o: o["experience"][0].update(experience_type="government"),
        lambda o: o["publications"][0].update(type="book"),
        lambda o: o["publications"][0].update(year=2020),
        lambda o: o["stated_totals"].update(total="65"),
        lambda o: o["stated_totals"].pop("journal"),
    ],
)
def test_invalid_main_is_rejected(change):
    assert schema_errors(_broken(VALID_MAIN, change), "resume.schema.json")


@pytest.mark.parametrize(
    "change",
    [
        lambda o: o.update(decision="MAYBE"),
        lambda o: o["flags"][0].update(section="skills"),
        lambda o: o["stage_report"].update(stage9=o["stage_report"]["stage1"]),
        lambda o: o["stage_report"]["stage1"].update(status="BAD"),
        lambda o: o.update(resume_id="Test Person"),
        lambda o: o.update(timestamp="2026-01-01"),
    ],
)
def test_invalid_meta_is_rejected(change):
    assert schema_errors(_broken(VALID_META, change), "meta.schema.json")


def test_invalid_index_is_rejected():
    bad = _broken(VALID_INDEX, lambda o: o["resumes"][0].update(flags_count=-1))
    assert schema_errors(bad, "run_index.schema.json")


def test_errors_do_not_show_values():
    bad = _broken(VALID_MAIN, lambda o: o.update(candidate_name=["Secret Name"]))
    errors = schema_errors(bad, "resume.schema.json")
    assert errors == ["candidate_name: type"]
