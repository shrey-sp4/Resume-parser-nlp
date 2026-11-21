# Synthetic fixtures

Fake resumes with invented names and data. They are not stored here: the tests
build them in a temporary folder, and `.gitignore` blocks `*.pdf`.

To look at them, build them into a folder outside the repo:

    python scripts/make_synthetic_resumes.py <out_dir>
