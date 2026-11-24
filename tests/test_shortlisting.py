"""The shortlisting scorer (src/shortlisting/) on invented candidates, with stand-in tier lookups."""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

import pytest

from resume_parser.schema import REPO_ROOT, load_config
from shortlisting import cli, features
from shortlisting.bm25 import BM25, tokenize
from shortlisting.profile import RequirementProfile, job_ad_keywords
from shortlisting.scorer import Candidate, Shortlister

TODAY = dt.date(2025, 6, 1)


class FakeVenues:
    TIERS = {"Journal of Alpha Things": "A", "Journal of Beta Things": "B", "Journal of Gamma Things": "C"}

    def tier(self, venue, entry_type, field_name=None):
        t = self.TIERS.get(venue) if entry_type in ("journal", "conference") else None
        return {"tier": t, "method": "exact" if t else None, "certainty": "high" if t else "none"}


class FakeInstitutions:
    TIERS = {"Alpha University": ("Tier 1", "nirf2025"), "Beta Institute": ("Tier 2", "nirf2025"),
             "Gamma College": ("Tier 3", "nirf2025"), "Override University": ("Tier 2", "editorial override")}

    def tier(self, text, field_name=None):
        t, src = self.TIERS.get(text, (None, None))
        return {"tier": t, "source": src, "certainty": "high" if t else "none", "best_category": "Overall" if t else None,
                "status": "listed" if t else "unlisted"}


def scorer(**kw) -> Shortlister:
    profile = RequirementProfile(**{"mode": "default", "experience_kind": "academic", "experience_years": 5, **kw})
    return Shortlister(profile, FakeVenues(), FakeInstitutions(), today=TODAY)


def edu(level="doctorate", inst="Alpha University", stream="Invented Studies"):
    return {"degree": "Degree", "degree_level": level, "stream": stream, "institution": inst}


def job(dates="Jan 2010 - Dec 2020", kind="academic"):
    return {"job_title": "Invented Role", "institution": "Somewhere", "dates": dates, "experience_type": kind}


def pub(venue="Journal of Alpha Things", year="2024", title="On invented matters", ty="journal"):
    return {"title": title, "type": ty, "year": year, "venue": venue}


def cand(rid, education=(), experience=(), publications=(), decision="ACCEPT", **extra):
    return Candidate(rid, {"education": list(education), "experience": list(experience), "publications": list(publications), **extra},
                     decision)


def by_id(results):
    return {r["resume_id"]: r for r in results}


def test_weights_are_rescaled_over_usable_components():
    s = scorer(keywords=["invented"])
    r = s.score_pool([cand("r_00000001", [edu()], [], [pub()])])[0]  # no experience: unusable, not zero
    assert r["components"]["experience"]["value"] is None
    assert {d["component"] for d in r["dropped_components"]} == {"experience"}
    assert r["weights_used"] == {"education": round(25 / 80, 4), "publications": round(25 / 80, 4), "topic_fit": round(30 / 80, 4)}
    comp = r["components"]
    expected = (25 * comp["education"]["value"] + 25 * comp["publications"]["value"] + 30 * comp["topic_fit"]["value"]) / 80
    assert r["score"] == pytest.approx(expected, abs=1e-4)
    assert r["certainty"]["usable_weight_share"] == 0.8


def test_an_unlisted_institution_is_unknown_never_fail_or_low():
    s = scorer(mode="education", institution_tier_cutoff="Tier 2")
    listed, unlisted = s.score_pool([cand("r_00000001", [edu(inst="Alpha University")], [job()], [pub()]),
                                     cand("r_00000002", [edu(inst="Unheard Of Institute")], [job()], [pub()])])
    assert unlisted["eligibility"]["institution_tier"]["result"] == "unknown"
    e = unlisted["components"]["education"]
    assert e["value"] == 1.0 and e["evidence"]["parts_used"] == ["degree_level"]  # the unlisted part left out, not low
    assert "unlisted_institution" in e["notes"] and unlisted["tier"] == "Review"
    assert listed["eligibility"]["institution_tier"]["result"] == "pass"
    low = s.score_pool([cand("r_00000003", [edu(inst="Gamma College")], [job()], [pub()])])[0]
    assert low["eligibility"]["institution_tier"]["result"] == "fail"  # listed below the cutoff: a real fail


