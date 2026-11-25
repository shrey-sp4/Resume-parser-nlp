"""Service layer for the recruiter UI (app/streamlit_app.py): no streamlit here, so everything is
testable on its own.

    run_shortlist(profile, pdf_paths, work_dir=None, progress_callback=None)
        reads each PDF with the parser (outputs saved by file hash in work_dir/cache), scores the
        pool with the shortlisting scorer and returns a ShortlistRun: ranked results, by tier, and
        the path of each file (for the Open CV button).

Progress: progress_callback(event) gets one dict per step event, in the order of STEPS:
{"cv": k or None, "total": n, "file": name, "step": step, "state": "running" | "done"}.
- The first four steps are the parser's own stage boundaries, observed by wrapping its stage
  functions (resume_parser.pipeline.default_stages, passed to run_pipeline as its stage list): the
  parser is unchanged and its output identical. Stage 5 reads education, experience and
  publications in one call, so those three are one step. A CV read before gives one
  {"state": "saved"} instead; a CV that could not be read gives {"state": "could_not_read"} and the
  run continues.
- Then, per CV, our own steps: the venue and institution lookups (they fill the scorer's own
  lookup cache, with the same calls the features make) and the experience years (the same
  calculator the scorer uses); then, for the pool (cv None), scoring and sorting into tiers (the
  scorer's score_pool and its ranking, as Shortlister.rank without the stability runs).
StatusBoard turns the events into the status line, the progress bar and the pipeline panel state.

Everything shown to a recruiter is in plain words: no parser codes, entry numbers or internal
names. work_dir: RESUME_SHORTLIST_DIR, else RESUME_DATA_DIR/shortlist_runs; a folder inside the
repo is always refused. Uploads go only to work_dir/uploads and exports to work_dir/exports.
Nothing here reads or returns a name, gender, age, photo or nationality field (the scorer sees
only education, experience and publications).
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Callable

from resume_parser.schema import REPO_ROOT, DataDirError, data_root, load_config, resume_id_for
from shortlisting.experience import compute_experience
from shortlisting.features import safe_view
from shortlisting.profile import RequirementProfile
from shortlisting.scorer import TIER_ORDER, Candidate, Shortlister

ENV_WORK_DIR = "RESUME_SHORTLIST_DIR"
CACHE_VERSION = 1
TIERS = TIER_ORDER

MODE_LABELS = {"default": "Default", "education": "Education-heavy", "experience": "Experience-heavy",
               "publications": "Publications-heavy"}
DEGREE_LABELS = {"bachelors": "Bachelors", "masters": "Masters", "doctorate": "Doctorate"}
INSTITUTION_LABELS = {"Tier 1": "Top", "Tier 2": "Good", "Tier 3": "Fair"}
VENUE_LABELS = {"A": "Top", "B": "Good", "C": "Fair"}
NOT_LISTED = "Not listed"
AREAS = {"education": "Education", "experience": "Experience", "publications": "Publications",
         "topic_fit": "Topic match"}
WEIGHT_NAMES = {"education": "Education", "experience": "Experience", "publications": "Publications",
                "topic_fit": "Topic"}

NOTE = "Tiers are decision support. Please read the CV before deciding."
UNREADABLE_TEXT = "We could not read this CV automatically. Please open it and review it manually."
FOLDER_MISSING = "We could not find this folder. Please check the path."

# (step, one-line description); the first four are parser stages
READ_PDF, SECTIONS, ENTRIES, CHECKING = ("Reading the PDF", "Finding sections",
                                         "Reading education, experience and publications", "Checking the reading")
LOOKUPS, YEARS, SCORING, SORTING = ("Looking up journal, conference and institution rankings",
                                    "Working out experience years", "Scoring", "Sorting into tiers")
STEPS = (
    (READ_PDF, "Takes the text out of each page"),
    (SECTIONS, "Finds headings like Education and Experience"),
    (ENTRIES, "Finds education, experience and publications"),
    (CHECKING, "Checks the reading is complete"),
    (LOOKUPS, "Matches venues and institutions to ranking lists"),
    (YEARS, "Adds up years from the dates"),
    (SCORING, "Weighs the areas for your mode"),
    (SORTING, "Puts each CV into a tier"),
)
STEP_NAMES = tuple(s for s, _ in STEPS)
SHORT_NAMES = ("Read", "Sections", "Details", "Check", "Rankings", "Years", "Score", "Tiers")
PARSE_STEPS = ((READ_PDF, ("stage0", "stage1", "stage2")), (SECTIONS, ("stage3", "stage4")),
               (ENTRIES, ("stage5",)), (CHECKING, ("stage6", "stage7")))
INPUTS = (("Your CVs", "The PDF files you chose"), ("Your requirements", "Field, degree, mode and keywords"))
TIER_TEXT = {"Shortlist": "Strong match with enough found", "Review": "Worth a closer look",
             "Hold": "Not shortlisted for now", "Unreadable": "Please open and read by hand"}
SAVED = "Already read (saved)"
COULD_NOT_READ = "Could not read"

# a clear reason for an unreadable CV, from the parser's codes (first match wins)
UNREADABLE_REASONS = (
    (("needs_ocr", "no_text_layer", "ocr_unavailable"), "It looks like a scanned image without text."),
    (("open_failed", "no_pages", "unreadable"), "The file could not be opened as a PDF."),
    (("no_target_section",), "No education, experience or publications sections were found."),
)

MISSING_TEXT = {
    "no_education_entries": "No education found in the CV.",
    "no_usable_education_data": "Education found, but the degree level could not be read.",
    "no_experience_entries": "No experience found in the CV.",
    "no_usable_dates": "Experience found, but the dates could not be read.",
    "below_tier_with_missing_or_partial_dates": "Some experience dates are missing.",
    "no_publication_entries": "No publications found in the CV.",
    "no_text": "The topic match could not be checked.",
}

EXPERIENCE_STATUS = {"meets": "meets the required years", "likely_meets": "probably meets the required years",
                     "does_not_meet": "below the required years", "unknown": "years could not be worked out"}

HINTS = {
    "Rank": "Position by score among all CVs",
    "CV": "The file name of the CV",
    "Score": "How well the CV matches your requirements",
    "Based on": "How many of the four areas were found",
    "Reason": "Why the CV is in this tier",
}


class ServiceError(RuntimeError):
    """A problem with the folders or inputs. Messages never show resume content."""


# ---------------------------------------------------------------- folders and files


def _inside_repo(path: Path) -> bool:
    p = Path(path).resolve()
    return p == REPO_ROOT or REPO_ROOT in p.parents


def resolve_work_dir(work_dir: str | Path | None = None) -> Path:
    if work_dir is None:
        value = os.environ.get(ENV_WORK_DIR, "").strip()
        if value:
            work_dir = Path(value)
        else:
            try:
                work_dir = data_root() / "shortlist_runs"
            except DataDirError:
                raise ServiceError(f"Please set {ENV_WORK_DIR} or RESUME_DATA_DIR to a folder for the results.") from None
    path = Path(work_dir).resolve()
    if _inside_repo(path):
        raise ServiceError("The results folder must be outside the program folder.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_pdfs(folder: str | Path) -> list[Path]:
    """The .pdf files directly inside a folder, sorted by name."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"), key=lambda p: p.name)


