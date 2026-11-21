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
7. **Tests use fake (synthetic) resumes**, built by `scripts/make_synthetic_resumes.py` into a
   temporary folder during the tests. No PDF is ever committed. Real resumes are never in the repo.

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
It also records which method it used and every method it tried (see "Choosing between
methods" below).

**What a FAIL does.**
- FAIL in stage 0 or 1: stop. There is no usable text, so nothing later can be trusted.
  Only a sidecar and an index entry are written, with the reason. (Nothing is written to
  `review_queue/` yet; the index shows the decision.)
- WARN or FAIL in stages 2 to 7: carry on, but mark the resume and the affected section.
  The JSON is still written, with `needs_review: true` and the reasons, so no information is lost.
- Reason codes that only say a method is not built yet (`not_implemented`) are kept in the
  stage report but are not flags.

**Choosing between methods.** Every stage has a fixed, ordered chain of named methods.
A shared helper in the runner (`run_with_fallbacks`) does the choosing:
1. Run the methods in order. After each one, compute the stage's check score in [0, 1].
2. Stop at the first result whose score reaches the stage's accept threshold
   (`config/thresholds.json`). Otherwise try the next method.
3. If no method reaches the threshold, keep the best-scoring result and set WARN.
   Ties go to the earlier method.
4. If no method gives any output: FAIL for stages 0 and 1, WARN for later stages.
   Methods that are not built yet are recorded but never chosen. While any method in the
   chain is still unbuilt, "no output" gives OK with the code `not_implemented` instead,
   because the unit cannot be judged yet. The exception is a unit that must give output
   (an image-only page in stage 1): it FAILs. This rule only lets the skeleton run in tests:
   each unbuilt method is marked `stub`, and `cli run` refuses to start while any are left,
   listing them, unless `--allow-stubs` is given.
5. Some methods are last resorts that always give WARN when chosen
   (`whole_section_one_entry`, `from_file_name`, `keep_as_other_with_warning`).
6. A stage may ask for every method to be run and scored even after one is accepted
   (`run_all`, used in stage 1 so all scores are recorded). The choice rule is unchanged.
   A method may have a condition (`run_if`); when it is false the method is recorded as
   `SKIPPED`. A check may return the separate values behind its score; they are stored with
   the attempt (`details`).
7. A method that needs an optional dependency (OCR) is marked `optional`, not `stub`: it does
   not block a run, and reports why it cannot run.

The choice is made per unit where layouts differ inside one resume: per page in stage 1, per
section in stage 5. The stage report records the method used and every method tried
(name, score, status, reason codes), per unit where the stage uses units. The same input
always picks the same method.

## 4. Stages in detail

### Stage 0: Intake and triage
- Job: confirm the file is a valid, unencrypted PDF; count pages; check there is a text layer.
- Methods: `open_pdfplumber`, then `open_pdfminer`. If neither can read the file: FAIL `unreadable`.
- Page profile, per page: text characters, share of the page covered by images, and a class
  `TEXT`, `IMAGE_ONLY` or `MIXED`; `table_present`; `two_column_hint` (few text lines cross the
  middle of the page while both halves have text). A blank page counts as `IMAGE_ONLY`.
  Cut-offs are in `config/thresholds.json` under `page_profile`.
- Checks: valid PDF; at least 200 text characters per page on average.
- Gate: FAIL (unreadable) stops the resume. Low or no text gives WARN `needs_ocr`, and
  stage 1 decides whether the pages are usable (so OCR gets a chance to run).

### Stage 1: Layout extraction
- Job: turn the PDF into a list of **lines**. Every line has `id` (`p001_l0007`), `page`,
  `text`, `bbox` (x0, top, x1, bottom), `font_size` (median), `bold` (share of characters in a
  bold font), `in_table`, `cell_row`, `cell_col`, `row_id`, `segment_index` and `reading_order`.
- Lines are built from words. Words whose tops are within 0.5 x font size form a row; a row is
  split into segments where a gap is at least 2.5 x the row's median font size (so a justified
  paragraph stays whole, but blocks side by side do not merge). Segments of one row share a
  `row_id`, so stage 5 can join them again.
