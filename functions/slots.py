"""
Delivery-time rules, shared by the app's tick (functions/scheduler.py) and the
lightweight ticker process (ticker.py). No app or database imports here.
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional

import pytz

logger = logging.getLogger(__name__)

# A list is sent at the first tick at or after its delivery time. Ticks can
# arrive late (a missed or slow tick), so a list still goes out if it's up to
# this late; one that's older is skipped.
CATCH_UP_SECONDS = 2 * 3600
# Ticks may fire a little before the quarter-hour.
EARLY_SECONDS = 60


def parse_user_times(time_string: str) -> List[str]:
    if not time_string:
        return []
    return [t.strip() for t in time_string.split(',') if t.strip()]


def convert_to_24hour(time_str: str) -> Optional[str]:
    for fmt in ('%I:%M %p', '%I:%M%p'):
        try:
            return datetime.strptime(time_str, fmt).time().strftime('%H:%M')
        except ValueError:
            continue
    logger.error("Could not parse time string: %s", time_str)
    return None


def due_slot(user_time: str, now: datetime, timezone_str: str) -> Optional[datetime]:
    """The most recent occurrence of `user_time` that's due now, in UTC, or None."""
    parsed = convert_to_24hour(user_time)
    if not parsed:
        return None
    try:
        tz = pytz.timezone(timezone_str)
    except Exception:
        return None
    hour, minute = map(int, parsed.split(':'))
    local_now = now.astimezone(tz)
    for days_back in (0, 1):
        day = (local_now - timedelta(days=days_back)).date()
        slot = tz.localize(datetime(day.year, day.month, day.day, hour, minute))
        lateness = (now - slot).total_seconds()
        if -EARLY_SECONDS <= lateness <= CATCH_UP_SECONDS:
            return slot.astimezone(pytz.UTC)
    return None


def latest_due_slot(time_string: str, timezone_str: str, now: datetime) -> Optional[datetime]:
    """The latest of a user's delivery times that's due now, or None."""
    slots = [s for s in (due_slot(t, now, timezone_str) for t in parse_user_times(time_string)) if s]
    return max(slots) if slots else None
