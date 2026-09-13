"""Domain model for sequence-gate.

Everything here is a value object. The engine reads these, never mutates them,
and returns a Verdict that carries the evidence it used.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Channel(StrEnum):
    """Delivery channels a step can use. The gate never talks to any of them."""

    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    VOICE = "voice"
    LETTER = "letter"
    PUSH = "push"


class SignalType(StrEnum):
    """External facts that arrive about a case or a contact.

    REPLIED, PAID, DISPUTED and CLOSED are case-scoped: they end one sequence.
    OPTED_OUT is contact-scoped and outlives every sequence the contact is in.
    BOUNCED is contact-and-channel scoped: the address is bad, other channels live on.
    """

    REPLIED = "replied"
    PAID = "paid"
    DISPUTED = "disputed"
    CLOSED = "closed"
    OPTED_OUT = "opted_out"
    BOUNCED = "bounced"


#: Signals that terminate the sequence they belong to.
STOPPING_SIGNALS: frozenset[SignalType] = frozenset(
    {
        SignalType.REPLIED,
        SignalType.PAID,
        SignalType.DISPUTED,
        SignalType.CLOSED,
    }
)


class VerdictKind(StrEnum):
    SEND = "send"
    SKIP = "skip"
    HOLD = "hold"
    STOP = "stop"
    ESCALATE = "escalate"


class Reason(StrEnum):
    """Why the engine decided what it decided.

    Every verdict carries one. The strings are stable: callers route on them and
    they end up in the audit log, so renaming one is a breaking change.
    """

    # SEND
    CLEAR = "clear"

    # SKIP
    NO_ADDRESS_FOR_CHANNEL = "no_address_for_channel"

    # HOLD
    DELAY_NOT_ELAPSED = "delay_not_elapsed"
    QUIET_HOURS = "quiet_hours"
    FREQUENCY_CAP = "frequency_cap"
    STATE_UNREADABLE = "state_unreadable"
    SIGNALS_STALE = "signals_stale"
    AWAITING_RESPONSE = "awaiting_response"
    TOKEN_OUTSTANDING = "token_outstanding"

    # STOP
    STOPPING_SIGNAL = "stopping_signal"
    OPTED_OUT = "opted_out"
    SEQUENCE_EXHAUSTED = "sequence_exhausted"

    # ESCALATE
    NO_RESPONSE_AT_RUNG = "no_response_at_rung"

    # idempotency
    ALREADY_SENT = "already_sent"


class Signal(BaseModel):
    """A timestamped external fact. Immutable once recorded."""

    model_config = ConfigDict(frozen=True)

    type: SignalType
    at: datetime
    source: str = Field(min_length=1, description="who reported it, for the audit trail")
    case_id: str | None = None
    contact_id: str | None = None
    channel: Channel | None = None

    @model_validator(mode="after")
    def _scope_is_addressable(self) -> Signal:
        if self.case_id is None and self.contact_id is None:
            raise ValueError("a signal needs a case_id or a contact_id")
        if self.type is SignalType.OPTED_OUT and self.contact_id is None:
            raise ValueError("opt-out is contact-scoped and needs a contact_id")
        if self.type is SignalType.BOUNCED and self.channel is None:
            raise ValueError("a bounce needs the channel that bounced")
        return self

    @field_validator("at")
    @classmethod
    def _must_be_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("signal timestamps must be timezone aware")
        return v


class QuietHours(BaseModel):
    """A daily window during which nothing may be sent, in the contact's own zone.

    A window that crosses midnight is normal and supported: start 21:00, end 08:00
    means quiet from nine at night until eight the next morning.
    """

    model_config = ConfigDict(frozen=True)

    start_hour: int = Field(ge=0, le=23)
    start_minute: int = Field(default=0, ge=0, le=59)
    end_hour: int = Field(ge=0, le=23)
    end_minute: int = Field(default=0, ge=0, le=59)

    @model_validator(mode="after")
    def _not_empty(self) -> QuietHours:
        if (self.start_hour, self.start_minute) == (self.end_hour, self.end_minute):
            raise ValueError(
                "start and end are identical, which is either a whole day or nothing; "
                "say which one explicitly"
            )
        return self


class Contact(BaseModel):
    """Who is being contacted, and the rules that follow them everywhere.

    The timezone lives here and nowhere else. The engine never reads a server clock
    to decide whether a local hour is acceptable.
    """

    model_config = ConfigDict(frozen=True)

    contact_id: str = Field(min_length=1)
    timezone: str = Field(description="IANA zone name, e.g. Asia/Kolkata")
    addresses: dict[Channel, str] = Field(default_factory=dict)
    quiet_hours: QuietHours | None = None

    @field_validator("timezone")
    @classmethod
    def _zone_must_resolve(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {v!r}") from exc
        return v

    def can_reach_on(self, channel: Channel) -> bool:
        return bool(self.addresses.get(channel))


class EscalationRung(BaseModel):
    """One step of the ladder taken when nobody answers at the current level."""

    model_config = ConfigDict(frozen=True)

    target: str = Field(min_length=1, description="contact_id, queue name, or human desk")
    is_human: bool = False
    wait: timedelta = Field(description="how long to wait at this rung before moving on")

    @field_validator("wait")
    @classmethod
    def _wait_is_positive(cls, v: timedelta) -> timedelta:
        if v <= timedelta(0):
            raise ValueError("an escalation rung must wait a positive amount of time")
        return v


class EscalationLadder(BaseModel):
    """Ordered rungs. The last one must be a human, and that is checked at load time.

    Rule six of the README: a ladder that can run out of rungs without reaching a
    person is a machine that abandons the case in silence. Rejecting it here, when
    the config is read, is the difference between a config error and a 3am incident.
    """

    model_config = ConfigDict(frozen=True)

    rungs: tuple[EscalationRung, ...]

    @model_validator(mode="after")
    def _terminates_in_a_human(self) -> EscalationLadder:
        if not self.rungs:
            raise ValueError("an escalation ladder needs at least one rung")
        if not self.rungs[-1].is_human:
            raise ValueError(
                "the last rung of an escalation ladder must be a human handoff; "
                f"got target {self.rungs[-1].target!r} with is_human=False"
            )
        return self

    @property
    def max_depth(self) -> int:
        return len(self.rungs)


class Step(BaseModel):
    model_config = ConfigDict(frozen=True)

    step_id: str = Field(min_length=1)
    channel: Channel
    delay_after_previous: timedelta = Field(default=timedelta(0))
    content_ref: str = Field(min_length=1, description="template id; the gate never renders it")
    escalation: EscalationLadder | None = None

    @field_validator("delay_after_previous")
    @classmethod
    def _delay_not_negative(cls, v: timedelta) -> timedelta:
        if v < timedelta(0):
            raise ValueError("a step cannot be scheduled before the one before it")
        return v


class FrequencyCap(BaseModel):
    """At most `limit` sends on `channel` per contact within `window`.

    Counted across every sequence the contact is in, which is the whole point:
    two campaigns that each respect their own cap can still add up to harassment.
    """

    model_config = ConfigDict(frozen=True)

    channel: Channel
    limit: int = Field(ge=1)
    window: timedelta

    @field_validator("window")
    @classmethod
    def _window_is_positive(cls, v: timedelta) -> timedelta:
        if v <= timedelta(0):
            raise ValueError("a frequency cap needs a positive window")
        return v


class Sequence(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence_id: str = Field(min_length=1)
    steps: tuple[Step, ...]
    caps: tuple[FrequencyCap, ...] = ()
    version: str = "1"

    @model_validator(mode="after")
    def _steps_are_usable(self) -> Sequence:
        if not self.steps:
            raise ValueError("a sequence needs at least one step")
        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                raise ValueError(f"duplicate step_id {step.step_id!r} in {self.sequence_id!r}")
            seen.add(step.step_id)
        return self

    def step(self, step_id: str) -> Step | None:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None

    def index_of(self, step_id: str) -> int | None:
        for i, s in enumerate(self.steps):
            if s.step_id == step_id:
                return i
        return None


class Case(BaseModel):
    """One contact moving through one sequence."""

    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1)
    sequence_id: str = Field(min_length=1)
    contact_id: str = Field(min_length=1)
    started_at: datetime
    rung: int = Field(default=0, ge=0, description="current position on the escalation ladder")

    @field_validator("started_at")
    @classmethod
    def _must_be_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("started_at must be timezone aware")
        return v


class SendRecord(BaseModel):
    """Proof that a step was decided, and later that it actually went out.

    Keyed on (case_id, step_id). Never on a provider message id: a provider that
    retries a webhook hands you a fresh id for the same intent, and keying on it is
    how the same person gets the same message twice.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    step_id: str
    channel: Channel
    token: str
    decided_at: datetime
    confirmed_at: datetime | None = None
    provider_message_id: str | None = None
    status: Literal["pending", "sent", "failed"] = "pending"

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None and self.status == "sent"

    def is_expired(self, now: datetime, ttl: timedelta) -> bool:
        """A decided-but-unconfirmed token goes stale so the step can be retried.

        Without this, a provider outage between the decision and the send freezes the
        case forever; with it, and only after the ttl, the step is decidable again.
        """
        if self.is_confirmed or self.status == "failed":
            return False
        return now - self.decided_at >= ttl


class Evidence(BaseModel):
    """What the engine actually read, with timestamps, so a verdict can be re-argued."""

    model_config = ConfigDict(frozen=True)

    signals_read: tuple[Signal, ...] = ()
    last_send_at: datetime | None = None
    sends_in_window: int | None = None
    local_time: str | None = None
    notes: tuple[str, ...] = ()


class Verdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: VerdictKind
    reason: Reason
    detail: str = ""
    send_token: str | None = None
    retry_after: datetime | None = None
    next_step_id: str | None = None
    escalate_to: str | None = None
    evidence: Evidence = Evidence()
    config_version: str = "1"

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> Verdict:
        if self.kind is VerdictKind.SEND and not self.send_token:
            raise ValueError("a SEND verdict must carry a send_token")
        if self.kind is VerdictKind.HOLD and self.retry_after is None:
            raise ValueError("a HOLD verdict must say when to try again")
        if self.kind is VerdictKind.ESCALATE and not self.escalate_to:
            raise ValueError("an ESCALATE verdict must name the next target")
        return self
