"""Experience entries: split the experience sections into jobs, then read the fields.

Input: the lines of every experience section of the Canonical Document, in order (stage 4
already sent courses taught, responsibilities, projects and talks to other); a section boundary
always ends an entry. Lines are marked first:
  subheading    a short (up to 3 words) bold or capitalised line with no title, date or
                institution word
  continuation  starts with a continuation word (responsible, worked, courses taught, ...), or
                starts with a bullet and has no date and no job title: it goes to the previous
                entry's raw text and never starts an entry
Every other line can fill three slots of an entry: a job title (config/titles.json, at the start
of the line or of a segment), dates, and an institution word.

Split methods, in order (the first whose check reaches the accept threshold wins):
  date_anchor_split       an entry starts at a line with dates or a job title. A line only starts
                          a new entry when it would fill a slot the current entry already has
                          (a second title, second dates, or a second institution-only line), so a
                          title line followed by its institution or dates line stays one entry
                          whatever the order. Lines on the same row as the entry's first line
                          never start a new entry. An entry with dates only is then folded:
                          forward into the next entry when that one has a title and no dates
                          (dates written before the title), else back into the entry before;
                          never across a section boundary. Not used when most lines are table
                          cells.
  table_row_split         one table row is one entry; a row of column-header words with no year
                          is a header, is dropped and tells which column holds the title,
                          institution and dates. Lines outside the table join the previous entry.
  paragraph_split         a vertical gap of at least paragraph_gap_ratio x the line height, or a
                          new page, starts an entry
  whole_section_one_entry the whole section as one entry; always WARN

Sections that reached experience only through their content (stage 4 content_guess) are
stricter: an entry made only of their lines is kept only when it has a job title and an
institution or dates; the others are dropped and counted (dropped_from_content).

Fields, as printed:
  job_title    the longest title from config/titles.json, plus a bracket part written right
               after it ("Assistant Professor (Contract)"); "of ..." or "in ..." after the title
               is not added
  institution  the first segment (cut at commas, dashes, brackets, bars, colons) that is a
               department or holds an institution word. When it is a department, school, faculty
               or centre, the following segments on the line are kept too while they are
               institution, department or short place segments (up to 3 capitalised words), as
               printed. Otherwise only the institution segment is kept, except that a bare IIT,
               NIT, IIIT, IIM, IISER or BITS keeps the place written after it.
  dates        the first date range (month names, 4- or 2-digit years, dash, en-dash, "to",
               "till"; "Present", "till date", ... kept as printed), else a single date
  experience_type  from the institution (field_norm.experience_type)
Parsed start and end go to the sidecar parsed_dates; raw text and provenance to the sidecar.
When a field is not clear it stays empty.

Check: min(share of entries that pass the entry checks, coverage), and 0 when an experience
section exists but no entry was found. Entry checks: a job title or an institution; years within
1950 to current_year + year_max_offset; end not before start; not header text. An entry without
dates passes but is flagged no_dates. Overlapping date ranges are counted, never scored.
"""

from __future__ import annotations

import re
from functools import lru_cache

from resume_parser.field_norm import (
    _clean_segment,
    experience_type,
    find_dates,
    has_institution,
    parse_dates,
)
from resume_parser.schema import Method, MethodOutput, load_config

_SEP = re.compile(r",|;|\||\s[-–—]\s|\(|\)|:")
_BULLET = re.compile(r"^\s*(?:[•●▪■◦○◆◇►▸➢➤✓✔*·]|o\s|[-–—]\s)")
_DEPT = re.compile(r"^(?:department|dept\.?|school\s+of|faculty|centre|center|division|laboratory)\b",
                   re.IGNORECASE)
