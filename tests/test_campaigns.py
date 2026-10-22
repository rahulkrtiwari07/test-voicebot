"""Campaigns: CSV parsing, concurrency, and the two separate retry budgets."""

import time

import pytest
from conftest import AGENT, wait_for

from vap import calls, campaigns, telephony
from vap.db import CallRow, CampaignContactRow, CampaignRow, session_scope

CSV = """phone_number,customer_name
9876500001,Asha
+919876500002,Ravi
+919876500002,Ravi again
12,Bad Number
+919876500004,
"""


def test_parse_contacts():
    contacts, rejected = campaigns.parse_contacts(CSV, AGENT)
    assert contacts == [
        {"phone_number": "+919876500001", "variables": {"customer_name": "Asha"}},
        {"phone_number": "+919876500002", "variables": {"customer_name": "Ravi"}},
    ]
    reasons = {r["line"]: r["reason"] for r in rejected}
    assert reasons[4] == "duplicate number +919876500002"
    assert "E.164" in reasons[5]
    assert reasons[6] == "missing column value(s): customer_name"

    with pytest.raises(ValueError, match="phone column"):
        campaigns.parse_contacts("name\nx\n", AGENT)


def _setup(sqlite_db, n=3, **kw):
    from vap.db import AgentRow

    with session_scope() as s:
        agent = AgentRow(name="a", config=AGENT)
        s.add(agent)
        s.flush()
        agent_id = agent.id
    csv_text = "phone,customer_name\n" + "\n".join(f"+9198765000{i:02d},C{i}" for i in range(n))
    defaults = dict(concurrency=2, max_attempts=2, retry_delay_seconds=600)
    return campaigns.create_campaign(name="t", agent_id=agent_id, csv_text=csv_text, **{**defaults, **kw})["id"]


def _finalize(call_id, status, answered=False, reason=None):
    with session_scope() as s:
        c = s.get(CallRow, call_id)
        if answered:
            c.answered_at = time.time()
    calls.finalize_without_conversation(call_id, status, reason)
    return campaigns.apply_call_result(call_id)


def _contact(call_id):
    with session_scope() as s:
        c = s.get(CampaignContactRow, s.get(CallRow, call_id).campaign_contact_id)
        return {"status": c.status, "attempts": c.attempts, "infra": c.infra_attempts, "next": c.next_attempt_at}


def test_concurrency_limits(sqlite_db, monkeypatch):
    _setup(sqlite_db, n=5, concurrency=2)
    first = campaigns.claim_due_contacts()
    assert len(first) == 2
    assert campaigns.claim_due_contacts() == []  # campaign limit reached

    monkeypatch.setenv("MAX_CONCURRENT_CALLS", "3")
    _setup(sqlite_db, n=5, concurrency=5)  # second campaign
    assert len(campaigns.claim_due_contacts()) == 1  # only 1 global slot left


def test_no_answer_uses_attempts_and_is_retried_later(sqlite_db):
    _setup(sqlite_db, n=1, max_attempts=2, retry_delay_seconds=600)
    (call_id, _, _), = campaigns.claim_due_contacts()
    assert _finalize(call_id, "no_answer") == "pending"
    c = _contact(call_id)
    assert c["attempts"] == 1 and c["infra"] == 0 and c["next"] > time.time() + 590
    assert campaigns.claim_due_contacts() == []  # not due yet

    (call_id2, _, _), = campaigns.claim_due_contacts(now=time.time() + 601)
    assert _finalize(call_id2, "busy") == "exhausted"  # 2/2 real attempts used
    assert _contact(call_id2)["attempts"] == 2


def test_infra_failures_do_not_use_contact_attempts(sqlite_db):
    campaign_id = _setup(sqlite_db, n=1, max_attempts=1)
    now = time.time()
    for i in range(4):
        (call_id, _, _), = campaigns.claim_due_contacts(now=now + i * 61)
        assert _finalize(call_id, "failed", reason="agent_did_not_join") == "pending"
        c = _contact(call_id)
        assert (c["attempts"], c["infra"]) == (0, i + 1)
    (call_id, _, _), = campaigns.claim_due_contacts(now=now + 4 * 61)
    # A real attempt can still happen after four infra failures
    assert _finalize(call_id, "completed", answered=True) == "done"
    assert _contact(call_id)["attempts"] == 1
    with session_scope() as s:
        assert s.get(CampaignRow, campaign_id).status == "completed"


