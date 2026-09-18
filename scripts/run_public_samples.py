"""End-to-end smoke test against a running instance of the service.

Posts every case from the public sample pack to a live /optimize-energy
endpoint (using the real configured LLM) and reports schema/constraint
validity plus cost comparison against the organizer reference. This is the
"public-sample test command" referenced in the README.

Usage:
    python scripts/run_public_samples.py [BASE_URL]

    BASE_URL defaults to http://localhost:8000
"""

import json
import sys
from pathlib import Path

import httpx

SAMPLES_PATH = (
    Path(__file__).resolve().parent.parent
    / "hackathon_details"
    / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)


def energy_balance_ok(hour, plan_entry):
    charge = plan_entry["battery_kwh"] if plan_entry["battery_action"] == "charge" else 0.0
    discharge = plan_entry["battery_kwh"] if plan_entry["battery_action"] == "discharge" else 0.0
    lhs = plan_entry["grid_kwh"] + plan_entry["solar_used_kwh"] + discharge
    rhs = hour["demand_kwh"] + charge
    return abs(lhs - rhs) <= 0.05


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

    with open(SAMPLES_PATH, encoding="utf-8") as f:
        data = json.load(f)

    health = httpx.get(f"{base_url}/health", timeout=10)
    print(f"GET /health -> {health.status_code} {health.text}")
    if health.status_code != 200:
        print("Health check failed; aborting.")
        return 1

    passed = 0
    failed = 0

    for case in data["cases"]:
        scenario = case["input"]
        resp = httpx.post(f"{base_url}/optimize-energy", json=scenario, timeout=35)
        if resp.status_code != 200:
            print(f"[FAIL] {case['id']}: HTTP {resp.status_code} {resp.text[:200]}")
            failed += 1
            continue

        body = resp.json()
        hours_by_index = {h["hour"]: h for h in scenario["hours"]}
        ok = True

        if len(body.get("directive_interpretation", [])) != len(scenario["operator_notes"]):
            ok = False

        plan = body.get("hourly_plan", [])
        if len(plan) != 24:
            ok = False
        else:
            for entry in plan:
                if not energy_balance_ok(hours_by_index[entry["hour"]], entry):
                    ok = False
                    break
            if abs(plan[-1]["battery_energy_after_kwh"] - scenario["battery"]["initial_energy_kwh"]) > 0.5:
                ok = False

        expected_cost = case["expected_output"]["total_cost_bdt"]
        got_cost = body.get("total_cost_bdt", float("inf"))
        cost_note = f"cost={got_cost:.2f} (reference optimal={expected_cost:.2f})"

        if ok:
            print(f"[PASS] {case['id']}: {cost_note}")
            passed += 1
        else:
            print(f"[FAIL] {case['id']}: schema/constraint check failed, {cost_note}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed out of {len(data['cases'])} public cases.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
