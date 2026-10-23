"""The Cynet Health reference-check agent (examples/cynet_reference_check.json)
and the platform features it relies on: start-of-call tools, string[] and
bounded fields, voicemail handling, outcome mapping."""

import json
import time
from datetime import date
from pathlib import Path

import pytest
from conftest import wait_for
from pydantic import ValidationError

from vap import postcall, telephony
from vap.calls import missing_variables
from vap.postcall import system_outcome, validate_output
from vap.runtime import build_instructions, render_template, start_result_variables
from vap.schemas import AgentConfig

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "cynet_reference_check.json"
INTERNAL = {"X-Internal-Secret": "test-secret"}


def reference_config(base="http://test") -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8").replace("$PLATFORM_URL", base))


CFG = AgentConfig(**reference_config())


def test_reference_config_matches_spec():
    fields = {f.name: f for f in CFG.extraction_schema}
    assert list(fields) == [
        "reference_confirmed", "relationship", "employment_dates", "dates_match_claim", "would_rehire",
        "strengths", "improvements", "rating", "callback_requested", "callback_time",
    ]
    assert fields["would_rehire"].options == ["yes", "no", "unsure"]
    assert fields["strengths"].type == "string[]" and fields["improvements"].type == "string[]"
    assert (fields["rating"].type, fields["rating"].minimum, fields["rating"].maximum) == ("integer", 1, 5)
    assert [o.label for o in CFG.outcome_labels] == [
        "completed", "callback_requested", "declined", "wrong_number", "voicemail", "failed"
    ]
    tool = CFG.tools[0]
    assert tool.name == "get_candidate_details" and tool.run_at_start
    assert tool.start_arguments == {"candidate_id": "{{candidate_id}}"}
    # Callers only supply these; {{get_candidate_details.name}} comes from the tool
    assert missing_variables(CFG.model_dump(), {}) == {"reference_name", "candidate_id"}


def test_tool_field_placeholder_must_reference_a_start_tool():
    cfg = reference_config()
    cfg["tools"][0]["run_at_start"] = False
    with pytest.raises(ValidationError, match="not a run_at_start tool"):
        AgentConfig(**cfg)


def test_leave_message_requires_message():
    with pytest.raises(ValidationError, match="voicemail_message is required"):
        AgentConfig(**{**reference_config(), "voicemail_message": " "})


def test_start_tool_result_feeds_first_message_and_instructions():
    result = json.dumps({"ok": True, "result": {"found": True, "name": "Priya Nair", "role": "ICU RN", "years": 3}})
    tvars = {"reference_name": "Dr. Iyer", "candidate_id": "C-1001", **start_result_variables("get_candidate_details", result)}
    assert tvars["get_candidate_details.name"] == "Priya Nair"
    assert "get_candidate_details.found" not in tvars  # booleans aren't spoken values
    first = render_template(CFG.first_message, tvars)
    assert first.startswith("Hi Dr. Iyer, this is Ava calling on behalf of Cynet Health")
    assert "reference check on Priya Nair" in first

    text = build_instructions(
        CFG, {"reference_name": "Dr. Iyer", "candidate_id": "C-1001"}, today=date(2026, 10, 22),
        start_results=[{"name": "get_candidate_details", "arguments": {"candidate_id": "C-1001"}, "result": result}],
    )
    assert "# Data fetched at call start" in text and "Priya Nair" in text
    assert "The person is not Dr. Iyer" in text  # end-call conditions are rendered too


def test_failed_start_tool_leaves_placeholder_for_fallback():
    failed = json.dumps({"ok": False, "error": "The get_candidate_details service did not respond in time."})
    assert start_result_variables("get_candidate_details", failed) == {}
    first = render_template(CFG.first_message, {"reference_name": "Dr. Iyer"})
    assert "{{get_candidate_details.name}}" in first  # worker falls back to an LLM opening


def test_reference_extraction_validation():
    result, errors = validate_output(
        CFG,
        {
            "summary": "Reference completed.",
            "outcome": "completed",
            "data": {
                "reference_confirmed": True, "relationship": "charge nurse", "employment_dates": "2021 to early 2024",
                "dates_match_claim": True, "would_rehire": "yes", "strengths": "calm under pressure",
                "improvements": ["documentation speed", " "], "rating": "4", "callback_requested": False,
                "callback_time": None,
            },
        },
    )
    assert errors == []
    assert result["data"]["strengths"] == ["calm under pressure"]
    assert result["data"]["improvements"] == ["documentation speed"]
    assert result["data"]["rating"] == 4

    _, errors = validate_output(CFG, {"summary": "x", "outcome": "completed",
                                      "data": {"rating": 4.5, "strengths": [{"a": 1}], "reference_confirmed": True,
                                               "callback_requested": False}})
    assert "data.rating: expected an integer (got 4.5)" in errors
    assert any(e.startswith("data.strengths: expected a list of strings") for e in errors)
    _, errors = validate_output(CFG, {"summary": "x", "outcome": "completed",
                                      "data": {"rating": 7, "reference_confirmed": True, "callback_requested": False}})
    assert errors == ["data.rating: must be at most 5 (got 7)"]


