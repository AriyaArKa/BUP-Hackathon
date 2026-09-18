import pytest

from app.optimizer import solve_schedule
from app.schemas import BatteryConfig, DirectiveInterpretation, HourEntry

EPS = 1e-3


def _flat_hours(demand=100.0, solar=0.0, tariff=10.0):
    return [
        HourEntry(hour=h, demand_kwh=demand, solar_kwh=solar, tariff_bdt_per_kwh=tariff)
        for h in range(24)
    ]


def _battery(**overrides):
    defaults = dict(
        capacity_kwh=500,
        initial_energy_kwh=100,
        minimum_energy_kwh=20,
        max_charge_kwh_per_hour=100,
        max_discharge_kwh_per_hour=100,
    )
    defaults.update(overrides)
    return BatteryConfig(**defaults)


def _assert_valid_plan(plan, hours, battery):
    prev_energy = battery.initial_energy_kwh
    for p, h in zip(plan, hours):
        # energy balance
        lhs = p.grid_kwh + p.solar_used_kwh + (p.battery_kwh if p.battery_action == "discharge" else 0)
        rhs = h.demand_kwh + (p.battery_kwh if p.battery_action == "charge" else 0)
        assert lhs == pytest.approx(rhs, abs=EPS)

        # battery bounds
        assert p.battery_energy_after_kwh >= battery.minimum_energy_kwh - EPS
        assert p.battery_energy_after_kwh <= battery.capacity_kwh + EPS

        # transition consistency
        if p.battery_action == "charge":
            expected = prev_energy + p.battery_kwh
        elif p.battery_action == "discharge":
            expected = prev_energy - p.battery_kwh
        else:
            expected = prev_energy
            assert p.battery_kwh == pytest.approx(0, abs=EPS)
        assert p.battery_energy_after_kwh == pytest.approx(expected, abs=EPS)
        prev_energy = p.battery_energy_after_kwh

        # rate limits
        if p.battery_action == "charge":
            assert p.battery_kwh <= battery.max_charge_kwh_per_hour + EPS
        if p.battery_action == "discharge":
            assert p.battery_kwh <= battery.max_discharge_kwh_per_hour + EPS

    # end-of-day neutrality
    assert plan[-1].battery_energy_after_kwh == pytest.approx(battery.initial_energy_kwh, abs=EPS)


def test_no_directives_satisfies_all_constraints_and_meets_demand():
    hours = _flat_hours()
    battery = _battery()
    plan, total_grid, total_cost, peak = solve_schedule(hours, battery, [])
    assert len(plan) == 24
    _assert_valid_plan(plan, hours, battery)
    assert total_cost == pytest.approx(sum(p.grid_kwh * 10.0 for p in plan), abs=EPS)
    assert peak == pytest.approx(max(p.grid_kwh for p in plan), abs=EPS)


def test_cheap_hour_is_preferred_over_expensive_hour():
    hours = [
        HourEntry(hour=h, demand_kwh=0, solar_kwh=0, tariff_bdt_per_kwh=(5 if h == 0 else 50))
        for h in range(24)
    ]
    # single hour of demand at an expensive hour; battery should prefer charging cheap and discharging expensive
    hours[12] = HourEntry(hour=12, demand_kwh=100, solar_kwh=0, tariff_bdt_per_kwh=50)
    battery = _battery(initial_energy_kwh=50, minimum_energy_kwh=0, capacity_kwh=500)
    plan, total_grid, total_cost, peak = solve_schedule(hours, battery, [])
    _assert_valid_plan(plan, hours, battery)
    # grid should not be used at hour 12 if battery covers it via cheap-hour charging
    hour12 = next(p for p in plan if p.hour == 12)
    assert hour12.grid_kwh < 100


def test_no_charge_window_directive_forces_zero_charge():
    hours = _flat_hours(demand=50, solar=200, tariff=10)
    battery = _battery(initial_energy_kwh=100, minimum_energy_kwh=0)
    directive = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="no_charge_window",
        structured_adjustment={"hours": [6, 7, 8]},
        explanation="test",
    )
    plan, *_ = solve_schedule(hours, battery, [directive])
    for h in (6, 7, 8):
        p = next(x for x in plan if x.hour == h)
        assert p.battery_action != "charge"


def test_no_discharge_window_directive_forces_zero_discharge():
    hours = _flat_hours(demand=150, solar=0, tariff=10)
    battery = _battery(initial_energy_kwh=300, minimum_energy_kwh=0, capacity_kwh=500)
    directive = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="no_discharge_window",
        structured_adjustment={"hours": [1, 2, 3]},
        explanation="test",
    )
    plan, *_ = solve_schedule(hours, battery, [directive])
    for h in (1, 2, 3):
        p = next(x for x in plan if x.hour == h)
        assert p.battery_action != "discharge"


def test_solar_reduction_lowers_usable_solar():
    hours = _flat_hours(demand=100, solar=100, tariff=10)
    battery = _battery()
    directive = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="solar_reduction",
        structured_adjustment={"hours": [10, 11], "factor": 0.2},
        explanation="test",
    )
    plan, *_ = solve_schedule(hours, battery, [directive])
    for h in (10, 11):
        p = next(x for x in plan if x.hour == h)
        assert p.solar_used_kwh <= 20 + EPS


def test_minimum_battery_reserve_is_respected():
    hours = _flat_hours(demand=50, solar=0, tariff=10)
    battery = _battery(initial_energy_kwh=200, minimum_energy_kwh=20, capacity_kwh=500)
    directive = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="minimum_battery_reserve",
        structured_adjustment={"hours": [18, 19, 20], "minimum_energy_kwh": 150},
        explanation="test",
    )
    plan, *_ = solve_schedule(hours, battery, [directive])
    for h in (18, 19, 20):
        p = next(x for x in plan if x.hour == h)
        assert p.battery_energy_after_kwh >= 150 - EPS


def test_max_grid_window_caps_grid_import():
    hours = _flat_hours(demand=150, solar=0, tariff=10)
    battery = _battery(initial_energy_kwh=100, minimum_energy_kwh=0, capacity_kwh=1000, max_discharge_kwh_per_hour=200)
    directive = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="max_grid_window",
        structured_adjustment={"hours": [9], "max_grid_kwh": 50},
        explanation="test",
    )
    plan, *_ = solve_schedule(hours, battery, [directive])
    p9 = next(x for x in plan if x.hour == 9)
    assert p9.grid_kwh <= 50 + EPS
