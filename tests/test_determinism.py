"""Two runs over the same fake resumes give byte-identical outputs, index and report."""

from __future__ import annotations

import shutil
from pathlib import Path

import make_synthetic_resumes as synth
from resume_parser.cli import main


def _snapshot(root: Path) -> dict[str, bytes]:
    files = {}
    for sub in ("outputs", "reports"):
        for path in sorted((root / sub).rglob("*")):
            if path.is_file():
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _clear(root: Path) -> None:
    for sub in ("outputs", "reports"):
        shutil.rmtree(root / sub)
        (root / sub).mkdir()


def test_two_runs_are_byte_identical(data_dir, tmp_path, capsys):
    synth.build_all(data_dir / "pdfs")
    synth.make_image_only_pdf(data_dir / "pdfs" / "synthetic_d_scan.pdf")

    assert main(["run", "--allow-stubs"]) == 0
    first = _snapshot(data_dir)
    _clear(data_dir)
    assert main(["run", "--allow-stubs"]) == 0
    second = _snapshot(data_dir)

    assert first == second
    assert "reports/run_summary.json" in first
    assert "outputs/run_index.json" in first
    # 3 main JSONs (the scan stops at stage 1), 4 sidecars, 1 index
    assert sum(k.endswith(".meta.json") for k in first) == 4
    assert sum(k.endswith(".json") and not k.endswith(".meta.json") for k in first) == 3 + 1 + 1


def test_outputs_have_no_absolute_paths_or_names(data_dir, capsys):
    synth.build_all(data_dir / "pdfs")
    main(["run", "--allow-stubs"])
    printed = capsys.readouterr().out
    for content in _snapshot(data_dir).values():
        text = content.decode("utf-8")
        assert str(data_dir) not in text
        assert data_dir.as_posix() not in text
    # Printed output shows resume IDs only
    for name in ("Testwood", "Fakely", "Samplestone", "synthetic_"):
        assert name not in printed
