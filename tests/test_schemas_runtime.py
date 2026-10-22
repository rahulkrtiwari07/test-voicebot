import json
from datetime import date

import httpx
import pytest
from conftest import AGENT
from pydantic import ValidationError

from vap.runtime import (
    build_instructions,
    call_webhook_tool,
    placeholders,
    render_template,
    summarize_v2v,
    turn_latencies,
)
from vap.schemas import AgentConfig, WebhookTool

# --- config validation --------------------------------------------------------


def test_valid_agent_config_round_trips():
    cfg = AgentConfig(**AGENT)
    assert cfg.tools[0].name == "lookup_order"
    assert {o.label for o in cfg.outcome_labels} >= {"goal_achieved", "not_achieved"}


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"tools": [{**AGENT["tools"][0], "name": "end_call"}]}, "reserved"),
        ({"tools": [{**AGENT["tools"][0], "name": "bad name"}]}, "letters"),
        ({"tools": [{**AGENT["tools"][0], "url": "ftp://x"}]}, "http"),
        ({"tools": [{**AGENT["tools"][0], "parameters": {"type": "string"}}]}, "object"),
        ({"extraction_schema": [{"name": "x", "type": "enum"}]}, "option"),
        ({"extraction_schema": [{"name": "a"}, {"name": "a"}]}, "duplicate"),
        ({"outcome_labels": []}, "outcome"),
        ({"max_call_duration_seconds": 5}, "greater"),
    ],
)
def test_invalid_agent_configs_are_rejected(patch, message):
    with pytest.raises(ValidationError) as exc:
        AgentConfig(**{**AGENT, **patch})
    assert message in str(exc.value)


# --- templating / instructions ---------------------------------------------------


def test_render_template_and_placeholders():
    assert placeholders("Hi {{ name }}", "{{awb}} and {{name}}") == {"name", "awb"}
    assert render_template("Hi {{name}}, {{unknown}}", {"name": "Asha"}) == "Hi Asha, {{unknown}}"


def test_build_instructions_includes_platform_rules():
    cfg = AgentConfig(**AGENT)
    text = build_instructions(cfg, {"customer_name": "Asha"}, today=date(2026, 10, 22))
    assert "calling Asha" in text
    assert "A delivery date is confirmed" in text
    assert "end_call" in text
    assert '"ok": false' in text  # tool-failure recovery guidance
    assert "2026-10-23) - tomorrow" in text


# --- webhook tools -------------------------------------------------------------------


def _tool(**kw):
    return WebhookTool(**{**AGENT["tools"][0], **kw})


async def _run(tool, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await call_webhook_tool(tool, {"order_id": "1001"}, call_id="c1", agent_id="a1", client=client)


async def test_tool_success_passes_result_to_llm():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "out_for_delivery"})

    result, record = await _run(_tool(), handler)
    assert json.loads(result) == {"ok": True, "result": {"status": "out_for_delivery"}}
    assert seen["body"] == {"tool": "lookup_order", "arguments": {"order_id": "1001"}, "call_id": "c1", "agent_id": "a1"}
    assert record["status"] == "ok" and record["http_status"] == 200


async def test_tool_http_error_is_recoverable():
    result, record = await _run(_tool(), lambda r: httpx.Response(500, text="boom"))
    assert json.loads(result)["ok"] is False
    assert record["status"] == "error" and record["error"] == "HTTP 500"


async def test_tool_timeout_is_recoverable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    result, record = await _run(_tool(timeout_seconds=1), handler)
    assert json.loads(result) == {"ok": False, "error": "The lookup_order service did not respond in time."}
    assert record["status"] == "timeout"


async def test_tool_connection_error_is_recoverable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    result, record = await _run(_tool(), handler)
    assert json.loads(result)["ok"] is False and record["status"] == "error"


async def test_get_tool_sends_query_params():
    def handler(request):
        assert request.method == "GET" and request.url.params["order_id"] == "1001"
        return httpx.Response(200, text="plain text answer")

    result, _ = await _run(_tool(method="GET"), handler)
    assert json.loads(result) == {"ok": True, "result": "plain text answer"}


# --- latency ------------------------------------------------------------------------------


def test_turn_latencies_and_summary():
    agent = turn_latencies("agent", {"e2e_latency": 0.9123, "llm_node_ttft": 0.4, "tts_node_ttfb": 0.2})
    assert agent == {"v2v_ms": 912.3, "llm_ttft_ms": 400.0, "tts_ttfb_ms": 200.0}
    user = turn_latencies("user", {"end_of_turn_delay": 0.3, "transcription_delay": 0.1})
    assert user == {"end_of_turn_ms": 300.0, "transcription_ms": 100.0}
    assert summarize_v2v([None, 800.0, 1200.0]) == (1000.0, 1200.0)
    assert summarize_v2v([None]) == (None, None)


def test_voice_to_voice_survives_split_user_turns():
    from vap.runtime import voice_to_voice_ms

    turns = [
        ("agent", {"started_speaking_at": 100.0}),                                 # greeting: no user speech yet
        ("user", {"started_speaking_at": 103.0, "stopped_speaking_at": 105.0}),
        ("user", {"on_user_turn_completed_delay": 0.0}),                           # split fragment, no timestamps
        ("agent", {"started_speaking_at": 106.2}),                                 # SDK left e2e_latency empty
        ("user", {"started_speaking_at": 110.0, "stopped_speaking_at": 111.0}),
        ("agent", {"started_speaking_at": 131.0, "unprompted": 1}),                # silence check-in: not a reply
        ("user", {"started_speaking_at": 121.0, "stopped_speaking_at": 122.0}),
        ("agent", {"started_speaking_at": 123.0, "e2e_latency": 0.95}),            # SDK value wins when present
    ]
    assert voice_to_voice_ms(turns) == [None, None, None, 1200.0, None, None, None, 950.0]
