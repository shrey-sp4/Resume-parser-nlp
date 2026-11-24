"""Scoring features. Each returns a Component: value 0..1 and certainty (high / medium / low), or
value None when the data is unusable (never read as low). Features see only safe_view(resume):
the education, experience and publications entries. No feature reads a name, gender, age, date of
birth, photo or nationality field (hard rule; tests/test_shortlisting.py checks it).

Evidence holds entry indices, tier labels and counts, never text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from shortlisting.bm25 import tokenize
from shortlisting.experience import compute_experience

ALLOWED_FIELDS = ("education", "experience", "publications")
FORBIDDEN_FIELDS = ("candidate_name", "name", "gender", "sex", "age", "date_of_birth", "dob", "birth_date", "photo",
                    "picture", "image", "nationality", "citizenship", "marital_status", "religion", "caste")
CERTAINTY_ORDER = ("low", "medium", "high")
_YEAR = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")


def safe_view(resume: dict) -> dict:
    """Only the allowed fields, read by name: a forbidden field is never touched."""
    return {k: list(resume[k] or []) for k in ALLOWED_FIELDS if k in resume}


@dataclass
class Component:
    name: str
    value: float | None
    certainty: str | None
    evidence: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)  # e.g. editorial_override, unlisted_institution
    reason: str = "ok"

    @property
    def usable(self) -> bool:
        return self.value is not None

    def as_dict(self) -> dict:
        return {"value": None if self.value is None else round(self.value, 4), "certainty": self.certainty,
                "evidence": self.evidence, "notes": list(self.notes), "reason": self.reason}


def certainty_label(x: float, values: dict) -> str:
    """The label whose value is nearest below x (high 1.0, medium 0.6, low 0.3 by default)."""
    for label in ("high", "medium"):
        if x >= values[label] - 1e-9:
            return label
    return "low"


def mean_certainty(labels_weights: list[tuple[str, float]], values: dict) -> str:
    total = sum(w for _, w in labels_weights)
    return certainty_label(sum(values[c] * w for c, w in labels_weights) / total, values) if total else "low"


# ---------------------------------------------------------------- education


def education(view: dict, ctx) -> Component:
    cfg, profile = ctx.cfg, ctx.profile
    entries = view.get("education") or []
    if not entries:
        return Component("education", None, None, reason="no_education_entries")
    parts, evidence, notes = [], {}, []
    order = cfg["degree_levels"]
    levels = [(i, e.get("degree_level")) for i, e in enumerate(entries) if e.get("degree_level") in order]
    if levels:
        i, best = max(levels, key=lambda il: order.index(il[1]))
        if profile.min_degree_level:
            gap = order.index(profile.min_degree_level) - order.index(best)
            below = cfg["degree_below_minimum_values"]
            value = below[min(max(gap, 0), len(below) - 1)]
        else:
            value = cfg["degree_level_values"][best]
        parts.append(("degree_level", value, "high"))
        evidence["degree_level"] = {"entry": i, "level": best}
    tiers, unlisted = [], []
    for i, e in enumerate(entries):
        if not str(e.get("institution") or "").strip():
            continue
        r = ctx.institution_tier(e["institution"])
        if r["tier"]:
            tiers.append((i, r))
        else:
            unlisted.append(i)
    if tiers:
        tier_order = cfg["institution_tier_order"]
        i, r = min(tiers, key=lambda ir: tier_order.index(ir[1]["tier"]))
        parts.append(("institution_tier", cfg["institution_tier_values"][r["tier"]], r["certainty"] or "medium"))
        evidence["institution"] = {"entry": i, "tier": r["tier"], "source": r["source"], "best_category": r.get("best_category")}
        if r["source"] == "editorial override":
            notes.append("editorial_override")
    elif unlisted:
        notes.append("unlisted_institution")  # unusable, never low
    if unlisted:
        evidence["unlisted_entries"] = unlisted
    keywords = ctx.keyword_tokens
    streams = [(i, tokenize(e.get("stream") or "")) for i, e in enumerate(entries) if str(e.get("stream") or "").strip()]
    if keywords and streams:
        phrase_hit = [i for i, toks in streams if any(p and set(p) <= set(toks) for p in ctx.keyword_phrases)]
        word_hit = [i for i, toks in streams if set(toks) & set(keywords)]
        value = 1.0 if phrase_hit else 0.5 if word_hit else 0.0
        parts.append(("stream_fit", value, "medium"))
        evidence["stream_fit"] = {"entries": phrase_hit or word_hit, "match": "phrase" if phrase_hit else "word" if word_hit else "none"}
    if not parts:
        return Component("education", None, None, evidence, notes, reason="no_usable_education_data")
    w = cfg["education_parts"]
    total = sum(w[n] for n, _, _ in parts)
    value = sum(w[n] * v for n, v, _ in parts) / total
    evidence["parts_used"] = [n for n, _, _ in parts]
    return Component("education", value, mean_certainty([(c, w[n]) for n, _, c in parts], cfg["certainty_values"]),
                     evidence, notes)


def best_degree_level(view: dict, order: list[str]) -> str | None:
    levels = [e.get("degree_level") for e in view.get("education") or [] if e.get("degree_level") in order]
    return max(levels, key=order.index) if levels else None


def best_institution(view: dict, ctx) -> tuple[str | None, bool]:
    """(best institution tier, whether an institution was named but unlisted)."""
    tiers, unlisted = [], False
    for e in view.get("education") or []:
        if str(e.get("institution") or "").strip():
            r = ctx.institution_tier(e["institution"])
            if r["tier"]:
                tiers.append(r["tier"])
            else:
                unlisted = True
    order = ctx.cfg["institution_tier_order"]
    return (min(tiers, key=order.index) if tiers else None), unlisted


# ---------------------------------------------------------------- experience


def experience(view: dict, ctx) -> tuple[Component, dict]:
    p = ctx.profile
    calc = compute_experience(view, p.experience_kind, p.experience_years, today=ctx.today, text=ctx.text)
    value = ctx.cfg["experience_status_values"].get(calc["status"])
    evidence = {"status": calc["status"], "years": calc["years"], "source": calc["source"],
                "entries": [{"entry": e["index"], "type": e["type"], "date_quality": e["date_quality"]}
                            for e in calc["entries"] if e["counted"]]}
    comp = Component("experience", value, calc["certainty"] if value is not None else None, evidence,
                     reason=calc["reason"] if value is None else "ok")
    return comp, calc


# ---------------------------------------------------------------- publications


def publications(view: dict, ctx) -> tuple[Component, list[str]]:
    """The capped, recency- and venue-weighted publication value, and the tier label of each
    counted entry (for the venue cutoff)."""
    cfg = ctx.cfg
    pc = cfg["publications"]
    values = cfg["venue_tier_values"]
    counted, labels, evidence_entries = 0, [], []
    total = 0.0
    for i, pub in enumerate(view.get("publications") or []):
        ty = pub.get("type")
        if ty not in pc["types"]:
            continue
        counted += 1
        venue = str(pub.get("venue") or "").strip()
        tier = ctx.venue_tier(venue, ty)["tier"] if venue else None
        label = tier or "unknown"
        labels.append(label)
        m = _YEAR.search(str(pub.get("year") or ""))
        if m:
            age = max(0, ctx.today.year - int(m.group(0)))
            recency = 0.5 ** (age / pc["recency_half_life_years"])
        else:
            recency = pc["unknown_year_weight"]
        total += values[label] * recency
        evidence_entries.append({"entry": i, "tier": label, "year_known": bool(m)})
    if not counted:
        return Component("publications", 0.0, "low", {"counted": 0}, reason="no_publication_entries"), labels
    share = sum(1 for lb in labels if lb != "unknown") / counted
    certainty = "high" if share >= pc["certainty_high_share"] else "medium" if share >= pc["certainty_medium_share"] else "low"
    by_tier = {t: labels.count(t) for t in (*cfg["venue_tier_order"], "unknown") if labels.count(t)}
    evidence = {"counted": counted, "by_tier": by_tier, "capped": total > pc["cap"], "entries": evidence_entries}
    return Component("publications", min(1.0, total / pc["cap"]), certainty, evidence), labels
