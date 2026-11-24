"""Recruiter UI for the shortlisting scorer.

    streamlit run app/streamlit_app.py

All work is done by src/shortlisting/service.py; this file only draws the screens. Runs locally:
no network calls, no learned model. Files are parsed into the work folder (RESUME_SHORTLIST_DIR,
else RESUME_DATA_DIR/shortlist_runs), never into the repo. The tables show the file name or the
resume ID, the score, the certainty and the parser flag, never a name, gender, age, photo or
nationality.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import streamlit as st  # noqa: E402

from shortlisting import service  # noqa: E402

TIERS = ("Shortlist", "Review", "Hold", "Unreadable")
TIER_INTRO = {
    "Shortlist": "Candidates whose score reaches the threshold with enough certainty.",
    "Review": "Worth a closer look: something could not be checked or is uncertain.",
    "Hold": "Not shortlisted for now; the reason is shown for each candidate.",
    "Unreadable": service.UNREADABLE_TEXT,
}

st.set_page_config(page_title="Candidate shortlisting", layout="wide")
st.title("Candidate shortlisting")
st.caption("Decision support for reading CVs. Runs on this computer only.")

# ---------------------------------------------------------------- input

st.header("1. CVs")
source = st.radio("Where are the CVs?", ["A folder on this computer", "Upload PDF files"], horizontal=True, key="source")
pdfs: list[Path] = []
uploads = []
if source == "A folder on this computer":
    folder = st.text_input("Folder with PDF files", key="folder")
    if folder:
        pdfs = service.find_pdfs(folder)
        st.write(f"**{len(pdfs)}** PDF file(s) found." if Path(folder).is_dir() else "That folder does not exist.")
else:
    uploads = st.file_uploader("PDF files", type=["pdf"], accept_multiple_files=True, key="uploads") or []
    st.write(f"**{len(uploads)}** PDF file(s) selected.")

# ---------------------------------------------------------------- requirements

st.header("2. Requirements")
modes = list(service.MODE_LABELS)
mode = st.radio("What matters most?", modes, format_func=service.MODE_LABELS.get, horizontal=True, key="mode")
st.caption(service.MODE_SENTENCES[mode])

c1, c2 = st.columns(2)
with c1:
    options = service.field_options()
    field_key = st.selectbox("Field", [k for k, _ in options], format_func=dict(options).get, key="field")
    min_degree = st.selectbox("Minimum degree (optional)", [None, "bachelors", "masters", "doctorate"],
                              format_func=lambda v: "No minimum" if v is None else v.capitalize(), key="min_degree")
with c2:
    kind, years = "academic", 5
    inst_cut = venue_cut = None
    if mode == "experience":
        kind = st.radio("Experience kind", ["academic", "industry"], horizontal=True, key="kind")
        years = st.select_slider("At least this many years", [2, 5, 10], value=5, key="years")
    if mode == "education":
        inst_cut = st.selectbox("Institution tier at least (optional)", [None, "Tier 1", "Tier 2", "Tier 3"],
                                format_func=lambda v: "No cutoff" if v is None else v, key="inst_cut")
    if mode == "publications":
        venue_cut = st.selectbox("At least one venue of tier (optional)", [None, "A", "B", "C"],
                                 format_func=lambda v: "No cutoff" if v is None else v, key="venue_cut")

job_ad = st.text_area("Job advertisement (optional): paste it to get keyword suggestions", key="job_ad", height=100)
if st.button("Suggest keywords from the advertisement", key="suggest"):
    words = service.suggest_keywords(job_ad)
    st.session_state["keywords"] = ", ".join(words)
    st.info(f"{len(words)} keyword(s) suggested below; edit them before running." if words else "No keywords found in the text.")
st.text_area("Topic keywords (comma separated; leave empty to skip topic fit)", key="keywords", height=70)
st.text_area("Synonyms (optional, one per line: keyword: other word, other word)", key="synonyms", height=70)

# ---------------------------------------------------------------- run

st.header("3. Run")
count = len(pdfs) if source == "A folder on this computer" else len(uploads)
if st.button("Rank the CVs", type="primary", disabled=count == 0, key="run"):
    try:
        profile = service.build_profile({
            "mode": mode, "field": field_key, "min_degree_level": min_degree, "experience_kind": kind,
            "experience_years": years, "institution_tier_cutoff": inst_cut, "venue_tier_cutoff": venue_cut,
            "keywords": st.session_state.get("keywords", ""), "synonyms": st.session_state.get("synonyms", "")})
        paths = pdfs if source == "A folder on this computer" else service.save_uploads([(u.name, u.getvalue()) for u in uploads])
        bar = st.progress(0.0, text="Starting")

        def progress(done, total, step):
            bar.progress(done / total, text=f"{'Reading CVs' if step == 'parsing' else 'Scoring'}: {done} of {total}")

        st.session_state["shortlist_run"] = service.run_shortlist(profile, paths, progress_callback=progress)
        bar.progress(1.0, text="Done")
    except (service.ServiceError, ValueError) as exc:
        st.error(f"Could not run: {exc}")

# ---------------------------------------------------------------- results


def show_card(r: dict) -> None:
    c = service.card(r)
    cert = c["certainty"]
    st.write(f"Overall certainty: **{cert['level']}** (usable weight {cert['usable_weight_share']:.0%}). "
             + (f"Rank range in the stability check: **{c['rank_range'][0]}–{c['rank_range'][1]}**." if c["rank_range"] else ""))
    for comp in c["components"]:
        left, mid, right = st.columns([2, 3, 3])
        left.write(f"**{comp['name']}**" + (" · editorial override" if comp["editorial_override"] else ""))
        if comp["value"] is None:
            mid.write(f"not used: {comp['reason'].replace('_', ' ')}")
        else:
            mid.progress(float(comp["value"]), text=f"value {comp['value']:.2f}")
            right.progress(float(comp["certainty_value"]), text=f"certainty {comp['certainty']}")
        if comp["evidence"]:
            st.caption("Evidence: " + "; ".join(comp["evidence"]))
    if c["dropped"]:
        st.write("Left out of the score: " + "; ".join(f"{d['name']} ({d['reason']})" for d in c["dropped"]))
    if c["eligibility"]:
        st.write("Requirements:")
        st.table([{"requirement": e["check"], "result": e["result"], "why": e["reason"]} for e in c["eligibility"]])
    if c["reason"]:
        st.caption(f"Why this tier: {c['reason']}")


run = st.session_state.get("shortlist_run")
if run is not None:
    st.header("4. Results")
    st.info(service.NOTE)
    tabs = st.tabs([f"{t} ({len(run.by_tier.get(t, []))})" for t in TIERS])
    for tier, tab in zip(TIERS, tabs):
        with tab:
            st.caption(TIER_INTRO[tier])
            rows = run.by_tier.get(tier, [])
            if not rows:
                st.write("No candidates here.")
                continue
            table = service.table_rows(rows)
            if tier not in ("Hold", "Unreadable"):
                table = [{k: v for k, v in row.items() if k != "reason"} for row in table]
            st.dataframe(table, hide_index=True)
            if tier != "Unreadable":
                for r in rows:
                    with st.expander(f"#{r['rank']} · {r['file_name']}"):
                        show_card(r)
    st.subheader("Export")
    label = st.radio("Label rows by", ["resume_id", "file_name"], format_func={"resume_id": "Resume ID (r_…)",
                                                                                "file_name": "File name"}.get,
                     horizontal=True, key="export_label")
    if st.button("Save the ranked table as CSV", key="export"):
        path = service.export_csv(run, label)
        st.success(f"Saved in the work folder: {path}")
