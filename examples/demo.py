"""The four scenes the animation shows, run for real.

Every number in the README hero comes from this file. Run it and the output is the
animation's script:

    python examples/demo.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sequence_gate import (
    Case,
    Channel,
    Contact,
    FrequencyCap,
    FrozenClock,
    InMemoryAuditLog,
    InMemoryCaseStore,
    InMemorySendLedger,
    InMemorySignalStore,
    QuietHours,
    Sequence,
    SequenceGate,
    Signal,
    SignalType,
    Step,
)
from sequence_gate.wrong import DeliveryKeyedLedger

T0 = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)

CONTACT = Contact(
    contact_id="c-4417",
    timezone="Asia/Kolkata",
    addresses={Channel.EMAIL: "buyer@example.test", Channel.SMS: "+915550000000"},
    quiet_hours=QuietHours(start_hour=21, end_hour=8),
)

SEQUENCE = Sequence(
    sequence_id="dunning",
    steps=(
        Step(step_id="s1", channel=Channel.EMAIL, content_ref="tpl.invoice_due"),
        Step(
            step_id="s2",
            channel=Channel.SMS,
            delay_after_previous=timedelta(days=3),
            content_ref="tpl.reminder",
        ),
        Step(
            step_id="s3",
            channel=Channel.SMS,
            delay_after_previous=timedelta(days=4),
            content_ref="tpl.final",
        ),
    ),
    caps=(FrequencyCap(channel=Channel.SMS, limit=1, window=timedelta(days=1)),),
)

CASE = Case(case_id="k-4417", sequence_id="dunning", contact_id="c-4417", started_at=T0)


def build(clock: FrozenClock) -> tuple[SequenceGate, InMemorySignalStore, InMemorySendLedger]:
    signals = InMemorySignalStore()
    ledger = InMemorySendLedger()
    ledger.bind_case_to_contact(CASE.case_id, CONTACT.contact_id)
    gate = SequenceGate(
        clock=clock,
        cases=InMemoryCaseStore([CASE], [CONTACT]),
        sequences={SEQUENCE.sequence_id: SEQUENCE},
        signals=signals,
        ledger=ledger,
        audit=InMemoryAuditLog(),
        token_factory=lambda: f"tok-{len(ledger._by_step) + 1}",
    )
    return gate, signals, ledger


def line(label: str, verdict) -> None:
    extra = ""
    if verdict.retry_after:
        extra = f"  retry_after={verdict.retry_after.isoformat()}"
    if verdict.send_token:
        extra = f"  token={verdict.send_token}"
    print(f"  {label:<34} {verdict.kind.value.upper():<9} {verdict.reason.value}{extra}")


def scene_00_setup() -> None:
    print("\n00  THE CASE")
    print(f"  sequence      {SEQUENCE.sequence_id}, {len(SEQUENCE.steps)} steps")
    print(f"  contact       {CONTACT.contact_id} in {CONTACT.timezone}")
    print(
        f"  quiet hours   {CONTACT.quiet_hours.start_hour:02d}:00 to "
        f"{CONTACT.quiet_hours.end_hour:02d}:00 local"
    )
    print(f"  cap           {SEQUENCE.caps[0].limit} sms per {SEQUENCE.caps[0].window.days} day")


def scene_01_stop_before_send() -> None:
    print("\n01  STOP BEFORE THE SEND")
    clock = FrozenClock(T0)
    gate, signals, ledger = build(clock)

    line("day 0, first email", gate.decide("k-4417", "s1"))
    gate.confirm("tok-1", "esp-9f31")

    clock.advance(timedelta(days=3))
    line("day 3, reminder sms", gate.decide("k-4417", "s2"))
    gate.confirm("tok-2", "sms-77a2")

    clock.advance(timedelta(days=1))
    signals.add(
        Signal(type=SignalType.PAID, at=clock.now(), source="stripe", case_id="k-4417")
    )
    print("  day 4, the invoice is paid")

    clock.advance(timedelta(days=3))
    line("day 7, final notice", gate.decide("k-4417", "s3"))
    print(f"  ledger rows written for s3     {ledger.get('k-4417', 's3')}")


def scene_02_replay() -> None:
    print("\n02  THE REPLAY, TWO LEDGERS")
    clock = FrozenClock(T0)

    gate, _, ledger = build(clock)
    gate.decide("k-4417", "s1")
    gate.confirm("tok-1", "esp-9f31")
    line("keyed on (case, step), replay", gate.decide("k-4417", "s1"))

    wrong = DeliveryKeyedLedger()
    wrong.bind_case_to_contact(CASE.case_id, CONTACT.contact_id)
    bad = SequenceGate(
        clock=clock,
        cases=InMemoryCaseStore([CASE], [CONTACT]),
        sequences={SEQUENCE.sequence_id: SEQUENCE},
        signals=InMemorySignalStore(),
        ledger=wrong,
        audit=InMemoryAuditLog(),
        token_factory=lambda: f"bad-{len(wrong.deliveries) + 1}",
    )
    first = bad.decide("k-4417", "s1")
    bad.confirm(first.send_token, "esp-9f31")
    second = bad.decide("k-4417", "s1")
    bad.confirm(second.send_token, "esp-1c08")
    line("keyed on provider id, replay", second)
    ids = sorted({r.provider_message_id for r in wrong.deliveries if r.provider_message_id})
    print(f"  messages on the wire           {len(ids)}  {ids}")


def scene_03_quiet_hours() -> None:
    print("\n03  THE CONTACT'S CLOCK")
    clock = FrozenClock(datetime(2026, 9, 14, 18, 42, tzinfo=UTC))
    gate, _, _ = build(clock)
    verdict = gate.decide("k-4417", "s1")
    print(f"  server clock                   {clock.now().strftime('%H:%M')} UTC")
    print(f"  contact clock                  {verdict.detail.split('is ')[-1]}")
    line("decide", verdict)


if __name__ == "__main__":
    print("sequence-gate, the four scenes")
    scene_00_setup()
    scene_01_stop_before_send()
    scene_02_replay()
    scene_03_quiet_hours()
    print()
