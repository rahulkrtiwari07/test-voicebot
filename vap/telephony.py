"""Starting calls on LiveKit: agent dispatch, outbound SIP dial (Plivo trunk),
and browser test-call tokens. Same flow the NDR bot's ndr-api uses:

1. dispatch the agent worker into a fresh room (metadata = call_id only; the
   worker fetches the full config snapshot from the API),
2. wait for the agent to actually join — so a dispatch no worker picked up
   never rings a customer into an empty room,
3. dial with wait_until_answered=True, so a SIP-level failure surfaces here
   with the carrier's status code."""

import asyncio
import json
import logging
import re

from livekit import api

from . import settings

logger = logging.getLogger("vap.telephony")

SIP_PARTICIPANT_IDENTITY = "sip-callee"
WEB_PARTICIPANT_IDENTITY = "web-caller"
AGENT_JOIN_TIMEOUT_SECONDS = 15.0
AGENT_JOIN_POLL_SECONDS = 0.5

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_phone_number(raw: str) -> str:
    """E.164, with bare 10-digit Indian mobile numbers given +91 (as the NDR
    bot's OMS import does). Raises ValueError for anything else."""
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    number = "+" + digits if raw.startswith("+") else ("+91" + digits if len(digits) == 10 else "+" + digits)
    if not _E164_RE.match(number):
        raise ValueError("phone number must be E.164, e.g. +919876543210")
    return number


def map_sip_status(status_code: int | None) -> str:
    """Carrier SIP response code -> call status. Same buckets as ndr-api."""
    if status_code in (486, 600):
        return "busy"
    if status_code in (480, 404, 410, 484, 487, 408):
        return "no_answer"
    if status_code in (403, 603):
        return "rejected"
    return "failed"


def _lkapi() -> api.LiveKitAPI:
    return api.LiveKitAPI(
        url=settings.env("LIVEKIT_URL"),
        api_key=settings.env("LIVEKIT_API_KEY"),
        api_secret=settings.env("LIVEKIT_API_SECRET"),
    )


async def dispatch_agent(lk: api.LiveKitAPI, room: str, call_id: str) -> None:
    await lk.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(
            agent_name=settings.agent_name(), room=room, metadata=json.dumps({"call_id": call_id})
        )
    )


async def wait_for_agent(lk: api.LiveKitAPI, room: str, timeout: float = AGENT_JOIN_TIMEOUT_SECONDS) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            resp = await lk.room.list_participants(api.ListParticipantsRequest(room=room))
            if resp.participants:
                return True
        except Exception:
            pass  # room not created yet
        await asyncio.sleep(AGENT_JOIN_POLL_SECONDS)
    return False


async def delete_room(lk: api.LiveKitAPI, room: str) -> None:
    try:
        await lk.room.delete_room(api.DeleteRoomRequest(room=room))
    except Exception:
        logger.debug("delete_room(%s) failed (likely already gone)", room)


async def place_phone_call(room: str, call_id: str, to_number: str) -> tuple[str, int | None, str | None]:
    """Dispatches the agent and dials. Returns (status, sip_status_code,
    end_reason): status is "answered" on pickup, otherwise the terminal call
    status (no_answer/busy/rejected/failed)."""
    trunk = settings.sip_trunk_id()
    if not trunk:
        return "failed", None, "sip_trunk_not_configured"
    lk = _lkapi()
    try:
        await dispatch_agent(lk, room, call_id)
        if not await wait_for_agent(lk, room):
            await delete_room(lk, room)
            return "failed", None, "agent_did_not_join"
        try:
            await lk.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    sip_trunk_id=trunk,
                    sip_call_to=to_number,
                    room_name=room,
                    participant_identity=SIP_PARTICIPANT_IDENTITY,
                    participant_name="Callee",
                    krisp_enabled=True,
                    wait_until_answered=True,
                )
            )
            return "answered", None, None
        except api.SipCallError as e:
            status = map_sip_status(e.sip_status_code)
            logger.info("SIP dial failed call=%s sip=%s -> %s", call_id, e.sip_status_code, status)
            await delete_room(lk, room)
            return status, e.sip_status_code, f"sip_{e.sip_status_code}"
    except Exception as e:
        logger.exception("placing call %s failed", call_id)
        await delete_room(lk, room)
        return "failed", None, f"dial_error: {type(e).__name__}"
    finally:
        await lk.aclose()


async def start_web_call(room: str, call_id: str) -> str:
    """Dispatches the agent and returns a token the browser uses to join the
    same room as the caller (WebRTC test call)."""
    lk = _lkapi()
    try:
        await dispatch_agent(lk, room, call_id)
    finally:
        await lk.aclose()
    return (
        api.AccessToken(settings.env("LIVEKIT_API_KEY"), settings.env("LIVEKIT_API_SECRET"))
        .with_identity(WEB_PARTICIPANT_IDENTITY)
        .with_name("Test caller")
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True))
        .to_jwt()
    )
