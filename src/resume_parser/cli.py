"""Command line.

    python -m resume_parser.cli run [PDF]     process RESUME_DATA_DIR/pdfs (or one file);
                                              refuses while stub methods are left,
                                              unless --allow-stubs is given;
                                              --save-stages keeps stage output for eval/funnel.py;
                                              --groups picks GT groups (default dev,tuned;
                                              test_frozen only when named)
    python -m resume_parser.cli validate-gt   check RESUME_DATA_DIR/ground_truth_v2/*.json
    python -m resume_parser.cli check-determinism [--groups]
                                              run the selected PDFs twice into
                                              RESUME_DATA_DIR/reports/determinism/a and b and
                                              compare every output byte for byte

Each run writes outputs/<name>.json and .meta.json, outputs/run_index.json and
review_queue/review_queue.json (resume IDs and reason codes of the resumes that need a person).

Output shows resume IDs only, never names, emails or phone numbers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from resume_parser import groups as groups_mod
from resume_parser.schema import DataDirError, data_root, read_json, resume_id_for, schema_errors

DEFAULT_GROUPS = "dev,tuned"


def _pdfs_in(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"), key=lambda p: p.name)


def _select_by_group(pdfs: list[Path], groups_arg: str | None) -> tuple[list[tuple[Path, str]], list[str]]:
    """(pdf, group) pairs to process, and the groups asked for.

    With a _split.json, only the named groups run (default dev,tuned); test_frozen runs only
    when named. Without one (no ground truth set up), every PDF runs and --groups is refused.
    """
    if not groups_mod.has_split():
        if groups_arg is not None:
            raise groups_mod.GroupError("--groups needs ground_truth_v2/_split.json.")
        return [(p, "unlabelled") for p in pdfs], []
    wanted = groups_mod.parse_group_list(groups_arg if groups_arg is not None else DEFAULT_GROUPS)
    where = groups_mod.group_of_stems([p.stem for p in pdfs])
    return [(p, where[p.stem]) for p in pdfs if where[p.stem] in wanted], wanted


def cmd_run(args: argparse.Namespace) -> int:
    from resume_parser.pipeline import run_pipeline, unimplemented_methods
    from resume_parser.report import write_run_summary
    from resume_parser.writer import (
        unknown_headings,
        write_index,
        write_resume,
        write_review_queue,
        write_stage_files,
        write_unknown_headings,
    )

    # Stub methods pass their pages and sections as OK "not_implemented", so a run
    # with stubs left gives outputs that look fine but hold nothing. Refuse by default.
    stubs = unimplemented_methods()
    if stubs and not args.allow_stubs:
        print(f"run: these methods are not built yet ({len(stubs)}):", file=sys.stderr)
        for name in stubs:
            print(f"  {name}", file=sys.stderr)
        print("Pass --allow-stubs to run anyway (outputs are not real results).", file=sys.stderr)
        return 2

    root = data_root()
    if stubs:
        print(f"run: --allow-stubs given, {len(stubs)} method(s) are stubs", file=sys.stderr)
    out_dir = root / "outputs"
    if args.pdf:
        pdf = Path(args.pdf)
        if not pdf.is_file():
            print("run: the given PDF does not exist.", file=sys.stderr)
            return 2
        candidates = [pdf]
    else:
        candidates = _pdfs_in(root / "pdfs")
        if not candidates:
            print("run: no PDFs found in RESUME_DATA_DIR/pdfs.")
    try:
        selected, wanted = _select_by_group(candidates, args.groups)
    except groups_mod.GroupError as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 2
    if args.pdf and not selected:
        print(f"run: {resume_id_for(candidates[0].name)} is not in the groups asked for "
              f"({','.join(wanted)}).", file=sys.stderr)
        return 2
    if wanted:
        counts = {g: sum(1 for _, gr in selected if gr == g) for g in wanted}
        print("groups: " + ", ".join(f"{g} {n}" for g, n in counts.items()))
    pdfs = [p for p, _ in selected]
    group_of = {p: g for p, g in selected}
    frozen_done = 0
    review: list[tuple[str, str]] = []

    errors = 0
    for pdf in pdfs:
        rid = resume_id_for(pdf.name)
        quiet = group_of[pdf] == "test_frozen"  # never show test_frozen per resume
        try:
            record = run_pipeline(pdf)
            write_resume(record, out_dir)
            if args.save_stages:
                write_stage_files(record, root / "reports" / "stages")
        except Exception as exc:  # report and carry on with the next resume
            errors += 1
            print("test_frozen: one resume failed" if quiet else f"{rid} ERROR {type(exc).__name__}")
            continue
        if quiet:
            frozen_done += 1
            continue
        unknown = unknown_headings(record)
        review += [(rid, text) for text in unknown]
        stopped = f" stopped_at=stage{record.stopped_at}" if record.stopped_at is not None else ""
        print(f"{rid} {record.sidecar['decision']} flags={len(record.flags)} unknown_headings={len(unknown)}{stopped}")
    if frozen_done:
        print(f"test_frozen: {frozen_done} processed (not shown per resume)")

    if args.save_stages:
        write_unknown_headings(review, root / "reports")
        print(f"unknown heading-like lines: {len(review)} (for review in RESUME_DATA_DIR/reports/"
              "stage3_unknown_headings.txt; test_frozen is never listed)")
    index = write_index(out_dir)
    write_review_queue(index, root / "review_queue")
    summary = write_run_summary(out_dir, root / "reports")
    print(
        f"done: {len(pdfs)} processed, {errors} error(s); index has {len(index['resumes'])} resume(s); "
        f"{summary['decisions']['NEEDS_REVIEW']} need review, {summary['decisions']['FAIL']} failed"
    )
    return 1 if errors else 0


def cmd_check_determinism(args: argparse.Namespace) -> int:
    """Run the selected PDFs twice into two folders inside the data folder and compare every
    output file byte for byte. Prints counts and resume IDs only. test_frozen is refused."""
    import shutil

    from resume_parser.pipeline import run_pipeline
    from resume_parser.schema import require_inside_data_root
    from resume_parser.writer import write_index, write_resume, write_review_queue

    root = data_root()
    try:
        selected, wanted = _select_by_group(_pdfs_in(root / "pdfs"), args.groups)
    except groups_mod.GroupError as exc:
        print(f"check-determinism: {exc}", file=sys.stderr)
        return 2
    if "test_frozen" in wanted:
        print("check-determinism: test_frozen is not allowed here.", file=sys.stderr)
        return 2
    base = require_inside_data_root(root / "reports" / "determinism")
    for run in ("a", "b"):
        folder = base / run
        if folder.exists():
            shutil.rmtree(folder)
        for pdf, _group in selected:
            write_resume(run_pipeline(pdf), folder / "outputs")
        index = write_index(folder / "outputs")
        write_review_queue(index, folder / "review_queue")
    files_a = sorted(p.relative_to(base / "a") for p in (base / "a").rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(base / "b") for p in (base / "b").rglob("*") if p.is_file())
    differ = [f for f in files_a if f not in files_b or (base / "a" / f).read_bytes() != (base / "b" / f).read_bytes()]
    differ += [f for f in files_b if f not in files_a]
    def stem_of(name: str) -> str:
        return name[: -len(".meta.json")] if name.endswith(".meta.json") else name[: -len(".json")]

    ids = sorted({resume_id_for(stem_of(f.name) + ".pdf") for f in differ if f.parent.name == "outputs"
                  and f.name != "run_index.json"})
    others = sorted({f.name for f in differ if f.name in ("run_index.json", "review_queue.json")})
    print(f"check-determinism: {len(selected)} resume(s), {len(files_a)} file(s) compared, {len(set(differ))} differ")
    for rid in ids:
        print(f"  {rid} differs")
    for name in others:
        print(f"  {name} differs")
    return 1 if differ else 0


def cmd_validate_gt(args: argparse.Namespace) -> int:
    folder = data_root() / "ground_truth_v2"
    files = sorted(
        (p for p in folder.glob("*.json") if not p.name.startswith("_")),
        key=lambda p: p.name,
    ) if folder.is_dir() else []
    invalid = 0
    for path in files:
        rid = resume_id_for(path.name)
        try:
            errors = schema_errors(read_json(path), "resume.schema.json")
        except ValueError:  # bad JSON or bad encoding
            errors = ["(file): not valid UTF-8 JSON"]
        if errors:
            invalid += 1
            print(f"{rid} INVALID ({len(errors)} error(s))")
            for err in errors:
                print(f"    {err}")
        else:
            print(f"{rid} OK")
    print(f"checked {len(files)} file(s), {invalid} invalid")
    return 1 if invalid else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m resume_parser.cli",
        description="Rule-based parser for academic CVs. Data folder: RESUME_DATA_DIR.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="process all PDFs in RESUME_DATA_DIR/pdfs, or one given file")
    run.add_argument("pdf", nargs="?", help="optional path to one PDF")
    run.add_argument(
        "--allow-stubs",
        action="store_true",
        help="run even though some methods are not built yet (for testing only)",
    )
    run.add_argument(
        "--save-stages",
        action="store_true",
        help="also save stage 1, 3, 4 and 5 output per resume under RESUME_DATA_DIR/reports/stages",
    )
    run.add_argument(
        "--groups",
        help="comma list of dev, tuned, test_frozen, unlabelled (default dev,tuned; "
        "test_frozen runs only when named)",
    )
    run.set_defaults(func=cmd_run)

    gt = sub.add_parser(
        "validate-gt",
        help="check RESUME_DATA_DIR/ground_truth_v2/*.json against schema/resume.schema.json",
    )
    gt.set_defaults(func=cmd_validate_gt)

    det = sub.add_parser("check-determinism", help="run twice and compare every output byte for byte")
    det.add_argument("--groups", help="comma list of dev, tuned, unlabelled (default dev,tuned)")
    det.set_defaults(func=cmd_check_determinism)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DataDirError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