def save_uploads(files: list[tuple[str, bytes]], work_dir: str | Path | None = None) -> list[Path]:
    """Copy uploaded (name, bytes) pairs into work_dir/uploads, only there; returns their paths."""
    folder = resolve_work_dir(work_dir) / "uploads"
    folder.mkdir(exist_ok=True)
    out = []
    for name, data in files:
        safe = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(str(name)).name).strip(" .") or "upload"
        target = folder / (safe if safe.lower().endswith(".pdf") else safe + ".pdf")
        target.write_bytes(data)
        out.append(target)
    return out


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- parsing (saved by hash)


def _record_text(record) -> str:
    stage1 = record.results.get("stage1")
    data = (stage1.data if stage1 is not None else None) or {}
    return "\n".join(str(x.get("text", "")) for page in sorted(data) for x in (data[page] or {}).get("lines") or [])


def run_pipeline_observed(pdf: Path, on_stage: Callable[[str, str], None]):
    """run_pipeline with each of the parser's own stage functions wrapped, so on_stage(stage,
    "start" | "end") fires at the parser's stage boundaries. Same functions, same order: the
    output is identical to run_pipeline(pdf)."""
    from resume_parser.pipeline import default_stages, run_pipeline

    def wrap(name, fn):
        def run(pdf_path, ctx):
            on_stage(name, "start")
            result = fn(pdf_path, ctx)
            on_stage(name, "end")
            return result
        return run

    return run_pipeline(pdf, stages=[(name, wrap(name, fn)) for name, fn in default_stages()])


def _cache_path(pdf: Path, cache_dir: Path) -> Path:
    return cache_dir / f"{file_hash(pdf)}.json"


def read_saved(pdf: Path, cache_dir: Path) -> dict | None:
    path = _cache_path(pdf, cache_dir)
    if path.is_file():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("cache_version") == CACHE_VERSION:
            return cached
    return None


