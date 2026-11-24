"""Where the lexicon files live: RESUME_LEXICON_DIR, or RESUME_DATA_DIR/Lexicons by default.

The files (SCImago journal list, ICORE conference list, NIRF ranking pages) are public but
licensed by their publishers and large, so they are never inside the repo; a folder inside the
repo is refused. Messages never show a path.
"""

from __future__ import annotations

import os
from pathlib import Path

from resume_parser.schema import REPO_ROOT, DataDirError, data_root, load_config

ENV_LEXICONS = "RESUME_LEXICON_DIR"


class LexiconError(RuntimeError):
    """A lexicon folder or file is missing, misplaced or malformed."""


def lexicon_dir() -> Path:
    value = os.environ.get(ENV_LEXICONS, "").strip()
    if value:
        folder = Path(value).resolve()
    else:
        try:
            folder = data_root() / "Lexicons"
        except DataDirError as exc:
            raise LexiconError(f"set {ENV_LEXICONS} or RESUME_DATA_DIR ({exc})") from None
    if folder == REPO_ROOT or REPO_ROOT in folder.parents:
        raise LexiconError("the lexicon folder must be outside the repo.")
    if not folder.is_dir():
        raise LexiconError("the lexicon folder does not exist.")
    return folder


def tier_settings() -> dict:
    return load_config("tiers.json")


def lexicon_file(key: str) -> Path:
    """The file named for key in config/tiers.json lexicon_files, inside the lexicon folder."""
    name = tier_settings()["lexicon_files"][key]
    path = lexicon_dir() / name
    if not path.exists():
        raise LexiconError(f"lexicon file for {key!r} is missing (see config/tiers.json).")
    return path
