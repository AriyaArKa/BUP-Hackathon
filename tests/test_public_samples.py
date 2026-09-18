"""Validates the optimizer against the organizer-provided public sample pack.

This bypasses the real LLM call and feeds the optimizer the *reference*
directive_interpretation for each public case, so it can run without an API
key. It checks that our schedule is valid and at least as cheap as the
organizer's reference optimal cost (allowing a small tolerance for
floating-point/alternate-optimum differences).
"""

import json
from pathlib import Path

import pytest

from app.optimizer import solve_schedule
from app.schemas import BatteryConfig, DirectiveInterpretation, HourEntry

SAMPLES_PATH = Path(__file__).resolve().parent.parent / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"

with open(SAMPLES_PATH, encoding="utf-8") as f:
    SAMPLE_DATA = json.load(f)

CASES = SAMPLE_DATA["cases"]


def _directives_from_reference(entries):
    return [
        DirectiveInterpretation(
            note_index=e["note_index"],
            applies=e["applies"],
            directive_type=e["directive_type"],
            structured_adjustment=e["structured_adjustment"],
            explanation=e["explanation"],
        )
        for e in entries
    ]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_optimizer_matches_or_beats_reference_cost(case):
    scenario = case["input"]
    hours = [HourEntry(**h) for h in scenario["hours"]]
    battery = BatteryConfig(**scenario["battery"])
    directives = _directives_from_reference(case["expected_output"]["directive_interpretation"])

    plan, total_grid_kwh, total_cost_bdt, peak_grid_kwh = solve_schedule(hours, battery, directives)

    expected_cost = case["expected_output"]["total_cost_bdt"]
    # Our LP should reach the true optimum; allow a small tolerance for
    # floating point noise and legitimate alternate-optimal reference data.
    tolerance = max(1.0, abs(expected_cost) * 0.02)
    assert total_cost_bdt <= expected_cost + tolerance, (
        f"{case['id']}: got cost {total_cost_bdt}, reference optimal was {expected_cost}"
    )

    # internal consistency: totals must match what's recalculated from hourly_plan
    assert total_grid_kwh == pytest.approx(sum(p.grid_kwh for p in plan), abs=1e-3)
    assert peak_grid_kwh == pytest.approx(max(p.grid_kwh for p in plan), abs=1e-3)

    # end-of-day neutrality
    assert plan[-1].battery_energy_after_kwh == pytest.approx(
        battery.initial_energy_kwh, abs=1e-2
    )
