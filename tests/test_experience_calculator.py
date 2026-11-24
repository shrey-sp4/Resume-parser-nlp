"""The experience calculator (src/shortlisting/experience.py) on invented entries only."""

from __future__ import annotations

import datetime as dt

import pytest

from shortlisting.experience import compute_experience, merged_years, read_entry

TODAY = dt.date(2025, 1, 1)


def cv(*entries):
    return {"experience": [{"job_title": "Invented Role", "institution": "Invented Place", "dates": d, "experience_type": t}
                           for t, d in entries]}


def run(resume, kind="industry", tier=2, text=None):
    return compute_experience(resume, kind, tier, today=TODAY, text=text)


def test_overlapping_jobs_are_merged():
    r = run(cv(("industry", "Jan 2015 - Dec 2016"), ("industry", "Jun 2016 - Dec 2017")), tier=2)
    assert r["years"] == 3.0 and (r["status"], r["certainty"], r["source"]) == ("meets", "high", "entries")
    assert round(sum(e["years"] for e in r["entries"]), 2) == 3.58  # a plain sum counts the 7 shared months twice
    assert merged_years([]) == 0


def test_present_ends_today():
    r = run(cv(("industry", "Jan 2020 - Present")), tier=5)
    assert (r["years"], r["status"]) == (5.0, "meets")
    assert run(cv(("industry", "Jan 2020 - current")), tier=5)["years"] == 5.0
    later = compute_experience(cv(("industry", "Jan 2020 - Present")), "industry", 5, today=dt.date(2026, 1, 1))
    assert later["years"] == 6.0


def test_year_only_dates_give_likely_meets_or_unknown():
    r = run(cv(("academic", "2015 - 2020")), kind="academic", tier=5)  # widest reading: six years
    assert (r["status"], r["certainty"], r["years_fully_dated"], r["years_with_partial"]) == ("likely_meets", "medium", 0, 6.0)
    assert r["entries"][0]["date_quality"] == "year_only"
    r = run(cv(("academic", "2015 - 2020")), kind="academic", tier=10)  # below even at its widest
    assert (r["status"], r["certainty"]) == ("unknown", "low")
    r = run(cv(("academic", "2018")), kind="academic", tier=2)  # a start with no end
    assert (r["status"], r["entries"][0]["date_quality"]) == ("unknown", "start_only")
    # a fully dated job that reaches the tier is enough, whatever the other entries say
    r = run(cv(("industry", "Jan 2015 - Dec 2019"), ("industry", "2021")), tier=5)
    assert (r["status"], r["certainty"]) == ("meets", "medium")


def test_academic_and_industry_are_separate():
    resume = cv(("academic", "Jan 2010 - Dec 2015"), ("industry", "Jan 2016 - Dec 2016"), ("other", "Jan 2017 - Dec 2020"))
    a = run(resume, kind="academic", tier=5)
    i = run(resume, kind="industry", tier=5)
    assert (a["years"], a["status"]) == (6.0, "meets")
    assert (i["years"], i["status"]) == (1.0, "does_not_meet")
    assert a["entries_unknown_type"] == i["entries_unknown_type"] == 1
    assert [e["counted"] for e in a["entries"]] == [True, False, False]
    assert all(set(e) == {"index", "type", "counted", "years", "date_quality"} for e in a["entries"])  # never text


def test_text_statement_fallback():
    text = "Profile. Over 8 years of teaching experience in invented subjects."
    r = run(cv(("academic", "")), kind="academic", tier=5, text=text)
    assert (r["status"], r["certainty"], r["source"], r["years"]) == ("meets", "low", "text_statement", 8.0)
    r = run(cv(), kind="industry", tier=5, text=text)  # the type word is academic: not industry
    assert (r["status"], r["source"], r["statement"]["type"], r["years"]) == ("unknown", "text_statement", "academic", None)
    r = run(cv(), kind="industry", tier=5, text="I have 6 years of experience.")  # no type word
    assert (r["status"], r["statement"]["type"], r["years"]) == ("unknown", "unknown", None)
    r = run(cv(), kind="industry", tier=10, text="7 years of industry experience")
    assert (r["status"], r["certainty"]) == ("does_not_meet", "low")
    # a statement is only a fallback: dated entries win
    r = run(cv(("industry", "Jan 2023 - Dec 2023")), tier=10, text="12 years of industry experience")
    assert (r["source"], r["status"]) == ("entries", "does_not_meet")


def test_no_data_is_unknown_never_zero():
    for resume in (cv(), {"experience": None}, {}, cv(("industry", ""), ("industry", "not a date"))):
        r = run(resume, tier=2)
        assert (r["status"], r["certainty"], r["years"], r["source"]) == ("unknown", "low", None, "none")
    assert run(cv(), tier=2)["reason"] == "no_entries_of_type"  # reported, not read as zero
    assert run(cv(("industry", "")), tier=2)["reason"] == "no_usable_dates"
    # missing dates beside short dated jobs: not enough to say does_not_meet
    r = run(cv(("industry", "Jan 2020 - Dec 2020"), ("industry", "")), tier=5)
    assert (r["status"], r["years_fully_dated"], r["reason"]) == ("unknown", 1.0, "below_tier_with_missing_or_partial_dates")
    assert run(cv(("industry", "Jan 2015 - Dec 2020")), tier=5)["reason"] == "ok"


@pytest.mark.parametrize("tier, start", [(2, 2020), (5, 2015), (10, 2010)])
def test_each_tier_at_the_boundary(tier, start):
    end = start + tier - 1
    assert run(cv(("industry", f"Jan {start} - Dec {end}")), tier=tier)["status"] == "meets"          # exactly T years
    assert run(cv(("industry", f"Jan {start} - Jun {end}")), tier=tier)["status"] == "likely_meets"   # 0.5 below
    r = run(cv(("industry", f"Jan {start} - May {end}")), tier=tier)                                   # clearly below
    assert (r["status"], r["certainty"]) == ("does_not_meet", "high")


def test_entry_reading_and_arguments():
    assert read_entry("Dec 2020 - Jan 2019", TODAY)[0] == "missing"  # an end before its start
    assert read_entry("2019-20", TODAY)[0] == "year_only"
    with pytest.raises(ValueError):
        compute_experience(cv(), "both", 5)
    with pytest.raises(ValueError):
        compute_experience(cv(), "industry", 3)
