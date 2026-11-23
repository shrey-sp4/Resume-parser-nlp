"""Varied synthetic CVs for the synthetic sweep (eval/synthetic_sweep.py).

Every CV is one seeded combination of layout factors (name header, heading wording and style,
entry formats, publication style, page structure, layout), rendered with reportlab into a
temporary folder, together with its own GT v2 record built from the same invented content.
Every name, institution, company, venue and title comes from the word lists below: all made up,
none taken from any real CV. Deterministic: combo(seed) always gives the same factors and text.

    from synthetic_sweep_gen import combo, build
    spec = combo(7)              # factors and content for seed 7
    pdf, gt = build(spec, folder)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

WIDTH, HEIGHT = A4
MARGIN = 54
TOP, BOTTOM = HEIGHT - MARGIN, MARGIN + 30

# ---------------------------------------------------------------- invented word lists

FIRST = ["Talvi", "Oreno", "Mirsa", "Quenby", "Ilvar", "Daska", "Feyra", "Brannoc", "Selwyn", "Varna", "Ottel", "Jessamy"]
LAST = ["Ondrake", "Pellistor", "Quarne", "Vesselby", "Hartmoor", "Zellwin", "Corvane", "Mistrel", "Ashgrove",
        "Tannerly", "Brookhallow", "Fenwright"]
PLACES = ["Northmere", "Calloway Ridge", "Westbrook Vale", "Emberfield", "Silverlake", "Harrowgate", "Duskmoor",
          "Pinecrest", "Lowmarsh", "Greyhaven", "Amberly", "Coldwater"]
UNI_KINDS = ["University", "Institute of Technology", "College of Science", "University of Applied Arts",
             "Institute of Advanced Study"]
COMPANY_KINDS = ["Labs Pvt Ltd", "Systems Ltd", "Instruments Pvt Ltd", "Analytics Inc", "Technologies Pvt Ltd"]
COMPANY_STEMS = ["Glimmerworks", "Quillspark", "Echoforge", "Brightloom", "Mossfield", "Tinderbox"]
DEGREES = [("Ph.D.", "doctorate"), ("M.Sc.", "masters"), ("M.Tech.", "masters"), ("M.A.", "masters"),
           ("B.Sc.", "bachelors"), ("B.Tech.", "bachelors")]
SUBJECTS = ["Imaginary Acoustics", "Toy Chemistry", "Pretend Optics", "Fictional Ecology", "Invented Geology",
            "Make-Believe Robotics", "Notional Linguistics", "Hypothetical Botany"]
ACADEMIC_TITLES = ["Assistant Professor", "Associate Professor", "Lecturer", "Postdoctoral Fellow",
                   "Senior Research Fellow", "Research Associate"]
INDUSTRY_TITLES = ["Research Engineer", "Data Scientist", "Software Engineer", "Senior Research Engineer"]
FIELDS = ["Fictional Acoustics", "Pretend Materials", "Invented Systems", "Toy Photonics", "Imaginary Networks",
          "Notional Computing", "Made-Up Ecology"]
ADJ = ["Quiet", "Folded", "Silent", "Hollow", "Bright", "Drifting", "Woven", "Scattered", "Gentle", "Tangled"]
NOUN = ["lattices", "membranes", "echoes", "filaments", "gradients", "meadows", "circuits", "tides", "spirals", "ridges"]
CONTEXT = ["invented halls", "pretend crystals", "toy fibres", "imaginary rivers", "made-up forests",
           "notional cities", "fictional reefs", "unbuilt towers"]
BULLETS = ["Taught invented courses to fictional students", "Built pretend sensors for imaginary clients",
           "Ran a make-believe laboratory", "Supervised notional projects"]

# ---------------------------------------------------------------- factors

FACTORS = {
    "layout": ["one_column", "two_columns", "sidebar_left", "sidebar_right", "tables"],
    "wrap": ["hanging", "flush"],
    "name_align": ["left", "center"],
    "name_prefix": ["none", "Dr.", "Prof."],
    "name_suffix": ["none", "Ph.D."],
    "name_caps": ["no", "yes"],
    "name_two_line": ["no", "yes"],
    "name_position": ["first", "after_contact"],
    "heading_alias": ["plain", "variant_1", "variant_2", "variant_3"],
    "heading_case": ["title", "caps"],
    "heading_mark": ["none", "colon", "numbered"],
    "heading_style": ["bold", "larger", "ruled", "bar"],
    "edu_format": ["degree_first", "institution_first", "multi_line"],
    "year_format": ["single", "range", "months", "present"],
    "edu_grade": ["no", "yes"],
    "exp_format": ["title_first", "institution_first", "date_left", "date_right"],
    "exp_bullets": ["no", "yes"],
    "exp_dates": ["range", "months", "present", "onwards"],
    "pub_marker": ["bracket", "dot", "paren", "bullet", "none_indent", "none_blank"],
    "pub_style": ["default", "apa", "ieee", "comma"],
    "pub_subheadings": ["no", "yes"],
    "author_style": ["initials_first", "surname_first", "full_names"],
    "pages": ["one", "two", "three"],
    "header_footer": ["none", "header", "footer_page_numbers", "both"],
}
HEADINGS = {
    "education": ["Education", "Academic Qualifications", "Educational Background", "Education and Training"],
    "experience": ["Experience", "Work Experience", "Employment History", "Academic Appointments"],
    "publications": ["Publications", "Selected Publications", "List of Publications", "Research Papers"],
}
ALIAS_INDEX = {"plain": 0, "variant_1": 1, "variant_2": 2, "variant_3": 3}


@dataclass
class Spec:
    seed: int
    factors: dict
    name_first: str
    name_last: str
    education: list = field(default_factory=list)   # (render dict, gt dict)
    experience: list = field(default_factory=list)
    publications: list = field(default_factory=list)
    skills: list = field(default_factory=list)


def _years(r: random.Random, fmt: str, end: int) -> str:
    start = end - r.choice([2, 3, 4, 5])
    return {"single": str(end), "range": f"{start} - {end}", "months": f"Jan {start} - Jun {end}",
            "present": f"{start} - Present"}[fmt]


def _dates(r: random.Random, fmt: str, end: int) -> str:
    start = end - r.choice([2, 3, 4])
    return {"range": f"{start} - {end}", "months": f"Jul {start} - Jun {end}", "present": f"{start} - Present",
            "onwards": f"{start} onwards"}[fmt]


def _authors(r: random.Random, style: str, first: str, last: str) -> str:
    co = [(r.choice(FIRST), r.choice(LAST)) for _ in range(r.choice([0, 1, 2]))]
    people = [(first, last)] + co
    if style == "initials_first":
        names = [f"{f[0]}. {l}" for f, l in people]
    elif style == "surname_first":
        names = [f"{l}, {f[0]}." for f, l in people]
    else:
        names = [f"{f} {l}" for f, l in people]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def combo(seed: int) -> Spec:
    r = random.Random(seed)
    f = {k: r.choice(v) for k, v in FACTORS.items()}
    if f["layout"] == "tables" and f["edu_format"] == "multi_line":
        f["edu_format"] = "degree_first"
    spec = Spec(seed=seed, factors=f, name_first=r.choice(FIRST), name_last=r.choice(LAST))
    used_inst = set()

    def inst():
        while True:
            name = f"{r.choice(PLACES)} {r.choice(UNI_KINDS)}"
            if name not in used_inst:
                used_inst.add(name)
                return name

    end = 2024
    for _ in range(r.choice([2, 3, 4])):
        deg, level = r.choice(DEGREES)
        subject, place = r.choice(SUBJECTS), inst()
        years = _years(r, f["year_format"], end)
        end -= r.choice([2, 3, 4])
        spec.education.append(({"degree": deg, "stream": subject, "institution": place, "years": years,
                                "grade": f"CGPA: {r.choice(['8.1', '8.6', '9.0', '7.8'])}/10"},
                               {"degree": deg, "degree_level": level, "stream": subject, "institution": place}))
    end = 2025
    for k in range(r.choice([2, 3, 4])):
        if r.random() < 0.6:
            title, place, kind = r.choice(ACADEMIC_TITLES), inst(), "academic"
        else:
            title = r.choice(INDUSTRY_TITLES)
            place, kind = f"{r.choice(COMPANY_STEMS)} {r.choice(COMPANY_KINDS)}", "industry"
        fmt = f["exp_dates"] if k == 0 else ("months" if f["exp_dates"] == "months" else "range")
        dates = _dates(r, fmt, end)
        end -= r.choice([2, 3, 4])
        spec.experience.append(({"title": title, "institution": place, "dates": dates,
                                 "bullets": r.sample(BULLETS, 2)},
                                {"job_title": title, "institution": place, "dates": dates, "experience_type": kind}))
    for k in range(r.choice([2, 3, 4])):
        title = f"{r.choice(ADJ)} {r.choice(NOUN)} in {r.choice(CONTEXT)}"
        kind = r.choice(["journal", "conference"])
        venue = (r.choice([f"Journal of {r.choice(FIELDS)}", f"International Journal of {r.choice(FIELDS)}",
                           f"{r.choice(FIELDS)} Letters"]) if kind == "journal" else
                 r.choice([f"Proc. International Conference on {r.choice(FIELDS)}",
                           f"International Symposium on {r.choice(FIELDS)}"]))
        year = str(2024 - k - r.choice([0, 1]))
        spec.publications.append(({"authors": _authors(r, f["author_style"], spec.name_first, spec.name_last),
                                   "title": title, "venue": venue, "year": year},
                                  {"title": title, "type": kind, "year": year, "venue": venue}))
    if f["pub_subheadings"] == "yes":
        spec.publications.sort(key=lambda p: p[1]["type"] != "journal")
    spec.skills = r.sample(["Signal theory", "Room modelling", "Soldering", "Field sampling", "Glassblowing",
                            "Map drawing"], 3)
    return spec


def ground_truth(spec: Spec, stem: str) -> dict:
    name = f"{spec.name_first} {spec.name_last}"
    if spec.factors["name_caps"] == "yes":
        name = name.upper()
    return {"resume_file": f"{stem}.pdf", "candidate_name": name,
            "education": [g for _, g in spec.education], "experience": [g for _, g in spec.experience],
            "publications": [g for _, g in spec.publications],
            "stated_totals": {"journal": None, "conference": None, "total": None}, "notes": ""}


# ---------------------------------------------------------------- text of each entry


def edu_lines(e: dict, f: dict) -> list[str]:
    deg = f"{e['degree']} in {e['stream']}"
    if f["edu_format"] == "degree_first":
        out = [f"{deg}, {e['institution']}, {e['years']}"]
    elif f["edu_format"] == "institution_first":
        out = [f"{e['institution']}, {deg}, {e['years']}"]
    else:
        out = [deg, e["institution"], e["years"]]
    if f["edu_grade"] == "yes":
        out.append(e["grade"])
    return out


def exp_lines(e: dict, f: dict) -> list[tuple[str, str]]:
    """(left text, right-aligned text) pairs."""
    fmt = f["exp_format"]
    if fmt == "title_first":
        out = [(f"{e['title']}, {e['institution']}, {e['dates']}", "")]
    elif fmt == "institution_first":
        out = [(e["institution"], ""), (f"{e['title']}, {e['dates']}", "")]
    elif fmt == "date_left":
        out = [(f"{e['dates']}  {e['title']}, {e['institution']}", "")]
    else:
        out = [(f"{e['title']}, {e['institution']}", e["dates"])]
    if f["exp_bullets"] == "yes":
        out += [(f"- {b}", "") for b in e["bullets"]]
    return out


def pub_text(p: dict, f: dict) -> str:
    style = f["pub_style"]
    authors = p["authors"].rstrip(".")
    if style == "apa":
        return f"{p['authors']} ({p['year']}). {p['title']}. {p['venue']}."
    if style == "ieee":
        return f"{p['authors']}, “{p['title']},” {p['venue']}, {p['year']}."
    if style == "comma":
        return f"{p['authors']}, {p['title']}, {p['venue']}, {p['year']}."
    return f"{authors}. {p['title']}. {p['venue']}, {p['year']}."


def pub_marker(k: int, f: dict) -> str:
    return {"bracket": f"[{k}] ", "dot": f"{k}. ", "paren": f"({k}) ", "bullet": "- ",
            "none_indent": "", "none_blank": ""}[f["pub_marker"]]


def heading_text(section: str, f: dict, number: int) -> str:
    text = HEADINGS[section][ALIAS_INDEX[f["heading_alias"]]]
    if f["heading_case"] == "caps":
        text = text.upper()
    if f["heading_mark"] == "colon":
        text += ":"
    elif f["heading_mark"] == "numbered":
        text = f"{number}. {text}"
    return text


# ---------------------------------------------------------------- layout pass

@dataclass
class Op:
    page: int
    kind: str          # text, right, rule, bar, cell_grid
    x: float
    y: float
    text: str = ""
    font: str = "Helvetica"
    size: float = 9.5
    width: float = 0.0
    extra: dict = field(default_factory=dict)


class Region:
    """A column of the page; lines flow down and continue on the next page at the same x."""

    def __init__(self, x: float, width: float, top: float, page: int, pages_target: int):
        self.x, self.width, self.top, self.y, self.page = x, width, top, top, page
        self.ops: list[Op] = []
        self.pages_target = pages_target

    def need(self, h: float):
        if self.y - h < BOTTOM:
            self.page += 1
            self.y = TOP - 14

    def text(self, text, font="Helvetica", size=9.5, step=13.0, indent=0.0, hanging=0.0, right=""):
        # keep room for right-aligned text and for the hanging indent of wrapped parts, so no
        # line runs past the column edge or under the right-aligned dates
        reserve = (_w(right, "Helvetica", size) + 12) if right else 0
        width = self.width - indent - reserve - hanging
        parts = simpleSplit(text, font, size, width) or [""]
        for k, part in enumerate(parts):
            self.need(step)
            self.ops.append(Op(self.page, "text", self.x + indent + (hanging if k else 0), self.y, part, font, size))
            if right and k == 0:
                self.ops.append(Op(self.page, "right", self.x + self.width, self.y, right, "Helvetica", size))
            self.y -= step

    def gap(self, h):
        self.y -= h

    def page_break(self):
        self.page += 1
        self.y = TOP - 14


def _heading(reg: Region, text: str, f: dict):
    style = f["heading_style"]
    reg.gap(8)
    reg.need(30)
    size = 14 if style == "larger" else 11.5
    font = "Helvetica" if style == "larger" else "Helvetica-Bold"
    if style == "bar":
        reg.ops.append(Op(reg.page, "bar", reg.x - 2, reg.y - 3, width=reg.width + 4))
    reg.ops.append(Op(reg.page, "text", reg.x, reg.y, text, font, size))
    if style == "ruled":
        reg.ops.append(Op(reg.page, "rule", reg.x, reg.y - 4, width=reg.width))
    reg.y -= 20


def _table(reg: Region, header: list[str], rows: list[list[str]], widths: list[float]):
    """A ruled table; each cell's text wraps inside its cell."""
    size, step = 8.5, 11
    all_rows = [header] + rows
    reg.gap(2)
    for i, row in enumerate(all_rows):
        cells = [simpleSplit(t, "Helvetica-Bold" if i == 0 else "Helvetica", size, w - 6) or [""] for t, w in zip(row, widths)]
        h = max(len(c) for c in cells) * step + 6
        reg.need(h)
        top = reg.y + 10
        x = reg.x
        for c, w in zip(cells, widths):
            reg.ops.append(Op(reg.page, "cell_grid", x, top - h, width=w, extra={"h": h}))
            for k, part in enumerate(c):
                reg.ops.append(Op(reg.page, "text", x + 3, top - 9 - k * step, part,
                                  "Helvetica-Bold" if i == 0 else "Helvetica", size))
            x += w
        reg.y -= h


