"""Call lifecycle on the API side: creating calls, recording the worker's
final report (idempotently), post-call processing, and recovery.

All DB work is in plain sync functions; async orchestration calls them via
asyncio.to_thread so the event loop never blocks on Postgres."""

import asyncio
import json
import logging
import time
import uuid

from sqlalchemy import select

from . import postcall, telephony, webhooks
from .db import AgentRow, CallRow, ToolInvocationRow, TurnRow, session_scope
from .runtime import placeholders, summarize_v2v, turn_latencies, voice_to_voice_ms
from .schemas import AgentConfig, CallReport

logger = logging.getLogger("vap.calls")

# Statuses after which a call never changes again (besides post-processing)
TERMINAL_STATUSES = {"completed", "voicemail", "no_answer", "busy", "rejected", "failed"}
# Grace period past max_call_duration before a call with no worker report is
# given up on (worker crashed, report lost, ...)
REPORT_GRACE_SECONDS = 180

_background: set[asyncio.Task] = set()

# Called (as coroutine functions) with the call_id each time a call is
# finalized, whichever path finalized it. campaigns.py registers itself here
# to schedule retries, without calls.py having to know about campaigns.
FINALIZE_HOOKS: list = []


def spawn(coro) -> asyncio.Task:
    """create_task that keeps a strong reference until the task finishes."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


# --- creation -----------------------------------------------------------


def missing_variables(config: dict, variables: dict) -> set[str]:
    """Placeholders a call must be given values for. An unfilled one would be
    spoken literally by the TTS. {{tool.field}} placeholders are filled at
    runtime from run_at_start tools, so they're not the caller's job."""
    needed = placeholders(
        config.get("system_prompt", ""),
        config.get("first_message", ""),
        config.get("voicemail_message", ""),
        *config.get("end_call_conditions", []),
        # run_at_start tools fill their arguments from call variables too
        *(
            v
            for t in config.get("tools", [])
            if t.get("run_at_start")
            for v in (t.get("start_arguments") or {}).values()
            if isinstance(v, str)
        ),
    )
    return {n for n in needed if "." not in n} - {k for k, v in variables.items() if str(v).strip()}


def new_call_row(
    agent: AgentRow, mode: str, to_number: str | None, variables: dict, campaign_contact_id: int | None = None
) -> CallRow:
    call_id = str(uuid.uuid4())
    return CallRow(
        id=call_id,
        agent_id=agent.id,
        agent_name=agent.name,
        agent_snapshot=agent.config,
        variables=variables,
        mode=mode,
        to_number=to_number,
        room=f"vap-{mode}-{call_id[:8]}",
        status="dialing" if mode == "phone" else "in_progress",
        campaign_contact_id=campaign_contact_id,
    )


def create_call(agent_id: str, mode: str, to_number: str | None, variables: dict) -> CallRow:
    with session_scope() as s:
        agent = s.get(AgentRow, agent_id)
        if agent is None or agent.deleted_at is not None:
            raise LookupError("agent not found")
        missing = missing_variables(agent.config, variables)
        if missing:
            raise ValueError(f"missing call variable(s): {', '.join(sorted(missing))}")
        call = new_call_row(agent, mode, to_number, variables)
        s.add(call)
        return call


async def run_phone_call(call_id: str, room: str, to_number: str) -> None:
    status, sip_code, reason = await telephony.place_phone_call(room, call_id, to_number)
    if status == "answered":
        await asyncio.to_thread(mark_answered, call_id, time.time())
        return
    finalized = await asyncio.to_thread(finalize_without_conversation, call_id, status, reason, sip_code)
    if finalized:
        after_finalize(call_id)


def after_finalize(call_id: str) -> None:
    """Everything that follows a call being finalized: post-call processing,
    plus any registered hooks (campaign retry scheduling)."""
    spawn(process_call(call_id))
    for hook in FINALIZE_HOOKS:
        spawn(hook(call_id))


# --- worker events / report -------------------------------------------------


def mark_answered(call_id: str, at: float) -> None:
    with session_scope() as s:
        call = s.get(CallRow, call_id)
        if call is None or call.finalized_at is not None:
            return
        call.status = "in_progress"
        if call.answered_at is None:
            call.answered_at = at


def finalize_without_conversation(call_id: str, status: str, reason: str | None, sip_code: int | None = None) -> bool:
    """For calls that never reached a conversation (dial failure, no report).
    Returns False if the call was already finalized (no-op)."""
    with session_scope() as s:
        call = s.get(CallRow, call_id, with_for_update=True)
        if call is None or call.finalized_at is not None:
            return False
        now = time.time()
        call.status = status
        call.end_reason = reason
        call.sip_status_code = sip_code
        call.ended_at = call.ended_at or now
        call.finalized_at = now
        if call.answered_at:
            call.duration_seconds = round(call.ended_at - call.answered_at, 1)
        return True


def record_report(call_id: str, report: CallReport) -> bool:
    """Stores the worker's end-of-call report. Idempotent: the worker retries
    this request, so a second copy of the same report (or a report arriving
    after the call was already finalized another way) is acknowledged but
    ignored. Returns True only when this call newly finalized the call."""
    with session_scope() as s:
        call = s.get(CallRow, call_id, with_for_update=True)
        if call is None:
            raise LookupError("call not found")
        if call.finalized_at is not None:
            return False

        call.answered_by = report.answered_by
        if report.connected:
            call.status = "voicemail" if report.answered_by == "machine" else "completed"
            call.answered_at = call.answered_at or report.connected_at
        else:
            # Web: the browser never joined. Phone: telephony would normally
            # have finalized already; this is the worker-side fallback.
            call.status = "no_answer" if call.mode == "phone" else "failed"
        call.end_reason = report.end_reason
        call.ended_at = report.ended_at
        call.finalized_at = time.time()
        if call.answered_at:
            call.duration_seconds = round(max(0.0, report.ended_at - call.answered_at), 1)

        v2v = voice_to_voice_ms([(t.speaker, t.metrics) for t in report.turns])
        for idx, t in enumerate(report.turns):
            lat = turn_latencies(t.speaker, t.metrics)
            if t.speaker == "agent":
                lat["v2v_ms"] = v2v[idx]
            s.add(
                TurnRow(
                    call_id=call_id,
                    idx=idx,
                    speaker=t.speaker,
                    text=t.text,
                    started_at=t.started_at,
                    ended_at=t.ended_at,
                    interrupted=t.interrupted,
                    metrics=t.metrics,
                    **lat,
                )
            )
        call.avg_v2v_ms, call.max_v2v_ms = summarize_v2v(v2v)

        for inv in report.tool_invocations:
            s.add(ToolInvocationRow(call_id=call_id, **inv.model_dump()))
        return True


# --- post-call processing ---------------------------------------------------


def _transcript_text(call: CallRow) -> str:
    """Speaker-labelled, timestamped transcript for post-call analysis, with
    tool calls and their results interleaved (fields like "dates match the
    claim" are judged against what a tool returned)."""
    base = call.answered_at or (call.turns[0].started_at if call.turns else None) or call.created_at
    events = []
    for t in call.turns:
        who = "Agent" if t.speaker == "agent" else "User"
        cut = " [interrupted]" if t.interrupted else ""
        events.append((t.started_at or base, f"{who}: {t.text}{cut}"))
    for i in call.tool_invocations:
        result = i.response_excerpt if i.status == "ok" else f"FAILED ({i.error})"
        events.append((i.started_at, f"Tool {i.tool_name}({json.dumps(i.arguments, ensure_ascii=False)}) -> {result}"))
    lines = []
    for at, text in sorted(events, key=lambda e: e[0]):
        offset = max(0, at - base)
        lines.append(f"[{int(offset // 60):02d}:{int(offset % 60):02d}] {text}")
    return "\n".join(lines)


def _begin_processing(call_id: str) -> dict | None:
    """Claims the call for processing. Returns what's needed to analyze it,
    or None if it's already processed/being processed."""
    with session_scope() as s:
        call = s.get(CallRow, call_id, with_for_update=True)
        if call is None or call.finalized_at is None or call.postcall_status in ("done", "processing"):
            return None
        call.postcall_status = "processing"
        call.postcall_error = None
        has_user_speech = any(t.speaker == "user" for t in call.turns)
        return {
            "cfg": AgentConfig(**call.agent_snapshot),
            "status": call.status,
            "end_reason": call.end_reason,
            "transcript": _transcript_text(call),
            "has_conversation": call.status == "completed" and has_user_speech,
        }


def _save_analysis(call_id: str, result: dict) -> None:
    with session_scope() as s:
        call = s.get(CallRow, call_id)
        call.summary = result["summary"]
        call.outcome = result["outcome"]
        call.extracted_data = result["data"]
        call.extraction_errors = result["errors"]
        call.extraction_raw = result.get("raw")
        call.postcall_status = "done"


def _fail_processing(call_id: str, error: str) -> None:
    with session_scope() as s:
        call = s.get(CallRow, call_id)
        call.postcall_status = "failed"
        call.postcall_error = error[:2000]


async def process_call(call_id: str, attempts: int = 3) -> None:
    """Extraction + summary + outcome, then the post-call webhook. The
    webhook is only sent once processing succeeded, so its payload always
    carries final data; a failed run can be retried from the dashboard."""
    job = await asyncio.to_thread(_begin_processing, call_id)
    if job is not None:
        cfg: AgentConfig = job["cfg"]
        if not job["has_conversation"]:
            # Nothing was said by the callee — no LLM call needed.
            kind = job["status"] if job["status"] in postcall.CONNECTIVITY_OUTCOMES else "no_conversation"
            left_message = job["end_reason"] == "voicemail_message_left"
            summary = {
                "no_answer": "The call was not answered.",
                "busy": "The line was busy.",
                "rejected": "The call was rejected.",
                "failed": "The call could not be connected.",
                "voicemail": "Reached voicemail; "
                + ("left the configured message." if left_message else "hung up without leaving a message."),
            }.get(kind, "The call connected but the callee never spoke.")
            result = {
                "summary": summary,
                "outcome": postcall.system_outcome(cfg, kind),
                "data": {f.name: None for f in cfg.extraction_schema},
                "errors": [],
                "raw": None,
            }
            await asyncio.to_thread(_save_analysis, call_id, result)
        else:
            last_error = None
            for attempt in range(attempts):
                try:
                    result = await postcall.analyze_call(cfg, job["transcript"])
                    await asyncio.to_thread(_save_analysis, call_id, result)
                    last_error = None
                    break
                except Exception as e:
                    last_error = f"{type(e).__name__}: {e}"
                    logger.exception("post-call analysis failed call=%s attempt=%d", call_id, attempt + 1)
                    await asyncio.sleep(2 * (attempt + 1))
            if last_error:
                await asyncio.to_thread(_fail_processing, call_id, last_error)
                return

    status = await asyncio.to_thread(_postcall_status, call_id)
    if status == "done":
        delivery_id = await asyncio.to_thread(webhooks.enqueue, call_id)
        if delivery_id is not None:
            await webhooks.deliver(delivery_id)


def _postcall_status(call_id: str) -> str | None:
    with session_scope() as s:
        call = s.get(CallRow, call_id)
        return call.postcall_status if call else None


# --- recovery / sweeping -------------------------------------------------------


def sweep_stale_calls() -> list[str]:
    """Finalizes calls whose worker never reported back (crashed, report
    lost) once they're well past their own max duration."""
    now = time.time()
    finalized = []
    with session_scope() as s:
        open_calls = list(s.scalars(select(CallRow).where(CallRow.finalized_at.is_(None))))
        stale = [
            c.id
            for c in open_calls
            if now - c.created_at > (c.agent_snapshot or {}).get("max_call_duration_seconds", 180) + REPORT_GRACE_SECONDS
        ]
    for call_id in stale:
        if finalize_without_conversation(call_id, "failed", "no_report_from_agent"):
            finalized.append(call_id)
    return finalized


def calls_needing_processing() -> list[str]:
    with session_scope() as s:
        return list(
            s.scalars(
                select(CallRow.id).where(
                    CallRow.finalized_at.is_not(None), CallRow.postcall_status.in_(["pending", "processing"])
                )
            )
        )


def reset_stuck_processing() -> None:
    """On startup nothing can actually be processing — a 'processing' row was
    interrupted by the restart."""
    with session_scope() as s:
        for call in s.scalars(select(CallRow).where(CallRow.postcall_status == "processing")):
            call.postcall_status = "pending"


async def recover_on_startup() -> None:
    await asyncio.to_thread(reset_stuck_processing)
    for call_id in await asyncio.to_thread(calls_needing_processing):
        spawn(process_call(call_id))
    for delivery_id in await asyncio.to_thread(webhooks.pending_delivery_ids):
        spawn(webhooks.deliver(delivery_id))


async def sweeper_loop(interval: float = 30.0) -> None:
    while True:
        try:
            for call_id in await asyncio.to_thread(sweep_stale_calls):
                logger.warning("call %s finalized by sweeper (no worker report)", call_id)
                after_finalize(call_id)
        except Exception:
            logger.exception("sweeper iteration failed")
        await asyncio.sleep(interval)