- Methods, chosen per page:
  1. `pdfplumber_words_tables`: lines from words; tables found by `find_tables` are read cell
     by cell (one line per cell), and words inside a cell are not read again. Always runs.
  2. `pdfminer_layout`: the same line structure from pdfminer's text lines, in pdfminer's own
     reading order; no tables. Always runs (it is also the agreement reference).
  3. `pdfplumber_loose_spacing`: method 1 with a tight and a wide word-spacing tolerance,
     keeping the variant with fewer glued words. Runs only when method 1 was not accepted.
  4. `ocr_tesseract`: optional (pytesseract and the tesseract program); reports
     `ocr_unavailable` when missing. Not built yet.
  Any further extractor only if its licence is compatible with a public repo (PyMuPDF is AGPL).
- Pages that stage 0 marked `IMAGE_ONLY` only try OCR. If that gives nothing, the page is
  FAIL `no_text_layer`.
- Page score (0..1), label-free, settings in `thresholds.json` (`stage1_checks`, to calibrate):
  - 0 when more than 1% of the characters are garbled (`(cid:N)`, replacement and private-use
    characters); a page with no lines gives no output (`empty_page`);
  - otherwise the word overlap with the other text method: shared words, as a multiset of
    lowercase words (order does not matter), over the larger word count (method 1 and 3 are
    compared with method 2, method 2 with method 1);
  - halved when more than 5% of the words look glued: a run of 25+ letters, or 3+ lowercase
    letters followed by an Uppercase and a lowercase letter; emails, URLs and DOIs are left
    out, and an ignore list (PhD, LaTeX, MATLAB, BibTeX, ...) never counts.
  The separate values (garbled rate, glued rate and count, overlap, words, lines) are stored
  with every attempt. The stage metrics count the pages over the glued-word limit.
- Gate: FAIL only when no page is usable. Some unusable pages give WARN.
- With `--save-stages`, every method's lines per page are also saved (`stage1_methods.json`).

### Stage 2: Cleaning
- Job: clean the stage 1 lines, keeping every line's `id`, `page` and `bbox`.
- Methods: `full_clean`; `conservative_clean` (glyphs and whitespace only); `raw_lines` (no change).
- `full_clean`:
  - removes repeated page headers and footers, only on documents of 3 or more pages: the
    same text (digits masked, so page numbers match) in the same vertical band (within 5 pt,
    in the top or bottom 12% of the page) on at least half the pages;
  - strips bullet and icon glyphs, `(cid:N)` and replacement characters; normalises unicode
    (NFKC) and whitespace;
  - joins a wrapped line to the one above, fixing a hyphen at the line end, only inside
    paragraph blocks: neither line in a table cell, a small vertical gap (at most 0.6 x font
    size), the same left edge (within 2 pt), the next line starting with a lowercase letter.
    A joined line keeps the first line's id and lists the others in `joined_ids`. Wrapped
    citations are joined in stage 5, not here.
- Check: letters and digits kept at least 95% (excluding removed headers, footers, glyphs).
  A drop means the cleaner deleted real content; the next method is then tried.
- Settings in `thresholds.json` (`stage2_clean`, to calibrate).

### Stage 3: Section detection
- Job: find where each section starts and ends, and give each block a raw label (the heading as
  printed, plus a best-guess target name). Mapping to the ideal section names is finished in
  stage 4.
- Signals: heading words from `config/section_aliases.json`; heading look (larger relative font,
  bold, all caps, short line, white space above); the content that follows (dates, degree
  words, citation patterns).
- Methods: `heading_vocabulary`, `heading_look`, `content_patterns` (for lines with no
  heading). A small classifier can be added later if data allows.
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
- Methods: `exact_alias_map`; `fuzzy_alias_match` (rapidfuzz against the alias map);
  `keep_as_other_with_warning` (the section is `other` with the original label and a WARN).
  Heading look and the content that follows come from stage 3.
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

