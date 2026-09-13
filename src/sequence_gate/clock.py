"""Time, injected.

There is exactly one reason this module exists: nothing in the decision path is
allowed to call datetime.now() on its own. A gate that reads the wall clock cannot
be tested for the 3am case, the DST case, or the token-expiry case without sleeping,
and a test suite that sleeps is a test suite people stop running.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current instant, timezone aware, in UTC."""
        ...


class SystemClock:
    """The real clock. The only place in the package that reads wall time."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """A clock that only moves when a test moves it."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock needs an aware datetime")
        self._at = at.astimezone(UTC)

    def now(self) -> datetime:
        return self._at

    def advance(self, delta: timedelta) -> datetime:
        self._at = self._at + delta
        return self._at

    def set(self, at: datetime) -> datetime:
        if at.tzinfo is None:
            raise ValueError("FrozenClock needs an aware datetime")
        self._at = at.astimezone(UTC)
        return self._at
