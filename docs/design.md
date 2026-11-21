# Resume Parser: Design (v1)

## 1. Goal and scope

- Read academic CVs (PDF) and write one JSON file per resume.
- Only four sections matter: **personal information, education, work experience, publications**.
  Everything else is best effort. If other sections are detected badly, that is fine.
- For the four sections the aim is to miss nothing. We cannot promise 100%, so the system
  must (a) extract generously, (b) keep the raw text of everything it extracts, and
  (c) flag any resume where it is unsure.
- No LLMs anywhere in this repo.
- The JSON files are the input to the shortlisting system (top-k ranking), built on top later.
- This repo is public. No resumes, no ground truth, no real outputs are ever committed.
  Real data lives in a folder outside the repo (see section 9).

## 2. Principles

1. **Never silently drop text.** Every line of the resume ends up in a section, an entry,
   "other", or "unassigned". Unassigned lines that look like education, experience or
   publications are a warning.
2. **Every stage has: a main method, a fallback, a check, and a status** (OK / WARN / FAIL).
3. **Checks that run on new resumes use no ground truth** (label-free). Ground truth is only
   used offline to measure stages and to pick thresholds.
4. **Deterministic.** Same input gives byte-identical output. No timestamps, no absolute paths,
   fixed ordering.
5. **Provenance.** Every extracted field records where it came from (page, line numbers).
6. **Vocabulary lives in config files**, not in code (headings, degrees, titles, institutions).
7. **Tests use fake (synthetic) resumes** kept in the repo. Real resumes are never in the repo.

## 3. Pipeline overview

```
PDF
 -> Stage 0 Intake and triage
 -> Stage 1 Layout extraction            (lines with position, font size, bold, table cell)
 -> Stage 2 Cleaning                     (headers/footers, bullets, wrapped lines)
 -> Stage 3 Section detection            (find where each section starts and ends)
 -> Stage 4 NORMALIZATION LAYER          (one standard "Canonical Document" for every resume,
                                          with the ideal section names)
 -> Stage 5 Entity extraction            (reads ONLY the Canonical Document)
              + field normalization      (degree_level, experience_type, publication type)
 -> Stage 6 Validation and scoring       (per-section scores, decision)
 -> Stage 7 Output                       (JSON in the same format as the ground truth)
```

The idea: all the messy differences between resumes (layout, fonts, bullets, heading wording)
are absorbed in stages 1 to 4. After stage 4 every resume looks the same, so the extraction
rules in stage 5 are written once for one standard form.

Each stage returns: its result, its metrics, its status, and reasons for any WARN/FAIL.

**What a FAIL does.**
- FAIL in stage 0 or 1: stop. There is no usable text, so nothing later can be trusted.
  The resume goes to the review queue with the reason.
- WARN or FAIL in stages 2 to 5: carry on, but mark the resume and the affected section.
  The JSON is still written, with `needs_review: true` and the reasons, so no information is lost.

## 4. Stages in detail

### Stage 0: Intake and triage
- Job: confirm the file is a valid, unencrypted PDF; count pages; check there is a text layer.
- Fallback: none. If there is no text layer, flag "needs OCR" (OCR is a later add-on, non-LLM).
- Checks: valid PDF; at least 200 text characters per page on average.
- Gate: FAIL stops the resume.

### Stage 1: Layout extraction
- Job: turn the PDF into a list of **lines**, each with: text, page, bounding box,
  font size, bold, whether inside a table cell, reading-order index.
- Main: pdfplumber (words with coordinates, tables read cell by cell).
- Fallback 1: pdfminer.six layout analysis. Fallback 2: another extractor only if its licence
  is compatible with a public repo (PyMuPDF is AGPL: check before using).
- Method: run the main extractor, check quality; if the checks fail, try the fallback and keep
  the better result.
- Checks: share of garbled characters (`(cid:N)`, replacement characters) under 1%; glued-word
  rate low; agreement with the second extractor's words at least 90%; no empty pages.
- Gate: FAIL if both extractors fail the checks.

### Stage 2: Cleaning
- Job: remove repeated page headers and footers; remove bullet and icon glyphs; join lines that
  wrap; fix hyphenation; normalise unicode and spaces.
- Check: letters and digits kept at least 95% (excluding removed headers, footers, glyphs).
  A drop means the cleaner deleted real content.
- Gate: WARN if below the threshold; use the uncleaned lines for that page.

### Stage 3: Section detection
- Job: find where each section starts and ends, and give each block a raw label (the heading as
  printed, plus a best-guess target name). Mapping to the ideal section names is finished in
  stage 4.
