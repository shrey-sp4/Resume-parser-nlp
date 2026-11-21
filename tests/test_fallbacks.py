"""The fallback helper: method order, thresholds, ties, stubs and per-unit choice."""

from __future__ import annotations

from resume_parser.pipeline import run_with_fallbacks
from resume_parser.schema import AttemptStatus, Method, MethodOutput, Reason, Status, Unit, not_implemented


def scored(name: str, score: float) -> Method:
    """A method whose output is its own score, so check() can just return it."""
    return Method(name, lambda payload: MethodOutput(score))


def stub(name: str) -> Method:
    return Method(name, lambda payload: not_implemented())


def empty(name: str, code: str = "nothing_found") -> Method:
    return Method(name, lambda payload: MethodOutput(None, (Reason(code),)))


def identity(data):
    return data


def test_first_below_threshold_second_chosen():
    r = run_with_fallbacks("stage3", [scored("m1", 0.5), scored("m2", 0.9), scored("m3", 1.0)], identity, 0.8)
    assert r.status is Status.OK
    assert r.method_used == "m2"
    assert [a.method for a in r.methods_tried] == ["m1", "m2"]  # m3 never runs
    assert [a.status for a in r.methods_tried] == [AttemptStatus.BELOW_THRESHOLD, AttemptStatus.ACCEPTED]
    assert r.data == 0.9


def test_all_below_threshold_keeps_best_and_warns():
    r = run_with_fallbacks("stage3", [scored("m1", 0.4), scored("m2", 0.6), scored("m3", 0.5)], identity, 0.8)
    assert r.status is Status.WARN
    assert r.method_used == "m2"
    assert r.data == 0.6
    assert [x.code for x in r.reasons] == ["below_accept_threshold"]
    assert len(r.methods_tried) == 3


def test_tie_goes_to_earlier_method():
    r = run_with_fallbacks("stage3", [scored("m1", 0.6), scored("m2", 0.6)], identity, 0.8)
    assert r.method_used == "m1"


def test_stub_is_never_chosen():
    r = run_with_fallbacks("stage3", [stub("s1"), scored("m1", 0.1)], identity, 0.8)
    assert r.method_used == "m1"
    assert r.methods_tried[0].status is AttemptStatus.NOT_IMPLEMENTED


def test_all_stubs_give_ok_not_implemented():
    r = run_with_fallbacks("stage2", [stub("s1"), stub("s2")], identity, 0.8)
    assert r.status is Status.OK
    assert r.method_used is None
    assert [x.code for x in r.reasons] == ["not_implemented"]


def test_no_output_fails_when_gated_and_warns_otherwise():
    gated = run_with_fallbacks("stage0", [empty("m1"), empty("m2")], identity, 1.0,
                               fail_if_unusable=True, unusable_code="unreadable")
    assert gated.status is Status.FAIL
    assert [x.code for x in gated.reasons] == ["unreadable", "nothing_found"]

    later = run_with_fallbacks("stage3", [empty("m1")], identity, 1.0)
    assert later.status is Status.WARN


def test_warn_code_method_always_warns():
    last = Method("whole_section_one_entry", lambda p: MethodOutput(1.0), warn_code="whole_section_one_entry")
    r = run_with_fallbacks("stage5", [stub("anchor_split"), last], identity, 0.8)
    assert r.method_used == "whole_section_one_entry"
    assert r.status is Status.WARN
    assert "whole_section_one_entry" in [x.code for x in r.reasons]


def by_page(scores: dict[int, float]):
    """A method whose score depends on the page number in the payload."""
    return lambda payload: MethodOutput(scores[payload]) if payload in scores else MethodOutput(None)


def test_per_page_choice_is_recorded():
    m1 = Method("pdfplumber_words_tables", by_page({1: 0.95, 2: 0.3}))
    m2 = Method("pdfminer_layout", by_page({1: 0.99, 2: 0.92}))
    units = [Unit("page_001", 1, section="document"), Unit("page_002", 2, section="document")]
    r = run_with_fallbacks("stage1", [m1, m2], identity, 0.9, units=units, fail_if_unusable=True)
    assert r.status is Status.OK
    assert r.method_used == "per_unit"
    assert r.per_unit["page_001"].method_used == "pdfplumber_words_tables"
    assert r.per_unit["page_002"].method_used == "pdfminer_layout"
    assert r.data == {"page_001": 0.95, "page_002": 0.92}
    assert r.to_report()["per_unit"]["page_002"]["methods_tried"][0]["status"] == "BELOW_THRESHOLD"


def test_stage_one_fails_only_when_no_page_is_usable():
    ocr = empty("ocr_tesseract", "ocr_unavailable")
    good = scored("pdfplumber_words_tables", 1.0)
    partial = run_with_fallbacks(
        "stage1", [good], identity, 0.9, fail_if_unusable=True, unusable_code="no_text_layer",
        units=[Unit("page_001", None), Unit("page_002", None, methods=(ocr,), require_output=True)],
    )
    assert partial.status is Status.WARN
    assert partial.per_unit["page_002"].status is Status.FAIL
    assert {(x.code, x.detail) for x in partial.reasons} >= {("no_text_layer", "page_002")}

    none_usable = run_with_fallbacks(
        "stage1", [good], identity, 0.9, fail_if_unusable=True, unusable_code="no_text_layer",
        units=[Unit("page_001", None, methods=(ocr,), require_output=True)],
    )
    assert none_usable.status is Status.FAIL


def test_text_page_with_stubs_and_no_ocr_is_not_a_failure():
    ocr = empty("ocr_tesseract", "ocr_unavailable")
    r = run_with_fallbacks("stage1", [stub("pdfplumber_words_tables"), ocr], identity, 0.9,
                           fail_if_unusable=True, unusable_code="no_text_layer",
                           units=[Unit("page_001", None)])
    assert r.status is Status.OK
    assert [x.code for x in r.reasons] == ["not_implemented"]
    assert r.per_unit["page_001"].methods_tried[1].status.name == "NO_OUTPUT"


def test_required_output_with_only_stubs_fails():
    r = run_with_fallbacks("stage1", [stub("ocr_tesseract")], identity, 0.9, fail_if_unusable=True,
                           unusable_code="no_text_layer", units=[Unit("page_001", None, require_output=True)])
    assert r.status is Status.FAIL


def test_same_input_same_choice():
    methods = [scored("m1", 0.7), scored("m2", 0.7), scored("m3", 0.2)]
    first = run_with_fallbacks("stage3", methods, identity, 0.8).to_report()
    second = run_with_fallbacks("stage3", methods, identity, 0.8).to_report()
    assert first == second
