"""Education entries: split the education sections into entries, then read the fields.

Input: the lines of every education section of the Canonical Document, in order; a section
boundary always ends an entry. Lines are marked first:
  subheading    a short bold or capitalised line with no degree, year or institution
  continuation  starts with a continuation word (thesis, advisor, CGPA, specialisation, ...), or
                mentions a thesis, dissertation, supervisor or advisor, or teaching, courses,
                subjects, supervision or students, with no year and no institution (also when it
                holds a degree word: "Courses taught: M.Sc. Physics"): it goes to the previous
                entry's raw text, never starts an entry
  school        a school-level line (10th, 12th, SSC, HSC, ...) with no higher degree

Split methods, in order (the first whose check reaches the accept threshold wins):
  anchor_split            an entry starts at a line with a degree word, a school-level word, or
                          a year range at the start of its row. If the section starts with an
                          institution line before any degree (institution-first style), entries
                          start at institution lines instead. Lines on the same row as the
                          entry's first line never start a new entry. Not used when most lines are
                          table cells (table_row_split reads those).
  table_row_split         one table row (row_id) is one entry; a row of column-header words with
                          no year is a header and is dropped; a header row also tells which
                          column holds the degree, stream, institution, year and grade. Lines
                          outside the table join the previous entry. Needs most lines in a table.
  paragraph_split         a vertical gap of at least paragraph_gap_ratio x the line height, or a
                          new page, starts an entry
  whole_section_one_entry the whole section as one entry; always WARN

Sections that reached education only through their content (stage 4 content_guess, not a
heading) are stricter: an entry made only of their lines is kept only when it has a degree and
an institution or a year; the others (degree words mentioned in teaching or supervision lists,
fragments) are dropped and counted (dropped_from_content). Sections with a heading are never
filtered this way.

Fields, as printed: degree (longest form in config/degrees.json), degree_level, stream (after
"in", a dash, a colon or in parentheses after the degree, or capitalised subject words right
after it; in a table row with no header row, the cell right after the degree cell when it is a
subject phrase), institution (the segment with an institution keyword or a known name). Year, grade, raw text and provenance go to the sidecar.
School-level entries go to the sidecar excluded list, never to the main list.

Check: min(share of entries that pass the entry checks, coverage), and 0 when the section has
no higher-education entry. Entry checks: a degree or an institution; years within 1950 to
current_year + year_max_offset; not header text. Coverage: share of lines that are in an
entry, a continuation, the excluded list, a header row or a subheading.
"""

from __future__ import annotations

import re

from resume_parser.field_norm import (
    find_degree,
    find_grade,
    find_institution,
    find_stream,
    find_years,
    has_institution,
    is_school_level,
    stream_from_next_cell,
)
from resume_parser.headings import DATE_RANGE
from resume_parser.schema import Method, MethodOutput, load_config

_YEAR_START = re.compile(rf"^\s*(?:\(?\s*){DATE_RANGE.pattern}", re.IGNORECASE)
_YEAR_ONLY_START = re.compile(r"^\s*\(?(?:19|20)\d{2}\b")
_THESIS = re.compile(r"\b(?:thesis|dissertation|supervisor|advisor|adviser|under the guidance)\b", re.IGNORECASE)
# teaching, course or supervision lists ("Courses taught: M.Sc. Physics", "Ph.D. students supervised")
_TEACHING = re.compile(r"\b(?:taught|teach(?:ing|es)?|courses?|subjects?|supervis\w*|guided|guiding|students?|"
                       r"scholars?|mentor\w*)\b", re.IGNORECASE)


def _cfg():
    return load_config("degrees.json")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9%/.]+", " ", text.lower()).strip()


def is_continuation(text: str) -> bool:
    first = _norm(text).split()
    words = {w.lower() for w in _cfg()["continuation_words"]}
    if first and (first[0].rstrip(".:") in words or " ".join(first[:2]) in words):
        return True
    if find_years(text) or has_institution(text):
        return False  # a line with an institution or a year is never only a mention
    return bool(_THESIS.search(text)) or bool(_TEACHING.search(text))


