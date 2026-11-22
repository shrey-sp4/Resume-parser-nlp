"""Build fake academic resumes as PDFs for tests. All names and data are invented.

    python scripts/make_synthetic_resumes.py <out_dir>

Builds three resumes:
  synthetic_a_simple.pdf    single column, plain headings
  synthetic_b_table.pdf     education given as a ruled table
  synthetic_c_variants.pdf  different heading wording and bullet styles

Also gives ground truth (GT v2) for the three resumes, for the evaluator tests, and has
helpers for page-profile tests: a text-only page, an image-only page
(a drawn raster, no text) and a mixed page. LAYOUTS builds one invented resume in four page
layouts (one column, two columns, left sidebar, tables) for the reading-order tests.

PDFs are built with reportlab's invariant mode, so the same input gives the
same bytes. Never commit the PDFs; tests build them in a temp folder.
"""

from __future__ import annotations

import sys
from pathlib import Path

from reportlab import rl_config

rl_config.invariant = 1  # no creation date, fixed document id

from PIL import Image, ImageDraw  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import getSampleStyleSheet  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle  # noqa: E402

WIDTH, HEIGHT = A4
MARGIN = 56

# (style, text). Styles: name, contact, heading, body.
RESUME_A = [
    ("name", "Mira Testwood"),
    ("contact", "mira.testwood@example.org | +00 0000 000 001 | Fakeville"),
    ("heading", "EDUCATION"),
    ("body", "Ph.D. in Computational Biology, Northfield Institute of Science, 2016 - 2021"),
    ("body", "M.Sc. in Applied Mathematics, Lakeside University, 2014 - 2016"),
    ("body", "B.Sc. in Mathematics, Riverbend College, 2011 - 2014"),
    ("heading", "WORK EXPERIENCE"),
    ("body", "Assistant Professor, Department of Biology, Hillcrest University, Jul 2023 - Present"),
    ("body", "Postdoctoral Fellow, Northfield Institute of Science, Aug 2021 - Jun 2023"),
    ("body", "Data Analyst, Examplecorp Analytics Pvt Ltd, Jan 2014 - Jun 2014"),
    ("heading", "PUBLICATIONS"),
    ("body", "1. M. Testwood, R. Doe. Modelling gene networks with sparse graphs. Journal of Invented Biology, 2022."),
    ("body", "2. M. Testwood. Fast alignment of made-up sequences. Proc. Imaginary Conference on Bioinformatics, 2020."),
    ("body", "Total publications: 2"),
    ("heading", "SKILLS"),
    ("body", "Python, R, statistics, teaching"),
]

RESUME_B_TOP = [
    ("name", "Jonas Fakely"),
    ("contact", "jonas.fakely@example.org | +00 0000 000 002"),
]
RESUME_B_EDU = [
    ["Degree", "Discipline", "Institution", "Year"],
    ["Ph.D.", "Physics", "Westbrook University", "2019"],
    ["M.Tech.", "Materials Science", "Eastvale Institute of Technology", "2014"],
    ["B.Tech.", "Engineering Physics", "Southgate College of Engineering", "2012"],
]
RESUME_B_REST = [
    ("heading", "Professional Experience"),
    ("body", "Associate Professor, School of Physics, Westbrook University, 2021 - present"),
    ("body", "Research Scientist, Imaginary Labs Inc, 2019 - 2021"),
    ("heading", "Journal Publications"),
    ("body", "J. Fakely, A. Roe. Thin films that do not exist. Invented Physics Letters 12 (2021) 1-9."),
    ("body", "J. Fakely. Phonons in fictional crystals. Journal of Pretend Materials 4 (2018) 33-40."),
    ("heading", "Conference Papers"),
    ("body", "J. Fakely. A sample talk title. Proceedings of the Placeholder Symposium, 2017."),
]

