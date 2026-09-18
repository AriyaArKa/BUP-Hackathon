# GridWise LLM — Smart Campus Energy Optimizer

Solution for the BUP CSE Fest 2026 Hackathon online preliminary ("GridWise LLM"
/ Smart Campus Energy Optimization Challenge). It exposes an HTTP API that
interprets natural-language operator notes with an LLM, validates that
interpretation deterministically, and hands it to a linear-programming
optimizer that returns a cost-minimizing 24-hour grid/solar/battery schedule.

Canonical behavior is defined by the organizers'
`hackathon_details/BUP_CSE_FEST_2026_Preliminary_Problem_Statement_GridWise_LLM.pdf`.
This README only documents how to run and test this implementation.

## Architecture

```
Energy Data + Operator Notes
        │
        ▼
  LLM Interpreter  (app/llm_interpreter.py) — OpenRouter call, JSON output.
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
  Final Validator (app/optimizer.py:_final_replay_check) — independently
        │            replays the solved plan hour-by-hour against energy
        │            balance, solar/battery bounds, rate limits, and every
        │            applied directive. Never trusts the solver's own
        │            bookkeeping; raises a safe error instead of returning
        │            an unverified plan.
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
- An [OpenRouter](https://openrouter.ai/) API key (the LLM is mandatory for
  operator-note interpretation; OpenRouter gives access to many providers —
  proprietary and open-weight — through one OpenAI-compatible API)

## Environment variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `OPENROUTER_API_KEY` | Yes | — | OpenRouter API key used for operator-note interpretation. |
| `OPENROUTER_MODEL` | No | `meta-llama/llama-3.3-70b-instruct` | OpenRouter model identifier used for interpretation. Swap to any model on [openrouter.ai/models](https://openrouter.ai/models) (e.g. an OpenRouter `:free` variant, or a top-ranked proprietary model) without code changes. |
| `OPENROUTER_BASE_URL` | No | `https://openrouter.ai/api/v1` | OpenRouter's OpenAI-compatible endpoint. |
| `LLM_TIMEOUT_SECONDS` | No | `25` | Per-request timeout for the LLM call (kept under the API's 30s hard request-timeout budget). |
| `PORT` | No | `8000` | Port the service listens on. |

Copy `.env.example` to `.env` and fill in `OPENROUTER_API_KEY` before running
locally. **Never commit `.env` or real key values** — `.gitignore` already
excludes it.

## Local quickstart (clean environment)

```bash
git clone <this-repo-url>
cd BUP-Hackathon
cp .env.example .env        # then edit .env and set OPENROUTER_API_KEY
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
`hackathon_details/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` for full
worked examples.)

### Public-sample test command

Against the organizer-provided public sample pack, with the server running
and a real `OPENROUTER_API_KEY` configured:

```bash
python scripts/run_public_samples.py http://localhost:8000
```

This posts all 10 public cases to the live endpoint using the real configured
LLM, and for each case reports: schema validity, energy-balance/constraint
validity, a diff of the returned `directive_interpretation` against the
organizer's reference ground truth (directive type, `applies`, hours, and
numeric fields — this is the check that actually exercises the mandatory LLM
path end-to-end, not just the deterministic layers), and cost vs. the
organizer reference optimum.

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
docker run --rm -p 8000:8000 -e OPENROUTER_API_KEY=sk-or-v1-... gridwise-llm
curl http://localhost:8000/health
```

The image binds to `0.0.0.0:8000` and contains no baked-in secrets — the key
must be supplied at `docker run` time via `-e OPENROUTER_API_KEY=...`.

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
- **Deterministic hour-window cross-check**: the LLM was observed in testing
  to occasionally miscount explicit whole-hour windows on multi-hour spans
  (e.g. "6 PM until 9 PM" → `[18,19,20]` instead of `[18,19,20,21]`), roughly
  1 in 3-5 calls on the default `OPENROUTER_MODEL`. Because operator notes
  almost always state times explicitly, `app/guardrails.py` re-parses the
  original note text with a deterministic regex parser
  (`app/time_window.py::parse_time_window`) and, when it gets a confident
  parse, that parse overrides the LLM's `hours` field rather than trusting
  the model's arithmetic. This does not replace the LLM (which still decides
  directive type, `applies`/`no_op`, and numeric values) — it only removes
  a demonstrated LLM weak point from a place where a cheap deterministic
  check is exact.
