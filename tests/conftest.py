"""Shared fixtures.

Every test here runs offline, with a clock that only moves when a test moves it.
There is no sleep anywhere in this suite, and a lint test asserts that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sequence_gate import (
    Case,
    Channel,
    Contact,
    FrozenClock,
    GateConfig,
    InMemoryAuditLog,
    InMemoryCaseStore,
    InMemorySendLedger,
    InMemorySignalStore,
    QuietHours,
    Sequence,
    SequenceGate,
    Step,
)

#: A Monday, midday UTC. Far from any DST boundary in the zones used below.
T0 = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(T0)


@pytest.fixture
def contact() -> Contact:
    return Contact(
        contact_id="c1",
        timezone="UTC",
        addresses={
            Channel.EMAIL: "person@example.test",
            Channel.SMS: "+10000000000",
            Channel.WHATSAPP: "+10000000000",
        },
    )


@pytest.fixture
def two_step_sequence() -> Sequence:
    return Sequence(
        sequence_id="seq",
        steps=(
            Step(step_id="s1", channel=Channel.EMAIL, content_ref="tpl.one"),
            Step(
                step_id="s2",
                channel=Channel.SMS,
                delay_after_previous=timedelta(days=2),
                content_ref="tpl.two",
            ),
        ),
    )


@pytest.fixture
def case() -> Case:
    return Case(case_id="k1", sequence_id="seq", contact_id="c1", started_at=T0)


class Harness:
    """Everything wired, with the pieces exposed so a test can poke one of them."""

    def __init__(
        self,
        clock: FrozenClock,
        case: Case,
        contact: Contact,
        sequence: Sequence,
        config: GateConfig | None = None,
    ) -> None:
        self.clock = clock
        self.case = case
        self.contact = contact
        self.sequence = sequence
        self.cases = InMemoryCaseStore([case], [contact])
        self.signals = InMemorySignalStore()
        self.ledger = InMemorySendLedger()
        self.ledger.bind_case_to_contact(case.case_id, contact.contact_id)
        self.audit = InMemoryAuditLog()
        self.gate = SequenceGate(
            clock=clock,
            cases=self.cases,
            sequences={sequence.sequence_id: sequence},
            signals=self.signals,
            ledger=self.ledger,
            audit=self.audit,
            config=config,
            token_factory=self._token,
        )
        self._counter = 0

    def _token(self) -> str:
        self._counter += 1
        return f"tok-{self._counter}"

    def send_and_confirm(self, step_id: str, provider_id: str = "prov-1") -> str:
        verdict = self.gate.decide(self.case.case_id, step_id)
        assert verdict.send_token is not None, f"expected a send, got {verdict.reason}"
        self.gate.confirm(verdict.send_token, provider_id)
        return verdict.send_token

    def decide(self, step_id: str):
        return self.gate.decide(self.case.case_id, step_id)


@pytest.fixture
def harness(clock, case, contact, two_step_sequence) -> Harness:
    return Harness(clock, case, contact, two_step_sequence)


@pytest.fixture
def quiet_contact() -> Contact:
    """A contact in India with a normal nine-to-eight quiet window."""
    return Contact(
        contact_id="c-in",
        timezone="Asia/Kolkata",
        addresses={Channel.SMS: "+915550000000", Channel.EMAIL: "in@example.test"},
        quiet_hours=QuietHours(start_hour=21, end_hour=8),
    )
