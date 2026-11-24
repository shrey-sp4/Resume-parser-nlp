"""The data guard rejects PDFs, stray JSON, large files and banned words."""

from __future__ import annotations

from pathlib import Path

import check_no_data
from check_no_data import MAX_BYTES, find_problems

WORD = "cla" + "ude"  # built from parts, like the guard itself


def _make(root: Path, rel: str, content: bytes = b"ok\n") -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return rel


def test_rejects_a_pdf(tmp_path):
    rel = _make(tmp_path, "notes/fake.pdf", b"%PDF-1.4 fake")
    assert find_problems([rel], tmp_path) == ["notes/fake.pdf: PDF files are not allowed"]


def test_rejects_json_outside_allowed_folders(tmp_path):
    paths = [
        _make(tmp_path, "data/r_0001.json", b"{}"),
        _make(tmp_path, "top.json", b"{}"),
        _make(tmp_path, "src/resume_parser/x.json", b"{}"),
    ]
    problems = find_problems(paths, tmp_path)
    assert len(problems) == 3
    assert all("JSON allowed only in" in p for p in problems)


def test_allows_json_in_allowed_folders(tmp_path):
    paths = [_make(tmp_path, f"{d}/a.json", b"{}") for d in ("config", "schema", "fixtures", "docs")]
    assert find_problems(paths, tmp_path) == []


def test_rejects_lexicon_files_and_large_csvs(tmp_path):
    names = ["data/scimagojr 2025.csv", "core/ICORE2026.csv", "x/CORE2023.csv", "nirf_2025/OverallRanking.html",
             "pages/EngineeringRanking150.html", "pages/LawRankingALL.html", "config/nirf_tables.json",
             "dumps/ror-data.json", "dumps/openalex_institutions.txt"]
    paths = [_make(tmp_path, n) for n in names]
    problems = find_problems(paths, tmp_path)
    assert sum("lexicon file" in p for p in problems) == len(names)
    big = _make(tmp_path, "notes/list.csv", b"a,b\n" * 30000)
    small = _make(tmp_path, "notes/small.csv", b"a,b\n")
    assert find_problems([big, small], tmp_path) == [f"{big}: CSV larger than 100 KB (120000 bytes); lists belong in RESUME_LEXICON_DIR"]
    # code, docs and ordinary names are not lexicons
    ok = [_make(tmp_path, n) for n in ("src/shortlisting/tiers/institutions.py", "docs/nirf_notes.md", "notes/score2021.txt",
                                       "config/tiers.json")]
    assert find_problems(ok, tmp_path) == []


def test_rejects_large_files(tmp_path):
    rel = _make(tmp_path, "big.txt", b"a" * (MAX_BYTES + 1))
    assert find_problems([rel], tmp_path) == [f"big.txt: larger than 1 MB ({MAX_BYTES + 1} bytes)"]


def test_rejects_banned_word_in_contents_and_paths(tmp_path):
    inside = _make(tmp_path, "src/a.py", f"x = 1\n# made with {WORD.upper()}\n".encode())
    named = _make(tmp_path, f"{WORD}.md")
    problems = find_problems([inside, named], tmp_path)
    assert "src/a.py:2: banned word in file contents" in problems
    assert f"{WORD}.md: banned word in file path" in problems


def test_skips_binary_files_for_contents(tmp_path):
    rel = _make(tmp_path, "img.png", b"\x89PNG\0\0" + WORD.encode())
    assert find_problems([rel], tmp_path) == []


def test_guard_script_does_not_contain_the_words():
    source = Path(check_no_data.__file__).read_text(encoding="utf-8")
    assert not check_no_data.BANNED_PATTERN.search(source)