def parse_cached(pdf: Path, cache_dir: Path, on_stage: Callable[[str, str], None] | None = None
                 ) -> tuple[dict | None, dict | None, str]:
    """(main JSON, sidecar, line text) for one PDF, saved or by running the parser. A parser crash
    gives (None, None, "") and the CV is listed as unreadable."""
    cached = read_saved(pdf, cache_dir)
    if cached is not None:
        return cached["main"], cached["sidecar"], cached["text"]
    from resume_parser import pipeline

    try:
        record = run_pipeline_observed(pdf, on_stage) if on_stage else pipeline.run_pipeline(pdf)
    except Exception:  # never shown: the CV is listed as unreadable
        return None, None, ""
    entry = {"cache_version": CACHE_VERSION, "main": record.main, "sidecar": record.sidecar, "text": _record_text(record)}
    cache_dir.mkdir(parents=True, exist_ok=True)
    _cache_path(pdf, cache_dir).write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    return entry["main"], entry["sidecar"], entry["text"]


def clear_saved_results(work_dir: str | Path | None = None) -> int:
    """Delete the saved parser results (work_dir/cache/<sha256>.json only); returns how many."""
    cache = resolve_work_dir(work_dir) / "cache"
    removed = 0
    if cache.is_dir():
        for p in cache.iterdir():
            if p.is_file() and re.fullmatch(r"[0-9a-f]{64}\.json", p.name):
                p.unlink()
                removed += 1
    return removed


@lru_cache(maxsize=1)
def load_indexes():
    """The venue and institution lookups (from the lexicon folder), loaded once."""
    from shortlisting.tiers.institutions import load_index as load_institutions
    from shortlisting.tiers.venues import load_index as load_venues

    return load_venues(), load_institutions()


# ---------------------------------------------------------------- progress


class StatusBoard:
    """The status line, the progress bar and the pipeline panel state, from the progress events.
    Panel state: active (index into STEPS or None), done (set of indexes), started, finished."""

    def __init__(self, total: int):
        self.total = total
        self.active: int | None = None
        self.done: set[int] = set()
        self.started = self.finished = False
        self.status = ""
        self.units = 0  # finished step events, for the bar

    def update(self, e: dict) -> None:
        self.started = True
        where = f"CV {e['cv']} of {self.total}" if e["cv"] is not None else f"{self.total} CV{'s' if self.total != 1 else ''}"
        state = e["state"]
        if state == "running":
            i = STEP_NAMES.index(e["step"])
            self.active, self.done = i, set(range(i))
            self.status = f"{e['step']} · {where}"
        elif state == "done":
            i = STEP_NAMES.index(e["step"])
            self.done.add(i)
            self.active = None
            self.units += 1
            if e["step"] == SORTING:
                self.finished, self.done = True, set(range(len(STEPS)))
                self.status = f"Done · {where}"
        elif state in ("saved", "could_not_read"):
            self.active, self.done = None, set(range(len(PARSE_STEPS)))
            self.units += len(PARSE_STEPS)
            self.status = f"{SAVED if state == 'saved' else COULD_NOT_READ} · {where}"

    def bar(self) -> float:
        total = self.total * (len(PARSE_STEPS) + 2) + 2
        return min(1.0, self.units / total) if total else 1.0

    def box_states(self) -> list[str]:
        """One of neutral, active, done, later per step."""
        if not self.started:
            return ["neutral"] * len(STEPS)
        return ["done" if i in self.done else "active" if i == self.active else "later" for i in range(len(STEPS))]


# ---------------------------------------------------------------- the run


@dataclass
class ShortlistRun:
    results: list[dict]  # ranked, each with file_name
    work_dir: Path
    by_tier: dict = field(default_factory=dict)
    paths: dict = field(default_factory=dict)  # resume_id -> PDF path (for Open CV); never exported
    mode: str = "default"

    @property
    def unreadable(self) -> list[dict]:
        return self.by_tier.get("Unreadable", [])


def _ids(pdfs: list[Path]) -> list[str]:
    ids, seen = [], set()
    for p in pdfs:
        rid = resume_id_for(p.name)
        if rid in seen:  # two files with the same name: the content hash tells them apart
            rid = resume_id_for(f"{p.stem}_{file_hash(p)[:8]}")
        seen.add(rid)
        ids.append(rid)
    return ids


