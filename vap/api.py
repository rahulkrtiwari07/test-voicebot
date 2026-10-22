"""HTTP API + static portal/dashboard.

  /api/*       portal & dashboard (agents CRUD, start calls, call log)
  /internal/*  worker <-> API (config fetch, events, final report), shared secret
  /demo/*      sample webhook tools and a webhook receiver for trying things out

Run: uv run uvicorn vap.api:app --port 8080
"""

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import func, select

from . import calls, campaigns, db, providers, settings, telephony, webhooks
from .db import AgentRow, CallRow, CampaignContactRow, CampaignRow, session_scope
from .schemas import (
    AgentConfig,
    CallEvent,
    CallReport,
    CreateCampaign,
    StartPhoneCall,
    StartWebCall,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("vap.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_engine()
    await calls.recover_on_startup()
    loops = [asyncio.create_task(calls.sweeper_loop()), asyncio.create_task(campaigns.scheduler_loop())]
    yield
    for task in loops:
        task.cancel()


app = FastAPI(title="Voice Agent Platform", lifespan=lifespan)


def _agent_out(row: AgentRow) -> dict:
    return {**row.config, "id": row.id, "created_at": row.created_at, "updated_at": row.updated_at}


def _require_internal(secret: str | None) -> None:
    if secret != settings.internal_secret():
        raise HTTPException(status_code=401, detail="invalid internal secret")


# --- agents -------------------------------------------------------------------


@app.get("/api/agents")
def list_agents():
    with session_scope() as s:
        rows = s.scalars(select(AgentRow).where(AgentRow.deleted_at.is_(None)).order_by(AgentRow.created_at.desc()))
        counts = dict(s.execute(select(CallRow.agent_id, func.count()).group_by(CallRow.agent_id)).all())
        return [{**_agent_out(r), "call_count": counts.get(r.id, 0)} for r in rows]


def _check_providers(cfg: AgentConfig) -> None:
    problems = providers.config_problems(cfg)
    if problems:
        raise HTTPException(status_code=422, detail="\n".join(problems))


@app.get("/api/voices")
def voices():
    return providers.voice_catalog()


@app.post("/api/agents", status_code=201)
def create_agent(cfg: AgentConfig):
    _check_providers(cfg)
    with session_scope() as s:
        row = AgentRow(name=cfg.name, config=cfg.model_dump(mode="json"))
        s.add(row)
        s.flush()
        return _agent_out(row)


def _get_agent(s, agent_id: str) -> AgentRow:
    row = s.get(AgentRow, agent_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="agent not found")
    return row


@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    with session_scope() as s:
        return _agent_out(_get_agent(s, agent_id))


@app.put("/api/agents/{agent_id}")
def update_agent(agent_id: str, cfg: AgentConfig):
    _check_providers(cfg)
    with session_scope() as s:
        row = _get_agent(s, agent_id)
        row.name = cfg.name
        row.config = cfg.model_dump(mode="json")
        row.updated_at = time.time()
        return _agent_out(row)


@app.delete("/api/agents/{agent_id}", status_code=204)
def delete_agent(agent_id: str):
    with session_scope() as s:
        _get_agent(s, agent_id).deleted_at = time.time()


@app.get("/api/templates")
def list_templates(request: Request):
    """Example agents (examples/*.json) for "New from template". $PLATFORM_URL
    in a template points its demo tool/webhook URLs at this server."""
    base = str(request.base_url).rstrip("/")
    out = []
    for path in sorted(EXAMPLES_DIR.glob("*.json")):
        config = json.loads(path.read_text(encoding="utf-8").replace("$PLATFORM_URL", base))
        out.append({"id": path.stem, "name": config["name"], "config": config})
    return out


# --- starting calls -------------------------------------------------------------


@app.post("/api/calls/phone", status_code=201)
async def start_phone_call(body: StartPhoneCall):
    try:
        number = telephony.normalize_phone_number(body.to_number)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    if not settings.sip_trunk_id():
        raise HTTPException(status_code=400, detail="SIP_TRUNK_ID is not configured — use a browser test call")
    try:
        call = await asyncio.to_thread(calls.create_call, body.agent_id, "phone", number, body.variables)
    except LookupError:
        raise HTTPException(status_code=404, detail="agent not found") from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    calls.spawn(calls.run_phone_call(call.id, call.room, number))
    return {"call_id": call.id, "room": call.room, "status": call.status}


@app.post("/api/calls/web", status_code=201)
async def start_web_call(body: StartWebCall):
    try:
        call = await asyncio.to_thread(calls.create_call, body.agent_id, "web", None, body.variables)
    except LookupError:
        raise HTTPException(status_code=404, detail="agent not found") from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    try:
        token = await telephony.start_web_call(call.room, call.id)
    except Exception as e:
        await asyncio.to_thread(calls.finalize_without_conversation, call.id, "failed", f"dispatch_error: {e}")
        raise HTTPException(status_code=502, detail=f"could not start agent: {e}") from None
    return {"call_id": call.id, "room": call.room, "token": token, "livekit_url": settings.env("LIVEKIT_URL")}


@app.post("/api/calls/{call_id}/hangup")
async def hangup(call_id: str):
    with session_scope() as s:
        call = s.get(CallRow, call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="call not found")
        room = call.room
    lk = telephony._lkapi()
    try:
        await telephony.delete_room(lk, room)
    finally:
        await lk.aclose()
    return {"ok": True}


# --- call log -------------------------------------------------------------------


def _call_summary(c: CallRow) -> dict:
    return {
        "id": c.id,
        "agent_id": c.agent_id,
        "agent_name": c.agent_name,
        "mode": c.mode,
        "to_number": c.to_number,
        "status": c.status,
        "end_reason": c.end_reason,
        "answered_by": c.answered_by,
        "campaign_contact_id": c.campaign_contact_id,
        "created_at": c.created_at,
        "answered_at": c.answered_at,
        "ended_at": c.ended_at,
        "duration_seconds": c.duration_seconds,
        "outcome": c.outcome,
        "postcall_status": c.postcall_status,
        "avg_v2v_ms": c.avg_v2v_ms,
        "max_v2v_ms": c.max_v2v_ms,
    }


@app.get("/api/calls")
def list_calls(agent_id: str | None = None, status: str | None = None, limit: int = 100):
    with session_scope() as s:
        q = select(CallRow).order_by(CallRow.created_at.desc()).limit(min(limit, 500))
        if agent_id:
            q = q.where(CallRow.agent_id == agent_id)
        if status:
            q = q.where(CallRow.status == status)
        return [_call_summary(c) for c in s.scalars(q)]


@app.get("/api/calls/{call_id}")
def get_call(call_id: str):
    with session_scope() as s:
        c = s.get(CallRow, call_id)
        if c is None:
            raise HTTPException(status_code=404, detail="call not found")
        return {
            **_call_summary(c),
            "variables": c.variables,
            "summary": c.summary,
            "extracted_data": c.extracted_data,
            "extraction_errors": c.extraction_errors or [],
            "postcall_error": c.postcall_error,
            "sip_status_code": c.sip_status_code,
            "extraction_schema": (c.agent_snapshot or {}).get("extraction_schema", []),
            "turns": [
                {
                    "idx": t.idx,
                    "speaker": t.speaker,
                    "text": t.text,
                    "started_at": t.started_at,
                    "ended_at": t.ended_at,
                    "interrupted": t.interrupted,
                    "v2v_ms": t.v2v_ms,
                    "llm_ttft_ms": t.llm_ttft_ms,
                    "tts_ttfb_ms": t.tts_ttfb_ms,
                    "end_of_turn_ms": t.end_of_turn_ms,
                    "transcription_ms": t.transcription_ms,
                }
                for t in c.turns
            ],
            "tool_invocations": [
                {
                    "tool_name": i.tool_name,
                    "arguments": i.arguments,
                    "status": i.status,
                    "http_status": i.http_status,
                    "response_excerpt": i.response_excerpt,
                    "error": i.error,
                    "started_at": i.started_at,
                    "latency_ms": i.latency_ms,
                    "at_start": i.at_start,
                }
                for i in c.tool_invocations
            ],
            "webhook_deliveries": [
                {
                    "id": w.id,
                    "event_id": w.event_id,
                    "event_type": w.event_type,
                    "url": w.url,
                    "status": w.status,
                    "attempts": w.attempts,
                    "last_status_code": w.last_status_code,
                    "last_error": w.last_error,
                    "delivered_at": w.delivered_at,
                }
                for w in c.webhook_deliveries
            ],
        }


@app.post("/api/calls/{call_id}/reprocess")
async def reprocess(call_id: str):
    """Retries post-call processing after a failure (no-op once done)."""
    with session_scope() as s:
        c = s.get(CallRow, call_id)
        if c is None:
            raise HTTPException(status_code=404, detail="call not found")
        if c.finalized_at is None:
            raise HTTPException(status_code=409, detail="call has not ended yet")
    calls.spawn(calls.process_call(call_id))
    return {"ok": True}


@app.post("/api/webhook-deliveries/{delivery_id}/resend")
async def resend_webhook(delivery_id: int):
    if not await asyncio.to_thread(webhooks.reset_for_resend, delivery_id):
        raise HTTPException(status_code=404, detail="delivery not found")
    calls.spawn(webhooks.deliver(delivery_id))
    return {"ok": True}


# --- campaigns ------------------------------------------------------------------


@app.post("/api/campaigns", status_code=201)
def create_campaign(body: CreateCampaign):
    if not settings.sip_trunk_id():
        raise HTTPException(status_code=400, detail="SIP_TRUNK_ID is not configured — campaigns place phone calls")
    try:
        return campaigns.create_campaign(
            name=body.name,
            agent_id=body.agent_id,
            csv_text=body.csv,
            concurrency=body.concurrency,
            max_attempts=body.max_attempts,
            retry_delay_seconds=int(body.retry_delay_minutes * 60),
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="agent not found") from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


def _campaign_summary(s, c: CampaignRow) -> dict:
    counts = dict(
        s.execute(
            select(CampaignContactRow.status, func.count())
            .where(CampaignContactRow.campaign_id == c.id)
            .group_by(CampaignContactRow.status)
        ).all()
    )
    agent = s.get(AgentRow, c.agent_id)
    return {
        "id": c.id,
        "name": c.name,
        "agent_id": c.agent_id,
        "agent_name": agent.name if agent else None,
        "status": c.status,
        "concurrency": c.concurrency,
        "max_attempts": c.max_attempts,
        "retry_delay_minutes": c.retry_delay_seconds / 60,
        "max_infra_attempts": c.max_infra_attempts,
        "created_at": c.created_at,
        "completed_at": c.completed_at,
        "counts": {k: counts.get(k, 0) for k in ("pending", "calling", "done", "exhausted")},
        "total": sum(counts.values()),
    }


@app.get("/api/campaigns")
def list_campaigns():
    with session_scope() as s:
        return [_campaign_summary(s, c) for c in s.scalars(select(CampaignRow).order_by(CampaignRow.created_at.desc()))]


@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: str):
    with session_scope() as s:
        c = s.get(CampaignRow, campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        outcomes = dict(
            s.execute(
                select(CallRow.id, CallRow.outcome).where(
                    CallRow.id.in_([x.last_call_id for x in c.contacts if x.last_call_id])
                )
            ).all()
        )
        return {
            **_campaign_summary(s, c),
            "contacts": [
                {
                    "id": x.id,
                    "phone_number": x.phone_number,
                    "variables": x.variables,
                    "status": x.status,
                    "attempts": x.attempts,
                    "infra_attempts": x.infra_attempts,
                    "next_attempt_at": x.next_attempt_at if x.status == "pending" else None,
                    "last_call_id": x.last_call_id,
                    "last_result": x.last_result,
                    "last_outcome": outcomes.get(x.last_call_id),
                }
                for x in c.contacts
            ],
        }


@app.post("/api/campaigns/{campaign_id}/{action}")
def campaign_action(campaign_id: str, action: str):
    if action not in ("pause", "resume"):
        raise HTTPException(status_code=404, detail="unknown action")
    if not campaigns.set_status(campaign_id, "paused" if action == "pause" else "running"):
        raise HTTPException(status_code=409, detail="campaign not found or already completed")
    return {"ok": True}


# --- internal (worker) ------------------------------------------------------------


@app.get("/internal/calls/{call_id}/config")
def internal_call_config(call_id: str, x_internal_secret: str | None = Header(default=None)):
    _require_internal(x_internal_secret)
    with session_scope() as s:
        c = s.get(CallRow, call_id)
        if c is None:
            raise HTTPException(status_code=404, detail="call not found")
        return {
            "call_id": c.id,
            "agent_id": c.agent_id,
            "mode": c.mode,
            "to_number": c.to_number,
            "variables": c.variables,
            "agent": c.agent_snapshot,
        }


@app.post("/internal/calls/{call_id}/events")
def internal_call_event(call_id: str, event: CallEvent, x_internal_secret: str | None = Header(default=None)):
    _require_internal(x_internal_secret)
    if event.type == "answered":
        calls.mark_answered(call_id, event.at)
    return {"ok": True}


@app.post("/internal/calls/{call_id}/report")
async def internal_call_report(call_id: str, report: CallReport, x_internal_secret: str | None = Header(default=None)):
    _require_internal(x_internal_secret)
    try:
        newly_finalized = await asyncio.to_thread(calls.record_report, call_id, report)
    except LookupError:
        raise HTTPException(status_code=404, detail="call not found") from None
    if newly_finalized:
        calls.after_finalize(call_id)
    return {"ok": True, "duplicate": not newly_finalized}


# --- demo helpers ------------------------------------------------------------------
# Sample webhook tools + a receiver, so tool calling and post-call webhooks
# can be exercised end-to-end without any external service.

_demo_orders = {
    "1001": {"status": "out_for_delivery", "eta": "today by 7 PM", "courier": "Delhivery"},
    "1002": {"status": "delivery_failed", "reason": "customer unavailable", "next_attempt": "tomorrow"},
    "1003": {"status": "delivered", "delivered_on": "yesterday"},
}
_demo_received: list[dict] = []
_demo_seen_event_ids: set[str] = set()


@app.post("/demo/tools/order-status")
async def demo_order_status(request: Request):
    body = await request.json()
    order_id = str((body.get("arguments") or {}).get("order_id", "")).strip()
    if order_id not in _demo_orders:
        return {"found": False, "message": f"No order with id {order_id}"}
    return {"found": True, "order_id": order_id, **_demo_orders[order_id]}


# Mock for the reference-check agent's get_candidate_details tool
_demo_candidates = {
    "C-1001": {"name": "Priya Nair", "role": "ICU Registered Nurse",
               "employer": "St. Mary's Medical Center", "claimed_start": "March 2021", "claimed_end": "January 2024"},
    "C-1002": {"name": "James Carter", "role": "Travel Respiratory Therapist",
               "employer": "Mercy General Hospital", "claimed_start": "June 2019", "claimed_end": "August 2022"},
    "C-1003": {"name": "Aisha Khan", "role": "Medical-Surgical Nurse",
               "employer": "Lakeside Community Hospital", "claimed_start": "September 2020", "claimed_end": "present"},
}


@app.post("/demo/tools/candidate-details")
async def demo_candidate_details(request: Request):
    body = await request.json()
    candidate_id = str((body.get("arguments") or {}).get("candidate_id", "")).strip().upper()
    if candidate_id not in _demo_candidates:
        return {"found": False, "message": f"No candidate with id {candidate_id}"}
    return {"found": True, "candidate_id": candidate_id, **_demo_candidates[candidate_id]}


@app.post("/demo/tools/slow")
async def demo_slow_tool():
    await asyncio.sleep(20)  # longer than any tool timeout -> exercises recovery
    return {"ok": True}


@app.post("/demo/tools/broken")
async def demo_broken_tool():
    raise HTTPException(status_code=500, detail="simulated failure")


@app.post("/demo/webhook-receiver")
async def demo_webhook_receiver(request: Request, idempotency_key: str | None = Header(default=None)):
    """Dedupes on Idempotency-Key exactly as a real receiver should."""
    duplicate = idempotency_key in _demo_seen_event_ids
    if idempotency_key:
        _demo_seen_event_ids.add(idempotency_key)
    _demo_received.append(
        {"received_at": time.time(), "event_id": idempotency_key, "duplicate": duplicate, "body": await request.json()}
    )
    return {"ok": True, "duplicate": duplicate}


@app.get("/demo/webhook-receiver")
def demo_webhook_received():
    return list(reversed(_demo_received[-50:]))


# --- misc ------------------------------------------------------------------------


@app.get("/api/health")
def health():
    return {"ok": True, "telephony": bool(settings.sip_trunk_id())}


@app.exception_handler(ValidationError)
async def _validation_error(_request: Request, exc: ValidationError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=422, content={"detail": exc.errors()})


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")
