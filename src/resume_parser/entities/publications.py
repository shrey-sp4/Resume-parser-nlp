"""Publications: split the publication sections into citations, then read the fields.

Input: the lines of every publications section of the Canonical Document, in order (stage 4
keeps publication sub-headings inside publications and sends talks, posters and presentations to
other); a section boundary always ends an entry. Lines are marked first:
  subheading  a short heading line inside the section (Journal Papers, Conference Proceedings,
              Book Chapters, ...: config/publications.json, or any short bold or capitalised line
              with no digits and no comma). It sets the type of the entries under it, until the
              next sub-heading or section, and is never an entry.
  total       a printed total ("Total publications: 65", "12 journal papers", "Conference papers:
              5"). It goes to stated_totals and is never an entry; totals never create entries.
  yearhead    a line holding only a year (lists grouped by year); the next line starts an entry
  entry       every other line

Split methods, in order (the first whose check reaches the accept threshold wins):
  marker_split            an entry starts at a line beginning with a number or a bullet ("1.",
                          "[1]", "(1)", "J1.", "•", "-"; a bare number, "12 Doe, ...", only in a
                          run of 3 or more such lines numbered in sequence), right after a
                          year-only line, or (as
                          stage 2 removes bullet characters) at a line that follows a line ending
                          in a full stop and starts like an author list. Also at a line starting
                          with an author list that holds an initial, when the lines since the last
                          start already read as a whole citation (a title and a year), as a
                          citation may end without a year, pages or a full stop. Other lines are
                          wrapped parts of the entry before, also across pages. Gives no output
                          when no line starts an entry the first ways, or fewer than 2 entries
                          would result.
  hanging_indent_split    an entry starts at a line that begins at the left margin of its section
                          and page; lines indented past indent_tolerance_pt are wrapped parts.
                          Gives no output when no line is indented.
  paragraph_split         a vertical gap of at least paragraph_gap_ratio x the line height, or a
                          new page, starts an entry
  whole_section_one_entry the whole section as one entry; always WARN
Sub-heading, total and year-only lines always end the entry before them.

Fields, as printed (a field that is not clear stays empty). An initial extracted with a space
before its dot ("A . B .") is read as "A. B." first.
  title  quoted text when there is any; otherwise the text after the author list (names and
         initials separated by commas, "and" or "&", ending at a full stop or colon, or at the
         last initial before a sentence-case or Title-Case title of 3 or more words; or the
         authors followed by "(year)."), up to the end of that sentence; for comma-only citations
         the first comma segment after the author segments that has 3 or more words
  venue  the first comma segment after the title that holds a journal, conference or publisher
         word, else the first segment that is not volume, pages, year or DOI (at most 15 words);
         when that gives nothing, the segment right after the title if it has at most 8 words and
         is not volume, pages, year, DOI or an author list; otherwise empty. A segment that holds
         a journal, conference or publisher word and ends in volume or pages with no comma before
         them ("Journal of X Vol. 12", "Journal of X 12(3) 45-67") counts once those numbers are
         cut, when what is left still holds the word and is not volume, pages or a year. A leading
         "In" or "In:" is removed; volume and page numbers at its end are cut
  year   the last plausible 4-digit year (1950 to current_year + year_max_offset) outside DOIs
         and links, as a string
  type   the sub-heading, then a journal or a conference word in the entry's own venue (Journal,
         J. before an abbreviated word, Transactions, Letters, Proceedings, Proc., Conference,
         Conf., Symposium, Symp., Workshop, ...), then the section heading (also when the venue
         has both kinds of word, or only weak words such as Proceedings, Review, Letters; with no
         section heading those still count, and Proceedings/Proc. decides between both). With
         none of these: other cues (arXiv, preprint, technical report, thesis, patent, book
         chapter, "in press" with no venue, submitted), then conference cues ("In"/"In:"/"Proc"
         before the venue, an all-capitals acronym venue of 3 to 8 letters next to a year,
         "Int. Conf", "Annual Meeting"). Otherwise a non-empty venue gives journal and an empty
         one gives other; the sidecar then records type_source "default".
Authors, volume, pages, DOI, links, raw text, provenance and type_source go to the sidecar.

Check: min(share of entries that pass the entry checks, coverage), and 0 when the section has
lines but no entry. Entry checks: a title of at least 3 words; a year, when there is one, within
1950 to current_year + year_max_offset; not a heading and not an author list alone; not longer
than max_citation_chars, and no full stop followed by an author-list start with a year on both
sides (several citations run together). The entry count against a printed total is reported,
never scored.
"""

from __future__ import annotations

import re
from functools import lru_cache

from resume_parser.schema import Method, MethodOutput, load_config