def _section(reg: Region, section: str, spec: Spec, number: int):
    f = spec.factors
    # a page break inside the section, after its first entry: two pages break experience,
    # three pages break education and publications
    breaks = {"one": (), "two": ("experience",), "three": ("education", "publications")}[f["pages"]]
    _heading(reg, heading_text(section, f, number), f)
    hang = 12.0 if f["wrap"] == "hanging" else 0.0
    if section == "education":
        if f["layout"] == "tables":
            w = reg.width
            _table(reg, ["Degree", "Institution", "Year"],
                   [[f"{e['degree']} in {e['stream']}", e["institution"], e["years"]] for e, _ in spec.education],
                   [w * 0.38, w * 0.42, w * 0.20])
            return
        for i, (e, _) in enumerate(spec.education):
            if i == 1 and "education" in breaks:
                reg.page_break()
            for k, line in enumerate(edu_lines(e, f)):
                reg.text(line, hanging=hang, indent=0 if k == 0 else (8 if f["edu_format"] == "multi_line" else 0))
            reg.gap(3)
    elif section == "experience":
        if f["layout"] == "tables":
            w = reg.width
            _table(reg, ["Designation", "Organisation", "Duration"],
                   [[e["title"], e["institution"], e["dates"]] for e, _ in spec.experience],
                   [w * 0.32, w * 0.43, w * 0.25])
            return
        for i, (e, _) in enumerate(spec.experience):
            if i == 1 and "experience" in breaks:
                reg.page_break()
            for k, (left, right) in enumerate(exp_lines(e, f)):
                reg.text(left, hanging=hang, right=right, indent=10 if left.startswith("- ") else 0)
            reg.gap(3)
    else:
        groups = [("", spec.publications)]
        if f["pub_subheadings"] == "yes":
            groups = [("Journal Articles", [p for p in spec.publications if p[1]["type"] == "journal"]),
                      ("Conference Papers", [p for p in spec.publications if p[1]["type"] == "conference"])]
        k = 0
        for sub, pubs in groups:
            if sub and pubs:
                reg.need(16)
                reg.ops.append(Op(reg.page, "text", reg.x, reg.y, sub, "Helvetica-Bold", 10))
                reg.y -= 15
            for p, _ in pubs:
                k += 1
                if k == 2 and "publications" in breaks:
                    reg.page_break()
                indent = 14 if f["pub_marker"] in ("none_indent", "bracket", "dot", "paren", "bullet") else 0
                reg.text(pub_marker(k, f) + pub_text(p, f), hanging=indent)
                reg.gap(8 if f["pub_marker"] == "none_blank" else 2)


