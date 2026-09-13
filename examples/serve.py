"""A runnable deployment, wired with the in-memory stores.

This is the smallest thing that boots: one contact in Kolkata with a nine-to-eight quiet
window, a two-step dunning sequence, and one open case. Swap the stores for real ones and
the decision logic is untouched.

    uvicorn examples.serve:app --reload
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sequence_gate import (
    Case,
    Channel,
    Contact,
    FrequencyCap,
    InMemoryAuditLog,
    InMemoryCaseStore,
    InMemorySendLedger,
    InMemorySignalStore,
    QuietHours,
    Sequence,
    SequenceGate,
    Step,
    SystemClock,
)
from sequence_gate.service import build_app

CONTACT = Contact(
    contact_id="c-1",
    timezone="Asia/Kolkata",
    addresses={Channel.SMS: "+915550000000", Channel.EMAIL: "person@example.test"},
    quiet_hours=QuietHours(start_hour=21, end_hour=8),
)

SEQUENCE = Sequence(
    sequence_id="dunning",
    steps=(
        Step(step_id="s1", channel=Channel.EMAIL, content_ref="tpl.first"),
        Step(
            step_id="s2",
            channel=Channel.SMS,
            delay_after_previous=timedelta(days=3),
            content_ref="tpl.second",
        ),
    ),
    caps=(FrequencyCap(channel=Channel.SMS, limit=1, window=timedelta(days=1)),),
)

CASE = Case(
    case_id="k-1",
    sequence_id="dunning",
    contact_id="c-1",
    started_at=datetime.now(timezone.utc),
)

_ledger = InMemorySendLedger()
_ledger.bind_case_to_contact(CASE.case_id, CONTACT.contact_id)

gate = SequenceGate(
    clock=SystemClock(),
    cases=InMemoryCaseStore([CASE], [CONTACT]),
    sequences={SEQUENCE.sequence_id: SEQUENCE},
    signals=InMemorySignalStore(),
    ledger=_ledger,
    audit=InMemoryAuditLog(),
)

app = build_app(gate)
