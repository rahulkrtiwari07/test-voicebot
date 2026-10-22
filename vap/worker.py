"""LiveKit agent worker — one generic runtime for every configured agent.

Nothing here is agent-specific: each job's metadata only carries a call_id;
the worker fetches that call's frozen agent config from the API and builds
the STT -> LLM -> TTS session, tools and end-call behaviour from it.

Run:  uv run python -m vap.worker dev     (or `start` in production)
"""

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    JobContext,
    JobProcess,
    RunContext,
    cli,
    function_tool,
    inference,
    room_io,
)
from livekit.plugins import noise_cancellation, silero

from . import settings
from .providers import build_llm, build_stt, build_tts
from .runtime import (
    build_instructions,
    call_webhook_tool,
    placeholders,
    render_template,
    start_result_variables,
)
from .schemas import AgentConfig, WebhookTool
from .telephony import SIP_PARTICIPANT_IDENTITY, WEB_PARTICIPANT_IDENTITY

logger = logging.getLogger("vap.worker")

# Same Windows console fix as the NDR bot: Hindi transcripts in log lines
# otherwise crash cp1252 logging.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# How long to wait for the callee to pick up / the browser to join
CONNECT_TIMEOUT_SECONDS = 60.0
# Silence before the agent checks in; a second silence ends the call
USER_AWAY_TIMEOUT_SECONDS = 12.0
# Upper bound on the goodbye before we hang up regardless
CLOSING_SPEECH_TIMEOUT_SECONDS = 15.0
REPORT_ATTEMPTS = 3
FAILED_REPORTS_PATH = Path(__file__).resolve().parent.parent / "worker_failed_reports.jsonl"


class CallState:
    def __init__(self) -> None:
        self.connected_at: float | None = None
        self.end_reason: str | None = None
        self.ending = False
        self.tool_invocations: list[dict] = []
        self.background_audio_started = False
        # Set once the first message starts; silence handling waits for it
        self.greeted = False
        # "human" | "machine" when voicemail detection ran
        self.answered_by: str | None = None


# --- provider construction (config -> plugin) ------------------------------------


# --- the agent ------------------------------------------------------------------------


def make_webhook_tool(tool: WebhookTool, call_id: str, agent_id: str, state: CallState):
    """Turns one configured webhook tool into an LLM function tool. The
    returned string (success or structured failure) is the tool result the
    LLM sees and uses in its next reply."""

    async def handler(raw_arguments: dict[str, object], context: RunContext):
        result, record = await call_webhook_tool(tool, dict(raw_arguments), call_id=call_id, agent_id=agent_id)
        state.tool_invocations.append(record)
        logger.info("tool %s -> %s in %.0fms", tool.name, record["status"], record["latency_ms"])
        return result

    return function_tool(
        handler,
        raw_schema={"name": tool.name, "description": tool.description, "parameters": tool.parameters},
    )


class PlatformAgent(Agent):
    def __init__(self, *, instructions: str, tools: list, state: CallState, hang_up) -> None:
        super().__init__(instructions=instructions, tools=tools)
        self._state = state
        self._hang_up = hang_up

    @function_tool()
    async def end_call(self, context: RunContext, reason: str, closing_message: str):
        """End the phone call. Use once the call's goal is met or an end-call condition applies.

        Args:
            reason: Short snake_case reason the call is ending, e.g. goal_met, caller_declined, wrong_person.
            closing_message: The final goodbye, spoken verbatim before hanging up.
        """
        if self._state.ending:
            return None
        self._state.ending = True
        # Lessons from the NDR bot: stop listening and drop any half-buffered
        # user turn, so nothing can trigger an extra reply that races the
        # hang-up and gets cut off mid-word.
        self.session.input.set_audio_enabled(False)
        self.session.clear_user_turn()
        handle = self.session.say(closing_message, allow_interruptions=False)
        asyncio.create_task(self._hang_up(f"agent_ended:{reason}", handle))
        # None = tool completed silently (a string would trigger another reply)
        return None


# --- API communication -----------------------------------------------------------------


def _headers() -> dict:
    return {"X-Internal-Secret": settings.internal_secret()}


async def fetch_call_config(call_id: str) -> dict:
    url = f"{settings.platform_api_url()}/internal/calls/{call_id}/config"
    last: Exception | None = None
    async with httpx.AsyncClient(timeout=5.0) as client:
        for attempt in range(3):
            try:
                resp = await client.get(url, headers=_headers())
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last = e
                await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"could not fetch config for call {call_id}: {last}")


