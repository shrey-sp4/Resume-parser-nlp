"""Stage 5 pre-pass: wrapped lines inside a narrow column (entities/wraps.py)."""

from __future__ import annotations

from resume_parser.entities.wraps import join_column_wraps
from resume_parser.schema import load_thresholds

PARAMS = load_thresholds()["stage5_entities"]


def _line(i, text, x0=56.0, top=None, column="left", page=1, **extra):
    top = 100 + 14 * i if top is None else top
    line = {"id": f"p{page:03d}_l{i:04d}", "page": page, "text": text, "bbox": [x0, top, 280, top + 10],
            "font_size": 9.5, "bold": 0.0, "in_table": False, "joined_ids": [], **extra}
    if column is not None:
        line["column"] = column
        line["column_start"] = False
    return line


def _doc(section, lines):
    return {"sections": [{"name": section, "block_id": "b1", "heading_raw": section.upper(), "lines": lines}]}


def _texts(doc):
    return [l["text"] for l in doc["sections"][0]["lines"]]


def test_unfinished_line_and_closing_parts_join():
    lines = [_line(0, "Ph.D. in Toy Physics, Westmere University, 2012"), _line(1, "2017", x0=68),
             _line(2, "M.Sc. in Applied Optics, Calder Valley College,"), _line(3, "2010 - 2012", x0=68)]
    doc = join_column_wraps(_doc("education", lines), PARAMS)
    assert _texts(doc) == ["Ph.D. in Toy Physics, Westmere University, 2012 2017",
                           "M.Sc. in Applied Optics, Calder Valley College, 2010 - 2012"]
    assert doc["sections"][0]["lines"][0]["joined_ids"] == ["p001_l0001"]  # provenance as in stage 2
    exp = join_column_wraps(_doc("experience", [_line(0, "Associate Professor, Brightford Institute of"),
                                                _line(1, "Technology, 2020 - Present")]), PARAMS)
    assert _texts(exp) == ["Associate Professor, Brightford Institute of Technology, 2020 - Present"]


def test_venue_lead_joins_only_in_publications():
    lines = [_line(0, "2. T. Vonbrook. Folding light in toy fibres."), _line(1, "Proc. Imaginary Symposium on Optics, 2019.")]
    assert len(_texts(join_column_wraps(_doc("publications", lines), PARAMS))) == 1
    assert len(_texts(join_column_wraps(_doc("experience", lines), PARAMS))) == 2


def test_two_separate_entries_on_consecutive_column_lines_stay_apart():
    lines = [_line(0, "Ph.D. in Toy Physics, Westmere University, 2012 - 2017"),
             _line(1, "M.Sc. in Applied Optics, Calder Valley College, 2010 - 2012")]
    assert len(_texts(join_column_wraps(_doc("education", lines), PARAMS))) == 2


def test_lines_without_a_column_field_are_never_touched():
    lines = [_line(0, "Ph.D. in Toy Physics, Westmere University,", column=None), _line(1, "2012 - 2017", column=None)]
    doc = _doc("education", lines)
    assert join_column_wraps(doc, PARAMS) is doc  # a one-column page: the same document, unchanged
    full = [_line(0, "Ph.D. in Toy Physics, Westmere University,", column="full"), _line(1, "2012 - 2017", column="full")]
    assert len(_texts(join_column_wraps(_doc("education", full), PARAMS))) == 2


def test_guards_stop_a_join():
    base = "Associate Professor, Brightford Institute of"
    # a gap far larger than the normal line gap of the column
    spaced = [_line(0, base), _line(1, "Technology, 2020 - Present", top=160),
              _line(2, "Research Engineer, Glimmerworks Labs Pvt Ltd,", top=174), _line(3, "2017 - 2020", top=188)]
    assert _texts(join_column_wraps(_doc("experience", spaced), PARAMS)) == [
        base, "Technology, 2020 - Present", "Research Engineer, Glimmerworks Labs Pvt Ltd, 2017 - 2020"]
    for second in (_line(1, "Technology, 2020 - Present", x0=40),            # starts left of the line above
                   _line(1, "2. Technology, 2020 - Present"),                # a list number
                   _line(1, "Technology, 2020 - Present", column="right"),   # another column
                   _line(1, "Technology, 2020 - Present", in_table=True)):   # a table cell
        lines = [_line(0, base), second, _line(2, "Research Engineer, Glimmerworks Labs Pvt Ltd, 2017 - 2020")]
        assert len(_texts(join_column_wraps(_doc("experience", lines), PARAMS))) == 3, second
    # other sections (personal, other) are left as they are
    lines = [_line(0, "Talia Vonbrook,"), _line(1, "talia.vonbrook@example.org")]
    assert len(_texts(join_column_wraps(_doc("personal", lines), PARAMS))) == 2