def is_header_text(texts: list[str]) -> bool:
    """A row of column-header words (degree, board, year, ...) with no year."""
    header = {w.lower() for w in _cfg()["header_words"]}
    cells = [_norm(t) for t in texts if t.strip()]
    if not cells or any(find_years(t) for t in texts):
        return False
    hits = sum(1 for c in cells if c in header or all(w in header for w in c.split()))
    return hits >= 2 and hits >= len(cells) - 1


def is_subheading(line: dict) -> bool:
    text = line["text"].strip()
    if line.get("in_table") or len(text.split()) > 5 or not any(ch.isalpha() for ch in text):
        return False
    letters = [ch for ch in text if ch.isalpha()]
    caps = sum(ch.isupper() for ch in letters) / len(letters) >= 0.8
    if not ((line.get("bold") or 0) >= 0.6 or caps):
        return False
    return not (find_degree(text) or find_years(text) or has_institution(text) or is_school_level(text))


def mark(lines: list[dict]) -> list[str]:
    kinds = []
    for line in lines:
        text = line["text"]
        if is_subheading(line):
            kinds.append("subheading")
        elif is_continuation(text):
            kinds.append("continuation")
        elif is_school_level(text):
            kinds.append("school")
        elif find_degree(text):
            kinds.append("degree")
        else:
            kinds.append("plain")
    return kinds


# ---------------------------------------------------------------- entry fields


def parse_entry(lines: list[dict], kinds: list[str], columns: dict[int, str] | None = None) -> dict:
    """Main-format entry plus sidecar details from the lines of one entry."""
    core = [l for l, k in zip(lines, kinds) if k not in ("continuation", "subheading")]
    text = " , ".join(l["text"] for l in core)
    raw = " ".join(l["text"] for l in lines)
    degree = stream = institution = ""
    by_col: dict[str, str] = {}
    if columns:
        for l in core:
            role = columns.get(l.get("cell_col"))
            if role and l["text"].strip():
                by_col.setdefault(role, l["text"].strip())
    from_column = bool(by_col.get("degree"))
    source = by_col["degree"] if from_column else text
    found = find_degree(source)
    if found:
        degree = found[0]
        stream = find_stream(source, found[3])
        institution = find_institution(text, None if from_column else (found[2], found[3]))
    else:
        institution = find_institution(text)
    level = found[1] if found else "other"
    if found and not stream and not columns:
        stream = _stream_in_next_cell(core)
    if by_col.get("stream"):
        stream = by_col["stream"]
    if by_col.get("institution"):
        institution = re.sub(r"\s+", " ", by_col["institution"]).strip(" ,;")
    years = find_years(by_col.get("year", "") or raw)
    entry = {"degree": degree, "degree_level": level, "stream": stream, "institution": institution}
    side = {"page": lines[0]["page"], "lines": [l["id"] for l in lines], "raw_text": raw,
            "year": "-".join(dict.fromkeys(years)) or None,
            "grade": by_col.get("grade") or find_grade(raw) or None}
    school = not found and ("school" in kinds or is_school_level(raw))
    return {"entry": entry, "side": side, "years": [int(y) for y in years], "school": school,
            "texts": [l["text"] for l in core]}


def _stream_in_next_cell(core: list[dict]) -> str:
    """A table row with no header row: the stream from the cell right after the degree cell."""
    if not core or not all(l.get("in_table") and l.get("cell_col") is not None for l in core):
        return ""
    degree_cell = next((l for l in core if find_degree(l["text"])), None)
    if degree_cell is None:
        return ""
    nxt = next((l for l in core if l["cell_col"] == degree_cell["cell_col"] + 1
                and l.get("row_id") == degree_cell.get("row_id")), None)
    return stream_from_next_cell(nxt["text"]) if nxt else ""


# ---------------------------------------------------------------- splitting