_LEAD = re.compile(r"^(?:(?:in|at|with|of|the|for)\s+)+", re.IGNORECASE)
# words left at the end of an institution once the dates are taken out ("... University since")
_TRAILING = re.compile(r"(?:\s+(?:since|from|till|until|during|in|to|at|on|w\.e\.f\.?))+\s*$", re.IGNORECASE)
_PROTECTED = re.compile(r"^(?:IIT|NIT|IIIT|IIM|IISER|BITS)$")
_PLACE_WORD = re.compile(r"^[A-Z][A-Za-z.'\-]*$")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


@lru_cache(maxsize=1)
def _cfg() -> dict:
    return load_config("titles.json")


@lru_cache(maxsize=1)
def _title_patterns() -> list[re.Pattern]:
    cfg = _cfg()
    forms = {t for group in ("academic", "industry", "other") for t in cfg.get(group, [])}

    def words(form):
        return r"\s+".join(re.escape(w).replace(r"\.", r"\.?").replace(r"\-", r"[\-\s]?") for w in form.split())

    prefixes = sorted(cfg.get("prefixes", []), key=len, reverse=True)
    # up to two prefixes ("Visiting", "Guest", "Ad-hoc", ...) before a title, kept as printed
    lead = rf"(?:(?:{'|'.join(words(x) for x in prefixes)})[\s\-]+){{0,2}}" if prefixes else ""
    out = []
    for form in sorted(forms, key=len, reverse=True):
        out.append(re.compile(rf"(?<![A-Za-z]){lead}{words(form)}(?![A-Za-z])", re.IGNORECASE))
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9%/.]+", " ", text.lower()).strip()


# words that may come right before a title: "working as", "joined as", "currently", "position:"
_LEAD_IN = re.compile(r"(?:\b(?:working|worked|joined|joining|serving|served|appointed|employed|engaged|selected|"
                      r"currently|presently|now)(?:\s+(?:working|serving))?(?:\s+as)?(?:\s+an?)?|\bas(?:\s+an?)?|"
                      r"\b(?:position|designation|post|role)\s*(?:held)?\s*:?)\s*:?$", re.IGNORECASE)
# a title written after these is someone else's ("under Dr. ...", "supervised by Professor ...")
_NOT_MINE_BEFORE = re.compile(r"(?:\bunder(?:\s+the\s+guidance\s+of)?|\bsupervised\s+by|\breporting\s+to|\bguide\s*:?|"
                              r"\bguidance\s+of|\bmentored\s+by|\bmentor\s*:?|\bwith|\bby)\s*(?:the\s+)?$", re.IGNORECASE)
_HONORIFIC_AFTER = re.compile(r"^\s+(?:Dr|Prof|Mr|Ms|Mrs|Shri|Smt)\b\.?")
_NAME_AFTER = re.compile(r"^\s+((?:[A-Z][a-z]+|[A-Z]\.)(?:\s+(?:[A-Z][a-z]+|[A-Z]\.)){1,2})(?=\s*(?:[,;(|]|$|\s[-–—]\s))")


def _someone_else(text: str, start: int, end: int, mid: bool) -> bool:
    """A title that belongs to another person: after "under", "supervised by", "reporting to",
    "guide:", ..., or directly followed by an honorific; for a title after other words, also when
    directly followed by a capitalised name (2-3 words that are not an institution, a month or
    another title). At the start of a line those words are more often a subject or a place."""
    if _NOT_MINE_BEFORE.search(text[:start]):
        return True
    after = text[end:]
    if _HONORIFIC_AFTER.match(after):
        return True
    if not mid:
        return False
    m = _NAME_AFTER.match(after)
    return bool(m) and not _is_org(m.group(1)) and not find_dates(m.group(1)) and find_title(m.group(1)) is None