def test_a_fail_is_listed_but_not_shortlisted():
    s = scorer(min_degree_level="doctorate")
    results = s.rank([cand("r_00000001", [edu("masters")], [job()], [pub()] * 6), cand("r_00000002", [edu()], [job()], [pub()] * 6)],
                     stability=False)
    r = by_id(results)
    assert r["r_00000001"]["tier"] == "Hold" and r["r_00000001"]["tier_reasons"] == ["eligibility fail: min_degree_level"]
    assert r["r_00000001"]["score"] is not None and r["r_00000001"]["rank"] is not None  # still listed and ranked
    assert r["r_00000002"]["tier"] == "Shortlist"


def test_unreadable_parses_are_listed_apart_and_review_rules():
    results = by_id(scorer().rank([cand("r_00000001", [edu()], [job()], [pub()] * 6, decision="FAIL"),
                                   cand("r_00000002", [edu()], [job()], [pub()] * 6, decision="NEEDS_REVIEW"),
                                   cand("r_00000003", [edu(inst="Override University")], [job()], [pub()] * 6)], stability=False))
    assert (results["r_00000001"]["tier"], results["r_00000001"]["score"], results["r_00000001"]["rank"]) == ("Unreadable", None, None)
    assert "parse_decision:NEEDS_REVIEW" in results["r_00000002"]["parser_flags"]
    assert "education: editorial override" in results["r_00000003"]["tier_reasons"]


@pytest.mark.parametrize("mode, winner", [("education", "r_edu"), ("experience", "r_exp"), ("publications", "r_pub"),
                                          ("default", "r_top")])
def test_each_mode_ranks_its_heavy_component_first(mode, winner):
    weak_edu, weak_job, weak_pub = edu("bachelors", "Gamma College", ""), job("Jan 2020 - Dec 2020"), pub("Journal of Gamma Things")
    pool = [cand("r_edu", [edu(stream="")], [weak_job], [weak_pub]),
            cand("r_exp", [weak_edu], [job()], [weak_pub]),
            cand("r_pub", [weak_edu], [weak_job], [pub()] * 8),
            cand("r_top", [weak_edu], [weak_job], [pub("Journal of Gamma Things", title="Quantum widgets at scale")])]
    results = scorer(mode=mode, keywords=["quantum widgets"]).rank(pool, stability=False)
    ranked = sorted(results, key=lambda r: r["rank"])
    assert ranked[0]["resume_id"] == winner, [(r["resume_id"], r["score"]) for r in ranked]


def test_publications_are_capped_and_weighted():
    s = scorer()
    many, some, old, unknown = s.score_pool([cand("r_1", publications=[pub()] * 50), cand("r_2", publications=[pub()] * 7),
                                             cand("r_3", publications=[pub(year="1995")] * 7),
                                             cand("r_4", publications=[pub("Journal of Nowhere")] * 6)])
    assert many["components"]["publications"]["value"] == some["components"]["publications"]["value"] == 1.0
    assert many["components"]["publications"]["evidence"]["capped"] is True
    assert old["components"]["publications"]["value"] < some["components"]["publications"]["value"]  # recency
    p = unknown["components"]["publications"]
    assert p["certainty"] == "low" and p["evidence"]["by_tier"] == {"unknown": 6}
    assert p["value"] == pytest.approx(min(1.0, 6 * 0.4 * 0.5 ** (1 / 6) / 6), abs=1e-3)
    none = s.score_pool([cand("r_5", [edu()], [job()], [])])[0]["components"]["publications"]
    assert (none["value"], none["certainty"], none["reason"]) == (0.0, "low", "no_publication_entries")


