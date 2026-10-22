"""End-to-end API flow on SQLite: agent CRUD -> web call -> worker report ->
post-call processing -> post-call webhook, plus the idempotency guarantees."""

import json
import time

import httpx
import pytest
from conftest import AGENT, wait_for

from vap import postcall, telephony, webhooks

INTERNAL = {"X-Internal-Secret": "test-secret"}


@pytest.fixture
def receiver(monkeypatch):
    """Captures post-call webhook requests instead of hitting the network."""
    received = []
    state = {"fail_first": 0}

    def handler(request):
        received.append({"headers": dict(request.headers), "body": request.content.decode()})
        if state["fail_first"] > 0:
            state["fail_first"] -= 1
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(webhooks, "BACKOFF_SECONDS", [0, 0, 0, 0, 0, 0])
    return received, state


@pytest.fixture
def fake_llm(monkeypatch):
    async def complete(prompt):
        return json.dumps(
            {
                "summary": "Asha agreed to delivery tomorrow.",
                "outcome": "goal_achieved",
                "data": {"delivery_date": "2026-10-23", "wants_delivery": True, "intent": "reschedule"},
            }
        )

    monkeypatch.setattr(postcall, "gemini_complete_json", complete)


@pytest.fixture
def fake_dispatch(monkeypatch):
    async def start_web_call(room, call_id):
        return "fake-token"

    monkeypatch.setattr(telephony, "start_web_call", start_web_call)


def _report(t0):
    return {
        "connected": True,
        "connected_at": t0,
        "ended_at": t0 + 42,
        "end_reason": "agent_ended:goal_met",
        "turns": [
            {"speaker": "agent", "text": "Hi Asha!", "started_at": t0 + 0.5, "metrics": {"tts_node_ttfb": 0.2}},
            {"speaker": "user", "text": "Hi, what's this about?", "started_at": t0 + 2,
             "metrics": {"end_of_turn_delay": 0.3, "transcription_delay": 0.1}},
            {"speaker": "agent", "text": "Your delivery failed — can we come tomorrow?", "started_at": t0 + 4,
             "interrupted": True, "metrics": {"e2e_latency": 0.8, "llm_node_ttft": 0.35, "tts_node_ttfb": 0.18}},
            {"speaker": "user", "text": "Yes tomorrow is fine.", "started_at": t0 + 6, "metrics": {}},
            {"speaker": "agent", "text": "Great, see you tomorrow.", "started_at": t0 + 8,
             "metrics": {"e2e_latency": 1.2, "llm_node_ttft": 0.5, "tts_node_ttfb": 0.25}},
        ],
        "tool_invocations": [
            {"tool_name": "lookup_order", "arguments": {"order_id": "1001"}, "status": "timeout",
             "error": "timed out after 5s", "started_at": t0 + 3, "latency_ms": 5001.0}
        ],
    }


def test_agent_crud(client):
    r = client.post("/api/agents", json=AGENT)
    assert r.status_code == 201, r.text
    agent = r.json()

    assert [a["id"] for a in client.get("/api/agents").json()] == [agent["id"]]

    updated = client.put(f"/api/agents/{agent['id']}", json={**AGENT, "name": "Renamed", "max_call_duration_seconds": 60})
    assert updated.json()["name"] == "Renamed" and updated.json()["max_call_duration_seconds"] == 60

    bad = client.put(f"/api/agents/{agent['id']}", json={**AGENT, "tools": [{**AGENT["tools"][0], "name": "end_call"}]})
    assert bad.status_code == 422

    assert client.delete(f"/api/agents/{agent['id']}").status_code == 204
    assert client.get("/api/agents").json() == []
    assert client.get(f"/api/agents/{agent['id']}").status_code == 404


def test_phone_call_requires_trunk_and_valid_number(client):
    agent = client.post("/api/agents", json=AGENT).json()
    r = client.post("/api/calls/phone", json={"agent_id": agent["id"], "to_number": "12", "variables": {}})
    assert r.status_code == 422
    r = client.post("/api/calls/phone", json={"agent_id": agent["id"], "to_number": "9876543210", "variables": {}})
    assert r.status_code == 400 and "SIP_TRUNK_ID" in r.json()["detail"]


def test_call_requires_template_variables(client, fake_dispatch):
    agent = client.post("/api/agents", json=AGENT).json()
    r = client.post("/api/calls/web", json={"agent_id": agent["id"], "variables": {}})
    assert r.status_code == 422 and "customer_name" in r.json()["detail"]


def test_internal_endpoints_require_secret(client, fake_dispatch):
    agent = client.post("/api/agents", json=AGENT).json()
    call = client.post("/api/calls/web", json={"agent_id": agent["id"], "variables": {"customer_name": "Asha"}}).json()
    assert client.get(f"/internal/calls/{call['call_id']}/config").status_code == 401
    cfg = client.get(f"/internal/calls/{call['call_id']}/config", headers=INTERNAL).json()
    assert cfg["agent"]["name"] == "Test agent" and cfg["variables"] == {"customer_name": "Asha"}


