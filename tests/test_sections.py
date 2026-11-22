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


_BLOCKED = ("teach", "course", "subject", "project", "responsib", "service", "administr", "talk", "present",
            "poster", "award", "honour", "honor", "reference", "referees", "patent", "workshop", "training",
            "interest", "skill", "summary", "objective", "profile", "membership")


# job headings that contain a blocked word but are still about jobs ("teaching ... experience",
# "present position"); each one was checked by hand, so the word list keeps guarding the rest
_JOB_HEADINGS_WITH_BLOCKED_WORDS = {
    "research and teaching experience", "research & teaching experience", "teaching and industrial experience",
    "teaching and industry experience", "present position", "present employment", "employment profile",
}
NEW_EXPERIENCE_HEADINGS = (
    "post doctoral experience", "post doctoral research experience", "post-doctoral research experience",
    "research and teaching experience", "research & teaching experience", "teaching and industrial experience",
    "teaching and industry experience", "present position", "current position", "current appointment",
    "present employment", "previous positions", "academic and professional positions", "employment profile",
)


def test_new_experience_headings_map_only_as_whole_headings():
    fuzzy_min = load_thresholds()["stage4_normalize"]["fuzzy_min"]
    for heading in NEW_EXPERIENCE_HEADINGS:
        assert exact_alias(heading.title()) == ("experience", "experience"), heading
        assert exact_alias("2. " + heading.upper() + ":") == ("experience", "experience"), heading
    for heading in ("Research Interests", "Professional Activities", "Academic Service", "Career Objective"):
        found = exact_alias(heading) or fuzzy_alias(heading, fuzzy_min)
        assert found is None or found[0] != "experience", heading
    # inside a longer line an exact-only alias is neither an exact nor a fuzzy match
    for line in ("Present Position: Assistant Professor, Hillcrest University",
                 "Current position held since 2019 in the Department of Physics"):
        assert exact_alias(line) is None
        found = fuzzy_alias(line, fuzzy_min)
        assert found is None or found[0] != "experience", line


def test_new_experience_heading_starts_an_experience_section(tmp_path):
    lines = (PERSONAL_TOP + [("heading", "EDUCATION")] + EDU_LINES + [("heading", "PRESENT POSITION")]
             + EXP_LINES[:1] + [("heading", "PREVIOUS POSITIONS")] + EXP_LINES[1:])
    _stage3, stage4 = _run(synth.make_lines_pdf(tmp_path / "positions.pdf", lines))
    names = [name for name, _heading in _sections(stage4)]
    assert names.count("experience") == 2 and "other" not in names


def _exact_only() -> dict[str, list[str]]:
    from resume_parser.schema import load_config

    return load_config("section_aliases.json")["exact_only"]


def test_exact_only_aliases_map_exactly_and_never_fuzzily():
    from resume_parser.headings import fuzzy_index

    fuzzy_min = load_thresholds()["stage4_normalize"]["fuzzy_min"]
    for section, aliases in _exact_only().items():
        for alias in aliases:
            assert exact_alias(alias.upper() + ":") == (section, section), alias
            assert norm_heading(alias) not in fuzzy_index(), alias  # new, not a copy of an old alias
            found = fuzzy_alias(alias, fuzzy_min)
            assert found is None or found[2] < 100, alias  # fuzzy match never sees it


def test_exact_only_aliases_are_distinct_and_keep_the_rules():
    seen = set()
    for section, aliases in _exact_only().items():
        assert section in ("personal", "education", "experience", "publications")
        for alias in aliases:
            key = norm_heading(alias)
            assert key not in seen, alias
            seen.add(key)
            if key not in _JOB_HEADINGS_WITH_BLOCKED_WORDS:
                assert not any(word in key for word in _BLOCKED), alias  # jobs only; no other-section words
    assert exact_alias("Positions") is None and exact_alias("Preprints") is None  # left out on purpose
    assert exact_alias("Courses Taught") == ("other", "teaching")  # an old alias keeps its section


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


