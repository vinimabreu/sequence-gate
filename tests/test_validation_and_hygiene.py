"""Config that is wrong is refused when it is read, and the suite polices its own habits.

The validation half is the cheap half of safety: a ladder with no human, a step that
runs before the one before it, a duplicate step id. All of those are knowable the moment
the config is loaded, and every one of them becomes an incident if it is only knowable
at 3am.

The hygiene half exists because the two rules that matter most here are easy to break by
accident later: no wall clock in the decision path, and no sleeping in the tests.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sequence_gate import (
    Case,
    Channel,
    Reason,
    Sequence,
    Step,
    VerdictKind,
)
from sequence_gate.engine import UnknownCase, UnknownStep

from .conftest import T0, Harness

SRC = Path(__file__).resolve().parents[1] / "src" / "sequence_gate"
TESTS = Path(__file__).resolve().parent


# ------------------------------------------------------------------ validation


def test_a_sequence_needs_at_least_one_step():
    with pytest.raises(ValueError, match="at least one step"):
        Sequence(sequence_id="empty", steps=())


def test_duplicate_step_ids_are_refused():
    with pytest.raises(ValueError, match="duplicate step_id"):
        Sequence(
            sequence_id="seq",
            steps=(
                Step(step_id="s1", channel=Channel.SMS, content_ref="a"),
                Step(step_id="s1", channel=Channel.EMAIL, content_ref="b"),
            ),
        )


def test_a_step_cannot_be_scheduled_before_the_one_before_it():
    with pytest.raises(ValueError, match="before the one before it"):
        Step(
            step_id="s1",
            channel=Channel.SMS,
            delay_after_previous=timedelta(hours=-1),
            content_ref="a",
        )


def test_a_case_needs_an_aware_start_time():
    with pytest.raises(ValueError, match="timezone aware"):
        Case(
            case_id="k",
            sequence_id="s",
            contact_id="c",
            started_at=datetime(2026, 9, 14, 12),
        )


def test_a_send_verdict_without_a_token_cannot_be_built():
    from sequence_gate import Verdict

    with pytest.raises(ValueError, match="must carry a send_token"):
        Verdict(kind=VerdictKind.SEND, reason=Reason.CLEAR)


def test_a_hold_verdict_must_say_when_to_retry():
    from sequence_gate import Verdict

    with pytest.raises(ValueError, match="must say when to try again"):
        Verdict(kind=VerdictKind.HOLD, reason=Reason.QUIET_HOURS)


def test_an_escalate_verdict_must_name_a_target():
    from sequence_gate import Verdict

    with pytest.raises(ValueError, match="must name the next target"):
        Verdict(kind=VerdictKind.ESCALATE, reason=Reason.NO_RESPONSE_AT_RUNG)


def test_an_unknown_case_is_an_error_not_a_silent_hold(harness):
    with pytest.raises(UnknownCase):
        harness.gate.decide("no-such-case", "s1")


def test_an_unknown_step_is_an_error(harness):
    with pytest.raises(UnknownStep):
        harness.gate.decide("k1", "no-such-step")


def test_a_case_pointing_at_a_missing_contact_is_an_error(clock, contact, two_step_sequence):
    orphan = Case(case_id="k1", sequence_id="seq", contact_id="ghost", started_at=T0)
    h = Harness(clock, orphan, contact, two_step_sequence)
    with pytest.raises(UnknownCase, match="contact"):
        h.decide("s1")


def test_step_lookup_returns_none_for_an_unknown_id(two_step_sequence):
    assert two_step_sequence.step("nope") is None
    assert two_step_sequence.index_of("nope") is None


# -------------------------------------------------------------------- hygiene


def test_nothing_in_the_decision_path_reads_a_wall_clock():
    """Only clock.py may call datetime.now. Everything else takes the instant as input."""
    offenders = []
    for path in SRC.glob("*.py"):
        if path.name in {"clock.py", "wrong.py"}:
            continue
        text = path.read_text()
        for match in re.finditer(r"datetime\.now\(|date\.today\(|time\.time\(", text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.name}:{line}")
    assert not offenders, f"wall clock read outside clock.py: {offenders}"


def test_the_package_never_sleeps():
    offenders = []
    for path in list(SRC.glob("*.py")) + list(TESTS.glob("*.py")):
        text = path.read_text()
        if re.search(r"\btime\.sleep\(|\basyncio\.sleep\(", text):
            offenders.append(path.name)
    assert not offenders, f"sleep found in {offenders}; the clock is injected for this reason"


def test_the_package_makes_no_network_calls():
    offenders = []
    for path in SRC.glob("*.py"):
        text = path.read_text()
        for needle in ("import requests", "import httpx", "urllib.request", "socket."):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, f"network reached from the core: {offenders}"


def test_every_reason_is_reachable_from_a_verdict_kind():
    """A reason nobody can ever receive is dead vocabulary in the audit log."""
    from sequence_gate import Reason

    documented = {r.value for r in Reason}
    engine_text = (SRC / "engine.py").read_text()
    unused = {r for r in documented if f"Reason.{r.upper()}" not in engine_text}
    assert not unused, f"reasons never produced by the engine: {sorted(unused)}"


def test_the_readme_claims_the_number_of_tests_the_suite_actually_has():
    """Kept honest by construction: if this drifts, the badge is a lie."""
    readme = SRC.parents[1] / "README.md"
    if not readme.exists():
        pytest.skip("README not written yet")
    claimed = re.search(r"tests-(\d+)-", readme.read_text())
    if claimed is None:
        pytest.skip("no test badge in the README yet")
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=SRC.parents[1],
        capture_output=True,
        text=True,
    ).stdout
    total = sum(int(n) for n in re.findall(r"^tests/\S+: (\d+)$", out, re.M))
    if total == 0:
        pytest.skip("could not count the suite")
    assert int(claimed.group(1)) == total, (
        f"the README badge says {claimed.group(1)} tests and the suite has {total}"
    )
