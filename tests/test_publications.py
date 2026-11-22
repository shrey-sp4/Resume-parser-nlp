"""Stage 5, publications: splitting, fields, typing, stated totals and checks. Synthetic data only."""

from __future__ import annotations

import re

import make_synthetic_resumes as synth
from make_synthetic_resumes import PERSONAL_TOP
from resume_parser.cli import main as cli_main
from resume_parser.entities import publications as pb
from resume_parser.pipeline import default_stages, run_pipeline
from resume_parser.schema import load_thresholds, schema_errors

PARAMS = load_thresholds()["stage5_entities"]


def _line(i, text, x0=56.0, top=None, page=1, bold=0.0):
    top = 100 + 14 * i if top is None else top
    return {"id": f"p{page:03d}_l{i:04d}", "page": page, "text": text, "bbox": [x0, top, 500, top + 10],
            "font_size": 10, "bold": bold, "in_table": False, "row_id": f"r{i}", "segment_index": 0}


def _document(*sections):
    """sections: (heading, [line dicts])"""
    return {"sections": [{"name": "publications", "block_id": f"b{k}", "heading_raw": h, "lines": ls}
                         for k, (h, ls) in enumerate(sections)]}


def _run(texts, method=None, heading="Publications", lines=None):
    lines = lines or [_line(i, t) for i, t in enumerate(texts)]
    payload = pb.publications_payload(_document((heading, lines)), PARAMS)
    if method is None:
        for m in pb.SPLIT_METHODS:
            out = m.fn(payload)
            if out.data is not None and pb.check_publications(out.data, PARAMS)[0] >= 0.8:
                return out.data
        raise AssertionError("no method accepted")
    return method(payload).data


def _titles(data):
    return [e["title"] for e in data["entries"]]


A = "M. Testwood, R. Doe. Modelling gene networks with sparse graphs. Journal of Invented Biology, 4, 2022."
B = "M. Testwood. Fast alignment of made-up sequences. Proc. Imaginary Conference on Bioinformatics, 2020."
C = "J. Fakely, A. Roe. Thin films that do not exist. Invented Physics Letters 12 (2021) 1-9."


# ---------------------------------------------------------------- the fake resumes


def test_fake_resumes_give_publications(synthetic_pdfs):
    gt = synth.synthetic_ground_truth()
    for pdf in synthetic_pdfs:
        record = run_pipeline(pdf)
        assert record.main["publications"] == gt[pdf.stem]["publications"], pdf.stem
        assert record.main["stated_totals"] == gt[pdf.stem]["stated_totals"], pdf.stem
        assert schema_errors(record.sidecar, "meta.schema.json") == []
        side = record.sidecar["provenance"]["publications[0]"]
        assert side["lines"] and side["method"] and side["type_source"] in pb.TYPE_SOURCES


# ---------------------------------------------------------------- splitting


def test_numbered_list():
    data = _run([f"1. {A}", f"2. {B}", f"[3] {C}"], pb.marker_split)
    assert _titles(data) == ["Modelling gene networks with sparse graphs", "Fast alignment of made-up sequences",
                             "Thin films that do not exist"]


def test_bullet_list_and_unnumbered_list():
    assert len(_run([f"• {A}", f"• {B}"], pb.marker_split)["entries"]) == 2
    # bullets removed by stage 2: a new citation follows a full stop and starts like an author list
    assert _titles(_run([A, B, C], pb.marker_split)) == ["Modelling gene networks with sparse graphs",
                                                          "Fast alignment of made-up sequences",
                                                          "Thin films that do not exist"]


def test_hanging_indent():
    lines = [_line(0, "M. Testwood, R. Doe. Modelling gene networks with sparse"),
             _line(1, "graphs. Journal of Invented Biology, 4, 2022.", x0=72),
             _line(2, "M. Testwood. Fast alignment of made-up sequences. Proc."),
             _line(3, "Imaginary Conference on Bioinformatics, 2020.", x0=72)]
    data = _run(None, pb.hanging_indent_split, lines=lines)
    assert _titles(data) == ["Modelling gene networks with sparse graphs", "Fast alignment of made-up sequences"]
    assert data["entries"][1]["venue"] == "Proc. Imaginary Conference on Bioinformatics"