async def post_event(call_id: str, event_type: str, at: float) -> None:
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                f"{settings.platform_api_url()}/internal/calls/{call_id}/events",
                json={"type": event_type, "at": at},
                headers=_headers(),
            )


async def send_report(call_id: str, report: dict) -> None:
    """Retried; the API side is idempotent, so a retry after a lost response
    can't double-record the call. Falls back to a local file so nothing is
    lost if the API is down (the API's sweeper marks the call failed)."""
    url = f"{settings.platform_api_url()}/internal/calls/{call_id}/report"
    async with httpx.AsyncClient(timeout=3.0) as client:
        for attempt in range(REPORT_ATTEMPTS):
            try:
                resp = await client.post(url, json=report, headers=_headers())
                if resp.status_code < 500:
                    if resp.status_code != 200:
                        logger.error("report rejected: %s %s", resp.status_code, resp.text[:300])
                    return
            except Exception:
                logger.warning("report attempt %d failed for call %s", attempt + 1, call_id)
            await asyncio.sleep(0.5 * (attempt + 1))
    with contextlib.suppress(Exception), open(FAILED_REPORTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"call_id": call_id, "report": report}, ensure_ascii=False) + "\n")
    logger.error("could not deliver report for call %s; saved to %s", call_id, FAILED_REPORTS_PATH)


def transcript_turns(session: AgentSession, unprompted_ids: set[str] = frozenset()) -> list[dict]:
    """unprompted_ids: agent messages the platform triggered itself (silence
    check-ins, time-limit wrap-ups) rather than replies to the caller — they
    are flagged so they don't count as voice-to-voice latency."""
    turns = []
    for item in session.history.items:
        if item.type != "message" or item.role not in ("user", "assistant"):
            continue
        text = (item.text_content or "").strip()
        if not text:
            continue
        m = dict(item.metrics or {})
        if item.id in unprompted_ids:
            m["unprompted"] = 1
        turns.append(
            {
                "speaker": "agent" if item.role == "assistant" else "user",
                "text": text,
                "started_at": m.get("started_speaking_at") or item.created_at,
                "ended_at": m.get("stopped_speaking_at"),
                "interrupted": bool(item.interrupted),
                "metrics": {k: v for k, v in m.items() if isinstance(v, (int, float))},
            }
        )
    turns.sort(key=lambda t: t["started_at"] or 0)
    return turns


async def wait_for_sip_active(room: rtc.Room, identity: str) -> None:
    """ctx.wait_for_participant resolves when the SIP participant is created
    (dialing), not when the callee picks up. Same approach as the NDR bot."""
    def active() -> bool:
        p = room.remote_participants.get(identity)
        return p is not None and p.attributes.get("sip.callStatus") == "active"

    if active():
        return
    fut: asyncio.Future = asyncio.get_running_loop().create_future()

    def on_attrs(_changed, p):
        if p.identity == identity and active() and not fut.done():
            fut.set_result(None)

    def on_left(p):
        if p.identity == identity and not fut.done():
            fut.set_exception(ConnectionError("callee disconnected before answering"))

    room.on("participant_attributes_changed", on_attrs)
    room.on("participant_disconnected", on_left)
    try:
        if not active():
            await fut
    finally:
        room.off("participant_attributes_changed", on_attrs)
        room.off("participant_disconnected", on_left)


# --- answering-machine detection -------------------------------------------------------
# Ported from the NDR bot (src/agent.py, _classify_human_or_machine): LiveKit's
# built-in AMD subscribes to its own copy of the SIP track, which live testing
# there showed returning silent frames (livekit/python-sdks#690), so this uses
# the main session's own VAD instead. A short utterance followed by a pause is
# a person ("Hello?"); continuous speech past the threshold is a recorded
# greeting; nothing at all is "uncertain" and treated as a person (calling a
# human a machine is the worse mistake).
AMD_NO_SPEECH_TIMEOUT = 5.0
AMD_MACHINE_SPEECH_SECONDS = 2.5
AMD_HUMAN_PAUSE_SECONDS = 0.5
# Leave-a-message: wait for the greeting (and beep) to finish before speaking
VOICEMAIL_GREETING_SILENCE_SECONDS = 1.5
VOICEMAIL_GREETING_MAX_SECONDS = 30.0


