"""Stage 5, personal and education: names, contacts, entry splitting, fields and checks."""

from __future__ import annotations

import re

import make_synthetic_resumes as synth
from make_synthetic_resumes import EXP_LINES, PERSONAL_TOP
from resume_parser.cli import main as cli_main
from resume_parser.entities import education as edu
from resume_parser.entities.personal import clean_name, find_contacts, valid_name
from resume_parser.pipeline import default_stages, run_pipeline
from resume_parser.schema import load_thresholds, schema_errors
from test_sections import EDU_TABLE


def _run(pdf, through=6):
    return run_pipeline(pdf, stages=default_stages()[:through])


def _stage5(pdf):
    return _run(pdf).results["stage5"]


def _edu(stage5):
    return stage5.data["education"]


def _fields(entries):
    return [(e["degree"], e["degree_level"], e["stream"], e["institution"]) for e in entries]


# ---------------------------------------------------------------- the fake resumes


def test_fake_resumes_give_name_and_education(synthetic_pdfs):
    gt = synth.synthetic_ground_truth()
    for pdf in synthetic_pdfs:
        record = run_pipeline(pdf)
        want = gt[pdf.stem]
        assert record.main["candidate_name"] == want["candidate_name"]  # "Dr." removed for resume c
        got = record.main["education"]
        assert [e["degree_level"] for e in got] == [e["degree_level"] for e in want["education"]]
        assert [e["institution"] for e in got] == [e["institution"] for e in want["education"]]
        assert schema_errors(record.main, "resume.schema.json") == []
        assert schema_errors(record.sidecar, "meta.schema.json") == []


def test_degree_first_lines(synthetic_pdfs):
    stage5 = _stage5(synthetic_pdfs[0])
    assert stage5.per_unit["education"].method_used == "anchor_split"
    assert _fields(_edu(stage5)["entries"]) == [
        ("Ph.D.", "doctorate", "Computational Biology", "Northfield Institute of Science"),
        ("M.Sc.", "masters", "Applied Mathematics", "Lakeside University"),
        ("B.Sc.", "bachelors", "Mathematics", "Riverbend College"),
    ]
    side = _edu(stage5)["sides"][0]
    assert side["year"] == "2016-2021" and side["page"] == 1 and side["lines"]


def test_institution_first_lines(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION"),
                            ("body", "Northfield Institute of Science, 2016 - 2021"),
                            ("body", "Ph.D. in Computational Biology"),
                            ("body", "Lakeside University, 2014 - 2016"),
                            ("body", "M.Sc. in Applied Mathematics")] + [("heading", "WORK EXPERIENCE")] + EXP_LINES
    entries = _edu(_stage5(synth.make_lines_pdf(tmp_path / "inst.pdf", lines)))["entries"]
    assert _fields(entries) == [
        ("Ph.D.", "doctorate", "Computational Biology", "Northfield Institute of Science"),
        ("M.Sc.", "masters", "Applied Mathematics", "Lakeside University"),
    ]


def test_table_with_a_header_row(tmp_path):
    pdf = synth.make_heading_table_pdf(tmp_path / "t.pdf", "Education", EDU_TABLE, [("heading", "Work Experience")] + EXP_LINES)
    stage5 = _stage5(pdf)
    assert stage5.per_unit["education"].method_used == "table_row_split"
    assert _fields(_edu(stage5)["entries"]) == [
        ("Ph.D.", "doctorate", "", "Northfield Institute of Science"),
        ("M.Sc.", "masters", "", "Lakeside University"),
    ]
    assert _edu(stage5)["header_lines"] == 3


def test_school_level_rows_are_excluded(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION"),
                            ("body", "Ph.D. in Physics, Westbrook University, 2019"),
                            ("body", "Class XII (CBSE), Fakeville Public School, 2008, 89%"),
                            ("body", "SSC, Fakeville Public School, 2006")]
    record = _run(synth.make_lines_pdf(tmp_path / "school.pdf", lines), through=8)
    assert [e["degree"] for e in record.main["education"]] == ["Ph.D."]
    excluded = record.sidecar["excluded"]["school_level_education"]
    assert len(excluded) == 2 and all(x["page"] == 1 for x in excluded)


