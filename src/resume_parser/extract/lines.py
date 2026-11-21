"""Shared line building and label-free text checks for stage 1.

A line is a dict:
    id             "p001_l0007" (page, reading order)
    page           page number (1-based)
    text           words joined by single spaces
    bbox           [x0, top, x1, bottom] in points, top-left origin
    font_size      median character size
    bold           share of characters in a bold font (0..1)
    in_table       True for a table cell
    cell_row       table row index, or None
    cell_col       table column index, or None
    row_id         lines on the same visual row share this id, so a row split at a wide
                   gap (columns, a table-like row) can be joined again later
    segment_index  position of the line within its row, left to right
    reading_order  1-based index within the page

Rows are formed from words whose tops are within row_tolerance_ratio x font size. A row is
split into segments where the gap between two words is at least segment_gap_ratio x the
row's median font size, so a justified paragraph stays whole but separate blocks on one row
do not merge.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter

_BOLD = re.compile(r"bold|black|heavy|semibold|demibold", re.IGNORECASE)
_CID = re.compile(r"\(cid:\d+\)")


def is_bold(fontname: str | None) -> bool:
    return bool(fontname) and bool(_BOLD.search(fontname))


def _median(values: list[float], default: float = 0.0) -> float:
    return float(statistics.median(values)) if values else default


def _r(x: float) -> float:
    return round(float(x), 2)


def _char_sizes(words: list[dict]) -> list[float]:
    sizes = []
    for w in words:
        sizes += [float(w["size"])] * max(1, len(w["text"]))
    return sizes


def _bold_share(words: list[dict]) -> float:
    total = sum(len(w["text"]) for w in words)
    bold = sum(len(w["text"]) for w in words if w.get("bold"))
    return round(bold / total, 4) if total else 0.0


def _bbox(words: list[dict]) -> list[float]:
    return [_r(min(w["x0"] for w in words)), _r(min(w["top"] for w in words)),
            _r(max(w["x1"] for w in words)), _r(max(w["bottom"] for w in words))]


def group_rows(words: list[dict], row_tolerance_ratio: float) -> list[list[dict]]:
    """Words grouped into visual rows (sorted top to bottom, each row left to right)."""
    rows: list[list[dict]] = []
    ref_top = ref_size = 0.0
    for w in sorted(words, key=lambda w: (round(w["top"], 2), w["x0"])):
        size = float(w["size"]) or 1.0
        if rows and abs(w["top"] - ref_top) <= row_tolerance_ratio * max(size, ref_size):
            rows[-1].append(w)
        else:
            rows.append([w])
            ref_top, ref_size = w["top"], size
    return [sorted(r, key=lambda w: w["x0"]) for r in rows]


def split_segments(row: list[dict], segment_gap_ratio: float) -> list[list[dict]]:
    """Split one row (sorted left to right) where a gap is wide for its font size."""
    gap_limit = segment_gap_ratio * _median(_char_sizes(row), 10.0)
    segments = [[row[0]]]
    for prev, w in zip(row, row[1:]):
        if w["x0"] - prev["x1"] >= gap_limit:
            segments.append([w])
        else:
            segments[-1].append(w)
    return segments


def make_line(words: list[dict], page: int, **extra) -> dict:
    line = {
        "page": page,
        "text": " ".join(w["text"] for w in words),
        "bbox": _bbox(words),
        "font_size": _r(_median(_char_sizes(words))),
        "bold": _bold_share(words),
        "in_table": False,
        "cell_row": None,
        "cell_col": None,
    }
    line.update(extra)
    return line


def number_lines(lines: list[dict], page: int) -> list[dict]:
    """Give lines (already in reading order) their id and reading_order."""
    out = []
    for i, line in enumerate(lines, start=1):
        line = dict(line, id=f"p{page:03d}_l{i:04d}", reading_order=i)
        out.append({k: line[k] for k in sorted(line)})
    return out


def _center_in(w: dict, bbox) -> bool:
    x0, top, x1, bottom = bbox
    cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
    return x0 <= cx <= x1 and top <= cy <= bottom


def words_to_lines(words: list[dict], page: int, params: dict, tables: list | None = None) -> list[dict]:
    """Lines for one page, in reading order: rows top to bottom, tables cell by cell.

    tables: list of (table_bbox, rows), rows a list of lists of cell bboxes (None for a
    merged cell). Words inside a table cell are read only as part of that cell.
    """
    tables = tables or []
    in_cell: dict[int, tuple[int, int, int]] = {}
    for ti, (_tb, rows) in enumerate(tables):
        for r, cells in enumerate(rows):
            for c, cell in enumerate(cells):
                if cell is None:
                    continue
                for i, w in enumerate(words):
                    if i not in in_cell and _center_in(w, cell):
                        in_cell[i] = (ti, r, c)

    keyed: list[tuple[tuple, dict]] = []
    free = [w for i, w in enumerate(words) if i not in in_cell]
    for k, row in enumerate(group_rows(free, params["row_tolerance_ratio"]), start=1):
        row_id = f"p{page:03d}_r{k:04d}"
        top = min(w["top"] for w in row)
        for s, seg in enumerate(split_segments(row, params["segment_gap_ratio"])):
            keyed.append(((top, 0, k, s), make_line(seg, page, row_id=row_id, segment_index=s)))

    for ti, (tb, _rows) in enumerate(tables):
        cells: dict[tuple[int, int], list[dict]] = {}
        for i, (t, r, c) in in_cell.items():
            if t == ti:
                cells.setdefault((r, c), []).append(words[i])
        for (r, c), cell_words in sorted(cells.items()):
            ordered = [w for row in group_rows(cell_words, params["row_tolerance_ratio"]) for w in row]
            line = make_line(ordered, page, in_table=True, cell_row=r, cell_col=c,
                             row_id=f"p{page:03d}_t{ti + 1}r{r:02d}", segment_index=c)
            keyed.append(((float(tb[1]), 1, r, c), line))

    keyed.sort(key=lambda kv: kv[0])
    return number_lines([line for _, line in keyed], page)


def assign_rows(lines: list[dict], page: int, row_tolerance_ratio: float) -> list[dict]:
    """row_id and segment_index for lines that came without them (pdfminer)."""
    order = sorted(range(len(lines)), key=lambda i: (lines[i]["bbox"][1], lines[i]["bbox"][0]))
    row_of: dict[int, tuple[int, int]] = {}
    k, ref_top, ref_size, members = 0, None, 0.0, []
    for i in order:
        top, size = lines[i]["bbox"][1], lines[i]["font_size"] or 1.0
        if ref_top is None or abs(top - ref_top) > row_tolerance_ratio * max(size, ref_size):
            k, ref_top, ref_size, members = k + 1, top, size, []
        members.append(i)
        row_of[i] = (k, len(members) - 1)
    out = []
    for i, line in enumerate(lines):
        k, s = row_of[i]
        out.append(dict(line, row_id=f"p{page:03d}_r{k:04d}", segment_index=s))
    return out


# ---------------------------------------------------------------- label-free checks


def text_of(lines: list[dict] | None) -> str:
    return "\n".join(line["text"] for line in lines or [])


def garbled_rate(text: str) -> float:
    """Share of characters that are (cid:N) codes or replacement characters (U+FFFD).
    Each (cid:N) counts as one character. Private-use characters are not counted: they are
    icon-font glyphs (phone, email, link symbols), which stage 2 strips as icons."""
    cids = len(_CID.findall(text))
    rest = _CID.sub("", text)
    bad = cids + rest.count("�")
    total = cids + sum(1 for ch in rest if not ch.isspace())
    return round(bad / total, 4) if total else 0.0


_SKIP_TOKENS = re.compile(
    r"\S+@\S+\.\S+"                        # emails
    r"|(?:https?://|www\.)\S+"             # URLs
    r"|\S+\.(?:com|org|net|edu|ac\.\w+|in|io)(?:/\S*)?\b"  # bare web addresses
    r"|\bdoi:\s*\S+|\b10\.\d{4,9}/\S+",     # DOIs
    re.IGNORECASE,
)
_CAMEL_JOIN = re.compile(r"[a-z]{3}[A-Z][a-z]")


def glued_words(text: str, params: dict) -> tuple[float, int, int]:
    """(rate, glued count, word count). Emails, URLs and DOIs are left out. A word counts as
    glued when it has a run of glued_min_letters+ letters, or 3+ lowercase letters followed
    by an Uppercase letter and a lowercase letter (as in "ProfessorDepartment"). Words in
    glued_ignore (PhD, LaTeX, ...) never count."""
    ignore = {w.lower() for w in params["glued_ignore"]}
    long_run = re.compile(r"[^\W\d_]{%d,}" % int(params["glued_min_letters"]))
    words = glued = 0
    for token in _SKIP_TOKENS.sub(" ", text).split():
        token = token.strip(".,;:()[]{}\"'!?")
        if not any(ch.isalpha() for ch in token):
            continue
        words += 1
        if token.lower() in ignore:
            continue
        if long_run.search(token) or _CAMEL_JOIN.search(token):
            glued += 1
    return (round(glued / words, 4) if words else 0.0), glued, words


def word_bag(text: str) -> Counter:
    return Counter(re.findall(r"\w+", text.lower()))


def word_overlap(a: str, b: str) -> float | None:
    """Order-insensitive agreement of two texts: shared words (as a multiset of lowercase
    words) over the larger word count. None when both are empty."""
    ca, cb = word_bag(a), word_bag(b)
    na, nb = sum(ca.values()), sum(cb.values())
    if not na and not nb:
        return None
    return round(sum((ca & cb).values()) / max(na, nb), 4)