def test_paragraph_style():
    lines = [_line(0, "M. Testwood, R. Doe. Modelling gene networks with sparse", top=100),
             _line(1, "graphs. Journal of Invented Biology, 4, 2022.", top=112),
             _line(2, "M. Testwood. Fast alignment of made-up sequences.", top=140),
             _line(3, "Proc. Imaginary Conference on Bioinformatics, 2020.", top=152)]
    data = _run(None, pb.paragraph_split, lines=lines)
    assert _titles(data) == ["Modelling gene networks with sparse graphs", "Fast alignment of made-up sequences"]


def test_entry_wrapped_across_lines_and_pages():
    lines = [_line(0, "1. M. Testwood, R. Doe. Modelling gene net-", top=700),
             _line(1, "works with sparse graphs. Journal of Invented", top=712),
             _line(2, "Biology, 4, 2022.", top=60, page=2),
             _line(3, f"2. {B}", top=74, page=2)]
    data = _run(None, pb.marker_split, lines=lines)
    assert _titles(data) == ["Modelling gene net-works with sparse graphs", "Fast alignment of made-up sequences"]
    assert data["entries"][0]["venue"] == "Journal of Invented Biology"
    assert data["sides"][0]["lines"] == ["p001_l0000", "p001_l0001", "p002_l0002"]


def test_section_boundary_ends_an_entry():
    payload = pb.publications_payload(_document(("Publications", [_line(0, "1. M. Testwood, R. Doe. Modelling gene")]),
                                                ("Publications", [_line(1, "networks with sparse graphs, 2022.")])),
                                      PARAMS)
    data = pb.whole_section_one_entry(payload).data
    assert len(data["sides"][0]["lines"]) == 2  # the last resort still joins them
    groups = pb._group(payload, lambda i: False)
    assert groups == [[0], [1]]


# ---------------------------------------------------------------- fields and types


def test_quoted_and_unquoted_titles():
    quoted = _run(['[1] J. Fakely, "Phonons in fictional crystals," J. Appl. Toys, vol. 4, pp. 33-40, 2018.',
                   "[2] L. Samplestone, “Prices in an invented market,” in Proc. Placeholder Symposium, 2019."],
                  pb.marker_split)
    assert [(e["title"], e["venue"], e["year"]) for e in quoted["entries"]] == [
        ("Phonons in fictional crystals", "J. Appl. Toys", "2018"),
        ("Prices in an invented market", "Proc. Placeholder Symposium", "2019")]
    side = quoted["sides"][0]
    assert (side["authors"], side["volume"], side["pages"]) == ("J. Fakely", "4", "33-40")
    unquoted = _run([f"1. {A}", "2. Samplestone, L. (2021). Prices in an invented market. Review of Fictional "
                                "Economics, 8(2)."], pb.marker_split)
    assert _titles(unquoted) == ["Modelling gene networks with sparse graphs", "Prices in an invented market"]


def test_journal_and_conference_typing():
    data = _run([f"1. {A}", f"2. {B}", f"3. {C}",
                 "4. M. Testwood, Deep nets for toy data, In: TOYCONF 2020, pp. 1-5.",
                 "5. M. Testwood, Toy models revisited, arXiv preprint arXiv:2101.00001, 2021.",
                 "6. A. Roe and B. Moe, Grains that never formed, Nature Materials, 12, 2019."], pb.marker_split)
    got = [(e["type"], s["type_source"]) for e, s in zip(data["entries"], data["sides"])]
    assert got == [("journal", "venue word"), ("conference", "venue word"), ("journal", "venue word"),
                   ("conference", "cue"), ("other", "cue"), ("journal", "default")]
    assert data["type_default"] == 1


