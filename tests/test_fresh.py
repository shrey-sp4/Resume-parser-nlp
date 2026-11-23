"""The fresh set (eval/fresh.py): layout check with counts only, one run ever, tied to a clean
tree at a named commit, aggregates only, nothing written per resume, no error text. Synthetic
resumes only, in temporary folders; git is never run here (fresh._git is replaced)."""

from __future__ import annotations

import json
import re
import subprocess

import pytest

import check_no_data
import fresh
import make_synthetic_resumes as synth
from resume_parser.cli import main as cli_main
from resume_parser.schema import REPO_ROOT, resume_id_for

ID = re.compile(r"\b[re]_[0-9a-f]{8}\b")
HEAD = "ab12cd34" * 5


class FakeGit:
    """Stands in for git: a clean tree at HEAD, no tracked file, parser paths unchanged."""

    def __init__(self):
        self.head, self.status, self.diff_rc, self.calls = HEAD, "", 0, []

    def __call__(self, *args):
        self.calls.append(args)
        out, rc = "", 0
        if args[0] == "rev-parse":
            out = self.head + "\n"
        elif args[0] == "status":
            out = self.status
        elif args[0] == "diff":
            rc = self.diff_rc
        return subprocess.CompletedProcess(["git", *args], rc, out, "")


@pytest.fixture
def git(monkeypatch):
    fake = FakeGit()
    monkeypatch.setattr(fresh, "_git", fake)
    return fake


