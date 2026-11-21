"""Pipeline runner: runs stages 0 to 7 in order and applies the gate rules.

Gate rules (docs/design.md, section 3):
- FAIL in stage 0 or 1 stops the resume. Only a sidecar and an index entry are written.
- WARN or FAIL in a later stage is recorded as flags and the run carries on.

Every stage picks between named methods with run_with_fallbacks(). The choice is
deterministic: methods are tried in a fixed order, ties go to the earlier method,
and units (pages, sections) are processed in a fixed order.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from resume_parser.schema import (
    Attempt,
    AttemptStatus,
    Context,
    Method,
    PipelineRecord,
    Reason,
    StageResult,
    Status,
    Unit,
    UnitChoice,
    load_thresholds,
    resume_id_for,
    is_info,
    worst,
)

GATED_STAGES = (0, 1)

StageFn = Callable[[Path, Context], StageResult]
CheckFn = Callable[[Any], float]


# ---------------------------------------------------------------- fallbacks


@dataclass(frozen=True)
class _Choice:
    method_used: str | None
    attempts: tuple[Attempt, ...]
    data: Any
    status: Status
    reasons: tuple[Reason, ...]
    outputs: dict  # method name -> data, for every method that gave output


def _score_and_details(result) -> tuple[float, tuple]:
    """A check returns a score, or (score, {name: value}) with the values behind it."""
    if isinstance(result, tuple):
        score, details = result
        return _clamp(score), tuple(sorted((details or {}).items()))
    return _clamp(result), ()


def _clamp(score: float) -> float:
    return round(min(1.0, max(0.0, float(score))), 4)


def _dedupe(reasons: list[Reason]) -> tuple[Reason, ...]:
    seen: list[Reason] = []
    for r in reasons:
        if r not in seen:
            seen.append(r)
    return tuple(seen)


def _choose(
    methods: Sequence[Method],
    check: CheckFn,
    threshold: float,
    payload: Any,
    require_output: bool,
    fail_if_unusable: bool,
    unusable_code: str,
    run_all: bool = False,
) -> _Choice:
    """Try methods in order and pick one. See run_with_fallbacks for the rules."""
    attempts: list[Attempt] = []
    outputs: list[Any] = []
    accepted: int | None = None

    for method in methods:
        if method.run_if is not None and not method.run_if(list(attempts)):
            attempts.append(Attempt(method.name, None, AttemptStatus.SKIPPED))
            outputs.append(None)
            continue
        out = method.fn(payload)
        if not out.implemented:
            attempts.append(Attempt(method.name, None, AttemptStatus.NOT_IMPLEMENTED, out.reasons))
            outputs.append(None)
            continue
        if out.data is None:
            attempts.append(Attempt(method.name, None, AttemptStatus.NO_OUTPUT, out.reasons))
            outputs.append(None)
            continue
        score, details = _score_and_details(check(out.data))
        passed = score >= threshold
        status = AttemptStatus.ACCEPTED if passed else AttemptStatus.BELOW_THRESHOLD
        attempts.append(Attempt(method.name, score, status, out.reasons, details))
        outputs.append(out)
        if passed and accepted is None:
            accepted = len(attempts) - 1
            if not run_all:
                break

    chosen: int | None
    reasons: list[Reason] = []
    if accepted is not None:
        chosen, status = accepted, Status.OK
    else:
        scored = [i for i, a in enumerate(attempts) if a.score is not None]
        if scored:
            # Highest score wins; on a tie the earlier method wins.
            chosen = max(scored, key=lambda i: (attempts[i].score, -i))
            status = Status.WARN
            reasons.append(
                Reason(
                    "below_accept_threshold",
                    f"best score {attempts[chosen].score} below {threshold}",
                )
            )
        else:
            chosen = None
            stub_left = any(a.status is AttemptStatus.NOT_IMPLEMENTED for a in attempts)
            if require_output:
                status = Status.FAIL
                reasons.append(Reason(unusable_code))
            elif stub_left:
                # Some methods are still stubs, so we cannot yet say the unit is
                # unusable (for example: text methods not written, OCR missing).
                status = Status.OK
                reasons.append(Reason("not_implemented"))
            elif fail_if_unusable:
                status = Status.FAIL
                reasons.append(Reason(unusable_code))
            else:
                status = Status.WARN
                reasons.append(Reason(unusable_code))

    all_outputs = {a.method: o.data for a, o in zip(attempts, outputs) if o is not None}
    if chosen is not None:
        method = methods[chosen]
        reasons.extend(outputs[chosen].reasons)
        if method.warn_code:
            status = worst([status, Status.WARN])
            reasons.append(Reason(method.warn_code))
        return _Choice(method.name, tuple(attempts), outputs[chosen].data, status, _dedupe(reasons), all_outputs)

    if status is not Status.OK:
        for a in attempts:
            reasons.extend(r for r in a.reasons if not is_info(r.code))
    return _Choice(None, tuple(attempts), None, status, _dedupe(reasons), all_outputs)


def run_with_fallbacks(
    stage: str,
    methods: Sequence[Method],
    check: CheckFn,
    accept_threshold: float,
    payload: Any = None,
    units: Sequence[Unit] | None = None,
    fail_if_unusable: bool = False,
    unusable_code: str = "no_usable_output",
    run_all: bool = False,
) -> StageResult:
    """Run methods in order and keep the first one whose check score reaches the threshold.

    - check(data) gives a score in [0, 1] for one method's output, or (score, details)
      where details are the separate values behind the score (recorded per attempt).
      A unit may bring its own check.
    - With run_all, every method still runs (and is scored) after one is accepted, so
      all scores are recorded; the choice rule does not change. A method with run_if
      runs only when run_if(attempts so far) is true, otherwise it is SKIPPED.
    - Stub methods (not implemented) are recorded but never chosen.
    - If no method reaches the threshold, the best-scoring output is kept and the
      status is WARN. Ties go to the earlier method.
    - If no method gives any output: FAIL when the unit requires output. Otherwise,
      if any method is still a stub, the result is OK with the informational code
      not_implemented. Otherwise FAIL when fail_if_unusable is set (stages 0 and 1)
      and WARN in later stages.
    - With units, the choice is made per unit and data is a dict keyed by unit id.
      With fail_if_unusable, the stage FAILs only when every unit FAILs; a partial
      failure gives WARN.
    """
    if units is None:
        c = _choose(methods, check, accept_threshold, payload, False, fail_if_unusable, unusable_code, run_all)
        return StageResult(
            stage=stage,
            status=c.status,
            data=c.data,
            metrics={"methods_tried": len(c.attempts)},
            reasons=list(c.reasons),
            method_used=c.method_used,
            methods_tried=list(c.attempts),
            extra={"outputs": c.outputs} if run_all else {},
        )

    per_unit: dict[str, UnitChoice] = {}
    data: dict[str, Any] = {}
    reasons: list[Reason] = []
    statuses: list[Status] = []
    unit_outputs: dict[str, dict] = {}
    for unit in units:
        c = _choose(
            unit.methods if unit.methods is not None else methods,
            unit.check if unit.check is not None else check,
            unit.accept_threshold if unit.accept_threshold is not None else accept_threshold,
            unit.payload,
            unit.require_output,
            fail_if_unusable,
            unusable_code,
            run_all,
        )
        unit_outputs[unit.unit_id] = c.outputs
        per_unit[unit.unit_id] = UnitChoice(c.method_used, c.attempts, c.status)
        data[unit.unit_id] = c.data
        statuses.append(c.status)
        for r in c.reasons:
            detail = f"{unit.unit_id}: {r.detail}" if r.detail else unit.unit_id
            reasons.append(Reason(r.code, detail, r.section or unit.section))

    if fail_if_unusable and statuses and all(s is Status.FAIL for s in statuses):
        status = Status.FAIL
    elif fail_if_unusable:
        status = Status.WARN if any(s is not Status.OK for s in statuses) else Status.OK
    else:
        status = worst(statuses)

    metrics = {
        "units": len(statuses),
        "units_ok": sum(s is Status.OK for s in statuses),
        "units_warn": sum(s is Status.WARN for s in statuses),
        "units_fail": sum(s is Status.FAIL for s in statuses),
    }
    return StageResult(
        stage=stage,
        status=status,
        data=data,
        metrics=metrics,
        reasons=list(_dedupe(reasons)),
        method_used="per_unit",
        per_unit=per_unit,
        extra={"outputs": unit_outputs} if run_all else {},
    )


def escalate(result: StageResult, status: Status, reason: Reason) -> StageResult:
    """Raise a result's status (never lower it) and add a reason."""
    return replace(
        result,
        status=worst([result.status, status]),
        reasons=list(result.reasons) + [reason],
    )