def _unreadable_reason(sidecar: dict | None) -> str:
    if not sidecar:
        return ""
    codes = {f.get("code", "") for f in sidecar.get("flags") or [] if isinstance(f, dict)}
    codes |= set(sidecar.get("decision_reasons") or [])
    codes |= {c.split(":", 1)[0] for c in codes}
    for keys, text in UNREADABLE_REASONS:
        if codes & set(keys):
            return text
    return ""


def _degrees(resume: dict) -> list[str]:
    levels = [e.get("degree_level") for e in safe_view(resume).get("education") or []]
    order = list(DEGREE_LABELS)
    return [DEGREE_LABELS[lv] for lv in sorted({lv for lv in levels if lv in DEGREE_LABELS}, key=order.index, reverse=True)]


def _look_up_rankings(s: Shortlister, view: dict) -> None:
    """The venue and institution lookups for one CV, made exactly as features.education and
    features.publications make them, so they land in the scorer's own lookup cache."""
    for e in view.get("education") or []:
        if str(e.get("institution") or "").strip():
            s.institution_tier(e["institution"])
    for pub in view.get("publications") or []:
        ty = pub.get("type")
        venue = str(pub.get("venue") or "").strip()
        if ty in s.cfg["publications"]["types"] and venue:
            s.venue_tier(venue, ty)


def _rank(s: Shortlister, results: list[dict]) -> list[dict]:
    """Shortlister.rank without the stability runs: rank by score, then sort by tier."""
    ranks = s._ranks(results)
    for r in results:
        r["rank"] = ranks.get(r["resume_id"])
        r["rank_range"] = [r["rank"], r["rank"]] if r["rank"] is not None else None
    return sorted(results, key=lambda r: (TIER_ORDER.index(r["tier"]), r["rank"] or 10 ** 9, r["resume_id"]))


def run_shortlist(profile: RequirementProfile, pdf_paths: list[str | Path], work_dir: str | Path | None = None,
                  progress_callback: Callable[[dict], None] | None = None, today: dt.date | None = None
                  ) -> ShortlistRun:
    """Read (saved by hash), score and rank, with progress events (see the module docstring)."""
    work = resolve_work_dir(work_dir)
    cache = work / "cache"
    pdfs = [Path(p) for p in pdf_paths]
    ids = _ids(pdfs)
    n = len(pdfs)
    emit = progress_callback or (lambda e: None)
    candidates, names, extra = [], {}, {}
    step_of = {stage: (label, stages) for label, stages in PARSE_STEPS for stage in stages}
    for k, (pdf, rid) in enumerate(zip(pdfs, ids), 1):
        base = {"cv": k, "total": n, "file": pdf.name}
        saved = read_saved(pdf, cache) is not None
        if saved:
            emit({**base, "step": SAVED, "state": "saved"})
            main, sidecar, text = parse_cached(pdf, cache)
        else:
            def on_stage(stage, when, base=base):
                label, stages = step_of[stage]
                if when == "start" and stage == stages[0]:
                    emit({**base, "step": label, "state": "running"})
                elif when == "end" and stage == stages[-1]:
                    emit({**base, "step": label, "state": "done"})

            main, sidecar, text = parse_cached(pdf, cache, on_stage)
        if sidecar is None:
            decision, flags = "FAIL", ["parser_error"]
        else:
            decision = sidecar.get("decision")
            flags = sorted({f.get("code", "") for f in sidecar.get("flags") or [] if isinstance(f, dict)})
        if decision == "FAIL":
            emit({**base, "step": COULD_NOT_READ, "state": "could_not_read"})
        candidates.append(Candidate(rid, main or {}, decision, flags, text))
        names[rid] = pdf.name
        extra[rid] = {"unreadable_reason": _unreadable_reason(sidecar) if decision == "FAIL" else "",
                      "degrees": _degrees(main or {})}
    venues, institutions = load_indexes()
    s = Shortlister(profile, venues, institutions, today=today)
    for step, work_fn in ((LOOKUPS, lambda c: _look_up_rankings(s, safe_view(c.resume))),
                          (YEARS, lambda c: compute_experience(safe_view(c.resume), profile.experience_kind,
                                                               profile.experience_years, today=s.today, text=c.text))):
        for k, (pdf, cand) in enumerate(zip(pdfs, candidates), 1):
            base = {"cv": k, "total": n, "file": pdf.name, "step": step}
            emit({**base, "state": "running"})
            work_fn(cand)
            emit({**base, "state": "done"})
    pool = {"cv": None, "total": n, "file": None}
    emit({**pool, "step": SCORING, "state": "running"})
    scored = s.score_pool(candidates)
    emit({**pool, "step": SCORING, "state": "done"})
    emit({**pool, "step": SORTING, "state": "running"})
    results = _rank(s, scored)
    for r in results:
        r["file_name"] = names[r["resume_id"]]
        r.update(extra[r["resume_id"]])
    by_tier = {t: [r for r in results if r["tier"] == t] for t in TIERS}
    emit({**pool, "step": SORTING, "state": "done"})
    return ShortlistRun(results, work, by_tier, {rid: pdf for rid, pdf in zip(ids, pdfs)}, profile.mode)