TYPE_SOURCES = ("subheading", "section heading", "venue word", "cue", "default")
_MARKER = re.compile(r"^\s*(?:\[\s*\d{1,3}\s*\]|\(\s*\d{1,3}\s*\)|[A-Z]{0,2}\d{1,3}\s*[.)](?!\d)"
                     r"|[•●▪■◦○◆◇►▸➢➤✓✔*·]|[-–—](?=\s))\s*")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_YEAR_ONLY = re.compile(r"^\s*\(?(?:19|20)\d{2}\)?\s*:?\s*$")
_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_DOI = re.compile(r"\b(?:doi\s*:?\s*)?(10\.\d{4,9}/[^\s,;]+)", re.IGNORECASE)
_QUOTED = re.compile(r"[\"“”]\s*([^\"“”]{8,400}?)\s*[\"“”]|‘\s*([^‘’]{8,400}?)\s*’")
_APA_YEAR = re.compile(r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)\s*[.:,]?\s*")
# a full stop after a word of 2+ letters (not an initial), a colon, or "et al."; or a full stop
# after a closing initial when a sentence-case title follows ("Doe J, Roe A. Fast alignment of"),
# or a Title-Case one: three words with no comma, initial, "and" or "&" among them ("Doe, J. K.;
# Roe, A. Fast Alignment Of Reads"), so another name ("A. Ravi Kumar, ...") never counts
_SPACED_INITIAL = re.compile(r"(?<![A-Za-z])([A-Z])\s+\.(?=[\s,;]|$)")  # an initial with a space before its dot
_TITLE_WORD = r"(?!(?:and|&)\s)(?![A-Z]\.?(?=[\s,]|$))[^\s,;]+"
_CUT = re.compile(r"(?:(?<=[A-Za-zÀ-ɏ'’\-)]{2})\.|:|\bet\s+al\.)\s+|(?<=\b[A-Z])\.\s+(?=[A-Z][a-z]+\s+[a-z])"
                  rf"|(?<=\b[A-Z])\.\s+(?=[A-Z][a-zà-ɏ'’\-]+\s+{_TITLE_WORD}\s+{_TITLE_WORD}(?:\s|$))")
_SENTENCE_END = re.compile(r"(?<=[a-zà-ɏ0-9)\]]{2})\.(?=\s+\S)|[?!](?=\s+\S)")
_INITIALS = re.compile(r"^[A-Z]\.?(?:\s?-?[A-Z]\.?){0,3}$")
_AUTHOR_START = re.compile(r"^(?:(?:[A-Z]\.\s?-?){1,3}\s*[A-Z][a-zà-ɏ'’\-]+|[A-Z][a-zà-ɏ'’\-]+,?\s+(?:[A-Z]\.\s?){1,3}"
                           r"|[A-Z][a-zà-ɏ'’\-]+(?:,|\s+and\b|\s*&|\.\s))")
_VOL_PAGES = re.compile(r"\b(?:vol(?:ume)?\.?|no\.?|issue|pp?\.?|pages?)\s*\d|^\s*\(?\d[\d\s():\-–/]*\)?\s*$", re.IGNORECASE)
_TRAIL_NUMBERS = re.compile(r"(?:\s+(?:vol(?:ume)?\.?|no\.?|issue|pp?\.?)(?=\s*\d)\s*\S.*|\s+\(?\d[\d\s():\-–/.]*)$",
                            re.IGNORECASE)
_LEAD_IN = re.compile(r"^\s*(?:in\s*:|in\s+(?=[A-Z0-9])|accepted\s+(?:in|at|for)\s+|published\s+in\s+)", re.IGNORECASE)


@lru_cache(maxsize=1)
def _cfg() -> dict:
    return load_config("publications.json")


def _words_pattern(words: list[str]) -> re.Pattern:
    parts = []
    for w in sorted(words, key=len, reverse=True):
        body = re.escape(w).replace(r"\.", r"\.?").replace(r"\ ", r"\s+")
        parts.append(body)
    return re.compile(r"(?<![A-Za-z])(?:" + "|".join(parts) + r")(?![A-Za-z])", re.IGNORECASE)


@lru_cache(maxsize=1)
def _patterns() -> dict[str, re.Pattern]:
    cfg = _cfg()
    sub = cfg["subheading_words"]
    weak = {w.lower() for w in cfg.get("weak_type_words", [])}
    j_abbrev = r"|(?<![A-Za-z])J\.?\s+(?:of\b|[A-Z][a-z]{0,8}\.)"  # "J. Appl. Phys.", never "J. Surname"
    return {
        "journal": re.compile(_words_pattern(cfg["journal_words"]).pattern + j_abbrev, re.IGNORECASE),
        "conference": _words_pattern(cfg["conference_words"]),
        # strong words set the type even against a section heading; weak ones only without one
        "journal_strong": re.compile(_words_pattern([w for w in cfg["journal_words"] if w.lower() not in weak]).pattern
                                     + j_abbrev, re.IGNORECASE),
        "conference_strong": _words_pattern([w for w in cfg["conference_words"] if w.lower() not in weak]),
        "publisher": _words_pattern(cfg["publisher_words"]),
        "conference_cue": _words_pattern(cfg["conference_cues"]),
        "other_cue": _words_pattern(cfg["other_cues"]),
        "sub_journal": _words_pattern(sub["journal"]),
        "sub_conference": _words_pattern(sub["conference"]),
        "sub_other": _words_pattern(sub["other"]),
        "sub_any": _words_pattern(sub["journal"] + sub["conference"] + sub["other"]
                                  + ["publications", "papers", "list of publications", "research papers",
                                     "international", "national", "peer reviewed", "refereed"]),
    }


