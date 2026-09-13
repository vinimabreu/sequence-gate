"""sequence-gate: decide whether the next step of a follow-up sequence may fire."""

from .clock import Clock, FrozenClock, SystemClock
from .engine import GateConfig, SequenceGate, UnknownCase, UnknownStep
from .models import (
    Case,
    Channel,
    Contact,
    EscalationLadder,
    EscalationRung,
    Evidence,
    FrequencyCap,
    QuietHours,
    Reason,
    SendRecord,
    Sequence,
    Signal,
    SignalType,
    Step,
    Verdict,
    VerdictKind,
)
from .stores import (
    InMemoryAuditLog,
    InMemoryCaseStore,
    InMemorySendLedger,
    InMemorySignalStore,
    StoreUnavailable,
)

__all__ = [
    "Case", "Channel", "Clock", "Contact", "EscalationLadder", "EscalationRung",
    "Evidence", "FrequencyCap", "FrozenClock", "GateConfig", "InMemoryAuditLog",
    "InMemoryCaseStore", "InMemorySendLedger", "InMemorySignalStore", "QuietHours",
    "Reason", "SendRecord", "Sequence", "SequenceGate", "Signal", "SignalType",
    "Step", "StoreUnavailable", "SystemClock", "UnknownCase", "UnknownStep",
    "Verdict", "VerdictKind",
]
__version__ = "0.1.0"
