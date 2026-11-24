"""The recruiter's requirement profile (dataclass and JSON).

    mode                  default, education, experience or publications (weights: config/shortlisting.json)
    field                 a role field of config/venue_fields.json (venue quartiles, NIRF categories)
    min_degree_level      bachelors, masters or doctorate (optional)
    experience_kind       academic or industry
    experience_years      2, 5 or 10
    institution_tier_cutoff  Tier 1, Tier 2 or Tier 3 (education mode only, optional)
    venue_tier_cutoff     A, B or C (publications mode only, optional)
    keywords              topic keywords; synonyms: keyword -> list of other words for it
    job_ad                optional job-ad text; its TF-IDF keywords are added to the keywords
With no keywords (none given and no job ad), topic fit is dropped.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from pathlib import Path

from resume_parser.schema import load_config
from shortlisting.experience import KINDS, TIERS

MODES = ("default", "education", "experience", "publications")
DEGREE_LEVELS = ("bachelors", "masters", "doctorate")
INSTITUTION_TIERS = ("Tier 1", "Tier 2", "Tier 3")
VENUE_TIERS = ("A", "B", "C")


@dataclass
class RequirementProfile:
    mode: str = "default"
    field: str | None = None
    min_degree_level: str | None = None
    experience_kind: str = "academic"
    experience_years: int = 5
    institution_tier_cutoff: str | None = None
    venue_tier_cutoff: str | None = None
    keywords: list[str] = dc_field(default_factory=list)
    synonyms: dict[str, list[str]] = dc_field(default_factory=dict)
    job_ad: str | None = None

    def __post_init__(self):
        fields = load_config("venue_fields.json")["fields"]
        checks = [(self.mode in MODES, f"mode must be one of {MODES}"),
                  (self.field is None or self.field in fields, "field must be a key of config/venue_fields.json"),
                  (self.min_degree_level in (None, *DEGREE_LEVELS), f"min_degree_level must be one of {DEGREE_LEVELS}"),
                  (self.experience_kind in KINDS, f"experience_kind must be one of {KINDS}"),
                  (self.experience_years in TIERS, f"experience_years must be one of {TIERS}"),
                  (self.institution_tier_cutoff in (None, *INSTITUTION_TIERS), f"institution_tier_cutoff: one of {INSTITUTION_TIERS}"),
                  (self.venue_tier_cutoff in (None, *VENUE_TIERS), f"venue_tier_cutoff must be one of {VENUE_TIERS}")]
        for ok, message in checks:
            if not ok:
                raise ValueError(message)
        self.keywords = [k.strip() for k in self.keywords if str(k).strip()]

    def all_keywords(self) -> list[str]:
        """The keywords, their synonyms and the job-ad keywords, de-duplicated (lower case)."""
        words = list(self.keywords)
        for k in self.keywords:
            words += self.synonyms.get(k, [])
        if self.job_ad:
            words += job_ad_keywords(self.job_ad)
        return list(dict.fromkeys(w.lower().strip() for w in words if w.strip()))

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> "RequirementProfile":
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown profile field(s): {', '.join(unknown)}")
        return cls(**data)

    @classmethod
    def from_json(cls, path: Path) -> "RequirementProfile":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def job_ad_keywords(text: str) -> list[str]:
    """The top TF-IDF terms (single words and two-word phrases) of a job ad, its sentences taken as
    the documents (config/shortlisting.json job_ad_keywords)."""
    from sklearn.feature_extraction.text import TfidfVectorizer

    p = load_config("shortlisting.json")["job_ad_keywords"]
    sentences = [s for s in re.split(r"(?<=[.!?;:])\s+|\n+", text or "") if re.search(r"[A-Za-z]{3}", s)]
    if not sentences:
        return []
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, p["ngram_max"]), token_pattern=r"(?u)\b[A-Za-z][A-Za-z\-]{2,}\b")
    try:
        matrix = vec.fit_transform(sentences)
    except ValueError:  # only stop words
        return []
    scores = matrix.sum(axis=0).A1
    terms = vec.get_feature_names_out()
    ranked = sorted(zip(terms, scores), key=lambda ts: (-ts[1], ts[0]))
    return [t for t, _ in ranked[: p["top_k"]]]
