"""The shortlisting scorer: eligibility, a weighted score over the usable components, a tier, an
explanation card and a rank-stability range. Rule-based; settings in config/shortlisting.json.

Components (src/shortlisting/features.py): education, experience, publications, topic fit.
Weights depend on the profile mode; the score is the weighted mean over the usable components
only, the weights rescaled to them. Overall certainty: the usable weight share and the
weight-weighted mean certainty (high 1.0, medium 0.6, low 0.3), labelled high / medium / low by
config overall_certainty.

Topic fit: BM25 of the profile keywords (with synonyms and job-ad keywords) against each
candidate's publication titles and venues, scaled by the best score in the pool (certainty
medium); a candidate with no publication text falls back to the whole resume text (certainty low).
No keywords: topic fit is dropped.

Eligibility (pass / fail / unknown): minimum degree level; institution tier cutoff (education
mode); venue tier cutoff (publications mode); experience tier (experience mode). An unlisted
institution or an unknown venue gives unknown, never fail.

Tier: Unreadable for a FAIL parse (listed apart, never rejected); Hold for any eligibility fail
(listed with the reason); Review when overall certainty is low, a key component is unusable, an
eligibility check is unknown, an editorial override or an unlisted institution was involved, or the
parse decision is NEEDS_REVIEW; Shortlist when the score reaches shortlist_threshold with
certainty at least medium; Hold otherwise.

Rank stability: the pool re-scored ``runs`` times (fixed seed) with simulated parser noise (each
entry dropped with drop_rate, each experience or publication type flipped with flip_rate); the
range of ranks per candidate.
"""

from __future__ import annotations

import copy
import datetime as dt
import random
from dataclasses import dataclass, field

from resume_parser.schema import load_config
from shortlisting import features as F
from shortlisting.bm25 import BM25, tokenize
from shortlisting.profile import RequirementProfile

COMPONENTS = ("education", "experience", "publications", "topic_fit")
TIER_ORDER = ("Shortlist", "Review", "Hold", "Unreadable")


@dataclass
class Candidate:
    resume_id: str
    resume: dict
    decision: str | None = None  # the parser's ACCEPT / NEEDS_REVIEW / FAIL (None for ground truth)
    flags: list = field(default_factory=list)  # parser flag codes, shown on the card
    text: str | None = None  # resume text, for the topic and experience-statement fallbacks


class _Context:
    def __init__(self, scorer, text):
        self.cfg, self.profile, self.today = scorer.cfg, scorer.profile, scorer.today
        self.institution_tier, self.venue_tier = scorer.institution_tier, scorer.venue_tier
        self.keyword_tokens, self.keyword_phrases = scorer.keyword_tokens, scorer.keyword_phrases
        self.text = text