# ---------------------------------------------------------------- requirements


def field_options() -> list[tuple[str | None, str]]:
    fields = load_config("venue_fields.json")["fields"]
    return [(None, "Any field")] + [(k, v.get("label", k)) for k, v in fields.items()]


def parse_keywords(text: str) -> list[str]:
    return [w.strip() for w in re.split(r"[,\n;]", text or "") if w.strip()]


def parse_synonyms(text: str) -> dict[str, list[str]]:
    """Lines like 'machine learning: ML, statistical learning'."""
    out = {}
    for line in (text or "").splitlines():
        if ":" in line:
            key, rest = line.split(":", 1)
            words = parse_keywords(rest)
            if key.strip() and words:
                out[key.strip()] = words
    return out


def build_profile(form: dict) -> RequirementProfile:
    """A profile from the UI form: mode, field, min_degree_level, experience_kind,
    experience_years, institution_tier_cutoff, venue_tier_cutoff, keywords and synonyms (text).
    Cutoffs only apply in their own mode."""
    mode = form.get("mode", "default")
    synonyms = parse_synonyms(form.get("synonyms", ""))
    keywords = parse_keywords(form.get("keywords", ""))
    keywords += [k for k in synonyms if k not in keywords]
    return RequirementProfile(
        mode=mode, field=form.get("field") or None, min_degree_level=form.get("min_degree_level") or None,
        experience_kind=form.get("experience_kind") or "academic", experience_years=int(form.get("experience_years") or 5),
        institution_tier_cutoff=(form.get("institution_tier_cutoff") or None) if mode == "education" else None,
        venue_tier_cutoff=(form.get("venue_tier_cutoff") or None) if mode == "publications" else None,
        keywords=keywords, synonyms=synonyms)


def weights_text(mode: str) -> str:
    """The mode's weights from config, e.g. 'Education 25 · Experience 20 · Publications 25 · Topic 30'."""
    w = load_config("shortlisting.json")["weights"][mode]
    return " · ".join(f"{WEIGHT_NAMES[k]} {w[k]:g}" for k in WEIGHT_NAMES)


# ---------------------------------------------------------------- presentation (plain words only)


def percent(score: float | None) -> str:
    return "" if score is None else f"{round(score * 100)}%"


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


COUNTED = ("Found", "None found")  # areas the score is based on
EMPTY_BUT_COUNTED = {"no_publication_entries"}  # counted in the score (as 0) though the CV has none


def areas(result: dict) -> list[tuple[str, str]]:
    """(area, Found / None found / Not found / Not used) for the four areas: Found unless the scorer
    dropped it, None found when it is counted but the CV has none (no publications), Not found
    when dropped, Not used for a topic match without keywords."""
    dropped = {d["component"]: d["reason"] for d in result.get("dropped_components") or []}
    comps = result.get("components") or {}
    out = []
    for key, label in AREAS.items():
        if key not in dropped:
            empty = (comps.get(key) or {}).get("reason") in EMPTY_BUT_COUNTED
            out.append((label, "None found" if empty else "Found"))
        else:
            out.append((label, "Not used" if dropped[key] == "no_keywords" else "Not found"))
    return out


def based_on(result: dict) -> str:
    if result["tier"] == "Unreadable":
        return ""
    return f"{sum(1 for _, s in areas(result) if s in COUNTED)} of {len(AREAS)}"


def _institution_label(tier: str | None) -> str:
    return INSTITUTION_LABELS.get(tier or "", NOT_LISTED)


def _check_text(key: str, v: dict) -> tuple[str, str]:
    """(requirement, plain explanation) for one eligibility check."""
    res = v["result"]
    if key == "min_degree_level":
        best = v["reason"].removeprefix("highest degree ").strip()
        why = f"Highest degree: {DEGREE_LABELS.get(best, best)}" if res != "unknown" else "The degree level could not be confirmed"
        return "Minimum degree", why
    if key == "institution_tier":
        best = v["reason"].removeprefix("best ").strip()
        why = f"Best institution: {_institution_label(best)}" if res != "unknown" else "The institution is not on the ranking list"
        return "Institution rank", why
    if key == "venue_tier":
        why = {"pass": "At least one paper in a venue at this level", "fail": "Every paper is in a lower venue"}.get(
            res, "The venue levels could not be confirmed")
        return "Venue level", why
    status = v["reason"].split(" ", 1)[0]
    return "Years of experience", EXPERIENCE_STATUS.get(status, "years could not be worked out").capitalize()


