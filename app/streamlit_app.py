"""Recruiter UI for the shortlisting scorer.

    streamlit run app/streamlit_app.py

All work is done by src/shortlisting/service.py; this file only draws the screens, in plain words
for a recruiter. Runs locally: no network calls, no learned model. Files are read into the work
folder (RESUME_SHORTLIST_DIR, else RESUME_DATA_DIR/shortlist_runs), never into the repo. The
tables show the file name or the resume ID, the score, the confidence and the reason, never a
name, gender, age, photo or nationality.
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
# widget key -> form key (service.FORM_KEYS), with the starting value
WIDGETS = {"mode": ("mode", "default"), "field": ("field", None), "min_degree": ("min_degree_level", None),
           "kind": ("experience_kind", "academic"), "years": ("experience_years", 5),
           "inst_cut": ("institution_tier_cutoff", None), "venue_cut": ("venue_tier_cutoff", None),
           "keywords": ("keywords", ""), "synonyms": ("synonyms", "")}
VALID = {"mode": list(service.MODE_LABELS), "field": [k for k, _ in service.field_options()],
         "min_degree": [None, *service.DEGREE_LABELS], "kind": ["academic", "industry"], "years": [2, 5, 10],
         "inst_cut": [None, *service.INSTITUTION_LABELS], "venue_cut": [None, *service.VENUE_LABELS]}

st.set_page_config(page_title="Candidate shortlisting", layout="wide")
for key, (_, start) in WIDGETS.items():
    st.session_state.setdefault(key, start)
st.title("Candidate shortlisting")


def form_values() -> dict:
    return {form_key: st.session_state.get(key, start) for key, (form_key, start) in WIDGETS.items()}


def suggest() -> None:
    words = service.suggest_keywords(st.session_state.get("job_ad", ""))
    if words:
        st.session_state["keywords"] = ", ".join(words)
    st.session_state["message"] = None if words else "No keywords found in the advertisement. Please type them in."


def load_saved() -> None:
    try:
        data = service.load_profile(st.session_state.get("profile_pick") or "")
    except service.ServiceError as exc:
        st.session_state["message"] = str(exc)
        return
    for key, (form_key, start) in WIDGETS.items():
        value = data.get(form_key, start)
        if key in VALID and value not in VALID[key]:
            value = start
        st.session_state[key] = value if value is not None or key in VALID else start
    st.session_state["message"] = None


def save_current() -> None:
    try:
        service.save_profile(st.session_state.get("profile_name", ""), form_values())
        st.session_state["message"] = None
    except service.ServiceError as exc:
        st.session_state["message"] = str(exc)


def clear_saved() -> None:
    try:
        service.clear_saved_results()
    except service.ServiceError as exc:
        st.session_state["message"] = str(exc)
    st.session_state["confirm_clear"] = False


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
mode = st.radio("What matters most?", VALID["mode"], format_func=service.MODE_LABELS.get, horizontal=True, key="mode")
c1, c2 = st.columns(2)
with c1:
    labels = dict(service.field_options())
    st.selectbox("Field", VALID["field"], key="field",
                 format_func=lambda v: "Any field (for example AI/ML/Data Science)" if v is None else labels[v])
    st.selectbox("Minimum degree", VALID["min_degree"], key="min_degree",
                 format_func=lambda v: "No minimum (for example Masters)" if v is None else service.DEGREE_LABELS[v])
with c2:
    if mode == "experience":
        st.radio("Experience kind", VALID["kind"], format_func=str.capitalize, horizontal=True, key="kind")
        st.select_slider("At least this many years", VALID["years"], key="years")
    if mode == "education":
        st.selectbox("Institution rank at least", VALID["inst_cut"], key="inst_cut",
                     format_func=lambda v: "No cutoff" if v is None else service.INSTITUTION_LABELS[v])
    if mode == "publications":
        st.selectbox("At least one paper in a venue of level", VALID["venue_cut"], key="venue_cut",
                     format_func=lambda v: "No cutoff" if v is None else service.VENUE_LABELS[v])

st.text_area("Job advertisement (optional)", key="job_ad", height=100, placeholder="Paste the job description here")
st.button("Suggest keywords", key="suggest", on_click=suggest)
st.text_area("Topic keywords (comma separated)", key="keywords", height=70,
             placeholder="machine learning, signal processing, deep learning")
st.text_area("Synonyms (one per line)", key="synonyms", height=70, placeholder="machine learning: ML, deep learning")

with st.expander("Saved requirements"):
    try:
        saved = service.list_profiles()
    except service.ServiceError as exc:
        saved = []
        st.error(str(exc))
    s1, s2 = st.columns(2)
    with s1:
        st.selectbox("Load", saved, index=None, key="profile_pick", placeholder="Choose saved requirements")
        st.button("Load these requirements", key="load_profile", on_click=load_saved, disabled=not saved)
    with s2:
        st.text_input("Save as", key="profile_name", placeholder="e.g. Assistant professor, computing")
        st.button("Save these requirements", key="save_profile", on_click=save_current)

if st.session_state.get("message"):
    st.error(st.session_state["message"])

# ---------------------------------------------------------------- 3. Run

st.header("3. Run")
count = len(pdfs) if source == FOLDER else len(uploads)
if st.button("Rank the CVs", type="primary", disabled=count == 0, key="run"):
    try:
        profile = service.build_profile(form_values())
        paths = pdfs if source == FOLDER else service.save_uploads([(u.name, u.getvalue()) for u in uploads])
        board = service.ProgressBoard([p.name for p in paths])
        bar = st.progress(0.0, text=board.bar()[1])
        table = st.empty()
        table.dataframe(board.rows(), hide_index=True)

        def progress(event: dict) -> None:
            board.update(event)
            fraction, text = board.bar()
            bar.progress(min(fraction, 1.0), text=text)
            table.dataframe(board.rows(), hide_index=True)

        st.session_state["shortlist_run"] = service.run_shortlist(profile, paths, progress_callback=progress)
    except (service.ServiceError, ValueError) as exc:
        st.error(str(exc))

# ---------------------------------------------------------------- 4. Results


def open_button(run, r: dict) -> None:
    path = run.paths.get(r["resume_id"])
    if path is not None and Path(path).is_file():
        st.download_button("Open CV", Path(path).read_bytes(), file_name=Path(path).name, mime="application/pdf",
                           key=f"open_{r['resume_id']}", on_click="ignore")


def show_card(r: dict) -> None:
    c = service.card(r)
    st.write(f"**{c['confidence']}** · {c['reason']}")
    for comp in c["components"]:
        left, mid, right = st.columns([2, 3, 2])
        left.write(f"**{comp['name']}**")
        if comp["value"] is None:
            mid.write("Not found")
        else:
            mid.progress(float(comp["value"]), text=f"{comp['value']:.2f}")
            right.write(comp["confidence"])
        if comp["found"]:
            st.write(" · ".join(comp["found"]))
    if c["missing"]:
        st.write("Missing: " + " ".join(c["missing"]))
    if c["requirements"]:
        st.dataframe(c["requirements"], hide_index=True)


run = st.session_state.get("shortlist_run")
if run is not None:
    st.header("4. Results")
    st.write(service.summary_line(run))
    f1, f2 = st.columns(2)
    sort_by = f1.selectbox("Sort by", ["score", "file"], key="sort_by",
                           format_func={"score": "Score (high to low)", "file": "File name (A to Z)"}.get)
    min_score = f2.slider("Minimum score", 0.0, 1.0, 0.0, 0.05, key="min_score")
    shown = {t: service.sort_and_filter(run.by_tier.get(t, []), sort_by, min_score if t != "Unreadable" else 0.0)
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
                with st.expander(f"#{r['rank']} · {r['file_name']}"):
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

st.divider()
if st.session_state.get("confirm_clear"):
    st.warning("Delete the saved reading of every CV? The next run reads each CV again.")
    y, n = st.columns(2)
    y.button("Yes, clear saved results", key="clear_yes", on_click=clear_saved)
    n.button("Cancel", key="clear_no", on_click=lambda: st.session_state.update(confirm_clear=False))
else:
    st.button("Clear saved results", key="clear", on_click=lambda: st.session_state.update(confirm_clear=True))
