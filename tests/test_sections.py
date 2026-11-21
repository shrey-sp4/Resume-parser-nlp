"""Stages 3 and 4: headings, blocks, the canonical document and its checks."""

from __future__ import annotations

from rapidfuzz import fuzz

import make_synthetic_resumes as synth
from make_synthetic_resumes import EDU_LINES, EXP_LINES, PERSONAL_TOP, PUB_LINES
from resume_parser.headings import exact_alias, fuzzy_alias, line_kind, norm_heading
from resume_parser.pipeline import default_stages, flags_from, run_pipeline
from resume_parser.schema import load_thresholds


def _run(pdf):
    record = run_pipeline(pdf, stages=default_stages()[:5])
    return record.results["stage3"], record.results["stage4"]


def _sections(stage4) -> list[tuple[str, str | None]]:
    return [(s["name"], s["heading_raw"]) for s in stage4.data["sections"]]


def _codes(result) -> list[str]:
    return [r.code for r in result.reasons]


# ---------------------------------------------------------------- headings


def test_heading_normalisation():
    assert norm_heading("2. EDUCATIONAL QUALIFICATIONS:") == "educational qualifications"
    assert norm_heading("Conference Papers (5)") == "conference papers"
    assert norm_heading("  iv) Work   Experience - ") == "work experience"


def test_aliases_follow_the_rules():
    assert exact_alias("WORK EXPERIENCE")[0] == "experience"
    assert exact_alias("Teaching Experience")[0] == "experience"
    assert exact_alias("Courses Taught") == ("other", "teaching")
    assert exact_alias("Project Experience") == ("other", "projects")
    for heading in ("Papers Presented", "Presentations", "Talks", "Posters", "Conference Presentations"):
        assert exact_alias(heading)[0] == "other", heading
    assert exact_alias("Book Chapters")[0] == "publications"
    assert exact_alias("12th") is None and exact_alias("Research Associate") is None  # entry labels


def test_fuzzy_uses_ratio_on_the_whole_heading():
    fuzzy_min = load_thresholds()["stage4_normalize"]["fuzzy_min"]
    assert fuzz.ratio("project experience", "experience") < fuzzy_min
    found = fuzzy_alias("Project Experiences", fuzzy_min)
    assert found is not None and found[0] == "other"  # never experience
    assert fuzzy_alias("Project Experience Details", fuzzy_min) is None or \
        fuzzy_alias("Project Experience Details", fuzzy_min)[0] != "experience"
    assert fuzzy_alias("EDUCATON", fuzzy_min)[0] == "education"  # a typo still maps


def test_line_kinds():
    assert line_kind(PUB_LINES[0][1]) == "citation"
    assert line_kind(EDU_LINES[0][1]) == "degree"
    assert line_kind(EXP_LINES[0][1]) == "date"
    assert line_kind("Python, R, statistics, teaching") is None


# ---------------------------------------------------------------- the fake resumes


def test_fake_resumes_are_routed_by_vocabulary(synthetic_pdfs):
    expected = [
        [("personal", None), ("education", "EDUCATION"), ("experience", "WORK EXPERIENCE"),
         ("publications", "PUBLICATIONS"), ("other", "SKILLS")],
        [("personal", None), ("education", "Education"), ("experience", "Professional Experience"),
         ("publications", "Journal Publications"), ("publications", "Conference Papers")],
        [("personal", None), ("education", "Academic Qualifications"), ("experience", "Employment History"),
         ("publications", "Research Output"), ("other", "Courses Taught")],
    ]
    for pdf, want in zip(synthetic_pdfs, expected):
        stage3, stage4 = _run(pdf)
        assert stage3.method_used == "heading_vocabulary" and stage3.status.value == "OK"
        assert _sections(stage4) == want
        assert stage4.status.value == "OK" and not flags_from(stage4)
    other = _run(synthetic_pdfs[2])[1].data["sections"][-1]
    assert other["original_name"] == "teaching"


def test_name_line_stays_in_the_personal_block(synthetic_pdfs):
    stage3, _ = _run(synthetic_pdfs[0])
    personal = stage3.data["blocks"][0]
    assert personal["heading_line"] is None
    assert personal["lines"][0]["text"] == "Mira Testwood"


