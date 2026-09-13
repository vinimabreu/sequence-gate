"""A thin HTTP shell over the gate, so n8n, Make and Zapier can call it.

Thin is the design. Every decision lives in `engine.py` and is tested without a server;
this module maps JSON onto that and back. If you are reading this file to understand how
the gate decides anything, you are in the wrong file.

Install with the extra: `pip install sequence-gate[service]`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .engine import SequenceGate, UnknownCase, UnknownStep
from .models import Channel, Signal, SignalType, Verdict


class DecideRequest(BaseModel):
    case_id: str = Field(min_length=1)
    step_id: str | None = Field(
        default=None,
        description="omit to let the gate walk the sequence and pick the next step itself",
    )


class SignalRequest(BaseModel):
    type: SignalType
    at: datetime
    source: str = Field(min_length=1)
    case_id: str | None = None
    contact_id: str | None = None
    channel: Channel | None = None


class ConfirmRequest(BaseModel):
    send_token: str = Field(min_length=1)
    provider_message_id: str = Field(min_length=1)
    status: str = "sent"


def build_app(gate: SequenceGate, *, signal_sink: Any | None = None) -> Any:
    """Return a FastAPI app bound to an already-wired gate.

    The gate is passed in rather than constructed here on purpose: the stores, the clock
    and the sequence registry are deployment decisions, and a web module that picks them
    for you is a web module you end up fighting.
    """
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:  # pragma: no cover - exercised by the extras install
        raise RuntimeError(
            "the HTTP shell needs the service extra: "
            "pip install 'sequence-gate[service]'"
        ) from exc

    app = FastAPI(
        title="sequence-gate",
        version="0.1.0",
        summary="Decide whether the next step of a follow-up sequence may fire.",
    )
    sink = signal_sink if signal_sink is not None else gate.signals

    @app.post("/decide", response_model=Verdict)
    def decide(request: DecideRequest) -> Verdict:
        try:
            if request.step_id is None:
                return gate.advance(request.case_id)
            return gate.decide(request.case_id, request.step_id)
        except (UnknownCase, UnknownStep) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/signals", status_code=202)
    def record_signal(request: SignalRequest) -> dict[str, str]:
        signal = Signal(**request.model_dump())
        add = getattr(sink, "add", None)
        if add is None:
            raise HTTPException(
                status_code=501,
                detail="this deployment has a read-only signal store",
            )
        add(signal)
        return {"status": "recorded"}

    @app.post("/sends", status_code=200)
    def confirm(request: ConfirmRequest) -> dict[str, str]:
        try:
            record = gate.confirm(
                request.send_token, request.provider_message_id, request.status
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown send token") from exc
        return {
            "status": record.status,
            "case_id": record.case_id,
            "step_id": record.step_id,
        }

    @app.get("/audit/{case_id}")
    def audit(case_id: str) -> list[dict[str, Any]]:
        return gate.audit.for_case(case_id)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
