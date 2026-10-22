"""Campaigns: a CSV of contacts dialled by one agent with a concurrency limit,
and scheduled retries for contacts that didn't pick up.

All scheduling state lives in the database (contact status, attempts,
next_attempt_at), so a restart loses nothing: the scheduler just picks up
whatever is due. The retry rules follow ndr-api's design:

  * a real attempt (the phone rang: no_answer / busy / rejected / voicemail)
    counts against the contact's max_attempts;
  * an infrastructure failure (agent never joined, trunk/carrier 5xx, worker
    crash before pickup, dial error) counts against a separate
    max_infra_attempts budget and is retried quickly, so our own bad day
    never uses up a contact's retries.
"""

import asyncio
import csv
import io
import logging
import time

from sqlalchemy import func, select

from . import calls, settings, telephony
from .db import AgentRow, CallRow, CampaignContactRow, CampaignRow, new_id, session_scope

logger = logging.getLogger("vap.campaigns")

RETRYABLE_STATUSES = {"no_answer", "busy", "rejected", "voicemail"}
PHONE_COLUMNS = ("phone_number", "phone", "number", "to_number", "mobile")
SCHEDULER_INTERVAL_SECONDS = 2.0


def is_infra_failure(call: CallRow) -> bool:
    """A call that ended "failed" without ever being answered never reached
    the contact's phone in any meaningful way — that's on us, not them. Every
    callee-side result (busy/no answer/rejected) has its own status."""
    return call.status == "failed" and call.answered_at is None


def parse_contacts(csv_text: str, agent_config: dict) -> tuple[list[dict], list[dict]]:
    """Returns (contacts, rejected). Every non-phone column becomes a call
    variable, so a CSV with candidate_id/reference_name columns feeds an agent
    whose prompt uses {{candidate_id}}/{{reference_name}}."""
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    phone_col = next((h for h in headers if h.lower() in PHONE_COLUMNS), None)
    if phone_col is None:
        raise ValueError(f"CSV needs a phone column (one of: {', '.join(PHONE_COLUMNS)})")

    contacts, rejected, seen = [], [], set()
    for line_no, raw in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if not any(row.values()):
            continue
        try:
            number = telephony.normalize_phone_number(row.get(phone_col, ""))
        except ValueError as e:
            rejected.append({"line": line_no, "reason": str(e), "row": row})
            continue
        if number in seen:
            rejected.append({"line": line_no, "reason": f"duplicate number {number}", "row": row})
            continue
        variables = {k: v for k, v in row.items() if k != phone_col and k and v}
        missing = calls.missing_variables(agent_config, variables)
        if missing:
            rejected.append({"line": line_no, "reason": f"missing column value(s): {', '.join(sorted(missing))}", "row": row})
            continue
        seen.add(number)
        contacts.append({"phone_number": number, "variables": variables})
    return contacts, rejected


def create_campaign(
    *,
    name: str,
    agent_id: str,
    csv_text: str,
    concurrency: int,
    max_attempts: int,
    retry_delay_seconds: int,
) -> dict:
    with session_scope() as s:
        agent = s.get(AgentRow, agent_id)
        if agent is None or agent.deleted_at is not None:
            raise LookupError("agent not found")
        contacts, rejected = parse_contacts(csv_text, agent.config)
        if not contacts:
            raise ValueError("no valid contacts in the CSV" + (f" ({len(rejected)} rejected)" if rejected else ""))
        campaign = CampaignRow(
            id=new_id(),
            name=name,
            agent_id=agent_id,
            concurrency=concurrency,
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )
        s.add(campaign)
        now = time.time()
        for c in contacts:
            s.add(CampaignContactRow(campaign_id=campaign.id, next_attempt_at=now, **c))
        return {"id": campaign.id, "accepted": len(contacts), "rejected": rejected}


# --- scheduling -------------------------------------------------------------------


