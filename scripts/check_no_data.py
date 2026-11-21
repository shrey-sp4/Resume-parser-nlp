"""Refuse real data and banned words in the repo. Used by the pre-commit hook and CI.

Checks every tracked and staged file:
- no PDF files
- no .json files outside config/, schema/, fixtures/, docs/
- no file larger than 1 MB
- no banned tool names in file paths or text contents (case-insensitive)

Usage:
    python scripts/check_no_data.py            tracked + staged files
    python scripts/check_no_data.py --staged   staged files only
Exit code 1 when a problem is found. Uses only the standard library.
"""

from __future__ import annotations

import argparse
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


def find_problems(paths: list[str], root: Path) -> list[str]:
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

    problems = find_problems(paths, root)
    for message in problems:
        print(message)
    if problems:
        print(f"check_no_data: {len(problems)} problem(s) found")
        return 1
    print(f"check_no_data: {len(paths)} file(s) checked, no problems")
    return 0


if __name__ == "__main__":
    sys.exit(main())