def test_sub_headings_set_the_type_and_are_not_entries():
    lines = [_line(0, "Journal Papers", bold=1.0), _line(1, f"1. {B}"),
             _line(2, "Conference Papers", bold=1.0), _line(3, f"2. {A}")]
    data = _run(None, pb.marker_split, lines=lines)
    assert [(e["title"][:8], e["type"]) for e in data["entries"]] == [("Fast ali", "journal"), ("Modellin", "conference")]
    assert all(s["type_source"] == "subheading" for s in data["sides"])
    # the section heading types entries whose venue has no journal or conference word
    heading = _run(["1. M. Testwood, R. Doe. Modelling gene networks with sparse graphs. Toy Biology Reports, 4, 2022.",
                    f"2. {A}"], pb.marker_split, heading="Conference Proceedings")
    assert [(e["type"], s["type_source"]) for e, s in zip(heading["entries"], heading["sides"])] == [
        ("conference", "section heading"), ("journal", "venue word")]


def test_stated_totals_are_read_and_never_create_entries():
    data = _run(["Total publications: 12", "8 International journal papers", "Conference papers: 4",
                 f"1. {A}", f"2. {B}"], pb.marker_split)
    assert data["stated_totals"] == {"total": 12, "journal": 8, "conference": 4}
    assert len(data["entries"]) == 2
    score, details = pb.check_publications(data, PARAMS)
    assert score == 1.0 and details["entries_minus_stated_total"] == -10


def test_entry_checks():
    def score(texts):
        return pb.check_publications(_run(texts, pb.whole_section_one_entry), PARAMS)[0]
    assert score([A]) == 1.0
    assert score(["M. Testwood, R. Doe. Short note. Journal of Invented Biology, 2022."]) == 0.0  # under 3 words
    assert score(["M. Testwood. A paper from far ahead. Journal of Invented Biology, 2041."]) == 0.0
    assert score([A, B]) == 0.0  # two citations run together
    assert score([A[:-1] + " " + "x" * 800 + "."]) == 0.0  # longer than max_citation_chars


def test_unclear_fields_stay_empty():
    data = _run(["1. some words without any structure at all", f"2. {A}"], pb.marker_split)
    assert _titles(data) == ["Modelling gene networks with sparse graphs"]
    assert data["leftover"] == 1


# ---------------------------------------------------------------- determinism and privacy


def test_publications_are_deterministic(synthetic_pdfs):
    stages = default_stages()[:6]
    a = run_pipeline(synthetic_pdfs[1], stages=stages).results["stage5"]
    b = run_pipeline(synthetic_pdfs[1], stages=stages).results["stage5"]
    assert a.data["publications"] == b.data["publications"] and a.to_report() == b.to_report()


def test_cli_prints_ids_only(data_dir, capsys):
    synth.build_all(data_dir / "pdfs")
    assert cli_main(["run"]) == 0
    out = capsys.readouterr().out
    for line in out.strip().splitlines():
        assert re.match(r"^(r_[0-9a-f]{8} \w+ flags=\d+ unknown_headings=\d+.*|done: .*|groups: .*)$", line), line
    for gt in synth.synthetic_ground_truth().values():
        assert all(p["title"] not in out for p in gt["publications"])


def test_publications_in_a_one_page_cv(tmp_path):
    lines = PERSONAL_TOP + [("heading", "PUBLICATIONS"), ("body", f"1. {A}"), ("body", f"2. {B}")]
    stage5 = run_pipeline(synth.make_lines_pdf(tmp_path / "pubs.pdf", lines), stages=default_stages()[:6]).results["stage5"]
    assert stage5.per_unit["publications"].method_used == "marker_split"
    assert _titles(stage5.data["publications"]) == ["Modelling gene networks with sparse graphs",
                                                    "Fast alignment of made-up sequences"]


# ---------------------------------------------------------------- author lists and title-first citations


