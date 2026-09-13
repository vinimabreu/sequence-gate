"""Storage seams.

Each one is a Protocol plus an in-memory implementation. The engine depends on the
Protocol, so a caller can back these with Postgres, Redis or a Google Sheet without
the decision logic knowing.

`StoreUnavailable` is the important type here. A store that cannot answer must raise
it rather than return an empty list: an empty list reads as "no stopping signals" and
that is precisely how a machine keeps texting someone who already asked it to stop.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from .models import Case, Channel, Contact, SendRecord, Signal, Verdict


class StoreUnavailable(RuntimeError):
    """Raised when a store cannot answer. Never swallowed inside the engine."""


@runtime_checkable
class SignalStore(Protocol):
    def for_case(self, case_id: str) -> list[Signal]: ...
    def for_contact(self, contact_id: str) -> list[Signal]: ...


@runtime_checkable
class SendLedger(Protocol):
    def get(self, case_id: str, step_id: str) -> SendRecord | None: ...
    def record(self, record: SendRecord) -> None: ...
    def confirm(
        self, token: str, provider_message_id: str, at: datetime, status: str
    ) -> SendRecord: ...
    def sends_for_contact(
        self, contact_id: str, channel: Channel, since: datetime
    ) -> list[SendRecord]: ...


@runtime_checkable
class AuditLog(Protocol):
    def append(self, case_id: str, step_id: str, verdict: Verdict, at: datetime) -> None: ...
    def for_case(self, case_id: str) -> list[dict[str, Any]]: ...


@runtime_checkable
class CaseStore(Protocol):
    def get(self, case_id: str) -> Case | None: ...
    def contact(self, contact_id: str) -> Contact | None: ...


class InMemorySignalStore:
    def __init__(self, signals: Iterable[Signal] = ()) -> None:
        self._signals: list[Signal] = list(signals)
        self.available = True

    def add(self, signal: Signal) -> None:
        self._signals.append(signal)

    def for_case(self, case_id: str) -> list[Signal]:
        if not self.available:
            raise StoreUnavailable(f"signal store unavailable for case {case_id}")
        return [s for s in self._signals if s.case_id == case_id]

    def for_contact(self, contact_id: str) -> list[Signal]:
        if not self.available:
            raise StoreUnavailable(f"signal store unavailable for contact {contact_id}")
        return [s for s in self._signals if s.contact_id == contact_id]


class InMemorySendLedger:
    """Keyed on (case_id, step_id). That key is the whole anti-duplicate story."""

    def __init__(self) -> None:
        self._by_step: dict[tuple[str, str], SendRecord] = {}
        self._by_token: dict[str, tuple[str, str]] = {}
        self._contact_of_case: dict[str, str] = {}
        self.available = True

    def bind_case_to_contact(self, case_id: str, contact_id: str) -> None:
        self._contact_of_case[case_id] = contact_id

    def get(self, case_id: str, step_id: str) -> SendRecord | None:
        if not self.available:
            raise StoreUnavailable(f"send ledger unavailable for case {case_id}")
        return self._by_step.get((case_id, step_id))

    def record(self, record: SendRecord) -> None:
        if not self.available:
            raise StoreUnavailable("send ledger unavailable")
        self._by_step[(record.case_id, record.step_id)] = record
        self._by_token[record.token] = (record.case_id, record.step_id)

    def confirm(
        self, token: str, provider_message_id: str, at: datetime, status: str = "sent"
    ) -> SendRecord:
        if not self.available:
            raise StoreUnavailable("send ledger unavailable")
        key = self._by_token.get(token)
        if key is None:
            raise KeyError(f"unknown send token {token!r}")
        existing = self._by_step[key]
        updated = existing.model_copy(
            update={
                "confirmed_at": at,
                "provider_message_id": provider_message_id,
                "status": status,
            }
        )
        self._by_step[key] = updated
        return updated

    def sends_for_contact(
        self, contact_id: str, channel: Channel, since: datetime
    ) -> list[SendRecord]:
        if not self.available:
            raise StoreUnavailable(f"send ledger unavailable for contact {contact_id}")
        out: list[SendRecord] = []
        for (case_id, _step), rec in self._by_step.items():
            if self._contact_of_case.get(case_id) != contact_id:
                continue
            if rec.channel is not channel:
                continue
            # Strictly after: at exactly `since` the send has aged out of the window.
            # With >= here, retry_after (oldest + window) lands on an instant that is
            # still capped, and a caller that obeys it gets held again forever.
            if rec.decided_at > since and rec.status != "failed":
                out.append(rec)
        return out


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def append(self, case_id: str, step_id: str, verdict: Verdict, at: datetime) -> None:
        self._rows.append(
            {
                "at": at,
                "case_id": case_id,
                "step_id": step_id,
                "kind": verdict.kind.value,
                "reason": verdict.reason.value,
                "detail": verdict.detail,
                "config_version": verdict.config_version,
                "evidence": verdict.evidence.model_dump(mode="json"),
            }
        )

    def for_case(self, case_id: str) -> list[dict[str, Any]]:
        return [r for r in self._rows if r["case_id"] == case_id]

    def __len__(self) -> int:
        return len(self._rows)


class InMemoryCaseStore:
    def __init__(
        self, cases: Iterable[Case] = (), contacts: Iterable[Contact] = ()
    ) -> None:
        self._cases = {c.case_id: c for c in cases}
        self._contacts = {c.contact_id: c for c in contacts}

    def put_case(self, case: Case) -> None:
        self._cases[case.case_id] = case

    def put_contact(self, contact: Contact) -> None:
        self._contacts[contact.contact_id] = contact

    def get(self, case_id: str) -> Case | None:
        return self._cases.get(case_id)

    def contact(self, contact_id: str) -> Contact | None:
        return self._contacts.get(contact_id)


def window_start(now: datetime, window: timedelta) -> datetime:
    return now - window