# ---------------------------------------------------------------- line marks

_TOTAL_PATTERNS = (
    ("total", re.compile(r"\btotal\b[^0-9\n]{0,40}?\b(?:publications?|papers?|articles?)\b[^0-9\n]{0,10}?(\d{1,4})\b"
                         r"|\btotal\s*(?:no\.?|number)?\s*(?:of\s*)?(?:publications?|papers?)?\s*[:=\-–]\s*(\d{1,4})\b"
                         r"|\b(\d{1,4})\s+(?:publications?|papers?)\s+in\s+total\b", re.IGNORECASE)),
    ("journal", re.compile(r"\b(\d{1,4})\+?\s+(?:[A-Za-z\-]+\s+){0,3}?journal\s+(?:papers?|articles?|publications?)\b"
                           r"|\bjournal\s+(?:papers?|articles?|publications?)\s*[:=\-–]\s*(\d{1,4})\b", re.IGNORECASE)),
    ("conference", re.compile(r"\b(\d{1,4})\+?\s+(?:[A-Za-z\-]+\s+){0,3}?conference\s+(?:papers?|articles?|publications?|proceedings)\b"
                              r"|\bconference\s+(?:papers?|articles?|publications?|proceedings)\s*[:=\-–]\s*(\d{1,4})\b",
                              re.IGNORECASE)),
)


def find_totals(text: str) -> dict[str, int]:
    """Printed totals in one short line: {"total" | "journal" | "conference": N}."""
    if len(text.split()) > 15 or _QUOTED.search(text):
        return {}
    out = {}
    for key, pat in _TOTAL_PATTERNS:
        m = pat.search(text)
        if m:
            out[key] = int(next(g for g in m.groups() if g))
    return out


def heading_type(text: str) -> str | None:
    """journal / conference / other from a sub-heading or section heading, None when it names no
    single type ("Publications", "Journal and Conference Papers")."""
    p = _patterns()
    hits = [t for t in ("journal", "conference", "other") if p[f"sub_{t}"].search(text or "")]
    if not hits and p["conference"].search(text or ""):
        hits = ["conference"]
    return hits[0] if len(hits) == 1 else None


def is_subheading(line: dict) -> bool:
    text = line["text"].strip()
    words = text.split()
    if not words or len(words) > 8 or len(text) > 70 or _MARKER.match(text) or re.search(r"\d", text):
        return False
    if text.count(",") >= 1 or text.endswith(".") and len(words) > 4:
        return False
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    caps = sum(ch.isupper() for ch in letters) / len(letters) >= 0.8
    looks = (line.get("bold") or 0) >= 0.6 or caps or text.endswith(":")
    if _patterns()["sub_any"].search(text):
        return looks or len(words) <= 5
    return looks and len(words) <= 5


def mark(lines: list[dict]) -> list[str]:
    kinds = []
    for line in lines:
        text = line["text"]
        if _YEAR_ONLY.match(text):
            kinds.append("yearhead")
        elif find_totals(text) and not _MARKER.match(text):
            kinds.append("total")
        elif is_subheading(line):
            kinds.append("subheading")
        else:
            kinds.append("entry")
    return kinds


# ---------------------------------------------------------------- entry fields


def _join(texts: list[str]) -> str:
    out = ""
    for t in texts:
        t = t.strip()
        if not t:
            continue
        if not out:
            out = t
        elif out.endswith("-") and t[:1].islower():
            out += t  # a word wrapped at a hyphen
        else:
            out += " " + t
    return out


def _author_like(prefix: str) -> bool:
    """An author list: names and initials separated by commas, "and" or "&"."""
    p = prefix.strip(" ,;.:*")
    if not p or len(p) > 600:
        return False
    tokens = [t.strip("*,;†‡0123456789") for t in re.split(r"[\s,;]+", p)]
    tokens = [t for t in tokens if t]
    connectors = {w.lower() for w in _cfg()["author_connectors"]}
    names = [t for t in tokens if t.lower() not in connectors]
    if not names or len(names) > 80:
        return False
    if any(not (t[:1].isupper() or t[:1] in "'’") for t in names):
        return False
    if sum(not _INITIALS.match(t) for t in names) > 3 * max(1, p.count(",") + p.count(" and ") + p.count("&") + 1):
        return False
    has_initial = any(_INITIALS.match(t) for t in names)
    has_sep = "," in p or " and " in p or "&" in p
    return has_initial or (has_sep and len(names) >= 2)


