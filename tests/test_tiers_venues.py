"""Venue tiers (src/shortlisting/tiers/venues.py) on tiny invented lexicons: abbreviation
expansion, exact / ISSN / acronym / fuzzy matching, the wrong-match guards, unknown handling,
a field (economics) example and conference examples. Every venue name here is made up."""

from __future__ import annotations

import csv

import pytest

from resume_parser.schema import REPO_ROOT, load_config
from shortlisting.tiers import lexicons
from shortlisting.tiers.text import aligned, content_tokens, normalise, variants
from shortlisting.tiers.venues import VenueIndex, load_core, load_scimago

HEAD = ["Rank", "Sourceid", "Title", "Type", "Issn", "Publisher", "Open Access", "Open Access Diamond", "SJR",
        "SJR Best Quartile", "H index", "Total Docs. (2025)", "Total Docs. (3years)", "Total Refs.",
        "Total Citations (3years)", "Citable Docs. (3years)", "Citations / Doc. (2years)", "Ref. / Doc.", "%Female",
        "Overton", "Country", "Region", "Publisher", "Coverage", "Categories", "Areas"]

JOURNALS = [  # title, type, issn, best quartile, categories, areas
    ("Journal of Imaginary Signal Processing", "journal", "12345678", "Q1", "Signal Processing (Q1)", "Engineering"),
    ("Transactions on Imaginary Image Processing", "journal", "", "Q2", "Signal Processing (Q2)", "Engineering"),
    ("Annals of Imaginary Speech Processing and Toy Pattern Analysis and Recognition Systems", "journal", "", "Q2", "Signal Processing (Q2)", "Engineering"),
    ("Letters on Imaginary Signal Processing", "journal", "", "Q3", "Signal Processing (Q3)", "Engineering"),
    ("Review of Toy Physics A", "journal", "", "Q1", "Condensed Matter Physics (Q1)", "Physics and Astronomy"),
    ("Review of Toy Physics B", "journal", "", "Q2", "Condensed Matter Physics (Q2)", "Physics and Astronomy"),
    ("Journal of Pretend Finance", "journal", "", "Q2", "Finance (Q2)", "Economics, Econometrics and Finance"),
    ("Journal of Pretend Financial Economics", "journal", "", "Q1",
     "Economics and Econometrics (Q1); Applied Mathematics (Q3)", "Economics, Econometrics and Finance; Mathematics"),
    ("Journal of Toy Networking Systems", "journal", "", "Q2", "Software (Q2)", "Computer Science"),
    ("Journal of Toy Networked Systems", "journal", "", "Q3", "Software (Q3)", "Computer Science"),
    ("Toy Optics", "journal", "", "Q4", "Software (Q4)", "Computer Science"),
    ("Bulletin of Unrated Things", "journal", "", "-", "Software", "Computer Science"),
    ("Pretend Conference Proceedings Series", "conference and proceedings", "", "Q1", "Software (Q1)", "Computer Science"),
]

CONFERENCES = [  # id, title, acronym, source, rank
    ("1", "International Conference on Pretend Learning", "ICPL", "ICORE2026", "A*"),
    ("2", "Symposium on Toy Networks", "STN", "ICORE2026", "B"),
    ("3", "Theory of Toy Cryptography Conference", "TCC", "ICORE2026", "A"),
    ("4", "Toy Computing and Control Conference", "TCC", "ICORE2026", "C"),
    ("5", "National Conference on Toy Things", "NCTT", "ICORE2026", "National: India"),
    ("6", "Conference on Pretend Robotics, Agents and Systems", "CPRAS", "ICORE2026", "C"),
]

FIELDS = {"fields": {
    "economics": {"label": "Economics", "categories": ["Economics and Econometrics", "Finance"], "areas": []},
    "physics_math": {"label": "Physics and Mathematical Sciences", "categories": ["Applied Mathematics", "Condensed Matter Physics"], "areas": []},
    "law": {"label": "Law", "categories": ["Finance"], "areas": []},
}}
ACRONYMS = {"journals": {"JIPS": "Journal of Imaginary Signal Processing", "TIIP": "Transactions on Imaginary Image Processing"},
            "conference_aliases": {"ICPLEARN": "ICPL"},
            "conference_title_aliases": {"Advances in Pretend Learning": "ICPL"}}


