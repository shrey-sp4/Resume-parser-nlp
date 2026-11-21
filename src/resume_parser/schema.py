"""Core types shared by all stages, plus loaders for config and JSON schemas.

Every stage returns a StageResult. Methods inside a stage return a MethodOutput,
and the fallback helper in pipeline.py turns those into Attempts.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
SCHEMA_DIR = REPO_ROOT / "schema"

SCHEMA_VERSION = "1.0"
STAGE_NAMES = tuple(f"stage{i}" for i in range(8))
TARGET_SECTIONS = ("personal", "education", "experience", "publications")
CANONICAL_SECTIONS = TARGET_SECTIONS + ("other",)
FLAG_SECTIONS = CANONICAL_SECTIONS + ("document",)

# Reason codes that are kept in the stage report but are never raised as flags.
INFO_CODES = frozenset({"not_implemented"})


class Status(str, Enum):
    """Status of a stage or of one unit (page, section) inside a stage."""

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"

    @property
    def rank(self) -> int:
        return {"OK": 0, "WARN": 1, "FAIL": 2}[self.value]


class AttemptStatus(str, Enum):
    """What happened when one method was tried."""

    ACCEPTED = "ACCEPTED"  # score reached the accept threshold
    BELOW_THRESHOLD = "BELOW_THRESHOLD"  # gave output, but the score was too low
    NO_OUTPUT = "NO_OUTPUT"  # ran, but gave nothing usable
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"  # stub, never chosen


def worst(statuses: list[Status]) -> Status:
    return max(statuses, key=lambda s: s.rank, default=Status.OK)


@dataclass(frozen=True)
class Reason:
    """A reason code with a short detail. section is one of FLAG_SECTIONS or None."""

    code: str
    detail: str = ""
    section: str | None = None

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail, "section": self.section}


@dataclass(frozen=True)
class MethodOutput:
    """What a method returns. data is None when the method has nothing usable."""

    data: Any = None
    reasons: tuple[Reason, ...] = ()
    implemented: bool = True


def not_implemented() -> MethodOutput:
    """Output of a stub method. The fallback helper never chooses it."""
    return MethodOutput(data=None, reasons=(Reason("not_implemented"),), implemented=False)


@dataclass(frozen=True)
class Method:
    """A named method of a stage.

    warn_code: if set, choosing this method always gives at least WARN with this
    code (used for last-resort methods such as whole_section_one_entry).
    stub: True while the method is not built yet. The cli "run" command refuses
    to run while any stub is left, unless --allow-stubs is given.
    """

    name: str
    fn: Callable[[Any], MethodOutput]
    warn_code: str | None = None
    stub: bool = False


@dataclass(frozen=True)
class Unit:
    """One piece of a stage that gets its own method choice (a page, a section)."""

    unit_id: str
    payload: Any
    methods: tuple[Method, ...] | None = None  # None means: use the stage's methods
    accept_threshold: float | None = None  # None means: use the stage's threshold
    require_output: bool = False  # FAIL this unit if no method gives output
    section: str | None = None  # section used when reasons become flags


@dataclass(frozen=True)
class Attempt:
    method: str
    score: float | None
    status: AttemptStatus
    reasons: tuple[Reason, ...] = ()

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "score": self.score,
            "status": self.status.value,
            "reasons": [r.code for r in self.reasons],
        }


@dataclass(frozen=True)
class UnitChoice:
    method_used: str | None
    methods_tried: tuple[Attempt, ...]
    status: Status

    def to_dict(self) -> dict:
        return {
            "method_used": self.method_used,
            "methods_tried": [a.to_dict() for a in self.methods_tried],
            "status": self.status.value,
        }


@dataclass
class StageResult:
    """Result of one stage. data is passed forward; the rest goes to the sidecar."""

    stage: str
    status: Status
    data: Any = None
    metrics: dict = field(default_factory=dict)
    reasons: list[Reason] = field(default_factory=list)
    method_used: str | None = None  # "per_unit" when the choice is made per unit
    methods_tried: list[Attempt] = field(default_factory=list)
    per_unit: dict[str, UnitChoice] = field(default_factory=dict)

    def to_report(self) -> dict:
        """The part that goes into the sidecar's stage_report (data is left out)."""
        return {
            "status": self.status.value,
            "metrics": self.metrics,
            "method_used": self.method_used,
            "methods_tried": [a.to_dict() for a in self.methods_tried],
            "per_unit": {k: v.to_dict() for k, v in self.per_unit.items()},
            "reasons": [r.to_dict() for r in self.reasons],
        }


@dataclass
class Context:
    """What a stage can see: the file name, earlier results, flags so far, thresholds."""

    resume_file: str
    thresholds: dict
    results: dict[str, StageResult] = field(default_factory=dict)
    flags: list[dict] = field(default_factory=list)


@dataclass
class PipelineRecord:
    """Final record for one resume."""

    resume_id: str
    resume_file: str
    stopped_at: int | None
    results: dict[str, StageResult]
    flags: list[dict]
    main: dict | None = None
    sidecar: dict | None = None


def resume_id_for(file_name: str | Path) -> str:
    """Stable id from the file stem, so names in file names are never printed."""
    stem = Path(file_name).stem
    return "r_" + hashlib.sha256(stem.encode("utf-8")).hexdigest()[:8]


class DataDirError(RuntimeError):
    """RESUME_DATA_DIR is unset, missing, or a path is outside it."""


def data_root() -> Path:
    """The real-data folder named by RESUME_DATA_DIR. Never inside this repo."""
    value = os.environ.get("RESUME_DATA_DIR", "").strip()
    if not value:
        raise DataDirError("RESUME_DATA_DIR is not set; point it at your private data folder.")
    root = Path(value).resolve()
    if not root.is_dir():
        raise DataDirError("RESUME_DATA_DIR does not point to an existing folder.")
    if root == REPO_ROOT or REPO_ROOT in root.parents:
        raise DataDirError("RESUME_DATA_DIR must be outside the repo.")
    return root


def require_inside_data_root(path: Path) -> Path:
    """Refuse to write anywhere except under RESUME_DATA_DIR."""
    root = data_root()
    resolved = Path(path).resolve()
    if resolved != root and root not in resolved.parents:
        raise DataDirError("refusing to write outside RESUME_DATA_DIR.")
    return resolved


def read_json(path: Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=None)
def _load_config_cached(name: str) -> str:
    return (CONFIG_DIR / name).read_text(encoding="utf-8")


def load_config(name: str) -> dict:
    """Load a config file (a fresh copy each time, so callers may change it)."""
    return json.loads(_load_config_cached(name))


def load_thresholds() -> dict:
    return load_config("thresholds.json")


@lru_cache(maxsize=None)
def _validator(schema_name: str):
    from jsonschema import Draft202012Validator

    schema = read_json(SCHEMA_DIR / schema_name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def schema_errors(obj: Any, schema_name: str) -> list[str]:
    """Return schema errors as 'path: rule' strings, sorted.

    Values are left out on purpose, so an error message never shows a name.
    """
    errors = []
    for err in _validator(schema_name).iter_errors(obj):
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{path}: {err.validator}")
    return sorted(set(errors))
