"""Rule four: the window is read on the contact's clock, including the days that clock lies.

Twice a year a local wall time either happens twice or does not happen at all. A gate
that computes "when does the quiet window end" by pasting an hour onto today's date and
trusting the result will, on exactly those two days, hand back a retry time in the past
or an hour off. Both failures look like a scheduler bug and get chased in the wrong place.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sequence_gate import QuietHours, Reason, VerdictKind
from sequence_gate.quiet_hours import is_quiet, local_time_of, next_open

NIGHT = QuietHours(start_hour=21, end_hour=8)
DAYTIME_BLOCK = QuietHours(start_hour=9, end_hour=17)


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


# ----------------------------------------------------------------- the basics


@pytest.mark.parametrize(
    "hour_utc,quiet",
    [(20, False), (21, True), (23, True), (0, True), (7, True), (8, False), (12, False)],
)
def test_a_window_that_crosses_midnight_is_quiet_on_both_sides_of_it(hour_utc, quiet):
    instant = at(2026, 9, 14, hour_utc)
    assert is_quiet(instant, "UTC", NIGHT) is quiet


@pytest.mark.parametrize(
    "hour_utc,quiet",
    [(8, False), (9, True), (13, True), (16, True), (17, False), (20, False)],
)
def test_a_window_inside_one_day_behaves_the_ordinary_way(hour_utc, quiet):
    assert is_quiet(at(2026, 9, 14, hour_utc), "UTC", DAYTIME_BLOCK) is quiet


def test_the_same_instant_is_quiet_in_one_zone_and_not_another():
    instant = at(2026, 9, 14, 18, 42)  # 00:12 in Kolkata, 14:42 in New York
    assert is_quiet(instant, "Asia/Kolkata", NIGHT) is True
    assert is_quiet(instant, "America/New_York", NIGHT) is False


def test_next_open_returns_the_instant_unchanged_when_nothing_is_quiet():
    instant = at(2026, 9, 14, 12)
    assert next_open(instant, "UTC", NIGHT) == instant


def test_next_open_lands_exactly_on_the_end_of_the_window():
    opens = next_open(at(2026, 9, 14, 23), "UTC", NIGHT)
    assert opens == at(2026, 9, 15, 8)


def test_next_open_crosses_into_the_following_day_before_midnight():
    opens = next_open(at(2026, 9, 14, 21, 30), "UTC", NIGHT)
    assert opens.date() == datetime(2026, 9, 15).date()


def test_next_open_is_never_in_the_past():
    for hour in range(24):
        instant = at(2026, 9, 14, hour)
        assert next_open(instant, "Asia/Kolkata", NIGHT) >= instant


# ------------------------------------------------------------------ daylight saving


def test_spring_forward_the_window_end_does_not_exist_locally():
    """2026-03-08 in New York: 02:00 becomes 03:00, so 02:30 never happens."""
    tz = ZoneInfo("America/New_York")
    nominal = datetime(2026, 3, 8, 2, 30, tzinfo=tz)
    # The proof that this wall time is a fiction: it does not survive a round trip.
    assert nominal.astimezone(UTC).astimezone(tz).hour != 2


def test_next_open_across_spring_forward_is_a_real_future_instant():
    window = QuietHours(start_hour=22, end_hour=2, end_minute=30)
    during = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)  # 01:00 EST, inside the window
    opens = next_open(during, "America/New_York", window)

    assert opens > during
    assert is_quiet(opens, "America/New_York", window) is False


def test_next_open_across_fall_back_stays_quiet_through_the_repeated_hour():
    """2026-11-01 in New York: 01:00 to 02:00 happens twice. The window covers both."""
    window = QuietHours(start_hour=22, end_hour=8)
    first_pass = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 01:30 EDT
    second_pass = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # 01:30 EST, the repeat

    assert is_quiet(first_pass, "America/New_York", window) is True
    assert is_quiet(second_pass, "America/New_York", window) is True
    assert next_open(first_pass, "America/New_York", window) > second_pass


def test_local_time_of_reports_the_contacts_wall_clock():
    reported = local_time_of(at(2026, 9, 14, 18, 42), "Asia/Kolkata")
    assert (reported.hour, reported.minute) == (0, 12)


# -------------------------------------------------------------- through the gate


def test_the_gate_holds_during_quiet_hours_and_says_when_it_opens(
    clock, case, two_step_sequence, quiet_contact
):
    from .conftest import Harness

    case_in = case.model_copy(update={"contact_id": quiet_contact.contact_id})
    h = Harness(clock, case_in, quiet_contact, two_step_sequence)
    h.clock.set(at(2026, 9, 14, 17, 0))  # 22:30 in Kolkata

    verdict = h.decide("s1")
    assert verdict.kind is VerdictKind.HOLD
    assert verdict.reason is Reason.QUIET_HOURS
    assert verdict.retry_after is not None
    assert is_quiet(verdict.retry_after, quiet_contact.timezone, quiet_contact.quiet_hours) is False


def test_the_gate_sends_once_the_window_has_passed(
    clock, case, two_step_sequence, quiet_contact
):
    from .conftest import Harness

    case_in = case.model_copy(update={"contact_id": quiet_contact.contact_id})
    h = Harness(clock, case_in, quiet_contact, two_step_sequence)
    h.clock.set(at(2026, 9, 14, 17, 0))
    held = h.decide("s1")

    h.clock.set(held.retry_after)
    assert h.decide("s1").kind is VerdictKind.SEND


def test_a_contact_without_quiet_hours_is_reachable_at_any_hour(harness):
    harness.clock.set(at(2026, 9, 15, 3, 0))  # after the case opened, middle of the night
    assert harness.decide("s1").kind is VerdictKind.SEND


def test_the_local_time_goes_into_the_evidence(clock, case, two_step_sequence, quiet_contact):
    from .conftest import Harness

    case_in = case.model_copy(update={"contact_id": quiet_contact.contact_id})
    h = Harness(clock, case_in, quiet_contact, two_step_sequence)
    h.clock.set(at(2026, 9, 14, 17, 0))
    verdict = h.decide("s1")
    assert verdict.evidence.local_time is not None
    assert "T22:30" in verdict.evidence.local_time


# ------------------------------------------------------------------ validation


def test_a_window_with_identical_ends_is_refused():
    with pytest.raises(ValueError, match="identical"):
        QuietHours(start_hour=9, end_hour=9)


def test_an_unknown_timezone_is_refused_when_the_contact_is_built():
    from sequence_gate import Channel, Contact

    with pytest.raises(ValueError, match="unknown IANA timezone"):
        Contact(
            contact_id="x",
            timezone="Mars/Olympus_Mons",
            addresses={Channel.SMS: "+1"},
        )
