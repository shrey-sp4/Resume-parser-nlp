"""Institution tiers (src/shortlisting/tiers/institutions.py) on invented NIRF-style pages: the
tier boundaries, the best rank across categories, band pages, the pages never read, the
aliases and patterns, campuses, the wrong-match guard and the global seed list."""

from __future__ import annotations

import json

import pytest

from resume_parser.schema import load_config
from shortlisting.tiers import institutions as inst_mod
from shortlisting.tiers.institutions import InstitutionIndex, load_nirf, parse_nirf_page

INCLUDED = load_config("tiers.json")["nirf"]["categories_included"]


def ranked_page(category: str, rows: list[tuple], score: bool = True, repeat_long_rows: bool = False) -> str:
    """rows: (nirf_id, name, city, state, rank). Each name cell holds a nested detail table, as on
    the real pages; repeat_long_rows adds the 8-cell repeated rows seen on one saved page."""
    head = "<th>Institute ID</th><th>Name</th><th>City</th><th>State</th>" + ("<th>Score</th>" if score else "") + "<th>Rank</th>"
    body = ""
    for nid, name, city, state, rank in rows:
        detail = "<table><tr><td>99.0</td><td>88.0</td><td>77.0</td><td>66.0</td><td>55.0</td></tr></table>"
        body += (f"<tr><td>{nid}</td><td>{name}<br>More Details | <a href='#'>x</a>{detail}</td><td>{city}</td>"
                 f"<td>{state}</td>" + ("<td>70.00</td>" if score else "") + f"<td>{rank}</td></tr>")
        if repeat_long_rows:
            body += f"<tr><td>{nid}</td><td>{name}</td><td>{city}</td><td>{state}</td><td>1</td><td>2</td><td>3</td><td>{rank}</td></tr>"
    return (f"<html><body><h2>India Rankings 2025: {category}</h2><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{body}</tbody></table></body></html>")


def band_page(category: str, band: str, rows: list[tuple]) -> str:
    body = "".join(f"<tr><td>{n}</td><td>{c}</td><td>{s}</td></tr>" for n, c, s in rows)
    return (f"<html><body><h3>India Rankings 2025: {category} (Rank-band: {band})</h3><table><thead><tr><th>Name</th>"
            f"<th>City</th><th>State</th></tr></thead><tbody>{body}</tbody></table></body></html>")


PAGES = {
    "OverallRanking": ranked_page("Overall", [
        ("IR-O-U-0001", "Indian Institute of Technology Delhi", "New Delhi", "Delhi", 2),
        ("IR-O-U-0002", "Toyland University", "Toytown", "Toy Pradesh", 25),
        ("IR-O-U-0003", "Imaginary Institute of Technology Toyland", "Toyland", "Toy Pradesh", 26),
        ("IR-O-U-0004", "Pretend Institute of Science", "Pretendpur", "Pretend Nadu", 100),
        ("IR-O-U-0005", "S.R.M. Institute of Science and Technology", "Chennai", "Tamil Nadu", 22),
        ("IR-O-I-0006", "Imaginary School of Mines", "Minetown", "Toy Pradesh", 60),
    ], repeat_long_rows=True),
    "OverallRanking150": band_page("Overall", "101-150", [("Fictional College of Arts", "Artville", "Toy Pradesh")]),
    "OverallRanking200": band_page("Overall", "151-200", [("Toyland University", "Toyburg", "Pretend Nadu")]),
    "EngineeringRanking": ranked_page("Engineering", [
        ("IR-E-U-0003", "Imaginary Institute of Technology Toyland", "Toyland", "Toy Pradesh", 12),
    ]),
    "EngineeringRanking300": band_page("Engineering", "201-300", [("Faraway Institute of Engineering", "Farville", "Pretend Nadu"),
                                                                ("Dhirubhai Ambani Institute of Information and Communication Technology",
                                                                 "Gandhinagar", "Gujarat")]),
    "EngineeringRanking150": band_page("Engineering", "101-150", [("Pretend Institute of Science", "Pretendpur", "Pretend Nadu")]),
    "LawRanking": ranked_page("Law", [("IR-L-U-0007", "Make Believe Law School", "Lawtown", "Toy Pradesh", 30)]),
    "DentalRanking": ranked_page("Dental", [("IR-D-C-0008", "Toy Dental College", "Toytown", "Toy Pradesh", 20)]),
    "InnovationRanking": ranked_page("Innovation", [("IR-N-U-0009", "Unranked Innovation Hub", "Hubtown", "Toy Pradesh", 1)],
                                     score=False),
    "OverallRankingALL": band_page("Overall", "1-9999", [("Participant Only Institute", "Nowhere", "Toy Pradesh")]),
}
OVERALL_90 = ("IR-O-U-0007", "Make Believe Law School", "Lawtown", "Toy Pradesh", 90)


