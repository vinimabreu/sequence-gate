"""The HTTP shell, checked for the mapping and nothing more.

The decisions are tested in the other files without a server. What can still go wrong
here is the translation: a verdict that loses its reason on the way out, a 404 that
should be a 200, a signal that arrives and is quietly dropped.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from sequence_gate.service import build_app  # noqa: E402

from .conftest import T0  # noqa: E402


@pytest.fixture
def client(harness) -> TestClient:
    return TestClient(build_app(harness.gate))


def test_decide_without_a_step_walks_the_sequence(client):
    body = client.post("/decide", json={"case_id": "k1"}).json()
    assert body["kind"] == "send"
    assert body["next_step_id"] == "s1"
    assert body["send_token"]


def test_decide_with_a_step_answers_about_that_step(client):
    body = client.post("/decide", json={"case_id": "k1", "step_id": "s2"}).json()
    assert body["kind"] == "hold"
    assert body["reason"] == "delay_not_elapsed"


def test_an_unknown_case_is_a_404(client):
    assert client.post("/decide", json={"case_id": "ghost"}).status_code == 404


def test_a_signal_posted_over_http_stops_the_sequence(client, harness):
    accepted = client.post(
        "/signals",
        json={
            "type": "replied",
            "at": T0.isoformat(),
            "source": "whatsapp",
            "case_id": "k1",
        },
    )
    assert accepted.status_code == 202

    body = client.post("/decide", json={"case_id": "k1"}).json()
    assert body["kind"] == "stop"
    assert body["reason"] == "stopping_signal"


def test_a_malformed_signal_is_rejected(client):
    bad = client.post("/signals", json={"type": "replied", "source": "x"})
    assert bad.status_code == 422


def test_confirming_closes_the_step_over_http(client):
    decided = client.post("/decide", json={"case_id": "k1"}).json()
    confirmed = client.post(
        "/sends",
        json={"send_token": decided["send_token"], "provider_message_id": "SM1"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "sent"

    again = client.post("/decide", json={"case_id": "k1", "step_id": "s1"}).json()
    assert again["reason"] == "already_sent"


def test_confirming_an_unknown_token_is_a_404(client):
    response = client.post(
        "/sends", json={"send_token": "nope", "provider_message_id": "SM1"}
    )
    assert response.status_code == 404


def test_the_audit_route_returns_the_trail(client):
    client.post("/decide", json={"case_id": "k1"})
    rows = client.get("/audit/k1").json()
    assert rows and rows[0]["reason"] == "clear"


def test_the_verdict_keeps_its_evidence_through_serialisation(client):
    client.post(
        "/signals",
        json={"type": "paid", "at": T0.isoformat(), "source": "stripe", "case_id": "k1"},
    )
    body = client.post("/decide", json={"case_id": "k1"}).json()
    assert body["evidence"]["signals_read"][0]["source"] == "stripe"


def test_healthz_answers(client):
    assert client.get("/healthz").json() == {"status": "ok"}
