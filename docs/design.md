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
  Only a sidecar and an index entry are written, with the decision FAIL and the reason
  `stage<N>_failed`; the resume is listed in `review_queue/review_queue.json`.
- WARN or FAIL in stages 2 to 7: carry on, and record the flags with the affected section.
  The JSON is still written, so no information is lost. The decision itself comes from stage 6.
- Information-level reason codes are kept in the stage report but are not flags:
  `not_implemented` (a method is not built yet), and `missing_target_section:personal` /
  `missing_target_section:publications` (stage 4 found no such section). A missing education
  or experience section is a WARN `missing_target_section:<name>` instead.

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
  - 0 when more than 1% of the characters are garbled (`(cid:N)` and replacement characters;
    private-use icon glyphs from icon fonts do not count, stage 2 strips them); a page with no
    lines gives no output (`empty_page`);
  - otherwise the word overlap with the other text method: shared words, as a multiset of
    lowercase words (order does not matter), over the larger word count (method 1 and 3 are
    compared with method 2, method 2 with method 1);
  - halved when more than 5% of the words look glued: a run of 25+ letters, or 3+ lowercase
    letters followed by an Uppercase and a lowercase letter; emails, URLs and DOIs are left
    out, and an ignore list (PhD, LaTeX, MATLAB, BibTeX, ...) never counts.
  The separate values (garbled rate, glued rate and count, overlap, words, lines) are stored
  with every attempt. The stage metrics count the pages over the glued-word limit.
- Reading order with columns (`extract/lines.py`, `column_order`, settings in
  `stage1_extract`): lines are read top to bottom, unless the page has a clear vertical gutter.
  A gutter is an empty band at least 12 pt wide, crossed by lines over at most 10% of the
  page's text height, with text on both sides: at least 5 lines per side, covering at least
  30% of the text height, spanning at least 15% of the text width, and with fewer than half of
  them date-only (so a column of right-aligned dates next to one-column text is not a second
  column); a narrow side (under 35% of the text width) needs fewer than a quarter date-only
  lines, as a narrow strip of dates and places beside entries is not a sidebar. Table cells do
  not count. A band found this way is first shrunk to its core, the widest part (still at least
  12 pt) that no line crosses except lines spanning the whole band, and the side checks run on
  that core; so a line that only reaches into the band (text running to the column edge) stays
  in its column, and a sidebar's widest line still counts for the sidebar. The looser warning
  test below does not shrink. Then:
  - lines crossing the gutter are full width (a name, a heading across the page); they stay
    in place and cut the page into bands; in a band the left column is read, then the right;
  - a side narrower than 35% of the text width is a sidebar, read after the main text as one
    block;
  - reordered lines get `column` (full, left, right) and `column_start`; stage 3 starts a new
    block at a `column_start` line, so a sidebar or a column without a heading never joins the
    section before it (stage 4 maps such a block from its content). When the page after a page
    read by columns is not read by columns and starts with no heading, its first block continues
    the main column's last section (the column read first), not the column or sidebar read last:
    stage 3 marks it `continues` that block, and stage 4 gives it the same section.
  A page without a clear gutter keeps its lines exactly as before, with no column fields. Pages read by columns get the note
  `columns_reordered`; pages that only pass a looser gutter test (8 pt, 25%, 3 lines, 15%) get
  `possible_columns`. Both are sidecar flags only: they never change a stage status, a
  decision, a score or the review queue.
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
- Job: split the cleaned lines into blocks, each starting at a heading line, and give each a
  best-guess target name. The block before the first heading is the personal block. Mapping to
  the ideal section names is finished in stage 4.
- Heading signals (settings in `thresholds.json`, `stage3_sections`, to calibrate):
  - vocabulary: the whole normalised line is an alias in `config/section_aliases.json`, or has
    `fuzz.ratio` at least 90 with one (misspelt headings).
  - table cells: a cell is a heading only if it is an exact alias of a target section
    (education, experience, publications) and the heading of its row: the leftmost non-empty
    cell, with the rest of the row empty or holding real content (a digit, or more than 3
    words). A row of short column-header words (Course, Board, Year) is a table header, never
    a section heading. Cells are never matched fuzzily.
  - inline headings: a line that starts with a target-section alias followed by a colon or a
    spaced dash ("Experience: 25 years ...") is split into a heading line and a content line;
    the content line is never a heading candidate. Aliases of other sections never split
    inline ("Research interests: ..." inside an entry is a field, not a section).
  - look: a short line (at most 8 words, 60 characters, no full stop, not a table cell, not a
    date, degree or citation line) with at least 2 of: font at least 1.15 x body size, bold,
    all capitals, white space above; one of them must be the larger font or the capitals, so a
    bold job title at body size is not a heading. Never in the first 3 lines of page 1 (the
    name).
  - content: citation patterns (publications), degree words (education), date ranges
    (experience).
- Methods: `heading_vocabulary` (vocabulary headings only); `heading_look` (plus look headings);
  `content_patterns` (as `heading_look`, and blocks with an unknown heading get a guess from
  their content).
- Score: 0 when no education, experience or publications block is found; otherwise 1 - the
  share of target-looking lines where they do not belong (a citation outside publications, a
  degree line outside education, a date range outside education, experience and publications;
  the first 5 lines of the personal block are not counted). `heading_vocabulary` scores half
  while unknown heading-like lines remain.
- Unknown heading-like lines are counted per resume; with `--save-stages` their text goes to
  `reports/stage3_unknown_headings.txt` in the data folder (never printed, never test_frozen)
  for adding aliases by hand.
- Gate: WARN `no_target_section` if no target section is found.

### Stage 4: Normalization layer (the standard form)

- Job: turn the cleaned lines plus the section boundaries from stage 3 into one
  **Canonical Document**. This is the only thing stage 5 reads.
- **Canonical section names** (the ideal names): `personal`, `education`, `experience`,
  `publications`, `other`. Every heading variant is mapped to one of these using
  `config/section_aliases.json` (many aliases -> one canonical name; `other` is split into
  labels such as `skills`, `awards`, `presentations` that become `original_name`). The map
  merges the old repo's heading map with a starter list; job titles and school-level labels
  (12th, HSC) are entry content, never headings. A separate `exact_only` list holds common
  academic CV headings for the four target sections (from general knowledge, not from resumes);
  they match only exactly after normalising, never by fuzzy match, and never override an alias
  that is already listed.
  Sections that are not one of the four are kept under `other` with their original label
  (for example `skills`, `awards`) so nothing is lost.