def test_missing_experience_is_a_warning(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION")] + EDU_LINES + [("heading", "PUBLICATIONS")] + PUB_LINES
    _stage3, stage4 = _run(synth.make_lines_pdf(tmp_path / "noexp.pdf", lines))
    assert "missing_target_section:experience" in _codes(stage4)
    assert "missing_target_section:education" not in _codes(stage4)
    assert stage4.status.value == "WARN"
    flags = [f for f in flags_from(stage4) if f["code"].startswith("missing_target_section")]
    assert flags == [{"section": "experience", "code": "missing_target_section:experience",
                      "detail": "stage4"}]


def test_missing_publications_is_information_only(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION")] + EDU_LINES + [("heading", "WORK EXPERIENCE")] + EXP_LINES
    _stage3, stage4 = _run(synth.make_lines_pdf(tmp_path / "nopub.pdf", lines))
    assert "missing_target_section:publications" in _codes(stage4)
    assert stage4.status.value == "OK" and not flags_from(stage4)


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


# ---------------------------------------------------------------- inline headings


def _line(text, i=1, **extra):
    return {"id": f"p001_l{i:04d}", "page": 1, "text": text, "bbox": [56, 100, 300, 110], "font_size": 10,
            "bold": 0.0, "in_table": False, **extra}


def test_split_inline_headings_rule():
    from resume_parser.sections import split_inline_headings

    out = split_inline_headings([_line("Work Experience: 12 years in teaching and research")])
    assert [(x["id"], x["text"], x.get("inline_heading"), x.get("no_heading")) for x in out] == [
        ("p001_l0001_h", "Work Experience", True, None),
        ("p001_l0001", "12 years in teaching and research", None, True)]
    assert len(split_inline_headings([_line("Publications - 12 papers in journals")])) == 2  # spaced dash
    # aliases of other sections never split inline; nor do non-aliases or a bare heading
    for text in ("Research interests: gene networks", "Summary: a short profile", "Skills - Python, R",
                 "Email: someone@example.org", "Experience:", "Self-funded experience in labs",
                 "Duration: 2019 - 2021"):
        assert split_inline_headings([_line(text)]) == [_line(text)], text
    cell = _line("Education: Ph.D.", in_table=True)
    assert split_inline_headings([cell]) == [cell]  # table cells are left to the table rule


def test_inline_heading_cv_is_routed_and_keeps_every_line(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION")] + EDU_LINES + \
        [("heading", "WORK EXPERIENCE: 3 years of teaching")] + EXP_LINES + \
        [("heading", "PUBLICATIONS")] + PUB_LINES
    record = run_pipeline(synth.make_lines_pdf(tmp_path / "inline.pdf", lines), stages=default_stages()[:5])
    stage4 = record.results["stage4"]
    exp = next(s for s in stage4.data["sections"] if s["name"] == "experience")
    assert exp["heading_raw"] == "WORK EXPERIENCE" and exp["heading_line"]["inline_heading"]
    assert [x["text"] for x in exp["lines"]] == ["3 years of teaching"] + [t for _, t in EXP_LINES]
    assert "lines_lost_or_duplicated" not in _codes(stage4)


def test_other_aliases_inside_an_education_entry_do_not_open_a_section(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION"), EDU_LINES[0], ("body", "Research interests: gene networks"),
                            ("body", "Summary: thesis on sparse graphs"), EDU_LINES[1]] +         [("heading", "WORK EXPERIENCE")] + EXP_LINES
    _stage3, stage4 = _run(synth.make_lines_pdf(tmp_path / "fields.pdf", lines))
    assert [s["name"] for s in stage4.data["sections"]] == ["personal", "education", "experience"]
    edu = stage4.data["sections"][1]
    assert [x["text"] for x in edu["lines"]] == [EDU_LINES[0][1], "Research interests: gene networks",
                                                 "Summary: thesis on sparse graphs", EDU_LINES[1][1]]


def test_inline_experience_line_splits_into_heading_and_plain_content(tmp_path):
    """A styled inline heading: the content part keeps the bold caps style but is not a heading."""
    lines = PERSONAL_TOP + [("heading", "EDUCATION")] + EDU_LINES + [("heading", "EXPERIENCE: TWELVE YEARS")] + EXP_LINES
    stage3, stage4 = _run(synth.make_lines_pdf(tmp_path / "styled.pdf", lines))
    exp = next(s for s in stage4.data["sections"] if s["name"] == "experience")
    assert exp["heading_line"]["inline_heading"] and exp["heading_raw"] == "EXPERIENCE"
    content = exp["lines"][0]
    assert content["text"] == "TWELVE YEARS" and content["no_heading"] and content["bold"] == 1.0
    assert len(stage3.data["blocks"]) == 3  # personal, education, experience: no extra block


# ---------------------------------------------------------------- table cells as headings

EDU_TABLE = [
    ["Course", "Board / University", "Year"],
    ["Ph.D.", "Northfield Institute of Science", "2021"],
    ["M.Sc.", "Lakeside University", "2016"],
]


def _cell(text, col, row_id="p001_t1r00", i=1):
    return _line(text, i, in_table=True, cell_row=0, cell_col=col, row_id=row_id)


def _table_headings(cells):
    from resume_parser.sections import find_headings

    p = dict(load_thresholds()["stage3_sections"], fuzzy_min=90)
    headings, _unknown = find_headings(cells, p, use_look=True)
    return {cells[i]["text"]: h[0] for i, h in headings.items()}


def test_column_header_row_is_never_a_section_heading():
    row = [_cell("Education", 0, i=1), _cell("Institution", 1, i=2), _cell("Year", 2, i=3)]
    assert _table_headings(row) == {}


def test_row_heading_needs_a_target_alias_and_content_beside_it():
    with_content = [_cell("Education", 0, i=1), _cell("Ph.D. in Physics, Westbrook University, 2019", 1, i=2)]
    assert _table_headings(with_content) == {"Education": "education"}
    assert _table_headings([_cell("Experience", 0)]) == {"Experience": "experience"}  # a title row
    other_alias = [_cell("Skills", 0, i=1), _cell("Python, R, statistics and teaching", 1, i=2)]
    assert _table_headings(other_alias) == {}  # not a target section
    not_leftmost = [_cell("2019", 0, i=1), _cell("Publications", 1, i=2)]
    assert _table_headings(not_leftmost) == {}


def test_education_table_with_a_header_row_stays_in_education(tmp_path):
    pdf = synth.make_heading_table_pdf(tmp_path / "edu_table.pdf", "Education", EDU_TABLE,
                                       [("heading", "Work Experience")] + EXP_LINES)
    _stage3, stage4 = _run(pdf)
    assert [s["name"] for s in stage4.data["sections"]] == ["personal", "education", "experience"]
    edu = stage4.data["sections"][1]
    assert [x["text"] for x in edu["lines"]] == [c for row in EDU_TABLE for c in row]


def test_two_column_table_cv_still_splits_at_row_headings(tmp_path):
    _stage3, stage4 = _run(synth.make_table_headings_pdf(tmp_path / "t.pdf"))
    assert [s["name"] for s in stage4.data["sections"]] == ["personal", "education", "experience"]


# ---------------------------------------------------------------- content guess on wrapped entries


def _block(heading, texts):
    lines = [{"id": f"p001_l{i:04d}", "page": 1, "text": t, "bbox": [56 + (16 if i % 2 else 0), 100 + 14 * i, 500,
              110 + 14 * i], "font_size": 10, "bold": 0.0, "in_table": False, "row_id": f"r{i}", "segment_index": 0}
             for i, t in enumerate(texts)]
    return {"block_id": "block_002", "heading_raw": heading, "heading_line": {"id": "h"}, "lines": lines}


def _params():
    t = load_thresholds()
    return {**t["stage4_normalize"], "content_min_share": t["stage3_sections"]["content_min_share"],
            "entry_params": t["stage5_entities"]}


WRAPPED = ["1. M. Testwood, R. Doe, Modelling gene networks with",  # each citation wraps over two lines
           "sparse graphs, Invented Letters 4, 2019.",
           "2. J. Fakely, A. Roe, Thin films that do not exist in",
           "any real lab, Invented Physics Letters 12, 2021.",
           "3. L. Samplestone, Prices in an invented market and",
           "its toy traders, Review of Fictional Economics, 2018."]


def test_wrapped_citations_are_found_by_content():
    from resume_parser.normalize import content_guess_method

    block = _block("Things I Wrote Over The Years", WRAPPED)
    out = content_guess_method((block, _params())).data
    assert out is not None and out["name"] == "publications" and out["confidence"] >= 0.6


def test_wrapped_citations_need_the_share_and_no_other_heading():
    from resume_parser.normalize import content_guess_method

    mixed = _block("Things I Wrote Over The Years", WRAPPED[:2] + ["Plain words about hobbies.", "More plain words.",
                                                                  "Even more plain words here.", "And a last line."])
    assert content_guess_method((mixed, _params())).data is None  # under 60% of entries are citations
    other = _block("Work Experience", WRAPPED)  # the heading is an alias of another section
    assert content_guess_method((other, _params())).data is None
    # a year and a venue word without an author list is not enough (talks, events, ...)
    # (each item wraps, so no single line looks like a citation and only the entry test decides)
    events = _block("Things I Took Part In", ["1. Invited talk at the Toy Conference", "on Graphs in Fakeville, 2019.",
                                              "2. Poster at the Placeholder Symposium", "held in Lakeside, 2020.",
                                              "3. Panel at the Invented Journal day", "in Northfield, 2021."])
    from resume_parser.headings import content_guess

    assert content_guess(events["lines"], _params()["content_min_share"]) is None
    assert content_guess_method((events, _params())).data is None
    # an entry longer than max_citation_chars is several items run together, never one citation
    long_one = _block("Things I Wrote Over The Years", ["M. Testwood, R. Doe, " + "a very long text " * 60 + "2019."])
    assert content_guess_method((long_one, _params())).data is None