def find_title(text: str, mid: bool = False) -> tuple[str, int, int, bool] | None:
    """(title as printed, start, end, mid-line): the longest title that starts the line or a
    segment (after a bullet, comma, dash, colon, bar or bracket) or comes right after a lead-in
    such as "working as", "joined as", "currently", "position:". With mid=True a title after other
    words is also taken (the caller decides when that is safe). The title needs a capital first
    letter unless a lead-in comes before it. A bracket part right after it is added. Titles that
    belong to someone else (see _someone_else) are never taken."""
    best = None
    for pat in _title_patterns():
        for m in pat.finditer(text):
            before = _BULLET.sub("", text[: m.start()]).strip()
            at_start = not before or bool(re.search(r"(?:[,;|:(]|\s[-–—]|^[-–—])$", " " + before))
            lead_in = bool(before) and not at_start and bool(_LEAD_IN.search(before))
            if not (at_start or lead_in or mid):
                continue
            if not m.group(0)[:1].isupper() and not lead_in:
                continue
            if _someone_else(text, m.start(), m.end(), not (at_start or lead_in)):
                continue
            key = (at_start or lead_in, m.end() - m.start(), -m.start())
            if best is None or key > best[0]:
                best = (key, m.start(), m.end())
    if best is None:
        return None
    start, end = best[1], best[2]
    if re.search(r"\bof$", text[start:end], re.IGNORECASE):  # "Head of ...": up to the end of the segment
        rest = re.match(r"(\s+[^,;|()\n:]+?)(?=\s*(?:[,;|():]|\s[-–—]\s|$))", text[end:])
        if not rest or len(rest.group(1).split()) > 5 or find_dates(rest.group(1)):
            return None
        end += len(rest.group(1))
    paren = re.match(r"\s*\(([^()]{1,40})\)", text[end:])
    if paren and not _YEAR.search(paren.group(1)) and not has_institution(paren.group(1)) \
            and not find_dates(paren.group(1)):
        end += paren.end()
    return text[start:end].strip(), start, end, not best[0][0]


def is_continuation(text: str) -> bool:
    words = _norm(_BULLET.sub("", text)).split()
    cont = {w.lower() for w in _cfg().get("continuation_words", [])}
    if words and (words[0].rstrip(".:") in cont or " ".join(words[:2]) in cont):
        return True
    return bool(_BULLET.match(text)) and not find_dates(text) and find_title(_BULLET.sub("", text)) is None


def is_header_text(texts: list[str]) -> bool:
    """A row of column-header words (designation, organisation, duration, ...) with no year."""
    header = {w.lower() for w in _cfg().get("header_words", [])}
    cells = [_norm(t) for t in texts if t.strip()]
    if not cells or any(_YEAR.search(t) for t in texts):
        return False
    hits = sum(1 for c in cells if c in header or all(w in header for w in c.split()))
    return hits >= 2 and hits >= len(cells) - 1


def _is_org(text: str) -> bool:
    return has_institution(text) or experience_type(text) == "industry"


def is_subheading(line: dict) -> bool:
    text = line["text"].strip()
    if line.get("in_table") or len(text.split()) > 3 or not any(ch.isalpha() for ch in text):
        return False
    letters = [ch for ch in text if ch.isalpha()]
    caps = sum(ch.isupper() for ch in letters) / len(letters) >= 0.8
    if not ((line.get("bold") or 0) >= 0.6 or caps):
        return False
    return not (find_title(text) or find_dates(text) or _is_org(text))


def _has_context(text: str) -> bool:
    return bool(find_dates(text)) or _is_org(text)


def mark(lines: list[dict]) -> list[str]:
    """subheading / continuation / entry for each line. Also sets line["title_mid_ok"]: a title
    after other words (not after a lead-in) counts only when the same line or the next one has
    dates or an institution. A line with a title after any words (a lead-in or others) and such a
    date or institution is an entry line even when it starts with a continuation word."""
    kinds = []
    for i, line in enumerate(lines):
        text = line["text"]
        t = find_title(text, mid=True)
        nxt = lines[i + 1]["text"] if i + 1 < len(lines) and not lines[i + 1].get("section_start") else ""
        context = _has_context(text) or _has_context(nxt)
        line["title_mid_ok"] = bool(t and t[3] and context)
        after_words = bool(t) and bool(_BULLET.sub("", text[: t[1]]).strip())
        if is_subheading(line):
            kinds.append("subheading")
        elif after_words and context:  # "Worked as Lecturer, ...": a title line, not a continuation
            kinds.append("entry")
        elif is_continuation(text):
            kinds.append("continuation")
        else:
            kinds.append("entry")
    return kinds


