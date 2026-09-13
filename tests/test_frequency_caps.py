"""Rule five: caps count across every sequence the contact is in.

Two campaigns that each respect their own cap of one message a day still add up to two
messages a day for the person receiving them. The contact does not experience your
sequences separately, so the counter cannot be scoped to one either.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from sequence_gate import (
    Case,
    Channel,
    FrequencyCap,
    InMemoryAuditLog,
    InMemoryCaseStore,
    InMemorySendLedger,
    InMemorySignalStore,
    Reason,
    Sequence,
    SequenceGate,
    Step,
    VerdictKind,
)

from .conftest import T0, Harness


def capped_sequence(limit: int = 1, window: timedelta = timedelta(days=1)) -> Sequence:
    return Sequence(
        sequence_id="seq",
        steps=(
            Step(step_id="s1", channel=Channel.SMS, content_ref="tpl.one"),
            Step(
                step_id="s2",
                channel=Channel.SMS,
                delay_after_previous=timedelta(hours=1),
                content_ref="tpl.two",
            ),
            Step(
                step_id="s3",
                channel=Channel.EMAIL,
                delay_after_previous=timedelta(hours=1),
                content_ref="tpl.three",
            ),
        ),
        caps=(FrequencyCap(channel=Channel.SMS, limit=limit, window=window),),
    )


def test_the_cap_holds_the_second_send_inside_the_window(clock, case, contact):
    h = Harness(clock, case, contact, capped_sequence())
    h.send_and_confirm("s1")
    h.clock.advance(timedelta(hours=1))

    verdict = h.decide("s2")
    assert verdict.kind is VerdictKind.HOLD
    assert verdict.reason is Reason.FREQUENCY_CAP
    assert verdict.evidence.sends_in_window == 1


def test_the_cap_releases_once_the_window_slides_past(clock, case, contact):
    h = Harness(clock, case, contact, capped_sequence())
    h.send_and_confirm("s1")
    h.clock.advance(timedelta(days=1, minutes=1))
    assert h.decide("s2").kind is VerdictKind.SEND


def test_the_hold_says_exactly_when_the_window_clears(clock, case, contact):
    h = Harness(clock, case, contact, capped_sequence())
    h.send_and_confirm("s1")
    h.clock.advance(timedelta(hours=1))
    held = h.decide("s2")

    assert held.retry_after == T0 + timedelta(days=1)
    h.clock.set(held.retry_after)
    assert h.decide("s2").kind is VerdictKind.SEND


def test_a_cap_on_one_channel_does_not_gag_another(clock, case, contact):
    h = Harness(clock, case, contact, capped_sequence())
    h.send_and_confirm("s1")
    h.clock.advance(timedelta(hours=2))
    assert h.decide("s3").kind is VerdictKind.SEND, "the email cap does not exist"


def test_a_higher_limit_allows_the_second_message(clock, case, contact):
    h = Harness(clock, case, contact, capped_sequence(limit=2))
    h.send_and_confirm("s1")
    h.clock.advance(timedelta(hours=1))
    assert h.decide("s2").kind is VerdictKind.SEND


def test_caps_count_sends_from_a_different_sequence(clock, case, contact):
    """The point of the whole rule: one shared ledger, one contact, one counter."""
    marketing = capped_sequence()
    dunning = Sequence(
        sequence_id="dunning",
        steps=(Step(step_id="d1", channel=Channel.SMS, content_ref="tpl.d"),),
        caps=(FrequencyCap(channel=Channel.SMS, limit=1, window=timedelta(days=1)),),
    )
    other_case = Case(
        case_id="k2", sequence_id="dunning", contact_id=contact.contact_id, started_at=T0
    )

    ledger = InMemorySendLedger()
    ledger.bind_case_to_contact(case.case_id, contact.contact_id)
    ledger.bind_case_to_contact(other_case.case_id, contact.contact_id)

    gate = SequenceGate(
        clock=clock,
        cases=InMemoryCaseStore([case, other_case], [contact]),
        sequences={"seq": marketing, "dunning": dunning},
        signals=InMemorySignalStore(),
        ledger=ledger,
        audit=InMemoryAuditLog(),
    )

    first = gate.decide("k1", "s1")
    gate.confirm(first.send_token, "p1")

    second = gate.decide("k2", "d1")
    assert second.kind is VerdictKind.HOLD
    assert second.reason is Reason.FREQUENCY_CAP, (
        "a second campaign cannot spend the same person's daily allowance"
    )


def test_a_failed_send_does_not_consume_the_allowance(clock, case, contact):
    h = Harness(clock, case, contact, capped_sequence())
    verdict = h.decide("s1")
    h.gate.confirm(verdict.send_token, "p1", status="failed")
    h.clock.advance(timedelta(hours=1))
    assert h.decide("s2").kind is VerdictKind.SEND


def test_quiet_hours_and_a_cap_hold_until_the_later_of_the_two(
    clock, case, two_step_sequence, quiet_contact
):
    """Both constraints have to clear, so the retry time is the later one, not the first."""
    sequence = Sequence(
        sequence_id="seq",
        steps=(
            Step(step_id="s1", channel=Channel.SMS, content_ref="tpl.one"),
            Step(
                step_id="s2",
                channel=Channel.SMS,
                delay_after_previous=timedelta(hours=2),
                content_ref="tpl.two",
            ),
        ),
        caps=(FrequencyCap(channel=Channel.SMS, limit=1, window=timedelta(days=3)),),
    )
    case_in = case.model_copy(update={"contact_id": quiet_contact.contact_id})
    h = Harness(clock, case_in, quiet_contact, sequence)

    h.send_and_confirm("s1")
    h.clock.advance(timedelta(hours=6))  # 23:30 in Kolkata, and inside the cap window

    verdict = h.decide("s2")
    assert verdict.kind is VerdictKind.HOLD
    assert verdict.reason is Reason.FREQUENCY_CAP, "the cap clears later than the night does"
    assert verdict.retry_after == T0 + timedelta(days=3)


def test_a_cap_needs_a_positive_window():
    with pytest.raises(ValueError, match="positive window"):
        FrequencyCap(channel=Channel.SMS, limit=1, window=timedelta(0))


def test_a_cap_needs_a_limit_of_at_least_one():
    with pytest.raises(ValueError):
        FrequencyCap(channel=Channel.SMS, limit=0, window=timedelta(days=1))