RESUME_C = [
    ("name", "Dr. Lena Samplestone"),
    ("contact", "lena.samplestone@example.org  |  +00 0000 000 003  |  orcid.org/0000-0000-0000-0000"),
    ("heading", "Academic Qualifications"),
    ("body", "• Doctor of Philosophy (Economics) - Greenhill University (2015)"),
    ("body", "• Master of Arts (Economics) - Greenhill University (2010)"),
    ("body", "• Bachelor of Arts (Honours) - Stonebridge College (2008)"),
    ("heading", "Employment History"),
    ("body", "– Professor, Faculty of Economics, Maplewood University (2020 – till date)"),
    ("body", "– Lecturer, Maplewood University (2015 – 2020)"),
    ("heading", "Research Output"),
    ("body", "* Samplestone, L. (2021). Prices in an invented market. Review of Fictional Economics, 8(2)."),
    ("body", "* Samplestone, L. and Poe, K. (2019). Trade between imaginary towns. Placeholder Economic Journal."),
    ("heading", "Courses Taught"),
    ("body", "o Microeconomics, Econometrics, Game Theory"),
]

FONTS = {
    "name": ("Helvetica-Bold", 16, 24),
    "contact": ("Helvetica", 9, 20),
    "heading": ("Helvetica-Bold", 12, 22),
    "body": ("Helvetica", 9.5, 14),
}


def _draw_lines(c: canvas.Canvas, lines: list[tuple[str, str]], y: float) -> float:
    for style, text in lines:
        font, size, step = FONTS[style]
        if style == "heading":
            y -= 6
        c.setFont(font, size)
        c.drawString(MARGIN, y, text)
        y -= step
    return y


def _new_canvas(path: Path, title: str) -> canvas.Canvas:
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    c.setTitle(title)
    c.setAuthor("synthetic")
    return c


def make_simple(path: Path) -> Path:
    c = _new_canvas(path, "synthetic resume a")
    _draw_lines(c, RESUME_A, HEIGHT - MARGIN)
    c.showPage()
    c.save()
    return path


def make_with_table(path: Path) -> Path:
    styles = getSampleStyleSheet()
    styles_map = {"name": styles["Title"], "contact": styles["Normal"], "heading": styles["Heading2"], "body": styles["Normal"]}
    story = [Paragraph(text, styles_map[style]) for style, text in RESUME_B_TOP]
    story += [Spacer(1, 8), Paragraph("Education", styles["Heading2"])]
    table = Table(RESUME_B_EDU, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]
        )
    )
    story.append(table)
    story += [Paragraph(text, styles_map[style]) for style, text in RESUME_B_REST]
    # invariant mode comes from rl_config, set at the top of this file
    doc = SimpleDocTemplate(str(path), pagesize=A4, title="synthetic resume b", author="synthetic")
    doc.build(story)
    return path


def make_variants(path: Path) -> Path:
    c = _new_canvas(path, "synthetic resume c")
    _draw_lines(c, RESUME_C, HEIGHT - MARGIN)
    c.showPage()
    c.save()
    return path


