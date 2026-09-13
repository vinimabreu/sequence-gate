"""The gate.

One public call: `SequenceGate.decide(case_id, step_id)`. It answers whether the next
step of a follow-up sequence may fire right now, and it answers with a reason and the
evidence it read.

The order of the checks below is the design. Stopping conditions are evaluated before
anything else, every time, because the bug this package exists to prevent is a machine
that sends step three to someone who already replied to step two.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from .clock import Clock
from .models import (
    STOPPING_SIGNALS,
    Case,
    Channel,
    Evidence,
    Reason,
    SendRecord,
    Sequence,
    Signal,
    SignalType,
    Step,
    Verdict,
    VerdictKind,
)
from .quiet_hours import is_quiet, local_time_of, next_open
from .stores import (
    AuditLog,
    CaseStore,
    SendLedger,
    SignalStore,
    StoreUnavailable,
)


class GateConfig:
    """Knobs that govern the gate itself rather than any one sequence."""

    def __init__(
        self,
        *,
        token_ttl: timedelta = timedelta(minutes=15),
        max_signal_age: timedelta | None = None,
        version: str = "1",
    ) -> None:
        if token_ttl <= timedelta(0):
            raise ValueError("token_ttl must be positive")
        if max_signal_age is not None and max_signal_age <= timedelta(0):
            raise ValueError("max_signal_age must be positive when set")
        self.token_ttl = token_ttl
        self.max_signal_age = max_signal_age
        self.version = version


class UnknownCase(LookupError):
    pass


class UnknownStep(LookupError):
    pass


def _default_token() -> str:
    return uuid.uuid4().hex


class SequenceGate:
    def __init__(
        self,
        *,
        clock: Clock,
        cases: CaseStore,
        sequences: dict[str, Sequence],
        signals: SignalStore,
        ledger: SendLedger,
        audit: AuditLog,
        config: GateConfig | None = None,
        token_factory: Callable[[], str] = _default_token,
    ) -> None:
        self.clock = clock
        self.cases = cases
        self.sequences = dict(sequences)
        self.signals = signals
        self.ledger = ledger
        self.audit = audit
        self.config = config or GateConfig()
        self.token_factory = token_factory

    # ------------------------------------------------------------------ public

    def decide(self, case_id: str, step_id: str) -> Verdict:
        now = self.clock.now()
        verdict = self._decide(case_id, step_id, now)
        self.audit.append(case_id, step_id, verdict, now)
        return verdict

    def advance(self, case_id: str) -> Verdict:
        """Ask about the case rather than about a step.

        This is what an orchestrator actually wants: it holds a case id and a loop, not
        a memory of which step it reached. Steps are walked in order and the first one
        that produces an actionable verdict is returned. When every step has been sent
        and nothing is still waiting on a ladder, the sequence is over and says so.
        """
        case = self.cases.get(case_id)
        if case is None:
            raise UnknownCase(case_id)
        sequence = self.sequences.get(case.sequence_id)
        if sequence is None:
            raise UnknownCase(f"sequence {case.sequence_id} of case {case_id}")

        for step in sequence.steps:
            verdict = self.decide(case_id, step.step_id)
            if verdict.kind is VerdictKind.SKIP and verdict.reason in {
                Reason.ALREADY_SENT,
                Reason.NO_ADDRESS_FOR_CHANNEL,
            }:
                continue  # this step is settled; look at the next one
            return verdict.model_copy(update={"next_step_id": step.step_id})

        exhausted = self._stop(
            Reason.SEQUENCE_EXHAUSTED,
            detail=f"every step of {sequence.sequence_id} is settled",
        )
        self.audit.append(case_id, "", exhausted, self.clock.now())
        return exhausted

    def confirm(
        self, token: str, provider_message_id: str, status: str = "sent"
    ) -> SendRecord:
        """Second half of the two-phase send: the caller reports what happened."""
        return self.ledger.confirm(
            token, provider_message_id, self.clock.now(), status
        )

    # ----------------------------------------------------------------- private

    def _decide(self, case_id: str, step_id: str, now: datetime) -> Verdict:
        case = self.cases.get(case_id)
        if case is None:
            raise UnknownCase(case_id)

        sequence = self.sequences.get(case.sequence_id)
        if sequence is None:
            raise UnknownCase(f"sequence {case.sequence_id} of case {case_id}")

        step = sequence.step(step_id)
        if step is None:
            raise UnknownStep(f"{step_id} is not a step of {sequence.sequence_id}")

        contact = self.cases.contact(case.contact_id)
        if contact is None:
            raise UnknownCase(f"contact {case.contact_id} of case {case_id}")

        # Rule 1. Read the world first, and refuse to act on a world you cannot read.
        try:
            case_signals = self.signals.for_case(case_id)
            contact_signals = self.signals.for_contact(case.contact_id)
        except StoreUnavailable as exc:
            return self._hold(
                Reason.STATE_UNREADABLE,
                retry_after=now + timedelta(minutes=1),
                detail=str(exc),
                evidence=Evidence(notes=("signal store raised; defaulting to no send",)),
            )

        seen = tuple(case_signals) + tuple(contact_signals)

        # Rule 7. Consent outlives the sequence.
        opt_out = self._find_opt_out(contact_signals, step.channel)
        if opt_out is not None:
            return self._stop(
                Reason.OPTED_OUT,
                detail=f"contact opted out at {opt_out.at.isoformat()}",
                evidence=Evidence(signals_read=(opt_out,)),
            )

        # Rule 1, continued. Anything that ends the case, ends it here.
        stopper = self._find_stopping_signal(case_signals)
        if stopper is not None:
            return self._stop(
                Reason.STOPPING_SIGNAL,
                detail=f"{stopper.type.value} at {stopper.at.isoformat()}",
                evidence=Evidence(signals_read=(stopper,)),
            )

        if self.config.max_signal_age is not None and case_signals:
            newest = max(s.at for s in case_signals)
            if now - newest > self.config.max_signal_age:
                return self._hold(
                    Reason.SIGNALS_STALE,
                    retry_after=now + timedelta(minutes=5),
                    detail=f"newest signal is from {newest.isoformat()}",
                    evidence=Evidence(signals_read=seen),
                )

        # Rule 2 and 3. Idempotency on (case, step), and the outstanding token.
        try:
            existing = self.ledger.get(case_id, step_id)
        except StoreUnavailable as exc:
            return self._hold(
                Reason.STATE_UNREADABLE,
                retry_after=now + timedelta(minutes=1),
                detail=str(exc),
            )

        if existing is not None:
            idempotent = self._idempotency_verdict(existing, now, case, step, seen)
            if idempotent is not None:
                return idempotent

        if not contact.can_reach_on(step.channel) or self._bounced_on(
            contact_signals, step.channel
        ):
            return self._skip(
                Reason.NO_ADDRESS_FOR_CHANNEL,
                detail=f"no usable {step.channel.value} address for {contact.contact_id}",
                next_step_id=self._step_after(sequence, step_id),
                evidence=Evidence(signals_read=seen),
            )

        due_at = self._due_at(case, sequence, step)
        if now < due_at:
            return self._hold(
                Reason.DELAY_NOT_ELAPSED,
                retry_after=due_at,
                detail=f"step is due at {due_at.isoformat()}",
                evidence=Evidence(signals_read=seen),
            )

        escalation = self._escalation_verdict(case, step, existing, now, seen)
        if escalation is not None:
            return escalation

        # Rules 4 and 5. Both are holds; whichever clears later wins the retry time.
        holds: list[tuple[datetime, Reason, str, Evidence]] = []

        if contact.quiet_hours is not None and is_quiet(
            now, contact.timezone, contact.quiet_hours
        ):
            opens = next_open(now, contact.timezone, contact.quiet_hours)
            holds.append(
                (
                    opens,
                    Reason.QUIET_HOURS,
                    f"local time is {local_time_of(now, contact.timezone).strftime('%H:%M')}"
                    f" in {contact.timezone}",
                    Evidence(
                        signals_read=seen,
                        local_time=local_time_of(now, contact.timezone).isoformat(),
                    ),
                )
            )

        cap_hold = self._cap_hold(case, sequence, step, now, seen)
        if cap_hold is not None:
            holds.append(cap_hold)

        if holds:
            opens, reason, detail, evidence = max(holds, key=lambda h: h[0])
            return self._hold(reason, retry_after=opens, detail=detail, evidence=evidence)

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
        return Verdict(
            kind=VerdictKind.SEND,
            reason=Reason.CLEAR,
            send_token=token,
            detail=f"{step.channel.value} via {step.content_ref}",
            evidence=Evidence(
                signals_read=seen,
                local_time=local_time_of(now, contact.timezone).isoformat(),
            ),
            config_version=self.config.version,
        )

    # --------------------------------------------------------------- helpers

    def _idempotency_verdict(
        self,
        existing: SendRecord,
        now: datetime,
        case: Case,
        step: Step,
        seen: tuple[Signal, ...],
    ) -> Verdict | None:
        if existing.is_confirmed:
            if step.escalation is None:
                return Verdict(
                    kind=VerdictKind.SKIP,
                    reason=Reason.ALREADY_SENT,
                    send_token=existing.token,
                    detail=(
                        f"already sent at {existing.confirmed_at.isoformat()}"
                        if existing.confirmed_at
                        else "already sent"
                    ),
                    evidence=Evidence(signals_read=seen, last_send_at=existing.confirmed_at),
                    config_version=self.config.version,
                )
            return None  # a step with a ladder keeps being asked about

        if existing.status == "failed":
            return None

        if not existing.is_expired(now, self.config.token_ttl):
            return self._hold(
                Reason.TOKEN_OUTSTANDING,
                retry_after=existing.decided_at + self.config.token_ttl,
                detail=f"token {existing.token} decided at {existing.decided_at.isoformat()}",
                evidence=Evidence(signals_read=seen),
            )
        return None  # expired: the step becomes decidable again

    def _escalation_verdict(
        self,
        case: Case,
        step: Step,
        existing: SendRecord | None,
        now: datetime,
        seen: tuple[Signal, ...],
    ) -> Verdict | None:
        ladder = step.escalation
        if ladder is None or existing is None or not existing.is_confirmed:
            return None

        rung_index = min(case.rung, ladder.max_depth - 1)
        rung = ladder.rungs[rung_index]
        waited_since = existing.confirmed_at or existing.decided_at
        if now - waited_since < rung.wait:
            return self._hold(
                Reason.AWAITING_RESPONSE,
                retry_after=waited_since + rung.wait,
                detail=f"waiting at rung {rung_index} ({rung.target})",
                evidence=Evidence(signals_read=seen, last_send_at=waited_since),
            )

        if rung_index + 1 >= ladder.max_depth:
            # Already parked with a person. The machine has nothing left to do.
            return self._hold(
                Reason.AWAITING_RESPONSE,
                retry_after=now + rung.wait,
                detail=f"held by {rung.target}, the terminal human rung",
                evidence=Evidence(signals_read=seen, last_send_at=waited_since),
            )

        nxt = ladder.rungs[rung_index + 1]
        return Verdict(
            kind=VerdictKind.ESCALATE,
            reason=Reason.NO_RESPONSE_AT_RUNG,
            escalate_to=nxt.target,
            detail=(
                f"no response in {rung.wait} at rung {rung_index}; "
                f"{'handing to a person' if nxt.is_human else 'moving to the next target'}"
            ),
            evidence=Evidence(signals_read=seen, last_send_at=waited_since),
            config_version=self.config.version,
        )

    def _cap_hold(
        self,
        case: Case,
        sequence: Sequence,
        step: Step,
        now: datetime,
        seen: tuple[Signal, ...],
    ) -> tuple[datetime, Reason, str, Evidence] | None:
        for cap in sequence.caps:
            if cap.channel is not step.channel:
                continue
            since = now - cap.window
            sends = self.ledger.sends_for_contact(case.contact_id, cap.channel, since)
            if len(sends) < cap.limit:
                continue
            oldest = min(s.decided_at for s in sends)
            return (
                oldest + cap.window,
                Reason.FREQUENCY_CAP,
                f"{len(sends)} {cap.channel.value} sends in the last {cap.window}, "
                f"cap is {cap.limit}",
                Evidence(signals_read=seen, sends_in_window=len(sends)),
            )
        return None

    def _due_at(self, case: Case, sequence: Sequence, step: Step) -> datetime:
        index = sequence.index_of(step.step_id)
        if index is None or index == 0:
            return case.started_at + step.delay_after_previous

        previous = sequence.steps[index - 1]
        record = self.ledger.get(case.case_id, previous.step_id)
        anchor = None
        if record is not None:
            anchor = record.confirmed_at or record.decided_at
        if anchor is None:
            anchor = case.started_at
        return anchor + step.delay_after_previous

    @staticmethod
    def _step_after(sequence: Sequence, step_id: str) -> str | None:
        index = sequence.index_of(step_id)
        if index is None or index + 1 >= len(sequence.steps):
            return None
        return sequence.steps[index + 1].step_id

    @staticmethod
    def _find_opt_out(signals: list[Signal], channel: Channel) -> Signal | None:
        for s in signals:
            if s.type is not SignalType.OPTED_OUT:
                continue
            if s.channel is None or s.channel is channel:
                return s
        return None

    @staticmethod
    def _find_stopping_signal(signals: list[Signal]) -> Signal | None:
        stoppers = [s for s in signals if s.type in STOPPING_SIGNALS]
        if not stoppers:
            return None
        return min(stoppers, key=lambda s: s.at)

    @staticmethod
    def _bounced_on(signals: list[Signal], channel: Channel) -> bool:
        return any(
            s.type is SignalType.BOUNCED and s.channel is channel for s in signals
        )

    # --------------------------------------------------------- verdict makers

    def _hold(
        self,
        reason: Reason,
        *,
        retry_after: datetime,
        detail: str = "",
        evidence: Evidence | None = None,
    ) -> Verdict:
        return Verdict(
            kind=VerdictKind.HOLD,
            reason=reason,
            retry_after=retry_after,
            detail=detail,
            evidence=evidence or Evidence(),
            config_version=self.config.version,
        )

    def _stop(
        self, reason: Reason, *, detail: str = "", evidence: Evidence | None = None
    ) -> Verdict:
        return Verdict(
            kind=VerdictKind.STOP,
            reason=reason,
            detail=detail,
            evidence=evidence or Evidence(),
            config_version=self.config.version,
        )

    def _skip(
        self,
        reason: Reason,
        *,
        detail: str = "",
        next_step_id: str | None = None,
        evidence: Evidence | None = None,
    ) -> Verdict:
        return Verdict(
            kind=VerdictKind.SKIP,
            reason=reason,
            detail=detail,
            next_step_id=next_step_id,
            evidence=evidence or Evidence(),
            config_version=self.config.version,
        )
