"""Service layer for the recruiter UI (app/streamlit_app.py): no streamlit here, so everything is
testable on its own.

    run_shortlist(profile, pdf_paths, work_dir=None, progress_callback=None)
        runs the parser on each PDF (outputs saved by file hash in work_dir/cache), scores the pool
        with the shortlisting scorer and returns a ShortlistRun: ranked results, by tier, and the
        path of each file (for the Open CV button).

Progress: progress_callback(event) gets one dict per stage event, in order. Per CV:
{"cv": k, "total": n, "file": name, "step": STEP, "state": "running" | "done"} for each step in
PARSE_STEPS, then {"state": "finished"} or {"state": "could_not_read"}; a CV read before gives
one {"state": "saved"} instead. The steps are observed by wrapping the parser's own stage functions
(resume_parser.pipeline.default_stages, passed to run_pipeline as its stage list), so the parser
itself is unchanged and its output is identical. Stage 5 reads education, experience and
publications in one call, so those three are one step here. After all CVs:
{"cv": None, "step": "Scoring", "state": "running" | "done"}. ProgressBoard turns the events into
rows and a progress bar.

Everything shown to a recruiter (table_rows, card, summary_line, export) is in plain words: no
parser codes, entry numbers or internal names. work_dir: RESUME_SHORTLIST_DIR, else
RESUME_DATA_DIR/shortlist_runs; a folder inside the repo is always refused. Uploads go only to
work_dir/uploads, exports to work_dir/exports, saved requirements to work_dir/profiles. Nothing here
reads or returns a name, gender, age, photo or nationality field (the scorer sees only education,
experience and publications).
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
from pathlib import Path
from typing import Callable

from resume_parser.schema import REPO_ROOT, DataDirError, data_root, load_config, resume_id_for
from shortlisting.features import safe_view
from shortlisting.profile import RequirementProfile, job_ad_keywords
from shortlisting.scorer import Candidate, Shortlister

ENV_WORK_DIR = "RESUME_SHORTLIST_DIR"
CACHE_VERSION = 1
TIERS = ("Shortlist", "Review", "Hold", "Unreadable")

MODE_LABELS = {"default": "Default", "education": "Education-heavy", "experience": "Experience-heavy",
               "publications": "Publications-heavy"}
DEGREE_LABELS = {"bachelors": "Bachelors", "masters": "Masters", "doctorate": "Doctorate"}
INSTITUTION_LABELS = {"Tier 1": "Top", "Tier 2": "Good", "Tier 3": "Fair"}
VENUE_LABELS = {"A": "Top", "B": "Good", "C": "Fair"}
NOT_LISTED = "Not listed"
CONFIDENCE = {"high": "High confidence", "medium": "Medium confidence", "low": "Low confidence"}
COMPONENT_LABELS = {"education": "Education", "experience": "Experience", "publications": "Publications",
                    "topic_fit": "Topic match"}

NOTE = "Tiers are decision support. Please read the CV before deciding."
UNREADABLE_TEXT = "We could not read this CV automatically. Please open it and review it manually."
FOLDER_MISSING = "We could not find this folder. Please check the path."

# the parser's stages, grouped into the steps a recruiter sees
PARSE_STEPS = (
    ("Reading the PDF", ("stage0", "stage1", "stage2")),
    ("Finding sections", ("stage3", "stage4")),
    ("Reading education, experience and publications", ("stage5",)),
    ("Checking", ("stage6", "stage7")),
)
SCORING = "Scoring"
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


def _safe_name(name: str, suffix: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(str(name)).name).strip(" .") or "file"
    return safe if safe.lower().endswith(suffix) else safe + suffix


def save_uploads(files: list[tuple[str, bytes]], work_dir: str | Path | None = None) -> list[Path]:
    """Copy uploaded (name, bytes) pairs into work_dir/uploads, only there; returns their paths."""
    folder = resolve_work_dir(work_dir) / "uploads"
    folder.mkdir(exist_ok=True)
    out = []
    for name, data in files:
        target = folder / _safe_name(name, ".pdf")
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


class ProgressBoard:
    """Rows (one per CV: file, step, done tick) and a progress bar, from the progress events."""

    def __init__(self, files: list[str]):
        self.files = list(files)
        self.step = {k: "Waiting" for k in range(1, len(files) + 1)}
        self.done = {k: False for k in self.step}
        self.units = {k: 0 for k in self.step}  # finished parse steps per CV
        self.current = 0
        self.scoring = None

    def update(self, e: dict) -> None:
        if e["cv"] is None:
            self.scoring = e["state"]
            for k in self.step:
                if self.step[k] != COULD_NOT_READ:
                    self.step[k] = SCORING if e["state"] == "running" else "Done"
                    self.done[k] = e["state"] == "done"
            return
        k, state = e["cv"], e["state"]
        self.current = k
        if state == "running":
            self.step[k] = e["step"]
        elif state == "done":
            self.units[k] += 1
        elif state == "saved":
            self.step[k], self.units[k] = SAVED, len(PARSE_STEPS)
        elif state == "could_not_read":
            self.step[k], self.units[k] = COULD_NOT_READ, len(PARSE_STEPS)
        elif state == "finished":
            self.step[k], self.units[k] = "Read", len(PARSE_STEPS)

    def rows(self) -> list[dict]:
        return [{"CV": f, "Stage": self.step[k], "Done": "✓" if self.done[k] else ""}
                for k, f in enumerate(self.files, 1)]

    def bar(self) -> tuple[float, str]:
        n = len(self.files)
        total = n * len(PARSE_STEPS) + 1
        done = sum(self.units.values()) + (self.scoring == "done")
        text = SCORING if self.scoring else f"CV {max(self.current, 1)} of {n}"
        return (done / total if total else 1.0), text


# ---------------------------------------------------------------- the run


@dataclass
class ShortlistRun:
    results: list[dict]  # ranked, each with file_name
    work_dir: Path
    by_tier: dict = field(default_factory=dict)
    paths: dict = field(default_factory=dict)  # resume_id -> PDF path (for Open CV); never exported

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


def run_shortlist(profile: RequirementProfile, pdf_paths: list[str | Path], work_dir: str | Path | None = None,
                  progress_callback: Callable[[dict], None] | None = None, today: dt.date | None = None,
                  stability: bool = False) -> ShortlistRun:
    """Parse (saved by hash), score and rank, with progress events (see the module docstring)."""
    work = resolve_work_dir(work_dir)
    cache = work / "cache"
    pdfs = [Path(p) for p in pdf_paths]
    ids = _ids(pdfs)
    n = len(pdfs)
    emit = progress_callback or (lambda e: None)
    candidates, names, extra = [], {}, {}
    for k, (pdf, rid) in enumerate(zip(pdfs, ids), 1):
        base = {"cv": k, "total": n, "file": pdf.name}
        saved = read_saved(pdf, cache) is not None
        if saved:
            emit({**base, "step": SAVED, "state": "saved"})
            main, sidecar, text = parse_cached(pdf, cache)
        else:
            step_of = {s: (label, stages) for label, stages in PARSE_STEPS for s in stages}

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
        elif not saved:
            emit({**base, "step": "Read", "state": "finished"})
        candidates.append(Candidate(rid, main or {}, decision, flags, text))
        names[rid] = pdf.name
        extra[rid] = {"unreadable_reason": _unreadable_reason(sidecar) if decision == "FAIL" else "",
                      "degrees": _degrees(main or {})}
    emit({"cv": None, "total": n, "step": SCORING, "state": "running"})
    venues, institutions = load_indexes()
    results = Shortlister(profile, venues, institutions, today=today).rank(candidates, stability=stability)
    for r in results:
        r["file_name"] = names[r["resume_id"]]
        r.update(extra[r["resume_id"]])
    emit({"cv": None, "total": n, "step": SCORING, "state": "done"})
    by_tier = {t: [r for r in results if r["tier"] == t] for t in TIERS}
    return ShortlistRun(results, work, by_tier, {rid: pdf for rid, pdf in zip(ids, pdfs)})


# ---------------------------------------------------------------- requirements (form, saved profiles)


FORM_KEYS = ("mode", "field", "min_degree_level", "experience_kind", "experience_years",
             "institution_tier_cutoff", "venue_tier_cutoff", "keywords", "synonyms")


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


def suggest_keywords(job_ad: str) -> list[str]:
    return job_ad_keywords(job_ad or "")


def build_profile(form: dict) -> RequirementProfile:
    """A profile from the UI form (FORM_KEYS; keywords and synonyms as text). Cutoffs only apply
    in their own mode."""
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


def _profiles_dir(work_dir) -> Path:
    folder = resolve_work_dir(work_dir) / "profiles"
    folder.mkdir(exist_ok=True)
    return folder


def save_profile(name: str, form: dict, work_dir: str | Path | None = None) -> Path:
    """Save the form values as work_dir/profiles/<name>.json."""
    if not str(name or "").strip():
        raise ServiceError("Please give the requirements a name.")
    path = _profiles_dir(work_dir) / _safe_name(name.strip(), ".json")
    data = {k: form.get(k) for k in FORM_KEYS}
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def list_profiles(work_dir: str | Path | None = None) -> list[str]:
    return sorted(p.stem for p in _profiles_dir(work_dir).glob("*.json"))


def load_profile(name: str, work_dir: str | Path | None = None) -> dict:
    path = _profiles_dir(work_dir) / _safe_name(name, ".json")
    if not path.is_file():
        raise ServiceError("Those saved requirements were not found.")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: data.get(k) for k in FORM_KEYS}


# ---------------------------------------------------------------- presentation (plain words only)


def confidence_text(level: str | None) -> str:
    return CONFIDENCE.get(level or "", "")


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


def reason_text(result: dict) -> str:
    """Why the CV is in its tier, in plain words (never 'rejected' or 'failed')."""
    if result["tier"] == "Unreadable":
        return " ".join(x for x in (UNREADABLE_TEXT, result.get("unreadable_reason", "")) if x)
    out = []
    for r in result.get("tier_reasons") or []:
        kind, _, key = r.partition(": ")
        if kind == "eligibility fail":
            out.append({"min_degree_level": "Below the minimum degree",
                        "institution_tier": "Institution below the chosen rank",
                        "venue_tier": "No paper in a venue at the chosen level",
                        "experience_tier": "Fewer years of experience than required"}.get(key, "A requirement is not met"))
        elif kind == "eligibility unknown":
            out.append(_check_text(key, {"result": "unknown", "reason": ""})[1])
        elif kind == "key component unusable":
            out.append(f"No usable {COMPONENT_LABELS.get(key, key).lower()} information")
        elif r == "overall certainty low":
            out.append("Too little could be read with confidence")
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
    """Rows for a results table: rank, CV (file name or resume ID), score, confidence, reason.
    No name, gender, age, photo or nationality field ever appears."""
    return [{"Rank": r["rank"] if r["rank"] is not None else "",
             "CV": r["file_name"] if label == "file_name" else r["resume_id"],
             "Score": "" if r["score"] is None else round(r["score"], 2),
             "Confidence": "" if r["tier"] == "Unreadable" else confidence_text(r["certainty"]["level"]),
             "Reason": reason_text(r)} for r in results]


def sort_and_filter(results: list[dict], sort_by: str = "score", min_score: float = 0.0) -> list[dict]:
    """sort_by 'score' (high first) or 'file' (A to Z); CVs without a score are kept only when
    min_score is 0."""
    kept = [r for r in results if (r["score"] if r["score"] is not None else -1) >= min_score or
            (min_score <= 0 and r["score"] is None)]
    if sort_by == "file":
        return sorted(kept, key=lambda r: (r["file_name"].lower(), r["resume_id"]))
    return sorted(kept, key=lambda r: (-(r["score"] if r["score"] is not None else -1), r["file_name"].lower()))


def summary_line(run: ShortlistRun) -> str:
    n = len(run.results)
    return f"{n} CV{'s' if n != 1 else ''} read: " + ", ".join(f"{len(run.by_tier.get(t, []))} {t}" for t in TIERS)


def card(result: dict) -> dict:
    """What was found and what is missing, in plain words, for one CV. Components: name, value,
    confidence, found (list of lines). Plus missing, requirements, reason."""
    comps, missing = [], []
    for name, c in result["components"].items():
        ev = c["evidence"] or {}
        found = []
        if name == "education":
            degrees = result.get("degrees") or []
            if degrees:
                found.append("Degrees: " + ", ".join(degrees))
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
            tiers = ev.get("by_tier") or {}
            found.append("Venues: " + ", ".join(f"{VENUE_LABELS.get(t, NOT_LISTED)} {k}" for t, k in tiers.items()))
        if c["value"] is None or c["reason"] in ("no_publication_entries",):
            text = MISSING_TEXT.get(c["reason"])
            if text:
                missing.append(text)
        if name == "topic_fit" and c["reason"] == "no_keywords":
            continue  # switched off by the recruiter: not a finding about the CV
        comps.append({"name": COMPONENT_LABELS.get(name, name), "value": c["value"],
                      "confidence": confidence_text(c["certainty"]), "found": [f for f in found if f]})
    reqs = []
    for k, v in result["eligibility"].items():
        req, why = _check_text(k, v)
        reqs.append({"Requirement": req, "Result": RESULT_TEXT[v["result"]], "Details": why})
    return {"components": comps, "missing": list(dict.fromkeys(missing)), "requirements": reqs,
            "confidence": confidence_text(result["certainty"]["level"]), "reason": reason_text(result)}


def export_rows(run: ShortlistRun, label: str = "resume_id") -> list[dict]:
    rows = []
    for r in run.results:
        row = {"Tier": r["tier"]}
        row.update(table_rows([r], "file_name" if label == "file_name" else "resume_id")[0])
        rows.append(row)
    return rows


def export_csv_text(run: ShortlistRun, label: str = "resume_id") -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["Tier", "Rank", "CV", "Score", "Confidence", "Reason"], lineterminator="\n")
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
