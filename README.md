# GridWise LLM — Smart Campus Energy Optimizer

Solution for the BUP CSE Fest 2026 Hackathon online preliminary ("GridWise LLM"
/ Smart Campus Energy Optimization Challenge). It exposes an HTTP API that
interprets natural-language operator notes with an LLM, validates that
interpretation deterministically, and hands it to a linear-programming
optimizer that returns a cost-minimizing 24-hour grid/solar/battery schedule.

Canonical behavior is defined by the organizers'
`BUP_CSE_FEST_2026_Preliminary_Problem_Statement_GridWise_LLM.pdf`. This
README only documents how to run and test this implementation.

## Architecture

```
Energy Data + Operator Notes
        │
        ▼
  LLM Interpreter  (app/llm_interpreter.py) — OpenAI call, structured JSON output.
        │            Untrusted: may hallucinate, omit notes, or use wrong shapes.
        ▼
  Guardrail Validator (app/guardrails.py) — pure deterministic code.
        │            Rejects anything that doesn't match the exact directive
        │            shapes/ranges in the Problem Statement and falls back to
        │            a safe no_op per note instead of crashing or inventing
        │            a rule. Normalizes hours to sorted/unique/in-range.
        ▼
  Math Optimizer (app/optimizer.py) — PuLP linear program (CBC solver).
        │            Compiles validated directives into per-hour bounds
        │            (effective solar, min reserve, no-charge/no-discharge
        │            hours, grid caps) and minimizes total grid cost subject
        │            to energy balance, battery bounds/rate limits, and
        │            end-of-day battery neutrality.
        ▼
  Final response (app/main.py) — totals recalculated from hourly_plan itself,
                                  never trusted from solver internals directly.
```

- **LLM role**: interpreting `operator_notes` into candidate structured
  directives. It is *not* used for the optimization math itself and is not
  used only for cosmetic text — see `app/llm_interpreter.py`.
- **Guardrails**: `app/guardrails.py`. Validates directive type, note-index
  mapping (no missing/duplicate notes), hour uniqueness/range/ordering,
  numeric ranges (`solar_reduction.factor` in `[0,1]`, reserve `<= battery
  capacity`, grid cap `>= 0`), and `applies`/`structured_adjustment`
  semantics. Anything invalid becomes a safe `no_op` for that note only —
  the rest of the response is unaffected.
