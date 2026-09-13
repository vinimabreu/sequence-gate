"""`advance` is the call an orchestrator actually makes.

A cron in n8n holds a case id and a loop. It does not hold a memory of which step the
case reached, and it should not have to: a workflow that tracks position is a workflow
that loses position when it restarts. So the gate walks the steps and answers about the
case as a whole.
"""

from __future__ import annotations

from datetime import timedelta

from sequence_gate import Reason, Signal, SignalType, VerdictKind

from .conftest import T0


def test_a_fresh_case_is_pointed_at_its_first_step(harness):
    verdict = harness.gate.advance("k1")
    assert verdict.kind is VerdictKind.SEND
    assert verdict.next_step_id == "s1"


def test_a_settled_step_is_walked_past(harness):
    harness.send_and_confirm("s1")
    harness.clock.advance(timedelta(days=2))
    verdict = harness.gate.advance("k1")
    assert verdict.kind is VerdictKind.SEND
    assert verdict.next_step_id == "s2"


def test_a_case_with_every_step_sent_reports_the_sequence_is_over(harness):
    harness.send_and_confirm("s1")
    harness.clock.advance(timedelta(days=2))
    harness.send_and_confirm("s2", provider_id="prov-2")

    verdict = harness.gate.advance("k1")
    assert verdict.kind is VerdictKind.STOP
    assert verdict.reason is Reason.SEQUENCE_EXHAUSTED


def test_a_stopped_case_reports_the_stop_not_the_next_step(harness):
    harness.signals.add(Signal(type=SignalType.PAID, at=T0, source="billing", case_id="k1"))
    verdict = harness.gate.advance("k1")
    assert verdict.kind is VerdictKind.STOP
    assert verdict.reason is Reason.STOPPING_SIGNAL


def test_a_held_case_reports_the_hold_and_names_the_step(harness):
    harness.send_and_confirm("s1")
    verdict = harness.gate.advance("k1")
    assert verdict.kind is VerdictKind.HOLD
    assert verdict.reason is Reason.DELAY_NOT_ELAPSED
    assert verdict.next_step_id == "s2"


def test_an_unreachable_channel_does_not_stall_the_whole_case(clock, case, contact):
    """A step with no address is settled, not blocking. The case keeps moving."""
    from sequence_gate import Channel, Sequence, Step

    from .conftest import Harness

    sequence = Sequence(
        sequence_id="seq",
        steps=(
            Step(step_id="s1", channel=Channel.LETTER, content_ref="tpl.post"),
            Step(step_id="s2", channel=Channel.EMAIL, content_ref="tpl.mail"),
        ),
    )
    h = Harness(clock, case, contact, sequence)  # the contact has no postal address
    verdict = h.gate.advance("k1")
    assert verdict.kind is VerdictKind.SEND
    assert verdict.next_step_id == "s2"


def test_the_exhausted_verdict_is_audited(harness):
    harness.send_and_confirm("s1")
    harness.clock.advance(timedelta(days=2))
    harness.send_and_confirm("s2", provider_id="prov-2")
    harness.gate.advance("k1")

    kinds = [r["reason"] for r in harness.audit.for_case("k1")]
    assert "sequence_exhausted" in kinds


def test_advance_on_an_unknown_case_is_an_error(harness):
    import pytest

    from sequence_gate.engine import UnknownCase

    with pytest.raises(UnknownCase):
        harness.gate.advance("nope")
