"""Stage 2: headers and footers, glyphs, wrapped lines, the retention check and its fallback."""

from __future__ import annotations

import make_synthetic_resumes as synth
from resume_parser import clean
from resume_parser.pipeline import default_stages, run_pipeline
from resume_parser.schema import Method, MethodOutput


def _run(pdf):
    record = run_pipeline(pdf, stages=default_stages()[:3])
    return record.results["stage1"], record.results["stage2"]


def _texts(result):
    return [line["text"] for line in result.data["lines"]]


def test_headers_and_footers_removed_on_three_pages(tmp_path):
    _s1, s2 = _run(synth.make_multipage_pdf(tmp_path / "three.pdf", pages=3))
    texts = _texts(s2)
    assert s2.method_used == "full_clean" and s2.status.value == "OK"
    assert synth.HEADER_TEXT not in texts
    assert not any(t.startswith("Page ") for t in texts)
    assert s2.data["removed_lines"]["header_footer"] == 6
    assert "Mira Testwood" in texts  # the name itself stays


def test_headers_and_footers_kept_on_one_page(tmp_path):
    _s1, s2 = _run(synth.make_multipage_pdf(tmp_path / "one.pdf", pages=1))
    texts = _texts(s2)
    assert synth.HEADER_TEXT in texts and "Page 1 of 1" in texts
    assert s2.data["removed_lines"]["header_footer"] == 0


def test_glyphs_stripped_and_lines_keep_id_page_bbox(synthetic_pdfs):
    s1, s2 = _run(synthetic_pdfs[2])
    texts = _texts(s2)
    assert not any("(cid:" in t or t.startswith(("*", "–", "o ")) for t in texts)
    assert "Doctor of Philosophy (Economics) - Greenhill University (2015)" in texts
    by_id = {line["id"]: line for line in s1.data["page_001"]["lines"]}
    for line in s2.data["lines"]:
        assert line["page"] == by_id[line["id"]]["page"] and line["bbox"] == by_id[line["id"]]["bbox"]


def test_wrapped_lines_joined_with_hyphen_fixed(tmp_path):
    _s1, s2 = _run(synth.make_layout_pdf(tmp_path / "layout.pdf"))
    texts = _texts(s2)
    assert "Research interests include gene regulatory networks and sparse graph models for invented data." in texts
    joined = next(line for line in s2.data["lines"] if line["text"].startswith("Research interests"))
    assert len(joined["joined_ids"]) == 2
    # the three segments of the table-like row are never joined
    for text, _x in synth.TABLE_LIKE_ROW:
        assert text in texts


def test_table_cells_are_never_joined(synthetic_pdfs):
    s1, s2 = _run(synthetic_pdfs[1])
    cells_before = [line for line in s1.data["page_001"]["lines"] if line["in_table"]]
    cells_after = [line for line in s2.data["lines"] if line["in_table"]]
    assert len(cells_after) == len(cells_before) and all(not line["joined_ids"] for line in cells_after)


def test_low_retention_falls_back_to_conservative_clean(synthetic_pdfs, monkeypatch):
    def lossy(payload):
        out = clean.full_clean(payload).data
        return MethodOutput(dict(out, lines=out["lines"][: len(out["lines"]) // 2]))

    monkeypatch.setattr(clean, "METHODS", (Method("full_clean", lossy),) + clean.METHODS[1:])
    _s1, s2 = _run(synthetic_pdfs[0])
    first = s2.methods_tried[0]
    assert first.status.value == "BELOW_THRESHOLD" and dict(first.details)["retention"] < 0.95
    assert s2.method_used == "conservative_clean" and s2.status.value == "OK"


def test_raw_lines_is_the_last_resort(synthetic_pdfs, monkeypatch):
    def empty(payload):
        out = clean.full_clean(payload).data
        return MethodOutput(dict(out, lines=[]))

    monkeypatch.setattr(clean, "METHODS", (Method("full_clean", empty), Method("conservative_clean", empty),
                                           clean.METHODS[2]))
    _s1, s2 = _run(synthetic_pdfs[0])
    assert s2.method_used == "raw_lines"
