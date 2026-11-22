"""Ground-truth groups (docs/design.md, section 6).

RESUME_DATA_DIR/ground_truth_v2/_split.json lists dev, dev2, validation and (optionally)
test_frozen; tuned is every other GT file. dev, tuned and dev2 are development groups (per-resume
results allowed). validation and test_frozen are guarded: aggregates only, every run logged. An entry may be a file stem, a file name (.json or .pdf), or a resume ID ("r_...",
looked up in private/id_map.json, or matched against the GT stems).

PDFs with no ground truth form a fourth group, "unlabelled".
"""

from __future__ import annotations

from pathlib import Path

from resume_parser.schema import data_root, read_json, resume_id_for

GT_GROUPS = ("dev", "tuned", "dev2", "validation", "test_frozen")
DEV_GROUPS = ("dev", "tuned", "dev2")
GUARDED_GROUPS = ("validation", "test_frozen")
LISTED_GROUPS = ("dev", "dev2", "validation", "test_frozen")  # the groups _split.json names
ALL_GROUPS = GT_GROUPS + ("unlabelled",)
SPLIT_FILE = "_split.json"


class GroupError(ValueError):
    """The split cannot be read or does not match the ground truth. Messages show IDs only."""


def split_path() -> Path:
    return data_root() / "ground_truth_v2" / SPLIT_FILE


def has_split() -> bool:
    return split_path().is_file()


def _id_map() -> dict:
    path = data_root() / "private" / "id_map.json"
    return read_json(path) if path.is_file() else {}


def _to_stem(entry, by_id: dict[str, str]) -> str:
    text = str(entry).strip()
    if text in by_id:
        return by_id[text]
    name = Path(text).name
    for suffix in (".json", ".pdf"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def load_groups() -> dict[str, list[str]]:
    """Stems per GT group, each list sorted."""
    folder = data_root() / "ground_truth_v2"
    if not has_split():
        raise GroupError(f"ground_truth_v2/{SPLIT_FILE} is missing.")
    split = read_json(split_path())
    all_stems = sorted(p.stem for p in folder.glob("*.json") if not p.name.startswith("_"))
    known = set(all_stems)
    by_id = {resume_id_for(s): s for s in all_stems}
    for rid, entry in _id_map().items():
        if isinstance(entry, dict) and entry.get("file") in known:
            by_id.setdefault(rid, entry["file"])
            if entry.get("eval_id"):
                by_id.setdefault(entry["eval_id"], entry["file"])

    groups: dict[str, list[str]] = {}
    for name in LISTED_GROUPS:
        stems = sorted({_to_stem(s, by_id) for s in split.get(name, [])})
        missing = [s for s in stems if s not in known]
        if missing:
            ids = ", ".join(resume_id_for(s) for s in missing)
            raise GroupError(f"{SPLIT_FILE}: {name} lists {len(missing)} file(s) with no ground truth ({ids}).")
        groups[name] = stems
    for i, a in enumerate(LISTED_GROUPS):
        for b in LISTED_GROUPS[i + 1:]:
            both = set(groups[a]) & set(groups[b])
            if both:
                raise GroupError(f"{SPLIT_FILE}: {len(both)} file(s) are in both {a} and {b}.")
    taken = set().union(*(groups[g] for g in LISTED_GROUPS))
    groups["tuned"] = [s for s in all_stems if s not in taken]
    return {g: groups[g] for g in GT_GROUPS}


def group_of_stems(stems: list[str]) -> dict[str, str]:
    """stem -> group name, for the given PDF stems ("unlabelled" when there is no GT)."""
    groups = load_groups()
    where = {s: g for g, members in groups.items() for s in members}
    return {s: where.get(s, "unlabelled") for s in stems}


def parse_group_list(text: str) -> list[str]:
    names = [x.strip() for x in str(text).split(",") if x.strip()]
    bad = [x for x in names if x not in ALL_GROUPS]
    if bad or not names:
        raise GroupError(f"--groups takes a comma list of {', '.join(ALL_GROUPS)}.")
    return sorted(set(names), key=ALL_GROUPS.index)
