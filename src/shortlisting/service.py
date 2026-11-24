"""Service layer for the recruiter UI (app/streamlit_app.py): no streamlit here, so everything is
testable on its own.

    run_shortlist(profile, pdf_paths, work_dir=None, progress_callback=None)
        runs the parser on each PDF (outputs cached by file hash in work_dir/cache), scores the pool
        with the shortlisting scorer and returns a ShortlistRun: ranked results (each with its
        explanation card), by tier, and the unreadable list.

work_dir: RESUME_SHORTLIST_DIR, else RESUME_DATA_DIR/shortlist_runs; a folder inside the repo is
always refused. Uploaded files are copied only into work_dir/uploads; exports go to
work_dir/exports. The cache holds the parser's main JSON, sidecar and the resume's line text (for
the text fallbacks); it stays in work_dir, which is private. Nothing here reads or returns a name,
gender, age, photo or nationality field (the scorer sees only education, experience and
publications).
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from resume_parser.schema import REPO_ROOT, DataDirError, data_root, load_config, resume_id_for
from shortlisting.profile import RequirementProfile, job_ad_keywords
from shortlisting.scorer import Candidate, Shortlister

ENV_WORK_DIR = "RESUME_SHORTLIST_DIR"
CACHE_VERSION = 1

MODE_LABELS = {"default": "Default", "education": "Education-heavy", "experience": "Experience-heavy",
               "publications": "Publications-heavy"}
MODE_SENTENCES = {
    "default": "A balanced view: education, experience, publications and topic fit all count.",
    "education": "Puts most weight on degrees and where they were earned.",
    "experience": "Puts most weight on years of academic or industry experience.",
    "publications": "Puts most weight on publications, their venues and how recent they are.",
}
COMPONENT_LABELS = {"education": "Education", "experience": "Experience", "publications": "Publications",
                    "topic_fit": "Topic fit"}
CHECK_LABELS = {"min_degree_level": "minimum degree level", "institution_tier": "institution tier cutoff",
                "venue_tier": "venue tier cutoff", "experience_tier": "experience years"}
NOTE = ("Tiers are decision support, not decisions. Tier values and lookup coverage are limited; "
        "an unlisted institution or an unknown venue is neutral, never a penalty.")
UNREADABLE_TEXT = "The parser could not read this file; please open it manually."


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
            except DataDirError as exc:
                raise ServiceError(f"set {ENV_WORK_DIR} or RESUME_DATA_DIR ({exc})") from None
    path = Path(work_dir).resolve()
    if _inside_repo(path):
        raise ServiceError("the work folder must be outside the repo.")
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
        safe = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(str(name)).name) or "upload.pdf"
        if not safe.lower().endswith(".pdf"):
            safe += ".pdf"
        target = folder / safe
        target.write_bytes(data)
        out.append(target)
    return out


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- parsing (cached)


def _record_text(record) -> str:
    stage1 = record.results.get("stage1")
    data = (stage1.data if stage1 is not None else None) or {}
    return "\n".join(str(x.get("text", "")) for page in sorted(data) for x in (data[page] or {}).get("lines") or [])


def parse_cached(pdf: Path, cache_dir: Path) -> tuple[dict | None, dict | None, str]:
    """(main JSON, sidecar, line text) for one PDF, from the cache or by running the parser. A
    parser crash gives (None, None, "") and is listed as unreadable."""
    key = file_hash(pdf)
    path = cache_dir / f"{key}.json"
    if path.is_file():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("cache_version") == CACHE_VERSION:
            return cached["main"], cached["sidecar"], cached["text"]
    from resume_parser.pipeline import run_pipeline

    try:
        record = run_pipeline(pdf)
    except Exception:  # never shown: the file is listed as unreadable
        return None, None, ""
    entry = {"cache_version": CACHE_VERSION, "main": record.main, "sidecar": record.sidecar, "text": _record_text(record)}
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    return entry["main"], entry["sidecar"], entry["text"]


@lru_cache(maxsize=1)
def load_indexes():
    """The venue and institution lookups (from the lexicon folder), loaded once."""
    from shortlisting.tiers.institutions import load_index as load_institutions
    from shortlisting.tiers.venues import load_index as load_venues

    return load_venues(), load_institutions()


# ---------------------------------------------------------------- the run


@dataclass
class ShortlistRun:
    results: list[dict]  # ranked, each with file_name and its explanation card
    work_dir: Path
    by_tier: dict = field(default_factory=dict)

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


def run_shortlist(profile: RequirementProfile, pdf_paths: list[str | Path], work_dir: str | Path | None = None,
                  progress_callback: Callable[[int, int, str], None] | None = None, today: dt.date | None = None,
                  stability: bool = True) -> ShortlistRun:
    """Parse (cached), score and rank. progress_callback(done, total, step) after each file."""
    work = resolve_work_dir(work_dir)
    cache = work / "cache"
    pdfs = [Path(p) for p in pdf_paths]
    ids = _ids(pdfs)
    candidates, names = [], {}
    for k, (pdf, rid) in enumerate(zip(pdfs, ids), 1):
        main, sidecar, text = parse_cached(pdf, cache)
        if sidecar is None:
            decision, flags = "FAIL", ["parser_error"]
        else:
            decision = sidecar.get("decision")
            flags = sorted({f.get("code", "") for f in sidecar.get("flags") or [] if isinstance(f, dict)})
        candidates.append(Candidate(rid, main or {}, decision, flags, text))
        names[rid] = pdf.name
        if progress_callback:
            progress_callback(k, len(pdfs) + 1, "parsing")
    venues, institutions = load_indexes()
    results = Shortlister(profile, venues, institutions, today=today).rank(candidates, stability=stability)
    for r in results:
        r["file_name"] = names[r["resume_id"]]
    if progress_callback:
        progress_callback(len(pdfs) + 1, len(pdfs) + 1, "scoring")
    by_tier = {t: [r for r in results if r["tier"] == t] for t in ("Shortlist", "Review", "Hold", "Unreadable")}
    return ShortlistRun(results, work, by_tier)


# ---------------------------------------------------------------- presentation helpers (plain data)


def field_options() -> list[tuple[str | None, str]]:
    fields = load_config("venue_fields.json")["fields"]
    return [(None, "None")] + [(k, v.get("label", k)) for k, v in fields.items()]


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
    """A profile from the UI form: mode, field, min_degree_level, experience_kind,
    experience_years, institution_tier_cutoff, venue_tier_cutoff, keywords (text), synonyms (text).
    Cutoffs only apply in their own mode."""
    mode = form.get("mode", "default")
    synonyms = parse_synonyms(form.get("synonyms", ""))
    keywords = parse_keywords(form.get("keywords", ""))
    keywords += [k for k in synonyms if k not in keywords]
    return RequirementProfile(
        mode=mode, field=form.get("field") or None, min_degree_level=form.get("min_degree_level") or None,
        experience_kind=form.get("experience_kind", "academic"), experience_years=int(form.get("experience_years", 5)),
        institution_tier_cutoff=(form.get("institution_tier_cutoff") or None) if mode == "education" else None,
        venue_tier_cutoff=(form.get("venue_tier_cutoff") or None) if mode == "publications" else None,
        keywords=keywords, synonyms=synonyms)


def parser_flag_text(result: dict) -> str:
    flags = result.get("parser_flags") or []
    parts = []
    if "parse_decision:NEEDS_REVIEW" in flags:
        parts.append("parser asks for a check")
    other = [f for f in flags if not f.startswith("parse_decision:")]
    if other:
        parts.append(f"{len(other)} parser note(s)")
    return "; ".join(parts)


def reason_text(result: dict) -> str:
    """The tier reasons in plain words (never 'rejected' or 'failed')."""
    if result["tier"] == "Unreadable":
        return UNREADABLE_TEXT
    out = []
    for r in result.get("tier_reasons") or []:
        if r.startswith("eligibility fail: "):
            out.append(f"does not meet the {CHECK_LABELS.get(r.split(': ', 1)[1], r.split(': ', 1)[1])} requirement")
        elif r.startswith("eligibility unknown: "):
            out.append(f"{CHECK_LABELS.get(r.split(': ', 1)[1], r.split(': ', 1)[1])} could not be checked")
        elif r.startswith("key component unusable: "):
            out.append(f"no usable {COMPONENT_LABELS.get(r.split(': ', 1)[1], r).lower()} data")
        else:
            out.append(r)
    return "; ".join(out)


def table_rows(results: list[dict], label: str = "file_name") -> list[dict]:
    """Rows for a results table: rank, file name (or resume ID), score, certainty, parser flag,
    reason. No name, gender, age, photo or nationality field ever appears."""
    rows = []
    for r in results:
        rows.append({"rank": r["rank"] if r["rank"] is not None else "",
                     "file": r["file_name"] if label == "file_name" else r["resume_id"],
                     "score": "" if r["score"] is None else round(r["score"], 3),
                     "certainty": r["certainty"]["level"] if r["tier"] != "Unreadable" else "",
                     "parser flag": parser_flag_text(r), "reason": reason_text(r)})
    return rows


def card(result: dict) -> dict:
    """The explanation card as plain data for display: components (value, certainty, evidence as
    entry numbers from 1 and tier labels), dropped components, eligibility, rank range, override."""
    cvals = load_config("shortlisting.json")["certainty_values"]
    comps = []
    for name, c in result["components"].items():
        ev = c["evidence"] or {}
        evidence = []
        if "degree_level" in ev:
            evidence.append(f"degree level {ev['degree_level']['level']} (entry {ev['degree_level']['entry'] + 1})")
        if "institution" in ev:
            i = ev["institution"]
            src = " — editorial override" if i.get("source") == "editorial override" else ""
            evidence.append(f"institution {i['tier']} (entry {i['entry'] + 1}){src}")
        if ev.get("unlisted_entries"):
            evidence.append("unlisted institution (neutral): entries " + ", ".join(str(k + 1) for k in ev["unlisted_entries"]))
        if "stream_fit" in ev:
            evidence.append(f"stream fit: {ev['stream_fit']['match']}")
        if name == "experience" and "status" in ev:
            evidence.append(f"{ev['status'].replace('_', ' ')}" + (f", {ev['years']} years" if ev.get("years") is not None else "")
                            + (f" ({len(ev['entries'])} entr{'y' if len(ev['entries']) == 1 else 'ies'})" if ev.get("entries") else ""))
        if "by_tier" in ev:
            evidence.append("venues " + ", ".join(f"{t} {n}" for t, n in ev["by_tier"].items()) + (" (capped)" if ev.get("capped") else ""))
        if name == "topic_fit" and ev.get("source"):
            evidence.append(f"from {ev['source'].replace('_', ' ')}" + (f": entries {', '.join(str(k + 1) for k in ev['entries'])}" if ev.get("entries") else ""))
        comps.append({"name": COMPONENT_LABELS.get(name, name), "value": c["value"], "certainty": c["certainty"],
                      "certainty_value": cvals.get(c["certainty"]) if c["certainty"] else None,
                      "evidence": evidence, "reason": c["reason"],
                      "editorial_override": "editorial_override" in (c.get("notes") or [])})
    return {"components": comps,
            "dropped": [{"name": COMPONENT_LABELS.get(d["component"], d["component"]), "reason": d["reason"].replace("_", " ")}
                        for d in result["dropped_components"]],
            "eligibility": [{"check": CHECK_LABELS.get(k, k), "result": v["result"], "reason": v["reason"]}
                            for k, v in result["eligibility"].items()],
            "rank_range": result.get("rank_range"), "certainty": result["certainty"],
            "reason": reason_text(result)}


def export_csv(run: ShortlistRun, label: str = "resume_id") -> Path:
    """The ranked table as CSV in work_dir/exports (never in the repo); label resume_id or file_name."""
    folder = resolve_work_dir(run.work_dir) / "exports"
    folder.mkdir(exist_ok=True)
    path = folder / f"shortlist_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    rows = []
    for r in run.results:
        row = table_rows([r], "file_name" if label == "file_name" else "resume_id")[0]
        row.update(tier=r["tier"], rank_range="" if not r.get("rank_range") else f"{r['rank_range'][0]}-{r['rank_range'][1]}")
        rows.append(row)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["tier", "rank", "file", "score", "certainty", "parser flag", "reason", "rank_range"])
        w.writeheader()
        w.writerows(rows)
    return path