def test_thesis_and_advisor_lines_join_the_previous_entry(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION"),
                            ("body", "Ph.D. in Physics, Westbrook University, 2019"),
                            ("body", "Thesis: Phonons in fictional crystals"),
                            ("body", "Advisor: A. Roe"),
                            ("body", "M.Sc. in Physics, Eastvale University, 2014"),
                            ("body", "CGPA: 8.9/10")]
    stage5 = _stage5(synth.make_lines_pdf(tmp_path / "thesis.pdf", lines))
    data = _edu(stage5)
    assert [e["degree"] for e in data["entries"]] == ["Ph.D.", "M.Sc."]
    assert "Advisor" in data["sides"][0]["raw_text"] and "Thesis" in data["sides"][0]["raw_text"]
    assert data["sides"][1]["grade"] == "CGPA: 8.9/10"


def test_paragraph_split_on_vertical_gaps():
    def line(i, top, text):
        return {"id": f"p001_l{i:04d}", "page": 1, "text": text, "bbox": [56, top, 300, top + 10],
                "font_size": 10, "bold": 0.0, "in_table": False, "row_id": f"r{i}", "segment_index": 0}
    lines = [line(1, 100, "Northfield Institute of Science"), line(2, 112, "Research degree in biology"),
             line(3, 150, "Lakeside University"), line(4, 162, "Course work in mathematics")]
    payload = {"lines": lines, "kinds": edu.mark(lines), "params": load_thresholds()["stage5_entities"]}
    out = edu.paragraph_split(payload).data
    assert [e["institution"] for e in out["entries"]] == ["Northfield Institute of Science", "Lakeside University"]


def test_implausible_year_fails_the_entry_check(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION"), ("body", "Ph.D. in Physics, Westbrook University, 2099"),
                            ("body", "M.Sc. in Physics, Eastvale University, 2014")]
    stage5 = _stage5(synth.make_lines_pdf(tmp_path / "year.pdf", lines))
    tried = stage5.per_unit["education"].methods_tried[0]
    assert dict(tried.details)["entries_ok"] == 1 and tried.score == 0.5
    assert stage5.per_unit["education"].status.value == "WARN"


# ---------------------------------------------------------------- names and contacts


def test_honorifics_and_trailing_degrees_are_removed():
    assert clean_name("Dr. Lena Samplestone") == "Lena Samplestone"
    assert clean_name("Prof. (Dr.) Mira Testwood, Ph.D.") == "Mira Testwood"
    assert clean_name("Name: Jonas Fakely") == "Jonas Fakely"
    assert valid_name("Mira A. Testwood") and not valid_name("Mira") and not valid_name("Mira Testwood 2")


def test_name_split_over_two_lines(tmp_path):
    lines = [("name", "MIRA"), ("name", "TESTWOOD"), ("contact", "mira.testwood@example.org"),
             ("heading", "EDUCATION"), ("body", "Ph.D. in Physics, Westbrook University, 2019")]
    stage5 = _stage5(synth.make_lines_pdf(tmp_path / "split.pdf", lines))
    assert stage5.data["personal"]["candidate_name"] == "MIRA TESTWOOD"


def test_name_from_email_when_no_name_line(tmp_path):
    lines = [("contact", "mira.testwood@example.org | +00 0000 000 001"), ("heading", "EDUCATION"),
             ("body", "Ph.D. in Physics, Westbrook University, 2019")]
    stage5 = _stage5(synth.make_lines_pdf(tmp_path / "email.pdf", lines))
    assert stage5.per_unit["personal"].method_used == "from_email"
    assert stage5.data["personal"]["candidate_name"] == "Mira Testwood"
    assert "name_from_email" in [r.code for r in stage5.reasons]


def test_name_from_file_name_is_the_last_resort(tmp_path):
    lines = [("contact", "+00 0000 000 001"), ("heading", "EDUCATION"),
             ("body", "Ph.D. in Physics, Westbrook University, 2019")]
    stage5 = _stage5(synth.make_lines_pdf(tmp_path / "mira_testwood_cv.pdf", lines))
    assert stage5.per_unit["personal"].method_used == "from_file_name"
    assert stage5.data["personal"]["candidate_name"] == "Mira Testwood"
    assert "name_from_file_name" in [r.code for r in stage5.reasons]


def test_contacts_go_to_the_sidecar_only(synthetic_pdfs):
    record = run_pipeline(synthetic_pdfs[2])
    extra = record.sidecar["personal_extra"]
    assert extra["emails"] == ["lena.samplestone@example.org"]
    assert extra["phone_digits"] == ["000000000003"] and extra["links"] == ["orcid.org/0000-0000-0000-0000"]
    assert "example.org" not in str(record.main)
    contacts = find_contacts([{"text": "Tel: +91 98765 43210, email: a.b@example.org, 2016 - 2021"}])
    assert contacts["phone_digits"] == ["919876543210"] and contacts["emails"] == ["a.b@example.org"]


# ---------------------------------------------------------------- determinism and output


