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


# ---------------------------------------------------------------- column reading order

from resume_parser.extract.lines import column_order  # noqa: E402

COLS = load_thresholds()["stage1_extract"]


def _l(i, text, x0, x1, top, in_table=False):
    return {"id": f"p001_l{i:04d}", "page": 1, "text": text, "bbox": [x0, top, x1, top + 10], "font_size": 10,
            "bold": 0.0, "in_table": in_table, "row_id": f"r{i}", "segment_index": 0, "cell_row": None,
            "cell_col": None}


def test_one_column_and_table_pages_are_returned_unchanged():
    one = [_l(i, f"Body line number {i} of a plain one column page", 56, 520, 100 + 14 * i) for i in range(20)]
    assert column_order(one, COLS) is one
    table = [_l(i, f"cell {i}", 56 + 160 * (i % 3), 200 + 160 * (i % 3), 100 + 14 * (i // 3), in_table=True)
             for i in range(18)]
    assert column_order(table, COLS) is table and not any("column" in line for line in table)


def test_right_aligned_dates_are_not_a_second_column():
    lines = []
    for k in range(8):
        lines.append(_l(2 * k, f"Lecturer {k}, Hillcrest University", 56, 300, 100 + 14 * k))
        lines.append(_l(2 * k + 1, f"20{10 + k} - 20{11 + k}", 470, 540, 100 + 14 * k))
    assert column_order(lines, COLS) is lines


def _band(start_id, top, rows, tag):
    out = []
    for k in range(rows):  # a two-column band, rows side by side
        out.append(_l(start_id + 2 * k, f"{tag} left line {k}", 56, 280, top + 14 * k))
        out.append(_l(start_id + 2 * k + 1, f"{tag} right line {k}", 310, 540, top + 14 * k))
    return out


def test_mixed_page_reads_full_width_lines_in_place_and_columns_in_turn():
    name = _l(0, "Talia Vonbrook, a name line written right across the whole page width", 56, 540, 80)
    between = _l(99, "A full-width heading line that runs across both columns of the page", 56, 540, 250)
    lines = [name] + _band(1, 100, 10, "a") + [between] + _band(30, 270, 10, "b")
    out = column_order(lines, COLS)
    assert [line["text"] for line in out] == (
        [name["text"]] + [f"a left line {k}" for k in range(10)] + [f"a right line {k}" for k in range(10)]
        + [between["text"]] + [f"b left line {k}" for k in range(10)] + [f"b right line {k}" for k in range(10)])
    assert [line["column"] for line in out][:2] == ["full", "left"]
    assert [line["text"] for line in out if line["column_start"]] == [
        "a left line 0", "a right line 0", "b left line 0", "b right line 0"]
    # much full-width text across the gutter: not a column page, the order stays as it is
    busy = [name] + _band(1, 100, 6, "a") + [_l(60 + k, f"one column text line {k} across the gutter", 56, 540,
                                                 190 + 14 * k) for k in range(6)]
    assert column_order(busy, COLS) is busy


def test_column_pages_are_read_column_by_column_and_noted(tmp_path):
    for stem in ("layout_two_columns", "layout_sidebar", "layout_sidebar_right", "layout_two_columns_unequal"):
        result = _stage1(synth.LAYOUTS[stem](tmp_path / f"{stem}.pdf"))
        lines = _lines(result)
        assert all("column" in line for line in lines), stem
        assert result.metrics["notes"] == [{"code": "columns_reordered", "detail": "page_001", "section": None}]
    plain = _stage1(synth.LAYOUTS["layout_one_column"](tmp_path / "one.pdf"))
    assert plain.metrics["notes"] == [] and not any("column" in line for line in _lines(plain))


def test_a_sidebar_without_headings_stays_out_of_the_main_sections(tmp_path):
    record = run_pipeline(synth.make_layout_sidebar_no_headings(tmp_path / "side.pdf"))
    sections = record.results["stage4"].data["sections"]
    for word in synth.LAYOUT_SKILLS + synth.SIDEBAR_NO_HEADINGS_EXTRA:
        homes = [s["name"] for s in sections if any(word in line["text"] for line in s["lines"])]
        assert homes and not set(homes) & {"education", "experience", "publications"}, word
    assert len(record.main["experience"]) == 2 and len(record.main["publications"]) == 2


def test_column_notes_are_flags_only_and_never_change_the_decision(tmp_path, monkeypatch):
    from resume_parser.extract import base
    from resume_parser.writer import build_review_queue

    pdf = synth.LAYOUTS["layout_two_columns"](tmp_path / "two.pdf")
    with_notes = run_pipeline(pdf)
    monkeypatch.setattr(base, "_column_notes", lambda result, params: [])
    without = run_pipeline(pdf)
    codes = [f["code"] for f in with_notes.sidecar["flags"]]
    assert "columns_reordered" in codes and "columns_reordered" not in [f["code"] for f in without.sidecar["flags"]]
    assert with_notes.results["stage1"].status == without.results["stage1"].status
    for key in ("decision", "decision_reasons", "section_scores"):
        assert with_notes.sidecar[key] == without.sidecar[key], key
    entry = lambda r: {"resume_id": r.resume_id, "resume_file": r.resume_file, "decision": r.sidecar["decision"],  # noqa: E731
                       "reasons": r.sidecar["decision_reasons"], "flags_count": 0}
    assert build_review_queue({"resumes": [entry(with_notes)]}) == build_review_queue({"resumes": [entry(without)]})


def test_column_order_is_deterministic(tmp_path):
    pdf = synth.LAYOUTS["layout_sidebar"](tmp_path / "s.pdf")
    assert _lines(_stage1(pdf)) == _lines(_stage1(pdf))


def test_a_narrow_strip_of_dates_and_places_is_not_a_sidebar(tmp_path):
    from resume_parser.extract.lines import find_gutter

    result = _stage1(synth.make_layout_dates_and_places(tmp_path / "dates.pdf"))
    lines = _lines(result)
    assert not any("column" in line for line in lines)
    assert not any(n["code"] == "columns_reordered" for n in result.metrics["notes"])
    # the strip is narrow and about a third date-only: it passes the 50% limit of a wide side,
    # so it is the narrow-side limit that keeps the page as it is
    loose_dates = find_gutter(lines, COLS["column_gutter_min_pt"], COLS["column_cross_max"], COLS["column_min_lines"],
                              COLS["column_side_cover_min"], COLS["column_min_width"], COLS["column_date_only_max"])
    assert loose_dates is not None
    # a real sidebar, on either side, is still read after the main text
    for stem in ("layout_sidebar", "layout_sidebar_right"):
        side = _lines(_stage1(synth.LAYOUTS[stem](tmp_path / f"{stem}.pdf")))
        assert all("column" in line for line in side), stem
        assert [line["text"] for line in side][-1] in synth.LAYOUT_SKILLS, stem