def test_infra_budget_exhausts(sqlite_db):
    _setup(sqlite_db, n=1)
    now = time.time()
    for i in range(5):
        (call_id, _, _), = campaigns.claim_due_contacts(now=now + i * 61)
        status = _finalize(call_id, "failed", reason="sip_503")
    assert status == "exhausted"


def test_answered_call_that_fails_is_a_real_attempt(sqlite_db):
    _setup(sqlite_db, n=1, max_attempts=3)
    (call_id, _, _), = campaigns.claim_due_contacts()
    # The worker crashed after pickup: the customer's phone did ring.
    assert _finalize(call_id, "failed", answered=True, reason="no_report_from_agent") == "done"
    assert _contact(call_id)["attempts"] == 1


def test_apply_is_idempotent(sqlite_db):
    _setup(sqlite_db, n=1)
    (call_id, _, _), = campaigns.claim_due_contacts()
    assert _finalize(call_id, "no_answer") == "pending"
    assert campaigns.apply_call_result(call_id) is None  # second hook run: no double count
    assert _contact(call_id)["attempts"] == 1


def test_campaign_end_to_end_through_api(client, monkeypatch):
    """Real scheduler loop + finalize hook; telephony faked per number."""
    monkeypatch.setenv("SIP_TRUNK_ID", "ST_test")
    monkeypatch.setattr(campaigns, "SCHEDULER_INTERVAL_SECONDS", 0.05)
    script = {"+919876500001": ["no_answer", "answered"], "+919876500002": ["failed", "no_answer", "no_answer"]}
    dialled = []

    async def fake_place(room, call_id, number):
        dialled.append(number)
        result = script[number].pop(0)
        if result == "answered":
            return "answered", None, None
        reason = "agent_did_not_join" if result == "failed" else "sip_480"
        return result, None, reason

    monkeypatch.setattr(telephony, "place_phone_call", fake_place)
    agent = client.post("/api/agents", json=AGENT).json()
    r = client.post("/api/campaigns", json={
        "name": "e2e", "agent_id": agent["id"], "concurrency": 2, "max_attempts": 2, "retry_delay_minutes": 0,
        "csv": "phone_number,customer_name\n9876500001,Asha\n9876500002,Ravi\n12,bad\n",
    })
    assert r.status_code == 201, r.text
    assert r.json()["accepted"] == 2 and r.json()["rejected"][0]["line"] == 4
    cid = r.json()["id"]
    with session_scope() as s:
        s.get(CampaignRow, cid).infra_retry_delay_seconds = 0

    # Contact 1 answers on its second try; the worker then reports the call.
    answered = wait_for(lambda: next((c for c in client.get("/api/calls?status=in_progress").json()), None), timeout=25)
    t0 = time.time()
    client.post(f"/internal/calls/{answered['id']}/report", headers={"X-Internal-Secret": "test-secret"}, json={
        "connected": True, "connected_at": t0, "ended_at": t0 + 30, "end_reason": "agent_ended:goal_met",
        "turns": []})

    detail = wait_for(lambda: (d := client.get(f"/api/campaigns/{cid}").json())["status"] == "completed" and d, timeout=25)
    by_number = {c["phone_number"]: c for c in detail["contacts"]}
    one, two = by_number["+919876500001"], by_number["+919876500002"]
    assert (one["status"], one["attempts"], one["infra_attempts"]) == ("done", 2, 0)
    # infra failure first: two real no-answers still allowed afterwards
    assert (two["status"], two["attempts"], two["infra_attempts"]) == ("exhausted", 2, 1)
    assert dialled.count("+919876500002") == 3
    assert client.post(f"/api/campaigns/{cid}/pause").status_code == 409  # completed