def test_comma_author_lists_with_glued_initials_and_full_names():
    data = _run(["1. D. A. Moon, A.K.Roe, Grain growth in toy metals under stress, IJTM, 4(2): 33-40, (2019).",
                 "2. Mira Testwood, Ravi Doe, Lena Hart and Jonas Fakely, Modelling gene networks with sparse graphs, "
                 "ACS Toy Chem., 4, 2022.",
                 "3. A.B. Testwood , S.H. Doe, R.M. Hart, A study of toy fluids (TF) in small pipes, Invented Letters, "
                 "12, 2018."], pb.marker_split)
    assert _titles(data) == ["Grain growth in toy metals under stress", "Modelling gene networks with sparse graphs",
                             "A study of toy fluids (TF) in small pipes"]
    assert data["sides"][1]["authors"] == "Mira Testwood, Ravi Doe, Lena Hart and Jonas Fakely"
    assert data["entries"][0]["type"] == "journal"  # an acronym with no year next to it is no conference cue


def test_title_first_citations():
    data = _run(["1. Toy models of grain growth in metals; C. Moon, T. Roe, R. P. Hart, Fake Mater. Lett. 4 (2019) 1-9.",
                 "2. Graph models of toy cities, C. Moon, T. Roe, Fake Mater. Lett., 4, 2019."], pb.marker_split)
    assert [(e["title"], e["venue"]) for e in data["entries"]] == [
        ("Toy models of grain growth in metals", "Fake Mater. Lett"), ("Graph models of toy cities", "Fake Mater. Lett")]
    assert data["sides"][0]["authors"] == "C. Moon, T. Roe, R. P. Hart"


def test_acronym_venue_next_to_a_year_is_a_conference_cue():
    data = _run(["1. M. Testwood, Deep nets for toy data, TOYCONF 2020, pp. 1-5."], pb.whole_section_one_entry)
    assert (data["entries"][0]["type"], data["sides"][0]["type_source"]) == ("conference", "cue")


# ---------------------------------------------------------------- where an unnumbered citation starts


def test_new_citation_after_a_line_ending_a_citation():
    data = _run(["M. Testwood, R. Doe, Modelling gene networks with sparse graphs, Invented Letters 4, 2019",
                 "J. Fakely, A. Roe, Thin films that do not exist, Invented Physics Letters 12, 2021"], pb.marker_split)
    assert _titles(data) == ["Modelling gene networks with sparse graphs", "Thin films that do not exist"]


def test_wrapped_citation_whose_line_ends_with_a_year_stays_one():
    for second in ("pp. 1-9.", "vol. 4, no. 2, pp. 1-9.", "and other toy systems, Invented Letters 4, pp. 1-9.",
                   "10.1000/toy.2019.1", "https://example.org/toy/2019"):
        data = _run(["J. Fakely, A. Roe, Thin films that do not exist, Invented Physics Letters 12, 2021",
                     "M. Testwood, R. Doe, Toy chemistry revisited, Invented Letters 4, 2019", second], pb.marker_split)
        assert len(data["entries"]) == 2, second
        assert second.split()[0] in data["sides"][1]["raw_text"], second


def test_venue_word_comes_before_the_section_heading():
    data = _run([f"1. {A}", f"2. {B}",
                 "3. M. Testwood, Toy rules for small games, Journal and Proceedings of the Toy Society, 2020."],
                pb.marker_split, heading="Journal Publications")
    got = [(e["type"], s["type_source"]) for e in data["entries"] for s in [data["sides"][data["entries"].index(e)]]]
    assert got == [("journal", "venue word"), ("conference", "venue word"), ("journal", "section heading")]


def test_weak_venue_words_yield_to_the_section_heading():
    texts = ["1. M. Testwood, Toy rules for small games, Proceedings of the Toy Society, 2020.",
             "2. M. Testwood, Toy rules for large games, Toy Conference Series, 2021."]
    under_heading = _run(texts, pb.marker_split, heading="Journal Publications")
    assert [(e["type"], s["type_source"]) for e, s in zip(under_heading["entries"], under_heading["sides"])] == [
        ("journal", "section heading"), ("conference", "venue word")]  # weak word yields, strong word does not
    no_heading = _run(texts, pb.marker_split, heading="Publications")
    assert [e["type"] for e in no_heading["entries"]] == ["conference", "conference"]


