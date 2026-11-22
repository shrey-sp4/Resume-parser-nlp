"""Refuse real data and banned words in the repo. Used by the pre-commit hook and CI.

Checks every tracked and staged file:
- no PDF files
- no .json files outside config/, schema/, fixtures/, docs/
- no file larger than 1 MB
- no banned tool names in file paths or text contents (case-insensitive)
- when RESUME_DATA_DIR is set: no resume ID or file stem of a guarded group (validation,
  test_frozen, from ground_truth_v2/_split.json) in file paths or text contents; the message
  never shows the value found
- when RESUME_FINAL_GT or RESUME_FINAL_PDFS is set: the same for the final set (the stems of the
  .json and .pdf files directly inside those folders, and their resume IDs)

Usage:
    python scripts/check_no_data.py            tracked + staged files
    python scripts/check_no_data.py --staged   staged files only
Exit code 1 when a problem is found. Uses only the standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ALLOWED_JSON_DIRS = ("config", "schema", "fixtures", "docs")
MAX_BYTES = 1024 * 1024
BINARY_SNIFF = 8192

# Built from parts so this file does not itself contain the banned words.
_BANNED = ("cla" + "ude", "anthr" + "opic")
BANNED_PATTERN = re.compile("|".join(_BANNED), re.IGNORECASE)


def _is_binary(raw: bytes) -> bool:
    return b"\0" in raw[:BINARY_SNIFF]


GUARDED_GROUPS = ("validation", "test_frozen")


def _add_marker(markers: set[str], entry: str) -> None:
    text = Path(str(entry).strip()).name
    for suffix in (".json", ".pdf"):
        if text.lower().endswith(suffix):
            text = text[: -len(suffix)]
    if text.startswith("r_"):
        markers.add(text)
    elif len(text) >= 6:  # a stem short enough to be a common word is not checked
        markers.add(text)
        markers.add("r_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8])


def final_markers() -> set[str]:
    """Stems and resume IDs of the final set: the .json files directly inside RESUME_FINAL_GT
    and the .pdf files directly inside RESUME_FINAL_PDFS (empty when neither is set)."""
    markers: set[str] = set()
    for env, suffix in (("RESUME_FINAL_GT", ".json"), ("RESUME_FINAL_PDFS", ".pdf")):
        folder = os.environ.get(env, "").strip()
        if folder and Path(folder).is_dir():
            for path in Path(folder).iterdir():
                if path.is_file() and path.suffix.lower() == suffix:
                    _add_marker(markers, path.name)
    return markers


def guarded_markers() -> set[str]:
    """Resume IDs and file stems of the guarded groups, from RESUME_DATA_DIR, and of the final
    set (empty without them)."""
    markers = final_markers()
    folder = os.environ.get("RESUME_DATA_DIR", "")
    split = Path(folder) / "ground_truth_v2" / "_split.json" if folder else None
    if split is None or not split.is_file():
        return markers
    data = json.loads(split.read_text(encoding="utf-8"))
    for group in GUARDED_GROUPS:
        for entry in data.get(group, []):
            _add_marker(markers, entry)
    return markers


def find_problems(paths: list[str], root: Path, guarded: set[str] | frozenset = frozenset()) -> list[str]:
    """Return one message per problem, sorted by path. Paths are relative to root."""
    problems = []
    for rel in sorted({p.replace("\\", "/") for p in paths}):
        posix = PurePosixPath(rel)
        suffix = posix.suffix.lower()
        if suffix == ".pdf":
            problems.append(f"{rel}: PDF files are not allowed")
        if suffix == ".json" and (len(posix.parts) < 2 or posix.parts[0] not in ALLOWED_JSON_DIRS):
            problems.append(f"{rel}: JSON allowed only in {', '.join(ALLOWED_JSON_DIRS)}")
        if BANNED_PATTERN.search(rel):
            problems.append(f"{rel}: banned word in file path")
        if any(m in rel for m in guarded):
            problems.append(f"{rel}: guarded-group resume in file path")

        full = root / rel
        if not full.is_file():
            continue  # deleted in the working tree
        size = full.stat().st_size
        if size > MAX_BYTES:
            problems.append(f"{rel}: larger than 1 MB ({size} bytes)")
            continue
        raw = full.read_bytes()
        if _is_binary(raw):
            continue
        text = raw.decode("utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            if BANNED_PATTERN.search(line):
                problems.append(f"{rel}:{number}: banned word in file contents")
                break
        for number, line in enumerate(text.splitlines(), start=1):
            if any(m in line for m in guarded):
                problems.append(f"{rel}:{number}: guarded-group resume ID or file name in file contents")
                break
    return problems


def _git_files(args: list[str]) -> list[str]:
    out = subprocess.run(["git", *args], check=True, capture_output=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="check staged files only")
    args = parser.parse_args(argv)

    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True)
    root = Path(top.stdout.decode("utf-8").strip())
    staged = _git_files(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"])
    paths = staged if args.staged else sorted(set(staged) | set(_git_files(["ls-files", "-z"])))

    problems = find_problems(paths, root, guarded_markers())
    for message in problems:
        print(message)
    if problems:
        print(f"check_no_data: {len(problems)} problem(s) found")
        return 1
    print(f"check_no_data: {len(paths)} file(s) checked, no problems")
    return 0


if __name__ == "__main__":
    sys.exit(main())
