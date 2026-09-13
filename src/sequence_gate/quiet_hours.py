"""Quiet hours, evaluated in the contact's own timezone.

Two things make this harder than it looks, and both have bitten real systems:

1. A window that crosses midnight (21:00 to 08:00) is the common case, not the edge
   case, and a naive `start <= t < end` comparison silently inverts it, turning the
   quiet window into the only window when sending is allowed.

2. The instant a quiet window ends is a local wall time, and local wall times go
   missing once a year. On a spring-forward morning 02:30 does not exist; asking for
   it and trusting the answer produces a "retry at" in the past or an hour off.

Nothing here reads a server clock. The instant comes in as an argument.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import QuietHours

_ONE_MINUTE = timedelta(minutes=1)


def _minutes(hour: int, minute: int) -> int:
    return hour * 60 + minute


def local_time_of(now_utc: datetime, timezone: str) -> datetime:
    """The contact's wall clock at this instant."""
    return now_utc.astimezone(ZoneInfo(timezone))


def is_quiet(now_utc: datetime, timezone: str, window: QuietHours) -> bool:
    local = local_time_of(now_utc, timezone)
    t = _minutes(local.hour, local.minute)
    start = _minutes(window.start_hour, window.start_minute)
    end = _minutes(window.end_hour, window.end_minute)

    if start < end:
        return start <= t < end
    # crosses midnight: quiet from start until end the following day
    return t >= start or t < end


def next_open(now_utc: datetime, timezone: str, window: QuietHours) -> datetime:
    """The first instant at or after `now_utc` that is no longer quiet.

    Returns `now_utc` unchanged when the window is not currently quiet, so callers
    can use it unconditionally.
    """
    if not is_quiet(now_utc, timezone, window):
        return now_utc

    zone = ZoneInfo(timezone)
    local = local_time_of(now_utc, timezone)

    candidate_local = local.replace(
        hour=window.end_hour, minute=window.end_minute, second=0, microsecond=0
    )
    if candidate_local <= local:
        candidate_local = candidate_local + timedelta(days=1)

    candidate_utc = candidate_local.astimezone(ZoneInfo("UTC"))

    # A wall time that does not exist (spring forward) round-trips to something other
    # than what we asked for. Walk forward a minute at a time until the local clock
    # actually reads at or past the end of the window, and is no longer quiet.
    guard = 0
    while guard < 180:
        back = candidate_utc.astimezone(zone)
        if not is_quiet(candidate_utc, timezone, window) and back >= candidate_local.replace(
            tzinfo=back.tzinfo
        ):
            return candidate_utc
        if not is_quiet(candidate_utc, timezone, window):
            return candidate_utc
        candidate_utc = candidate_utc + _ONE_MINUTE
        guard += 1

    # Unreachable for any real window, but never return a time in the past.
    return max(candidate_utc, now_utc + _ONE_MINUTE)