# ---------------------------------------------------------------- runner


def default_stages() -> list[tuple[str, StageFn]]:
    """Stage functions in order. Imported here to avoid import cycles."""
    from resume_parser.clean import run_clean
    from resume_parser.entities import run_entities
    from resume_parser.extract.base import run_layout
    from resume_parser.intake import run_intake
    from resume_parser.normalize import run_normalize
    from resume_parser.sections import run_sections
    from resume_parser.validate import run_validate
    from resume_parser.writer import run_output

    return [
        ("stage0", run_intake),
        ("stage1", run_layout),
        ("stage2", run_clean),
        ("stage3", run_sections),
        ("stage4", run_normalize),
        ("stage5", run_entities),
        ("stage6", run_validate),
        ("stage7", run_output),
    ]


def method_chains() -> dict[str, tuple[Method, ...]]:
    """Every module-level method chain, keyed by stage (the name chain as stage5.personal).

    Stage 0 builds its methods from config at run time, and stages 6 and 7 have one
    built method each, so they are not listed here.
    """
    from resume_parser import clean, normalize, sections
    from resume_parser.entities import SPLIT_METHODS
    from resume_parser.entities.education import SPLIT_METHODS as EDUCATION_METHODS
    from resume_parser.entities.experience import SPLIT_METHODS as EXPERIENCE_METHODS
    from resume_parser.entities.personal import NAME_METHODS
    from resume_parser.extract.base import METHODS as LAYOUT_METHODS

    return {
        "stage1": tuple(LAYOUT_METHODS),
        "stage2": tuple(clean.METHODS),
        "stage3": tuple(sections.METHODS),
        "stage4": tuple(normalize.METHODS),
        "stage5": tuple(SPLIT_METHODS),  # publications
        "stage5.education": tuple(EDUCATION_METHODS),
        "stage5.experience": tuple(EXPERIENCE_METHODS),
        "stage5.personal": tuple(NAME_METHODS),
    }


