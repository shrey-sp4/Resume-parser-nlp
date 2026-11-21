"""Stage 5, experience: entry splitting, fields, dates, typing and checks. Synthetic data only."""

from __future__ import annotations

import re

import make_synthetic_resumes as synth
from make_synthetic_resumes import EDU_LINES, PERSONAL_TOP
from resume_parser.cli import main as cli_main
from resume_parser.entities import experience as ex
from resume_parser.field_norm import experience_type, find_dates, parse_dates
from resume_parser.pipeline import default_stages, run_pipeline
from resume_parser.schema import load_thresholds, schema_errors

PARAMS = load_thresholds()["stage5_entities"]


def _line(i, text, top=None, bold=0.0):
    top = 100 + 14 * i if top is None else top
    return {"id": f"p001_l{i:04d}", "page": 1, "text": text, "bbox": [56, top, 400, top + 10], "font_size": 10,
            "bold": bold, "in_table": False, "row_id": f"r{i}", "segment_index": 0}


def _payload(texts, tops=None):
    lines = [_line(i, t, None if tops is None else tops[i]) for i, t in enumerate(texts)]
    lines[0]["section_start"] = True
    return {"lines": lines, "kinds": ex.mark(lines), "params": PARAMS}


def _split(texts, method=ex.date_anchor_split, tops=None):
    return method(_payload(texts, tops)).data


def _fields(data):
    return [(e["job_title"], e["institution"], e["dates"]) for e in data["entries"]]


def _stage5(pdf):
    return run_pipeline(pdf, stages=default_stages()[:6]).results["stage5"]


# ---------------------------------------------------------------- the fake resumes


def test_fake_resumes_give_experience(synthetic_pdfs):
    gt = synth.synthetic_ground_truth()
    for pdf in synthetic_pdfs:
        record = run_pipeline(pdf)
        assert record.main["experience"] == gt[pdf.stem]["experience"], pdf.stem
        assert schema_errors(record.sidecar, "meta.schema.json") == []
        assert record.results["stage5"].per_unit["experience"].method_used == "date_anchor_split"


def test_sidecar_has_parsed_dates_and_provenance(synthetic_pdfs):
    sidecar = run_pipeline(synthetic_pdfs[0]).sidecar
    assert sidecar["parsed_dates"]["experience[0]"] == {"start": "2023-07", "end": None}  # "Present"
    assert sidecar["parsed_dates"]["experience[2]"] == {"start": "2014-01", "end": "2014-06"}
    side = sidecar["provenance"]["experience[1]"]
    assert side["page"] == 1 and side["lines"] and side["method"] == "date_anchor_split" and side["no_dates"] is False


# ---------------------------------------------------------------- line orders


def test_title_first_lines():
    data = _split(["Assistant Professor, Hillcrest University, Jul 2023 - Present",
                   "Lecturer, Southgate College of Engineering, 2015 - 2023"])
    assert _fields(data) == [("Assistant Professor", "Hillcrest University", "Jul 2023 - Present"),
                             ("Lecturer", "Southgate College of Engineering", "2015 - 2023")]


def test_title_first_over_two_lines():
    data = _split(["Assistant Professor", "Hillcrest University, Fakeville, 2019 - 2023",
                   "Lecturer", "Southgate College of Engineering, 2015 - 2019"])
    assert _fields(data) == [("Assistant Professor", "Hillcrest University", "2019 - 2023"),
                             ("Lecturer", "Southgate College of Engineering", "2015 - 2019")]


def test_institution_first_lines():
    data = _split(["Hillcrest University", "Associate Professor, 2019 - 2023",
                   "Northfield Institute of Science", "Research Scientist, 2015 - 2019"])
    assert _fields(data) == [("Associate Professor", "Hillcrest University", "2019 - 2023"),
                             ("Research Scientist", "Northfield Institute of Science", "2015 - 2019")]


def test_date_first_lines():
    data = _split(["2020 – Present", "Professor, Maplewood University", "2015 – 2020", "Lecturer, Maplewood University"])
    assert _fields(data) == [("Professor", "Maplewood University", "2020 – Present"),
                             ("Lecturer", "Maplewood University", "2015 – 2020")]