- **Mapping rule for experience** (decide it once, in the config, with comments): only headings
  that list positions held (Work Experience, Employment, Academic Positions, Postdoctoral
  Positions) map to `experience`. Headings that list courses taught, responsibilities or
  projects map to `other` (including "Project Experience"). This matches the ground-truth
  labelling guide ("jobs only"). Likewise papers presented, presentations, talks and posters map
  to `other`; book chapters stay in `publications`.
- **Standard form of the lines:** one line per logical line, bullets and icons removed, wrapped
  lines joined, spaces and unicode normalised, original text otherwise untouched (dates stay as
  printed). Each line keeps its id, page and position, so provenance survives.
- Methods, chosen per block: `exact_alias_map`; `fuzzy_alias_match` (`fuzz.ratio` of the
  whole normalised heading against every alias, at least `stage4_normalize.fuzzy_min` = 90);
  `content_guess` (the block's dominant content by share of lines; when no kind reaches
  `content_min_share`, the lines are joined into wrapped entries as the publication splitters do,
  and the block becomes publications when at least `citation_entry_min_share` = 0.6 of the
  entries have a plausible year plus an author-list start and are at most `max_citation_chars`
  long, unless its heading is an alias of another section; always WARN `section_from_content`);
  `keep_as_other_with_warning` (the section is `other` with the original label and a WARN).
- Checks: every cleaned line is in exactly one section (or `unassigned`); line order is kept;
  joining the canonical lines gives back the cleaned text (no loss, no duplication);
  all section names are from the allowed list; no target-looking lines (dates, degrees,
  citations) sit in `other` or `unassigned` beyond a small limit.
- Gate: WARN `target_lines_in_other` when more than 10% of target-looking lines sit in
  `other` or `unassigned`; WARN `lines_lost_or_duplicated` if the line check fails;
  `missing_target_section:<name>` for a missing target section (WARN for education and
  experience, information only for personal and publications).

Canonical Document (sketch; each section also has `block_id` and `heading_line`):
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