def layout_ops(spec: Spec) -> list[Op]:
    f = spec.factors
    name = f"{spec.name_first} {spec.name_last}"
    if f["name_caps"] == "yes":
        name = name.upper()
    shown = (f"{f['name_prefix']} " if f["name_prefix"] != "none" else "") + name + \
        (", Ph.D." if f["name_suffix"] == "Ph.D." else "")
    contact = [f"{spec.name_first.lower()}.{spec.name_last.lower()}@example.org", "+00 0000 000 042"]
    full = Region(MARGIN, WIDTH - 2 * MARGIN, TOP, 0, 1)

    def name_block(reg: Region):
        parts = shown.split(" ", 1) if f["name_two_line"] == "yes" and " " in shown else [shown]
        for part in parts:
            x = reg.x + (reg.width - _w(part, "Helvetica-Bold", 16)) / 2 if f["name_align"] == "center" else reg.x
            reg.ops.append(Op(reg.page, "text", x, reg.y, part, "Helvetica-Bold", 16))
            reg.y -= 21

    def contact_block(reg: Region):
        for line in contact:
            reg.ops.append(Op(reg.page, "text", reg.x, reg.y, line, "Helvetica", 9))
            reg.y -= 12

    sections = ["education", "experience", "publications"]
    side_layout = f["layout"] in ("sidebar_left", "sidebar_right")
    if not side_layout:
        if f["name_position"] == "first":
            name_block(full); contact_block(full)
        else:
            contact_block(full); full.gap(4); name_block(full)
        full.gap(6)
    ops: list[Op] = []
    if f["layout"] in ("one_column", "tables"):
        reg = full
        for n, s in enumerate(sections, 1):
            _section(reg, s, spec, n + 1)
        _heading(reg, heading_text_other("Skills", f), f)
        reg.text(", ".join(spec.skills))
        ops = reg.ops
    elif f["layout"] == "two_columns":
        top = full.y
        col = (WIDTH - 2 * MARGIN - 24) / 2
        left = Region(MARGIN, col, top, 0, 1)
        right = Region(MARGIN + col + 24, col, top, 0, 1)
        _section(left, "education", spec, 2)
        _section(left, "experience", spec, 3)
        _section(right, "publications", spec, 4)
        _heading(right, heading_text_other("Skills", f), f)
        right.text(", ".join(spec.skills))
        ops = full.ops + left.ops + right.ops
    else:
        side_w = 140
        main_x = MARGIN + side_w + 24 if f["layout"] == "sidebar_left" else MARGIN
        side_x = MARGIN if f["layout"] == "sidebar_left" else WIDTH - MARGIN - side_w
        main = Region(main_x, WIDTH - 2 * MARGIN - side_w - 24, TOP, 0, 1)
        side = Region(side_x, side_w, TOP, 0, 1)
        name_block(main)
        main.gap(6)
        _heading(side, heading_text_other("Contact", f), f)
        for line in contact:
            side.text(line, size=8.5, step=12)
        _heading(side, heading_text_other("Skills", f), f)
        for s in spec.skills:
            side.text(s, size=8.5, step=12)
        for n, s in enumerate(sections, 1):
            _section(main, s, spec, n + 1)
        ops = main.ops + side.ops
    return ops