def test_paragraph_style():
    texts = ["Hillcrest University", "Visiting Scholar", "Northfield Institute of Science", "Research Fellow"]
    data = _split(texts, ex.paragraph_split, tops=[100, 112, 150, 162])
    assert _fields(data) == [("Visiting Scholar", "Hillcrest University", ""),
                             ("Research Fellow", "Northfield Institute of Science", "")]


def test_bullets_join_the_previous_entry():
    data = _split(["Assistant Professor, Hillcrest University, 2019 - 2023",
                   "• Taught genetics and cell biology", "– Worked on gene networks", "o sub-point under it",
                   "Responsible for the teaching laboratory",
                   "Lecturer, Southgate College of Engineering, 2015 - 2019"])
    assert len(data["entries"]) == 2 and data["leftover"] == 0
    assert "gene networks" in data["sides"][0]["raw_text"] and "laboratory" in data["sides"][0]["raw_text"]


def test_table_with_a_header_row(tmp_path):
    rows = [["Designation", "Organisation", "Duration"],
            ["Assistant Professor", "Hillcrest University", "2019 - 2023"],
            ["Lecturer", "Southgate College of Engineering", "2015 - 2019"]]
    pdf = synth.make_heading_table_pdf(tmp_path / "exp_table.pdf", "Work Experience", rows,
                                       [("heading", "Education")] + EDU_LINES)
    stage5 = _stage5(pdf)
    data = stage5.data["experience"]
    assert stage5.per_unit["experience"].method_used == "table_row_split"
    assert _fields(data) == [("Assistant Professor", "Hillcrest University", "2019 - 2023"),
                             ("Lecturer", "Southgate College of Engineering", "2015 - 2019")]
    assert data["header_lines"] == 3


# ---------------------------------------------------------------- institution rule (three branches)


def test_institution_department_first_keeps_the_full_run():
    data = _split(["Assistant Professor, Department of Biology, Hillcrest University, Fakeville, Jul 2023 - Present"])
    assert data["entries"][0]["institution"] == "Department of Biology, Hillcrest University, Fakeville"


def test_institution_first_with_places_keeps_only_the_institution():
    data = _split(["Lecturer, Hillcrest University, Fakeville, Northland, 2015 - 2019"])
    assert data["entries"][0]["institution"] == "Hillcrest University"


def test_institution_alone():
    data = _split(["Research Scientist at Northfield Institute of Science 2015 - 2019"])
    assert data["entries"][0]["institution"] == "Northfield Institute of Science"


def test_bare_iit_keeps_its_place():
    data = _split(["Assistant Professor, IIT, Fakeville, 2019 - 2023"])
    assert data["entries"][0]["institution"] == "IIT, Fakeville"


def test_unclear_institution_stays_empty():
    data = _split(["Assistant Professor, 2019 - 2023, worked on several things"])
    assert data["entries"][0]["institution"] == ""


# ---------------------------------------------------------------- titles and dates


def test_title_with_bracket_part_and_without_subject():
    data = _split(["Assistant Professor (Contract), Hillcrest University, 2019 - 2023",
                   "Assistant Professor of Physics, Hillcrest University, 2015 - 2019"])
    assert [e["job_title"] for e in data["entries"]] == ["Assistant Professor (Contract)", "Assistant Professor"]


def test_present_style_dates_are_kept_as_printed():
    for printed in ("Jul 2023 - Present", "2020 – till date", "Aug. 2016 to Current", "Jul'19 - Now"):
        data = _split([f"Lecturer, Hillcrest University, {printed}"])
        assert data["entries"][0]["dates"] == printed
        assert parse_dates(printed, 2026)["ongoing"] and parse_dates(printed, 2026)["end"] is None


def test_single_year_and_two_digit_years():
    assert _fields(_split(["Lecturer, Hillcrest University, 2012"])) == [("Lecturer", "Hillcrest University", "2012")]
    assert parse_dates("Aug'19 to Mar'21", 2026) == {"start": "2019-08", "end": "2021-03", "start_year": 2019,
                                                      "end_year": 2021, "ongoing": False}
    assert find_dates("2019-20")[0] == "2019-20"
    assert find_dates("Worked for 3 years") is None and find_dates("Section 12") is None


def test_entry_without_dates_is_flagged_not_dropped():
    data = _split(["Visiting Scholar, Lakeside University"])
    assert _fields(data) == [("Visiting Scholar", "Lakeside University", "")]
    assert data["sides"][0]["no_dates"] is True
    score, details = ex.check_experience(data, PARAMS)
    assert score == 1.0 and details["no_dates"] == 1


