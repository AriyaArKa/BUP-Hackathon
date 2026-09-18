"""Linear-programming optimizer for the 24-hour GridWise schedule.

Validated directives (already passed through guardrails.py) are compiled into
deterministic per-hour bounds, then a linear program minimizes total grid
electricity cost subject to the normal GridWise energy/battery rules.
"""

from typing import List

import pulp

from app.schemas import BatteryConfig, DirectiveInterpretation, HourEntry, HourlyPlanEntry

EPS = 1e-6


class InfeasibleScheduleError(Exception):
    pass


def _compile_directives(
    directives: List[DirectiveInterpretation], base_min_energy_kwh: float
):
    effective_solar_factor = {h: 1.0 for h in range(24)}
    min_energy = {h: base_min_energy_kwh for h in range(24)}
    no_charge_hours: set[int] = set()
    no_discharge_hours: set[int] = set()
    max_grid: dict[int, float] = {}

    for d in directives:
        if not d.applies or d.structured_adjustment is None:
            continue
        adj = d.structured_adjustment
        hours = adj["hours"]

        if d.directive_type == "solar_reduction":
            factor = adj["factor"]
            for h in hours:
                # Multiple overlapping reductions compose multiplicatively:
                # each directive independently scales the remaining usable solar.
                effective_solar_factor[h] *= factor

        elif d.directive_type == "minimum_battery_reserve":
            reserve = adj["minimum_energy_kwh"]
            for h in hours:
                min_energy[h] = max(min_energy[h], reserve)

        elif d.directive_type == "no_charge_window":
            no_charge_hours.update(hours)

        elif d.directive_type == "no_discharge_window":
            no_discharge_hours.update(hours)

        elif d.directive_type == "max_grid_window":
            cap = adj["max_grid_kwh"]
            for h in hours:
                max_grid[h] = min(max_grid.get(h, cap), cap)

    return effective_solar_factor, min_energy, no_charge_hours, no_discharge_hours, max_grid


def solve_schedule(
    hours: List[HourEntry],
    battery: BatteryConfig,
    directives: List[DirectiveInterpretation],
) -> tuple[List[HourlyPlanEntry], float, float, float]:
    hours_sorted = sorted(hours, key=lambda h: h.hour)
    demand = {h.hour: h.demand_kwh for h in hours_sorted}
    solar = {h.hour: h.solar_kwh for h in hours_sorted}
    tariff = {h.hour: h.tariff_bdt_per_kwh for h in hours_sorted}

    solar_factor, min_energy, no_charge_hours, no_discharge_hours, max_grid = _compile_directives(
        directives, battery.minimum_energy_kwh
    )
    effective_solar = {h: solar[h] * solar_factor[h] for h in range(24)}

    prob = pulp.LpProblem("gridwise_schedule", pulp.LpMinimize)

    grid = {h: pulp.LpVariable(f"grid_{h}", lowBound=0, upBound=max_grid.get(h)) for h in range(24)}
    solar_used = {
        h: pulp.LpVariable(f"solar_used_{h}", lowBound=0, upBound=effective_solar[h]) for h in range(24)
    }
    charge = {
        h: pulp.LpVariable(
            f"charge_{h}",
            lowBound=0,
            upBound=0.0 if h in no_charge_hours else battery.max_charge_kwh_per_hour,
        )
        for h in range(24)
    }
    discharge = {
        h: pulp.LpVariable(
            f"discharge_{h}",
            lowBound=0,
            upBound=0.0 if h in no_discharge_hours else battery.max_discharge_kwh_per_hour,
        )
        for h in range(24)
    }
    energy_after = {
        h: pulp.LpVariable(f"energy_after_{h}", lowBound=min_energy[h], upBound=battery.capacity_kwh)
        for h in range(24)
    }

    prob += pulp.lpSum(grid[h] * tariff[h] for h in range(24))

    prev_energy = battery.initial_energy_kwh
    for h in range(24):
        prob += (
            grid[h] + solar_used[h] + discharge[h] == demand[h] + charge[h],
            f"energy_balance_{h}",
        )
        prob += (
            energy_after[h] == prev_energy + charge[h] - discharge[h],
            f"battery_transition_{h}",
        )
        prev_energy = energy_after[h]

    prob += (energy_after[23] == battery.initial_energy_kwh, "end_of_day_neutrality")

    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status] != "Optimal":
        raise InfeasibleScheduleError(
            f"No feasible schedule satisfies the given directives (solver status: {pulp.LpStatus[status]})."
        )

    plan: List[HourlyPlanEntry] = []
    for h in range(24):
        c = max(0.0, pulp.value(charge[h]))
        d = max(0.0, pulp.value(discharge[h]))
        net = c - d
        if net > EPS:
            action, magnitude = "charge", net
        elif net < -EPS:
            action, magnitude = "discharge", -net
        else:
            action, magnitude = "idle", 0.0

        g = max(0.0, pulp.value(grid[h]))
        s = max(0.0, pulp.value(solar_used[h]))
        e_after = pulp.value(energy_after[h])

        plan.append(
            HourlyPlanEntry(
                hour=h,
                grid_kwh=round(g, 6),
                solar_used_kwh=round(s, 6),
                battery_action=action,
                battery_kwh=round(magnitude, 6),
                battery_energy_after_kwh=round(e_after, 6),
            )
        )

    total_grid_kwh = round(sum(p.grid_kwh for p in plan), 6)
    total_cost_bdt = round(sum(p.grid_kwh * tariff[p.hour] for p in plan), 6)
    peak_grid_kwh = round(max(p.grid_kwh for p in plan), 6)

    return plan, total_grid_kwh, total_cost_bdt, peak_grid_kwh