async def classify_human_or_machine(session: AgentSession) -> str:
    """Requires turn_detection="manual" so nothing replies while listening.
    Returns "human", "machine" or "uncertain"."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    speaking_since: float | None = None
    decision: asyncio.Future[str] = loop.create_future()

    async def confirm_after_pause() -> None:
        await asyncio.sleep(AMD_HUMAN_PAUSE_SECONDS)
        if speaking_since is None and not decision.done():
            decision.set_result("human")

    def on_state(ev) -> None:
        nonlocal speaking_since
        if ev.new_state == "speaking" and speaking_since is None:
            speaking_since = loop.time()
        elif ev.new_state != "speaking" and speaking_since is not None:
            speaking_since = None
            asyncio.create_task(confirm_after_pause())

    session.on("user_state_changed", on_state)
    try:
        while not decision.done():
            await asyncio.sleep(0.1)
            now = loop.time()
            if speaking_since is not None and now - speaking_since >= AMD_MACHINE_SPEECH_SECONDS:
                decision.set_result("machine")
            elif speaking_since is None and now - start >= AMD_NO_SPEECH_TIMEOUT and not decision.done():
                decision.set_result("uncertain")
        return decision.result()
    finally:
        session.off("user_state_changed", on_state)


async def wait_for_greeting_end(session: AgentSession) -> None:
    """Returns once the callee side has been quiet for a moment (the greeting
    and beep are over), or after VOICEMAIL_GREETING_MAX_SECONDS."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + VOICEMAIL_GREETING_MAX_SECONDS
    quiet_since = loop.time() if session.user_state != "speaking" else None
    while loop.time() < deadline:
        await asyncio.sleep(0.1)
        if session.user_state == "speaking":
            quiet_since = None
        elif quiet_since is None:
            quiet_since = loop.time()
        elif loop.time() - quiet_since >= VOICEMAIL_GREETING_SILENCE_SECONDS:
            return


# --- job -------------------------------------------------------------------------------

server = AgentServer()


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load(min_silence_duration=0.3, min_speech_duration=0.1)


server.setup_fnc = prewarm