- Signals: heading words from `config/section_aliases.json`; heading look (larger relative font,
  bold, all caps, short line, white space above); the content that follows (dates, degree
  words, citation patterns).
- Fallbacks: (1) heading vocabulary, (2) heading look, (3) content patterns for lines with no
  heading. A small classifier can be added later if data allows.
- Checks: personal block found near the top; at least one of education, experience, publications
  found; unassigned lines that look like dates, degrees or citations are within a small limit;
  short bold or capitalised lines that look like headings but are unknown are listed.
- Gate: WARN if a target section is missing or leaking lines.

### Stage 4: Normalization layer (the standard form)

- Job: turn the cleaned lines plus the section boundaries from stage 3 into one
  **Canonical Document**. This is the only thing stage 5 reads.
- **Canonical section names** (the ideal names): `personal`, `education`, `experience`,
  `publications`, `other`. Every heading variant is mapped to one of these using
  `config/section_aliases.json` (many aliases -> one canonical name).
  Sections that are not one of the four are kept under `other` with their original label
  (for example `skills`, `awards`) so nothing is lost.
- **Mapping rule for experience** (decide it once, in the config, with comments): only headings
  that list positions held (Work Experience, Employment, Academic Positions, Postdoctoral
  Positions) map to `experience`. Headings that list courses taught, responsibilities or
  projects map to `other`. This matches the ground-truth labelling guide ("jobs only").
- **Standard form of the lines:** one line per logical line, bullets and icons removed, wrapped
  lines joined, spaces and unicode normalised, original text otherwise untouched (dates stay as
  printed). Each line keeps its id, page and position, so provenance survives.
- Fallbacks: if a heading is not in the alias map, use the heading look (bigger font, bold, caps)
  and the content that follows; if still unknown, the section is `other` with the original label
  and a WARN.
- Checks: every cleaned line is in exactly one section (or `unassigned`); line order is kept;
  joining the canonical lines gives back the cleaned text (no loss, no duplication);
  all section names are from the allowed list; no target-looking lines (dates, degrees,
  citations) sit in `other` or `unassigned` beyond a small limit.
- Gate: WARN if a target section is empty or missing, or lines leak into `other`.

Canonical Document (sketch):
```json
{
  "doc_id": "r_0001",
  "sections": [
    {"name": "personal",     "heading_raw": null,                      "lines": [{"id": 1, "page": 1, "text": "..."}]},
    {"name": "education",    "heading_raw": "Academic Qualifications", "lines": []},
    {"name": "experience",   "heading_raw": "Work History",            "lines": []},
    {"name": "publications", "heading_raw": "Journal Articles",        "lines": []},
    {"name": "other",        "heading_raw": "Skills", "original_name": "skills", "lines": []}
  ],
  "unassigned": []
}
```

### Stage 5: Entity extraction and field normalization

**Personal.** Name (top lines, no digits, 2 to 4 words, honorifics removed; fallback: name from
the email address), email, phone(s), links (ORCID, Scholar, LinkedIn).

**Education.** Split the section into entries using anchors (degree words, year ranges).
Fields: degree, stream, institution (institution list plus keywords), then `degree_level` by
field normalization. Year and grade are kept in the sidecar. School-level entries (10th, 12th,
etc.) are stored under `excluded` in the sidecar, not in the main list. Raw text goes to the
sidecar as well.

**Experience.** Split into entries using date ranges as anchors. Fields: job_title, institution (employer),
dates as printed, then `experience_type`. Parsed start and end go to the sidecar. Raw text goes
to the sidecar.

**Publications.** Split into citations (numbering, year anchors, merge wrapped lines).
Fields: title, year, venue, type (journal / conference / other; finer types such as book chapter
are kept in the sidecar) from the heading and venue words. The raw citation goes to the sidecar.
Remove duplicates. Talks, posters, newspaper articles go to `non_paper_publications` in the
sidecar. Also read printed totals ("Total: 65") into `stated_totals`.

**Fallback for all entry splitting:** anchors, then blank-line splitting, then the whole section
as one entry with a WARN.

**Field normalization** (the values that the ground-truth format needs):
- `degree_level` (doctorate / masters / bachelors / other) from `config/degrees.json`.
- `experience_type` (academic / industry / other): university, college, institute, school,
  research centre words -> academic; company words (Pvt, Ltd, Inc, Technologies) -> industry;
  otherwise other.
