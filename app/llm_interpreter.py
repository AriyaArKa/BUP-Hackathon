"""LLM-backed interpretation of operator notes, via OpenRouter.

OpenRouter exposes an OpenAI-compatible Chat Completions API in front of many
different models (proprietary and open-weight), so this uses the OpenAI SDK
pointed at OpenRouter's base URL. This is the only place a language model
touches the pipeline. Its output is treated as untrusted structured data --
guardrails.py is solely responsible for deciding what is safe to hand to the
optimizer, so this module deliberately does not need strict provider-side
JSON-schema enforcement to be safe.
"""

import json
import re
from typing import List

from openai import AsyncOpenAI

from app.config import (
    LLM_TIMEOUT_SECONDS,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
)

SYSTEM_PROMPT = """You are the operator-note interpreter for a smart-campus energy \
scheduling system called GridWise. You convert short natural-language notes from \
campus operators into structured directives for a downstream optimizer.

Supported directive types (use exactly one per note):
- solar_reduction: usable solar drops during specific hours.
  structured_adjustment.factor is the USABLE FRACTION REMAINING (an 80% reduction
  means factor = 0.2).
- minimum_battery_reserve: battery energy must stay at or above a level during
  specific hours. Use structured_adjustment.minimum_energy_kwh.
- no_charge_window: battery charging is unavailable during specific hours.
- no_discharge_window: battery discharging is unavailable during specific hours.
- max_grid_window: grid import may not exceed a stated amount during specific
  hours. Use structured_adjustment.max_grid_kwh.
- no_op: the note does not affect the current 24-hour energy schedule (e.g. it
  is unrelated to energy, demand, solar, tariff, or battery operation). Many
  notes are realistic distractors and must be marked no_op -- do not invent an
  energy rule for a note that does not describe one.

Rules:
- Time windows are start-inclusive, end-exclusive. "1 PM to 3 PM" means hours
  [13, 14], not [13, 14, 15].
- hours must be unique integers from 0 to 23.
- Never invent or change base demand, tariff, or battery parameters yourself --
  only extract what the note actually says using the supported directive
  shapes above.
- Return exactly one interpretation entry per operator note, in the same order
  as the notes are given, using note_index equal to the note's position
  (0-based).
- For directive types other than no_op, set applies to true. For no_op, set
  applies to false and structured_adjustment to null.

Respond with ONLY a single JSON object (no markdown fences, no commentary)
matching exactly this shape:

{
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
      "explanation": "short explanation"
    }
  ]
}

For no_charge_window / no_discharge_window, structured_adjustment is
{"hours": [...]} only. For minimum_battery_reserve, it is
{"hours": [...], "minimum_energy_kwh": number}. For max_grid_window, it is
{"hours": [...], "max_grid_kwh": number}. For no_op, structured_adjustment is
null.
"""

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _build_user_prompt(operator_notes: List[str]) -> str:
    numbered = "\n".join(f"{i}: {note}" for i, note in enumerate(operator_notes))
    return f"Operator notes for this scenario (note_index: text):\n{numbered}"


def _extract_json_object(raw_text: str) -> dict:
    """Models occasionally wrap JSON in markdown fences or add stray text.
    Try a direct parse first, then fall back to extracting the outermost
    {...} block, since guardrails.py needs a dict to inspect even from a
    slightly malformed response.
    """
    try:
        return json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        pass
    match = _JSON_OBJECT_RE.search(raw_text or "")
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


async def interpret_notes(operator_notes: List[str]) -> List[dict]:
    """Call the LLM (via OpenRouter) and return its raw (untrusted) directive candidates."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")

    client = AsyncOpenAI(
        api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL, timeout=LLM_TIMEOUT_SECONDS
    )
    try:
        completion = await client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(operator_notes)},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            extra_headers={"X-Title": "GridWise LLM - BUP CSE Fest 2026"},
        )
    finally:
        await client.close()

    content = completion.choices[0].message.content
    parsed = _extract_json_object(content)
    entries = parsed.get("directive_interpretation", [])
    if not isinstance(entries, list):
        return []
    return entries