def _groups_to_output(groups: list[list[int]], lines: list[dict], kinds: list[str], method: str,
                      leftover: int = 0, header_lines: int = 0, columns_of=None) -> MethodOutput:
    entries, sides, excluded, checks = [], [], [], []
    dropped = 0
    for g in groups:
        ls, ks = [lines[i] for i in g], [kinds[i] for i in g]
        if all(k in ("subheading", "continuation") for k in ks):
            continue
        parsed = parse_entry(ls, ks, columns_of(g) if columns_of else None)
        if parsed["school"]:
            excluded.append(parsed["side"])
            continue
        e = parsed["entry"]
        if all(l.get("from_content") for l in ls) and not (e["degree"] and (e["institution"] or parsed["years"])):
            dropped += 1  # a mention or fragment in a section routed by content only
            continue
        entries.append(parsed["entry"])
        sides.append(dict(parsed["side"], method=method))
        checks.append(parsed)
    return MethodOutput({"method": method, "entries": entries, "sides": sides, "excluded": excluded,
                         "checks": [{"entry": c["entry"], "years": c["years"], "texts": c["texts"]}
                                    for c in checks],
                         "lines_total": len(lines), "leftover": leftover, "header_lines": header_lines,
                         "dropped_from_content": dropped})


def _boundaries(lines: list[dict]) -> set[int]:
    return {i for i, l in enumerate(lines) if l.get("section_start")}


def _table_share(lines: list[dict]) -> float:
    return sum(bool(l.get("in_table")) for l in lines) / len(lines) if lines else 0.0


def anchor_split(payload) -> MethodOutput:
    lines, kinds = payload["lines"], payload["kinds"]
    if not lines:
        return MethodOutput({"method": "anchor_split", "entries": [], "sides": [], "excluded": [], "checks": [],
                             "lines_total": 0, "leftover": 0, "header_lines": 0})
    if _table_share(lines) >= 0.5:
        return MethodOutput(None)
    starts = _boundaries(lines)
    content = [i for i, k in enumerate(kinds) if k not in ("subheading", "continuation")]
    first_degree = next((i for i in content if kinds[i] in ("degree", "school")), None)
    institution_first = bool(content) and content[0] != first_degree and has_institution(lines[content[0]]["text"])
    for i, (line, kind) in enumerate(zip(lines, kinds)):
        if kind in ("subheading", "continuation"):
            continue
        text = line["text"]
        if institution_first:
            anchor = has_institution(text) and kind != "degree" or kind == "school"
        else:
            anchor = kind in ("degree", "school")
        if line.get("segment_index", 0) == 0 and _YEAR_START.match(text):
            anchor = True
        if anchor:
            starts.add(i)
    def has_core(group: list[int]) -> bool:
        return any(kinds[j] in ("degree", "school") or has_institution(lines[j]["text"]) for j in group)

    groups, current, leftover = [], [], 0
    row_of_start = None
    for i, line in enumerate(lines):
        same_row = bool(current) and line.get("row_id") is not None and line.get("row_id") == row_of_start
        new_entry = i in starts and (line.get("section_start") or (not same_row and has_core(current)))
        if new_entry or (i in starts and not current):
            if current:
                groups.append(current)
            current, row_of_start = [i], line.get("row_id")
        elif current:
            current.append(i)  # a continuation, a plain line, or an anchor joining a year-only start
        else:
            leftover += kinds[i] not in ("subheading",)
    if current:
        groups.append(current)
    return _groups_to_output(groups, lines, kinds, "anchor_split", leftover=leftover)


def table_row_split(payload) -> MethodOutput:
    lines, kinds = payload["lines"], payload["kinds"]
    if not lines or _table_share(lines) < 0.5:
        return MethodOutput(None)
    rows: list[list[int]] = []
    row_key = None
    for i, line in enumerate(lines):
        key = (line.get("row_id"), line.get("in_table"))
        if line.get("in_table") and rows and key == row_key and not line.get("section_start"):
            rows[-1].append(i)
        elif line.get("in_table") or not rows or line.get("section_start"):
            rows.append([i])
            row_key = key
        else:
            rows[-1].append(i)  # a line outside the table joins the previous entry
            row_key = None
    header_words = {w.lower() for w in _cfg()["header_words"]}
    columns: dict[int, str] = {}
    groups, header_lines = [], 0
    for r in rows:
        texts = [lines[i]["text"] for i in r]
        if all(lines[i].get("in_table") for i in r) and is_header_text(texts):
            header_lines += len(r)
            columns = _column_roles({lines[i].get("cell_col"): _norm(lines[i]["text"]) for i in r}, header_words)
            continue
        groups.append(r)
    roles = dict(columns)
    return _groups_to_output(groups, lines, kinds, "table_row_split", header_lines=header_lines,
                             columns_of=(lambda g: roles) if roles else None)