def test_stage5_is_deterministic(synthetic_pdfs):
    a, b = _stage5(synthetic_pdfs[1]), _stage5(synthetic_pdfs[1])
    assert a.data == b.data and a.to_report() == b.to_report()


def test_cli_prints_ids_only(data_dir, capsys):
    synth.build_all(data_dir / "pdfs")
    assert cli_main(["run", "--allow-stubs"]) == 0
    out = capsys.readouterr().out
    for line in out.strip().splitlines():
        assert re.match(r"^(r_[0-9a-f]{8} \w+ flags=\d+ unknown_headings=\d+.*|done: .*|groups: .*)$", line), line
    for gt in synth.synthetic_ground_truth().values():
        assert gt["candidate_name"] not in out


# ---------------------------------------------------------------- name search (all personal blocks, page 1)

EDU_BLOCK = [("heading", "EDUCATION"), ("body", "Ph.D. in Physics, Westbrook University, 2019")]


def _name(tmp_path, lines, file="cv.pdf"):
    stage5 = _stage5(synth.make_lines_pdf(tmp_path / file, lines))
    return stage5.data["personal"]["candidate_name"], stage5.per_unit["personal"].method_used


def test_name_in_a_later_personal_block_after_a_cv_heading(tmp_path):
    lines = [("heading", "CURRICULUM VITAE"), ("name", "Mira Testwood"),
             ("contact", "mira.testwood@example.org")] + EDU_BLOCK
    assert _name(tmp_path, lines) == ("Mira Testwood", "top_lines")


def test_name_sharing_a_line_with_contacts(tmp_path):
    lines = [("name", "Mira Testwood | mira.testwood@example.org | +00 0000 000 001")] + EDU_BLOCK
    assert _name(tmp_path, lines) == ("Mira Testwood", "top_lines")
    lines = [("name", "Mira Testwood  Email: mira.testwood@example.org")] + EDU_BLOCK
    assert _name(tmp_path, lines, "b.pdf") == ("Mira Testwood", "top_lines")


def test_name_with_trailing_degree_and_initials(tmp_path):
    lines = [("name", "M K Testwood, Ph.D."), ("contact", "mira.testwood@example.org")] + EDU_BLOCK
    assert _name(tmp_path, lines) == ("M K Testwood", "top_lines")


def test_lower_case_name_accepted_when_bold(tmp_path):
    lines = [("name", "mira testwood"), ("contact", "mira.testwood@example.org")] + EDU_BLOCK
    assert _name(tmp_path, lines) == ("mira testwood", "top_lines")


def test_university_name_in_large_bold_is_never_the_name(tmp_path):
    lines = [("name", "WESTBROOK UNIVERSITY"), ("contact", "Department of Physics"),
             ("body", "Mira Testwood"), ("contact", "mira.testwood@example.org")] + EDU_BLOCK
    assert _name(tmp_path, lines) == ("Mira Testwood", "top_lines")


def test_lines_that_are_never_names():
    from resume_parser.entities.personal import candidate_text

    for text in ("Westbrook University", "Work Experience", "Fakeville Road, Sector 4", "Ph.D. Physics",
                 "Mira Testwood 2019", "Curriculum Vitae", "Resume"):
        assert candidate_text(text) is None, text
    assert candidate_text("Dr. Mira Testwood, Ph.D.") == "Mira Testwood"


# ---------------------------------------------------------------- sections routed by content only

# degree words in lines that are not degrees (no teaching words: those lines are continuations anyway)
MENTIONS = [("body", "Member of the M.Sc. and B.Sc. review board"), ("body", "Reviewer for the Ph.D. evaluation panel"),
            ("body", "Ph.D. in Physics, Westbrook University, 2019")]


def test_content_routed_education_keeps_only_real_entries(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION"), ("body", "M.Sc. in Physics, Eastvale University, 2014"),
                            ("heading", "MENTORING NOTES")] + MENTIONS
    record = _run(synth.make_lines_pdf(tmp_path / "content.pdf", lines), through=6)
    stage4 = record.results["stage4"]
    mentoring = stage4.data["sections"][-1]
    assert mentoring["name"] == "education" and stage4.per_unit[mentoring["block_id"]].method_used == "content_guess"
    data = record.results["stage5"].data["education"]
    assert [e["degree"] for e in data["entries"]] == ["M.Sc.", "Ph.D."]  # the two mentions are dropped
    assert data["dropped_from_content"] == 2