RESULT_TEXT = {"pass": "Met", "fail": "Not met", "unknown": "Could not check"}


def _thin_evidence(result: dict) -> str:
    """The plain sentence for a CV held back by thin evidence or missing areas."""
    score = result.get("score")
    missing = [a for a, s in areas(result) if s == "Not found"]
    if score is None:
        lead = "Not enough was found to score"
        return f"{lead}: {_join(missing)} {'was' if len(missing) == 1 else 'were'} not found. Please check the CV." \
            if missing else f"{lead}. Please check the CV."
    level = "High" if score >= load_config("shortlisting.json")["shortlist_threshold"] else "Medium" if score >= 0.4 else "Low"
    if missing:
        return f"{level} match, but {_join(missing)} {'was' if len(missing) == 1 else 'were'} not found. Please check the CV."
    return f"{level} match, but little could be read clearly. Please check the CV."


def reason_text(result: dict) -> str:
    """Why the CV is in its tier, in plain words (never 'rejected' or 'failed')."""
    if result["tier"] == "Unreadable":
        return " ".join(x for x in (UNREADABLE_TEXT, result.get("unreadable_reason", "")) if x)
    out = []
    reasons = result.get("tier_reasons") or []
    if any(r == "overall certainty low" or r.startswith("key component unusable") for r in reasons):
        out.append(_thin_evidence(result))
    for r in reasons:
        kind, _, key = r.partition(": ")
        if r == "overall certainty low" or kind == "key component unusable":
            continue
        if kind == "eligibility fail":
            out.append({"min_degree_level": "Below the minimum degree",
                        "institution_tier": "Institution below the chosen rank",
                        "venue_tier": "No paper in a venue at the chosen level",
                        "experience_tier": "Fewer years of experience than required"}.get(key, "A requirement is not met"))
        elif kind == "eligibility unknown":
            out.append(_check_text(key, {"result": "unknown", "reason": ""})[1])
        elif r == "education: editorial override":
            out.append("Institution rank set by the university's list")
        elif r == "education: unlisted institution":
            out.append("The institution is not on the ranking list")
        elif r == "parse decision NEEDS_REVIEW":
            out.append("Parts of the CV were hard to read")
        elif r == "score at or above the threshold":
            out.append("Score above the shortlist mark")
        elif r == "score below the threshold":
            out.append("Score below the shortlist mark")
        elif r == "no usable score":
            out.append("Not enough information to score")
        else:
            out.append("Needs a closer look")
    return "; ".join(dict.fromkeys(out))


def table_rows(results: list[dict], label: str = "file_name") -> list[dict]:
    """Rows for a results table: Rank, CV (file name or resume ID), Score (a percentage), Based on,
    Reason. No name, gender, age, photo or nationality field ever appears."""
    return [{"Rank": r["rank"] if r["rank"] is not None else "",
             "CV": r["file_name"] if label == "file_name" else r["resume_id"],
             "Score": percent(r["score"]), "Based on": based_on(r), "Reason": reason_text(r)} for r in results]


def sort_and_filter(results: list[dict], sort_by: str = "score", min_score: float = 0.0) -> list[dict]:
    """sort_by 'score' (high first) or 'file' (A to Z); min_score in 0-1. CVs without a score are
    kept only when min_score is 0."""
    kept = [r for r in results if (r["score"] if r["score"] is not None else -1) >= min_score or
            (min_score <= 0 and r["score"] is None)]
    if sort_by == "file":
        return sorted(kept, key=lambda r: (r["file_name"].lower(), r["resume_id"]))
    return sorted(kept, key=lambda r: (-(r["score"] if r["score"] is not None else -1), r["file_name"].lower()))


def summary_line(run: ShortlistRun) -> str:
    n = len(run.results)
    return f"{n} CV{'s' if n != 1 else ''} read: " + ", ".join(f"{len(run.by_tier.get(t, []))} {t}" for t in TIERS)