def _line_title(line: dict):
    return find_title(line["text"], mid=bool(line.get("title_mid_ok")))


def _slots(line: dict) -> set[str]:
    text = line["text"]
    out = set()
    if _line_title(line):
        out.add("title")
    if find_dates(text):
        out.add("dates")
    if _is_org(text):
        out.add("institution")
    return out


# ---------------------------------------------------------------- entry fields


def _segments(text: str) -> list[tuple[int, int]]:
    spans, pos = [], 0
    for m in _SEP.finditer(text):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(text)))
    return spans


def _segment_kind(seg: str) -> str:
    seg = _LEAD.sub("", seg.strip())
    if not seg:
        return "empty"
    if _DEPT.match(seg):
        return "department"
    if _is_org(seg):
        return "institution"
    words = seg.split()
    if len(words) <= 3 and all(_PLACE_WORD.match(w) for w in words) and not re.search(r"\d", seg):
        return "place"
    return "other"


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    for a, b in spans:
        text = text[:a] + " " * (b - a) + text[b:]
    return text


def institution_in_line(text: str) -> str:
    """The institution of one line whose title and dates are already blanked out (see the
    module docstring for the rule)."""
    spans = _segments(text)
    kinds = [_segment_kind(text[a:b]) for a, b in spans]
    first = next((k for k, kind in enumerate(kinds) if kind in ("department", "institution")), None)
    if first is None:
        return ""
    if kinds[first] == "department":
        last = first
        while last + 1 < len(spans) and kinds[last + 1] in ("institution", "department", "place"):
            last += 1
        a = spans[first][0]
        a += len(text[a:spans[first][1]]) - len(_LEAD.sub("", text[a:spans[first][1]].lstrip()))
        printed = text[a:spans[last][1]]
        return _TRAILING.sub("", re.sub(r"\s+", " ", printed).strip(" ,;:-–—")).strip(" ,;:-–—")
    seg = text[spans[first][0]:spans[first][1]]
    parts = re.split(r"\s(?:from|at)\s", seg, flags=re.IGNORECASE)
    tail = [p for p in parts if _is_org(p)]
    name = _LEAD.sub("", _clean_segment(tail[-1] if tail else seg))
    if _PROTECTED.match(name) and first + 1 < len(spans) and kinds[first + 1] == "place":
        name = re.sub(r"\s+", " ", text[spans[first][0]:spans[first + 1][1]]).strip(" ,;:-–—")
        name = _LEAD.sub("", name)
    return _TRAILING.sub("", name).strip(" ,;:-–—")


