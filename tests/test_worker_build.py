"""The worker builds every provider/tool purely from config. These checks run
the real LiveKit plugin constructors and tool plumbing (no network)."""

import pytest
from conftest import AGENT

from vap.schemas import AgentConfig


@pytest.fixture(autouse=True)
def provider_keys(monkeypatch):
    for k in ("DEEPGRAM_API_KEY", "GOOGLE_API_KEY", "SARVAM_API_KEY", "OPENAI_API_KEY", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.setenv(k, "test")


@pytest.mark.parametrize(
    "overrides, llm_cls, tts_cls",
    [
        ({}, "google", "sarvam"),
        ({"llm_provider": "openai", "llm_model": "gpt-4.1-mini", "tts_provider": "deepgram", "tts_voice": "aura-2-thalia-en"}, "openai", "deepgram"),
        ({"llm_provider": "inference", "llm_model": "openai/gpt-4.1-mini", "tts_provider": "inference",
          "tts_model": "cartesia/sonic-2", "tts_voice": "voice-id"}, "inference", "inference"),
    ],
)
def test_providers_built_from_config(overrides, llm_cls, tts_cls):
    from vap import worker

    cfg = AgentConfig(**{**AGENT, **overrides})
    assert llm_cls in type(worker.build_llm(cfg)).__module__
    assert tts_cls in type(worker.build_tts(cfg)).__module__
    assert "deepgram" in type(worker.build_stt(cfg)).__module__


async def test_webhook_tool_registers_and_receives_arguments(monkeypatch):
    from livekit.agents import RunContext
    from livekit.agents.llm import utils as llm_utils

    from vap import worker

    captured = {}

    async def fake_call(tool, arguments, *, call_id, agent_id):
        captured.update(tool=tool.name, arguments=arguments, call_id=call_id)
        return '{"ok": true}', {"tool_name": tool.name, "status": "ok", "latency_ms": 1.0}

    monkeypatch.setattr(worker, "call_webhook_tool", fake_call)
    cfg = AgentConfig(**AGENT)
    state = worker.CallState()
    tool = worker.make_webhook_tool(cfg.tools[0], "call-1", "agent-1", state)

    agent = worker.PlatformAgent(instructions="x", tools=[tool], state=state, hang_up=None)
    names = {t.info.name for t in agent.tools}
    assert names == {"lookup_order", "end_call"}

    # Same argument preparation the SDK runs when the LLM calls the tool
    class Ctx(RunContext):
        def __init__(self):
            pass

    args, kwargs = llm_utils._prepare_function_arguments(fnc=tool, args_dict={"order_id": "1001"}, call_ctx=Ctx())
    result = await tool(*args, **kwargs)
    assert result == '{"ok": true}'
    assert captured == {"tool": "lookup_order", "arguments": {"order_id": "1001"}, "call_id": "call-1"}
    assert state.tool_invocations[0]["status"] == "ok"