@pytest.fixture
def index(tmp_path):
    folder = tmp_path / "nirf_2025"
    folder.mkdir()
    pages = dict(PAGES)
    pages["UniversityRanking"] = ranked_page("University", [OVERALL_90])
    for stem, html in pages.items():
        (folder / f"{stem}.html").write_text(html, encoding="utf-8")
    entries, missing = load_nirf(folder, INCLUDED)
    assert "Innovation" not in {e.category for e in entries}  # excluded category never read
    assert "Overall" not in missing and "Research" in missing
    return InstitutionIndex(entries)


def test_page_parsing_ignores_nested_and_repeated_rows():
    rows = parse_nirf_page(PAGES["OverallRanking"], "Overall")
    assert len(rows) == 6 and rows[0].name == "Indian Institute of Technology Delhi" and rows[0].nirf_id == "U-0001"
    band = parse_nirf_page(PAGES["OverallRanking150"], "Overall")
    assert [(b.rank_low, b.rank_label, b.nirf_id) for b in band] == [(101, "101-150", None)]


@pytest.mark.parametrize("name, field, tier, rank, category", [
    ("Toyland University, Toytown", None, "Tier 1", "25", "Overall"),                       # 25: last of Tier 1
    ("Imaginary Institute of Technology Toyland", None, "Tier 2", "26", "Overall"),        # 26: first of Tier 2
    ("Imaginary Institute of Technology Toyland", "software_networking", "Tier 1", "12", "Engineering"),
    ("Pretend Institute of Science", "vlsi_embedded", "Tier 2", "100", "Overall"),        # 100 beats its 101-150 band
    ("Fictional College of Arts", None, "Tier 3", "101-150", "Overall"),                   # a band counts at its lower bound
    ("Toyland University, Toyburg, Pretend Nadu", None, "Tier 3", "151-200", "Overall"),   # the other campus
    ("Make Believe Law School", None, "Tier 2", "90", "University"),                       # no field: Law does not count
    ("Make Believe Law School", "law", "Tier 2", "30", "Law"),                             # Law field: Law 30 beats University 90
])
def test_tier_boundaries_and_best_rank(index, name, field, tier, rank, category):
    r = index.tier(name, field)
    assert (r["tier"], r["best_rank_label"], r["best_category"], r["status"], r["review"]) == (tier, rank, category, "listed", False)


def test_boundary_26_and_beyond_200(index):
    r = index.tier("Imaginary School of Mines")
    assert (r["tier"], r["best_rank"]) == ("Tier 2", 60)
    r = index.tier("Faraway Institute of Engineering", "ai_ml_data_science")
    assert (r["tier"], r["status"], r["review"], r["reason"]) == (None, "unlisted", True, "ranked_beyond_200")
    assert index.tier("Faraway Institute of Engineering")["reason"] == "no_rank_in_counted_categories"
    bands = [tuple(b) for b in load_config("tiers.json")["nirf"]["tier_bands"]]
    assert bands == [(1, 25, "Tier 1"), (26, 100, "Tier 2"), (101, 200, "Tier 3")]
    assert [index._tier_of(n) for n in (1, 25, 26, 100, 101, 200, 201)] == ["Tier 1", "Tier 1", "Tier 2", "Tier 2", "Tier 3", "Tier 3", None]