def parse_entry(lines: list[dict], kinds: list[str], columns: dict | None = None, params: dict | None = None) -> dict:
    """Main-format entry plus sidecar details from the lines of one entry."""
    current_year = (params or {}).get("current_year", 2026)
    core = [l for l, k in zip(lines, kinds) if k == "entry"]
    raw = " ".join(l["text"] for l in lines)
    by_col: dict[str, str] = {}
    if columns:
        for l in core:
            role = columns.get(l.get("cell_col"))
            if role and l["text"].strip():
                by_col.setdefault(role, l["text"].strip())
    title = dates = institution = ""
    texts = {id(l): l["text"] for l in core}
    blanks: dict[int, list[tuple[int, int]]] = {id(l): [] for l in core}
    for l in core:
        if columns and columns.get(l.get("cell_col")) not in (None, "title"):
            continue
        t = _line_title(l)
        if t:
            title = t[0]
            blanks[id(l)].append((t[1], t[2]))
            break
    if not title and by_col.get("title"):
        title = re.sub(r"\s+", " ", by_col["title"]).strip(" ,;:")
    found = None
    for want_range in (True, False):
        for l in core:
            d = find_dates(l["text"])
            if d and (not want_range or parse_dates(d[0], current_year)["end"] or
                      parse_dates(d[0], current_year)["ongoing"]):
                found = (l, d)
                break
        if found:
            break
    if found:
        dates = found[1][0].strip("() ")
        blanks[id(found[0])].append((found[1][1], found[1][2]))
    if by_col.get("dates") and by_col.get("dates_end"):
        dates = f"{by_col['dates'].strip()} - {by_col['dates_end'].strip()}"
    elif by_col.get("dates") and not dates:
        dates = by_col["dates"].strip()
    for l in core:
        if columns and columns.get(l.get("cell_col")) not in (None, "institution"):
            continue
        institution = institution_in_line(_blank(texts[id(l)], blanks[id(l)]))
        if institution:
            break
    if not institution and by_col.get("institution"):
        institution = re.sub(r"\s+", " ", by_col["institution"]).strip(" ,;:")
    parsed = parse_dates(dates, current_year) if dates else {"start": None, "end": None, "start_year": None,
                                                             "end_year": None, "ongoing": False}
    entry = {"job_title": title, "institution": institution, "dates": dates,
             "experience_type": experience_type(institution)}
    side = {"page": lines[0]["page"], "lines": [l["id"] for l in lines], "raw_text": raw, "no_dates": not dates}
    years = [y for y in (parsed["start_year"], parsed["end_year"]) if y is not None]
    return {"entry": entry, "side": side, "parsed": {"start": parsed["start"], "end": parsed["end"]},
            "start_year": parsed["start_year"], "end_year": parsed["end_year"], "ongoing": parsed["ongoing"],
            "years": years, "texts": [l["text"] for l in core]}


# ---------------------------------------------------------------- splitting


def _groups_to_output(groups: list[list[int]], payload: dict, method: str, leftover: int = 0,
                      header_lines: int = 0, columns: dict | None = None) -> MethodOutput:
    lines, kinds, params = payload["lines"], payload["kinds"], payload["params"]
    entries, sides, parsed_dates, checks = [], [], [], []
    dropped = 0
    groups, merged = _merge_halves([g for g in groups if any(kinds[i] == "entry" for i in g)], payload, columns)
    for g in groups:
        ls, ks = [lines[i] for i in g], [kinds[i] for i in g]
        p = parse_entry(ls, ks, columns, params)
        e = p["entry"]
        if not (e["job_title"] or e["institution"] or e["dates"]):
            leftover += sum(k == "entry" for k in ks)  # nothing readable: counted as missed lines
            continue
        if all(l.get("from_content") for l in ls) and not (e["job_title"] and (e["institution"] or e["dates"])):
            dropped += 1  # a mention or fragment in a section routed by content only
            continue
        entries.append(e)
        sides.append(dict(p["side"], method=method))
        parsed_dates.append(p["parsed"])
        checks.append({"entry": e, "years": p["years"], "start_year": p["start_year"], "end_year": p["end_year"],
                       "ongoing": p["ongoing"], "texts": p["texts"]})
    return MethodOutput({"method": method, "entries": entries, "sides": sides, "parsed_dates": parsed_dates,
                         "checks": checks, "lines_total": len(lines), "leftover": leftover,
                         "header_lines": header_lines, "dropped_from_content": dropped,
                         "merged_halves": merged})


def _filled(entry: dict) -> set[str]:
    return {k for k in ("job_title", "institution", "dates") if entry[k]}