def claim_due_contacts(now: float | None = None) -> list[tuple[str, str, str]]:
    """Moves due contacts to "calling" and creates their calls, within both
    the campaign's own concurrency and the global MAX_CONCURRENT_CALLS cap
    (LiveKit's plan limit is shared by every campaign and manual call).
    Returns [(call_id, room, number)] to dial. Single API instance assumed."""
    now = now or time.time()
    to_dial = []
    with session_scope() as s:
        in_flight_global = s.scalar(
            select(func.count()).select_from(CallRow).where(CallRow.mode == "phone", CallRow.finalized_at.is_(None))
        )
        budget = settings.max_concurrent_calls() - in_flight_global
        for campaign in s.scalars(select(CampaignRow).where(CampaignRow.status == "running").order_by(CampaignRow.created_at)):
            agent = s.get(AgentRow, campaign.agent_id)
            if agent is None or agent.deleted_at is not None:
                campaign.status = "paused"
                continue
            calling = s.scalar(
                select(func.count()).where(
                    CampaignContactRow.campaign_id == campaign.id, CampaignContactRow.status == "calling"
                )
            )
            slots = min(campaign.concurrency - calling, budget)
            if slots <= 0:
                continue
            due = s.scalars(
                select(CampaignContactRow)
                .where(
                    CampaignContactRow.campaign_id == campaign.id,
                    CampaignContactRow.status == "pending",
                    CampaignContactRow.next_attempt_at <= now,
                )
                .order_by(CampaignContactRow.next_attempt_at, CampaignContactRow.id)
                .limit(slots)
            ).all()
            for contact in due:
                call = calls.new_call_row(agent, "phone", contact.phone_number, contact.variables, contact.id)
                s.add(call)
                contact.status = "calling"
                contact.last_call_id = call.id
                to_dial.append((call.id, call.room, contact.phone_number))
                budget -= 1
    return to_dial


def apply_call_result(call_id: str) -> str | None:
    """Updates the contact behind a finalized campaign call. Idempotent: only
    the contact's current call, while it's still "calling", is applied, so a
    hook that fires twice (or a recovery pass) can't double-count."""
    with session_scope() as s:
        call = s.get(CallRow, call_id)
        if call is None or call.campaign_contact_id is None or call.finalized_at is None:
            return None
        contact = s.get(CampaignContactRow, call.campaign_contact_id, with_for_update=True)
        if contact is None or contact.last_call_id != call_id or contact.status != "calling":
            return None
        campaign = contact.campaign
        now = time.time()
        contact.last_result = call.status

        if is_infra_failure(call):
            contact.infra_attempts += 1
            if contact.infra_attempts >= campaign.max_infra_attempts:
                contact.status = "exhausted"
                contact.last_result = f"infra_failed:{call.end_reason}"
            else:
                contact.status = "pending"
                contact.next_attempt_at = now + campaign.infra_retry_delay_seconds
        else:
            contact.attempts += 1
            if call.status in RETRYABLE_STATUSES:
                if contact.attempts < campaign.max_attempts:
                    contact.status = "pending"
                    contact.next_attempt_at = now + campaign.retry_delay_seconds
                else:
                    contact.status = "exhausted"
            else:
                contact.status = "done"

        _maybe_complete(s, campaign)
        return contact.status


def _maybe_complete(s, campaign: CampaignRow) -> None:
    open_contacts = s.scalar(
        select(func.count()).where(
            CampaignContactRow.campaign_id == campaign.id,
            CampaignContactRow.status.in_(["pending", "calling"]),
        )
    )
    if open_contacts == 0 and campaign.status != "completed":
        campaign.status = "completed"
        campaign.completed_at = time.time()


async def on_call_finalized(call_id: str) -> None:
    status = await asyncio.to_thread(apply_call_result, call_id)
    if status:
        logger.info("campaign call %s -> contact %s", call_id, status)


def recover_contacts() -> list[str]:
    """After a restart: contacts still "calling" whose call already finalized
    (the hook never ran) are applied now. Calls that never finalized are left
    to the call sweeper, which finalizes them and triggers the hook."""
    with session_scope() as s:
        rows = s.execute(
            select(CampaignContactRow.last_call_id)
            .join(CallRow, CallRow.id == CampaignContactRow.last_call_id)
            .where(CampaignContactRow.status == "calling", CallRow.finalized_at.is_not(None))
        ).all()
    return [r[0] for r in rows if apply_call_result(r[0])]


async def scheduler_loop() -> None:
    await asyncio.to_thread(recover_contacts)
    while True:
        try:
            for call_id, room, number in await asyncio.to_thread(claim_due_contacts):
                calls.spawn(calls.run_phone_call(call_id, room, number))
        except Exception:
            logger.exception("campaign scheduler iteration failed")
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)


def set_status(campaign_id: str, status: str) -> bool:
    with session_scope() as s:
        campaign = s.get(CampaignRow, campaign_id)
        if campaign is None or campaign.status == "completed":
            return False
        campaign.status = status
        return True


calls.FINALIZE_HOOKS.append(on_call_finalized)