def _title_end(text: str, start: int) -> int:
    """The end of the sentence that starts at start, or the comma before the venue (a segment
    with a venue word, or starting with a number), whichever comes first."""
    m = _SENTENCE_END.search(text, start)
    end = m.end() if m and text[m.start()] in "?!" else (m.start() if m else len(text))
    for c in re.finditer(r",\s+", text[start:end]):
        nxt = re.split(r",\s+", text[start + c.end():end], maxsplit=1)[0]
        if _venue_word(nxt) or re.match(r"\(?\d", nxt) or _LEAD_IN.match(nxt):
            return start + c.start()
    return end


def find_title(text: str) -> tuple[str, int, int, str]:
    """(title, start, end, authors) or ("", 0, 0, "")."""
    for m in _QUOTED.finditer(text):
        t = (m.group(1) or m.group(2) or "").strip(" ,.;:")
        if len(t.split()) >= 3:
            return t, m.start(), m.end(), text[: m.start()].strip(" ,.;:")
    apa = _APA_YEAR.search(text)
    if apa and _author_like(text[: apa.start()]):
        start = apa.end()
        end = _title_end(text, start)
        t = text[start:end].strip(" ,.;:")
        if len(t.split()) >= 3:
            return t, start, end, text[: apa.start()].strip(" ,.;:")
    for m in _CUT.finditer(text):
        prefix, rest = text[: m.start()], text[m.end():]
        if not _author_like(prefix) or _AUTHOR_START.match(rest):
            continue
        start = m.end()
        end = _title_end(text, start)
        t = text[start:end].strip(" ,.;:")
        if len(t.split()) >= 3:
            return t, start, end, prefix.strip(" ,.;:")
        break
    segs = _comma_segments(text)
    # comma-only citations: the author segments are judged together, then the title follows
    run = _author_run(segs)
    if run:
        for start, seg in segs[run:]:
            if len(seg.split()) >= 3 and not _venue_word(seg) and not _VOL_PAGES.search(seg):
                return seg.strip(" ,.;:"), start, start + len(seg), text[: segs[run - 1][0] + len(segs[run - 1][1])].strip(" ,.;:")
            break
    # title first: "Title; Authors, ..." or "Title, Authors, ..."
    semi = text.find(";")
    if semi > 0:
        head, tail = text[:semi], text[semi + 1:]
        tail_segs = _comma_segments(tail.strip())
        n = _author_run(tail_segs)
        if _title_like(head) and n:
            return head.strip(" ,.;:"), 0, semi, ", ".join(s.strip() for _p, s in tail_segs[:n])
    n = _author_run(segs[1:])
    if len(segs) >= 3 and _title_like(segs[0][1]) and n:
        start, seg = segs[0]
        return seg.strip(" ,.;:"), start, start + len(seg), ", ".join(s.strip() for _p, s in segs[1:1 + n])
    return "", 0, 0, ""


_GLUED = re.compile(r"^(?:[A-Z]\.\s?-?){1,3}[A-Z][a-zà-ɏ'’\-]+$")  # "A.K.Surname", "A. K.Surname"
_NAME_WORD = re.compile(r"^[A-Z][a-zà-ɏ'’\-]+$|^[A-Z]{2,3}$")


