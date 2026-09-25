"""Single source of time for agent, PDP and mock backends.

`HR_FIXED_TODAY=YYYY-MM-DD` pins the calendar date (wall-clock time still ticks,
so time windows such as the 5-minute ticket dedupe keep working) so evaluation
cases with relative dates ("next Monday") are reproducible across runs.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Singapore")


def now() -> datetime:
    real = datetime.now(TZ)
    fixed = os.getenv("HR_FIXED_TODAY")
    if fixed:
        return datetime.combine(date.fromisoformat(fixed), real.time(), TZ)
    return real


def today() -> date:
    return now().date()