def card(result: dict) -> dict:
    """What was found and what is missing, in plain words, for one CV: areas (Found / Not found),
    based_on, components (name, value, found lines), missing, requirements, reason."""
    comps, missing = [], []
    status = dict(areas(result))
    for name, c in result["components"].items():
        ev = c["evidence"] or {}
        found = []
        if name == "education":
            if result.get("degrees"):
                found.append("Degrees: " + ", ".join(result["degrees"]))
            if "institution" in ev:
                found.append(f"Best institution: {_institution_label(ev['institution']['tier'])}")
            elif ev.get("unlisted_entries"):
                found.append(f"Institution: {NOT_LISTED}")
        elif name == "experience" and "status" in ev:
            if ev.get("years") is not None:
                found.append(f"About {round(float(ev['years']), 1):g} years")
            found.append(EXPERIENCE_STATUS.get(ev["status"], "").capitalize())
        elif name == "publications" and ev.get("counted"):
            found.append(f"{ev['counted']} paper{'s' if ev['counted'] != 1 else ''}")
            found.append("Venues: " + ", ".join(f"{VENUE_LABELS.get(t, NOT_LISTED)} {k}" for t, k in (ev.get("by_tier") or {}).items()))
        if (c["value"] is None or c["reason"] == "no_publication_entries") and MISSING_TEXT.get(c["reason"]):
            missing.append(MISSING_TEXT[c["reason"]])
        comps.append({"name": AREAS.get(name, name), "status": status[AREAS[name]], "value": c["value"],
                      "score": percent(c["value"]), "found": [f for f in found if f]})
    reqs = []
    for k, v in result["eligibility"].items():
        req, why = _check_text(k, v)
        reqs.append({"Requirement": req, "Result": RESULT_TEXT[v["result"]], "Details": why})
    return {"areas": areas(result), "based_on": based_on(result), "components": comps,
            "missing": list(dict.fromkeys(missing)), "requirements": reqs, "reason": reason_text(result)}


EXPORT_COLUMNS = ["Tier", "Rank", "CV", "Score", "Based on", "Areas found", "Reason"]


def export_rows(run: ShortlistRun, label: str = "resume_id") -> list[dict]:
    rows = []
    for r in run.results:
        row = {"Tier": r["tier"]}
        row.update(table_rows([r], "file_name" if label == "file_name" else "resume_id")[0])
        row["Areas found"] = "" if r["tier"] == "Unreadable" else ", ".join(a for a, s in areas(r) if s in COUNTED)
        rows.append({k: row[k] for k in EXPORT_COLUMNS})
    return rows


def export_csv_text(run: ShortlistRun, label: str = "resume_id") -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(export_rows(run, label))
    return buf.getvalue()


def export_csv(run: ShortlistRun, label: str = "resume_id") -> Path:
    """The ranked table as CSV in work_dir/exports (never in the repo); label resume_id or file_name."""
    folder = resolve_work_dir(run.work_dir) / "exports"
    folder.mkdir(exist_ok=True)
    path = folder / f"shortlist_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    path.write_text(export_csv_text(run, label), encoding="utf-8", newline="")
    return path


# ---------------------------------------------------------------- HTML (fonts, panel, tables, chips)

HEADING_FONT = "Garamond, 'EB Garamond', Georgia, serif"
TEXT_FONT = "'Times New Roman', Times, serif"

