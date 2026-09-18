"""Deterministic validation of untrusted LLM output.

The LLM's raw output is never trusted directly. Every candidate interpretation
is checked against the exact directive shapes in the Problem Statement before
it is allowed to influence the optimizer. Anything that fails validation is
replaced with a safe no_op fallback instead of crashing or inventing a rule.
"""

import math
import re
from typing import Any, List, Optional

from app.schemas import DirectiveInterpretation
from app.time_window import parse_time_window

ALLOWED_DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}

FALLBACK_EXPLANATION = "Guardrail fallback: model output was missing or failed validation."

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _percentage_reserve_override(original_note: str, battery_capacity_kwh: float) -> Optional[float]:
    """FR-GUARD-5 requires guardrails -- not the LLM's own arithmetic -- to be
    the thing that normalizes a percentage reserve to kWh. If the note text
    states a plain percentage, deterministically recompute the kWh amount
    from `battery_capacity_kwh` and let it override whatever number the LLM
    produced, rather than trusting the model to have done that math right.
    """
    match = _PERCENT_RE.search(original_note or "")
    if not match:
        return None
    pct = float(match.group(1))
    if not (0.0 <= pct <= 100.0):
        return None
    return (pct / 100.0) * battery_capacity_kwh


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_hours(raw_hours: Any) -> Optional[List[int]]:
    if not isinstance(raw_hours, list) or not raw_hours:
        return None
    try:
        ints = [int(h) for h in raw_hours if not isinstance(h, bool)]
    except (TypeError, ValueError):
        return None
    if len(ints) != len(raw_hours):
        return None
    if any(h < 0 or h > 23 for h in ints):
        return None
    if len(set(ints)) != len(ints):
        return None
    return sorted(ints)


def _fallback(note_index: int, reason: str = FALLBACK_EXPLANATION) -> DirectiveInterpretation:
    return DirectiveInterpretation(
        note_index=note_index,
        applies=False,
        directive_type="no_op",
        structured_adjustment=None,
        explanation=reason,
    )


def _validate_single(
    note_index: int,
    entry: Optional[dict],
    battery_capacity_kwh: float,
    original_note: str = "",
) -> DirectiveInterpretation:
    if not isinstance(entry, dict):
        return _fallback(note_index)

    directive_type = entry.get("directive_type")
    if directive_type not in ALLOWED_DIRECTIVE_TYPES:
        return _fallback(note_index)

    explanation = entry.get("explanation")
    explanation = str(explanation).strip()[:500] if explanation else ""

    if directive_type == "no_op":
        return DirectiveInterpretation(
            note_index=note_index,
            applies=False,
            directive_type="no_op",
            structured_adjustment=None,
            explanation=explanation or "This note does not affect the current 24-hour schedule.",
        )

    adjustment = entry.get("structured_adjustment")
    if not isinstance(adjustment, dict):
        return _fallback(note_index)

    hours = _validate_hours(adjustment.get("hours"))
    if hours is None:
        return _fallback(note_index)

    # Deterministic cross-check: the LLM is observed to occasionally miscount
    # whole-hour windows on explicit "X to/until Y" phrasing. When the note
    # text yields a confident deterministic parse, prefer it over the model's
    # arithmetic rather than trusting an LLM-computed hour range.
    deterministic_hours = parse_time_window(original_note)
    if deterministic_hours is not None and deterministic_hours != hours:
        hours = deterministic_hours

    if directive_type == "solar_reduction":
        factor = adjustment.get("factor")
        if not _is_number(factor) or not (0.0 <= float(factor) <= 1.0):
            return _fallback(note_index)
        clean_adjustment = {"hours": hours, "factor": float(factor)}

    elif directive_type == "minimum_battery_reserve":
        min_kwh = adjustment.get("minimum_energy_kwh")
        if not _is_number(min_kwh) or not (0.0 <= float(min_kwh) <= battery_capacity_kwh):
            return _fallback(note_index)
        min_kwh = float(min_kwh)
        pct_override = _percentage_reserve_override(original_note, battery_capacity_kwh)
        if pct_override is not None:
            min_kwh = pct_override
        clean_adjustment = {"hours": hours, "minimum_energy_kwh": min_kwh}

    elif directive_type == "no_charge_window":
        clean_adjustment = {"hours": hours}

    elif directive_type == "no_discharge_window":
        clean_adjustment = {"hours": hours}

    elif directive_type == "max_grid_window":
        max_grid = adjustment.get("max_grid_kwh")
        if not _is_number(max_grid) or float(max_grid) < 0:
            return _fallback(note_index)
        clean_adjustment = {"hours": hours, "max_grid_kwh": float(max_grid)}

    else:  # pragma: no cover - guarded by ALLOWED_DIRECTIVE_TYPES membership
        return _fallback(note_index)

    return DirectiveInterpretation(
        note_index=note_index,
        applies=True,
        directive_type=directive_type,
        structured_adjustment=clean_adjustment,
        explanation=explanation or f"Applying {directive_type} as instructed by the operator note.",
    )


def guardrail_validate(
    raw_entries: List[dict],
    num_notes: int,
    battery_capacity_kwh: float,
    original_notes: Optional[List[str]] = None,
) -> List[DirectiveInterpretation]:
    """Reduce untrusted LLM output to exactly one safe entry per note, in order."""
    by_index: dict[int, dict] = {}
    for entry in raw_entries or []:
        if not isinstance(entry, dict):
            continue
        raw_index = entry.get("note_index")
        if not isinstance(raw_index, int) or isinstance(raw_index, bool):
            continue
        if raw_index < 0 or raw_index >= num_notes:
            continue
        if raw_index in by_index:
            continue
        by_index[raw_index] = entry

    notes = original_notes or []
    return [
        _validate_single(
            i,
            by_index.get(i),
            battery_capacity_kwh,
            notes[i] if i < len(notes) else "",
        )
        for i in range(num_notes)
    ]