def write_lexicons(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "scimagojr 2025.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(HEAD)
        for k, (title, ty, issn, q, cats, areas) in enumerate(JOURNALS, 1):
            row = [""] * len(HEAD)
            row[0], row[1], row[2], row[3], row[4], row[9], row[24], row[25] = str(k), str(k), title, ty, issn, q, cats, areas
            w.writerow(row)
    (folder / "core").mkdir(exist_ok=True)
    with (folder / "core" / "ICORE2026.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        for row in CONFERENCES:
            w.writerow([*row, "Yes", "4602", "", ""])
    return folder


@pytest.fixture
def index(tmp_path):
    folder = write_lexicons(tmp_path / "lex")
    return VenueIndex(load_scimago(folder / "scimagojr 2025.csv"), load_core(folder / "core" / "ICORE2026.csv"),
                      fields=FIELDS, acronyms=ACRONYMS, scimago_label="scimago2025")


# ---------------------------------------------------------------- text


def test_abbreviations_expand_with_and_without_a_full_stop():
    abbr = load_config("venue_abbreviations.json")
    assert "transactions imaginary image processing" in variants("Trans. Imaginary Image Process.", abbr)
    assert variants("Int. J. Toy Res.", abbr)[-1] == "international journal toy research"
    assert {"journal toy computing", "journal toy computer"} <= set(variants("J. Toy Comput.", abbr))  # ambiguous
    assert "process" in variants("Toy Process Studies", abbr)[0].split()  # an ordinary word without a full stop
    assert normalise("The Journal of Toys & Games (discontinued)") == "journal of toys and games"


def test_token_guard():
    assert aligned(content_tokens("journal imaginary signal processing"), content_tokens("journal of imaginary signal processing"))
    assert not aligned(["signal", "processing"], ["image", "processing"])
    assert not aligned(["review", "toy", "physics", "a"], ["review", "toy", "physics", "b"])
    assert not aligned(["letters", "signal"], ["transactions", "signal"])
    assert aligned(["comput", "vis"], ["computer", "vision"])  # abbreviation prefixes


# ---------------------------------------------------------------- journals


def test_exact_abbreviated_issn_and_acronym_matches(index):
    r = index.tier("Journal of Imaginary Signal Processing", "journal")
    assert (r["tier"], r["method"], r["certainty"], r["source"]) == ("A", "exact", "high", "scimago2025")
    r = index.tier("J. Imaginary Signal Process.", "journal")
    assert (r["tier"], r["method"]) == ("A", "exact")
    assert index.tier("Some Journal, ISSN 1234-5678", "journal")["method"] == "issn"
    r = index.tier("JIPS", "journal")
    assert (r["tier"], r["method"], r["matched_title"]) == ("A", "acronym", "Journal of Imaginary Signal Processing")
    assert index.tier("Transactions on Imaginary Image Processing (TIIP)", "journal")["tier"] == "B"


def test_fuzzy_match_absorbs_a_typo(index):
    r = index.tier("Journal of Imaginary Signal Procesing", "journal")
    assert (r["tier"], r["method"], r["certainty"]) == ("A", "fuzzy", "medium")
    assert 90 <= r["match_score"] < 100


@pytest.mark.parametrize("venue, reason", [
    ("Annals of Imaginary Speed Processing and Toy Pattern Analysis and Recognition Systems", "rejected_by_token_guard"),  # scores 98, but Speed is not Speech
    ("Transactions on Imaginary Signal Processing", "no_candidate"),  # too far even before the guard
    ("Review of Toy Physics C", "rejected_by_token_guard"),                       # C is neither A nor B
    ("Journal of Toy Network Systems", "ambiguous_margin"),                       # fits two titles equally
    ("Toy Optic", "short_name"),                                                  # short names: exact only
    ("Journal of Wholly Unknown Things", "no_candidate"),
])
def test_wrong_fuzzy_matches_are_refused(index, venue, reason):
    r = index.tier(venue, "journal")
    assert r["tier"] is None and r["matched_title"] is None and r["certainty"] == "none"
    assert r["reason"] == reason


def test_unknown_is_neutral(index):
    for venue, ty, reason in (("Bulletin of Unrated Things", "journal", "no_quartile"),
                              ("Journal of Imaginary Signal Processing", "other", "type_not_tiered"),
                              ("", "journal", "no_venue"),
                              ("Pretend Conference Proceedings Series", "journal", "no_candidate")):
        r = index.tier(venue, ty)
        assert r["tier"] is None and r["reason"] == reason, venue
    assert index.tier("Journal of Imaginary Signal Processing", None)["reason"] == "type_not_tiered"


def test_economics_field_uses_the_best_quartile_in_the_field(index):
    venue = "J. Pretend Financ. Econ."
    r = index.tier(venue, "journal")
    assert (r["tier"], r["quartile"], r["out_of_field"]) == ("A", "Q1", None)  # no field: best overall
    r = index.tier(venue, "journal", "economics")
    assert (r["tier"], r["out_of_field"], r["field_categories"]) == ("A", False, ["Economics and Econometrics"])
    r = index.tier(venue, "journal", "physics_math")
    assert (r["tier"], r["quartile"], r["out_of_field"]) == ("C", "Q3", False)  # Applied Mathematics is Q3
    r = index.tier("Review of Toy Physics A", "journal", "law")
    assert (r["tier"], r["out_of_field"]) == ("A", True)  # nothing in the field: best overall, marked
    assert {c["name"] for c in index.tier(venue, "journal")["categories"]} == {"Economics and Econometrics", "Applied Mathematics"}


def test_a_comma_part_is_tried_but_never_a_one_word_part(index):
    r = index.tier("Journal of Pretend Finance, Pretend Press, Toyland", "journal")
    assert (r["tier"], r["from_part"]) == ("B", True)
    assert index.tier("Toy Optics, Toyland", "journal")["tier"] == "C"  # "Toy Optics" is two words
    assert index.tier("Wholly Unknown, Toyland", "journal")["tier"] is None


# ---------------------------------------------------------------- conferences


def test_conference_examples(index):
    r = index.tier("Proceedings of the 12th International Conference on Pretend Learning (ICPL 2021)", "conference")
    assert (r["tier"], r["method"], r["core_rank"], r["source"]) == ("A", "exact", "A*", "icore2026")
    assert index.tier("ICPL 2022, Toyland", "conference")["method"] == "acronym"
    assert index.tier("Proc. ICPLEARN", "conference")["tier"] == "A"  # an alias of the acronym
    assert index.tier("Advances in Pretend Learning", "conference")["method"] == "title_alias"
    assert index.tier("IEEE Symposium on Toy Networks 2020", "conference")["tier"] == "B"
    # an acronym listed twice: shared title words decide, otherwise ambiguous
    assert index.tier("TCC 2021: Theory of Toy Cryptography", "conference")["tier"] == "A"
    assert index.tier("TCC 2021", "conference")["reason"] == "ambiguous_acronym"
    # a workshop never takes the main conference's rank; national ranks are not tiered
    assert index.tier("Workshop on Toy Agents at ICPL 2021", "conference")["reason"] == "satellite_event_not_ranked"
    r = index.tier("National Conference on Toy Things", "conference")
    assert (r["tier"], r["core_rank"], r["reason"]) == (None, "National: India", "core_rank_not_tiered")
    assert index.tier("Conference on Pretend Robotics, Agents and Systems", "conference")["tier"] == "C"  # quoted title
    # conferences never use the journal list, journals never the conference list
    assert index.tier("Pretend Conference Proceedings Series", "conference")["tier"] is None
    assert index.tier("International Conference on Pretend Learning", "journal")["tier"] is None


# ---------------------------------------------------------------- loading


def test_field_names_must_exist_in_the_journal_list(tmp_path):
    folder = write_lexicons(tmp_path / "lex")
    bad = {"fields": {"x": {"categories": ["Imaginary Category"], "areas": ["Imaginary Area"]}}}
    with pytest.raises(lexicons.LexiconError):
        VenueIndex(load_scimago(folder / "scimagojr 2025.csv"), load_core(folder / "core" / "ICORE2026.csv"), fields=bad)
    ix = VenueIndex(load_scimago(folder / "scimagojr 2025.csv"), load_core(folder / "core" / "ICORE2026.csv"),
                    fields=bad, strict_fields=False)
    assert ix.unknown_field_names() == ["Imaginary Area", "Imaginary Category"]


def test_real_field_mapping_and_acronyms_are_well_formed():
    fields = load_config("venue_fields.json")["fields"]
    assert len(fields) == 11 and all(f["categories"] for f in fields.values())
    acr = load_config("venue_acronyms.json")
    assert all(k == k.strip() and v for k, v in acr["journals"].items())


def test_lexicon_folder_outside_the_repo_only(tmp_path, monkeypatch):
    monkeypatch.setenv(lexicons.ENV_LEXICONS, str(REPO_ROOT / "config"))
    with pytest.raises(lexicons.LexiconError, match="outside the repo"):
        lexicons.lexicon_dir()
    monkeypatch.setenv(lexicons.ENV_LEXICONS, str(tmp_path / "missing"))
    with pytest.raises(lexicons.LexiconError):
        lexicons.lexicon_dir()
    write_lexicons(tmp_path / "lex")
    monkeypatch.setenv(lexicons.ENV_LEXICONS, str(tmp_path / "lex"))
    assert lexicons.lexicon_file("core").name == "ICORE2026.csv"