_ROLE_WORDS = {
    "degree": ("degree", "course", "qualification", "examination", "exam", "class"),
    "stream": ("subject", "subjects", "specialization", "specialisation", "stream", "discipline"),
    "institution": ("university", "institution", "institute", "college", "school", "board"),
    "year": ("year", "passing"),
    "grade": ("percentage", "cgpa", "gpa", "grade", "marks", "division", "%"),
}


def _column_roles(cells: dict, header_words: set[str]) -> dict[int, str]:
    roles = {}
    for col, text in cells.items():
        words = set(text.replace("/", " ").split())
        for role, keys in _ROLE_WORDS.items():
            if words & set(keys) and role not in roles.values():
                roles[col] = role
                break
    return roles


def paragraph_split(payload) -> MethodOutput:
    lines, kinds = payload["lines"], payload["kinds"]
    if not lines:
        return MethodOutput(None)
    ratio = payload["params"]["paragraph_gap_ratio"]
    groups = [[0]]
    for i in range(1, len(lines)):
        prev, line = lines[i - 1], lines[i]
        height = max(1.0, prev["bbox"][3] - prev["bbox"][1])
        gap = line["bbox"][1] - prev["bbox"][3]
        new = line.get("section_start") or line["page"] != prev["page"] or gap >= ratio * height
        if new and kinds[i] != "continuation":
            groups.append([i])
        else:
            groups[-1].append(i)
    return _groups_to_output(groups, lines, kinds, "paragraph_split")


def whole_section_one_entry(payload) -> MethodOutput:
    lines, kinds = payload["lines"], payload["kinds"]
    if not lines:
        return MethodOutput(None)
    return _groups_to_output([list(range(len(lines)))], lines, kinds, "whole_section_one_entry")


SPLIT_METHODS = (
    Method("anchor_split", anchor_split),
    Method("table_row_split", table_row_split),
    Method("paragraph_split", paragraph_split),
    Method("whole_section_one_entry", whole_section_one_entry, warn_code="whole_section_one_entry"),
)


# ---------------------------------------------------------------- check


def check_education(data: dict, params: dict):
    """(score, details): min(entry pass share, coverage); 0 without a higher-education entry."""
    total = data["lines_total"]
    if total == 0:
        return 1.0, {"entries": 0, "entries_ok": 0, "coverage": 1.0, "leftover": 0, "excluded": 0}
    max_year = params["current_year"] + params["year_max_offset"]
    ok = 0
    for c in data["checks"]:
        e = c["entry"]
        has_core = bool(e["degree"] or e["institution"])
        years_ok = all(1950 <= y <= max_year for y in c["years"])
        header = is_header_text(c["texts"])
        ok += has_core and years_ok and not header
    entries = len(data["checks"])
    ok_share = ok / entries if entries else 0.0
    coverage = round(1 - data["leftover"] / total, 4)
    higher = any(c["entry"]["degree"] for c in data["checks"])
    score = min(ok_share, coverage) if higher else 0.0
    return score, {"entries": entries, "entries_ok": ok, "coverage": coverage, "leftover": data["leftover"],
                   "excluded": len(data["excluded"]), "higher_education": int(higher)}


def education_payload(document: dict, params: dict, content_blocks: frozenset = frozenset()) -> dict:
    """content_blocks: block ids that stage 4 mapped to education by content_guess."""
    lines = []
    for sec in document.get("sections", []):
        if sec["name"] != "education":
            continue
        from_content = sec.get("block_id") in content_blocks
        for k, line in enumerate(sec["lines"]):
            lines.append(dict(line, section_start=(k == 0), from_content=from_content))
    return {"lines": lines, "kinds": mark(lines), "params": params}