def test_implausible_years_and_reversed_ranges_fail_the_check():
    for line in ("Lecturer, Hillcrest University, 1940 - 1945", "Lecturer, Hillcrest University, 2041 - 2045",
                 "Lecturer, Hillcrest University, 2019 - 2015"):
        score, details = ex.check_experience(_split([line]), PARAMS)
        assert score == 0.0 and details["entries_ok"] == 0, line


def test_overlaps_are_counted_not_scored():
    data = _split(["Professor, Hillcrest University, 2015 - 2020", "Adjunct Professor, Lakeside University, 2018 - 2019"])
    score, details = ex.check_experience(data, PARAMS)
    assert score == 1.0 and details["overlaps"] == 1


def test_section_without_entries_scores_zero():
    data = _split(["some words without a title or a date"])
    assert data["entries"] == [] and ex.check_experience(data, PARAMS)[0] == 0.0


def test_academic_and_industry_typing():
    assert experience_type("Department of Biology, Hillcrest University") == "academic"
    assert experience_type("IIT, Fakeville") == "academic"
    assert experience_type("Examplecorp Analytics Pvt Ltd") == "industry"
    assert experience_type("Imaginary Labs Inc") == "industry"
    assert experience_type("Placeholder Technologies") == "industry"
    assert experience_type("City Hospital") == "other"
    assert experience_type("") == "other"


# ---------------------------------------------------------------- content-routed blocks


def test_content_routed_experience_keeps_only_entries_with_title_and_more():
    def section(block, texts, start):
        return {"name": "experience", "block_id": block, "heading_raw": "", "lines": [_line(start + i, t)
                                                                                     for i, t in enumerate(texts)]}
    document = {"sections": [
        section("b1", ["Lecturer, Hillcrest University, 2015 - 2019", "Visiting Scholar"], 0),
        section("b2", ["Head, Hillcrest University, 2020 - 2022", "Reviewer for the 2021 issue"], 10),
        section("b3", ["Visiting Scholar"], 20),
    ]}
    payload = ex.experience_payload(document, PARAMS, content_blocks=frozenset({"b2", "b3"}))
    data = ex.date_anchor_split(payload).data
    # b1 has a heading: its title-only entry stays; b2 and b3 were routed by content: the
    # date-only and the title-only entries are dropped
    assert [e["job_title"] for e in data["entries"]] == ["Lecturer", "Visiting Scholar", "Head"]
    assert data["dropped_from_content"] == 2


# ---------------------------------------------------------------- determinism and privacy


def test_experience_is_deterministic(synthetic_pdfs):
    a, b = _stage5(synthetic_pdfs[2]), _stage5(synthetic_pdfs[2])
    assert a.data["experience"] == b.data["experience"] and a.to_report() == b.to_report()


def test_cli_prints_ids_only(data_dir, capsys):
    synth.build_all(data_dir / "pdfs")
    assert cli_main(["run", "--allow-stubs"]) == 0
    out = capsys.readouterr().out
    for line in out.strip().splitlines():
        assert re.match(r"^(r_[0-9a-f]{8} \w+ flags=\d+ unknown_headings=\d+.*|done: .*|groups: .*)$", line), line
    for gt in synth.synthetic_ground_truth().values():
        assert gt["candidate_name"] not in out
        assert all(e["institution"] not in out for e in gt["experience"])


def test_name_page_is_never_an_experience_entry(tmp_path):
    lines = PERSONAL_TOP + [("heading", "WORK EXPERIENCE"), ("body", "Lecturer, Hillcrest University, 2015 - 2019")]
    data = _stage5(synth.make_lines_pdf(tmp_path / "one.pdf", lines)).data["experience"]
    assert _fields(data) == [("Lecturer", "Hillcrest University", "2015 - 2019")]


# ---------------------------------------------------------------- titles after other words


def test_title_after_a_lead_in():
    data = _split(["Currently working as Assistant Professor at Hillcrest University since 2019",
                   "• Worked as Research Associate, Northfield Institute of Science, 2012 - 2014",
                   "Designation: Lecturer", "Southgate College of Engineering, 2008 - 2012"])
    assert _fields(data) == [("Assistant Professor", "Hillcrest University", "2019"),
                             ("Research Associate", "Northfield Institute of Science", "2012 - 2014"),
                             ("Lecturer", "Southgate College of Engineering", "2008 - 2012")]