**Methods for entry splitting, chosen per section:** `anchor_split`, `table_row_split`,
`paragraph_split`, then `whole_section_one_entry` (always last, always WARN).
**Methods for the name:** `top_lines`, `from_email`, `from_file_name` (lowest confidence, flagged).
Accept thresholds per section are in `config/thresholds.json`.

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
- Thresholds live in `config/thresholds.json` and are chosen from ground truth (section 7).
  Until then they are placeholders, marked as to be calibrated.

### Stage 7: Output
- Two files per resume, plus one index file and one run summary per run, all in the data
  folder (never in the repo).
- Resumes with stage-0/1 failures get a sidecar and an index entry only (no main JSON).
- The main JSON is checked against `schema/resume.schema.json`. If it is invalid, only the
  sidecar is written, with a FAIL flag `main_json_invalid`.
- Every file is checked against its schema before it is written.
- No timestamps, no absolute paths. Keys and lists in a fixed order.
- With `--save-stages`, the output of stages 1, 3, 4 and 5 is also saved per resume, for the
  funnel (section 6).

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
  "resume_id": "r_0a1b2c3d",
  "resume_file": "xyz.pdf",
  "decision": "ACCEPT",
  "needs_review": false,
  "flags": [{"section": "education", "code": "UNASSIGNED_TARGET_LINES", "detail": ""}],
  "section_scores": {"personal": 1.0, "education": 0.9, "experience": 1.0, "publications": 0.8},
  "personal_extra": {"emails": [], "phones": [], "links": []},
  "provenance": {"education[0]": {"page": 1, "lines": [10, 12], "raw_text": ""}},
  "excluded": {"school_level_education": [], "non_paper_publications": []},
  "parsed_dates": {"experience[0]": {"start": "2016-07", "end": "2023-03"}},
  "stage_report": {
    "stage1": {
      "status": "OK", "metrics": {}, "reasons": [],
      "method_used": "per_unit", "methods_tried": [],
      "per_unit": {
        "page_001": {
          "method_used": "pdfplumber_words_tables", "status": "OK",
          "methods_tried": [{"method": "pdfplumber_words_tables", "score": 0.97,
                             "status": "ACCEPTED", "reasons": []}]
        }
      }
    }
  }
}
```
- `resume_id` is `"r_"` plus the first 8 hex characters of the SHA-256 of the file stem, so
  logs never show a file name (which may contain a candidate's name). A PDF and its ground-truth
  file get the same id.
- Flag `section` is one of `personal`, `education`, `experience`, `publications`, `other`,
  `document`.
- Attempt `status` is one of `ACCEPTED`, `BELOW_THRESHOLD`, `NO_OUTPUT`, `NOT_IMPLEMENTED`.
- The main JSON keeps the key order of 5.1. The sidecar, index and run summary use sorted keys.
- Schema: `schema/meta.schema.json`.

Why a separate file: the main JSON stays identical to the ground truth, and the extra information
(flags, provenance, raw text, email and phone) is still available when someone asks
"why was this flagged?" or "where did this value come from?".

### 5.3 Run index

`outputs/run_index.json`: for each resume, its `resume_file`, `resume_id`, `decision` and
`flags_count`, sorted by file name. It is rebuilt from every sidecar in `outputs/` after each run.
The shortlisting system reads the index so it knows which resumes were accepted and which are
flagged. (A flagged resume still has its main JSON, so the recruiter can still see it.)
Schema: `schema/run_index.schema.json`.

### 5.4 Run summary (funnel report)

`reports/run_summary.json`, written after every batch from every sidecar in `outputs/`.
Resume IDs only, sorted keys:
- per stage: how many resumes entered it, and how many ended OK, WARN or FAIL, or stopped there;
  plus `not_implemented`, the number of choices passed as OK only because the methods are not
  built yet (pages in stage 1, sections in stage 5, one per resume elsewhere)
- method usage counts per stage (per unit for stages 1 and 5), and the number of fallbacks used
  (choices where the chosen method was not the first built method)
- the most common reason codes
- flagged resume IDs, each with the section and code of every flag

## 6. Evaluation (offline, uses ground truth)

Two tools in `eval/`, both read-only. They never run the parser and never edit ground truth.
They read from and write to `RESUME_DATA_DIR` only. Console output shows resume IDs and counts,
never names.

**Groups.** `ground_truth_v2/_split.json` lists **dev** (15, used for building) and
**test_frozen** (15, scored once at the end, aggregates only). **tuned** is every other GT
file (the first 10). Entries in the split may be stems, file names or resume IDs (looked up
in `private/id_map.json`). `cli run --groups` uses the same split: the default is
`dev,tuned`; test_frozen is processed only when named, and is then never shown per resume.
PDFs without ground truth are the group `unlabelled`.

**IDs.** Console tables show the pipeline's `resume_id`. Files written by `eval/` (`--json`
results, funnel item files) use a salted `eval_id` = `"e_"` + sha256(salt + stem)[:8]. The
salt is in `private/salt.txt`, and `private/id_map.json` maps each `resume_id` to its
`eval_id` and file name. `private/` never leaves the data folder.

**`eval/evaluate.py`**: scores `outputs/<stem>.json` against `ground_truth_v2/<stem>.json`. Both
are in the same format (5.1), so they are read with the same code. A missing prediction (for
example, a resume that stopped at stage 1) is scored as empty.
- Entry matching is one-to-one (assignment over above-threshold scores). Thresholds:
  education 70, experience 70, publications 85.
  - Education pair score = min(degree+stream, institution). Different degree levels never match.
  - Experience pair score = min(job title, institution).
  - Publications = partial ratio of the titles (full ratio for GT titles under 15 characters).
  - An empty GT component is left out of the pair score.
- Field checks on matched pairs: name (exact) and name without honorifics, case or
  punctuation; education and experience institution (normalised exact; the GT may add a
  location); experience dates (exact as printed); publication year (exact as printed), venue
  (partial ratio >= 85) and type.
- Further rows are reported separately, so the rows above stay comparable with earlier runs:
  degree_level, stream, job title and experience_type.
- Empty GT fields are skipped and counted. "Predicted although GT is empty" is reported, never
  scored. Predicted publication counts are compared with GT `stated_totals`.
- 95% percentile bootstrap CIs over resumes (10,000 samples, fixed seed 0).
- Per-resume rows are shown only for dev and tuned.

**`eval/funnel.py`**: for every GT item, finds the first stage where it is lost. It reads the
stage files saved by `cli run --save-stages` (below) and the predictions, and uses the
evaluator's matching and field checks.

| Step | Question | Score |
|---|---|---|
| S1 text | Are the item's key fields found in the stage 1 lines (fuzzy >= 85; >= 95 also shown)? | Stage 1 recall |
| S4 section | Are they in the right canonical section of the stage 4 document (name -> personal)? | Routing recall |
| S5 entry | Is there a matching predicted entry? | Entry recall |
| S5 fields | Is every checkable field correct? | Field accuracy |
| Completeness | Is a matched entry truncated, and where was it cut (S1, S4 or S5)? | Truncation rate |

- Items lost at S1 get one cause, the first that fits: garbled (the best page is over the
  garbled limit), glued words (found once spaces are removed), table cell (all words are
  there, all in table-cell lines), wrong order (all words there, not together), missing text,
  other. Each also lists the stage 1 methods whose own lines contain it (`recoverable_by`,
  from `stage1_methods.json`). `funnel.py --lost-items` lists them for dev and tuned, showing
  only resume ID, section, index, cause and `recoverable_by`.
- Each lost item is also given the method that handled it:
  - the stage 1 method of the page that matches it best
  - the stage 3 and stage 4 methods
  - or the stage 5 method of its section
- False positives get the first matching cause, from a fixed list: school-level entry, table
  header or skills line, non-paper publication, duplicate, wrapped-line fragment, bleed from
  another section, other.
- Tables are per section, with dev and tuned side by side.
- Per-item and false-positive files (`reports/funnel/`, no resume text) are written for dev and
  tuned only.
- Two parts of the old diagnostic are not carried over:
  - recovery by a second extractor, because stage 1 already tries other methods per page
  - the input-properties table, because stage 0 page profiles are in the sidecar and the run
    summary

**Saved stage files** (`cli run --save-stages`): `reports/stages/<resume_id>/stage1_lines.json`,
`stage1_methods.json` (every stage 1 method's lines per page), `stage3_sections.json`,
`stage4_canonical.json` and `stage5_entities.json`.
- Each holds `{schema_version, resume_id, stage, status, method_used, per_unit, data}`.
- `data` is what the stage passes forward:
  - stage 1: `{page_id: {"method", "lines": [line, ...], "width", "height"}}` (line fields in section 4)
  - stage 3: `{"blocks": [{"heading_raw", "lines"}]}`
  - stage 4: the Canonical Document (section 4)
  - stage 5: the per-unit data
- They hold resume text, so they are written only inside the data folder. Files from an earlier
  run are removed first.

**test_frozen guard.** Neither tool touches test_frozen unless it is given `--test-frozen`.
- Before scoring, a run appends one line to `reports/test_frozen_runs.log`:
  - tool name
  - run counter (that tool's earlier lines plus one; it needs no git)
  - UTC time
  - sha256 of the script and of `eval/common.py`
  - sha256 of the evaluation settings and of `config/thresholds.json`
  - the reason
- A second run of `evaluate.py` is refused unless `--allow-rerun "<reason>"` is given.
- `funnel.py` runs on test_frozen once only, with no override.
- test_frozen results are aggregates only.

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
  config/   section_aliases.json  degrees.json  titles.json  institutions.json  thresholds.json
  schema/   resume.schema.json  meta.schema.json  run_index.schema.json
  src/resume_parser/
     intake.py  pipeline.py  cli.py  schema.py  groups.py   (groups.py: dev / tuned / test_frozen split)
     report.py                                   (run summary, section 5.4)
     extract/  base.py  lines.py  source.py  pdfplumber_extractor.py  pdfminer_extractor.py  ocr_extractor.py
     clean.py  sections.py  normalize.py        (normalize.py builds the Canonical Document)
     entities/ personal.py  education.py  experience.py  publications.py
     field_norm.py  validate.py  scoring.py  writer.py   (writer.py writes main JSON + sidecar + index)
  eval/     common.py  evaluate.py  funnel.py  calibrate_thresholds.py   (common.py: groups, IDs, test_frozen guard)
  fixtures/ synthetic/   (fake resumes, built by a script)
  tests/    one test file per stage, plus determinism
  scripts/  check_no_data.py  make_synthetic_resumes.py
  docs/     design.md
```

## 9. Data (never in the repo)

- A folder on each person's computer, set by environment variable `RESUME_DATA_DIR`:
  `pdfs/`, `ground_truth_v2/` (with `_split.json`), `outputs/`, `review_queue/`, `reports/`
  (with `stages/`, `funnel/` and `test_frozen_runs.log`), `private/` (salt and ID map).
- Back it up somewhere private. Git will not have a copy.
- Guards: `.gitignore`, a pre-commit hook and a CI check (`scripts/check_no_data.py`) that
  refuse PDFs, JSON outside `config/`, `schema/`, `fixtures/` and `docs/`, any file larger than
  1 MB, and banned tool names in file paths or contents. Turn the hook on once per clone with
  `git config core.hooksPath .githooks`.
- Output is written only under `RESUME_DATA_DIR`; the writer refuses any other folder.
- Logs and test output show resume IDs, never names.

## 10. Build order and "done" for each stage

Build one stage at a time. A stage is done when it passes its checks on dev and its tests,
**before** the next stage starts.

| Order | Stage | Done when (proposed, adjust after first measurement) |
|---|---|---|
| 1 | Skeleton, schema, evaluation harness, synthetic fixtures, guards | Tests pass on fake resumes; harness works on synthetic data and loads the real ground truth |
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
