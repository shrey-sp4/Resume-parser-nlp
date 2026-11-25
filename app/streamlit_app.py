"""Recruiter UI for the shortlisting scorer.

    streamlit run app/streamlit_app.py

All work is done by src/shortlisting/service.py; this file only draws the screens, in plain words
for a recruiter. Runs locally: no network calls, no learned model. Files are read into the work
folder (RESUME_SHORTLIST_DIR, else RESUME_DATA_DIR/shortlist_runs), never into the repo. The
tables show the file name or the resume ID, the score, the areas found and the reason, never a
name, gender, age, photo or nationality. The pipeline panel sits in the sidebar so it stays in
view while the page scrolls.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import streamlit as st  # noqa: E402

from shortlisting import service  # noqa: E402

FOLDER, UPLOAD = "Folder with PDF files", "Upload PDF files"

st.set_page_config(page_title="Candidate shortlisting", layout="wide", initial_sidebar_state="expanded")
st.markdown(service.CSS, unsafe_allow_html=True)
st.title("Candidate shortlisting")


def html(text: str, where=st) -> None:
    where.markdown(text, unsafe_allow_html=True)


panel = st.sidebar.empty()

# ---------------------------------------------------------------- 1. CVs

st.header("1. CVs")
source = st.radio("Choose your CV files", [FOLDER, UPLOAD], horizontal=True, key="source")
pdfs: list[Path] = []
uploads = []
if source == FOLDER:
    folder = st.text_input("Folder path", key="folder", placeholder=r"C:\Users\Priya\Desktop\CVs")
    st.caption(r"Example — Windows: C:\Users\Priya\Desktop\CVs  ·  Mac: /Users/priya/Desktop/CVs")
    if folder:
        if not Path(folder).is_dir():
            st.error(service.FOLDER_MISSING)
        else:
            pdfs = service.find_pdfs(folder)
            st.write(f"**{len(pdfs)}** PDF file(s) found.")
else:
    uploads = st.file_uploader("PDF files", type=["pdf"], accept_multiple_files=True, key="uploads") or []
    st.write(f"**{len(uploads)}** PDF file(s) selected.")

# ---------------------------------------------------------------- 2. Requirements

st.header("2. Requirements")
mode = st.radio("What matters most?", list(service.MODE_LABELS), format_func=service.MODE_LABELS.get,
                horizontal=True, key="mode")
c1, c2 = st.columns(2)
kind, years, inst_cut, venue_cut = "academic", 5, None, None
with c1:
    labels = dict(service.field_options())
    field_key = st.selectbox("Field", list(labels), key="field",
                             format_func=lambda v: "Any field (for example AI/ML/Data Science)" if v is None else labels[v])
    min_degree = st.selectbox("Minimum degree", [None, *service.DEGREE_LABELS], key="min_degree",
                              format_func=lambda v: "No minimum (for example Masters)" if v is None else service.DEGREE_LABELS[v])
with c2:
    if mode == "experience":
        kind = st.radio("Experience kind", ["academic", "industry"], format_func=str.capitalize, horizontal=True,
                        key="kind")
        years = st.select_slider("At least this many years", [2, 5, 10], value=5, key="years")
    if mode == "education":
        inst_cut = st.selectbox("Institution rank at least", [None, *service.INSTITUTION_LABELS], key="inst_cut",
                                format_func=lambda v: "No cutoff" if v is None else service.INSTITUTION_LABELS[v])
    if mode == "publications":
        venue_cut = st.selectbox("At least one paper in a venue of level", [None, *service.VENUE_LABELS],
                                 key="venue_cut",
                                 format_func=lambda v: "No cutoff" if v is None else service.VENUE_LABELS[v])

keywords = st.text_area("Topic keywords (comma separated)", key="keywords", height=70,
                        placeholder="machine learning, signal processing, deep learning")
synonyms = st.text_area("Synonyms (one per line)", key="synonyms", height=70,
                        placeholder="machine learning: ML, deep learning")

# ---------------------------------------------------------------- 3. Run

st.header("3. Run")
run = st.session_state.get("shortlist_run")
count = len(pdfs) if source == FOLDER else len(uploads)
clicked = st.button("Rank the CVs", type="primary", disabled=count == 0, key="run")
status = st.empty()
bar_slot = st.empty()

if clicked:
    try:
        profile = service.build_profile({
            "mode": mode, "field": field_key, "min_degree_level": min_degree, "experience_kind": kind,
            "experience_years": years, "institution_tier_cutoff": inst_cut, "venue_tier_cutoff": venue_cut,
            "keywords": keywords, "synonyms": synonyms})
        paths = pdfs if source == FOLDER else service.save_uploads([(u.name, u.getvalue()) for u in uploads])
        board = service.StatusBoard(len(paths))
        html(service.panel_html(board), panel)
        bar = bar_slot.progress(0.0)

        def progress(event: dict) -> None:
            board.update(event)
            status.write(board.status)
            bar.progress(board.bar())
            html(service.panel_html(board), panel)

        run = service.run_shortlist(profile, paths, progress_callback=progress)
        st.session_state["shortlist_run"], st.session_state["board"] = run, board
    except (service.ServiceError, ValueError) as exc:
        st.error(str(exc))

board = st.session_state.get("board") if run is not None else None
html(service.panel_html(board, {t: len(v) for t, v in run.by_tier.items()} if run is not None else None), panel)

# ---------------------------------------------------------------- 4. Results


def open_button(run, r: dict) -> None:
    path = run.paths.get(r["resume_id"])
    if path is not None and Path(path).is_file():
        st.download_button("Open CV", Path(path).read_bytes(), file_name=Path(path).name, mime="application/pdf",
                           key=f"open_{r['resume_id']}", on_click="ignore")


def show_card(r: dict) -> None:
    c = service.card(r)
    html(service.chips_html(r))
    st.write(c["reason"])
    for comp in c["components"]:
        left, mid = st.columns([2, 5])
        left.write(f"**{comp['name']}**")
        if comp["value"] is None:
            mid.write(comp["status"])
        else:
            mid.progress(float(comp["value"]), text=comp["score"])
        if comp["found"]:
            st.write(" · ".join(comp["found"]))
    if c["missing"]:
        st.write("Missing: " + " ".join(c["missing"]))
    if c["requirements"]:
        st.dataframe(c["requirements"], hide_index=True)


if run is not None:
    st.header("4. Results")
    st.write(service.summary_line(run))
    f1, f2 = st.columns(2)
    sort_by = f1.selectbox("Sort by", ["score", "file"], key="sort_by",
                           format_func={"score": "Score (high to low)", "file": "File name (A to Z)"}.get)
    min_score = f2.slider("Minimum score", 0, 100, 0, 5, format="%d%%", key="min_score")
    shown = {t: service.sort_and_filter(run.by_tier.get(t, []), sort_by, min_score / 100 if t != "Unreadable" else 0.0)
             for t in service.TIERS}
    tabs = st.tabs([f"{t} ({len(shown[t])})" for t in service.TIERS])
    for tier, tab in zip(service.TIERS, tabs):
        with tab:
            rows = shown[tier]
            if not rows:
                continue
            if tier == "Unreadable":
                for r in rows:
                    a, b = st.columns([4, 1])
                    a.write(f"**{r['file_name']}** — {service.reason_text(r)}")
                    with b:
                        open_button(run, r)
                continue
            st.dataframe(service.table_rows(rows), hide_index=True)
            for r in rows:
                with st.expander(f"#{r['rank']} · {r['file_name']} · {service.percent(r['score'])}"):
                    show_card(r)
                    open_button(run, r)

    st.subheader("Export")
    label = st.radio("Label rows by", ["resume_id", "file_name"], horizontal=True, key="export_label",
                     format_func={"resume_id": "Resume ID", "file_name": "File name"}.get)
    st.download_button("Download the ranked table (CSV)", service.export_csv_text(run, label),
                       file_name="shortlist.csv", mime="text/csv", key="export",
                       on_click=lambda: service.export_csv(run, label))
    st.caption(service.NOTE)

# ---------------------------------------------------------------- saved results


def clear_saved() -> None:
    try:
        service.clear_saved_results()
    except service.ServiceError as exc:
        st.session_state["clear_error"] = str(exc)
    st.session_state["confirm_clear"] = False


st.divider()
if st.session_state.get("clear_error"):
    st.error(st.session_state.pop("clear_error"))
if st.session_state.get("confirm_clear"):
    st.warning("Delete the saved reading of every CV? The next run reads each CV again.")
    y, n = st.columns(2)
    y.button("Yes, clear saved results", key="clear_yes", on_click=clear_saved)
    n.button("Cancel", key="clear_no", on_click=lambda: st.session_state.update(confirm_clear=False))
else:
    st.button("Clear saved results", key="clear", on_click=lambda: st.session_state.update(confirm_clear=True))
