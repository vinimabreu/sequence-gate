"""Rule six: the ladder is bounded, and the bottom rung is a person.

The dispatch case is the one that makes this concrete. A job goes to the nearest
supplier, who does not answer. Something has to notice the silence, move to the next
supplier, and eventually stop guessing and put a human on it. A ladder without a last
rung is a case that disappears.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from sequence_gate import (
    Channel,
    EscalationLadder,
    EscalationRung,
    Reason,
    Sequence,
    Signal,
    SignalType,
    Step,
    VerdictKind,
)

from .conftest import Harness

LADDER = EscalationLadder(
    rungs=(
        EscalationRung(target="supplier-nearest", wait=timedelta(minutes=15)),
        EscalationRung(target="supplier-next", wait=timedelta(minutes=15)),
        EscalationRung(target="controller-desk", is_human=True, wait=timedelta(hours=2)),
    )
)


def dispatch_sequence() -> Sequence:
    return Sequence(
        sequence_id="seq",
        steps=(
            Step(
                step_id="s1",
                channel=Channel.WHATSAPP,
                content_ref="tpl.job",
                escalation=LADDER,
            ),
        ),
    )


def test_before_the_wait_elapses_the_gate_holds(clock, case, contact):
    h = Harness(clock, case, contact, dispatch_sequence())
    h.send_and_confirm("s1")
    h.clock.advance(timedelta(minutes=5))

    verdict = h.decide("s1")
    assert verdict.kind is VerdictKind.HOLD
    assert verdict.reason is Reason.AWAITING_RESPONSE
    assert "rung 0" in verdict.detail


def test_the_hold_says_when_the_rung_expires(clock, case, contact):
    h = Harness(clock, case, contact, dispatch_sequence())
    h.send_and_confirm("s1")
    h.clock.advance(timedelta(minutes=5))
    held = h.decide("s1")

    h.clock.set(held.retry_after)
    assert h.decide("s1").kind is VerdictKind.ESCALATE


def test_silence_at_the_first_rung_escalates_to_the_second(clock, case, contact):
    h = Harness(clock, case, contact, dispatch_sequence())
    h.send_and_confirm("s1")
    h.clock.advance(timedelta(minutes=16))

    verdict = h.decide("s1")
    assert verdict.kind is VerdictKind.ESCALATE
    assert verdict.reason is Reason.NO_RESPONSE_AT_RUNG
    assert verdict.escalate_to == "supplier-next"


def test_silence_at_the_second_rung_hands_over_to_a_person(clock, case, contact):
    h = Harness(clock, case, contact, dispatch_sequence())
    h.send_and_confirm("s1")
    h.clock.advance(timedelta(minutes=16))
    h.cases.put_case(h.case.model_copy(update={"rung": 1}))

    verdict = h.decide("s1")
    assert verdict.kind is VerdictKind.ESCALATE
    assert verdict.escalate_to == "controller-desk"
    assert "handing to a person" in verdict.detail


def test_the_machine_stops_climbing_once_a_person_holds_the_case(clock, case, contact):
    h = Harness(clock, case, contact, dispatch_sequence())
    h.send_and_confirm("s1")
    h.clock.advance(timedelta(hours=3))
    h.cases.put_case(h.case.model_copy(update={"rung": 2}))

    verdict = h.decide("s1")
    assert verdict.kind is VerdictKind.HOLD
    assert "terminal human rung" in verdict.detail


def test_a_rung_beyond_the_ladder_is_clamped_rather_than_crashing(clock, case, contact):
    """A caller that over-increments must not take the gate down with it."""
    h = Harness(clock, case, contact, dispatch_sequence())
    h.send_and_confirm("s1")
    h.clock.advance(timedelta(hours=3))
    h.cases.put_case(h.case.model_copy(update={"rung": 99}))

    verdict = h.decide("s1")
    assert verdict.kind is VerdictKind.HOLD
    assert "terminal human rung" in verdict.detail


def test_a_reply_beats_the_escalation(clock, case, contact):
    h = Harness(clock, case, contact, dispatch_sequence())
    h.send_and_confirm("s1")
    h.clock.advance(timedelta(minutes=16))
    h.signals.add(
        Signal(type=SignalType.REPLIED, at=h.clock.now(), source="whatsapp", case_id="k1")
    )

    verdict = h.decide("s1")
    assert verdict.kind is VerdictKind.STOP, "a supplier who accepted is not escalated past"


def test_a_step_with_a_ladder_is_not_marked_already_sent(clock, case, contact):
    """Steps without a ladder are done once sent. Steps with one keep being asked about."""
    h = Harness(clock, case, contact, dispatch_sequence())
    h.send_and_confirm("s1")
    assert h.decide("s1").reason is not Reason.ALREADY_SENT


def test_an_unsent_step_does_not_escalate(clock, case, contact):
    h = Harness(clock, case, contact, dispatch_sequence())
    assert h.decide("s1").kind is VerdictKind.SEND


def test_a_decided_but_unconfirmed_step_does_not_escalate(clock, case, contact):
    """Escalating on a send that never left the building would blame the wrong party."""
    h = Harness(clock, case, contact, dispatch_sequence())
    h.decide("s1")
    h.clock.advance(timedelta(minutes=16))
    verdict = h.decide("s1")
    assert verdict.kind is not VerdictKind.ESCALATE


# ------------------------------------------------------------------ validation


def test_an_empty_ladder_is_refused():
    with pytest.raises(ValueError, match="at least one rung"):
        EscalationLadder(rungs=())


def test_a_rung_needs_a_positive_wait():
    with pytest.raises(ValueError, match="positive amount of time"):
        EscalationRung(target="x", wait=timedelta(0))


def test_max_depth_reports_the_number_of_rungs():
    assert LADDER.max_depth == 3