def test_full_call_flow_is_idempotent(client, fake_dispatch, fake_llm, receiver):
    received, state = receiver
    state["fail_first"] = 2  # receiver is down for the first two attempts

    agent = client.post("/api/agents", json=AGENT).json()
    call = client.post("/api/calls/web", json={"agent_id": agent["id"], "variables": {"customer_name": "Asha"}}).json()
    call_id = call["call_id"]
    assert call["token"] == "fake-token"

    # Editing the agent mid-call must not change what this call runs with
    client.put(f"/api/agents/{agent['id']}", json={**AGENT, "name": "Edited later"})
    assert client.get(f"/internal/calls/{call_id}/config", headers=INTERNAL).json()["agent"]["name"] == "Test agent"

    t0 = time.time()
    report = _report(t0)
    r1 = client.post(f"/internal/calls/{call_id}/report", json=report, headers=INTERNAL)
    assert r1.json() == {"ok": True, "duplicate": False}
    # Worker retry of the same report (e.g. lost response) — acknowledged, ignored
    r2 = client.post(f"/internal/calls/{call_id}/report", json=report, headers=INTERNAL)
    assert r2.json() == {"ok": True, "duplicate": True}

    detail = wait_for(lambda: (d := client.get(f"/api/calls/{call_id}").json())["webhook_deliveries"]
                      and d["webhook_deliveries"][0]["status"] == "delivered" and d)

    # transcript stored once, with speakers, timestamps and per-turn latency
    assert [t["speaker"] for t in detail["turns"]] == ["agent", "user", "agent", "user", "agent"]
    assert detail["turns"][2]["interrupted"] is True
    assert detail["turns"][2]["v2v_ms"] == 800.0 and detail["turns"][0]["v2v_ms"] is None
    assert detail["avg_v2v_ms"] == 1000.0 and detail["max_v2v_ms"] == 1200.0
    assert detail["duration_seconds"] == 42.0 and detail["status"] == "completed"
    assert detail["tool_invocations"][0]["status"] == "timeout"

    # post-call analysis
    assert detail["outcome"] == "goal_achieved"
    assert detail["extracted_data"] == {"delivery_date": "2026-10-23", "wants_delivery": True, "intent": "reschedule"}
    assert detail["extraction_errors"] == []

    # webhook: one logical event, retried with the same id and body until delivered
    assert len(detail["webhook_deliveries"]) == 1
    delivery = detail["webhook_deliveries"][0]
    assert delivery["attempts"] == 3 and len(received) == 3
    assert {r["headers"]["idempotency-key"] for r in received} == {delivery["event_id"]}
    assert len({r["body"] for r in received}) == 1
    assert received[0]["headers"]["x-vap-signature"] == webhooks.sign(received[0]["body"], "shh")
    body = json.loads(received[0]["body"])
    assert body["event_id"] == delivery["event_id"]
    assert body["outcome"] == "goal_achieved" and len(body["transcript"]) == 5

    # Manual resend: still the same event, not a new one
    client.post(f"/api/webhook-deliveries/{delivery['id']}/resend")
    wait_for(lambda: len(received) == 4)
    assert received[3]["headers"]["idempotency-key"] == delivery["event_id"]
    assert len(client.get(f"/api/calls/{call_id}").json()["webhook_deliveries"]) == 1

    # Reprocessing a done call is a no-op: no second analysis, no new event
    client.post(f"/api/calls/{call_id}/reprocess")
    time.sleep(0.3)
    assert len(client.get(f"/api/calls/{call_id}").json()["webhook_deliveries"]) == 1

    listed = client.get("/api/calls").json()[0]
    assert listed["outcome"] == "goal_achieved" and listed["agent_name"] == "Test agent"


def test_web_call_where_caller_never_joined(client, fake_dispatch, receiver, monkeypatch):
    async def must_not_be_called(prompt):
        raise AssertionError("no LLM call for a call without conversation")

    monkeypatch.setattr(postcall, "gemini_complete_json", must_not_be_called)
    agent = client.post("/api/agents", json=AGENT).json()
    call_id = client.post("/api/calls/web", json={"agent_id": agent["id"], "variables": {"customer_name": "A"}}).json()["call_id"]
    client.post(
        f"/internal/calls/{call_id}/report",
        json={"connected": False, "ended_at": time.time(), "end_reason": "never_connected"},
        headers=INTERNAL,
    )
    detail = wait_for(lambda: (d := client.get(f"/api/calls/{call_id}").json())["postcall_status"] == "done" and d)
    assert detail["status"] == "failed" and detail["outcome"] == "failed"
    assert detail["extracted_data"] == {"delivery_date": None, "wants_delivery": None, "intent": None}