CSS = f"""<style>
.stApp, .stApp p, .stApp li, .stApp label, .stApp input, .stApp textarea, .stApp button, .stApp td, .stApp th,
.stApp span:not([data-testid="stIconMaterial"]), .stApp div, .stApp a, .stApp summary,
.stApp [data-baseweb="tab"] p, .stApp [data-testid="stMarkdownContainer"] p,
div[data-baseweb="popover"] li, div[data-baseweb="popover"] div, div[data-baseweb="tooltip"] div,
[role="listbox"], [role="listbox"] *, [role="option"], [role="option"] *,
[data-testid="stTooltipContent"] p {{ font-family: {TEXT_FONT} !important; }}
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h1 span, .stApp h2 span, .stApp h3 span,
.stApp h4 span {{ font-family: {HEADING_FONT} !important; }}
.rp-panel {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: stretch; margin: 4px 0 8px 0; }}
.rp-box {{ border: 1px solid #b8b8b8; border-radius: 3px; padding: 6px 8px; min-width: 120px; max-width: 170px;
  background: #ffffff; color: #222222; font-size: 0.85rem; line-height: 1.25; }}
.rp-box .rp-name {{ font-weight: bold; }}
.rp-box .rp-desc {{ font-size: 0.75rem; color: #555555; margin-top: 2px; }}
.rp-box.rp-input, .rp-box.rp-tier {{ background: #fafafa; }}
.rp-box.rp-active {{ border-color: #2f5d9b; background: #e9f0fa; animation: rp-pulse 1.4s ease-in-out infinite; }}
.rp-box.rp-done {{ border-color: #5f8a66; }}
.rp-box.rp-later {{ color: #9a9a9a; background: #f3f3f3; border-color: #d6d6d6; }}
.rp-box.rp-later .rp-desc {{ color: #a8a8a8; }}
.rp-arrow {{ align-self: center; color: #888888; }}
@keyframes rp-pulse {{ 0%, 100% {{ box-shadow: 0 0 0 0 rgba(47, 93, 155, 0.35); }}
  50% {{ box-shadow: 0 0 0 4px rgba(47, 93, 155, 0); }} }}
.rp-table {{ border-collapse: collapse; width: 100%; font-size: 0.92rem; }}
.rp-table th, .rp-table td {{ border-bottom: 1px solid #dddddd; text-align: left; padding: 4px 8px; vertical-align: top; }}
.rp-table th {{ border-bottom: 1px solid #999999; }}
.rp-hint {{ display: inline-block; border: 1px solid #999999; border-radius: 50%; width: 1.1em; height: 1.1em;
  line-height: 1.05em; text-align: center; font-size: 0.7rem; color: #555555; cursor: help; margin-left: 3px; }}
.rp-chip {{ display: inline-block; border: 1px solid #b8b8b8; border-radius: 3px; padding: 1px 6px; margin: 0 4px 2px 0;
  font-size: 0.82rem; }}
.rp-chip.rp-found {{ border-color: #5f8a66; }}
.rp-chip.rp-missing {{ color: #8a5a2b; border-color: #c9a27a; }}
.rp-chip.rp-none {{ color: #8a5a2b; border-color: #b8b8b8; }}
.rp-chip.rp-unused {{ color: #888888; }}
</style>"""


def _box(name: str, desc: str, kind: str, state: str, extra: str = "", index: int | None = None) -> str:
    tick = "✓ " if state == "done" else ""
    data = f' data-step="{index}"' if index is not None else ""
    lines = f'<div class="rp-name">{tick}{escape(name)}</div><div class="rp-desc">{escape(desc)}</div>'
    if extra:
        lines += f'<div class="rp-desc">{escape(extra)}</div>'
    return f'<div class="rp-box rp-{kind} rp-{state}" data-state="{state}"{data}>{lines}</div>'


def panel_html(board: StatusBoard | None, mode: str, counts: dict | None = None) -> str:
    """The pipeline panel: inputs, the eight steps, the four tiers. Box states come from the board
    (all neutral before a run)."""
    states = board.box_states() if board is not None else ["neutral"] * len(STEPS)
    in_state = "done" if board is not None and board.started else "neutral"
    tier_state = "done" if board is not None and board.finished else ("later" if board is not None and board.started else "neutral")
    parts = [_box(n, d, "input", in_state) for n, d in INPUTS]
    for i, ((name, desc), state) in enumerate(zip(STEPS, states)):
        parts.append(_box(name, desc, "step", state, weights_text(mode) if name == SCORING else "", i))
    for t in TIERS:
        label = f"{t} · {counts.get(t, 0)}" if counts is not None else t
        parts.append(_box(label, TIER_TEXT[t], "tier", tier_state))
    return '<div class="rp-panel">' + '<span class="rp-arrow">→</span>'.join(parts) + "</div>"


def strip_text(board: StatusBoard) -> str:
    """The one-line strip shown once the results are in."""
    states = board.box_states()
    return "  ·  ".join(("✓ " if s == "done" else "") + n for n, s in zip(SHORT_NAMES, states))


def table_html(rows: list[dict], columns: list[str]) -> str:
    """A results table with a ? hint on each header."""
    head = "".join(f'<th>{escape(c)}<span class="rp-hint" title="{escape(HINTS.get(c, ""))}">?</span></th>' for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(r.get(c, '')))}</td>" for c in columns) + "</tr>" for r in rows)
    return f'<table class="rp-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def plain_table_html(rows: list[dict]) -> str:
    if not rows:
        return ""
    cols = list(rows[0])
    head = "".join(f"<th>{escape(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(r[c]))}</td>" for c in cols) + "</tr>" for r in rows)
    return f'<table class="rp-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def chips_html(result: dict) -> str:
    kind = {"Found": "found", "None found": "none", "Not found": "missing", "Not used": "unused"}
    chips = "".join(f'<span class="rp-chip rp-{kind[s]}" data-area="{escape(a)}" data-status="{escape(s)}">'
                    f"{escape(a)} · {escape(s)}</span>" for a, s in areas(result))
    return f"Based on {based_on(result)} areas: {chips}"