def test_unknown_heading_found_by_look_and_routed_by_content(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION")] + EDU_LINES + [("heading", "SCHOLARLY WORKS")] + PUB_LINES
    stage3, stage4 = _run(synth.make_lines_pdf(tmp_path / "unknown.pdf", lines))
    assert stage3.method_used == "content_patterns"
    vocab = next(a for a in stage3.methods_tried if a.method == "heading_vocabulary")
    assert dict(vocab.details)["leaked_lines"] == 2  # citations inside education
    assert _sections(stage4)[-1] == ("publications", "SCHOLARLY WORKS")
    block = stage4.data["sections"][-1]["block_id"]
    assert stage4.per_unit[block].method_used == "content_guess"
    assert "section_from_content" in _codes(stage4) and stage4.status.value == "WARN"
    assert len(stage3.data["unknown_headings"]) == 1


def test_heading_typo_is_matched_by_fuzzy_ratio(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATON")] + EDU_LINES + [("heading", "WORK EXPERIENCE")] + EXP_LINES
    _stage3, stage4 = _run(synth.make_lines_pdf(tmp_path / "typo.pdf", lines))
    assert _sections(stage4)[1] == ("education", "EDUCATON")
    assert stage4.per_unit[stage4.data["sections"][1]["block_id"]].method_used == "fuzzy_alias_match"


def test_headings_in_table_cells(tmp_path):
    stage3, stage4 = _run(synth.make_table_headings_pdf(tmp_path / "table.pdf"))
    names = [s["name"] for s in stage4.data["sections"]]
    assert names == ["personal", "education", "experience"]
    edu = stage4.data["sections"][1]
    assert [line["text"] for line in edu["lines"]] == [EDU_LINES[0][1], EDU_LINES[1][1]]


def test_unknown_heading_with_plain_content_is_kept_as_other(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION")] + EDU_LINES + [("heading", "MISCELLANEOUS NOTES")] + \
        [("body", "Enjoys long walks and quiet evenings"), ("body", "Plays the cello in a local group")]
    _stage3, stage4 = _run(synth.make_lines_pdf(tmp_path / "other.pdf", lines))
    last = stage4.data["sections"][-1]
    assert (last["name"], last["original_name"]) == ("other", "miscellaneous notes")
    assert stage4.per_unit[last["block_id"]].method_used == "keep_as_other_with_warning"
    assert "heading_kept_as_other" in _codes(stage4)


def test_missing_target_section_is_information_only(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION")] + EDU_LINES + [("heading", "PUBLICATIONS")] + PUB_LINES
    _stage3, stage4 = _run(synth.make_lines_pdf(tmp_path / "noexp.pdf", lines))
    assert "missing_target_section:experience" in _codes(stage4)
    assert "missing_target_section:education" not in _codes(stage4)
    assert stage4.status.value == "OK"
    assert not [f for f in flags_from(stage4) if f["code"].startswith("missing_target_section")]


def test_no_target_section_warns(tmp_path):
    lines = PERSONAL_TOP + [("heading", "SKILLS"), ("body", "Python, R, statistics")]
    stage3, stage4 = _run(synth.make_lines_pdf(tmp_path / "none.pdf", lines))
    assert stage3.status.value == "WARN" and "no_target_section" in _codes(stage3)
    assert {"missing_target_section:education", "missing_target_section:experience",
            "missing_target_section:publications"} <= set(_codes(stage4))


def test_canonical_document_keeps_every_line_once_in_order(synthetic_pdfs, tmp_path):
    pdfs = synthetic_pdfs + [synth.make_table_headings_pdf(tmp_path / "t.pdf"),
                             synth.make_multipage_pdf(tmp_path / "m.pdf", 3)]
    for pdf in pdfs:
        record = run_pipeline(pdf, stages=default_stages()[:5])
        doc = record.results["stage4"].data
        ids = []
        for sec in doc["sections"]:
            ids += ([sec["heading_line"]["id"]] if sec["heading_line"] else []) + [x["id"] for x in sec["lines"]]
        assert ids == [x["id"] for x in record.results["stage2"].data["lines"]]
        assert "lines_lost_or_duplicated" not in _codes(record.results["stage4"])
        assert {s["name"] for s in doc["sections"]} <= {"personal", "education", "experience", "publications", "other"}


def test_sections_are_deterministic(synthetic_pdfs):
    a = run_pipeline(synthetic_pdfs[1], stages=default_stages()[:5]).results["stage4"]
    b = run_pipeline(synthetic_pdfs[1], stages=default_stages()[:5]).results["stage4"]
    assert a.data == b.data and a.to_report() == b.to_report()