def test_no_keywords_drops_topic_fit_without_lowering_certainty():
    r = scorer().score_pool([cand("r_1", [edu()], [job()], [pub()] * 6)])[0]
    assert r["dropped_components"] == [{"component": "topic_fit", "reason": "no_keywords"}]
    assert "topic_fit" not in r["weights_used"] and r["certainty"]["usable_weight_share"] == 1.0


def test_topic_fit_uses_bm25_and_falls_back_to_text():
    s = scorer(keywords=["quantum widgets"], synonyms={"quantum widgets": ["qubit gadgets"]})
    a, b, c = s.score_pool([cand("r_1", publications=[pub(title="Qubit gadgets in practice")]),
                            cand("r_2", publications=[pub(title="Unrelated matters")]),
                            Candidate("r_3", {"publications": []}, "ACCEPT", text="I build quantum widgets.")])
    assert a["components"]["topic_fit"]["value"] == 1.0 and a["components"]["topic_fit"]["evidence"]["entries"] == [0]
    assert b["components"]["topic_fit"]["value"] == 0.0
    t = c["components"]["topic_fit"]
    assert (t["certainty"], t["evidence"]["source"]) == ("low", "whole_text") and t["value"] > 0
    bm = BM25([tokenize("quantum widgets"), tokenize("other words")])
    assert bm.score(["quantum"], 0) > bm.score(["quantum"], 1) == 0


def test_rank_stability_is_deterministic_with_a_fixed_seed():
    pool = [cand(f"r_{k}", [edu()], [job()], [pub()] * k) for k in range(1, 7)]
    first = {r["resume_id"]: r["rank_range"] for r in scorer().rank(pool)}
    second = {r["resume_id"]: r["rank_range"] for r in scorer().rank(pool)}
    assert first == second
    for r in scorer().rank(pool):
        lo, hi = r["rank_range"]
        assert lo <= r["rank"] <= hi


def test_no_feature_reads_protected_fields():
    class Recording(dict):
        seen: set = set()

        def __getitem__(self, k):
            Recording.seen.add(k)
            return super().__getitem__(k)

        def get(self, k, default=None):
            Recording.seen.add(k)
            return super().get(k, default)

    protected = {"candidate_name": "Invented Person", "gender": "x", "age": "40", "photo": "img", "nationality": "y",
                 "date_of_birth": "1980"}
    resume = Recording({"education": [edu()], "experience": [job()], "publications": [pub()], **protected})
    s = scorer(keywords=["invented"])
    r1 = s.score_pool([Candidate("r_1", resume, "ACCEPT")])[0]
    assert not (Recording.seen & set(features.FORBIDDEN_FIELDS))
    r2 = s.score_pool([cand("r_1", [edu()], [job()], [pub()])])[0]  # the same without them
    assert r1["score"] == r2["score"] and r1["components"] == r2["components"]
    for module in ("features.py", "scorer.py", "bm25.py", "experience.py"):
        source = (REPO_ROOT / "src" / "shortlisting" / module).read_text(encoding="utf-8")
        for name in features.FORBIDDEN_FIELDS:
            hits = source.count(f'"{name}"')
            assert hits == (1 if module == "features.py" else 0), (module, name)  # only the list itself names them


def test_explanation_card_holds_no_text():
    r = scorer(keywords=["invented"]).score_pool([cand("r_1", [edu()], [job()], [pub()])])[0]
    blob = json.dumps(r)
    for secret in ("Alpha University", "Journal of Alpha Things", "On invented matters", "Invented Role", "Somewhere"):
        assert secret not in blob
    assert r["components"]["education"]["evidence"]["institution"] == {"entry": 0, "tier": "Tier 1", "source": "nirf2025",
                                                                       "best_category": "Overall"}