class CallRuntime:
    """One call: builds the session from the config snapshot, then drives
    connect -> (voicemail detection) -> greeting -> conversation -> hang-up."""

    def __init__(self, ctx: JobContext, conf: dict) -> None:
        self.ctx = ctx
        self.call_id = conf["call_id"]
        self.agent_id = conf["agent_id"]
        self.cfg = AgentConfig(**conf["agent"])
        self.variables: dict = conf.get("variables") or {}
        self.mode = conf["mode"]
        self.identity = SIP_PARTICIPANT_IDENTITY if self.mode == "phone" else WEB_PARTICIPANT_IDENTITY
        self.state = CallState()
        self.checkin_task: asyncio.Task | None = None
        # Speech the platform triggered itself (see transcript_turns)
        self.unprompted_handles: list = []

        self.session = AgentSession(
            stt=build_stt(self.cfg),
            llm=build_llm(self.cfg),
            tts=build_tts(self.cfg),
            vad=ctx.proc.userdata["vad"],
            turn_handling={
                # Manual while voicemail detection listens, so nothing replies
                # to a greeting; switched back to automatic once it decides.
                **({"turn_detection": "manual"} if self.cfg.voicemail_detection else {}),
                "endpointing": {"mode": "fixed", "min_delay": 0.3, "max_delay": 1.5},
                # Barge-in: the caller talking over the agent stops its speech;
                # a false alarm (cough, noise) resumes it.
                "interruption": {
                    "enabled": True,
                    "min_duration": 0.4,
                    "resume_false_interruption": True,
                    "false_interruption_timeout": 2.0,
                },
                "preemptive_generation": {"enabled": True},
            },
            user_away_timeout=USER_AWAY_TIMEOUT_SECONDS,
        )
        tools = [make_webhook_tool(t, self.call_id, self.agent_id, self.state) for t in self.cfg.tools]
        self.agent = PlatformAgent(
            instructions=build_instructions(self.cfg, self.variables),
            tools=tools,
            state=self.state,
            hang_up=self.hang_up,
        )
        self.background_audio = BackgroundAudioPlayer(
            thinking_sound=[AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.35)],
        )
        self.session.on("user_state_changed", self._on_user_state)
        self.session.on("close", self._on_session_close)
        ctx.room.on("participant_disconnected", self._on_participant_left)
        ctx.add_shutdown_callback(self.on_shutdown)

    # --- lifecycle ---

    async def run(self) -> None:
        # Start-of-call tools run while the phone is still ringing.
        start_tools = asyncio.create_task(self.run_start_tools())

        # connect() must come first: with participant_identity set, RoomIO
        # compares it against the local participant, which only exists once
        # the room is connected.
        await self.ctx.connect()
        await self.session.start(
            agent=self.agent,
            room=self.ctx.room,
            room_options=room_io.RoomOptions(
                participant_identity=self.identity,
                audio_input=room_io.AudioInputOptions(
                    noise_cancellation=None
                    if os.environ.get("DISABLE_NOISE_CANCELLATION", "").lower() == "true"
                    else (noise_cancellation.BVCTelephony() if self.mode == "phone" else noise_cancellation.BVC())
                ),
            ),
        )

        try:
            await asyncio.wait_for(self.ctx.wait_for_participant(identity=self.identity), timeout=CONNECT_TIMEOUT_SECONDS)
            if self.mode == "phone":
                await asyncio.wait_for(wait_for_sip_active(self.ctx.room, self.identity), timeout=CONNECT_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, ConnectionError) as e:
            logger.info("callee never connected: %s", e)
            self.state.ending = True
            self.state.end_reason = "never_connected"
            self.ctx.shutdown(reason="never connected")
            return

        self.state.connected_at = time.time()
        asyncio.create_task(post_event(self.call_id, "answered", self.state.connected_at))
        asyncio.create_task(self.enforce_max_duration())

        if self.cfg.voicemail_detection:
            category = await classify_human_or_machine(self.session)
            logger.info("voicemail detection: %s", category)
            self.state.answered_by = "machine" if category == "machine" else "human"
            if category == "machine":
                await self.handle_voicemail(await start_tools)
                return
            # Whatever was heard while listening ("Hello?") is a signal, not
            # a turn for the LLM to answer.
            self.session.clear_user_turn()
            # Back to the session's default end-of-turn model — the same one
            # calls without voicemail detection use. (None would disable turn
            # detection entirely: sentences split at pauses and LiveKit stops
            # recording voice-to-voice latency.)
            self.session.update_options(turn_detection=inference.TurnDetector())

        if os.environ.get("DISABLE_BACKGROUND_AUDIO", "").lower() != "true":
            self.state.background_audio_started = True
            await self.background_audio.start(room=self.ctx.room, agent_session=self.session)

        template_vars = await start_tools
        self.state.greeted = True
        first = render_template(self.cfg.first_message, template_vars).strip()
        if first and not placeholders(first):
            self.session.say(first)
        else:
            # No first message, or it needs a start-tool field that didn't
            # arrive (tool failed): let the LLM open, with whatever it has.
            self.session.generate_reply(instructions="Greet the person and begin the conversation per your instructions.")

    async def run_start_tools(self) -> dict:
        """Runs run_at_start tools, gives their results to the LLM, and returns
        the template variables ({{tool.field}} plus the call's own)."""
        template_vars = dict(self.variables)
        results = []
        for tool in (t for t in self.cfg.tools if t.run_at_start):
            args = {
                k: render_template(v, self.variables) if isinstance(v, str) else v
                for k, v in tool.start_arguments.items()
            }
            result, record = await call_webhook_tool(tool, args, call_id=self.call_id, agent_id=self.agent_id)
            record["at_start"] = True
            self.state.tool_invocations.append(record)
            logger.info("start tool %s -> %s in %.0fms", tool.name, record["status"], record["latency_ms"])
            results.append({"name": tool.name, "arguments": args, "result": result})
            template_vars.update(start_result_variables(tool.name, result))
        if results:
            await self.agent.update_instructions(build_instructions(self.cfg, self.variables, start_results=results))
        return template_vars

    async def handle_voicemail(self, template_vars: dict) -> None:
        self.state.ending = True
        if self.cfg.voicemail_action == "leave_message":
            await wait_for_greeting_end(self.session)
            message = render_template(self.cfg.voicemail_message, template_vars).strip()
            handle = self.session.say(message, allow_interruptions=False)
            self.unprompted_handles.append(handle)
            await self.hang_up("voicemail_message_left", handle)
        else:
            await self.hang_up("voicemail_hang_up")

    async def hang_up(self, reason: str, handle=None) -> None:
        self.state.end_reason = self.state.end_reason or reason
        if handle is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(handle, timeout=CLOSING_SPEECH_TIMEOUT_SECONDS)
            await asyncio.sleep(0.5)  # let the last audio frames flush
        with contextlib.suppress(Exception):
            await self.ctx.api.room.delete_room(api.DeleteRoomRequest(room=self.ctx.room.name))
        self.ctx.shutdown(reason=reason)

    async def enforce_max_duration(self) -> None:
        await asyncio.sleep(self.cfg.max_call_duration_seconds)
        if self.state.ending:
            return
        self.state.ending = True
        logger.info("max call duration (%ss) reached", self.cfg.max_call_duration_seconds)
        self.session.input.set_audio_enabled(False)
        self.session.clear_user_turn()
        with contextlib.suppress(Exception):
            await self.session.interrupt(force=True)
        handle = self.session.generate_reply(
            instructions="The maximum call time has been reached. In one short sentence, apologize, "
            "say you need to end the call now, and say goodbye. Do not ask anything.",
            allow_interruptions=False,
            tool_choice="none",
        )
        self.unprompted_handles.append(handle)
        await self.hang_up("max_duration_reached", handle)

    async def on_shutdown(self) -> None:
        report = {
            "connected": self.state.connected_at is not None,
            "connected_at": self.state.connected_at,
            "ended_at": time.time(),
            "end_reason": self.state.end_reason or "unknown",
            "answered_by": self.state.answered_by,
            "turns": transcript_turns(
                self.session, {item.id for h in self.unprompted_handles for item in h.chat_items}
            ),
            "tool_invocations": self.state.tool_invocations,
        }
        await send_report(self.call_id, report)
        if self.state.background_audio_started:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.background_audio.aclose(), timeout=1.5)

    # --- events ---

    async def _check_in_then_end(self) -> None:
        handle = self.session.generate_reply(instructions="The caller has gone quiet. Briefly ask if they are still there.")
        self.unprompted_handles.append(handle)
        with contextlib.suppress(Exception):
            await handle
        await asyncio.sleep(USER_AWAY_TIMEOUT_SECONDS)
        if self.state.ending:
            return
        self.state.ending = True
        handle = self.session.generate_reply(
            instructions="The caller is not responding. Say in one sentence that you'll end the call now, and say goodbye.",
            allow_interruptions=False,
            tool_choice="none",
        )
        self.unprompted_handles.append(handle)
        await self.hang_up("caller_unresponsive", handle)

    def _on_user_state(self, ev) -> None:
        # Silence handling: the state flips to "away" once and then stays
        # there while the caller remains silent (no second event), so the
        # follow-up is an explicit timer rather than a second "away" event.
        if not self.state.greeted or self.state.ending:
            return
        task = self.checkin_task
        if ev.new_state == "speaking" and task and not task.done():
            task.cancel()  # caller is back — no hang-up
            self.checkin_task = None
        elif ev.new_state == "away" and (task is None or task.done()):
            self.checkin_task = asyncio.create_task(self._check_in_then_end())

    def _on_session_close(self, ev) -> None:
        """The session can close on its own (e.g. an unrecoverable STT/LLM/TTS
        error) while the room and job stay up — end the job so the call is
        reported instead of hanging until the API's sweeper."""
        if ev.error is not None:
            self.state.end_reason = self.state.end_reason or f"agent_error:{type(ev.error).__name__}"
            logger.error("session closed with error: %s", ev.error)
        reason = str(getattr(ev.reason, "value", ev.reason))
        # Also fires on normal endings (may arrive before the room's own
        # participant_disconnected event), so map those to the usual names.
        self.state.end_reason = self.state.end_reason or (
            "caller_hung_up" if reason == "participant_disconnected" else f"session_closed:{reason}"
        )
        self.state.ending = True
        self.ctx.shutdown(reason=f"session closed: {ev.reason}")

    def _on_participant_left(self, p: rtc.RemoteParticipant) -> None:
        if p.identity == self.identity and not self.state.ending:
            self.state.ending = True
            self.state.end_reason = self.state.end_reason or "caller_hung_up"
            self.ctx.shutdown(reason="caller hung up")


@server.rtc_session(agent_name=settings.agent_name())
async def entrypoint(ctx: JobContext) -> None:
    meta = json.loads(ctx.job.metadata or "{}")
    call_id = meta["call_id"]
    ctx.log_context_fields = {"call_id": call_id, "room": ctx.room.name}

    try:
        call = CallRuntime(ctx, await fetch_call_config(call_id))
    except Exception as e:
        # Setup failed before the shutdown report was wired up — report now,
        # or the call would sit "in progress" until the API's sweeper.
        logger.exception("call setup failed")
        await send_report(call_id, {"connected": False, "ended_at": time.time(), "end_reason": f"agent_error:{type(e).__name__}"})
        raise
    try:
        await call.run()
    except Exception as e:
        # Still reported by on_shutdown — this just makes the cause visible
        # in the call log instead of "unknown".
        call.state.end_reason = call.state.end_reason or f"agent_error:{type(e).__name__}"
        raise


if __name__ == "__main__":
    cli.run_app(server)
