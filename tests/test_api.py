import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app

client = TestClient(app)


def _sample_payload():
    hours = [
        {"hour": h, "demand_kwh": 100.0, "solar_kwh": 20.0 if 8 <= h <= 17 else 0.0, "tariff_bdt_per_kwh": 10.0}
        for h in range(24)
    ]
    return {
        "scenario_id": "TEST-1",
        "operator_notes": ["Solar output will drop to about 20% from 1 PM to 3 PM."],
        "hours": hours,
        "battery": {
            "capacity_kwh": 500,
            "initial_energy_kwh": 100,
            "minimum_energy_kwh": 20,
            "max_charge_kwh_per_hour": 100,
            "max_discharge_kwh_per_hour": 100,
        },
    }


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_optimize_energy_happy_path(monkeypatch):
    async def fake_interpret(notes):
        return [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [13, 14], "factor": 0.2, "minimum_energy_kwh": None, "max_grid_kwh": None},
                "explanation": "panel cleaning",
            }
        ]

    monkeypatch.setattr(main, "interpret_notes", fake_interpret)

    resp = client.post("/optimize-energy", json=_sample_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["scenario_id"] == "TEST-1"
    assert len(body["directive_interpretation"]) == 1
    assert body["directive_interpretation"][0]["directive_type"] == "solar_reduction"
    assert len(body["hourly_plan"]) == 24
    assert body["total_grid_kwh"] >= 0
    assert body["peak_grid_kwh"] == max(h["grid_kwh"] for h in body["hourly_plan"])


def test_optimize_energy_llm_failure_falls_back_to_no_op(monkeypatch):
    async def failing_interpret(notes):
        raise RuntimeError("simulated LLM/provider outage")

    monkeypatch.setattr(main, "interpret_notes", failing_interpret)

    resp = client.post("/optimize-energy", json=_sample_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["directive_interpretation"][0]["directive_type"] == "no_op"
    assert body["directive_interpretation"][0]["applies"] is False


def test_malformed_json_returns_400():
    resp = client.post(
        "/optimize-energy",
        content=b"{not valid json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_missing_required_field_returns_422():
    payload = _sample_payload()
    del payload["battery"]
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 422


def test_wrong_hour_count_returns_422():
    payload = _sample_payload()
    payload["hours"] = payload["hours"][:23]
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 422


def test_too_many_operator_notes_returns_422():
    payload = _sample_payload()
    payload["operator_notes"] = ["a", "b", "c", "d"]
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 422