def build_all(out_dir: str | Path) -> list[Path]:
    """Build the three fake resumes into out_dir and return their paths, sorted."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return [
        make_simple(out_dir / "synthetic_a_simple.pdf"),
        make_with_table(out_dir / "synthetic_b_table.pdf"),
        make_variants(out_dir / "synthetic_c_variants.pdf"),
    ]


# ---------------------------------------------------------------- ground truth


def _edu(degree, level, stream, institution):
    return {"degree": degree, "degree_level": level, "stream": stream, "institution": institution}


def _exp(title, institution, dates, kind):
    return {"job_title": title, "institution": institution, "dates": dates, "experience_type": kind}


def _pub(title, kind, year, venue):
    return {"title": title, "type": kind, "year": year, "venue": venue}


def synthetic_ground_truth() -> dict[str, dict]:
    """GT v2 files for the three fake resumes, keyed by file stem. Every field is filled."""
    return {
        "synthetic_a_simple": {
            "resume_file": "synthetic_a_simple.pdf",
            "candidate_name": "Mira Testwood",
            "education": [
                _edu("Ph.D.", "doctorate", "Computational Biology", "Northfield Institute of Science"),
                _edu("M.Sc.", "masters", "Applied Mathematics", "Lakeside University"),
                _edu("B.Sc.", "bachelors", "Mathematics", "Riverbend College"),
            ],
            "experience": [
                _exp("Assistant Professor", "Department of Biology, Hillcrest University", "Jul 2023 - Present", "academic"),
                _exp("Postdoctoral Fellow", "Northfield Institute of Science", "Aug 2021 - Jun 2023", "academic"),
                _exp("Data Analyst", "Examplecorp Analytics Pvt Ltd", "Jan 2014 - Jun 2014", "industry"),
            ],
            "publications": [
                _pub("Modelling gene networks with sparse graphs", "journal", "2022", "Journal of Invented Biology"),
                _pub("Fast alignment of made-up sequences", "conference", "2020",
                     "Proc. Imaginary Conference on Bioinformatics"),
            ],
            "stated_totals": {"journal": None, "conference": None, "total": 2},
            "notes": "",
        },
        "synthetic_b_table": {
            "resume_file": "synthetic_b_table.pdf",
            "candidate_name": "Jonas Fakely",
            "education": [
                _edu("Ph.D.", "doctorate", "Physics", "Westbrook University"),
                _edu("M.Tech.", "masters", "Materials Science", "Eastvale Institute of Technology"),
                _edu("B.Tech.", "bachelors", "Engineering Physics", "Southgate College of Engineering"),
            ],
            "experience": [
                _exp("Associate Professor", "School of Physics, Westbrook University", "2021 - present", "academic"),
                _exp("Research Scientist", "Imaginary Labs Inc", "2019 - 2021", "industry"),
            ],
            "publications": [
                _pub("Thin films that do not exist", "journal", "2021", "Invented Physics Letters"),
                _pub("Phonons in fictional crystals", "journal", "2018", "Journal of Pretend Materials"),
                _pub("A sample talk title", "conference", "2017",
                     "Proceedings of the Placeholder Symposium"),
            ],
            "stated_totals": {"journal": None, "conference": None, "total": None},
            "notes": "",
        },
        "synthetic_c_variants": {
            "resume_file": "synthetic_c_variants.pdf",
            "candidate_name": "Lena Samplestone",
            "education": [
                _edu("Doctor of Philosophy", "doctorate", "Economics", "Greenhill University"),
                _edu("Master of Arts", "masters", "Economics", "Greenhill University"),
                _edu("Bachelor of Arts (Honours)", "bachelors", "Arts", "Stonebridge College"),
            ],
            "experience": [
                _exp("Professor", "Faculty of Economics, Maplewood University", "2020 – till date", "academic"),
                _exp("Lecturer", "Maplewood University", "2015 – 2020", "academic"),
            ],
            "publications": [
                _pub("Prices in an invented market", "journal", "2021", "Review of Fictional Economics"),
                _pub("Trade between imaginary towns", "journal", "2019", "Placeholder Economic Journal"),
            ],
            "stated_totals": {"journal": None, "conference": None, "total": None},
            "notes": "",
        },
    }


# ---------------------------------------------------------------- page-profile helpers


def _raster(width_px: int, height_px: int) -> Image.Image:
    """A grey image with dark bars that look like lines of a scanned page."""
    img = Image.new("L", (width_px, height_px), color=235)
    draw = ImageDraw.Draw(img)
    for i, top in enumerate(range(30, height_px - 30, 28)):
        draw.rectangle([30, top, width_px - 30 - (i * 37) % 160, top + 10], fill=40)
    return img


def make_text_pdf(path: Path) -> Path:
    c = _new_canvas(path, "synthetic text page")
    _draw_lines(c, RESUME_A[:10], HEIGHT - MARGIN)
    c.showPage()
    c.save()
    return path


def make_image_only_pdf(path: Path) -> Path:
    c = _new_canvas(path, "synthetic image page")
    c.drawImage(ImageReader(_raster(600, 840)), MARGIN, MARGIN, WIDTH - 2 * MARGIN, HEIGHT - 2 * MARGIN)
    c.showPage()
    c.save()
    return path


def make_mixed_pdf(path: Path) -> Path:
    c = _new_canvas(path, "synthetic mixed page")
    _draw_lines(c, RESUME_A[:8], HEIGHT - MARGIN)
    c.drawImage(ImageReader(_raster(600, 400)), MARGIN, MARGIN, WIDTH - 2 * MARGIN, HEIGHT * 0.4)
    c.showPage()
    c.save()
    return path


# ---------------------------------------------------------------- layout and cleaning helpers

HEADER_TEXT = "Curriculum Vitae - Mira Testwood"
JUSTIFIED_TEXT = (
    "The research programme studies how invented gene networks respond to made up signals, "
    "using sparse graphs, careful sampling and a great deal of patience. Each chapter of the "
    "work explains one small idea in plain words, with examples that anyone can follow."
)
TABLE_LIKE_ROW = (("2019 - 2021", 56), ("Research Scientist", 220), ("Imaginary Labs Inc", 400))
WRAPPED_LINES = (
    "Research interests include gene regu-",
    "latory networks and sparse graph",
    "models for invented data.",
)


def make_multipage_pdf(path: Path, pages: int = 3) -> Path:
    """A resume over several pages with the same header and a page-number footer on each."""
    c = _new_canvas(path, f"synthetic {pages}-page resume")
    bodies = [RESUME_A[:6], RESUME_A[6:11], RESUME_A[11:]]
    for n in range(1, pages + 1):
        c.setFont("Helvetica", 8)
        c.drawString(MARGIN, HEIGHT - 30, HEADER_TEXT)
        c.drawString(WIDTH / 2 - 20, 25, f"Page {n} of {pages}")
        _draw_lines(c, bodies[(n - 1) % len(bodies)], HEIGHT - MARGIN - 20)
        c.showPage()
    c.save()
    return path


def make_layout_pdf(path: Path) -> Path:
    """One page with a justified paragraph, a table-like row and a hyphen-wrapped paragraph."""
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.lib.styles import ParagraphStyle

    c = _new_canvas(path, "synthetic layout page")
    style = ParagraphStyle("justified", fontName="Helvetica", fontSize=10, leading=13, alignment=TA_JUSTIFY)
    para = Paragraph(JUSTIFIED_TEXT, style)
    _w, h = para.wrapOn(c, 300, 200)
    para.drawOn(c, MARGIN, HEIGHT - MARGIN - h)
    y = HEIGHT - MARGIN - h - 40
    c.setFont("Helvetica", 10)
    for text, x in TABLE_LIKE_ROW:
        c.drawString(x, y, text)
    y -= 40
    for text in WRAPPED_LINES:
        c.drawString(MARGIN, y, text)
        y -= 12
    c.showPage()
    c.save()
    return path


# ---------------------------------------------------------------- section helpers

PERSONAL_TOP = [("name", "Mira Testwood"), ("contact", "mira.testwood@example.org | +00 0000 000 001")]
EDU_LINES = [("body", "Ph.D. in Computational Biology, Northfield Institute of Science, 2016 - 2021"),
             ("body", "M.Sc. in Applied Mathematics, Lakeside University, 2014 - 2016")]
EXP_LINES = [("body", "Assistant Professor, Hillcrest University, Jul 2023 - Present"),
             ("body", "Postdoctoral Fellow, Northfield Institute of Science, Aug 2021 - Jun 2023")]
PUB_LINES = [("body", "M. Testwood, R. Doe. Modelling gene networks. Journal of Invented Biology, vol. 4, 2022."),
             ("body", "M. Testwood. Fast alignment of sequences. Proc. Imaginary Conference on Bioinformatics, 2020.")]


def make_lines_pdf(path: Path, lines: list[tuple[str, str]]) -> Path:
    """One page from (style, text) lines; styles: name, contact, heading, body."""
    c = _new_canvas(path, "synthetic sections page")
    _draw_lines(c, lines, HEIGHT - MARGIN)
    c.showPage()
    c.save()
    return path


def make_table_headings_pdf(path: Path) -> Path:
    """A CV laid out as a two-column table: heading cells on the left, content on the right."""
    styles = getSampleStyleSheet()
    rows = [
        ["Education", EDU_LINES[0][1]],
        ["", EDU_LINES[1][1]],
        ["Experience", EXP_LINES[0][1]],
        ["", EXP_LINES[1][1]],
    ]
    table = Table(rows, hAlign="LEFT", colWidths=[90, 400])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                               ("FONTSIZE", (0, 0), (-1, -1), 8)]))
    story = [Paragraph(text, styles["Title" if style == "name" else "Normal"]) for style, text in PERSONAL_TOP]
    story += [Spacer(1, 8), table]
    SimpleDocTemplate(str(path), pagesize=A4, title="synthetic table headings", author="synthetic").build(story)
    return path


def make_heading_table_pdf(path: Path, heading: str, rows: list[list[str]], after: list[tuple[str, str]]) -> Path:
    """Personal lines, a section heading, a ruled table, then more (style, text) lines."""
    styles = getSampleStyleSheet()
    style_of = {"name": styles["Title"], "contact": styles["Normal"], "heading": styles["Heading2"],
                "body": styles["Normal"]}
    table = Table(rows, hAlign="LEFT")
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 9)]))
    story = [Paragraph(text, style_of[s]) for s, text in PERSONAL_TOP]
    story += [Paragraph(heading, styles["Heading2"]), table]
    story += [Paragraph(text, style_of[s]) for s, text in after]
    SimpleDocTemplate(str(path), pagesize=A4, title="synthetic heading table", author="synthetic").build(story)
    return path


# ---------------------------------------------------------------- layout resumes
#
# Four page layouts with the same kind of invented content, for the reading-order tests
# (tests/test_layouts.py): one column, two columns, a left sidebar, and tables. Every name,
# institution, company, venue and title here is made up.

LAYOUT_EDU = [("Ph.D. in Toy Physics, Westmere University, 2012 - 2017",
               _edu("Ph.D.", "doctorate", "Toy Physics", "Westmere University")),
              ("M.Sc. in Applied Optics, Calder Valley College, 2010 - 2012",
               _edu("M.Sc.", "masters", "Applied Optics", "Calder Valley College"))]
LAYOUT_EXP = [("Associate Professor, Brightford Institute of Technology, 2020 - Present",
               _exp("Associate Professor", "Brightford Institute of Technology", "2020 - Present", "academic")),
              ("Research Engineer, Glimmerworks Labs Pvt Ltd, 2017 - 2020",
               _exp("Research Engineer", "Glimmerworks Labs Pvt Ltd", "2017 - 2020", "industry"))]
LAYOUT_PUB = [("1. T. Vonbrook, O. Halvaster. Quiet lattices in pretend crystals. Journal of Pretend Physics, 2021.",
               _pub("Quiet lattices in pretend crystals", "journal", "2021", "Journal of Pretend Physics")),
              ("2. T. Vonbrook. Folding light in toy fibres. Proc. Imaginary Symposium on Optics, 2019.",
               _pub("Folding light in toy fibres", "conference", "2019", "Proc. Imaginary Symposium on Optics"))]
LAYOUT_NAME = "Talia Vonbrook"
LAYOUT_CONTACT = "talia.vonbrook@example.org | +00 0000 000 009"
LAYOUT_SKILLS = ["Python", "Optics modelling", "Lab safety"]


def layout_ground_truth(stem: str) -> dict:
    """The ground truth (GT v2) of a layout resume; the same for all four layouts."""
    return {"resume_file": f"{stem}.pdf", "candidate_name": LAYOUT_NAME,
            "education": [gt for _, gt in LAYOUT_EDU], "experience": [gt for _, gt in LAYOUT_EXP],
            "publications": [gt for _, gt in LAYOUT_PUB],
            "stated_totals": {"journal": None, "conference": None, "total": None}, "notes": ""}


def _text(c: canvas.Canvas, x: float, y: float, style: str, text: str) -> float:
    font, size, step = FONTS[style]
    if style == "heading":
        y -= 6
    c.setFont(font, size)
    c.drawString(x, y, text)
    return y - step


def _wrapped(text: str, width_chars: int) -> list[str]:
    """Cut a line into parts of at most width_chars, at spaces."""
    out, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width_chars:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return out + ([cur] if cur else [])


def _column(c: canvas.Canvas, x: float, y: float, blocks: list[tuple[str, list[str]]], width_chars: int,
            indent: float = 12) -> float:
    """Headings with their entries; a long entry wraps, its later parts indented (hanging)."""
    for heading, entries in blocks:
        y = _text(c, x, y, "heading", heading)
        for entry in entries:
            for k, part in enumerate(_wrapped(entry, width_chars)):
                y = _text(c, x + (indent if k else 0), y, "body", part)
    return y


def make_layout_one_column(path: Path) -> Path:
    c = _new_canvas(path, "synthetic layout one column")
    y = _text(c, MARGIN, HEIGHT - MARGIN, "name", LAYOUT_NAME)
    y = _text(c, MARGIN, y, "contact", LAYOUT_CONTACT)
    _column(c, MARGIN, y, [("EDUCATION", [t for t, _ in LAYOUT_EDU]), ("EXPERIENCE", [t for t, _ in LAYOUT_EXP]),
                           ("PUBLICATIONS", [t for t, _ in LAYOUT_PUB]), ("SKILLS", [", ".join(LAYOUT_SKILLS)])], 110)
    c.showPage()
    c.save()
    return path


def make_layout_two_columns(path: Path) -> Path:
    """Name and contact across the page; education and experience in the left column,
    publications and skills in the right one, both starting at the same height."""
    c = _new_canvas(path, "synthetic layout two columns")
    y = _text(c, MARGIN, HEIGHT - MARGIN, "name", LAYOUT_NAME)
    y = _text(c, MARGIN, y, "contact", LAYOUT_CONTACT) - 6
    mid = WIDTH / 2 + 6
    _column(c, MARGIN, y, [("EDUCATION", [t for t, _ in LAYOUT_EDU]), ("EXPERIENCE", [t for t, _ in LAYOUT_EXP])], 48)
    _column(c, mid, y, [("PUBLICATIONS", [t for t, _ in LAYOUT_PUB]), ("SKILLS", [", ".join(LAYOUT_SKILLS)])], 48)
    c.showPage()
    c.save()
    return path


def make_layout_sidebar(path: Path) -> Path:
    """A narrow left sidebar (contact and skills) next to the main text (name, education,
    experience, publications) on the right."""
    c = _new_canvas(path, "synthetic layout sidebar")
    side_x, main_x = 36, 196
    top = HEIGHT - MARGIN
    y = _text(c, side_x, top, "heading", "CONTACT")
    for part in ("talia.vonbrook@example.org", "+00 0000 000 009", "Fakeville"):
        y = _text(c, side_x, y, "contact", part)
    y = _text(c, side_x, y, "heading", "SKILLS")
    for skill in LAYOUT_SKILLS:
        y = _text(c, side_x, y, "body", skill)
    y = _text(c, main_x, top, "name", LAYOUT_NAME)
    _column(c, main_x, y, [("EDUCATION", [t for t, _ in LAYOUT_EDU]), ("EXPERIENCE", [t for t, _ in LAYOUT_EXP]),
                           ("PUBLICATIONS", [t for t, _ in LAYOUT_PUB])], 70)
    c.showPage()
    c.save()
    return path


def make_layout_tables(path: Path) -> Path:
    """Education and experience as ruled tables with a header row; publications as a list."""
    styles = getSampleStyleSheet()
    style = TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9)])
    edu = [["Degree", "Institution", "Year"]] + [[f"{g['degree']} {g['stream']}", g["institution"], t.rsplit(", ", 1)[1]]
                                                 for t, g in LAYOUT_EDU]
    exp = [["Designation", "Organisation", "Duration"]] + [[g["job_title"], g["institution"], g["dates"]]
                                                           for _, g in LAYOUT_EXP]
    story = [Paragraph(LAYOUT_NAME, styles["Title"]), Paragraph(LAYOUT_CONTACT, styles["Normal"]), Spacer(1, 8),
             Paragraph("Education", styles["Heading2"]), Table(edu, hAlign="LEFT", style=style), Spacer(1, 8),
             Paragraph("Experience", styles["Heading2"]), Table(exp, hAlign="LEFT", style=style), Spacer(1, 8),
             Paragraph("Publications", styles["Heading2"])]
    story += [Paragraph(t, styles["Normal"]) for t, _ in LAYOUT_PUB]
    SimpleDocTemplate(str(path), pagesize=A4, title="synthetic layout tables", author="synthetic").build(story)
    return path


LAYOUTS = {"layout_one_column": make_layout_one_column, "layout_two_columns": make_layout_two_columns,
           "layout_sidebar": make_layout_sidebar, "layout_tables": make_layout_tables}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python scripts/make_synthetic_resumes.py <out_dir>")
        return 2
    for path in build_all(argv[0]):
        print(path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