def test_mid_line_title_needs_a_date_or_institution_nearby():
    with_context = _split(["Taught there as the Senior Lecturer, Hillcrest University, 2015 - 2019"])
    assert [e["job_title"] for e in with_context["entries"]] == ["Senior Lecturer"]
    data = _split(["Lecturer, Hillcrest University, 2015 - 2019", "Took part in the Lecturer exchange programme",
                   "and other duties"])
    assert len(data["entries"]) == 1 and data["entries"][0]["job_title"] == "Lecturer"


def test_titles_of_other_people_are_not_job_titles():
    from resume_parser.entities.experience import find_title

    for line in ("Worked under Professor Ravi Moon at Hillcrest University, 2015 - 2019",
                 "Supervised by Dr. Mira Roe, Hillcrest University", "Reporting to Director of the centre, 2019",
                 "Thesis guide: Professor Lena Hart", "Research done with Professor Lena Hart, Hillcrest University"):
        assert find_title(line, mid=True) is None, line
    assert find_title("visited the lab of Professor Lena Hart, 2019", mid=True) is None  # a name right after


def test_title_families_and_prefixes():
    from resume_parser.entities.experience import find_title

    assert find_title("Guest Faculty, Hillcrest University, 2019")[0] == "Guest Faculty"
    assert find_title("Ad-hoc Assistant Professor, Southgate College, 2019 - 2020")[0] == "Ad-hoc Assistant Professor"
    assert find_title("Visiting Research Scholar at Lakeside University, 2018")[0] == "Visiting Research Scholar"
    assert find_title("Project Associate, Northfield Institute of Science, 2012")[0] == "Project Associate"
    assert find_title("Head of Department, Hillcrest University, 2020 - Present")[0] == "Head of Department"
    assert find_title("Head of School, Hillcrest University")[0] == "Head of School"
    assert find_title("Head of, 2019") is None


# ---------------------------------------------------------------- the two halves of one job


def test_two_halves_of_one_job_are_merged():
    # the second title-less line has dates the first entry lacks: one job
    data = _split(["Assistant Professor, Hillcrest University", "• Taught genetics",
                   "Hillcrest Campus Road, Jul 2019 - Present",
                   "Lecturer, Southgate College of Engineering, 2015 - 2019"], ex.paragraph_split,
                  tops=[100, 112, 150, 190])
    assert _fields(data) == [("Assistant Professor", "Hillcrest University", "Jul 2019 - Present"),
                             ("Lecturer", "Southgate College of Engineering", "2015 - 2019")]
    assert data["merged_halves"] == 1


def test_halves_are_not_merged_across_sections_or_when_slots_are_filled():
    # the previous entry already has institution and dates: nothing to fill, no merge
    data = _split(["Lecturer, Southgate College of Engineering, 2015 - 2019", "Hillcrest University, 2012 - 2014"],
                  ex.paragraph_split, tops=[100, 150])
    assert len(data["entries"]) == 2 and data["merged_halves"] == 0
    # a section boundary between the halves
    lines = [_line(0, "Assistant Professor"), _line(1, "Hillcrest University, 2019 - 2023")]
    lines[0]["section_start"] = lines[1]["section_start"] = True
    out = ex.paragraph_split({"lines": lines, "kinds": ex.mark(lines), "params": PARAMS}).data
    assert len(out["entries"]) == 2 and out["merged_halves"] == 0


def test_day_month_year_dates():
    cases = {"01/07/2019 to 30/06/2021": ("2019-07", "2021-06"), "01-07-2019 - 30-06-2021": ("2019-07", "2021-06"),
             "01.07.2019 - till date": ("2019-07", None), "July 1, 2019 - June 30, 2021": ("2019-07", "2021-06"),
             "1st July 2019 to 30th June 2021": ("2019-07", "2021-06"), "12 May 2019 - Present": ("2019-05", None)}
    for printed, (start, end) in cases.items():
        data = _split([f"Lecturer, Hillcrest University, {printed}"])
        assert data["entries"][0]["dates"] == printed  # kept as printed
        parsed = parse_dates(printed, 2026)
        assert (parsed["start"], parsed["end"]) == (start, end), printed