**Personal** (built). Name methods, in order: `top_lines` (every personal section in order,
then the top lines of page 1, until a valid name is found; each line is cut at the first email,
phone, URL, pipe, bullet or contact word, and then at the first comma or a dash standing between
spaces ("Name, Ph.D." keeps the name; "Surname, Firstname" leaves a one-word surname, which is
never a name), unless the part after that cut holds a digit, an institution, an organisation or
an address word, in which case the line is still rejected; a "Name:" label, honorifics such as
Dr., Prof., Shri, Smt. and trailing degrees are removed, as in the ground truth, where no name
keeps an honorific; what is left is never a name when it holds an institution word or known
institution, an organisation word such as department or faculty, a section-heading alias, an
address word, a degree word, a digit, or only CV words (leading initials followed by a
capitalised word, as in "M. A. Khan", are not a degree on a line that is bold or in the largest
font of its page; on other lines they still are); a name is 2 to 5 words, initials with
or without dots, in title case or capitals, or in any case when the line is bold or has the
largest font on page 1 among the lines that pass the other rules; a name split over two short
one-word lines is joined),
`from_email` (the email's local part, 2+ alphabetic tokens; always WARN `name_from_email`),
`from_file_name` (always WARN `name_from_file_name`). Emails, phones (as printed and as digits)
and links (ORCID, Scholar, LinkedIn, web pages) go only to the sidecar `personal_extra`.
Check: 0.5 for a valid name (2 to 5 words, letters only) + 0.5 for an email or phone.

**Education** (built). All education sections are read in order; a section boundary ends an
entry. Methods: `anchor_split` (an entry starts at a degree word, a school-level word or a year
range at the start of a row; in institution-first sections at institution lines; in a year-last
section, where the first content line is a degree line, the last is date-only and at least half
of the date-only lines follow a line that is neither a degree nor date-only, a date-only line
closes the entry above it instead; not used when most lines are table cells), `table_row_split` (one table row is one entry; a header row of
column words such as degree, board, year is dropped and tells which column is which),
`paragraph_split` (vertical gaps), `whole_section_one_entry` (always WARN). Short bold or
capitalised lines without a degree, year or institution are sub-headings and skipped;
continuation lines (thesis, advisor, supervisor, CGPA, specialisation, ...) join the previous
entry's raw text. Fields as printed: degree (longest form in `config/degrees.json`),
`degree_level` (M.Phil is masters, as in the ground truth; diplomas are other), stream (after
"in", a dash, a colon or in parentheses; or capitalised subject words right after the degree,
left empty when more capitalised words follow before the next comma, "from"/"at", year or
institution word; in a table with no header row, the cell right after the degree cell when it is
a subject phrase), institution (the segment with a keyword or a name from
the `config/institutions.json` gazetteer). Year, grade, raw text and provenance (page, line ids,
method) go to the sidecar; school-level entries to `excluded.school_level_education`. Check:
min(share of entries with a degree or institution, plausible years 1950 to current year + 6 and
no header text; coverage of the section's lines), 0 without a higher-education entry.

**Experience** (built). All experience sections are read in order (jobs only: courses taught,
responsibilities, projects and talks are already in `other`); a section boundary ends an entry.
Continuation lines (a continuation word such as responsible, worked, courses taught at the start
of the line, or a bullet with no date and no job title) join the previous entry's raw text and
never start an entry. A job title counts at the start of a line or segment, after a lead-in
("working as", "joined as", "currently", "position:", a bullet or a colon), or after other words
when the same or the next line has dates or an institution; such a line is an entry line even
when it starts with a continuation word. A title after "under", "supervised by", "reporting to",
"guide:", ..., or right before an honorific, is someone else's; a mid-line title right before a
capitalised name is too. Titles may have up to two prefixes (Visiting, Adjunct, Guest, Ad-hoc,
...), kept as printed; "Head of ..." runs to the end of its segment. After splitting, an entry
with no title but an institution or dates is joined to the entry right before it when that one
has a title and lacks that slot (never across a section boundary).
Methods: `date_anchor_split` (an entry starts at a line with dates or a job title from
`config/titles.json`; a line starts a new entry only when it would fill a slot the current entry
already has, a second title, second dates or a second institution-only line, so title,
institution and dates written on separate lines in any order stay one entry; an entry with
dates only then joins the next entry when that one has a title and no dates, else the entry
before, never across a section boundary; not used when most lines are table
cells), `table_row_split` (as education: one row is one entry, a header row of words such as
designation, organisation, duration is dropped and tells which column is which),
`paragraph_split`, `whole_section_one_entry` (always WARN). An entry made only of lines from a
block that stage 4 routed by `content_guess` is kept only with a job title and an institution or
dates. Fields as printed: job_title (longest title, plus a bracket part written right after it;
"of ..." or "in ..." is not added, as in the ground truth), institution (the first segment that is
a department or holds an institution word; when it is a department, school, faculty or centre the
following institution, department and short place segments of the line are kept too, otherwise
only the institution segment; a bare IIT, NIT, IIIT, IIM, IISER or BITS keeps the place after it),
dates (the first range: month names, 4- or 2-digit years, dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy,
"Month day, year", "day Month year", dash, en-dash, "to", "till", and
"Present", "till date", ... kept as printed; else a single date), `experience_type`. Parsed start
and end go to the sidecar `parsed_dates` ("Present" gives end null); raw text and provenance (with
`no_dates`) to the sidecar. When a field is not clear it stays empty. Check: min(share of entries
with a title or an institution, plausible years, end not before start and no header text;
coverage), 0 when the section has lines but no entry; entries without dates are flagged
`no_dates`, overlapping ranges are counted, neither is scored.

**Publications** (built). All publications sections are read in order (talks, posters and
presentations are already in `other`); a section boundary ends an entry. Short heading lines
inside a section (Journal Papers, Conference Proceedings, Book Chapters, ...) are sub-headings:
never entries, they set the type of the entries under them. Lines with a printed total ("Total
publications: 65", "12 journal papers", "Conference papers: 4") fill `stated_totals` and never
create entries. Methods: `marker_split` (an entry starts at a number or bullet, after a
year-only line, or, since stage 2 removes bullet characters, at a line that follows a full stop
and starts like an author list; other lines are wrapped parts, also across pages),
`hanging_indent_split` (an entry starts at the left margin of its section and page; indented
lines are wrapped parts), `paragraph_split`, `whole_section_one_entry` (always WARN). Fields as
printed: title (quoted text, else the text after the author list up to the end of that sentence
or the comma before the venue; a single author written as a full name, "Firstname Lastname
(2022). Title", counts as the author list only when no other rule finds a title and at least
`full_name_author_min_entries` or `full_name_author_min_share` of the section's entries start that
way), venue (the first segment after the title with a journal,
conference or publisher word, else the first that is not volume, pages, year or DOI; when that
gives nothing, the segment right after the title if it has at most 8 words and is not volume,
pages, year, DOI or an author list; otherwise empty; a leading
"In"/"In:" removed, trailing volume and pages cut), year (the last plausible 4-digit year), type
(sub-heading, then a journal or conference word in the entry's own venue, including J. before an
abbreviated word, Trans., Proc., Conf., Symp.; then the section heading, which also decides when
the venue has both kinds of word or only weak words (Proceedings, Review, Letters, Bulletin,
Annals, Magazine, Congress: `weak_type_words`, used from the venue only without a heading type);
then other cues such as arXiv, preprint, thesis, patent, book chapter, "in press" with no venue,
submitted; then conference cues such as "In"/"In:"/"Proc" before the venue, an all-capitals
acronym venue next to a year, "Int. Conf", "Annual Meeting"; otherwise journal with a venue and
other without one, recorded as `type_source` "default"). Authors, volume, pages, DOI, links, raw
text, provenance and `type_source` go to the sidecar. When a field is not clear it stays empty.
Check: min(share of entries with a title of 3+ words, a plausible year, not a heading or an
author list alone, not two citations run together and not longer than `max_citation_chars`;
coverage), 0 when the section has lines but no entry; the count against a printed total is
reported, never scored. Words live in `config/publications.json`.

**Wrapped lines in narrow columns** (`entities/wraps.py`, before the methods run): on pages that
stage 1 reordered by columns, a line that continues the line above in the same column (a date
part, the end of an institution, a venue) is joined to it, in a copy of the stage 4 document,
inside education, experience and publications only. Both lines must carry column left or right,
the same column and page, no table cell, a gap of at most `column_wrap_gap_factor` times the
column's normal line gap, no outdent and no list marker; and the line above must end unfinished
(comma, dash, open bracket, "&" or a joining word such as "of"), or the next line must be a
closing part (lower case start, a dash, only a year or year range, pages, a volume) or, in
publications, start with a venue lead (In, Proc., Proceedings, Journal, Trans.). Lines without a
column field are never touched. The joined line keeps the first id and lists the others in
`joined_ids`, as in stage 2.

**Methods for entry splitting, chosen per section:** `anchor_split`, `table_row_split`,
`paragraph_split`, then `whole_section_one_entry` (always last, always WARN). Built for
education; experience starts with `date_anchor_split` and publications with `marker_split` and
`hanging_indent_split` instead of `anchor_split`.
**Methods for the name:** `top_lines`, `from_email`, `from_file_name` (lowest confidence, flagged).
Accept thresholds per section are in `config/thresholds.json`.

**Field normalization** (the values that the ground-truth format needs):
- `degree_level` (doctorate / masters / bachelors / other) from `config/degrees.json`.
- `experience_type` (academic / industry / other), from the institution: a company ending (Pvt,
  Ltd, Inc, LLC, Corp) -> industry; an academic keyword or a gazetteer name -> academic; another
  industry word (Technologies, Labs, Solutions, ...) -> industry; otherwise other.
- Publication `type` (journal / conference / other) from the author's heading, then venue words.
- Strings such as degree, institution, job title and dates stay **as printed** in the CV,
  as in the ground truth. Parsed dates and cleaned names go to the sidecar file, not the main JSON.

**Per-field confidence** is rule-based (which rules fired). Every field has `source` (page, lines),
stored in the sidecar file.

**Checks:**
- Personal: name found; email or phone found.
- Education: at least one higher-education entry if an education section exists; each entry has
  a degree or an institution; years plausible (1950 to current year + 6, `current_year` set in
  `thresholds.json` so runs are reproducible); no table-header text; coverage of the lines.
- Experience: each entry has a date range or a title; end not before start; no entry is a
  fragment (very short text).
- Publications: entry length sensible; most entries have a year; count vs stated total
  ("Total: 65") within 10% when a total is printed; duplicates removed count reported.
- Coverage (all sections): every line of the section belongs to an entry or is marked noise.
  Leftover lines are the main sign of missed content.

### Stage 6: Validation and scoring (built)
- Section scores, label-free: the check score of the method stage 5 chose for the name (the
  personal unit), education, experience and publications; None for a section stage 4 did not
  find, 0.0 for a found section whose methods gave no output (`scoring.py`).
- Decision from the four critical sections, as reason codes (`validate.py`; `<s>` is name,
  education, experience or publications):
  - **FAIL**: stage 0 or 1 failed (`stage0_failed`, `stage1_failed`; set when the sidecar is
    built, because stage 6 does not run then), or the name is empty (`empty_name`; the main
    JSON is still written)
  - **NEEDS_REVIEW**: `missing_section:<s>` (not for the name), `low_score:<s>` (below
    `section_score_threshold`), `whole_section_one_entry:<s>`, `name_from_email`,
    `name_from_file_name`, `name_is_job_title` (every word of the name is a job-title word from
    `config/titles.json`, joining words such as "of" ignored; label-free),
    `no_entries:<s>` (the section has lines but no entry; never silent);
    also `main_json_invalid` from stage 7
  - **ACCEPT** otherwise. Flags from earlier stages stay in the sidecar but do not decide alone.
- Stage 6 is OK for ACCEPT, WARN for NEEDS_REVIEW, FAIL for FAIL; its reasons become flags.
- `section_score_threshold` in `config/thresholds.json` is chosen by
  `eval/calibrate_thresholds.py` (section 7). `accept_threshold_by_section` (stage 5 method
  choice) is a separate setting and is not changed by the calibration.

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
- After each run: `outputs/run_index.json` and `review_queue/review_queue.json` (5.3).
- Determinism: `python -m resume_parser.cli check-determinism [--groups]` runs the selected
  PDFs twice into `reports/determinism/a` and `b` and compares every output byte for byte
  (main JSON, sidecar, index, review queue); it prints counts and the resume IDs that differ.
  test_frozen is refused.

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
  "decision_reasons": [],
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
- Attempt `status` is one of `ACCEPTED`, `BELOW_THRESHOLD`, `NO_OUTPUT`, `NOT_IMPLEMENTED`, `SKIPPED`.
- Provenance `lines` are line ids (`p001_l0007`); education provenance also has `year`, `grade`
  and `method`; experience provenance has `method` and `no_dates`; publication provenance has
  `method`, `type_source`, `authors`, `volume`, `pages`, `doi` and `urls`. `personal_extra` also has `phone_digits` (phones as digits, for matching).
- The main JSON keeps the key order of 5.1. The sidecar, index and run summary use sorted keys.
- Schema: `schema/meta.schema.json`.

Why a separate file: the main JSON stays identical to the ground truth, and the extra information
(flags, provenance, raw text, email and phone) is still available when someone asks
"why was this flagged?" or "where did this value come from?".

### 5.3 Run index

`outputs/run_index.json`: for each resume, its `resume_file`, `resume_id`, `decision`
(ACCEPT, NEEDS_REVIEW or FAIL), `reasons` (the stage 6 reason codes) and `flags_count`, sorted
by file name. It is rebuilt from every sidecar in `outputs/` after each run.
The shortlisting system reads the index so it knows which resumes were accepted and which are
flagged. (A flagged resume still has its main JSON, so the recruiter can still see it.)
Schema: `schema/run_index.schema.json`.

`review_queue/review_queue.json`: every resume that is not ACCEPT, as `resume_id`, `decision` and
`reasons`, sorted by resume ID; IDs only, never file names. Rebuilt from the index after each
run. Schema: `schema/review_queue.schema.json`.

### 5.4 Run summary (funnel report)

`reports/run_summary.json`, written after every batch from every sidecar in `outputs/`.
Resume IDs only, sorted keys:
- decisions: how many resumes are ACCEPT, NEEDS_REVIEW and FAIL
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

**Groups.** `ground_truth_v2/_split.json` lists **dev** (15), **dev2** (5) and **validation**
(10); **tuned** is every other GT file (the first 10). dev, tuned and dev2 are development
groups: per-resume results are allowed. The original 15 test_frozen resumes were scored once
(aggregates only, logged in `reports/test_frozen_runs.log`) and then re-split with seed 0 into
validation (10) and dev2 (5); the old split is kept as `_split_before_validation.json`. Because
of that one earlier aggregate run, validation is not a completely unseen holdout.
`test_frozen` stays a supported (now empty) group with its own guard. Entries in the split may
be stems, file names or resume IDs (looked up in `private/id_map.json`). `cli run --groups`
uses the same split: the default is `dev,tuned,dev2`; validation and test_frozen are processed
only when named, and are then shown as counts only and never put in the unknown-headings file.
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
  degree_level, stream, job title, experience_type, and experience dates once the spaces around
  dashes are removed (`exp_dates_spacing`, a reading aid; the exact `exp_dates` stays the one
  that counts, and the funnel uses it).
- Empty GT fields are skipped and counted. "Predicted although GT is empty" is reported, never
  scored. Predicted publication counts are compared with GT `stated_totals`.
- 95% percentile bootstrap CIs over resumes (10,000 samples, fixed seed 0).
- Per-resume rows are shown only for the development groups (dev, tuned, dev2).

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

- Items lost at S5 get one cause: not extracted, merged into another entry, split into
  fragments, or wrong fields (with the field names for a matched entry). Education false
  positives are grouped as school-level, header row, bleed from another section, split
  fragment, other. The stage 5 method per section is counted per group.
- Items lost at S1 get one cause, the first that fits: garbled (the best page is over the
  garbled limit), glued words (found once spaces are removed), table cell (all words are
  there, all in table-cell lines), wrong order (all words there, not together), missing text,
  other. Each also lists the stage 1 methods whose own lines contain it (`recoverable_by`,
  from `stage1_methods.json`). `funnel.py --lost-items` lists them for dev and tuned, showing
  only resume ID, section, index, cause and `recoverable_by`.
- `funnel.py --lost-items --stage S4` (or `S5`) lists those losses the same way, with the
  method that handled the item instead of `recoverable_by`.
- **Check validity** (dev and tuned only): for each label-free check that fired (every stage
  reason code, and each stage's WARN/FAIL), how many resumes it fired on, how many of those
  had a real loss at the step the stage feeds (stages 0-1: S1; 2-4: S4; 5: S5;
  `missing_target_section:<name>`: that section's S4 losses), and how many resumes with such a
  loss it missed. This shows which checks to trust before calibrating them.
- Each lost item is also given the method that handled it:
  - the stage 1 method of the page that matches it best
  - the stage 3 method and the stage 4 method of the block that holds it
  - or the stage 5 method of its section
- False positives get the first matching cause, from a fixed list: school-level entry, table
  header or skills line, non-paper publication, duplicate, wrapped-line fragment, bleed from
  another section, other. Education and experience are printed in groups (header row, bleed,
  split fragment, other; plus school-level for education and content-routed for experience: an
  "other" entry whose lines all come from content_guess blocks). Publications are printed as
  header or sub-heading, non-paper, bleed, split fragment, citation-like (not in GT), other.
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

**validation guard.** Neither tool touches validation unless it is given `--validation`.
- Every run appends one line to `reports/validation_runs.log` (tool, run counter per tool, UTC
  time, the same hashes as below) and is never refused.
- From the 4th run of a tool on, the log line and every report of that run carry the warning
  "validation has been used N times; results are no longer a fair estimate".
- validation results are aggregates only: no per-resume rows, no per-item or false-positive
  files, never in `--lost-items`, loss rankings, check validity, `end_to_end.py`,
  `calibrate_thresholds.py` or `cli check-determinism`.
- `scripts/check_no_data.py` (with `RESUME_DATA_DIR` set) refuses any repo file that holds a
  resume ID or file stem of validation or test_frozen, without printing it.

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

**The final set (never tuned on).** Ten fresh resumes, kept apart from everything used to build
the parser, for one last honest estimate. Nothing (no rule, threshold, alias, method order or
learned model) is ever chosen, changed or tuned on it, and it is never read during development.
- It lives outside `RESUME_DATA_DIR` and outside the repo, in three folders named by environment
  variables: `RESUME_FINAL_PDFS` (only `.pdf` files directly inside it are read),
  `RESUME_FINAL_GT` (only `.json` files directly inside it, GT v2, each named like its PDF) and
  `RESUME_FINAL_OUT` (`outputs/` with the main JSON, sidecars and `run_index.json`, `stages/`,
  an aggregate `run_summary.json` with no list of resumes, and `final_runs.log`). Each folder
  inside the repo or inside (or above) `RESUME_DATA_DIR` is refused. Resume IDs are the usual
  ones; the private salt and ID map stay in `RESUME_DATA_DIR/private`.
- Commands: `python -m resume_parser.cli validate-final` (counts of PDFs and GT files, GT files
  whose `resume_file` names no PDF or that are not named like their PDF, GT files failing the GT
  v2 schema; resume IDs only, never content; it does not run the pipeline and is not logged);
  `python -m resume_parser.cli run --groups final`; `python eval/evaluate.py --final`;
  `python eval/funnel.py --final`.
- Guard, the strictest one: aggregates only (no per-resume rows, no loss ranking, no per-item
  file); every run of those three tools appends a line to `final_runs.log` before it starts
  (tool, run counter, UTC time, sha256 of the script, of the package code, of the settings and
  of `config/thresholds.json`, and the rerun reason); a second run of a tool is refused unless
  `--allow-rerun "<reason>"` is given, and the reason is logged.
- Final runs only on its own: it can not be combined with another group, and it is never part
  of the unknown-headings list, the review queue, threshold calibration, `check-determinism`,
  `end_to_end.py` or any per-resume output or ranking.
- `scripts/check_no_data.py` refuses any repo file holding a final-set stem or resume ID when
  `RESUME_FINAL_GT` or `RESUME_FINAL_PDFS` is set.

**The fresh set (once, ever; `eval/fresh.py`).** A second held-out set of fresh resumes with the
final set's protections and a stricter guard. Nothing is ever chosen, changed or tuned on it.
- Four folders outside the repo and outside `RESUME_DATA_DIR`, named by environment variables:
  `RESUME_FRESH_PDFS` (only `.pdf` files directly inside), `RESUME_FRESH_GT` (only `.json` files
  directly inside, GT v2; it may sit inside the PDF folder), `RESUME_FRESH_BACKUP` (must exist,
  never read) and `RESUME_FRESH_OUT` (`fresh_runs.log`, `fresh_report.txt`, `fresh_report.json`
  and nothing else). The backup and output folders may not overlap any other fresh folder, and no
  fresh folder may overlap a final-set folder.
- `python eval/fresh.py check`: layout only, the pipeline is not run. Folder rules; PDF and GT
  counts; each GT file is paired with the PDF its `resume_file` names (counts of PDFs with no GT,
  PDFs named by several GT files, GT files with no PDF); `resume_file` values not ending in `.pdf`
  (pattern only); GT files failing the GT v2 schema; repo files naming a fresh stem or resume ID;
  PDFs byte-identical to one in `RESUME_DATA_DIR/pdfs` or the final set; runs logged. PASS or
  FAIL and counts only, never a value, stem, ID or path, also on failure.
- `python eval/fresh.py run --commit <hash> [--parser-commit <hash>]`: refused unless HEAD is
  the commit named, the tree is clean (untracked files included), `src/`, `config/` and
  `schema/` are unchanged since `--parser-commit` (when given), the layout check passes and no
  run is logged in `RESUME_FRESH_OUT/fresh_runs.log` or its mirror
  `RESUME_DATA_DIR/reports/fresh_runs.log`. The log line (commit, tree state, parser commit,
  hashes of the package, scripts and thresholds) is written to both before any work, so a
  crashed run counts. There is no override: the set runs once, ever. Git is only read.
- The run is in memory: no main JSON, sidecar, stage file, index or per-item file is written.
  Predictions and stage records are built exactly as the writer would write them (schema checked,
  JSON round trip) and scored with `evaluate.py` and `funnel.py` (a test checks equality with the
  on-disk final path). Report: P/R/F1 with 95% CIs, field accuracies, name exact and normalised,
  the funnel tables, decision counts, the wrong items (lost GT items, false-positive entries)
  that fall in flagged resumes (NEEDS_REVIEW or FAIL), and the aggregate check-validity table.
  Output of the parser is swallowed during the run; an exception is reported as its class name
  and a count, never its message or a traceback.
- `python eval/fresh.py rehearse [--seeds 1000-1009]`: dress rehearsal of check and run on
  synthetic sweep CVs with their generator GT, with temporary folders for the four fresh folders
  and for `RESUME_DATA_DIR`; the git checks are shown, not enforced; the temporary folder is
  removed afterwards.
- `scripts/check_no_data.py` also refuses fresh-set stems and resume IDs when `RESUME_FRESH_GT`
  or `RESUME_FRESH_PDFS` is set.

## 7. Choosing thresholds

In real use there is no ground truth, so decisions must come from label-free checks. To choose
thresholds:
1. Run the pipeline on dev. For each resume, compute the label-free scores.
2. Using ground truth, mark each resume's section as "really correct" or "has errors".
3. Pick the threshold so that sections with errors are caught (high recall) while few correct
   ones are flagged. Plot "accuracy of accepted resumes" against "share sent to review".
4. Freeze the thresholds, then score test_frozen once.

Report: of the accepted resumes, how many are fully correct; and what share go to review.

**`eval/calibrate_thresholds.py`** (dev+tuned only; `--write` writes `section_score_threshold`):
- Real loss per resume and section, used for the choice: recall or precision below 0.8 (name:
  `name_norm` wrong or empty). Tables for reading only: F1 below 1, and any item lost at any
  funnel step (field errors included).
- A section is flagged when its score is below the threshold or the section is missing. Per
  candidate threshold (observed scores, 0, 1 and "flag all", which is shown but never chosen):
  flagged share, catch rate, false alarms.
- Choice: the smallest flagged share with a catch rate of at least 90%; where none reaches it,
  the best catch rate, and the report says so. Leave-one-resume-out shows how stable the choice
  is. 25 resumes is a small sample.

**`eval/end_to_end.py`** (development groups): name, education, experience and publications P/R/F1
with 95% CIs (name: P = right names / names given, R = right names / GT names); the funnel per
section; items lost by step; the check-validity table for every stage; the decisions and, per
decision, how many resumes have a real loss. The thresholds were chosen on the same resumes, so
that last table is optimistic.

**`eval/folds.py`** (rotating hold-out, development groups only): the development resumes are
cut into 3 fixed folds (sorted by resume ID, shuffled with `random.Random(0)`); the folds are
stored as resume IDs in `RESUME_DATA_DIR/reports/folds/folds.json`, never in the repo. Per fold:
P/R/F1 with 95% CIs per section, name accuracy, the spread across folds, the resumes whose removal
raises the fold's F1 most, and the experience and education funnel. The rules were tuned on all of
these resumes, so these are not held-out scores; they show which sections vary most and which
resumes pull a fold down.

**Layout tests** (`tests/test_layouts.py`, invented content only): one column, two columns, a
left sidebar and tables, built at test time, each checked at S1 (reading order), S4 (section) and
S5 (entries). A layout that fails today is marked `xfail(strict=False)` with the failing stage.
Two more layouts (two columns and a sidebar) use different invented content and let the PDF
library wrap the text at the column width, so the wrapped-line rule is checked on breaks it was
not written against; their failures are recorded as found, with no rule added for them.

**Learned publication-field reader (experimental, not in the pipeline).**
`src/resume_parser/learn/pub_reader.py` reads title, venue, year and type from a stage 5
publication entry (the splitting stays rule-based) with scikit-learn (optional `learn` extra):
a token tagger (DictVectorizer + logistic regression over AUTHOR, TITLE, VENUE, YEAR, OTHER) and
an entry type classifier. Features are shapes only (token shape, length, position, neighbour
shapes, punctuation counts, quote and bracket flags, bold, membership flags from
`config/publications.json`), never words, so a model holds no personal data. Weak labels come
from GT publications the rules match (GT title and venue found as token spans by fuzzy match, GT
year as equal tokens). `eval/pub_reader_loo.py` compares rules, learned and "learned when its
confidence is at least t, else rules" with leave-one-resume-out over the development groups,
judged with evaluate.py's rules. Models are written only to `RESUME_DATA_DIR/models/`.

**Tier lookups for shortlisting (`src/shortlisting/tiers/`, rule-based, no learned or external
service at run time).** The lexicons are public lists kept outside the repo, in
`RESUME_LEXICON_DIR` (default `RESUME_DATA_DIR/Lexicons`; a folder inside the repo is refused): the
SCImago journal list (`scimagojr 2025.csv`) and the ICORE conference export (`core/ICORE2026.csv`).
The repo holds only the loaders, the rules, `config/tiers.json`, the field mapping and small
hand-written seed lists.
- Venue tier: A = SCImago Q1 or ICORE A*/A, B = Q2 or ICORE B, C = Q3/Q4 or ICORE C; anything
  else is unknown, which is neutral (never low). Journal tiers only for entries typed journal
  (SCImago rows typed journal, trade journal, book series), ICORE only for entries typed
  conference, nothing for other types.
- Matching: normalisation (ASCII, case, punctuation, "&", "the"), abbreviation expansion
  (`config/venue_abbreviations.json`, ISO 4 style), lookup on content words (stop words dropped),
  then ISSN, exact title, acronym (`config/venue_acronyms.json`; for conferences also ICORE's own
  acronyms; a standalone token with two or more capitals), then fuzzy (rapidfuzz
  token_sort_ratio at least 90). On SCImago 2025 the most similar other title of a journal scores
  80 at the median, 90 at p90 and 96 at p99 (Physical Review A vs B: 94), so a score alone is not
  safe: the token guard requires every content token to align one to one (equal, abbreviation
  prefix, or a per-token ratio of 85 or more for long tokens; single letters and numbers equal
  only), a fuzzy match needs a 3-point lead over the next guarded candidate, and names under 15
  characters or 3 content words are never fuzzy-matched. Conference names lose years, ordinals,
  "proceedings" and publisher words; an acronym ICORE lists twice is resolved by shared title
  words; a workshop, companion, tutorial, poster or demo never takes its main conference's rank.
  When the whole string finds nothing, each comma-separated part of two or more content words is
  tried ("Name, City, Country"). A journal that still finds nothing gets three more rules, in
  this order, each refused when it fits more than one title, and the method names the rule:
  prefix (a dotted abbreviation the table does not resolve, such as "Hydrodyn.", stands for a
  title word it begins, 3+ characters; same number of content words, in order, all others equal),
  colon (the venue is the main title before a colon of exactly one journal) and publisher
  (Elsevier, Springer, Wiley and similar words dropped when 3+ content words remain, then the usual
  methods). They never change a journal matched before them. Not adopted, after an in-memory
  trial on the development GT: a prefix on unabbreviated words, singular/plural matching, and
  case-insensitive conference acronyms.
- Role field (`config/venue_fields.json`, 11 DAU areas, every name checked against the SCImago
  list at load): the best quartile among the journal's categories in the field; when none is in
  the field, the best overall quartile, marked `out_of_field`; with no field, the best overall.
- Output per venue: tier, source, matched title, match score, method, categories, field
  categories, out_of_field, quartile or ICORE rank, certainty (high: ISSN, exact, acronym; medium:
  fuzzy; none) and a reason code for every unknown.
- `eval/tier_coverage.py`: development GT only, counts only (share of venues with a tier, the
  tier, method, certainty and reason distributions); the strings with no tier go to
  `RESUME_DATA_DIR/reports/tiers/unmatched_venues.txt`, never printed. ICORE covers computer
  science conferences only, so national and non-CS conferences stay unknown.
- Institution tier (`institutions.py`), Indian institutions: the NIRF 2025 ranking pages saved as
  HTML in `Lexicons/nirf_2025/` (a ranked page per category plus its rank-band pages; the "ALL"
  pages list participants, not ranks, and are never read). Included categories (`config/tiers.json`):
  Overall, University, College, Research, Engineering, Management, Pharmacy, Medical, Dental, Law,
  Architecture, Agriculture; Innovation, SDG, Open, Skill and State Public University rank within
  a narrow group and stay off unless switched on. One institution is joined across categories by
  its NIRF ID (the part after the category letter); a band row joins the same name and state.
  Tier = the best rank over its counted categories, a band counting at its lower bound: 1-25
  Tier 1, 26-100 Tier 2, 101-200 Tier 3, else unlisted. Field-aware: Overall, University, College
  and Research always count; a discipline category (Engineering, Management, Pharmacy, Medical,
  Dental, Law, Architecture, Agriculture) counts only when the recruiter's field maps to it
  (`config/tiers.json` nirf.field_categories: the five computing fields -> Engineering, Economics
  -> Management, Law -> Law, Humanities, Social Sciences and Design -> Architecture, Agriculture
  Analytics -> Agriculture, Physics and Mathematical Sciences and Interdisciplinary -> none). With
  no field, only the four general categories count, so a top-25 Dental rank never makes a college
  Tier 1 for a computing role. The result names the category that gave the best rank, the counted
  categories, and every category with its rank and whether it counted; an institution ranked only
  in categories that do not count is unlisted (`no_rank_in_counted_categories`).
- NIRF names checked by hand: Dhirubhai Ambani University is listed under its former name
  (Dhirubhai Ambani Institute of Information and Communication Technology, Engineering 201-300),
  so DA-IICT, DAU and the new name are aliases of it; by the NIRF rule alone it is unlisted (no
  general rank, and its Engineering band is beyond 200).
- Editorial overrides (`config/institution_overrides.json`, optional): an institution named there
  takes the tier written there for every field, before NIRF and the seed list, and every result
  it gives says source, method and reason "editorial override". The only entry is Dhirubhai
  Ambani University (with DAU and DA-IICT), Tier 2; deleting the file returns it to the NIRF rule.
  There are no overrides for SRM or IACS. NIRF writes "S.R.M. Institute of Science
  and Technology" (aliases SRMIST, SRM Institute of Science and Technology; not "SRM University"
  alone, which also names other universities). The Indian Association for the Cultivation of
  Science is on none of the saved pages and stays unlisted.
- Other institutions: `config/institution_global_seed.json`, an editorial list of about 100
  widely recognised non-Indian universities and schools (technical, economics, law, design), all
  Tier 1. It is a seed for review, not a ranking, and not copied from any ranking. Every other
  institution is unlisted. Unlisted and ambiguous mean review, never rejection (tier None,
  `review` true).
- Institution matching: abbreviations and aliases (`config/institution_aliases.json`: short forms,
  former names such as DA-IICT for Dhirubhai Ambani University, the IIT/NIT/IIM/IISER/IIIT/AIIMS/NLU
  patterns, city synonyms), the exact NIRF name (also with its city appended, as NIRF keeps the
  city in its own column), the exact seed name, then fuzzy with an order-sensitive ratio (so
  "Washington University" never meets "University of Washington") and the token guard in order.
  Each comma-separated part is tried when the whole string finds nothing. A name NIRF lists for
  several campuses needs a city or state in the string, or campuses of the same tier; otherwise it
  is ambiguous. Short forms that name more than one institution in an Indian CV (MIT, NTU, DU,
  VIT, BITS alone) are not aliases.
- `eval/tier_coverage.py` also counts education institutions and the institutions of academic
  experience (industry and other experience is counted, not tiered); institutions with no tier go
  to `RESUME_DATA_DIR/reports/tiers/unmatched_institutions.txt`, never printed.
- Licences: the lexicons are third-party data used locally (SCImago, ICORE: see their portals'
  usage notes; NIRF: Government of India rankings, no reuse terms were found on the ranking
  pages). They are never committed: `scripts/check_no_data.py` refuses lexicon file names
  (SCImago, ICORE/CORE, NIRF ranking pages, ROR, OpenAlex) and any CSV over 100 KB. A global
  ranking is not used; candidate free sources for later are ROR and OpenAlex (both CC0, metadata
  only); ARWU, THE and CWUR reserve their rights, and the Leiden Open Edition states no licence.

**Experience calculator (`src/shortlisting/experience.py`).** Years of academic OR industry
experience (the recruiter picks one; "other" counts as neither and is reported) against a tier of
2, 5 or 10 years, from the parser's experience entries; the dates are read with the parser's own
reader (`field_norm.parse_dates`), "Present"/"current" ends on `today` (a parameter). Intervals
of the chosen type are merged before summing (overlaps count once), in calendar months. Status:
meets (fully dated years reach the tier), likely_meets (only with partial dates read at their
widest, or within 0.5 below the tier), does_not_meet (every entry fully dated and clearly below),
unknown (no usable dates and no statement, or below the tier while dates are missing or partial).
Missing data is never read as zero: a resume with no entry of the type is unknown with reason
`no_entries_of_type`. Fallback, only without usable dates: a narrow "N years of (type)
experience" statement in the text, counted only when a word of the chosen type is near; source
`text_statement`, certainty low. The output lists entries by index, type, years and date quality,
never their text. `eval/experience_check.py` compares GT and parser results on the development
resumes (counts only) with disagreement causes from the evaluator's entry pairing.

Experience against a type with no entry of that type: when the resume lists experience of
another type only (and no statement of the chosen type), the result is does_not_meet with
certainty low and reason `no_entries_of_type`; with no experience entries at all it is unknown.

**Shortlisting scorer (`src/shortlisting/`: profile, features, bm25, scorer, cli; settings in
`config/shortlisting.json`, set before any held-out data was seen).** A requirement profile
(mode default / education / experience / publications, role field, minimum degree level,
experience kind and years tier, institution cutoff in education mode, venue cutoff in
publications mode, keywords with synonyms, optional job-ad text turned into keywords by
TF-IDF) drives four components, each a value 0..1 with a certainty, or unusable:
- education: degree level fit, best institution tier (Tier 1 1.0, Tier 2 0.7, Tier 3 0.4;
  unlisted is unusable, never low) and stream fit to the keywords;
- experience: the calculator (meets 1.0, likely_meets 0.7, does_not_meet 0.1, unknown unusable);
- publications: journal and conference entries, each venue tier (A 1.0, B 0.6, C 0.3,
  unknown 0.4 with low certainty) times a recency weight (half-life 6 years), summed, divided by
  a cap and capped at 1: presence, tier and recency, not exact counts (the parser over-detects);
- topic fit: BM25 (written out) of the keywords against publication titles and venues, scaled by
  the best in the pool; the whole text as a low-certainty fallback; dropped with no keywords.
Weights per mode (education / experience / publications / topic fit): default 25/20/25/30,
education 50/10/15/25, publications 15/10/45/30, experience 15/40/15/30. The score is the
weighted mean over the usable components with the weights rescaled; the overall certainty is the
usable weight share and the mean certainty. Eligibility (pass / fail / unknown): minimum degree,
institution cutoff (education mode), venue cutoff (publications mode), experience tier
(experience mode); unlisted institutions and unknown venues give unknown, never fail. Tiers:
Unreadable (FAIL parse, listed apart), Hold (any eligibility fail, listed with the reason),
Review (low certainty, an unusable key component, an unknown eligibility check, or an editorial
override or unlisted institution; a NEEDS_REVIEW parse is a parser flag on the card,
`parse_decision:NEEDS_REVIEW`, and forces Review only when `parse_decision_forces_review` is true,
default false), Shortlist (score at least 0.6 with
certainty at least medium), else Hold. Rank stability: 50 re-scorings (fixed seed) with 10% of
entries dropped and 5% of types flipped give a rank range per candidate. The explanation card is
data only (values, certainties, entry indices, tier labels, parser flag codes, dropped
components). Features read only the education, experience and publications entries: never a
name, gender, age, date of birth, photo or nationality field (a test checks it).
`python -m shortlisting.cli` writes a ranked CSV outside the repo; `eval/shortlist_check.py`
compares GT-based and parser-based rankings on the development resumes (aggregates only).

**Recruiter UI (`app/streamlit_app.py`, launched with `streamlit run app/streamlit_app.py`).**
The only file that imports streamlit; all work is in `src/shortlisting/service.py`
(`run_shortlist`: parse each PDF, cache the parser output by file hash, score, rank). The work
folder is `RESUME_SHORTLIST_DIR`, else `RESUME_DATA_DIR/shortlist_runs`; a folder inside the repo
is refused, and uploads, the cache and CSV exports stay there. Screens: CVs (a folder or an
upload, with the count found), requirements (mode with one sentence each, field, minimum degree,
experience kind and years only in experience mode, institution cutoff only in education mode,
venue cutoff only in publications mode, keywords and synonyms, keyword suggestions from a pasted
job ad), a run button with a progress bar, and results in four tabs (Shortlist, Review, Hold,
Unreadable) with a table (rank, file, score, certainty, parser flag; Hold and Unreadable also
show the reason in plain words, never "rejected" or "failed") and a card per candidate (value
and certainty bars, evidence as entry numbers and tier labels, dropped components, the rank
range, eligibility, any editorial override), the decision-support note, and a CSV export to the
work folder. No name, gender, age, photo or nationality is shown or used.

## 8. Repo layout

```
resume-parser/
  README.md  LICENSE  pyproject.toml  requirements.txt  .gitignore
  .githooks/pre-commit  .github/workflows/ci.yml
  config/   section_aliases.json  degrees.json  titles.json  institutions.json  thresholds.json
  schema/   resume.schema.json  meta.schema.json  run_index.schema.json  review_queue.schema.json
  src/resume_parser/
     intake.py  pipeline.py  cli.py  schema.py  groups.py  headings.py   (groups: data split; headings: aliases, content patterns)
     final.py                                    (the final set: its folders, write check and run guard)
     report.py                                   (run summary, section 5.4)
     extract/  base.py  lines.py  source.py  pdfplumber_extractor.py  pdfminer_extractor.py  ocr_extractor.py
     clean.py  sections.py  normalize.py        (normalize.py builds the Canonical Document)
     entities/ personal.py  education.py  experience.py  publications.py
     field_norm.py  validate.py  scoring.py  writer.py   (writer.py writes main JSON + sidecar + index + review queue)
     learn/    pub_reader.py                    (experimental learned publication fields; not used by any stage)
  eval/     common.py  evaluate.py  funnel.py  calibrate_thresholds.py  end_to_end.py  pub_reader_loo.py  folds.py   (common.py: groups, IDs, validation and test_frozen guards)
            fresh.py   (the fresh set: layout check, the one run, dress rehearsal)
            tier_coverage.py   (tier lookups on the development GT: counts only)
  src/shortlisting/tiers/  lexicons.py  text.py  venues.py  institutions.py   (tier lookups; lexicons outside the repo)
  fixtures/ synthetic/   (fake resumes, built by a script)
  tests/    one test file per stage, plus determinism
  scripts/  check_no_data.py  make_synthetic_resumes.py
  docs/     design.md
```

## 9. Data (never in the repo)

- A folder on each person's computer, set by environment variable `RESUME_DATA_DIR`:
  `pdfs/`, `ground_truth_v2/` (with `_split.json`), `outputs/`, `review_queue/`, `reports/`
  (with `stages/`, `funnel/`, `test_frozen_runs.log` and `validation_runs.log`), `private/`
  (salt and ID map), `models/` (learned readers, if saved).
- The final set has its own three folders outside `RESUME_DATA_DIR` (section 6), and the fresh
  set its own four (section 6); only the fresh run log is mirrored to `reports/fresh_runs.log`.
- Back it up somewhere private. Git will not have a copy.
- Guards: `.gitignore`, a pre-commit hook and a CI check (`scripts/check_no_data.py`) that
  refuse PDFs, JSON outside `config/`, `schema/`, `fixtures/` and `docs/`, any file larger than
  1 MB, any CSV larger than 100 KB, tier lexicon files by name, and banned tool names in file paths
  or contents. The lexicons live in `RESUME_DATA_DIR/Lexicons` (or `RESUME_LEXICON_DIR`). Turn the hook on once per clone with
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

## 14. Known limitations

- **Sidebar and layout-table templates.** CVs drawn with a sidebar (contact, skills or
  languages in a narrow column) or inside a layout table (the page or a column drawn as table
  cells) are read in the wrong order: stage 1 reads top to bottom (or cell by cell), so the
  sidebar and the main column interleave, and a whole page can become one or two huge cells.
  Their many bold or capitalised lines are then over-split by look-based headings in stage 3.
  The pipeline does not try to repair these layouts: it flags them for review
  (`no_target_section`, `missing_target_section:<name>`, `target_lines_in_other`). An attempt
  to read many-row table cells as normal lines was measured on dev and made routing worse
  (wrapped data cells were taken for layout boxes), so it was reverted.
