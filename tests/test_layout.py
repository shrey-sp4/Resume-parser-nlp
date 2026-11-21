"""Stage 1: line building, tables cell by cell, row segments, page checks and the method choice."""

from __future__ import annotations

import pytest

import make_synthetic_resumes as synth
from resume_parser.extract.lines import garbled_rate, glued_words, word_overlap
from resume_parser.pipeline import default_stages, run_pipeline
from resume_parser.schema import AttemptStatus, load_thresholds

LINE_KEYS = {"id", "page", "text", "bbox", "font_size", "bold", "in_table", "cell_row", "cell_col",
             "row_id", "segment_index", "reading_order"}


def _stage1(pdf, thresholds=None):
    return run_pipeline(pdf, stages=default_stages()[:2], thresholds=thresholds).results["stage1"]


def _lines(result, page="page_001"):
    return result.data[page]["lines"]


@pytest.fixture
def checks():
    return load_thresholds()["stage1_checks"]


def test_lines_have_every_field(synthetic_pdfs):
    for pdf in synthetic_pdfs:
        lines = _lines(_stage1(pdf))
        assert lines and all(set(line) == LINE_KEYS for line in lines)
        assert [line["reading_order"] for line in lines] == list(range(1, len(lines) + 1))
        assert lines[0]["bold"] == 1.0 and lines[0]["font_size"] > lines[3]["font_size"]  # the name line


def test_table_is_read_cell_by_cell(synthetic_pdfs):
    lines = _lines(_stage1(synthetic_pdfs[1]))
    cells = [line for line in lines if line["in_table"]]
    assert len(cells) == 16  # 4 rows x 4 columns
    cell = next(line for line in cells if line["text"] == "Eastvale Institute of Technology")
    assert (cell["cell_row"], cell["cell_col"]) == (2, 2)
    assert len({line["row_id"] for line in cells if line["cell_row"] == 2}) == 1
    # words inside the table are not read again as normal lines
    assert sum("Eastvale" in line["text"] for line in lines) == 1


def test_justified_paragraph_stays_whole_and_table_like_row_splits(tmp_path):
    lines = _lines(_stage1(synth.make_layout_pdf(tmp_path / "layout.pdf")))
    paragraph = [line for line in lines if line["bbox"][1] < 120]
    assert len(paragraph) == 4 and all(line["segment_index"] == 0 for line in paragraph)
    assert len({line["row_id"] for line in paragraph}) == 4
    row = [line for line in lines if line["text"] in {t for t, _ in synth.TABLE_LIKE_ROW}]
    assert [line["text"] for line in row] == [t for t, _ in synth.TABLE_LIKE_ROW]
    assert len({line["row_id"] for line in row}) == 1
    assert [line["segment_index"] for line in row] == [0, 1, 2]


def test_both_main_methods_always_run_and_scores_keep_their_parts(synthetic_pdfs):
    choice = _stage1(synthetic_pdfs[0]).per_unit["page_001"]
    tried = {a.method: a for a in choice.methods_tried}
    assert choice.method_used == "pdfplumber_words_tables"
    assert tried["pdfplumber_words_tables"].status is AttemptStatus.ACCEPTED
    assert tried["pdfminer_layout"].score is not None  # ran although method 1 was accepted
    assert tried["pdfplumber_loose_spacing"].status is AttemptStatus.SKIPPED
    details = dict(tried["pdfplumber_words_tables"].details)
    assert {"glued_rate", "overlap", "garbled_rate", "glued_words", "words", "lines"} <= set(details)
    assert details["overlap"] == 1.0 and details["glued_rate"] == 0.0


def test_loose_spacing_runs_only_when_method_one_is_below_threshold(synthetic_pdfs):
    thresholds = load_thresholds()
    strict = dict(thresholds, accept_threshold=dict(thresholds["accept_threshold"], stage1=1.01))
    result = _stage1(synthetic_pdfs[0], strict)
    tried = {a.method: a for a in result.per_unit["page_001"].methods_tried}
    assert tried["pdfplumber_loose_spacing"].status is AttemptStatus.BELOW_THRESHOLD
    assert result.per_unit["page_001"].method_used == "pdfplumber_words_tables"  # tie -> earlier
    assert result.status.value == "WARN"
    assert set(result.extra["outputs"]["page_001"]) == {
        "pdfplumber_words_tables", "pdfminer_layout", "pdfplumber_loose_spacing"}


def test_stage_one_records_glued_page_counts(synthetic_pdfs):
    metrics = _stage1(synthetic_pdfs[0]).metrics
    assert metrics["pages_glued_over_max"] == 0 and metrics["pages_glued_over_max_by_method"] == {}


# ---------------------------------------------------------------- the checks themselves


def test_glued_words_skip_emails_urls_and_dois(checks):
    text = ("Contact verylongname.withdots@someuniversityexample.org https://www.averyveryverylongdomainname.org/x "
            "doi:10.1000/abcdefghijklmnopqrstuvwxyz 10.1234/zzzzzzzzzzzzzzzzzzzzzzzzzzz")
    assert glued_words(text, checks)[1] == 0


def test_glued_words_long_runs_and_case_joins(checks):
    assert glued_words("internationalconferenceonthings", checks)[1] == 1  # 25+ letters
    assert glued_words("internationalconference", checks)[1] == 0  # under 25
    assert glued_words("ProfessorDepartment of Physics", checks)[1] == 1  # 3+ lower, Upper, lower
    assert glued_words("McDonald GitHub iPhone", checks)[1] == 0  # fewer than 3 lowercase before
    assert glued_words("PhD LaTeX MATLAB BibTeX JavaScript ResearchGate LinkedIn", checks)[1] == 0  # ignore list
    rate, glued, words = glued_words("one twoThree abFive six", checks)
    assert (glued, words) == (1, 4) and rate == 0.25  # "ab" is only two lowercase letters


def test_word_overlap_ignores_order_and_case():
    assert word_overlap("Professor of Physics", "physics OF professor") == 1.0
    assert word_overlap("a b c d", "a b") == 0.5
    assert word_overlap("", "") is None


def test_garbled_rate_counts_cid_and_replacement_chars():
    assert garbled_rate("(cid:12)abcd") == pytest.approx(1 / 5)
    assert garbled_rate("ab�") == 0.25  # the private-use icon does not count
    assert garbled_rate("plain text") == 0.0