def test_pages_and_categories_never_read(index):
    for name in ("Participant Only Institute", "Unranked Innovation Hub"):
        r = index.tier(name)
        assert (r["tier"], r["status"], r["review"]) == (None, "unlisted", True)


def test_aliases_patterns_and_the_city_column(index):
    for q in ("IIT Delhi", "IIT-Delhi", "Indian Institute of Technology, Delhi", "Dept. of Physics, IIT Delhi, New Delhi"):
        assert index.tier(q)["tier"] == "Tier 1", q
    assert index.tier("IIT Delhi")["method"] == "pattern"
    index.overrides = {}  # the NIRF rule on its own; the editorial override is tested below
    for q in ("DA-IICT, Gandhinagar", "DAU", "Dhirubhai Ambani University"):  # NIRF lists the former name
        r = index.tier(q, "ai_ml_data_science")
        assert (r["method"], r["matched_name"], r["best_rank_label"], r["tier"], r["review"]) == (
            "alias", "Dhirubhai Ambani Institute of Information and Communication Technology", "201-300", None, True), q
    for q in ("SRMIST", "SRM Institute of Science and Technology"):  # NIRF writes S.R.M.
        assert (index.tier(q)["tier"], index.tier(q)["method"]) == ("Tier 1", "alias"), q
    assert index.tier("SRM University")["tier"] is None  # also names other SRM universities: not an alias
    assert index.tier("Imaginary School of Mines Minetown")["tier"] == "Tier 2"  # name plus the city column
    r = index.tier("Department of Toys, Imaginary Institute of Technology Toyland, Toyland")
    assert (r["tier"], r["from_part"]) == ("Tier 2", True)


def test_campuses_need_a_city_or_agreeing_tiers(index):
    r = index.tier("Toyland University")
    assert (r["tier"], r["status"], r["review"], r["reason"]) == (None, "ambiguous", True, "ambiguous_campus")
    assert index.tier("Toyland University (Toy Pradesh)")["tier"] == "Tier 1"


def test_wrong_fuzzy_matches_are_refused(index):
    r = index.tier("Imaginary Institute of Technology Toylandd")  # a typo is accepted
    assert (r["tier"], r["method"], r["certainty"]) == ("Tier 2", "fuzzy", "medium")
    for q in ("Imaginary Institute of Technology Toytown",  # another city is another institute
              "Institute of Technology Imaginary Toyland",  # word order matters for institutions
              "Toy Dental Colleges of Pretendia"):
        r = index.tier(q)
        assert r["tier"] is None and r["review"] is True, q


def test_global_seed(index):
    r = index.tier("Stanford University")
    assert (r["tier"], r["source"], r["review"]) == ("Tier 1", "global_seed", False)
    assert index.tier("LSE")["tier"] == "Tier 1" and index.tier("Royal College of Art, London")["tier"] == "Tier 1"
    assert index.tier("University of Washington")["tier"] == "Tier 1"
    for q in ("Washington University", "University of Imaginary Studies, Freedonia", "Freedonia Institute of Technology"):
        r = index.tier(q)
        assert (r["tier"], r["status"], r["review"]) == (None, "unlisted", True), q
    assert index.tier("")["reason"] == "no_institution"


def test_seed_list_is_editorial_and_about_a_hundred():
    seed = load_config("institution_global_seed.json")
    assert "NOT A RANKING" in seed["_comment"] and "not copied" in seed["_comment"]
    assert seed["tier"] == "Tier 1" and 90 <= len(seed["institutions"]) <= 120
    assert all(s["country"] != "India" for s in seed["institutions"])
    names = [s["name"] for s in seed["institutions"]]
    assert len(names) == len(set(names))
    aliases = [a for s in seed["institutions"] for a in s["aliases"]]
    assert "MIT" not in aliases and "NTU" not in aliases  # forms that name more than one institution
    json.dumps(seed)


# ---------------------------------------------------------------- field-aware categories


def test_a_top25_dental_rank_is_not_tier1_for_a_computing_field(index):
    r = index.tier("Toy Dental College", "ai_ml_data_science")
    assert (r["tier"], r["status"], r["review"], r["reason"]) == (None, "unlisted", True, "no_rank_in_counted_categories")
    assert r["categories"] == [{"category": "Dental", "rank": "20", "counted": False}]
    assert r["counted_categories"] == ["Overall", "University", "College", "Research", "Engineering"]


