"""Shared test setup. Every test gets its own fake data folder in tmp_path, so the
real RESUME_DATA_DIR is never read or written by the tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import make_synthetic_resumes as synth
from resume_parser.schema import Context, StageResult, Status, load_thresholds

DATA_SUBDIRS = ("pdfs", "ground_truth_v2", "outputs", "review_queue", "reports")


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "resume_data"
    for sub in DATA_SUBDIRS:
        (root / sub).mkdir(parents=True)
    monkeypatch.setenv("RESUME_DATA_DIR", str(root))
    return root


@pytest.fixture
def synthetic_pdfs(tmp_path: Path) -> list[Path]:
    return synth.build_all(tmp_path / "synthetic")


@pytest.fixture
def ctx() -> Context:
    return Context(resume_file="sample.pdf", thresholds=load_thresholds())


def fake_stage(name: str, status: Status = Status.OK, reasons=None, data=None):
    """A stage function that returns a fixed result."""

    def run(pdf_path, ctx):
        return StageResult(stage=name, status=status, data=data, reasons=list(reasons or []))

    return run


def fake_stages(overrides: dict | None = None) -> list:
    """Eight OK stages, with some replaced by index."""
    overrides = overrides or {}
    return [(f"stage{i}", overrides.get(i, fake_stage(f"stage{i}"))) for i in range(8)]