- **Deterministic percentage-reserve normalization**: for
  `minimum_battery_reserve`, the note text is allowed to state the reserve as
  a percentage of capacity (e.g. "keep at least 50% of battery capacity").
  The system/user prompt in `app/llm_interpreter.py` tells the model to
  convert that itself using the `battery.capacity_kwh` it's given, but
  `app/guardrails.py::_percentage_reserve_override` never trusts that the
  model's arithmetic was correct: if the original note text contains an
  explicit `N%`, guardrails deterministically recompute
  `minimum_energy_kwh = (N / 100) * capacity_kwh` and that value overrides
  whatever number the LLM produced. Same philosophy as the hour-window
  cross-check above — the LLM decides *what* directive and *that* a
  percentage was given; deterministic code does the arithmetic.
- **`structured_adjustment` schema**: the system prompt asks the model to
  always return a single JSON object with all possible adjustment fields
  (`hours`, `factor`, `minimum_energy_kwh`, `max_grid_kwh`), using `null` for
  fields that don't apply to the chosen `directive_type`.
  `app/guardrails.py` only reads the field(s) relevant to the validated
  `directive_type` and ignores everything else, so it tolerates models that
  don't follow the shape perfectly.
- **JSON mode, not strict schema enforcement**: OpenRouter fronts many
  different providers/models, and not all of them support strict
  provider-side JSON-schema enforcement. `app/llm_interpreter.py` uses the
  more broadly-supported `{"type": "json_object"}` response format plus an
  explicit example in the prompt, and also tolerates a model wrapping its
  JSON in markdown fences or stray text (`_extract_json_object`). Guardrails
  are the real safety net regardless of what comes back.

## Known limitations

- **LLM latency variance**: the default `OPENROUTER_MODEL`
  (`meta-llama/llama-3.3-70b-instruct`) occasionally takes close to or beyond
  `LLM_TIMEOUT_SECONDS`, which then falls back to `no_op` for every note in
  that request (losing real interpretation credit for that request, though
  the service still returns a valid 200 response under normal GridWise
  rules — it never crashes or 5xxs). Observed rate in testing: roughly 1 in
  15-20 requests. If judged latency/reliability matters more than this
  model's cost, swap `OPENROUTER_MODEL` to a faster proprietary model.
- The optimizer assumes lossless battery charge/discharge (no round-trip
  efficiency factor), matching the energy-balance equation given in the
  Problem Statement.
- `plan_summary` is generated deterministically from the applied directives
  and totals (not by the LLM) to keep it cheap, fast, and to keep the LLM's
  role strictly on operator-note interpretation as required.
- The LLM provider is [OpenRouter](https://openrouter.ai/), accessed via the
  OpenAI Python SDK pointed at OpenRouter's OpenAI-compatible endpoint.
  Swapping the underlying model is a one-line env var change
  (`OPENROUTER_MODEL`); swapping to a different provider entirely means
  reimplementing `app/llm_interpreter.py`'s `interpret_notes` function — the
  guardrail/optimizer layers are provider-agnostic either way.

## Credits / dependencies

- [FastAPI](https://fastapi.tiangolo.com/) / [Uvicorn](https://www.uvicorn.org/) — HTTP service.
- [Pydantic](https://docs.pydantic.dev/) — request/response schema validation.
- [PuLP](https://github.com/coin-or/pulp) (bundled CBC) — linear programming solver.
- [OpenAI Python SDK](https://github.com/openai/openai-python) — used as an OpenAI-compatible client for [OpenRouter](https://openrouter.ai/) LLM calls.
- [pytest](https://docs.pytest.org/) / [httpx](https://www.python-httpx.org/) — testing.
