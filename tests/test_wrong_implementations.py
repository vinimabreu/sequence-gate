"""The three wrong implementations, run and watched failing.

A README that says "we handle idempotency correctly" is a claim. A test that runs the
naive version and catches it texting the same person twice is evidence. These tests
fail loudly if someone ever "fixes" the wrong implementations, which is the point:
they are the control group.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sequence_gate import (
    EscalationLadder,
    EscalationRung,
    InMemoryAuditLog,
    InMemoryCaseStore,
    InMemorySignalStore,
    QuietHours,
    Reason,
    SequenceGate,
    Signal,
    SignalType,
    VerdictKind,
)
from sequence_gate.quiet_hours import is_quiet
from sequence_gate.wrong import (
    DeliveryKeyedLedger,
    PostSendStopCheck,
    server_clock_is_quiet,
)

from .conftest import T0

# --------------------------------------------------------------------------- 1


def test_post_send_stop_check_sends_to_someone_who_already_replied(
    clock, case, contact, two_step_sequence
):
    """The wrong order writes the send first, so the reply arrives too late to matter."""
    from sequence_gate import InMemorySendLedger

    cases = InMemoryCaseStore([case], [contact])
    signals = InMemorySignalStore(
        [Signal(type=SignalType.REPLIED, at=T0, source="inbox", case_id="k1")]
    )
    ledger = InMemorySendLedger()
    ledger.bind_case_to_contact("k1", "c1")

    bad = PostSendStopCheck(
        clock=clock,
        cases=cases,
        sequences={"seq": two_step_sequence},
        signals=signals,
        ledger=ledger,
        audit=InMemoryAuditLog(),
    )
    verdict = bad.decide("k1", "s1")

    assert verdict.kind is VerdictKind.STOP, "it does eventually notice"
    assert ledger.get("k1", "s1") is not None, (
        "but the send was already committed before it looked, "
        "which is the whole bug: the record exists for a case that should never have been touched"
    )


def test_the_correct_order_writes_nothing_for_a_stopped_case(harness):
    harness.signals.add(Signal(type=SignalType.REPLIED, at=T0, source="inbox", case_id="k1"))
    assert harness.decide("s1").kind is VerdictKind.STOP
    assert harness.ledger.get("k1", "s1") is None


# --------------------------------------------------------------------------- 2


def test_delivery_keyed_ledger_double_sends_on_replay(clock, case, contact, two_step_sequence):
    """A retried webhook carries a fresh provider id, so the naive index sees nothing."""
    ledger = DeliveryKeyedLedger()
    ledger.bind_case_to_contact("k1", "c1")

    gate = SequenceGate(
        clock=clock,
        cases=InMemoryCaseStore([case], [contact]),
        sequences={"seq": two_step_sequence},
        signals=InMemorySignalStore(),
        ledger=ledger,
        audit=InMemoryAuditLog(),
        token_factory=lambda: f"tok-{len(ledger.deliveries) + 1}",
    )

    first = gate.decide("k1", "s1")
    gate.confirm(first.send_token, "provider-message-A")

    # The orchestrator restarts and asks again for the same case and the same step.
    second = gate.decide("k1", "s1")
    gate.confirm(second.send_token, "provider-message-B")

    assert first.kind is VerdictKind.SEND
    assert second.kind is VerdictKind.SEND, "the naive ledger cannot see the first send"
    assert len({r.provider_message_id for r in ledger.deliveries}) == 2, (
        "same case, same step, two messages on the wire"
    )


def test_case_and_step_keying_refuses_the_second_send(harness):
    harness.send_and_confirm("s1")
    second = harness.decide("s1")
    assert second.kind is VerdictKind.SKIP
    assert second.reason is Reason.ALREADY_SENT


def test_case_and_step_keying_survives_a_replay_before_confirmation(harness):
    first = harness.decide("s1")
    second = harness.decide("s1")
    assert first.kind is VerdictKind.SEND
    assert second.kind is VerdictKind.HOLD
    assert second.reason is Reason.TOKEN_OUTSTANDING


# --------------------------------------------------------------------------- 3


def test_server_clock_quiet_hours_texts_at_three_in_the_morning():
    """Server in UTC, contact in Kolkata, a window of 21:00 to 08:00.

    21:42 UTC is 03:12 the next morning in Kolkata. The contact's clock says the middle
    of the night; the server's clock says a perfectly reasonable evening.
    """
    window = QuietHours(start_hour=21, end_hour=8)
    instant = datetime(2026, 9, 14, 21, 42, tzinfo=UTC)

    assert is_quiet(instant, "Asia/Kolkata", window) is True, "the contact is asleep"
    assert server_clock_is_quiet(instant, "Asia/Kolkata", window, server_zone="UTC") is True

    # And the reverse, which is the one that actually sends: 18:42 UTC is 00:12 in Kolkata.
    midnight_there = datetime(2026, 9, 14, 18, 42, tzinfo=UTC)
    assert is_quiet(midnight_there, "Asia/Kolkata", window) is True
    assert (
        server_clock_is_quiet(midnight_there, "Asia/Kolkata", window, server_zone="UTC")
        is False
    ), "the server sees 18:42 and happily sends into 00:12 local"


def test_the_gate_holds_where_the_server_clock_would_have_sent(
    clock, case, two_step_sequence, quiet_contact
):
    from .conftest import Harness

    case_in = case.model_copy(update={"contact_id": quiet_contact.contact_id})
    h = Harness(clock, case_in, quiet_contact, two_step_sequence)
    h.clock.set(datetime(2026, 9, 14, 18, 42, tzinfo=UTC))  # 00:12 in Kolkata

    verdict = h.decide("s1")
    assert verdict.kind is VerdictKind.HOLD
    assert verdict.reason is Reason.QUIET_HOURS
    assert "00:12" in verdict.detail


# --------------------------------------------------------------------------- 4


def test_ladder_without_a_human_is_refused_at_load_time():
    """The refusal belongs in config validation, not in an incident report."""
    with pytest.raises(ValueError, match="must be a human handoff"):
        EscalationLadder(
            rungs=(
                EscalationRung(target="supplier-a", wait=timedelta(minutes=15)),
                EscalationRung(target="supplier-b", wait=timedelta(minutes=15)),
            )
        )


def test_a_ladder_that_ends_with_a_person_loads():
    ladder = EscalationLadder(
        rungs=(
            EscalationRung(target="supplier-a", wait=timedelta(minutes=15)),
            EscalationRung(target="controller-desk", is_human=True, wait=timedelta(hours=1)),
        )
    )
    assert ladder.max_depth == 2
    assert ladder.rungs[-1].is_human