- **Optimizer/solver**: [PuLP](https://github.com/coin-or/pulp) with the
  bundled CBC solver (no external solver installation required). See
  `app/optimizer.py`.

## Requirements

- Python 3.11+
- An OpenAI API key (the LLM is mandatory for operator-note interpretation)

## Environment variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `OPENAI_API_KEY` | Yes | — | OpenAI API key used for operator-note interpretation. |
| `OPENAI_MODEL` | No | `gpt-4o-mini` | Chat completions model used for interpretation. |
| `LLM_TIMEOUT_SECONDS` | No | `20` | Per-request timeout for the LLM call. |
| `PORT` | No | `8000` | Port the service listens on. |

Copy `.env.example` to `.env` and fill in `OPENAI_API_KEY` before running
locally. **Never commit `.env` or real key values** — `.gitignore` already
excludes it.

## Local quickstart (clean environment)

```bash
git clone <this-repo-url>
cd BUP
cp .env.example .env        # then edit .env and set OPENAI_API_KEY
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

In another terminal:

```bash
curl http://localhost:8000/health
# {"status":"ok"}

curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d @- <<'JSON'
{
  "scenario_id": "GRID-101",
  "operator_notes": [
    "Solar output will drop to about 20% from 1 PM to 3 PM.",
    "Do not charge the battery between 2 PM and 4 PM.",
    "The cafeteria menu changes tomorrow."
  ],
  "hours": [
    {"hour": 0, "demand_kwh": 180, "solar_kwh": 0, "tariff_bdt_per_kwh": 7}
  ],
  "battery": {
    "capacity_kwh": 500,
    "initial_energy_kwh": 200,
    "minimum_energy_kwh": 50,
    "max_charge_kwh_per_hour": 100,
    "max_discharge_kwh_per_hour": 100
  }
}
JSON
```

(The `hours` array must contain all 24 hourly entries — see
`BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` for full worked examples.)

### Public-sample test command

Against the organizer-provided public sample pack, with the server running
and a real `OPENAI_API_KEY` configured:

```bash
python scripts/run_public_samples.py http://localhost:8000
```

This posts all 10 public cases to the live endpoint and reports schema
validity, constraint validity, and cost vs. the organizer reference for each.

## Running the test suite

```bash
python -m pytest
```

This runs three layers, none of which require an API key:

- `tests/test_guardrails.py` — guardrail rejection/normalization behavior on
  hand-crafted malformed/adversarial LLM output.
- `tests/test_optimizer.py` — optimizer correctness against hand-built
  scenarios for each directive type (energy balance, battery bounds/rate
  limits, end-of-day neutrality, directive enforcement).
- `tests/test_public_samples.py` — runs the optimizer against all 10 public
  sample cases using the organizer's *reference* directive interpretation
  (bypassing the LLM), checking schedule validity and that our LP reaches an
  equal-or-better cost than the published reference optimum.
- `tests/test_api.py` — full API contract tests (`/health`, request
  validation → 400/422, response schema) with the LLM call mocked, including
  a test that a failed/unavailable LLM call degrades to a safe `no_op`
  response instead of crashing the service.

## Docker

```bash
docker build -t gridwise-llm .
docker run --rm -p 8000:8000 -e OPENAI_API_KEY=sk-... gridwise-llm
curl http://localhost:8000/health
```

The image binds to `0.0.0.0:8000` and contains no baked-in secrets — the key
must be supplied at `docker run` time via `-e OPENAI_API_KEY=...`.

## Design choices worth knowing about

- **Overlapping `solar_reduction` directives**: if two directives reduce
  solar in the same hour, their factors are composed multiplicatively
  (`effective_solar = base_solar * factor1 * factor2 * ...`) rather than one
  overwriting the other. Organizer scenarios are stated to be feasible and
  non-contradictory, so this mainly matters for robustness on edge cases.
- **Overlapping `max_grid_window` directives**: the tightest (`min`) cap
  applies for any hour covered by more than one such directive.
- **LLM failure handling**: if the LLM call errors, times out, or is
  misconfigured (missing key), every operator note for that request falls
  back to `no_op` and the optimizer still returns a valid schedule under the
  normal GridWise rules — the service never crashes or 5xxs on a provider
  outage, per the "safe failure" requirement in the Problem Statement.
- **`structured_adjustment` schema**: the LLM is asked (via OpenAI structured
  outputs / JSON schema) to always return an object with all possible
  adjustment fields (`hours`, `factor`, `minimum_energy_kwh`,
  `max_grid_kwh`), using `null` for fields that don't apply to the chosen
  `directive_type`. `app/guardrails.py` only reads the field(s) relevant to
  the validated `directive_type`.

## Known limitations

- The optimizer assumes lossless battery charge/discharge (no round-trip
  efficiency factor), matching the energy-balance equation given in the
  Problem Statement.
- `plan_summary` is generated deterministically from the applied directives
  and totals (not by the LLM) to keep it cheap, fast, and to keep the LLM's
  role strictly on operator-note interpretation as required.
- Only OpenAI is wired up as the LLM provider. Swapping providers means
  reimplementing `app/llm_interpreter.py`'s `interpret_notes` function; the
  guardrail/optimizer layers are provider-agnostic.

## Credits / dependencies

- [FastAPI](https://fastapi.tiangolo.com/) / [Uvicorn](https://www.uvicorn.org/) — HTTP service.
- [Pydantic](https://docs.pydantic.dev/) — request/response schema validation.
- [PuLP](https://github.com/coin-or/pulp) (bundled CBC) — linear programming solver.
- [OpenAI Python SDK](https://github.com/openai/openai-python) — LLM calls.
- [pytest](https://docs.pytest.org/) / [httpx](https://www.python-httpx.org/) — testing.
