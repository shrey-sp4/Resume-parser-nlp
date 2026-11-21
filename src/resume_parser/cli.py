"""Command line.

    python -m resume_parser.cli run [PDF]     process RESUME_DATA_DIR/pdfs (or one file);
                                              refuses while stub methods are left,
                                              unless --allow-stubs is given;
                                              --save-stages keeps stage output for eval/funnel.py
    python -m resume_parser.cli validate-gt   check RESUME_DATA_DIR/ground_truth_v2/*.json

Output shows resume IDs only, never names, emails or phone numbers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from resume_parser.schema import DataDirError, data_root, read_json, resume_id_for, schema_errors


def _pdfs_in(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"), key=lambda p: p.name)


def cmd_run(args: argparse.Namespace) -> int:
    from resume_parser.pipeline import run_pipeline, unimplemented_methods
    from resume_parser.report import write_run_summary
    from resume_parser.writer import write_index, write_resume, write_stage_files

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
        pdfs = [pdf]
    else:
        pdfs = _pdfs_in(root / "pdfs")
        if not pdfs:
            print("run: no PDFs found in RESUME_DATA_DIR/pdfs.")

    errors = 0
    for pdf in pdfs:
        rid = resume_id_for(pdf.name)
        try:
            record = run_pipeline(pdf)
            write_resume(record, out_dir)
            if args.save_stages:
                write_stage_files(record, root / "reports" / "stages")
        except Exception as exc:  # report and carry on with the next resume
            errors += 1
            print(f"{rid} ERROR {type(exc).__name__}")
            continue
        stopped = f" stopped_at=stage{record.stopped_at}" if record.stopped_at is not None else ""
        print(f"{rid} {record.sidecar['decision']} flags={len(record.flags)}{stopped}")

    index = write_index(out_dir)
    summary = write_run_summary(out_dir, root / "reports")
    print(
        f"done: {len(pdfs)} processed, {errors} error(s); index has {len(index['resumes'])} resume(s); "
        f"{summary['decisions']['NEEDS_REVIEW']} need review"
    )
    return 1 if errors else 0


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
    run.set_defaults(func=cmd_run)

    gt = sub.add_parser(
        "validate-gt",
        help="check RESUME_DATA_DIR/ground_truth_v2/*.json against schema/resume.schema.json",
    )
    gt.set_defaults(func=cmd_validate_gt)
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