- Publication `type` (journal / conference / other) from the author's heading, then venue words.
- Strings such as degree, institution, job title and dates stay **as printed** in the CV,
  as in the ground truth. Parsed dates and cleaned names go to the sidecar file, not the main JSON.

**Per-field confidence** is rule-based (which rules fired). Every field has `source` (page, lines),
stored in the sidecar file.

**Checks:**
- Personal: name found; email or phone found.
- Education: at least one higher-education entry if an education section exists; each entry has
  a degree or an institution; years plausible (1950 to current year + 6); no table-header text.
- Experience: each entry has a date range or a title; end not before start; no entry is a
  fragment (very short text).
- Publications: entry length sensible; most entries have a year; count vs stated total
  ("Total: 65") within 10% when a total is printed; duplicates removed count reported.
- Coverage (all sections): every line of the section belongs to an entry or is marked noise.
  Leftover lines are the main sign of missed content.

### Stage 6: Validation and scoring
- Each section gets a score from its checks (weighted share of checks passed).
- Decision: **ACCEPT** if every target section meets its threshold. Otherwise **NEEDS_REVIEW**,
  with the section names and reasons.
- Thresholds live in `config/thresholds.yaml` and are chosen from ground truth (section 7).

### Stage 7: Output
- Two files per resume, plus one index file per run, all in the data folder (never in the repo).
- Resumes with stage-0/1 failures get a sidecar and an index entry only (no main JSON).
- No timestamps, no absolute paths. Keys and lists in a fixed order.

## 5. Output format

### 5.1 Main file: exactly the same format as the ground-truth JSON (GT v2)

`outputs/<name>.json`, same name as the PDF (`xyz.pdf` -> `xyz.json`). The keys are exactly these
and nothing else, so the evaluator, the validator (`validate_gt_v2.py`) and the shortlisting system
can read predictions and ground truth with the same code:

```json
{
  "resume_file": "xyz.pdf",
  "candidate_name": "",
  "education": [
    {"degree": "", "degree_level": "doctorate", "stream": "", "institution": ""}
  ],
  "experience": [
    {"job_title": "", "institution": "", "dates": "", "experience_type": "academic"}
  ],
  "publications": [
    {"title": "", "type": "journal", "year": "", "venue": ""}
  ],
  "stated_totals": {"journal": null, "conference": null, "total": null},
  "notes": ""
}
```

Rules (same as the labelling guide): higher education only (bachelors and above); jobs only in
experience; dates copied as printed; if the CV does not say it, leave it empty; never guess.
`notes` is left empty by the parser.
`degree_level` in {doctorate, masters, bachelors, other}; `experience_type` in {academic,
industry, other}; publication `type` in {journal, conference, other}.

### 5.2 Sidecar file: everything the parser knows that the ground truth does not

`outputs/<name>.meta.json`:
```json
{
  "schema_version": "1.0",
  "resume_id": "r_0001",
  "decision": "ACCEPT",
  "needs_review": false,
  "flags": [{"section": "education", "code": "UNASSIGNED_TARGET_LINES", "detail": ""}],
  "section_scores": {"personal": 1.0, "education": 0.9, "experience": 1.0, "publications": 0.8},
  "personal_extra": {"emails": [], "phones": [], "links": []},
  "provenance": {"education[0]": {"page": 1, "lines": [10, 12], "raw_text": ""}},
  "excluded": {"school_level_education": [], "non_paper_publications": []},
  "parsed_dates": {"experience[0]": {"start": "2016-07", "end": "2023-03"}},
  "stage_report": {"stage1": {"status": "OK", "metrics": {}}}
}
```
Why a separate file: the main JSON stays identical to the ground truth, and the extra information
(flags, provenance, raw text, email and phone) is still available when someone asks
"why was this flagged?" or "where did this value come from?".

### 5.3 Run index

`outputs/run_index.json`: for each resume, its `resume_file`, `decision` and flags count.
The shortlisting system reads the index so it knows which resumes were accepted and which are
flagged. (A flagged resume still has its main JSON, so the recruiter can still see it.)

## 6. Evaluation (offline, uses ground truth)

The method is a stage-by-stage loss funnel. For every ground-truth item, find the first stage where
it is lost:

| Stage | Question | Score |
|---|---|---|
| 1 | Is the GT text found, complete, in the extracted lines? | Stage 1 recall |
| 3-4 | Is it in the right (canonical) section? | Routing recall |
| 5 | Is there a matching entry with correct fields? | Entry F1, field accuracy |
| Completeness | Is the entry truncated? | Truncation rate |

