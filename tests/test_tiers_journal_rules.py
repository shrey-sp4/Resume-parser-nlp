"""The three journal rules of src/shortlisting/tiers/venues.py (prefix, colon, publisher): one
match and one refused (ambiguous) case each, on an invented journal list. They run only when
nothing else matched, and each match records its rule as the method."""

from __future__ import annotations

import csv

import pytest

from shortlisting.tiers.venues import VenueIndex, load_core, load_scimago
from test_tiers_venues import CONFERENCES, FIELDS, HEAD

JOURNALS = [  # title, best quartile
    ("Bulletin of the Pretendological Hydrodynamics Society", "Q1"),
    ("Annals of Pretendology Research", "Q2"),
    ("Annals of Pretendonomics Research", "Q3"),
    ("Toy Robotics: Theory and Practice", "Q2"),
    ("Review of Pretend Botany: Series A", "Q1"),
    ("Review of Pretend Botany: Series B", "Q3"),
    ("Journal of Invented Hydrology", "Q3"),
    ("Journal of Toy Networking Systems", "Q2"),
    ("Journal of Toy Networked Systems", "Q3"),
]


@pytest.fixture
def index(tmp_path):
    with (tmp_path / "scimago.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(HEAD)
        for k, (title, q) in enumerate(JOURNALS, 1):
            row = [""] * len(HEAD)
            row[0], row[1], row[2], row[3], row[9], row[24], row[25] = str(k), str(k), title, "journal", q, f"Software ({q})", "Computer Science"
            w.writerow(row)
    with (tmp_path / "core.csv").open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows([[*r, "Yes", "4602", "", ""] for r in CONFERENCES])
    return VenueIndex(load_scimago(tmp_path / "scimago.csv"), load_core(tmp_path / "core.csv"), fields=FIELDS,
                      strict_fields=False, acronyms={"journals": {}, "conference_aliases": {}})


def test_prefix_rule(index):
    venue = "Bull. Pretendol. Hydrodyn. Soc."  # "Pretendol." and "Hydrodyn." are in no abbreviation table
    assert index.journal_matcher.match(["bulletin pretendol hydrodyn society"]).key is None  # fuzzy alone: no
    r = index.tier(venue, "journal")
    assert (r["tier"], r["method"], r["certainty"], r["matched_title"]) == (
        "A", "prefix", "medium", "Bulletin of the Pretendological Hydrodynamics Society")
    r = index.tier("Ann. Pretend. Res.", "journal")  # "Pretend." begins two titles' words: refused
    assert (r["tier"], r["method"], r["reason"]) == (None, None, "ambiguous_prefix")
    # a prefix only for a dotted abbreviation, never for a word written in full
    assert index.tier("Bulletin of Pretendol Hydrodyn Society", "journal")["tier"] is None


def test_colon_rule(index):
    r = index.tier("Toy Robotics", "journal")
    assert (r["tier"], r["method"], r["matched_title"]) == ("B", "colon", "Toy Robotics: Theory and Practice")
    r = index.tier("Review of Pretend Botany", "journal")  # two subtitles: refused
    assert (r["tier"], r["method"], r["reason"]) == (None, None, "ambiguous_colon")


def test_publisher_rule(index):
    r = index.tier("Elsevier Journal of Invented Hydrology", "journal")
    assert (r["tier"], r["method"], r["certainty"]) == ("C", "publisher", "medium")
    r = index.tier("Springer Journal of Toy Network Systems", "journal")  # fits two titles equally: refused
    assert (r["tier"], r["method"], r["reason"]) == (None, None, "ambiguous_publisher")
    assert index.tier("Wiley Toy Hydrology", "journal")["tier"] is None  # under 3 content words: not tried


def test_rules_never_touch_a_match_or_a_conference(index):
    assert index.tier("Journal of Invented Hydrology", "journal")["method"] == "exact"
    assert index.tier("Toy Robotics", "conference")["tier"] is None
    index.journal_rules = False
    assert index.tier("Bull. Pretendol. Hydrodyn. Soc.", "journal")["tier"] is None