def _comma_segments(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for part in re.split(r"(\s*,\s+)", text):
        if not re.fullmatch(r"\s*,\s+", part) and part.strip():
            out.append((pos, part))
        pos += len(part)
    return out


def _author_segment(seg: str) -> tuple[bool, bool]:
    """(is one or two author names, has an initial). A name is initials and a surname (also
    glued, "A.K.Surname"), or two or three capitalised words; "and" / "&" may join two names."""
    connectors = {w.lower() for w in _cfg()["author_connectors"]}
    seg = seg.strip(" .;:*")
    if not seg or _venue_word(seg) or re.search(r"\d", seg):
        return False, False
    names = [n for n in re.split(r"\s+(?:and|&)\s+|\s*&\s*", seg) if n.strip()]
    initial = False
    for name in names:
        tokens = [t for t in name.split() if t.lower().strip(".") not in connectors or t[:1].isupper()]
        tokens = [t for t in tokens if t.lower() not in ("et", "al", "al.")]
        if not tokens or len(tokens) > 4 or (len(tokens) == 1 and re.fullmatch(r"[A-Z]{4,}", tokens[0])):
            return False, False  # an acronym of 4+ capitals without dots ("IJTM") is not a name
        has_init = any(_INITIALS.match(t) or _GLUED.match(t) for t in tokens)
        if not all(_INITIALS.match(t) or _GLUED.match(t) or _NAME_WORD.match(t) for t in tokens):
            return False, False
        if not has_init and len(tokens) > 3:
            return False, False
        initial = initial or has_init
    return True, initial


def _author_run(segs: list[tuple[int, str]]) -> int:
    """How many leading comma segments form an author list (judged together): at least two name
    segments, or one with an initial or an "and"; 0 when there is none."""
    n, initial, joined = 0, False, False
    for _start, seg in segs:
        ok, init = _author_segment(seg)
        if not ok:
            break
        n += 1
        initial = initial or init
        joined = joined or bool(re.search(r"\s(?:and|&)\s", seg))
    if n >= 2 or (n == 1 and (initial or joined)):
        return n
    return 0


def _title_like(seg: str) -> bool:
    s = seg.strip(" ,.;:")
    return len(s.split()) >= 3 and not _author_segment(s)[0] and not _venue_word(s) and not re.search(r"\d{4}", s)


def _venue_word(text: str) -> bool:
    p = _patterns()
    return bool(p["journal"].search(text) or p["conference"].search(text) or p["publisher"].search(text))


def find_venue(rest: str) -> tuple[str, bool]:
    """(venue as printed, an "In"/"In:"/"Proc" lead came before it) from the text after the title."""
    rest = rest.strip(" .,;:")
    lead = bool(_LEAD_IN.match(rest)) or bool(re.match(r"^\s*proc", rest, re.IGNORECASE))
    rest = _LEAD_IN.sub("", rest)
    raw = [s.strip(" .;:") for s in re.split(r",\s+|\s+\((?=(?:19|20)\d{2})", rest)]
    raw = [s for s in raw if s]

    def usable(s):
        return bool(re.search(r"[A-Za-z]{2}", s)) and not _VOL_PAGES.search(s) and \
            not _YEAR.fullmatch(s.strip("() ")) and not _DOI.search(s) and not _URL.search(s)

    def venue_with_numbers(s):
        """A venue with its volume or pages written after it without a comma, numbers cut."""
        if not _venue_word(s):
            return None
        cut = _TRAIL_NUMBERS.sub("", s).strip(" .,;:(")
        return cut if cut != s and usable(cut) and _venue_word(cut) else None

    segs = [s if usable(s) else venue_with_numbers(s) for s in raw]
    segs = [s for s in segs if s]
    pick = next((s for s in segs if _venue_word(s)), segs[0] if segs else "")
    pick = _TRAIL_NUMBERS.sub("", pick).strip(" .,;:(")
    if 2 <= len(pick) and len(pick.split()) <= 15:
        return pick, lead
    # fallback when that gives nothing: the segment right after the title, when it is short and
    # is not volume, pages, DOI, year or an author list; otherwise the venue stays empty
    first = raw[0] if raw else ""
    if not first or not usable(first) or _author_run(_comma_segments(first)):
        return "", lead
    first = _TRAIL_NUMBERS.sub("", first).strip(" .,;:(")
    return (first, lead) if 2 <= len(first) and len(first.split()) <= 8 else ("", lead)


def publication_type(sub_type, sec_type, venue: str, lead: bool, text: str) -> tuple[str, str]:
    """(type, type_source); see the module docstring for the order."""
    if sub_type:
        return sub_type, "subheading"
    p = _patterns()
    conf = bool(venue) and bool(p["conference"].search(venue))
    jour = bool(venue) and bool(p["journal"].search(venue))
    if conf != jour:  # one kind of word in the entry's own venue
        strong = bool(p["conference_strong" if conf else "journal_strong"].search(venue))
        if strong or not sec_type:  # a weak word (Proceedings, Review, Letters, ...) yields to the heading
            return ("conference" if conf else "journal"), "venue word"
    if sec_type:  # also when the venue has both kinds of word
        return sec_type, "section heading"
    if conf and jour:
        both = "conference" if re.search(r"proceedings|proc\.?\b", venue, re.IGNORECASE) else "journal"
        return both, "venue word"
    other = p["other_cue"].search(text)
    if other and not (other.group(0).lower() == "in press" and venue):
        return "other", "cue"
    acronym_year = bool(venue) and bool(re.fullmatch(r"[A-Z]{3,8}", venue)) and bool(
        re.search(rf"\b{re.escape(venue)}\s*['’]?\s*(?:(?:19|20)\d{{2}}|\d{{2}})\b", text))
    if lead or p["conference_cue"].search(text) or acronym_year:
        return "conference", "cue"
    return ("journal", "default") if venue else ("other", "default")


def parse_entry(lines: list[dict], params: dict) -> dict:
    raw = _join([l["text"] for l in lines])
    text = _MARKER.sub("", raw, count=1).strip()
    text = re.sub(r"^\d{1,3}\s+(?=[A-Z])", "", text)  # a bare list number ("12 Surname, ...")
    text = _SPACED_INITIAL.sub(r"\1.", text)  # "Doe, A . B ." as extracted -> "Doe, A. B."
    urls = _URL.findall(text)
    doi = _DOI.search(text)
    clean = _DOI.sub(" ", _URL.sub(" ", text))
    max_year = params["current_year"] + params["year_max_offset"]
    years = [y for y in _YEAR.findall(clean) if 1950 <= int(y) <= max_year]
    title, _start, end, authors = find_title(clean)
    venue, lead = find_venue(clean[end:]) if title else ("", False)
    sub_type, sec_type = lines[0].get("pub_sub_type"), lines[0].get("pub_sec_type")
    ptype, source = publication_type(sub_type, sec_type, venue, lead, clean)
    volume = re.search(r"\bvol(?:ume)?\.?\s*(\d+)|\b(\d{1,4})\s*\(\s*\d{1,3}\s*\)", clean, re.IGNORECASE)
    pages = re.search(r"\bpp?\.?\s*(\d+\s*[-–]\s*\d+|\d+)|(?<![\d/.])(\d{1,5}\s*[-–]\s*\d{1,5})(?![\d/])", clean,
                      re.IGNORECASE)
    entry = {"title": title, "type": ptype, "year": years[-1] if years else "", "venue": venue}
    side = {"page": lines[0]["page"], "lines": [l["id"] for l in lines], "raw_text": raw, "type_source": source,
            "authors": authors or None, "volume": next((g for g in volume.groups() if g), None) if volume else None,
            "pages": next((g for g in pages.groups() if g), None) if pages else None,
            "doi": doi.group(1).rstrip(".") if doi else None, "urls": urls}
    all_years = [int(y) for y in _YEAR.findall(clean)]
    return {"entry": entry, "side": side, "years": all_years, "raw": raw, "authors_only": bool(authors) and not title
            and not venue}


# ---------------------------------------------------------------- splitting


def _empty(method: str) -> MethodOutput:
    return MethodOutput({"method": method, "entries": [], "sides": [], "checks": [], "lines_total": 0,
                         "leftover": 0, "stated_totals": {"journal": None, "conference": None, "total": None},
                         "type_default": 0})


def _groups_to_output(groups: list[list[int]], payload: dict, method: str) -> MethodOutput:
    if payload.get("groups_only"):  # wrapped_entries: the grouping alone, no fields
        return MethodOutput({"groups": groups})
    lines, params = payload["lines"], payload["params"]
    entries, sides, checks = [], [], []
    leftover = 0
    for g in groups:
        p = parse_entry([lines[i] for i in g], params)
        e = p["entry"]
        if not (e["title"] or e["venue"]):
            leftover += len(g)  # nothing readable: counted as missed lines
            continue
        entries.append(e)
        sides.append(dict(p["side"], method=method))
        checks.append({"entry": e, "years": p["years"], "raw": p["raw"], "authors_only": p["authors_only"],
                       "texts": [lines[i]["text"] for i in g]})
    return MethodOutput({"method": method, "entries": entries, "sides": sides, "checks": checks,
                         "lines_total": len(lines), "leftover": leftover, "stated_totals": payload["stated_totals"],
                         "type_default": sum(s["type_source"] == "default" for s in sides)})


def _group(payload, starts_entry) -> list[list[int]]:
    """Groups of entry lines; a new group at a section start, after a sub-heading, total or year
    line, or where starts_entry(i) says so."""
    lines, kinds = payload["lines"], payload["kinds"]
    groups: list[list[int]] = []
    current: list[int] = []
    boundary = True
    for i, (line, kind) in enumerate(zip(lines, kinds)):
        if line.get("section_start"):
            boundary = True
        if kind != "entry":
            if current:
                groups.append(current)
                current = []
            boundary = True
            continue
        if current and (boundary or starts_entry(i)):
            groups.append(current)
            current = []
        current.append(i)
        boundary = False
    if current:
        groups.append(current)
    return groups


# the end of a citation: a year, pages or a DOI as the last thing on the line
_CITATION_END = re.compile(r"(?:(?<!\d)(?:19|20)\d{2}[a-z]?\)?|\d+\s*[-–]\s*\d+|10\.\d{4,9}/\S+)\s*[.;,)]?\s*$")
# a wrapped part, never the start of a citation
_NOT_A_START = re.compile(r"^(?:(?-i:[a-z])|\d|pp?\.|vol\b|no\.|doi\b|10\.\d|https?:|www\.|/)", re.IGNORECASE)


def marker_split(payload) -> MethodOutput:
    lines, kinds = payload["lines"], payload["kinds"]
    if not lines:
        return _empty("marker_split")
    bare = _bare_number_starts(lines, kinds)

    def starts_here(i, line):
        if _MARKER.match(line["text"]) or i in bare or (i > 0 and kinds[i - 1] == "yearhead"):
            return True
        # stage 2 removes bullet characters: an unnumbered list shows as a line after a full stop
        # that starts like an author list
        prev = lines[i - 1]["text"].rstrip() if i > 0 and kinds[i - 1] == "entry" else ""
        text = line["text"].strip()
        if not prev or _NOT_A_START.match(text):
            return False
        author_start = bool(_AUTHOR_START.match(text)) or bool(_author_run(_comma_segments(text)[:2]))
        # a line that ends a citation (a year, pages or a DOI at its end), then an author list
        return author_start and (prev.endswith(".") or bool(_CITATION_END.search(prev)))

    starts = {i for i, (l, k) in enumerate(zip(lines, kinds)) if k == "entry" and starts_here(i, l)}
    firsts = {i for i, (l, k) in enumerate(zip(lines, kinds))
              if k == "entry" and (l.get("section_start") or (i > 0 and kinds[i - 1] != "entry"))}
    if len(starts) < 1 or len(starts | firsts) < 2:
        return MethodOutput(None)
    # a citation that ends without a year, pages or a full stop: a new one also starts at an author
    # list with initials when the lines since the last start already read as a whole citation (a
    # title and a year). These extra starts never decide whether the method runs.
    extra, current = set(), []
    for i, (line, kind) in enumerate(zip(lines, kinds)):
        if kind != "entry" or line.get("section_start"):
            current = [i] if kind == "entry" else []
            continue
        if i in starts:
            current = [i]
            continue
        if current and _initials_author_start(line["text"]):
            done = parse_entry([lines[j] for j in current], payload["params"])["entry"]
            if done["title"] and done["year"]:
                extra.add(i)
                current = [i]
                continue
        current.append(i)
    return _groups_to_output(_group(payload, lambda i: i in starts or i in extra), payload, "marker_split")


_BARE_NUMBER = re.compile(r"^\s*(\d{1,3})\s+(?=[A-Z(\[\"“‘])")
BARE_RUN_MIN = 3


def _bare_number_starts(lines: list[dict], kinds: list[str]) -> set[int]:
    """Entry lines that begin with a list number with no dot or bracket ("12 Doe, A., ..."). They
    start entries only in a run of at least BARE_RUN_MIN such lines of one section numbered in
    sequence (each one more, or each one less, than the one before), so a wrapped line that begins
    with a volume or a page number does not."""
    out: set[int] = set()
    run: list[tuple[int, int]] = []

    def close():
        if len(run) >= BARE_RUN_MIN:
            out.update(i for i, _ in run)

    step = 0
    for i, (line, kind) in enumerate(zip(lines, kinds)):
        if line.get("section_start"):
            close()
            run, step = [], 0
        m = _BARE_NUMBER.match(line["text"]) if kind == "entry" else None
        if not m:
            continue
        value = int(m.group(1))
        if run and value - run[-1][1] in (1, -1) and (step == 0 or value - run[-1][1] == step):
            step = value - run[-1][1]
            run.append((i, value))
        else:
            close()
            run, step = [(i, value)], 0
    close()
    return out


def _initials_author_start(text: str) -> bool:
    """A line that starts with an author list holding an initial ("A. Doe, ...", "Doe, A., ...",
    "Doe A, Roe B, ..."); never a wrapped part."""
    text = _SPACED_INITIAL.sub(r"\1.", text.strip())
    if not text or _NOT_A_START.match(text):
        return False
    if re.match(r"^(?:[A-Z]\.\s?-?){1,3}\s*[A-Z][a-zà-ɏ'’\-]+|^[A-Z][a-zà-ɏ'’\-]+,?\s+(?:[A-Z]\.\s?){1,3}(?=[\s,;&]|$)",
                text):
        return True
    segs = _comma_segments(text)
    return bool(segs) and _author_run(segs[:2]) > 0 and _author_segment(segs[0][1])[1]


def hanging_indent_split(payload) -> MethodOutput:
    lines, kinds = payload["lines"], payload["kinds"]
    if not lines:
        return _empty("hanging_indent_split")
    tol = payload["params"]["indent_tolerance_pt"]
    margin: dict[tuple, float] = {}
    section = 0
    keys = []
    for line in lines:
        section += bool(line.get("section_start"))
        keys.append((section, line["page"]))
    for line, kind, key in zip(lines, kinds, keys):
        if kind == "entry":
            margin[key] = min(margin.get(key, line["bbox"][0]), line["bbox"][0])
    indented = [i for i, (l, k, key) in enumerate(zip(lines, kinds, keys))
                if k == "entry" and l["bbox"][0] > margin[key] + tol]
    if not indented:
        return MethodOutput(None)
    return _groups_to_output(_group(payload, lambda i: lines[i]["bbox"][0] <= margin[keys[i]] + tol),
                             payload, "hanging_indent_split")


def paragraph_split(payload) -> MethodOutput:
    lines = payload["lines"]
    if not lines:
        return MethodOutput(None)
    ratio = payload["params"]["paragraph_gap_ratio"]

    def new_paragraph(i):
        if i == 0:
            return True
        prev, line = lines[i - 1], lines[i]
        height = max(1.0, prev["bbox"][3] - prev["bbox"][1])
        return line["page"] != prev["page"] or line["bbox"][1] - prev["bbox"][3] >= ratio * height

    return _groups_to_output(_group(payload, new_paragraph), payload, "paragraph_split")


def whole_section_one_entry(payload) -> MethodOutput:
    lines, kinds = payload["lines"], payload["kinds"]
    if not lines:
        return MethodOutput(None)
    groups = [[i for i, k in enumerate(kinds) if k == "entry"]]
    return _groups_to_output([g for g in groups if g], payload, "whole_section_one_entry")


SPLIT_METHODS = (
    Method("marker_split", marker_split),
    Method("hanging_indent_split", hanging_indent_split),
    Method("paragraph_split", paragraph_split),
    Method("whole_section_one_entry", whole_section_one_entry, warn_code="whole_section_one_entry"),
)


# ---------------------------------------------------------------- check


def looks_merged(raw: str, max_year: int) -> bool:
    """Two citations run together: a full stop followed by an author-list start, with a
    plausible year both before and after it."""
    def has_year(s):
        return any(1950 <= int(y) <= max_year for y in _YEAR.findall(s))
    for m in re.finditer(r"\.\s+", raw):
        if _AUTHOR_START.match(raw[m.end():]) and has_year(raw[: m.start()]) and has_year(raw[m.end():]):
            return True
    return False


def check_publications(data: dict, params: dict):
    """(score, details): min(entry pass share, coverage); 0 when the section has lines but no
    entry. The count against a printed total is reported only."""
    total = data["lines_total"]
    stated = (data.get("stated_totals") or {}).get("total")
    if total == 0:
        return 1.0, {"entries": 0, "entries_ok": 0, "coverage": 1.0, "leftover": 0, "type_default": 0}
    max_year = params["current_year"] + params["year_max_offset"]
    ok = 0
    for c in data["checks"]:
        e = c["entry"]
        title_ok = len(e["title"].split()) >= 3
        year_ok = all(1950 <= y <= max_year for y in c["years"][-1:])
        heading = len(c["texts"]) == 1 and is_subheading({"text": c["texts"][0]})
        ok += title_ok and year_ok and not heading and not c["authors_only"] \
            and len(c["raw"]) <= params["max_citation_chars"] and not looks_merged(c["raw"], max_year)
    entries = len(data["checks"])
    coverage = round(1 - data["leftover"] / total, 4)
    score = min(ok / entries, coverage) if entries else 0.0
    details = {"entries": entries, "entries_ok": ok, "coverage": coverage, "leftover": data["leftover"],
               "type_default": data.get("type_default", 0)}
    if stated:
        details["entries_minus_stated_total"] = entries - stated
    return score, details


def publications_payload(document: dict, params: dict) -> dict:
    """Lines of every publications section with the type their section heading and the latest
    sub-heading give, and the printed totals found."""
    lines, kinds = [], []
    totals = {"journal": None, "conference": None, "total": None}
    for sec in document.get("sections", []):
        if sec["name"] != "publications":
            continue
        sec_type = heading_type(sec.get("heading_raw") or "")
        sub_type = None
        block = [dict(line, section_start=(k == 0)) for k, line in enumerate(sec["lines"])]
        for line, kind in zip(block, mark(block)):
            if kind == "subheading":
                sub_type = heading_type(line["text"])
            if kind == "total":
                for key, n in find_totals(line["text"]).items():
                    totals[key] = totals[key] if totals[key] is not None else n
            line["pub_sec_type"], line["pub_sub_type"] = sec_type, sub_type
            lines.append(line)
            kinds.append(kind)
    return {"lines": lines, "kinds": kinds, "params": params, "stated_totals": totals}


# ---------------------------------------------------------------- used by stage 4


def wrapped_entries(lines: list[dict], params: dict) -> list[str]:
    """The lines of one block joined into entries the way the publication splitters would join
    them (markers, then hanging indent, then vertical gaps); one text per entry."""
    payload = publications_payload({"sections": [{"name": "publications", "heading_raw": "", "lines": lines}]},
                                   params)
    payload["groups_only"] = True
    for method in (marker_split, hanging_indent_split, paragraph_split):
        out = method(payload)
        if out.data is not None and out.data.get("groups"):
            return [_join([payload["lines"][i]["text"] for i in g]) for g in out.data["groups"]]
    return [_join([l["text"] for l in payload["lines"]])] if payload["lines"] else []


def looks_like_citation(text: str, max_year: int, max_chars: int) -> bool:
    """A plausible year plus an author-list start (a venue word alone is not enough), and no
    longer than max_chars (a longer entry is several items run together)."""
    clean = _MARKER.sub("", text, count=1).strip()
    if len(clean) > max_chars or not any(1950 <= int(y) <= max_year for y in _YEAR.findall(clean)):
        return False
    return bool(_AUTHOR_START.match(clean)) or bool(_author_run(_comma_segments(clean)[:2]))


def citation_share(lines: list[dict], params: dict) -> float:
    """Share of the block's wrapped entries that look like citations."""
    entries = wrapped_entries(lines, params)
    if not entries:
        return 0.0
    max_year = params["current_year"] + params["year_max_offset"]
    return sum(looks_like_citation(e, max_year, params["max_citation_chars"]) for e in entries) / len(entries)