def unimplemented_methods() -> list[str]:
    """Sorted "stageN: method" names of every method still marked as a stub."""
    return sorted(
        f"{stage}: {m.name}" for stage, chain in method_chains().items() for m in chain if m.stub
    )


def optional_methods() -> list[str]:
    """Sorted "stageN: method" names of methods that need an optional dependency (OCR)."""
    return sorted(
        f"{stage}: {m.name}" for stage, chain in method_chains().items() for m in chain if m.optional
    )


def flags_from(result: StageResult) -> list[dict]:
    """Turn the reasons of a WARN or FAIL stage into sidecar flags."""
    if result.status is Status.OK:
        return []
    flags = []
    for r in result.reasons:
        if is_info(r.code):
            continue
        detail = f"{result.stage}: {r.detail}" if r.detail else result.stage
        flags.append({"section": r.section or "document", "code": r.code, "detail": detail})
    return flags


def run_pipeline(
    pdf_path: str | Path,
    stages: Sequence[tuple[str, StageFn]] | None = None,
    thresholds: dict | None = None,
) -> PipelineRecord:
    """Run all stages on one PDF and return the final record (nothing is written)."""
    from resume_parser.writer import build_sidecar

    pdf_path = Path(pdf_path)
    stages = list(stages) if stages is not None else default_stages()
    ctx = Context(
        resume_file=pdf_path.name,
        thresholds=thresholds if thresholds is not None else load_thresholds(),
    )

    stopped_at: int | None = None
    for index, (name, fn) in enumerate(stages):
        result = fn(pdf_path, ctx)
        ctx.results[name] = result
        ctx.flags.extend(flags_from(result))
        if result.status is Status.FAIL and index in GATED_STAGES:
            stopped_at = index
            break

    main = None
    if stopped_at is None and "stage7" in ctx.results:
        stage7_data = ctx.results["stage7"].data
        if isinstance(stage7_data, dict):
            main = stage7_data.get("main")

    record = PipelineRecord(
        resume_id=resume_id_for(pdf_path.name),
        resume_file=pdf_path.name,
        stopped_at=stopped_at,
        results=ctx.results,
        flags=list(ctx.flags),
        main=main,
    )
    record.sidecar = build_sidecar(record)
    return record
