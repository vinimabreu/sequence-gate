"""Rule one: stopping conditions are evaluated before the send, every single time.

This is the bug the package exists to prevent. A sequence that checks whether the
contact replied only after it has already sent the next message is not a sequence with
a small timing flaw; it is a sequence that argues with people who already answered.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from sequence_gate import Channel, Reason, Signal, SignalType, VerdictKind
from sequence_gate.stores import StoreUnavailable

from .conftest import T0


@pytest.mark.parametrize(
    "signal_type",
    [SignalType.REPLIED, SignalType.PAID, SignalType.DISPUTED, SignalType.CLOSED],
)
def test_every_stopping_signal_stops_the_sequence(harness, signal_type):
    harness.signals.add(
        Signal(type=signal_type, at=T0, source="test", case_id="k1")
    )
    verdict = harness.decide("s1")
    assert verdict.kind is VerdictKind.STOP
    assert verdict.reason is Reason.STOPPING_SIGNAL
    assert signal_type.value in verdict.detail


def test_a_reply_between_steps_stops_the_next_step(harness):
    harness.send_and_confirm("s1")
    harness.clock.advance(timedelta(days=2))

    harness.signals.add(
        Signal(type=SignalType.REPLIED, at=harness.clock.now(), source="inbox", case_id="k1")
    )
    verdict = harness.decide("s2")

    assert verdict.kind is VerdictKind.STOP
    assert harness.ledger.get("k1", "s2") is None, "nothing may be written for a stopped step"


def test_stop_is_checked_before_the_delay(harness):
    """A stopped case reports STOP, not "not due yet". The reason has to be the truth."""
    harness.signals.add(Signal(type=SignalType.PAID, at=T0, source="billing", case_id="k1"))
    verdict = harness.decide("s2")
    assert verdict.reason is Reason.STOPPING_SIGNAL


def test_stop_is_checked_before_quiet_hours(clock, case, contact, two_step_sequence):
    from .conftest import Harness

    quiet = contact.model_copy(
        update={
            "timezone": "Asia/Kolkata",
            "quiet_hours": __import__(
                "sequence_gate", fromlist=["QuietHours"]
            ).QuietHours(start_hour=0, end_hour=23, end_minute=59),
        }
    )
    h = Harness(clock, case, quiet, two_step_sequence)
    h.signals.add(Signal(type=SignalType.CLOSED, at=T0, source="crm", case_id="k1"))
    assert h.decide("s1").reason is Reason.STOPPING_SIGNAL


def test_the_earliest_stopping_signal_is_the_one_reported(harness):
    later = T0 + timedelta(hours=3)
    harness.signals.add(Signal(type=SignalType.DISPUTED, at=later, source="agent", case_id="k1"))
    harness.signals.add(Signal(type=SignalType.REPLIED, at=T0, source="inbox", case_id="k1"))
    verdict = harness.decide("s1")
    assert "replied" in verdict.detail, "the first thing that ended the case is what ended it"


def test_a_signal_for_another_case_does_not_stop_this_one(harness):
    harness.signals.add(
        Signal(type=SignalType.REPLIED, at=T0, source="inbox", case_id="another-case")
    )
    assert harness.decide("s1").kind is VerdictKind.SEND


def test_unreadable_signal_store_holds_it_never_sends(harness):
    harness.signals.available = False
    verdict = harness.decide("s1")
    assert verdict.kind is VerdictKind.HOLD
    assert verdict.reason is Reason.STATE_UNREADABLE
    assert harness.ledger.get("k1", "s1") is None


def test_unreadable_store_raises_rather_than_returning_empty(harness):
    """An empty list reads as "nothing stopped this case", which is the dangerous lie."""
    harness.signals.available = False
    with pytest.raises(StoreUnavailable):
        harness.signals.for_case("k1")


def test_unreadable_ledger_also_holds(harness):
    harness.ledger.available = False
    verdict = harness.decide("s1")
    assert verdict.kind is VerdictKind.HOLD
    assert verdict.reason is Reason.STATE_UNREADABLE


def test_stale_signals_hold_when_a_max_age_is_configured(clock, case, contact, two_step_sequence):
    from sequence_gate import GateConfig

    from .conftest import Harness

    h = Harness(
        clock, case, contact, two_step_sequence,
        config=GateConfig(max_signal_age=timedelta(hours=1)),
    )
    h.signals.add(
        Signal(
            type=SignalType.BOUNCED,
            at=T0 - timedelta(days=3),
            source="esp",
            case_id="k1",
            channel=Channel.EMAIL,
        )
    )
    verdict = h.decide("s1")
    assert verdict.kind is VerdictKind.HOLD
    assert verdict.reason is Reason.SIGNALS_STALE


def test_fresh_signals_do_not_trip_the_staleness_hold(clock, case, contact, two_step_sequence):
    from sequence_gate import GateConfig

    from .conftest import Harness

    h = Harness(
        clock, case, contact, two_step_sequence,
        config=GateConfig(max_signal_age=timedelta(hours=1)),
    )
    h.signals.add(
        Signal(
            type=SignalType.BOUNCED,
            at=T0 - timedelta(minutes=5),
            source="esp",
            case_id="k1",
            channel=Channel.SMS,
        )
    )
    assert h.decide("s1").kind is VerdictKind.SEND


def test_every_decision_is_audited(harness):
    harness.decide("s1")
    harness.signals.add(Signal(type=SignalType.REPLIED, at=T0, source="inbox", case_id="k1"))
    harness.decide("s1")

    rows = harness.audit.for_case("k1")
    assert len(rows) == 2
    assert [r["kind"] for r in rows] == ["send", "stop"]


def test_the_audit_row_carries_the_signals_that_were_read(harness):
    harness.signals.add(Signal(type=SignalType.PAID, at=T0, source="stripe", case_id="k1"))
    harness.decide("s1")
    row = harness.audit.for_case("k1")[-1]
    read = row["evidence"]["signals_read"]
    assert read and read[0]["source"] == "stripe"
