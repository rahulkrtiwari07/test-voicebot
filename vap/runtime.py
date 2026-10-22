"""Pure helpers for the call runtime — no LiveKit imports, so everything that
decides *what* a call does from its config is unit-testable without a room.
The worker (worker.py) only wires these into a live AgentSession."""

import json
import time
from datetime import date, timedelta
from typing import Any

import httpx

from .schemas import PLACEHOLDER_RE as _PLACEHOLDER_RE
from .schemas import AgentConfig, WebhookTool

# Max characters of a tool response handed back to the LLM — enough for any
# realistic lookup result, small enough not to blow up the context or latency.
TOOL_RESULT_MAX_CHARS = 4000


def placeholders(*texts: str) -> set[str]:
    return {m.group(1) for t in texts for m in _PLACEHOLDER_RE.finditer(t or "")}


def render_template(text: str, variables: dict[str, str]) -> str:
    """Substitutes {{name}} placeholders with per-call variables. Unknown
    placeholders are left visible rather than silently blanked, so a missing
    variable shows up in the transcript/testing instead of producing a
    grammatical-but-wrong sentence."""
    return _PLACEHOLDER_RE.sub(lambda m: str(variables.get(m.group(1), m.group(0))), text)


# language -> (Deepgram STT language, Sarvam TTS target_language_code)
_LANGUAGE_MAP = {
    "en-IN": ("en-IN", "en-IN"),
    "en-US": ("en-US", "en-IN"),
    "en-GB": ("en-GB", "en-IN"),
    "hi-IN": ("hi", "hi-IN"),
    "multi": ("multi", "hi-IN"),
}


def stt_language(language: str) -> str:
    return _LANGUAGE_MAP.get(language, (language, language))[0]


def sarvam_language(language: str) -> str:
    return _LANGUAGE_MAP.get(language, (language, language))[1]


def start_result_variables(tool_name: str, result_text: str) -> dict[str, str]:
    """Top-level scalar fields of a successful run_at_start tool result, as
    {{tool_name.field}} template variables. A failed call yields nothing, so
    templates that need it stay unresolved and the worker falls back to an
    LLM-generated opening (see worker.py)."""
    try:
        data = json.loads(result_text)
    except ValueError:
        return {}
    result = data.get("result") if isinstance(data, dict) and data.get("ok") else None
    if not isinstance(result, dict):
        return {}
    return {
        f"{tool_name}.{k}": str(v)
        for k, v in result.items()
        if isinstance(v, (str, int, float)) and not isinstance(v, bool)
    }


def build_instructions(
    cfg: AgentConfig,
    variables: dict[str, str],
    today: date | None = None,
    start_results: list[dict] | None = None,
) -> str:
    """The agent's own system prompt plus the platform-level rules every
    agent gets: voice output format, when/how to end the call, and how to
    treat tool results/failures. start_results are the run_at_start tool
    calls already made for this call ({name, arguments, result})."""
    today = today or date.today()
    dates = "\n".join(
        f"- {(today + timedelta(days=i)).strftime('%A, %d %B %Y')} ({(today + timedelta(days=i)).isoformat()})"
        + {0: " - today", 1: " - tomorrow", 2: " - day after tomorrow"}.get(i, "")
        for i in range(7)
    )

    sections = [render_template(cfg.system_prompt, variables).strip()]

    if variables:
        sections.append(
            "# Call context\n" + "\n".join(f"- {k}: {v}" for k, v in variables.items())
        )

    sections.append(f"# Dates\nUse this table to resolve relative dates; never guess.\n{dates}")

    end_rules = [
        "When the conversation's goal is met, or any condition below applies, call the `end_call` "
        "tool. Put your final goodbye in its `closing_message` (it is spoken verbatim, then the "
        "line is hung up) — do not say goodbye in a normal reply first.",
        "Never call `end_call` in the same turn you ask the caller a question; wait for their answer.",
        "If the caller says goodbye or that they need nothing else, call `end_call` right away — "
        "answer any last question inside `closing_message`; do not ask whether they need anything else.",
    ]
    if cfg.end_call_conditions:
        end_rules.append(
            "End the call when any of these is true:\n"
            + "\n".join(f"  - {render_template(c, variables)}" for c in cfg.end_call_conditions)
        )
    sections.append("# Ending the call\n" + "\n".join(f"- {r}" for r in end_rules))

    if cfg.tools:
        sections.append(
            "# Tools\n"
            "- Use a tool whenever you need information or an action it provides; use its result in "
            "your next reply.\n"
            "- If a tool result has \"ok\": false, the lookup failed: briefly apologize, never invent "
            "the missing information, and continue (offer an alternative, a callback, or move on). "
            "Do not retry the same tool more than once."
        )

    if start_results:
        sections.append(
            "# Data fetched at call start\n"
            "These tools were already called for you before the call connected — use the results; "
            "do not call them again for the same arguments.\n"
            + "\n".join(
                f"- {r['name']}({json.dumps(r['arguments'], ensure_ascii=False)}) returned: {r['result']}"
                for r in start_results
            )
        )

    sections.append(
        "# Voice output\n"
        "You are speaking on a live phone call. Reply in plain conversational sentences — no "
        "markdown, lists, emojis or symbols. Keep replies short (one or two sentences). Never "
        "narrate your own actions or reasoning."
    )
    return "\n\n".join(sections)


