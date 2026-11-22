"""Personal details: the candidate name (main JSON) and emails, phones, links (sidecar only).

Name methods, in order:
  top_lines       the first lines of every personal section in order, then the top lines of
                  page 1 (headings included), until a valid name is found. Each line is cut at
                  the first email, phone, URL, pipe, bullet or contact word; a "Name:" label,
                  leading honorifics and trailing degrees and commas are removed. What is left
                  is never a name when it holds an institution keyword or known institution, an
                  organisation word (department, faculty, centre, laboratory, ...), a
                  section-heading alias, an address word, a degree word or a digit, or only CV
                  words ("curriculum vitae", "resume", "cv", "biodata"). A name is 2 to 5 words
                  (initials with or without dots; hyphens and apostrophes inside words), in title
                  case or capitals, or in any case when the line is bold or has the largest font
                  on page 1 among the lines that pass the other rules. A name split over two
                  short one-word lines is joined.
  leading_words   only when no line passed: the leading 2 to 4 capitalised words of the first
                  page-1 line or table cell (a line that is only a CV title is skipped), when they
                  pass every rule above and form a valid name; always WARN name_from_leading_words
  from_email     the local part of the first email, split on dots, underscores, hyphens and
                  digits, when it gives 2 or more alphabetic tokens; always WARN name_from_email
  from_file_name  the file stem; lowest confidence, always WARN name_from_file_name

Contacts come from every personal section (page 1 when they have none): emails, phones (as
printed, and digits only for matching) and links (ORCID, Scholar, LinkedIn, other web pages).

Check: 0.5 for a valid name (2 to 5 words, letters only after removing dots, hyphens and
apostrophes) plus 0.5 when an email or phone was found.
"""

from __future__ import annotations

import re
from pathlib import Path