def heading_text_other(text: str, f: dict) -> str:
    if f["heading_case"] == "caps":
        text = text.upper()
    if f["heading_mark"] == "colon":
        text += ":"
    return text


def _w(text: str, font: str, size: float) -> float:
    from reportlab.pdfbase.pdfmetrics import stringWidth
    return stringWidth(text, font, size)


# ---------------------------------------------------------------- drawing pass


def build(spec: Spec, folder: Path) -> tuple[Path, dict]:
    """Render the CV to folder/<stem>.pdf and return (path, GT v2 record)."""
    stem = f"sweep_{spec.seed:04d}"
    path = Path(folder) / f"{stem}.pdf"
    ops = layout_ops(spec)
    n_pages = max(o.page for o in ops) + 1
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    c.setTitle("synthetic sweep cv")
    c.setAuthor("synthetic")
    hf = spec.factors["header_footer"]
    for page in range(n_pages):
        if hf in ("header", "both"):
            c.setFont("Helvetica", 8)
            c.drawString(MARGIN, HEIGHT - 30, f"Curriculum Vitae of {spec.name_first} {spec.name_last}")
        if hf in ("footer_page_numbers", "both"):
            c.setFont("Helvetica", 8)
            c.drawCentredString(WIDTH / 2, 28, f"Page {page + 1} of {n_pages}")
        for o in ops:
            if o.page != page:
                continue
            if o.kind == "text":
                c.setFont(o.font, o.size)
                c.drawString(o.x, o.y, o.text)
            elif o.kind == "right":
                c.setFont(o.font, o.size)
                c.drawRightString(o.x, o.y, o.text)
            elif o.kind == "rule":
                c.setLineWidth(0.6)
                c.line(o.x, o.y, o.x + o.width, o.y)
            elif o.kind == "bar":
                c.setFillGray(0.88)
                c.rect(o.x, o.y, o.width, 15, stroke=0, fill=1)
                c.setFillGray(0)
            elif o.kind == "cell_grid":
                c.setLineWidth(0.5)
                c.rect(o.x, o.y, o.width, o.extra["h"], stroke=1, fill=0)
        c.showPage()
    c.save()
    return path, ground_truth(spec, stem)


def entry_keys(spec: Spec) -> dict[str, list[str]]:
    """A short, distinctive piece of text per entry, to find it in the extracted lines."""
    return {"education": [e["institution"] for e, _ in spec.education],
            "experience": [e["institution"] for e, _ in spec.experience],
            "publications": [" ".join(p["title"].split()[:3]) for p, _ in spec.publications]}
