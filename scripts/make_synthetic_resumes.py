"""Build fake academic resumes as PDFs for tests. All names and data are invented.

    python scripts/make_synthetic_resumes.py <out_dir>

Builds three resumes:
  synthetic_a_simple.pdf    single column, plain headings
  synthetic_b_table.pdf     education given as a ruled table
  synthetic_c_variants.pdf  different heading wording and bullet styles

Also gives ground truth (GT v2) for the three resumes, for the evaluator tests, and has
helpers for page-profile tests: a text-only page, an image-only page
(a drawn raster, no text) and a mixed page.

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
                _exp("Assistant Professor", "Hillcrest University", "Jul 2023 - Present", "academic"),
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
                _exp("Associate Professor", "Westbrook University", "2021 - present", "academic"),
                _exp("Research Scientist", "Imaginary Labs Inc", "2019 - 2021", "industry"),
            ],
            "publications": [
                _pub("Thin films that do not exist", "journal", "2021", "Invented Physics Letters"),
                _pub("Phonons in fictional crystals", "journal", "2018", "Journal of Pretend Materials"),
                _pub("A sample talk title for testing", "conference", "2017",
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
                _exp("Professor", "Maplewood University", "2020 – till date", "academic"),
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