def test_a_top25_engineering_rank_is_tier1_for_an_engineering_field(index):
    for field in ("ai_ml_data_science", "algorithms_theory", "communications_signal_processing", "software_networking", "vlsi_embedded"):
        r = index.tier("Imaginary Institute of Technology Toyland", field)
        assert (r["tier"], r["best_category"], r["best_rank"]) == ("Tier 1", "Engineering", 12), field
    for field in ("physics_mathematical_sciences", "interdisciplinary", "economics"):
        assert index.tier("Imaginary Institute of Technology Toyland", field)["best_category"] == "Overall", field


def test_no_field_means_the_general_categories_only(index):
    r = index.tier("Imaginary Institute of Technology Toyland")
    assert (r["tier"], r["best_category"], r["field"]) == ("Tier 2", "Overall", None)
    assert r["counted_categories"] == ["Overall", "University", "College", "Research"]
    assert {c["category"]: c["counted"] for c in r["categories"]} == {"Engineering": False, "Overall": True}
    assert index.tier("Toy Dental College")["tier"] is None


def test_law_field_uses_law(index):
    r = index.tier("Make Believe Law School", "law")
    assert (r["tier"], r["best_category"], r["best_rank"]) == ("Tier 2", "Law", 30)
    assert "Law" not in index.counted_categories("economics")


def test_field_mapping_covers_every_role_field():
    nirf = load_config("tiers.json")["nirf"]
    assert set(nirf["field_categories"]) == set(load_config("venue_fields.json")["fields"])
    assert set(nirf["always_counted"]) | set(nirf["discipline_categories"]) == set(nirf["categories_included"])
    assert all(set(v) <= set(nirf["discipline_categories"]) for v in nirf["field_categories"].values())
    assert nirf["field_categories"]["economics"] == ["Management"] and nirf["field_categories"]["interdisciplinary"] == []


def test_an_unknown_field_is_an_error(index):
    with pytest.raises(KeyError):
        index.tier("Toyland University", "no_such_field")

# ---------------------------------------------------------------- editorial overrides


def test_dau_takes_the_editorial_override(index):
    for q in ("DAU", "DA-IICT", "DA-IICT, Gandhinagar", "Dhirubhai Ambani University"):
        for field in (None, "ai_ml_data_science", "law"):
            r = index.tier(q, field)
            assert (r["tier"], r["status"], r["review"]) == ("Tier 2", "listed", False), (q, field)
            assert r["source"] == r["method"] == r["reason"] == "editorial override", q
            assert r["best_rank"] is None  # not a NIRF rank


def test_removing_the_override_file_returns_dau_to_unlisted(tmp_path, monkeypatch):
    monkeypatch.setattr(inst_mod, "overrides_path", lambda: tmp_path / "no_such_file.json")
    assert inst_mod.load_overrides() == {"overrides": []}
    entries = [e for stem, html in PAGES.items() if not stem.endswith("ALL")
               for e in parse_nirf_page(html, stem.replace("Ranking", "").rstrip("0123456789"))]
    ix = InstitutionIndex(entries)
    for q in ("DAU", "DA-IICT"):
        r = ix.tier(q, "ai_ml_data_science")
        assert (r["tier"], r["status"], r["review"], r["source"]) == (None, "unlisted", True, "nirf2025"), q


def test_overrides_leave_srm_and_iacs_alone(index):
    r = index.tier("SRMIST")
    assert (r["tier"], r["source"], r["method"]) == ("Tier 1", "nirf2025", "alias")
    r = index.tier("Indian Association for the Cultivation of Science")
    assert (r["tier"], r["status"], r["source"]) == (None, "unlisted", None)
    names = {o["name"] for o in load_config("institution_overrides.json")["overrides"]}
    assert names == {"Dhirubhai Ambani University"}
    assert all(o["source"] == "editorial override" for o in load_config("institution_overrides.json")["overrides"])