def test_profile_validation_and_job_ad_keywords():
    with pytest.raises(ValueError):
        RequirementProfile(mode="everything")
    with pytest.raises(ValueError):
        RequirementProfile.from_dict({"mode": "default", "salary": 1})
    words = job_ad_keywords("We seek a researcher in quantum widgets. Quantum widgets research and widget design matter.")
    assert "quantum widgets" in words or "quantum" in words
    p = RequirementProfile(keywords=["Widgets"], synonyms={"Widgets": ["Gadgets"]})
    assert p.all_keywords() == ["widgets", "gadgets"]
    assert json.loads(p.to_json())["mode"] == "default"


def test_cli_writes_a_ranked_csv_outside_the_repo_only(tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    for stem, decision in (("alpha", "ACCEPT"), ("beta", "NEEDS_REVIEW")):
        (out_dir / f"{stem}.json").write_text(json.dumps({"candidate_name": "Invented Person", "education": [edu()],
                                                          "experience": [job()], "publications": [pub()] * 3}), encoding="utf-8")
        (out_dir / f"{stem}.meta.json").write_text(json.dumps({"decision": decision, "flags": [{"section": "x", "code": "c1", "detail": "d"}]}),
                                                   encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text(RequirementProfile(keywords=["invented"]).to_json(), encoding="utf-8")
    real = cli.Shortlister
    monkeypatch.setattr(cli, "Shortlister", lambda p, today=None: real(p, FakeVenues(), FakeInstitutions(), today=TODAY))
    target = tmp_path / "ranked.csv"
    assert cli.main(["--profile", str(profile), "--outputs", str(out_dir), "--out", str(target), "--no-stability"]) == 0
    rows = list(csv.DictReader(target.open(encoding="utf-8")))
    assert len(rows) == 2 and all(r["resume_id"].startswith("r_") for r in rows)
    text = target.read_text(encoding="utf-8") + capsys.readouterr().out
    assert "Invented Person" not in text and "alpha" not in text and "beta" not in text
    assert cli.main(["--profile", str(profile), "--outputs", str(out_dir), "--out", str(REPO_ROOT / "x.csv")]) == 2
    assert not (REPO_ROOT / "x.csv").exists()


def _needs_review_pool():
    return [cand("r_00000001", [edu()], [job()], [pub()] * 7, decision="NEEDS_REVIEW"),
            cand("r_00000002", [edu()], [job()], [pub()] * 7, decision="FAIL")]


def test_needs_review_parse_is_a_flag_only_by_default():
    assert load_config("shortlisting.json")["parse_decision_forces_review"] is False
    nr, fail = scorer().score_pool(_needs_review_pool())
    assert nr["tier"] == "Shortlist" and "parse decision NEEDS_REVIEW" not in nr["tier_reasons"]
    assert nr["parser_flags"] == ["parse_decision:NEEDS_REVIEW"] and nr["parse_decision"] == "NEEDS_REVIEW"
    assert fail["tier"] == "Unreadable"
    # the other Review triggers stay
    other = scorer().score_pool([cand("r_3", [edu(inst="Override University")], [job()], [pub()] * 7, decision="NEEDS_REVIEW")])[0]
    assert other["tier"] == "Review" and other["tier_reasons"] == ["education: editorial override"]


def test_needs_review_parse_forces_review_when_set():
    cfg = load_config("shortlisting.json")
    cfg["parse_decision_forces_review"] = True
    profile = RequirementProfile(mode="default", experience_kind="academic", experience_years=5)
    nr, fail = Shortlister(profile, FakeVenues(), FakeInstitutions(), today=TODAY, config=cfg).score_pool(_needs_review_pool())
    assert (nr["tier"], nr["tier_reasons"]) == ("Review", ["parse decision NEEDS_REVIEW"])
    assert "parse_decision:NEEDS_REVIEW" in nr["parser_flags"] and fail["tier"] == "Unreadable"