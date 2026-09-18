import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import LLM_TIMEOUT_SECONDS
from app.guardrails import guardrail_validate
from app.llm_interpreter import interpret_notes
from app.optimizer import InfeasibleScheduleError, solve_schedule
from app.schemas import (
    DirectiveInterpretation,
    HealthResponse,
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
)

logger = logging.getLogger("gridwise")

app = FastAPI(title="GridWise LLM-Assisted Energy Optimizer")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Distinguish "the body wasn't valid JSON at all" (400) from
    # "it parsed but failed schema/semantic validation" (422).
    for error in exc.errors():
        if error.get("type") == "json_invalid":
            return JSONResponse(status_code=400, content={"detail": "Malformed JSON request body."})
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error while processing %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


def _build_plan_summary(
    directives: list[DirectiveInterpretation],
    total_grid_kwh: float,
    total_cost_bdt: float,
    peak_grid_kwh: float,
) -> str:
    applied = [d.directive_type for d in directives if d.applies]
    if applied:
        directive_text = "Applied directives: " + ", ".join(applied) + "."
    else:
        directive_text = "No operator directives applied; optimized under normal GridWise rules."
    return (
        f"{directive_text} Total grid usage {total_grid_kwh:.2f} kWh, "
        f"cost {total_cost_bdt:.2f} BDT, peak hourly grid draw {peak_grid_kwh:.2f} kWh."
    )


@app.post("/optimize-energy", response_model=OptimizeEnergyResponse)
async def optimize_energy(payload: OptimizeEnergyRequest) -> OptimizeEnergyResponse:
    try:
        # Never trust a third-party SDK's own timeout handling to actually
        # bound the call (observed in practice: some OpenRouter models can
        # hang well past the client-configured timeout). This wait_for is
        # the hard ceiling that guarantees the API contract's per-request
        # timeout regardless of what the LLM provider does.
        raw_llm_entries = await asyncio.wait_for(
            interpret_notes(payload.operator_notes, payload.battery.capacity_kwh),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("LLM interpretation failed; falling back to no_op for all notes")
        raw_llm_entries = []

    directives = guardrail_validate(
        raw_llm_entries,
        len(payload.operator_notes),
        payload.battery.capacity_kwh,
        payload.operator_notes,
    )

    try:
        hourly_plan, total_grid_kwh, total_cost_bdt, peak_grid_kwh = solve_schedule(
            payload.hours, payload.battery, directives
        )
    except InfeasibleScheduleError:
        logger.exception("No feasible schedule for scenario %s", payload.scenario_id)
        return JSONResponse(
            status_code=500,
            content={"detail": "Unable to compute a valid schedule for this scenario."},
        )

    plan_summary = _build_plan_summary(directives, total_grid_kwh, total_cost_bdt, peak_grid_kwh)

    return OptimizeEnergyResponse(
        scenario_id=payload.scenario_id,
        directive_interpretation=directives,
        hourly_plan=hourly_plan,
        total_grid_kwh=total_grid_kwh,
        total_cost_bdt=total_cost_bdt,
        peak_grid_kwh=peak_grid_kwh,
        plan_summary=plan_summary,
    )
