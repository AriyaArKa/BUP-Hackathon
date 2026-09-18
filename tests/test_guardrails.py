from app.guardrails import guardrail_validate


def test_missing_entries_fall_back_to_no_op():
    result = guardrail_validate([], num_notes=3, battery_capacity_kwh=500)
    assert len(result) == 3
    for i, entry in enumerate(result):
        assert entry.note_index == i
        assert entry.applies is False
        assert entry.directive_type == "no_op"
        assert entry.structured_adjustment is None


def test_valid_solar_reduction_passes_through():
    raw = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [14, 13], "factor": 0.2},
            "explanation": "panel cleaning",
        }
    ]
    result = guardrail_validate(raw, num_notes=1, battery_capacity_kwh=500)
    assert result[0].applies is True
    assert result[0].directive_type == "solar_reduction"
    # hours must be normalized to ascending order
    assert result[0].structured_adjustment == {"hours": [13, 14], "factor": 0.2}


def test_out_of_range_factor_falls_back():
    raw = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [13, 14], "factor": 1.5},
            "explanation": "bad factor",
        }
    ]
    result = guardrail_validate(raw, num_notes=1, battery_capacity_kwh=500)
    assert result[0].directive_type == "no_op"
    assert result[0].applies is False


def test_duplicate_hours_fall_back():
    raw = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [14, 14]},
            "explanation": "dup hours",
        }
    ]
    result = guardrail_validate(raw, num_notes=1, battery_capacity_kwh=500)
    assert result[0].directive_type == "no_op"


def test_out_of_bounds_hour_falls_back():
    raw = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "no_discharge_window",
            "structured_adjustment": {"hours": [23, 24]},
            "explanation": "bad hour",
        }
    ]
    result = guardrail_validate(raw, num_notes=1, battery_capacity_kwh=500)
    assert result[0].directive_type == "no_op"


def test_unsupported_directive_type_falls_back():
    raw = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "shutdown_campus",
            "structured_adjustment": {"hours": [1]},
            "explanation": "made up",
        }
    ]
    result = guardrail_validate(raw, num_notes=1, battery_capacity_kwh=500)
    assert result[0].directive_type == "no_op"


def test_minimum_battery_reserve_above_capacity_falls_back():
    raw = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": [1, 2], "minimum_energy_kwh": 9999},
            "explanation": "too high",
        }
    ]
    result = guardrail_validate(raw, num_notes=1, battery_capacity_kwh=500)
    assert result[0].directive_type == "no_op"


def test_no_op_forces_applies_false_and_null_adjustment_even_if_llm_disagrees():
    raw = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "no_op",
            "structured_adjustment": {"hours": [1]},
            "explanation": "inconsistent",
        }
    ]
    result = guardrail_validate(raw, num_notes=1, battery_capacity_kwh=500)
    assert result[0].applies is False
    assert result[0].structured_adjustment is None


def test_duplicate_note_index_keeps_first_only():
    raw = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [1]},
            "explanation": "first",
        },
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "no_discharge_window",
            "structured_adjustment": {"hours": [2]},
            "explanation": "second (should be ignored)",
        },
    ]
    result = guardrail_validate(raw, num_notes=1, battery_capacity_kwh=500)
    assert len(result) == 1
    assert result[0].directive_type == "no_charge_window"


def test_results_always_ordered_by_note_index():
    raw = [
        {
            "note_index": 2,
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [1]},
            "explanation": "note 2",
        },
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "no_discharge_window",
            "structured_adjustment": {"hours": [2]},
            "explanation": "note 0",
        },
    ]
    result = guardrail_validate(raw, num_notes=3, battery_capacity_kwh=500)
    assert [e.note_index for e in result] == [0, 1, 2]
    assert result[1].directive_type == "no_op"  # note 1 missing from LLM output
