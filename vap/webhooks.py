"""Post-call webhook delivery — at-least-once, without duplicates.

Duplicates are prevented on our side by construction:
  * one webhook_deliveries row per (call_id, event_type), enforced by a
    unique constraint — enqueue() is get-or-create, so a second report, a
    restart recovery or a manual resend can never create a second event;
  * the event_id and the serialized body are fixed when the row is created,
    so every retry is byte-identical (same Idempotency-Key, same signature);
  * a per-delivery asyncio lock stops two deliver loops (e.g. automatic
    retry + manual "resend") from racing on the same row.

At-least-once means the receiver may still see the same event twice (e.g.
it returned 200 but the response was lost), so every request carries the
stable event_id in an Idempotency-Key header for the receiver to dedupe on.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import CallRow, WebhookDeliveryRow, new_id, session_scope

logger = logging.getLogger("vap.webhooks")

EVENT_CALL_COMPLETED = "call.completed"
MAX_ATTEMPTS = 6
REQUEST_TIMEOUT_SECONDS = 10.0
# Delay before attempt n+1 (index = attempts already made)
BACKOFF_SECONDS = [0, 2, 5, 15, 30, 60]

# Keyed by (event loop, delivery id): an asyncio.Lock is bound to one loop.
_locks: dict[tuple[int, int], asyncio.Lock] = {}


def build_payload(call: CallRow, event_id: str) -> dict:
    return {
        "event_id": event_id,
        "event_type": EVENT_CALL_COMPLETED,
        "call": {
            "id": call.id,
            "agent_id": call.agent_id,
            "agent_name": call.agent_name,
            "mode": call.mode,
            "to_number": call.to_number,
            "status": call.status,
            "end_reason": call.end_reason,
            "started_at": call.answered_at,
            "ended_at": call.ended_at,
            "duration_seconds": call.duration_seconds,
            "variables": call.variables,
        },
        "outcome": call.outcome,
        "summary": call.summary,
        "extracted_data": call.extracted_data,
        "extraction_errors": call.extraction_errors or [],
        "latency": {"avg_v2v_ms": call.avg_v2v_ms, "max_v2v_ms": call.max_v2v_ms},
        "transcript": [
            {
                "speaker": t.speaker,
                "text": t.text,
                "started_at": t.started_at,
                "ended_at": t.ended_at,
                "interrupted": t.interrupted,
            }
            for t in call.turns
        ],
    }


def sign(body: str, secret: str | None) -> str | None:
    if not secret:
        return None
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def enqueue(call_id: str) -> int | None:
    """Get-or-create the call.completed delivery for this call. Returns its
    id, or None if the agent has no webhook URL configured."""
    with session_scope() as s:
        existing = s.scalar(
            select(WebhookDeliveryRow).where(
                WebhookDeliveryRow.call_id == call_id,
                WebhookDeliveryRow.event_type == EVENT_CALL_COMPLETED,
            )
        )
        if existing:
            return existing.id
        call = s.get(CallRow, call_id)
        url = (call.agent_snapshot or {}).get("post_call_webhook_url")
        if not url:
            return None
        event_id = new_id()
        row = WebhookDeliveryRow(
            call_id=call_id,
            event_type=EVENT_CALL_COMPLETED,
            event_id=event_id,
            url=url,
            payload=json.dumps(build_payload(call, event_id), ensure_ascii=False, sort_keys=True),
            next_attempt_at=time.time(),
        )
        s.add(row)
        try:
            s.flush()
        except IntegrityError:
            # Lost a race with a concurrent enqueue — use the winner's row.
            s.rollback()
            existing = s.scalar(
                select(WebhookDeliveryRow).where(
                    WebhookDeliveryRow.call_id == call_id,
                    WebhookDeliveryRow.event_type == EVENT_CALL_COMPLETED,
                )
            )
            return existing.id if existing else None
        return row.id


def _load_attempt(delivery_id: int) -> dict | str:
    """Next attempt's parameters, or a terminal status string."""
    with session_scope() as s:
        row = s.get(WebhookDeliveryRow, delivery_id)
        if row is None:
            return "missing"
        if row.status in ("delivered", "failed"):
            return row.status
        if row.attempts >= MAX_ATTEMPTS:
            row.status = "failed"
            return "failed"
        return {
            "attempts": row.attempts,
            "url": row.url,
            "body": row.payload,
            "event_id": row.event_id,
            "secret": (row.call.agent_snapshot or {}).get("post_call_webhook_secret"),
            "wait": max(0.0, (row.next_attempt_at or 0) - time.time()),
        }