def _merge_halves(groups: list[list[int]], payload: dict, columns: dict | None) -> tuple[list[list[int]], int]:
    """Join the two halves of one job: an entry with no title but an institution or dates is
    added to the entry right before it when that one has a title and lacks a slot this one fills.
    Never across a section boundary, never two entries that both have a title."""
    lines, kinds, params = payload["lines"], payload["kinds"], payload["params"]

    def fields(g):
        return parse_entry([lines[i] for i in g], [kinds[i] for i in g], columns, params)["entry"]

    out: list[list[int]] = []
    merged = 0
    prev_fields = None
    for g in groups:
        cur = fields(g)
        if out and prev_fields is not None and not lines[g[0]].get("section_start") \
                and not cur["job_title"] and prev_fields["job_title"] \
                and (_filled(cur) - {"job_title"}) - _filled(prev_fields):
            out[-1] = out[-1] + g
            prev_fields = fields(out[-1])
            merged += 1
            continue
        out.append(list(g))
        prev_fields = cur
    return out, merged


def _table_share(lines: list[dict]) -> float:
    return sum(bool(l.get("in_table")) for l in lines) / len(lines) if lines else 0.0


def _empty(method: str) -> MethodOutput:
    return MethodOutput({"method": method, "entries": [], "sides": [], "parsed_dates": [], "checks": [],
                         "lines_total": 0, "leftover": 0, "header_lines": 0, "dropped_from_content": 0,
                         "merged_halves": 0, "folded_dates_only": 0})


def date_anchor_split(payload) -> MethodOutput:
    lines, kinds = payload["lines"], payload["kinds"]
    if not lines:
        return _empty("date_anchor_split")
    if _table_share(lines) >= 0.5:
        return MethodOutput(None)
    groups, current, filled, leftover = [], [], set(), 0
    fills: list[set] = []
    row_of_start = None
    for i, (line, kind) in enumerate(zip(lines, kinds)):
        if kind != "entry":
            if current:
                current.append(i)
            elif kind == "continuation":
                leftover += 1
            continue
        slots = _slots(line)
        same_row = bool(current) and line.get("row_id") is not None and line.get("row_id") == row_of_start
        conflict = ("title" in slots and "title" in filled) or ("dates" in slots and "dates" in filled) or \
                   (slots == {"institution"} and "institution" in filled)
        new_entry = bool(slots) and (not current or line.get("section_start") or (conflict and not same_row))
        if line.get("section_start") and current:
            new_entry = True
        if new_entry:
            if current:
                groups.append(current)
                fills.append(filled)
            current, filled, row_of_start = [i], set(slots), line.get("row_id")
        elif current:
            current.append(i)
            filled |= slots
        else:
            leftover += 1
    if current:
        groups.append(current)
        fills.append(filled)
    groups, folded = _fold_dates_only(groups, fills, lines)
    out = _groups_to_output(groups, payload, "date_anchor_split", leftover=leftover)
    out.data["folded_dates_only"] = folded
    return out


def _fold_dates_only(groups: list[list[int]], fills: list[set], lines: list[dict]) -> tuple[list[list[int]], int]:
    """An entry with dates only (no title, no institution) is not a job of its own. When the entry
    right after it has a title and no dates, the dates belong to that one (dates written before the
    title) and go forward; otherwise they join the entry right before. Never across a section
    boundary; with no entry to join on either side it stays an entry."""
    out: list[list[int]] = []
    out_fills: list[set] = []
    folded = 0
    pending: list[int] | None = None  # dates-only lines waiting for the next entry
    for k, (g, f) in enumerate(zip(groups, fills)):
        if pending is not None:
            g, f = pending + g, f | {"dates"}
            pending = None
        if f == {"dates"}:
            nxt = k + 1 < len(groups) and not lines[groups[k + 1][0]].get("section_start")
            if nxt and "title" in fills[k + 1] and "dates" not in fills[k + 1]:
                pending = g
                folded += 1
                continue
            if out and not lines[g[0]].get("section_start"):
                out[-1] = out[-1] + g
                folded += 1
                continue
        out.append(g)
        out_fills.append(f)
    return out, folded


_ROLE_WORDS = {
    "title": ("designation", "position", "post", "title", "role", "job"),
    "institution": ("organisation", "organization", "employer", "institution", "institute", "university",
                    "college", "company", "place"),
    "dates": ("duration", "period", "from", "year", "years", "date", "dates", "tenure"),
    "dates_end": ("to", "till", "until"),
}


