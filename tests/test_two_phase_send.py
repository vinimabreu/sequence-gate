"""Rule three: decide, send, confirm. And a token that goes stale rather than sticking.

The failure this answers is dull and common. The gate says send, the workflow calls the
SMS provider, and the provider times out. Did the message go? Nobody knows. Treating
the decision as the send loses messages; treating it as nothing sends them twice. So the
decision reserves the step, and only a confirmation closes it, and a reservation nobody
confirms expires so the case can move again.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from sequence_gate import GateConfig, Reason, VerdictKind

from .conftest import Harness


def test_a_decision_reserves_the_step_without_closing_it(harness):
    verdict = harness.decide("s1")
    record = harness.ledger.get("k1", "s1")
    assert verdict.kind is VerdictKind.SEND
    assert record.status == "pending"
    assert record.is_confirmed is False


def test_confirming_closes_the_step(harness):
    verdict = harness.decide("s1")
    record = harness.gate.confirm(verdict.send_token, "provider-1")
    assert record.is_confirmed
    assert record.provider_message_id == "provider-1"


def test_an_outstanding_token_holds_a_second_caller(harness):
    harness.decide("s1")
    second = harness.decide("s1")
    assert second.kind is VerdictKind.HOLD
    assert second.reason is Reason.TOKEN_OUTSTANDING


def test_the_hold_expires_exactly_at_the_ttl(clock, case, contact, two_step_sequence):
    h = Harness(
        clock, case, contact, two_step_sequence,
        config=GateConfig(token_ttl=timedelta(minutes=10)),
    )
    first = h.decide("s1")
    held = h.decide("s1")

    h.clock.set(held.retry_after)
    second = h.decide("s1")
    assert second.kind is VerdictKind.SEND
    assert second.send_token != first.send_token, "a fresh reservation, not the stale one"


def test_a_provider_outage_does_not_freeze_the_case_forever(
    clock, case, contact, two_step_sequence
):
    h = Harness(
        clock, case, contact, two_step_sequence,
        config=GateConfig(token_ttl=timedelta(minutes=5)),
    )
    h.decide("s1")
    h.clock.advance(timedelta(minutes=6))
    assert h.decide("s1").kind is VerdictKind.SEND


def test_a_confirmed_step_never_reopens_no_matter_how_long_it_has_been(harness):
    harness.send_and_confirm("s1")
    harness.clock.advance(timedelta(days=365))
    verdict = harness.decide("s1")
    assert verdict.kind is VerdictKind.SKIP
    assert verdict.reason is Reason.ALREADY_SENT


def test_a_failed_send_can_be_retried_immediately(harness):
    verdict = harness.decide("s1")
    harness.gate.confirm(verdict.send_token, "provider-1", status="failed")
    retry = harness.decide("s1")
    assert retry.kind is VerdictKind.SEND


def test_a_failed_step_reports_the_failure_rather_than_hiding_it(harness):
    verdict = harness.decide("s1")
    record = harness.gate.confirm(verdict.send_token, "provider-1", status="failed")
    assert record.status == "failed"
    assert record.is_confirmed is False


def test_confirming_an_unknown_token_is_an_error(harness):
    with pytest.raises(KeyError):
        harness.gate.confirm("not-a-token", "provider-1")


def test_the_skip_echoes_the_original_token(harness):
    token = harness.send_and_confirm("s1")
    assert harness.decide("s1").send_token == token


def test_the_next_step_is_timed_from_the_confirmation_not_the_decision(
    clock, case, contact, two_step_sequence
):
    """The clock on step two starts when step one actually went out."""
    h = Harness(clock, case, contact, two_step_sequence)
    h.decide("s1")
    h.clock.advance(timedelta(hours=6))
    record = h.ledger.get("k1", "s1")
    h.gate.confirm(record.token, "provider-1")

    h.clock.advance(timedelta(days=2) - timedelta(minutes=1))
    assert h.decide("s2").kind is VerdictKind.HOLD, "two days from the send, not from the decision"

    h.clock.advance(timedelta(minutes=1))
    assert h.decide("s2").kind is VerdictKind.SEND


def test_the_first_step_is_timed_from_the_case_opening(harness):
    assert harness.decide("s1").kind is VerdictKind.SEND


def test_a_token_ttl_must_be_positive():
    with pytest.raises(ValueError, match="token_ttl must be positive"):
        GateConfig(token_ttl=timedelta(0))


def test_a_max_signal_age_must_be_positive_when_set():
    with pytest.raises(ValueError, match="max_signal_age must be positive"):
        GateConfig(max_signal_age=timedelta(seconds=-1))