def test_heading_routed_education_is_never_filtered(tmp_path):
    lines = PERSONAL_TOP + [("heading", "EDUCATION")] + MENTIONS
    data = _stage5(synth.make_lines_pdf(tmp_path / "heading.pdf", lines)).data["education"]
    assert len(data["entries"]) == 3 and data["dropped_from_content"] == 0


def test_stream_written_right_after_the_degree():
    from resume_parser.field_norm import find_degree, find_stream

    def stream(text):
        return find_stream(text, find_degree(text)[3])

    assert stream("B.Tech. Computer Science, Southgate College, 2012") == "Computer Science"
    assert stream("MSc Physics from IIT Bombay, 2015") == "Physics"
    assert stream("MBA Finance and Marketing") == "Finance and Marketing"
    assert stream("Ph.D. Westbrook University, 2019") == ""  # the start of an institution name
    assert stream("M.Sc. 2014, Lakeside University") == ""
    assert stream("Ph.D. thesis submitted") == ""  # not capitalised
    assert stream("Ph.D. in Computational Biology, Northfield Institute") == "Computational Biology"
    # a word, then a bracket or colon and more capitalised words before the institution: unsure, so empty
    assert stream("M.Tech. Energy (thesis on Solar Grid Storage) Eastfield University") == ""
    assert stream("M.Tech. Energy: with Applied Thermal Design, 2016") == ""


def test_stream_from_the_cell_after_the_degree_in_a_table_without_header():
    def cell(i, row, col, text):
        return {"id": f"p001_l{i:04d}", "page": 1, "text": text, "bbox": [56 + 120 * col, 100 + 20 * row, 170 + 120 * col,
                110 + 20 * row], "font_size": 10, "bold": 0.0, "in_table": True, "row_id": f"t1r{row}",
                "cell_row": row, "cell_col": col, "segment_index": col}
    lines = [cell(1, 0, 0, "M.Tech."), cell(2, 0, 1, "Power and Energy Systems"), cell(3, 0, 2, "Eastfield University"),
             cell(4, 0, 3, "2016"),
             cell(5, 1, 0, "B.E."), cell(6, 1, 1, "Electrical Engineering, Westbrook Institute of Technology, 2013"),
             cell(7, 2, 0, "B.Sc."), cell(8, 2, 1, "Lakeside College"), cell(9, 2, 2, "2010")]
    payload = {"lines": lines, "kinds": edu.mark(lines), "params": load_thresholds()["stage5_entities"]}
    out = edu.table_row_split(payload).data
    assert [e["stream"] for e in out["entries"]] == ["Power and Energy Systems", "Electrical Engineering", ""]
    assert out["entries"][1]["institution"] == "Westbrook Institute of Technology"


def test_teaching_and_supervision_lines_never_start_an_entry():
    texts = ["Ph.D. in Physics, Westbrook University, 2019", "Courses taught: M.Sc. Physics, B.Sc. Mathematics",
             "Ph.D. students supervised: 3", "M.Sc. in Physics, Lakeside University, 2014"]
    lines = [{"id": f"p001_l{i:04d}", "page": 1, "text": t, "bbox": [56, 100 + 14 * i, 400, 110 + 14 * i],
              "font_size": 10, "bold": 0.0, "in_table": False, "row_id": f"r{i}", "segment_index": 0}
             for i, t in enumerate(texts)]
    kinds = edu.mark(lines)
    assert kinds == ["degree", "continuation", "continuation", "degree"]
    out = edu.anchor_split({"lines": lines, "kinds": kinds, "params": load_thresholds()["stage5_entities"]}).data
    assert [e["degree"] for e in out["entries"]] == ["Ph.D.", "M.Sc."]
    # a teaching word with a year or an institution is never only a mention
    assert edu.mark([dict(lines[0], text="M.Tech. coursework, Southgate College, 2012")]) == ["degree"]


def test_name_at_the_start_of_a_longer_line_is_a_fallback(tmp_path):
    lines = [("contact", "Mira Testwood 12 Lake Road Fakeville Tel 0000 000 001"), ("heading", "EDUCATION"),
             ("body", "Ph.D. in Physics, Westbrook University, 2019")]
    name, method = _name(tmp_path, lines, "x1.pdf")
    assert (name, method) == ("Mira Testwood", "leading_words")
    # never when a normal line passed, and never when the leading words fail a rule
    assert _name(tmp_path, PERSONAL_TOP + EDU_BLOCK, "x2.pdf")[1] == "top_lines"
    lines = [("contact", "Westbrook University Physics Department 2019"), ("heading", "EDUCATION"),
             ("body", "Ph.D. in Physics, Westbrook University, 2019")]
    assert _name(tmp_path, lines, "mira_testwood_cv.pdf")[1] == "from_file_name"
