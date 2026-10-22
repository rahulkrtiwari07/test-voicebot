import asyncio
import time

import httpx
from conftest import AGENT

from vap import webhooks
from vap.db import AgentRow, CallRow, WebhookDeliveryRow, session_scope


def _make_call(url="http://receiver.test/hook") -> str:
    with session_scope() as s:
        agent = AgentRow(name="a", config={**AGENT, "post_call_webhook_url": url})
        s.add(agent)
        s.flush()
        call = CallRow(
            agent_id=agent.id, agent_name="a", agent_snapshot=agent.config, variables={}, mode="web",
            room="r", status="completed", finalized_at=time.time(), postcall_status="done", outcome="goal_achieved",
        )
        s.add(call)
        s.flush()
        return call.id


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _no_sleep(_):
    return None


def test_enqueue_is_get_or_create(sqlite_db):
    call_id = _make_call()
    first, second = webhooks.enqueue(call_id), webhooks.enqueue(call_id)
    assert first == second
    with session_scope() as s:
        assert s.query(WebhookDeliveryRow).count() == 1


def test_no_webhook_url_means_no_delivery(sqlite_db):
    assert webhooks.enqueue(_make_call(url=None)) is None


async def test_permanent_4xx_stops_retrying(sqlite_db):
    delivery_id = webhooks.enqueue(_make_call())
    hits = []
    async with _client(lambda r: hits.append(r) or httpx.Response(410)) as client:
        assert await webhooks.deliver(delivery_id, client=client, sleep=_no_sleep) == "failed"
    assert len(hits) == 1


async def test_gives_up_after_max_attempts(sqlite_db):
    delivery_id = webhooks.enqueue(_make_call())
    hits = []
    async with _client(lambda r: hits.append(r) or httpx.Response(500)) as client:
        assert await webhooks.deliver(delivery_id, client=client, sleep=_no_sleep) == "failed"
    assert len(hits) == webhooks.MAX_ATTEMPTS


async def test_concurrent_deliveries_send_once(sqlite_db):
    """Auto-retry and a manual resend racing on the same row: one POST."""
    delivery_id = webhooks.enqueue(_make_call())
    hits = []

    async def handler(request):
        hits.append(request)
        await asyncio.sleep(0.05)
        return httpx.Response(200)

    async with _client(handler) as client:
        results = await asyncio.gather(*(webhooks.deliver(delivery_id, client=client, sleep=_no_sleep) for _ in range(3)))
    assert results == ["delivered"] * 3
    assert len(hits) == 1