def _record_attempt(delivery_id: int, status_code: int | None, error: str | None) -> str | None:
    """Persists one attempt's result. Returns the terminal status, or None
    if another attempt is scheduled."""
    with session_scope() as s:
        row = s.get(WebhookDeliveryRow, delivery_id)
        row.attempts += 1
        row.last_status_code = status_code
        row.last_error = error
        if error is None:
            row.status = "delivered"
            row.delivered_at = time.time()
            row.next_attempt_at = None
            logger.info("webhook delivered call=%s event=%s", row.call_id, row.event_id)
            return "delivered"
        # 4xx (other than timeout/conflict/rate-limit) won't fix itself on retry
        permanent = status_code is not None and 400 <= status_code < 500 and status_code not in (408, 409, 425, 429)
        if permanent or row.attempts >= MAX_ATTEMPTS:
            row.status = "failed"
            row.next_attempt_at = None
            logger.warning("webhook failed call=%s event=%s: %s", row.call_id, row.event_id, error)
            return "failed"
        row.next_attempt_at = time.time() + BACKOFF_SECONDS[min(row.attempts, len(BACKOFF_SECONDS) - 1)]
        return None


async def deliver(delivery_id: int, *, client: httpx.AsyncClient | None = None, sleep=asyncio.sleep) -> str:
    """Attempts delivery until it succeeds, hits a permanent error, or runs
    out of attempts. Returns the final status. Safe to call concurrently or
    repeatedly for the same id — a delivered row is a no-op."""
    lock = _locks.setdefault((id(asyncio.get_running_loop()), delivery_id), asyncio.Lock())
    async with lock:
        owns_client = client is None
        client = client or httpx.AsyncClient()
        try:
            while True:
                attempt = await asyncio.to_thread(_load_attempt, delivery_id)
                if isinstance(attempt, str):
                    return attempt
                attempts, url, body, event_id = attempt["attempts"], attempt["url"], attempt["body"], attempt["event_id"]
                secret = attempt["secret"]
                if attempt["wait"]:
                    await sleep(attempt["wait"])

                headers = {
                    "Content-Type": "application/json",
                    "Idempotency-Key": event_id,
                    "X-VAP-Event-Id": event_id,
                    "X-VAP-Event-Type": EVENT_CALL_COMPLETED,
                    "X-VAP-Delivery-Attempt": str(attempts + 1),
                }
                signature = sign(body, secret)
                if signature:
                    headers["X-VAP-Signature"] = signature

                status_code, error = None, None
                try:
                    resp = await client.post(url, content=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
                    status_code = resp.status_code
                    if not resp.is_success:
                        error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"[:500]

                final = await asyncio.to_thread(_record_attempt, delivery_id, status_code, error)
                if final:
                    return final
        finally:
            if owns_client:
                await client.aclose()


def reset_for_resend(delivery_id: int) -> bool:
    """Manual resend from the dashboard: same row, same event_id, same body —
    the receiver sees a retry of the same event, never a new one."""
    with session_scope() as s:
        row = s.get(WebhookDeliveryRow, delivery_id)
        if row is None:
            return False
        row.status = "pending"
        row.attempts = 0
        row.next_attempt_at = time.time()
        return True


def pending_delivery_ids() -> list[int]:
    with session_scope() as s:
        return list(s.scalars(select(WebhookDeliveryRow.id).where(WebhookDeliveryRow.status == "pending")))
