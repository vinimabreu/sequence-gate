"""Rule seven: an opt-out follows the person, not the campaign.

Consent is the one rule here with a legal edge as well as a decency one. It is also the
one most often scoped wrongly, because opt-out usually arrives in the context of a
single sequence and gets recorded there. The person did not opt out of a sequence. They
opted out of being contacted.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from sequence_gate import (
    Case,
    Channel,
    InMemoryAuditLog,
    InMemoryCaseStore,
    InMemorySendLedger,
    InMemorySignalStore,
    Reason,
    Sequence,
    SequenceGate,
    Signal,
    SignalType,
    Step,
    VerdictKind,
)

from .conftest import T0, Harness


def multichannel_sequence() -> Sequence:
    return Sequence(
        sequence_id="seq",
        steps=(
            Step(step_id="s1", channel=Channel.SMS, content_ref="tpl.sms"),
            Step(
                step_id="s2",
                channel=Channel.EMAIL,
                delay_after_previous=timedelta(hours=1),
                content_ref="tpl.email",
            ),
        ),
    )


def test_a_global_opt_out_stops_every_channel(clock, case, contact):
    h = Harness(clock, case, contact, multichannel_sequence())
    h.signals.add(
        Signal(type=SignalType.OPTED_OUT, at=T0, source="stop-keyword", contact_id="c1")
    )
    assert h.decide("s1").reason is Reason.OPTED_OUT
    h.clock.advance(timedelta(hours=2))
    assert h.decide("s2").reason is Reason.OPTED_OUT


def test_a_channel_scoped_opt_out_leaves_the_others_alone(clock, case, contact):
    h = Harness(clock, case, contact, multichannel_sequence())
    h.signals.add(
        Signal(
            type=SignalType.OPTED_OUT,
            at=T0,
            source="stop-keyword",
            contact_id="c1",
            channel=Channel.SMS,
        )
    )
    assert h.decide("s1").reason is Reason.OPTED_OUT
    h.clock.advance(timedelta(hours=2))
    assert h.decide("s2").kind is VerdictKind.SEND, "the email consent was never withdrawn"


def test_an_opt_out_reaches_a_sequence_it_never_appeared_in(clock, case, contact):
    """The signal is contact-scoped, so a second campaign inherits it untouched."""
    other = Sequence(
        sequence_id="newsletter",
        steps=(Step(step_id="n1", channel=Channel.EMAIL, content_ref="tpl.n"),),
    )
    other_case = Case(
        case_id="k2", sequence_id="newsletter", contact_id="c1", started_at=T0
    )
    signals = InMemorySignalStore(
        [Signal(type=SignalType.OPTED_OUT, at=T0, source="preference-centre", contact_id="c1")]
    )
    ledger = InMemorySendLedger()
    ledger.bind_case_to_contact("k2", "c1")

    gate = SequenceGate(
        clock=clock,
        cases=InMemoryCaseStore([other_case], [contact]),
        sequences={"newsletter": other},
        signals=signals,
        ledger=ledger,
        audit=InMemoryAuditLog(),
    )
    assert gate.decide("k2", "n1").reason is Reason.OPTED_OUT


def test_an_opt_out_arriving_mid_sequence_stops_the_rest(clock, case, contact):
    h = Harness(clock, case, contact, multichannel_sequence())
    h.send_and_confirm("s1")
    h.clock.advance(timedelta(hours=2))
    h.signals.add(
        Signal(type=SignalType.OPTED_OUT, at=h.clock.now(), source="reply", contact_id="c1")
    )
    assert h.decide("s2").reason is Reason.OPTED_OUT


def test_the_opt_out_is_reported_ahead_of_any_other_stop(clock, case, contact):
    """Consent outranks everything, including a case that was closed for other reasons."""
    h = Harness(clock, case, contact, multichannel_sequence())
    h.signals.add(Signal(type=SignalType.CLOSED, at=T0, source="crm", case_id="k1"))
    h.signals.add(
        Signal(type=SignalType.OPTED_OUT, at=T0, source="stop-keyword", contact_id="c1")
    )
    assert h.decide("s1").reason is Reason.OPTED_OUT


def test_an_opt_out_for_a_different_contact_is_not_inherited(clock, case, contact):
    h = Harness(clock, case, contact, multichannel_sequence())
    h.signals.add(
        Signal(type=SignalType.OPTED_OUT, at=T0, source="stop-keyword", contact_id="someone-else")
    )
    assert h.decide("s1").kind is VerdictKind.SEND


def test_an_opt_out_without_a_contact_is_refused_when_it_is_built():
    with pytest.raises(ValueError, match="contact-scoped"):
        Signal(type=SignalType.OPTED_OUT, at=T0, source="x", case_id="k1")


# ------------------------------------------------------------------- bounces


def test_a_bounce_skips_that_channel_and_offers_the_next_step(clock, case, contact):
    h = Harness(clock, case, contact, multichannel_sequence())
    h.signals.add(
        Signal(
            type=SignalType.BOUNCED, at=T0, source="gateway", contact_id="c1", channel=Channel.SMS
        )
    )
    verdict = h.decide("s1")
    assert verdict.kind is VerdictKind.SKIP
    assert verdict.reason is Reason.NO_ADDRESS_FOR_CHANNEL
    assert verdict.next_step_id == "s2"


def test_a_bounce_on_one_channel_leaves_the_other_usable(clock, case, contact):
    h = Harness(clock, case, contact, multichannel_sequence())
    h.signals.add(
        Signal(
            type=SignalType.BOUNCED, at=T0, source="gateway", contact_id="c1", channel=Channel.SMS
        )
    )
    h.clock.advance(timedelta(hours=2))
    assert h.decide("s2").kind is VerdictKind.SEND


def test_a_bounce_needs_to_say_which_channel_bounced():
    with pytest.raises(ValueError, match="needs the channel"):
        Signal(type=SignalType.BOUNCED, at=T0, source="gateway", contact_id="c1")


def test_a_missing_address_skips_without_needing_a_signal(clock, case, contact):
    bare = contact.model_copy(update={"addresses": {Channel.EMAIL: "only@example.test"}})
    h = Harness(clock, case, bare, multichannel_sequence())
    verdict = h.decide("s1")
    assert verdict.kind is VerdictKind.SKIP
    assert verdict.reason is Reason.NO_ADDRESS_FOR_CHANNEL


def test_a_skip_for_the_last_step_offers_no_next_step(clock, case, contact):
    one_step = Sequence(
        sequence_id="seq",
        steps=(Step(step_id="s1", channel=Channel.VOICE, content_ref="tpl.v"),),
    )
    h = Harness(clock, case, contact, one_step)
    assert h.decide("s1").next_step_id is None


def test_a_signal_needs_a_scope_of_some_kind():
    with pytest.raises(ValueError, match="case_id or a contact_id"):
        Signal(type=SignalType.REPLIED, at=T0, source="x")


def test_signal_timestamps_must_be_timezone_aware():
    from datetime import datetime

    with pytest.raises(ValueError, match="timezone aware"):
        Signal(type=SignalType.REPLIED, at=datetime(2026, 9, 14, 12), source="x", case_id="k1")