from resume_parser.field_norm import find_degree, has_institution
from resume_parser.headings import alias_index
from resume_parser.schema import Method, MethodOutput

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\w/])\+?\(?\d[\d\s().\-]{8,}\d(?![\w/])")
LINK = re.compile(r"(?:https?://|www\.)\S+|\b(?:orcid\.org|scholar\.google\.\w+|linkedin\.com|researchgate\.net|"
                  r"github\.com)/\S*", re.IGNORECASE)
ORCID_ID = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b")
URLISH = re.compile(r"(?:https?://|www\.|\.com\b|\.org\b|\.in\b|\.net\b|\.edu\b)", re.IGNORECASE)
CV_WORDS = re.compile(r"\b(?:curriculum\s+vitae|resume|résumé|cv|bio-?\s?data|profile)\b", re.IGNORECASE)
ADDRESS_WORDS = re.compile(
    r"\b(?:address|road|rd|street|st|nagar|colony|sector|flat|house|apartment|near|district|dist|pin|pincode|"
    r"village|vill|post|tehsil|taluka|state|city|lane|block|floor|india)\b", re.IGNORECASE)
HONORIFIC = re.compile(r"^\s*(?:\(?\s*(?:dr|prof|professor|mr|mrs|ms|miss|shri|sri|smt|kum|km)\b\.?\s*\)?\s*)+",
                       re.IGNORECASE)
NAME_LABEL = re.compile(r"^\s*(?:full\s+)?name\s*[:\-–]\s*", re.IGNORECASE)
CUT = re.compile(
    r"[A-Za-z0-9._%+\-]+@|(?:https?://|www\.)|\+?\(?\d[\d\s().\-]{8,}\d|[|•·▪●◦■►]|"
    r"\b(?:e-?mail|phone|mobile|mob|tel|telephone|contact|address|ph)\b",
    re.IGNORECASE)
ORG_WORDS = re.compile(r"\b(?:department|dept|faculty|centre|center|laboratory|lab|division|office|group)\b",
                       re.IGNORECASE)
WORD = re.compile(r"^[^\W\d_]+(?:[.'\-][^\W\d_]+)*\.?$")
FILE_NOISE = {"cv", "resume", "biodata", "bio", "data", "curriculum", "vitae", "updated", "final", "new", "latest",
              "dr", "prof", "copy", "version", "draft", "pdf", "faculty", "application", "jan", "feb", "mar",
              "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "january", "february",
              "march", "april", "june", "july", "august", "september", "october", "november", "december"}


# ---------------------------------------------------------------- contacts


def _dedupe(xs: list[str]) -> list[str]:
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def find_contacts(lines: list[dict]) -> dict:
    emails, phones, links = [], [], []
    for line in lines:
        text = line["text"]
        emails += EMAIL.findall(text)
        links += [m.rstrip(".,;)") for m in LINK.findall(text)]
        for m in PHONE.findall(ORCID_ID.sub(" ", LINK.sub(" ", EMAIL.sub(" ", text)))):
            digits = re.sub(r"\D", "", m)
            if 10 <= len(digits) <= 13:
                phones.append(m.strip())
    phones = _dedupe(phones)
    return {"emails": _dedupe(emails), "phones": phones,
            "phone_digits": _dedupe([re.sub(r"\D", "", p) for p in phones]), "links": _dedupe(links)}


# ---------------------------------------------------------------- names


def clean_name(text: str) -> str:
    """Remove a "Name:" label, leading honorifics and trailing degrees; normalise spaces."""
    text = NAME_LABEL.sub("", text)
    text = HONORIFIC.sub("", text)
    while True:
        d = find_degree(text)
        if not d or d[2] == 0:
            break
        head = text[: d[2]].rstrip(" ,;(")
        if not head:
            break
        text = head  # a degree after the name ("..., Ph.D.")
    return re.sub(r"\s+", " ", text).strip(" ,;.-")


def valid_name(name: str) -> bool:
    """2 to 5 words of letters; initials with or without dots; hyphens and apostrophes inside."""
    words = name.split()
    return 2 <= len(words) <= 5 and all(WORD.match(w) for w in words)


def _has_alias(text: str) -> bool:
    words = text.lower().split()
    index = alias_index()
    return any(" ".join(words[i:j]) in index for i in range(len(words)) for j in range(i + 1, min(len(words), i + 4) + 1))


def candidate_text(raw: str) -> str | None:
    """The cleaned name part of a line, or None when the line can never hold the name."""
    m = CUT.search(raw)
    text = raw[: m.start()] if m else raw
    text = clean_name(text).strip(" ,;:-")
    if not text or not CV_WORDS.sub("", text).strip(" ,;:-"):
        return None  # empty, or only CV words
    if (any(ch.isdigit() for ch in text) or has_institution(text) or ADDRESS_WORDS.search(text)
            or find_degree(text) or find_degree(text, include_school=True) or _has_alias(text)
            or CV_WORDS.search(text) or ORG_WORDS.search(text)):
        return None
    return text


def _title_or_caps(text: str) -> bool:
    words = text.split()
    capped = sum(1 for w in words if w[:1].isupper())
    return capped >= len(words) - (1 if len(words) > 2 else 0)


def _candidates(lines: list[dict]) -> list[tuple[dict, str]]:
    return [(line, c) for line in lines if (c := candidate_text(line["text"])) is not None]


def _pick(cands: list[tuple[dict, str]], max_font: float, bold_min: float):
    for i, (line, text) in enumerate(cands):
        used, name = [line], text
        if len(name.split()) == 1 and i + 1 < len(cands):  # a name split over two short lines
            nxt_line, nxt = cands[i + 1]
            if len(nxt.split()) == 1 and len(name) <= 20 and len(nxt) <= 20 and nxt_line["page"] == line["page"]:
                name, used = f"{name} {nxt}", [line, nxt_line]
        if not valid_name(name):
            continue
        style = (line.get("bold") or 0) >= bold_min or (max_font and (line.get("font_size") or 0) >= max_font)
        if _title_or_caps(name) or style:
            return name, used
    return None


def name_from_top_lines(payload) -> MethodOutput:
    top = payload["params"]["name_top_lines"]
    page1 = _candidates(payload["page1_lines"][:top])
    max_font = max((line.get("font_size") or 0 for line, _ in page1), default=0)
    bold_min = payload["params"].get("name_bold_min", 0.6)
    for lines in [*payload["personal_sections"], payload["page1_lines"]]:
        found = _pick(_candidates(lines[:top]), max_font, bold_min)
        if found:
            name, used = found
            return MethodOutput(dict(payload["common"], candidate_name=name,
                                     name_source={"page": used[0]["page"], "lines": [x["id"] for x in used]}))
    return MethodOutput(None)


def name_from_email(payload) -> MethodOutput:
    emails = payload["common"]["personal_extra"]["emails"]
    if not emails:
        return MethodOutput(None)
    tokens = [t for t in re.split(r"[._\-+\d]+", emails[0].split("@")[0]) if len(t) >= 2 and t.isalpha()]
    if len(tokens) < 2:
        return MethodOutput(None)
    name = " ".join(t.capitalize() for t in tokens[:5])
    return MethodOutput(dict(payload["common"], candidate_name=name, name_source={"page": None, "lines": []}))


def name_from_file_name(payload) -> MethodOutput:
    stem = Path(payload["resume_file"]).stem
    tokens = [t for t in re.split(r"[\s_\-.\d()]+", stem) if t.isalpha() and t.lower() not in FILE_NOISE and len(t) > 1]
    if not tokens:
        return MethodOutput(None)
    name = " ".join(t.capitalize() if t.islower() or t.isupper() else t for t in tokens[:5])
    return MethodOutput(dict(payload["common"], candidate_name=name, name_source={"page": None, "lines": []}))


_LEAD_WORD = re.compile(r"^(?:[A-Z][^\W\d_]*(?:[.'\-][^\W\d_]+)*\.?|[A-Z]\.)$")


def name_from_leading_words(payload) -> MethodOutput:
    """The name at the start of a longer line or table cell: the leading 2 to 4 capitalised
    words of the first page-1 line or cell (a line that is only a CV title is skipped), when they
    pass every rule of top_lines and form a valid name. Only reached when no line passed."""
    for line in payload["page1_lines"][: payload["params"]["name_top_lines"]]:
        words = line["text"].split()
        lead = []
        for w in words:
            if not _LEAD_WORD.match(w.strip(",;:")) or len(lead) == 4:
                break
            lead.append(w.strip(",;:"))
        if not lead or not CV_WORDS.sub("", " ".join(lead)).strip(" ,;:-"):
            continue  # a CV title line: look at the next line
        for n in range(min(4, len(lead)), 1, -1):
            text = candidate_text(" ".join(lead[:n]))
            if text and valid_name(text) and len(text.split()) == n:
                return MethodOutput(dict(payload["common"], candidate_name=text,
                                         name_source={"page": line["page"], "lines": [line["id"]]}))
        break  # only the first line or cell is tried
    return MethodOutput(None)


NAME_METHODS = (
    Method("top_lines", name_from_top_lines),
    Method("leading_words", name_from_leading_words, warn_code="name_from_leading_words"),
    Method("from_email", name_from_email, warn_code="name_from_email"),
    Method("from_file_name", name_from_file_name, warn_code="name_from_file_name"),
)


def check_personal(data: dict):
    name_ok = valid_name(data["candidate_name"])
    extra = data["personal_extra"]
    contact_ok = bool(extra["emails"] or extra["phones"])
    return 0.5 * name_ok + 0.5 * contact_ok, {"name_ok": int(name_ok), "contact_ok": int(contact_ok)}


def personal_payload(document: dict, resume_file: str, params: dict) -> dict:
    sections = [s for s in document.get("sections", []) if s["name"] == "personal"]
    pool = [line for s in sections for line in s["lines"]]
    page1 = []
    for s in document.get("sections", []):
        heading = s.get("heading_line")
        if heading and heading["page"] == 1 and not heading.get("inline_heading"):
            page1.append(heading)
        page1 += [line for line in s["lines"] if line["page"] == 1]
    contacts = find_contacts(pool)
    if not (contacts["emails"] or contacts["phones"]):
        page1 = [line for s in document.get("sections", []) for line in s["lines"] if line["page"] == 1]
        contacts = find_contacts(page1)
    return {"personal_sections": [s["lines"] for s in sections], "page1_lines": page1,
            "resume_file": resume_file, "params": params, "common": {"personal_extra": contacts}}
