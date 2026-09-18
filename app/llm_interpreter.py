"""LLM-backed interpretation of operator notes.

This is the only place a language model touches the pipeline. Its output is
treated as untrusted structured data -- guardrails.py is solely responsible
for deciding what is safe to hand to the optimizer.
"""

import json
from typing import List

from openai import AsyncOpenAI

from app.config import LLM_TIMEOUT_SECONDS, OPENAI_API_KEY, OPENAI_MODEL

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
  applies to false.
- Always populate structured_adjustment as an object. Only the fields relevant
  to the chosen directive_type matter; set irrelevant numeric fields to null
  and hours to an empty list for no_op.
"""

RESPONSE_SCHEMA = {
    "name": "directive_interpretation_batch",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "directive_interpretation": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "note_index": {"type": "integer"},
                        "applies": {"type": "boolean"},
                        "directive_type": {
                            "type": "string",
                            "enum": [
                                "solar_reduction",
                                "minimum_battery_reserve",
                                "no_charge_window",
                                "no_discharge_window",
                                "max_grid_window",
                                "no_op",
                            ],
                        },
                        "structured_adjustment": {
                            "type": "object",
                            "properties": {
                                "hours": {"type": "array", "items": {"type": "integer"}},
                                "factor": {"type": ["number", "null"]},
                                "minimum_energy_kwh": {"type": ["number", "null"]},
                                "max_grid_kwh": {"type": ["number", "null"]},
                            },
                            "required": ["hours", "factor", "minimum_energy_kwh", "max_grid_kwh"],
                            "additionalProperties": False,
                        },
                        "explanation": {"type": "string"},
                    },
                    "required": [
                        "note_index",
                        "applies",
                        "directive_type",
                        "structured_adjustment",
                        "explanation",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["directive_interpretation"],
        "additionalProperties": False,
    },
}


def _build_user_prompt(operator_notes: List[str]) -> str:
    numbered = "\n".join(f"{i}: {note}" for i, note in enumerate(operator_notes))
    return f"Operator notes for this scenario (note_index: text):\n{numbered}"


async def interpret_notes(operator_notes: List[str]) -> List[dict]:
    """Call the LLM and return its raw (untrusted) directive candidates."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=LLM_TIMEOUT_SECONDS)
    try:
        completion = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(operator_notes)},
            ],
            response_format={"type": "json_schema", "json_schema": RESPONSE_SCHEMA},
            temperature=0,
        )
    finally:
        await client.close()

    content = completion.choices[0].message.content
    parsed = json.loads(content)
    entries = parsed.get("directive_interpretation", [])
    if not isinstance(entries, list):
        return []
    return entries