def test_venue_without_a_venue_word():
    # the first usable segment after the title; volume, pages and years are never a venue
    data = _run(["1. M. Testwood, R. Doe, Modelling gene networks with sparse graphs, Nature Toys, 12, 2019.",
                 "2. M. Testwood, R. Doe, Thin films that do not exist, vol. 4, pp. 1-9, 2019.",
                 "3. M. Testwood, R. Doe, Toy chemistry revisited, 12, 2020, Placeholder Toy Notes."],
                pb.marker_split)
    assert [e["venue"] for e in data["entries"]] == ["Nature Toys", "", "Placeholder Toy Notes"]
    assert pb.find_venue(", 12, pp. 1-9, 2019.") == ("", False)


# ---------------------------------------------------------------- S5 loss fixes


def test_title_case_title_after_an_initials_author_list():
    t, _s, _e, authors = pb.find_title("Testwood, M. K.; Doe, R. Modelling Gene Networks With Sparse Graphs. "
                                       "Journal of Invented Biology, 4, 2022.")
    assert t == "Modelling Gene Networks With Sparse Graphs" and authors.startswith("Testwood, M. K.; Doe")
    # another name after an initial is never taken as the title
    for text in ("A. Ravi Testwood and B. Doe, Toy rules for small games, Toy Notes, 2020.",
                 "A. Ravi Kumar Testwood, B. Doe, Toy rules for small games, Toy Notes, 2020.",
                 "M. Testwood C. Doe, Toy rules for small games, Toy Notes, 2020."):
        assert pb.find_title(text)[0] == "Toy rules for small games", text


def test_initials_extracted_with_a_space_before_the_dot():
    line = {"id": "l0", "text": "1. Testwood, M . K .; Doe, R . Modelling gene networks with sparse graphs. "
                                "Journal of Invented Biology, 4, 2022.", "page": 1}
    entry = pb.parse_entry([line], PARAMS)["entry"]
    assert entry["title"] == "Modelling gene networks with sparse graphs"
    assert entry["venue"] == "Journal of Invented Biology" and entry["year"] == "2022"


def test_citation_ending_without_a_year_or_pages_still_ends_at_the_next_author_list():
    texts = ["1. M. Testwood, R. Doe. Modelling gene networks with sparse graphs. Journal of Invented Biology, 2022,",
             "article e1234",
             "J. Fakely, A. Roe. Thin films that do not exist. Invented Physics Letters, 2021, article e77",
             "2. R. Doe, M. Testwood. Toy chemistry revisited. Placeholder Toy Notes, 2020."]
    data = _run(texts, pb.marker_split)
    assert _titles(data) == ["Modelling gene networks with sparse graphs", "Thin films that do not exist",
                             "Toy chemistry revisited"]
    # an author list that wraps (no title or year read yet) never starts an entry
    wrapped = _run(["1. M. Testwood, R. Doe, J. Fakely,", "A. Roe. Modelling gene networks with sparse graphs. "
                    "Journal of Invented Biology, 2022.", "2. M. Testwood. Toy chemistry revisited. Toy Notes, 2020."],
                   pb.marker_split)
    assert _titles(wrapped) == ["Modelling gene networks with sparse graphs", "Toy chemistry revisited"]


def test_bare_list_numbers_start_entries_only_in_a_sequence_of_three():
    texts = ["1 M. Testwood, R. Doe, Modelling gene networks with sparse graphs, Journal of Invented Biology, 2022",
             "2 J. Fakely, A. Roe, Thin films that do not exist, Invented Physics Letters, 2021",
             "3 R. Doe, Toy chemistry revisited, Placeholder Toy Notes, 2020"]
    assert len(_run(texts, pb.marker_split)["entries"]) == 3
    down = list(reversed(["3" + t[1:] if t[0] == "1" else "1" + t[1:] if t[0] == "3" else t for t in texts]))
    assert len(_run(down, pb.marker_split)["entries"]) == 3
    lines = [_line(i, t) for i, t in enumerate(texts[:2] + ["12 Placeholder Toy Notes, 2020"])]
    assert pb._bare_number_starts(lines, ["entry"] * 3) == set()  # 1, 2, 12: no run of three