def test_system_outcomes_map_to_agent_labels():
    assert system_outcome(CFG, "voicemail") == "voicemail"
    assert system_outcome(CFG, "no_answer") == "failed"  # agent has no "no_answer" label
    assert system_outcome(CFG, "no_conversation") == "failed"
    default = AgentConfig(name="x", system_prompt="y")
    assert system_outcome(default, "no_answer") == "no_answer"


# --- API ------------------------------------------------------------------------------


@pytest.fixture
def fake_dispatch(monkeypatch):
    async def start_web_call(room, call_id):
        return "fake-token"

    monkeypatch.setattr(telephony, "start_web_call", start_web_call)


def test_templates_and_mock_candidate_tool(client):
    templates = {t["id"]: t for t in client.get("/api/templates").json()}
    assert set(templates) == {"cynet_reference_check"}
    tool_url = templates["cynet_reference_check"]["config"]["tools"][0]["url"]
    assert tool_url == "http://testserver/demo/tools/candidate-details"
    for t in templates.values():
        AgentConfig(**t["config"])  # every shipped template is valid

    r = client.post("/demo/tools/candidate-details", json={"arguments": {"candidate_id": "c-1001"}}).json()
    assert r["found"] and r["name"] == "Priya Nair" and r["claimed_start"] == "March 2021"
    assert client.post("/demo/tools/candidate-details", json={"arguments": {"candidate_id": "nope"}}).json()["found"] is False


def _start(client):
    agent = client.post("/api/agents", json=reference_config("http://testserver")).json()
    r = client.post("/api/calls/web", json={"agent_id": agent["id"], "variables": {"reference_name": "Dr. Iyer", "candidate_id": "C-1001"}})
    assert r.status_code == 201, r.text
    return r.json()["call_id"]


def test_voicemail_call_is_labelled_without_llm(client, fake_dispatch, monkeypatch):
    async def must_not_be_called(prompt):
        raise AssertionError("voicemail needs no LLM analysis")

    monkeypatch.setattr(postcall, "gemini_complete_json", must_not_be_called)
    call_id = _start(client)
    t0 = time.time()
    client.post(f"/internal/calls/{call_id}/report", headers=INTERNAL, json={
        "connected": True, "connected_at": t0, "ended_at": t0 + 20, "end_reason": "voicemail_message_left",
        "answered_by": "machine",
        "turns": [{"speaker": "agent", "text": "Hi Dr. Iyer, this is Ava from Cynet Health...", "started_at": t0 + 9}],
    })
    d = wait_for(lambda: (x := client.get(f"/api/calls/{call_id}").json())["postcall_status"] == "done" and x)
    assert d["status"] == "voicemail" and d["answered_by"] == "machine"
    assert d["outcome"] == "voicemail"
    assert d["summary"] == "Reached voicemail; left the configured message."


def test_reference_call_analysis_sees_tool_results(client, fake_dispatch, monkeypatch):
    prompts = []

    async def fake_llm(prompt):
        prompts.append(prompt)
        return json.dumps({"summary": "Completed reference.", "outcome": "completed", "data": {
            "reference_confirmed": True, "relationship": "supervisor", "employment_dates": "2021 to 2024",
            "dates_match_claim": True, "would_rehire": "yes", "strengths": ["clinical judgment"],
            "improvements": [], "rating": 5, "callback_requested": False, "callback_time": None}})

    monkeypatch.setattr(postcall, "gemini_complete_json", fake_llm)
    call_id = _start(client)
    t0 = time.time()
    client.post(f"/internal/calls/{call_id}/report", headers=INTERNAL, json={
        "connected": True, "connected_at": t0, "ended_at": t0 + 90, "end_reason": "agent_ended:goal_met",
        "answered_by": "human",
        "turns": [
            {"speaker": "agent", "text": "Hi Dr. Iyer ... reference check on Priya Nair ...", "started_at": t0 + 1},
            {"speaker": "user", "text": "Sure, I was her supervisor from 2021 to 2024.", "started_at": t0 + 6},
        ],
        "tool_invocations": [{
            "tool_name": "get_candidate_details", "arguments": {"candidate_id": "C-1001"}, "status": "ok",
            "response_excerpt": '{"name": "Priya Nair", "claimed_start": "March 2021", "claimed_end": "January 2024"}',
            "started_at": t0 - 3, "latency_ms": 80.0, "at_start": True}],
    })
    d = wait_for(lambda: (x := client.get(f"/api/calls/{call_id}").json())["postcall_status"] == "done" and x)
    assert d["outcome"] == "completed" and d["extracted_data"]["strengths"] == ["clinical judgment"]
    assert d["tool_invocations"][0]["at_start"] is True
    assert "Tool get_candidate_details" in prompts[0] and "claimed_start" in prompts[0]
    # Tool line (before pickup) sorts first in the transcript
    transcript = prompts[0].split("<<<")[2]
    assert transcript.index("Tool get_candidate_details") < transcript.index("Agent:")


def test_incompatible_voice_is_rejected_at_save(client):
    cfg = {**reference_config(), "tts_model": "bulbul:v3", "tts_voice": "anushka"}
    r = client.post("/api/agents", json=cfg)
    assert r.status_code == 422 and "'anushka' is not a bulbul:v3 voice" in r.json()["detail"]
    assert "priya" in client.get("/api/voices").json()["sarvam"]["bulbul:v3"]