def _column_roles(cells: dict) -> dict:
    roles: dict = {}
    for col, text in sorted(cells.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
        words = set(text.replace("/", " ").split())
        for role, keys in _ROLE_WORDS.items():
            if words & set(keys) and role not in roles.values():
                if role == "dates_end" and len(words) > 1:
                    continue  # "from - to" in one cell is one dates column
                roles[col] = role
                break
    if "dates_end" in roles.values() and "dates" not in roles.values():
        roles = {c: ("dates" if r == "dates_end" else r) for c, r in roles.items()}
    return roles


def table_row_split(payload) -> MethodOutput:
    lines = payload["lines"]
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
    columns: dict = {}
    groups, header_lines = [], 0
    for r in rows:
        texts = [lines[i]["text"] for i in r]
        if all(lines[i].get("in_table") for i in r) and is_header_text(texts):
            header_lines += len(r)
            columns = _column_roles({lines[i].get("cell_col"): _norm(lines[i]["text"]) for i in r})
            continue
        groups.append(r)
    return _groups_to_output(groups, payload, "table_row_split", header_lines=header_lines,
                             columns=columns or None)


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
    return _groups_to_output(groups, payload, "paragraph_split")


def whole_section_one_entry(payload) -> MethodOutput:
    lines = payload["lines"]
    if not lines:
        return MethodOutput(None)
    return _groups_to_output([list(range(len(lines)))], payload, "whole_section_one_entry")


SPLIT_METHODS = (
    Method("date_anchor_split", date_anchor_split),
    Method("table_row_split", table_row_split),
    Method("paragraph_split", paragraph_split),
    Method("whole_section_one_entry", whole_section_one_entry, warn_code="whole_section_one_entry"),
)


# ---------------------------------------------------------------- check


def _overlaps(checks: list[dict], current_year: int) -> int:
    spans = []
    for c in checks:
        if c["start_year"] is None:
            continue
        end = current_year if c["ongoing"] else (c["end_year"] or c["start_year"])
        spans.append((c["start_year"], end))
    return sum(1 for i, a in enumerate(spans) for b in spans[i + 1:] if a[0] < b[1] and b[0] < a[1])


def check_experience(data: dict, params: dict):
    """(score, details): min(entry pass share, coverage); 0 when the section has lines but no
    entry."""
    total = data["lines_total"]
    if total == 0:
        return 1.0, {"entries": 0, "entries_ok": 0, "coverage": 1.0, "leftover": 0, "no_dates": 0, "overlaps": 0}
    max_year = params["current_year"] + params["year_max_offset"]
    ok = no_dates = 0
    for c in data["checks"]:
        e = c["entry"]
        core = bool(e["job_title"] or e["institution"])
        years_ok = all(1950 <= y <= max_year for y in c["years"])
        order_ok = c["end_year"] is None or c["start_year"] is None or c["end_year"] >= c["start_year"]
        no_dates += not e["dates"]
        ok += core and years_ok and order_ok and not is_header_text(c["texts"])
    entries = len(data["checks"])
    coverage = round(1 - data["leftover"] / total, 4)
    score = min(ok / entries, coverage) if entries else 0.0
    return score, {"entries": entries, "entries_ok": ok, "coverage": coverage, "leftover": data["leftover"],
                   "no_dates": no_dates, "overlaps": _overlaps(data["checks"], params["current_year"])}


def experience_payload(document: dict, params: dict, content_blocks: frozenset = frozenset()) -> dict:
    """content_blocks: block ids that stage 4 mapped to experience by content_guess."""
    lines = []
    for sec in document.get("sections", []):
        if sec["name"] != "experience":
            continue
        from_content = sec.get("block_id") in content_blocks
        for k, line in enumerate(sec["lines"]):
            lines.append(dict(line, section_start=(k == 0), from_content=from_content))
    return {"lines": lines, "kinds": mark(lines), "params": params}