class Shortlister:
    def __init__(self, profile: RequirementProfile, venue_index=None, institution_index=None,
                 today: dt.date | None = None, config: dict | None = None):
        self.profile = profile
        self.cfg = config if config is not None else load_config("shortlisting.json")
        self.today = today or dt.date.today()
        if venue_index is None:
            from shortlisting.tiers.venues import load_index
            venue_index = load_index()
        if institution_index is None:
            from shortlisting.tiers.institutions import load_index as load_institutions
            institution_index = load_institutions()
        self.venues, self.institutions = venue_index, institution_index
        self._vcache, self._icache = {}, {}
        words = profile.all_keywords()
        self.keyword_phrases = [tokenize(w) for w in words]
        self.keyword_tokens = list(dict.fromkeys(t for p in self.keyword_phrases for t in p))
        self.weights = self.cfg["weights"][profile.mode]

    # ---- cached lookups (the noise runs repeat them)

    def venue_tier(self, venue: str, entry_type: str) -> dict:
        key = (venue, entry_type)
        if key not in self._vcache:
            self._vcache[key] = self.venues.tier(venue, entry_type, self.profile.field)
        return self._vcache[key]

    def institution_tier(self, text: str) -> dict:
        if text not in self._icache:
            self._icache[text] = self.institutions.tier(text, self.profile.field)
        return self._icache[text]

    # ---- topic fit over the pool

    def _topic(self, views: list[dict], texts: list[str | None]) -> list[F.Component]:
        if not self.keyword_tokens:
            return [F.Component("topic_fit", None, None, reason="no_keywords") for _ in views]
        k = self.cfg["bm25"]
        pub_docs, pub_hits = [], []
        for v in views:
            toks, hits = [], []
            for i, pub in enumerate(v.get("publications") or []):
                t = tokenize(f"{pub.get('title') or ''} {pub.get('venue') or ''}")
                if set(t) & set(self.keyword_tokens):
                    hits.append(i)
                toks += t
            pub_docs.append(toks)
            pub_hits.append(hits)
        text_docs = [tokenize(t or "") for t in texts]
        pub_scores = BM25(pub_docs, k["k1"], k["b"]).scores(self.keyword_tokens)
        text_scores = BM25(text_docs, k["k1"], k["b"]).scores(self.keyword_tokens)
        pub_max, text_max = max(pub_scores, default=0) or 1.0, max(text_scores, default=0) or 1.0
        out = []
        for i in range(len(views)):
            if pub_docs[i]:
                matched = sorted(set(pub_docs[i]) & set(self.keyword_tokens))
                out.append(F.Component("topic_fit", pub_scores[i] / pub_max, "medium",
                                       {"source": "publications", "entries": pub_hits[i], "matched_terms": matched}))
            elif text_docs[i]:
                matched = sorted(set(text_docs[i]) & set(self.keyword_tokens))
                out.append(F.Component("topic_fit", text_scores[i] / text_max, "low",
                                       {"source": "whole_text", "matched_terms": matched}))
            else:
                out.append(F.Component("topic_fit", None, None, reason="no_text"))
        return out

    # ---- one candidate

    def _eligibility(self, view: dict, ctx, exp_calc: dict, venue_labels: list[str]) -> dict:
        p, cfg = self.profile, self.cfg
        out = {}
        if p.min_degree_level:
            best = F.best_degree_level(view, cfg["degree_levels"])
            if best is None:
                out["min_degree_level"] = {"result": "unknown", "reason": "no_degree_level"}
            else:
                ok = cfg["degree_levels"].index(best) >= cfg["degree_levels"].index(p.min_degree_level)
                out["min_degree_level"] = {"result": "pass" if ok else "fail", "reason": f"highest degree {best}"}
        if p.mode == "education" and p.institution_tier_cutoff:
            best, _unlisted = F.best_institution(view, ctx)
            order = cfg["institution_tier_order"]
            if best is None:
                out["institution_tier"] = {"result": "unknown", "reason": "unlisted_or_no_institution"}
            else:
                ok = order.index(best) <= order.index(p.institution_tier_cutoff)
                out["institution_tier"] = {"result": "pass" if ok else "fail", "reason": f"best {best}"}
        if p.mode == "publications" and p.venue_tier_cutoff:
            order = cfg["venue_tier_order"]
            known = [lb for lb in venue_labels if lb != "unknown"]
            if any(order.index(lb) <= order.index(p.venue_tier_cutoff) for lb in known):
                out["venue_tier"] = {"result": "pass", "reason": "a venue at or above the cutoff"}
            elif known and len(known) == len(venue_labels):
                out["venue_tier"] = {"result": "fail", "reason": "every venue below the cutoff"}
            else:
                out["venue_tier"] = {"result": "unknown", "reason": "unknown venues or no publications"}
        if p.mode == "experience":
            status = exp_calc["status"]
            result = {"meets": "pass", "likely_meets": "pass", "does_not_meet": "fail"}.get(status, "unknown")
            out["experience_tier"] = {"result": result, "reason": f"{status} ({exp_calc['reason']})"}
        return out

    def _combine(self, comps: dict) -> tuple[float | None, dict, dict, list]:
        cv = self.cfg["certainty_values"]
        usable = {n: c for n, c in comps.items() if c.usable and self.weights.get(n, 0) > 0}
        # a component the profile switched off (topic fit with no keywords) does not lower certainty
        total_w = sum(self.weights[n] for n, c in comps.items() if self.weights.get(n, 0) > 0 and c.reason != "no_keywords")
        uw = sum(self.weights[n] for n in usable)
        used = {n: self.weights[n] / uw for n in usable} if uw else {}
        score = sum(used[n] * usable[n].value for n in usable) if uw else None
        share = uw / total_w if total_w else 0.0
        mean = sum(used[n] * cv[usable[n].certainty] for n in usable) if uw else 0.0
        oc = self.cfg["overall_certainty"]
        level = ("high" if share >= oc["high"]["min_share"] and mean >= oc["high"]["min_mean"] else
                 "medium" if share >= oc["medium"]["min_share"] and mean >= oc["medium"]["min_mean"] else "low")
        dropped = [{"component": n, "reason": c.reason} for n, c in comps.items() if not c.usable]
        return score, {n: round(w, 4) for n, w in used.items()}, \
            {"usable_weight_share": round(share, 3), "mean_certainty": round(mean, 3), "level": level}, dropped

    def _tier(self, cand: Candidate, score, certainty, comps, elig) -> tuple[str, list[str]]:
        if cand.decision == "FAIL":
            return "Unreadable", ["the parse failed: listed apart, not rejected"]
        fails = [k for k, v in elig.items() if v["result"] == "fail"]
        if fails:
            return "Hold", [f"eligibility fail: {k}" for k in fails]
        review = []
        if certainty["level"] == "low":
            review.append("overall certainty low")
        key = self.cfg["key_components"][self.profile.mode]
        review += [f"key component unusable: {k}" for k in key if not comps[k].usable]
        review += [f"eligibility unknown: {k}" for k, v in elig.items() if v["result"] == "unknown"]
        for n, c in comps.items():
            review += [f"{n}: {note.replace('_', ' ')}" for note in c.notes if note in ("editorial_override", "unlisted_institution")]
        if cand.decision == "NEEDS_REVIEW":
            review.append("parse decision NEEDS_REVIEW")
        if review:
            return "Review", review
        if score is not None and score >= self.cfg["shortlist_threshold"] and certainty["level"] in ("medium", "high"):
            return "Shortlist", ["score at or above the threshold"]
        return "Hold", ["score below the threshold" if score is not None and score < self.cfg["shortlist_threshold"]
                        else "no usable score"]

    def score_pool(self, candidates: list[Candidate]) -> list[dict]:
        """One result (the explanation card, data only) per candidate, in input order."""
        views = [F.safe_view(c.resume) for c in candidates]
        topics = self._topic(views, [c.text for c in candidates])
        results = []
        for cand, view, topic in zip(candidates, views, topics):
            ctx = _Context(self, cand.text)
            exp_comp, exp_calc = F.experience(view, ctx)
            pub_comp, labels = F.publications(view, ctx)
            comps = {"education": F.education(view, ctx), "experience": exp_comp, "publications": pub_comp, "topic_fit": topic}
            elig = self._eligibility(view, ctx, exp_calc, labels)
            score, used, certainty, dropped = self._combine(comps)
            if cand.decision == "FAIL":
                score = None
            tier, why = self._tier(cand, score, certainty, comps, elig)
            results.append({
                "resume_id": cand.resume_id, "mode": self.profile.mode, "parse_decision": cand.decision,
                "parser_flags": list(cand.flags), "score": None if score is None else round(score, 4),
                "certainty": certainty, "weights_used": used, "dropped_components": dropped,
                "components": {n: c.as_dict() for n, c in comps.items()}, "eligibility": elig,
                "tier": tier, "tier_reasons": why})
        return results

    # ---- ranking and stability

    @staticmethod
    def _ranks(results: list[dict]) -> dict[str, int]:
        scored = sorted((r for r in results if r["score"] is not None), key=lambda r: (-r["score"], r["resume_id"]))
        return {r["resume_id"]: k for k, r in enumerate(scored, 1)}

    def _noisy(self, cand: Candidate, rng: random.Random) -> Candidate:
        s = self.cfg["stability"]
        resume = copy.deepcopy(F.safe_view(cand.resume))
        for section in F.ALLOWED_FIELDS:
            kept = []
            for e in resume.get(section) or []:
                if rng.random() < s["drop_rate"]:
                    continue
                if section == "experience" and rng.random() < s["flip_rate"]:
                    e["experience_type"] = {"academic": "industry", "industry": "academic"}.get(e.get("experience_type"), "academic")
                if section == "publications" and rng.random() < s["flip_rate"]:
                    e["type"] = {"journal": "conference", "conference": "journal"}.get(e.get("type"), "journal")
                kept.append(e)
            resume[section] = kept
        return Candidate(cand.resume_id, resume, cand.decision, cand.flags, cand.text)

    def rank(self, candidates: list[Candidate], stability: bool = True) -> list[dict]:
        """Score, rank (by score; Unreadable and unscored last) and, with stability, add the rank
        range over the noise runs. Sorted by tier, then rank."""
        results = self.score_pool(candidates)
        ranks = self._ranks(results)
        ranges = {rid: [k, k] for rid, k in ranks.items()}
        if stability:
            s = self.cfg["stability"]
            rng = random.Random(s["seed"])
            ordered = sorted(candidates, key=lambda c: c.resume_id)
            for _ in range(s["runs"]):
                noisy = self.score_pool([self._noisy(c, rng) for c in ordered])
                for rid, k in self._ranks(noisy).items():
                    if rid in ranges:
                        ranges[rid] = [min(ranges[rid][0], k), max(ranges[rid][1], k)]
        for r in results:
            r["rank"] = ranks.get(r["resume_id"])
            r["rank_range"] = ranges.get(r["resume_id"])
        return sorted(results, key=lambda r: (TIER_ORDER.index(r["tier"]), r["rank"] or 10 ** 9, r["resume_id"]))