Also report false positives by cause. Use one-to-one fuzzy matching, bootstrap confidence
intervals, and a fixed random seed. Ground truth is split into: **tuned** (first 10), **dev**
(15, used for building), **test_frozen** (15, scored once at the end, aggregates only).

## 7. Choosing thresholds

In real use there is no ground truth, so decisions must come from label-free checks. To choose
thresholds:
1. Run the pipeline on dev. For each resume, compute the label-free scores.
2. Using ground truth, mark each resume's section as "really correct" or "has errors".
3. Pick the threshold so that sections with errors are caught (high recall) while few correct
   ones are flagged. Plot "accuracy of accepted resumes" against "share sent to review".
4. Freeze the thresholds, then score test_frozen once.

Report: of the accepted resumes, how many are fully correct; and what share go to review.

## 8. Repo layout

```
resume-parser/
  README.md  LICENSE  pyproject.toml  requirements.txt  .gitignore
  .githooks/pre-commit  .github/workflows/ci.yml
  config/   section_aliases.json  degrees.json  titles.json  institutions.json  thresholds.yaml
  schema/   resume.schema.json
  src/resume_parser/
     intake.py  pipeline.py  cli.py  schema.py  report.py
     extract/  base.py  pdfplumber_extractor.py  pdfminer_extractor.py
     clean.py  sections.py  normalize.py        (normalize.py builds the Canonical Document)
     entities/ personal.py  education.py  experience.py  publications.py
     field_norm.py  validate.py  scoring.py  writer.py   (writer.py writes main JSON + sidecar + index)
  eval/     evaluate.py  funnel.py  calibrate_thresholds.py
  fixtures/ synthetic/   (fake resumes, built by a script)
  tests/    one test file per stage, plus determinism
  scripts/  check_no_data.py  make_synthetic_resumes.py
  docs/     design.md
```

## 9. Data (never in the repo)

- A folder on each person's computer, set by environment variable `RESUME_DATA_DIR`:
  `pdfs/`, `ground_truth_v2/`, `outputs/`, `review_queue/`, `reports/`.
- Back it up somewhere private. Git will not have a copy.
- Guards: `.gitignore`, a pre-commit hook and a CI check (`scripts/check_no_data.py`) that
  refuse PDFs, real JSON outside `config/` and `schema/` and `fixtures/`, and large files.
- Logs and test output show resume IDs, never names.

## 10. Build order and "done" for each stage

Build one stage at a time. A stage is done when it passes its checks on dev and its tests,
**before** the next stage starts.

| Order | Stage | Done when (proposed, adjust after first measurement) |
|---|---|---|
| 1 | Skeleton, schema, evaluation harness, synthetic fixtures, guards | Tests pass on fake resumes; harness reproduces old funnel numbers on old outputs |
| 2 | Stages 0, 1, 2 | Stage 1 recall of GT strings at least 97% on dev (fuzzy 85) |
| 3 | Stages 3 and 4 (sections and normalization layer) | Routing recall at least 95% for the four sections; Canonical Document checks pass (no line lost or duplicated) |
| 4 | Stage 5 personal, education | Entry recall at least 90% |
| 5 | Stage 5 experience | Entry recall at least 90% |
| 6 | Stage 5 publications | Entry recall at least 90%, duplicates and non-papers handled |
| 7 | Stages 6, 7, thresholds | Flag catches at least 90% of resumes with real errors on dev; main JSON passes validate_gt_v2.py |
| 8 | test_frozen run (once) | Report only, no tuning afterwards |

## 11. What we reuse from the old repo

- Evaluation and funnel scripts (copy the code, not the data).
- Heading alias map, regexes for dates, degrees, titles (move into `config/`).
- Lessons from the diagnostic: tables break reading order; school-level entries and honorifics
  need rules; publication scope needs a written rule; duplicates must be removed.

## 12. Open decisions

- **LLM policy.** The professor earlier asked for LLM-based ranking. This design uses no LLM.
  Confirm with the professor before the shortlisting phase. If an LLM is wanted for ranking,
  it can be an optional plug-in outside this extraction repo.
- Licence (MIT or Apache-2.0), agreed with the partner.
- Publication scope rule (what counts as a paper).
- Which parts the partner owns (suggestion: shortlisting module in `src/shortlisting/` later).

## 13. Risks

- A rewrite can repeat old problems. Mitigation: the evaluation harness comes first and every
  stage is measured before the next.
- Small ground truth (25 usable resumes plus 15 frozen test). Report confidence intervals.
- Table-heavy layouts and very long publication lists are the known hard cases.