@pytest.fixture
def fresh_dirs(tmp_path, monkeypatch, data_dir, git):
    pdfs, out, backup = tmp_path / "fresh_pdfs", tmp_path / "fresh_out", tmp_path / "fresh_gt_backup"
    gt = pdfs / "gt"  # inside the PDF folder, as in the real layout
    synth.build_all(pdfs)
    gt.mkdir()
    backup.mkdir()
    for stem, record in synth.synthetic_ground_truth().items():
        (gt / f"{stem}.json").write_text(json.dumps(record), encoding="utf-8")
        (backup / f"{stem}.json").write_text(json.dumps(record), encoding="utf-8")
    for env in ("RESUME_FINAL_PDFS", "RESUME_FINAL_GT", "RESUME_FINAL_OUT"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(fresh.ENV_PDFS, str(pdfs))
    monkeypatch.setenv(fresh.ENV_GT, str(gt))
    monkeypatch.setenv(fresh.ENV_BACKUP, str(backup))
    monkeypatch.setenv(fresh.ENV_OUT, str(out))
    return {"pdfs": pdfs, "gt": gt, "out": out, "backup": backup, "data": data_dir, "tmp": tmp_path}


def _stems():
    return sorted(synth.synthetic_ground_truth())


def _private(text: str, d: dict, extra=()) -> None:
    """No stem, resume ID, temporary path or extra secret in text."""
    for stem in _stems():
        assert stem not in text
    assert not ID.search(text)
    assert str(d["tmp"]) not in text and d["tmp"].name not in text
    for value in extra:
        assert value not in text


def _tree(folder):
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")) if folder.is_dir() else []


def _everything(d: dict, captured) -> str:
    texts = [captured.out, captured.err]
    texts += [p.read_text(encoding="utf-8") for p in d["out"].glob("*") if p.is_file()]
    texts += [p.read_text(encoding="utf-8") for p in (d["data"] / "reports").glob("*") if p.is_file()]
    return "\n".join(texts)


# ---------------------------------------------------------------- layout check


def test_check_passes_with_counts_only(fresh_dirs, capsys):
    assert fresh.main(["check"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("fresh check: PASS")
    assert re.search(r"PDFs:\s+3\b", out) and re.search(r"GT files:\s+3\b", out)
    _private(out, fresh_dirs)
    assert not fresh_dirs["out"].exists()  # the check writes nothing


def _gt(d, name):
    return d["gt"] / name


@pytest.mark.parametrize("case, label", [
    ("pdf_without_gt", "PDFs with no GT file"),
    ("gt_without_pdf", "GT files with no PDF"),
    ("not_pdf", "resume_file values not ending in .pdf"),
    ("schema", "GT files failing the GT v2 schema"),
    ("not_json", "GT files failing the GT v2 schema"),
    ("two_gt", "PDFs named by more than one GT file"),
])
def test_check_failures_show_counts_never_values(fresh_dirs, capsys, case, label):
    d = fresh_dirs
    secret = "zz_secret_candidate"
    record = dict(synth.synthetic_ground_truth()[_stems()[0]])
    if case == "pdf_without_gt":
        (d["pdfs"] / f"{secret}.pdf").write_bytes((d["pdfs"] / f"{_stems()[0]}.pdf").read_bytes() + b"\n%x")
    elif case == "gt_without_pdf":
        _gt(d, f"{_stems()[0]}.json").write_text(json.dumps(dict(record, resume_file=f"{secret}.pdf")), encoding="utf-8")
    elif case == "not_pdf":
        _gt(d, f"{_stems()[0]}.json").write_text(json.dumps(dict(record, resume_file=f"{secret}.docx")), encoding="utf-8")
    elif case == "schema":
        _gt(d, f"{_stems()[0]}.json").write_text(json.dumps({"resume_file": record["resume_file"],
                                                              "candidate_name": secret, "bad": 1}), encoding="utf-8")
    elif case == "not_json":
        _gt(d, f"{_stems()[0]}.json").write_text("{" + secret, encoding="utf-8")
    elif case == "two_gt":
        _gt(d, f"{secret}.json").write_text(json.dumps(record), encoding="utf-8")
    assert fresh.main(["check"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("fresh check: FAIL")
    assert re.search(re.escape(label) + r"[^:\n]*:\s+[1-9]", out), out
    _private(out, d, [secret, ".docx"])


def test_check_folder_rules(fresh_dirs, capsys, monkeypatch):
    d = fresh_dirs
    cases = [
        (fresh.ENV_BACKUP, str(d["tmp"] / "missing"), "RESUME_FRESH_BACKUP is not an existing folder"),
        (fresh.ENV_BACKUP, str(d["data"] / "reports"), "RESUME_FRESH_BACKUP is inside RESUME_DATA_DIR"),
        (fresh.ENV_BACKUP, str(d["gt"]), "RESUME_FRESH_BACKUP and RESUME_FRESH_GT overlap"),
        (fresh.ENV_OUT, str(d["pdfs"] / "out"), "RESUME_FRESH_OUT and RESUME_FRESH_PDFS overlap"),
        (fresh.ENV_PDFS, str(REPO_ROOT / "docs"), "RESUME_FRESH_PDFS is inside the repo"),
        (fresh.ENV_GT, "", "RESUME_FRESH_GT is not set"),
    ]
    for env, value, message in cases:
        with monkeypatch.context() as m:
            m.setenv(env, value)
            assert fresh.main(["check"]) == 1
            out = capsys.readouterr().out
            assert message in out, (env, out)
            assert "files not checked" in out
            _private(out, d, [str(REPO_ROOT)])
    with monkeypatch.context() as m:  # a fresh folder overlapping the final set
        m.setenv("RESUME_FINAL_PDFS", str(d["pdfs"]))
        assert fresh.main(["check"]) == 1
        assert "overlaps a folder of the final set" in capsys.readouterr().out


def test_check_counts_repo_files_naming_the_set_and_duplicate_pdfs(fresh_dirs, capsys, git, monkeypatch, tmp_path):
    d = fresh_dirs
    notes = tmp_path / "notes.md"
    notes.write_text(f"see {resume_id_for(_stems()[0])}\n", encoding="utf-8")
    real = check_no_data.find_problems
    monkeypatch.setattr(fresh, "_repo_files", lambda: ["notes.md"])
    monkeypatch.setattr(check_no_data, "find_problems", lambda paths, _root, markers: real(paths, tmp_path, markers))
    (d["data"] / "pdfs" / "other.pdf").write_bytes((d["pdfs"] / f"{_stems()[1]}.pdf").read_bytes())
    assert fresh.main(["check"]) == 1
    out = capsys.readouterr().out
    assert re.search(r"repo files naming a fresh file or resume ID:\s+1", out)
    assert re.search(r"PDFs identical to a PDF of another set:\s+1", out)
    _private(out, d)


# ---------------------------------------------------------------- the run: refusals


def _no_log(d):
    assert not (d["out"] / fresh.LOG_FILE).exists()
    assert not (d["data"] / "reports" / fresh.LOG_FILE).exists()


@pytest.mark.parametrize("setup, message", [
    (lambda g: setattr(g, "status", " M src/x.py\n?? new.txt\n"), "not clean (2 changed or untracked"),
    (lambda g: setattr(g, "head", "ff" * 20), "HEAD is not the commit"),
    (lambda g: setattr(g, "diff_rc", 1), "differ from --parser-commit"),
])
def test_run_refused_without_a_clean_tree_at_the_named_commit(fresh_dirs, git, capsys, setup, message):
    setup(git)
    assert fresh.main(["run", "--commit", HEAD[:12], "--parser-commit", "6c56966"]) == 2
    captured = capsys.readouterr()
    assert message in captured.err
    _private(captured.out + captured.err, fresh_dirs)
    _no_log(fresh_dirs)


def test_run_refused_on_a_bad_commit_argument_or_a_failing_layout(fresh_dirs, capsys):
    assert fresh.main(["run", "--commit", "xyz"]) == 2
    assert "7 to 40 hex" in capsys.readouterr().err
    (fresh_dirs["gt"] / f"{_stems()[0]}.json").unlink()
    assert fresh.main(["run", "--commit", HEAD]) == 2
    captured = capsys.readouterr()
    assert "layout check fails" in captured.err and "FAIL" in captured.out
    _private(captured.out + captured.err, fresh_dirs)
    _no_log(fresh_dirs)


def test_git_state_reads_head_status_and_parser_diff(git):
    git.status = " M a.py\n?? b.txt\n\n"
    state = fresh.git_state("6c56966")
    assert state == {"head": HEAD, "dirty": 2, "parser_commit": "6c56966", "parser_unchanged": True}
    assert ("diff", "--quiet", "6c56966", "HEAD", "--", "src", "config", "schema") in git.calls
    git.status, git.diff_rc = "", 1
    assert fresh.git_state("6c56966")["parser_unchanged"] is False
    fresh.require_frozen(fresh.git_state(), HEAD[:7].upper())  # a prefix, any case
    with pytest.raises(fresh.FreshError):
        fresh.require_frozen(fresh.git_state(), HEAD[:6])  # too short


# ---------------------------------------------------------------- the run


def test_one_run_aggregates_only_then_refused_forever(fresh_dirs, capsys):
    d = fresh_dirs
    (d["data"] / "reports" / "validation_runs.log").write_text('{"tool": "evaluate", "run": 1}\n', encoding="utf-8")
    data_before = _tree(d["data"])
    validation_before = (d["data"] / "reports" / "validation_runs.log").read_bytes()

    assert fresh.main(["run", "--commit", HEAD[:10], "--parser-commit", "6c56966"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith(f"commit: {HEAD}\n")
    for text in ("=== FRESH SET", "entry matching", "S5 entry matched", "decisions: ACCEPT",
                 "wrong items in resumes the parser flagged", "CHECK VALIDITY (fresh)", "name_norm"):
        assert text in captured.out, text
    assert "resumes: 3" in captured.out and "errors: 0" in captured.out
    # only the log and the two reports; the data folder gains the mirror log and nothing else
    assert _tree(d["out"]) == sorted(fresh.OUT_FILES)
    assert _tree(d["data"]) == sorted(data_before + ["reports/" + fresh.LOG_FILE])
    assert (d["data"] / "reports" / "validation_runs.log").read_bytes() == validation_before
    assert not any(p.name == "final_runs.log" for p in d["tmp"].rglob("*"))
    report = json.loads((d["out"] / fresh.REPORT_JSON).read_text(encoding="utf-8"))
    assert report["resumes"] == 3 and report["parser_unchanged"] is True
    assert set(report["evaluate"]) >= {"point", "ci", "counts", "fields"} and "per_resume" not in report["evaluate"]
    assert sum(report["decisions"].values()) == 3
    _private(_everything(d, captured), d)
    stems = set(_stems())
    for path in d["tmp"].rglob("*"):  # no file anywhere is named after a resume, apart from the inputs
        if d["pdfs"] not in path.parents and d["backup"] not in path.parents and path != d["pdfs"]:
            assert path.stem not in stems and not ID.search(path.name), path.name

    log = (d["out"] / fresh.LOG_FILE).read_text(encoding="utf-8")
    assert log == (d["data"] / "reports" / fresh.LOG_FILE).read_text(encoding="utf-8")
    entry = json.loads(log)
    assert entry["commit"] == HEAD and entry["run"] == 1 and entry["tree_clean"] and entry["git_enforced"]

    assert fresh.main(["run", "--commit", HEAD]) == 2  # a second run, ever
    assert "already been run" in capsys.readouterr().err
    (d["out"] / fresh.LOG_FILE).unlink()  # the mirror log still refuses it
    assert fresh.main(["run", "--commit", HEAD]) == 2
    assert "already been run" in capsys.readouterr().err
    assert not (d["out"] / fresh.LOG_FILE).exists()
    assert len((d["data"] / "reports" / fresh.LOG_FILE).read_text(encoding="utf-8").splitlines()) == 1
    assert fresh.main(["check"]) == 0
    assert re.search(r"runs logged so far:\s+1\b", capsys.readouterr().out)


def test_an_exception_shows_its_class_name_and_a_count_only(fresh_dirs, capsys, monkeypatch):
    d = fresh_dirs
    real = fresh.run_pipeline
    victim = _stems()[1]
    secret_path = f"C:\\secret_folder\\{victim}.pdf"

    def flaky(pdf):
        if pdf.stem == victim:
            print(f"parser chatter about {victim}")
            raise ValueError(f"cannot read {victim} at {secret_path}")
        return real(pdf)

    monkeypatch.setattr(fresh, "run_pipeline", flaky)
    assert fresh.main(["run", "--commit", HEAD]) == 0
    captured = capsys.readouterr()
    assert "errors: 1 (ValueError 1)" in captured.out
    assert json.loads((d["out"] / fresh.REPORT_JSON).read_text(encoding="utf-8"))["errors"] == {"ValueError": 1}
    _private(_everything(d, captured), d, ["secret_folder", "cannot read", "chatter"])


def test_an_exception_outside_a_resume_stops_the_run_with_its_class_name_only(fresh_dirs, capsys, monkeypatch):
    d = fresh_dirs

    def broken(pairs):
        raise KeyError(f"{_stems()[0]} at C:\\secret_folder")

    monkeypatch.setattr(fresh, "score", broken)
    assert fresh.main(["run", "--commit", HEAD]) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == "fresh: stopped by KeyError (no further detail is shown)"
    _private(_everything(d, captured), d, ["secret_folder", "Traceback"])
    assert (d["out"] / fresh.LOG_FILE).is_file()  # logged before the work: the crashed run counts
    assert fresh.main(["run", "--commit", HEAD]) == 2


def test_in_memory_scores_equal_the_on_disk_final_path(fresh_dirs, capsys, monkeypatch, tmp_path):
    """The same resumes through cli run + evaluate + funnel on the final folders give the same
    entry counts, field checks and funnel tables as the in-memory fresh run."""
    import shutil

    import evaluate
    import funnel
    from resume_parser import final

    d = fresh_dirs
    assert fresh.main(["run", "--commit", HEAD]) == 0
    fresh_text = capsys.readouterr().out
    fresh_json = json.loads((d["out"] / fresh.REPORT_JSON).read_text(encoding="utf-8"))

    fpdfs, fgt, fout = tmp_path / "final_pdfs", tmp_path / "final_gt", tmp_path / "final_out"
    shutil.copytree(d["pdfs"], fpdfs, ignore=shutil.ignore_patterns("gt"))
    shutil.copytree(d["gt"], fgt)
    monkeypatch.setenv(final.ENV_PDFS, str(fpdfs))
    monkeypatch.setenv(final.ENV_GT, str(fgt))
    monkeypatch.setenv(final.ENV_OUT, str(fout))
    assert cli_main(["run", "--groups", "final"]) == 0
    evaluate.common.use_final()
    try:
        res = evaluate.aggregate([evaluate.score_resume(evaluate.load_gt(s), evaluate.load_pred(s)) for s in _stems()])
    finally:
        evaluate.common.use_final(False)
    assert fresh_json["evaluate"]["counts"] == res["counts"]
    assert fresh_json["evaluate"]["fields"] == res["fields"]
    capsys.readouterr()
    assert funnel.main(["--final"]) == 0
    disk = capsys.readouterr().out

    def tables(text):
        return text[text.index("--- name"):text.index("=== FALSE-POSITIVE")].replace("final", "fresh")

    assert tables(disk) == tables(fresh_text)


# ---------------------------------------------------------------- isolation


def test_fresh_is_never_a_group_and_the_repo_check_knows_its_names(fresh_dirs, capsys, tmp_path):
    assert cli_main(["run", "--groups", "fresh"]) == 2
    assert cli_main(["check-determinism", "--groups", "fresh"]) == 2
    capsys.readouterr()
    markers = check_no_data.fresh_markers()
    stem = _stems()[0]
    assert stem in markers and resume_id_for(stem) in markers
    assert markers <= check_no_data.guarded_markers()
    (tmp_path / "notes.md").write_text(f"see {stem}\n", encoding="utf-8")
    assert check_no_data.find_problems(["notes.md"], tmp_path, markers) == [
        "notes.md:1: guarded-group resume ID or file name in file contents"]
