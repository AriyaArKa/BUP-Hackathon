"""Deterministic start-inclusive/end-exclusive hour-window parser.

The LLM is good at judging directive *type* and relevance, but is observed in
practice to occasionally miscount whole-hour windows (off-by-one errors on
multi-hour spans, e.g. "6 PM until 9 PM" -> [18, 19, 20] vs [18, 19, 20, 21]).
Since operator notes almost always state times explicitly, guardrails.py uses
this as a deterministic cross-check: when it can confidently parse an explicit
time window from the note text, that parse overrides the LLM's hours field
rather than trusting the model's arithmetic.
"""

import re
from typing import List, Optional

_AMPM_RANGE_RE = re.compile(
    r"(?:from|between|during)?\s*(\d{1,2})(?::00)?\s*(am|pm)?\s*"
    r"(?:to|until|and|-|through)\s*"
    r"(\d{1,2})(?::00)?\s*(am|pm)",
    re.IGNORECASE,
)

_24H_RANGE_RE = re.compile(
    r"(\d{1,2}):00\s*(?:to|until|and|-|through)\s*(\d{1,2}):00"
)


def _to_24h(hour: int, ampm: Optional[str]) -> int:
    if ampm is None:
        return hour
    ampm = ampm.lower()
    if ampm == "pm" and hour < 12:
        return hour + 12
    if ampm == "am" and hour == 12:
        return 0
    return hour


def parse_time_window(text: str) -> Optional[List[int]]:
    """Return the start-inclusive/end-exclusive hour list for the first explicit
    time-of-day range found in `text`, or None if no confident parse exists.
    """
    normalized = text.lower().replace("noon", "12 pm").replace("midnight", "12 am")

    match = _AMPM_RANGE_RE.search(normalized)
    if match:
        start_raw, start_ampm, end_raw, end_ampm = match.groups()
        start, end = int(start_raw), int(end_raw)
        if start_ampm is None:
            start_ampm = end_ampm
        start = _to_24h(start, start_ampm)
        end = _to_24h(end, end_ampm)
        if 0 <= start < end <= 24:
            return list(range(start, end))
        return None

    match = _24H_RANGE_RE.search(normalized)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if 0 <= start < end <= 24:
            return list(range(start, end))
        return None

    return None