def _truncate(text: str, limit: int = TOOL_RESULT_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


async def call_webhook_tool(
    tool: WebhookTool,
    arguments: dict[str, Any],
    *,
    call_id: str,
    agent_id: str,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, dict[str, Any]]:
    """Calls one configured webhook tool. Never raises: every failure mode
    (timeout, connection error, non-2xx, bad body) becomes a structured
    {"ok": false, ...} result the LLM is instructed to recover from (see
    build_instructions), so a broken tool degrades the conversation instead
    of killing the call.

    Returns (text handed to the LLM, invocation log record)."""
    started = time.time()
    record: dict[str, Any] = {
        "tool_name": tool.name,
        "arguments": arguments,
        "started_at": started,
        "http_status": None,
        "response_excerpt": None,
        "error": None,
    }
    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        if tool.method == "GET":
            resp = await client.get(
                tool.url,
                params={k: v if isinstance(v, str) else json.dumps(v) for k, v in arguments.items()},
                headers=tool.headers,
                timeout=tool.timeout_seconds,
            )
        else:
            resp = await client.post(
                tool.url,
                json={"tool": tool.name, "arguments": arguments, "call_id": call_id, "agent_id": agent_id},
                headers=tool.headers,
                timeout=tool.timeout_seconds,
            )
        record["http_status"] = resp.status_code
        body = resp.text
        record["response_excerpt"] = _truncate(body, 2000)
        if resp.is_success:
            record["status"] = "ok"
            try:
                data = resp.json()
            except ValueError:
                data = body
            result = json.dumps({"ok": True, "result": data}, ensure_ascii=False)
        else:
            record["status"] = "error"
            record["error"] = f"HTTP {resp.status_code}"
            result = json.dumps(
                {"ok": False, "error": f"The {tool.name} service returned an error (HTTP {resp.status_code})."}
            )
    except httpx.TimeoutException:
        record["status"] = "timeout"
        record["error"] = f"timed out after {tool.timeout_seconds:g}s"
        result = json.dumps({"ok": False, "error": f"The {tool.name} service did not respond in time."})
    except Exception as e:  # connection refused, DNS, invalid URL, ...
        record["status"] = "error"
        record["error"] = f"{type(e).__name__}: {e}"[:500]
        result = json.dumps({"ok": False, "error": f"The {tool.name} service is unavailable."})
    finally:
        if owns_client:
            await client.aclose()
        record["latency_ms"] = round((time.time() - started) * 1000, 1)
    return _truncate(result), record


def _ms(value: Any) -> float | None:
    return round(float(value) * 1000, 1) if isinstance(value, (int, float)) and value >= 0 else None


def turn_latencies(speaker: str, metrics: dict[str, Any]) -> dict[str, float | None]:
    """Per-turn latency columns from LiveKit's ChatMessage.metrics.
    e2e_latency is LiveKit's own measurement of end-of-user-speech to the
    agent starting to speak — exactly the voice-to-voice latency we report."""
    if speaker == "agent":
        return {
            "v2v_ms": _ms(metrics.get("e2e_latency")),
            "llm_ttft_ms": _ms(metrics.get("llm_node_ttft")),
            "tts_ttfb_ms": _ms(metrics.get("tts_node_ttfb")),
        }
    return {
        "end_of_turn_ms": _ms(metrics.get("end_of_turn_delay")),
        "transcription_ms": _ms(metrics.get("transcription_delay")),
    }


def voice_to_voice_ms(turns: list[tuple[str, dict]]) -> list[float | None]:
    """Per-turn voice-to-voice latency for (speaker, metrics) turns in order:
    for each agent turn that answers user speech, agent started_speaking_at
    minus the latest user stopped_speaking_at since the previous agent turn.

    That's the same definition as LiveKit's own e2e_latency (used when
    present), but LiveKit only computes it from the *last* user message, and
    it sometimes splits one utterance into two messages where the second has
    no stopped_speaking_at — leaving e2e_latency empty for that reply."""
    out: list[float | None] = []
    last_user_stop: float | None = None
    for speaker, m in turns:
        if speaker == "user":
            stop = m.get("stopped_speaking_at")
            if isinstance(stop, (int, float)):
                last_user_stop = max(stop, last_user_stop or 0)
            out.append(None)
            continue
        if m.get("unprompted"):  # a check-in/wrap-up the platform triggered, not a reply
            out.append(None)
            last_user_stop = None
            continue
        value = _ms(m.get("e2e_latency"))
        start = m.get("started_speaking_at")
        if value is None and last_user_stop is not None and isinstance(start, (int, float)) and start >= last_user_stop:
            value = round((start - last_user_stop) * 1000, 1)
        out.append(value)
        last_user_stop = None  # the next reply must answer new user speech
    return out


def summarize_v2v(values: list[float | None]) -> tuple[float | None, float | None]:
    """(average, max) voice-to-voice latency over the agent turns that had one
    — the greeting and tool-only turns have no preceding user speech."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None, None
    return round(sum(vals) / len(vals), 1), max(vals)
