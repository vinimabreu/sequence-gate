"""Three wrong implementations, kept on purpose.

Every one of these is a bug I have seen in a production follow-up pipeline, and every
one of them is invisible in a code review because the code reads as if it does the
right thing. Describing them in a README convinces nobody. So they live here, and
`tests/test_wrong_implementations.py` runs them and watches them fail.

Do not import these outside tests. They exist to be proven broken.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .engine import SequenceGate
from .models import Channel, QuietHours, SendRecord, Verdict, VerdictKind
from .quiet_hours import _minutes  # noqa: PLC2701  - deliberate: same helper, wrong clock


class PostSendStopCheck(SequenceGate):
    """Bug: evaluates stopping conditions after committing the send.

    The ordering looks harmless. "Decide, then check we should still be going" reads
    like belt and braces. What it actually does is write the send record and hand back
    a token, and only then notice that the contact replied twenty minutes ago.

    Proven by: test_post_send_stop_check_sends_to_someone_who_already_replied
    """

    def _decide(self, case_id: str, step_id: str, now: datetime) -> Verdict:
        case = self.cases.get(case_id)
        sequence = self.sequences[case.sequence_id]  # type: ignore[union-attr]
        step = sequence.step(step_id)
        assert step is not None

        token = self.token_factory()
        self.ledger.record(
            SendRecord(
                case_id=case_id,
                step_id=step_id,
                channel=step.channel,
                token=token,
                decided_at=now,
                status="pending",
            )
        )
        verdict = super()._decide(case_id, step_id, now)
        if verdict.kind is VerdictKind.STOP:
            return verdict
        return verdict


class DeliveryKeyedLedger:
    """Bug: dedupes on the provider's message id instead of (case_id, step_id).

    It is a natural mistake. The provider hands you a message id, the id is unique, so
    it feels like the right key. But the id identifies a *delivery attempt*, not the
    *intent to contact this person about this step*. A webhook redelivery, a retried
    HTTP call, or a workflow that resumes after a crash all produce a fresh id for the
    same intent, and the ledger reports no prior send.

    Proven by: test_delivery_keyed_ledger_double_sends_on_replay
    """

    def __init__(self) -> None:
        self._by_provider_id: dict[str, SendRecord] = {}
        self._by_token: dict[str, SendRecord] = {}
        self._contact_of_case: dict[str, str] = {}
        self.available = True

    def bind_case_to_contact(self, case_id: str, contact_id: str) -> None:
        self._contact_of_case[case_id] = contact_id

    def get(self, case_id: str, step_id: str) -> SendRecord | None:
        # There is no way to answer "was this step sent" from a provider id index.
        return None

    def record(self, record: SendRecord) -> None:
        self._by_token[record.token] = record

    def confirm(
        self, token: str, provider_message_id: str, at: datetime, status: str = "sent"
    ) -> SendRecord:
        existing = self._by_token[token]
        updated = existing.model_copy(
            update={
                "confirmed_at": at,
                "provider_message_id": provider_message_id,
                "status": status,
            }
        )
        self._by_token[token] = updated
        self._by_provider_id[provider_message_id] = updated
        return updated

    def sends_for_contact(
        self, contact_id: str, channel: Channel, since: datetime
    ) -> list[SendRecord]:
        return [
            r
            for r in self._by_token.values()
            if self._contact_of_case.get(r.case_id) == contact_id
            and r.channel is channel
            and r.decided_at > since
            and r.status != "failed"
        ]

    @property
    def deliveries(self) -> list[SendRecord]:
        return list(self._by_token.values())


def server_clock_is_quiet(
    now_utc: datetime, timezone: str, window: QuietHours, *, server_zone: str = "UTC"
) -> bool:
    """Bug: reads the window against the server's clock instead of the contact's.

    Every test passes in the office, because the office and the server agree. The
    failure ships the day a contact lives eight hours east: the code says 18:42 and
    sends, the phone on the nightstand says 03:12 and rings.

    Proven by: test_server_clock_quiet_hours_texts_at_three_in_the_morning
    """
    from zoneinfo import ZoneInfo

    local = now_utc.astimezone(ZoneInfo(server_zone))  # the bug is this one argument
    t = _minutes(local.hour, local.minute)
    start = _minutes(window.start_hour, window.start_minute)
    end = _minutes(window.end_hour, window.end_minute)
    if start < end:
        return start <= t < end
    return t >= start or t < end


def unbounded_ladder(rungs: Any) -> Any:
    """Bug: an escalation ladder with no human at the end.

    The real EscalationLadder refuses this at load time. Kept here only so a test can
    show what the refusal is protecting against: a case that walks off the end of the
    ladder and is never seen by a person again.

    Proven by: test_ladder_without_a_human_is_refused_at_load_time
    """
    return tuple(rungs)


__all__ = [
    "DeliveryKeyedLedger",
    "PostSendStopCheck",
    "server_clock_is_quiet",
    "unbounded_ladder",
]
