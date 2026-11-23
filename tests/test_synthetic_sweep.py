"""A small, fast subset of the synthetic sweep (eval/synthetic_sweep.py): ten invented CVs that
pass every stage today, chosen across layouts (one column, tables, two columns, a left sidebar),
page counts (one to three pages, sections crossing a page break, repeated headers and page
numbers) and publication markers. A regression guard only; the full sweep runs outside pytest."""

from __future__ import annotations

import pytest

import synthetic_sweep as sweep
import synthetic_sweep_gen as gen

PASSING_SEEDS = [14, 28, 32, 46, 5, 6, 48, 3, 26, 77]


def test_generator_is_deterministic():
    assert gen.combo(14) == gen.combo(14)
    assert gen.combo(14) != gen.combo(15)


@pytest.mark.parametrize("seed", PASSING_SEEDS)
def test_synthetic_cv_passes_every_stage(seed, tmp_path):
    result = sweep.score(gen.combo(seed), tmp_path)
    failing = {kind: (s["first_fail"], s["cause"]) for kind, s in result["sections"].items() if s["first_fail"]}
    assert not failing and result["name"], (seed, failing, result["name_cause"])
