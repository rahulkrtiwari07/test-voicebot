"""Post-call processing: structured extraction (validated against the agent's
extraction schema), a short summary, and an outcome label — in one LLM call.

The LLM is injected (`complete_json`) so tests and alternate providers don't
need network access. Validation never trusts the model: every field is
type-checked and coerced individually, so one bad field is reported (and
nulled) instead of discarding the whole extraction."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from . import settings
from .schemas import AgentConfig, ExtractionField

logger = logging.getLogger("vap.postcall")

CompleteJSON = Callable[[str], Awaitable[str]]

# Calls that never reached a conversation get an outcome from connectivity
# alone — there's nothing for an LLM to read.
CONNECTIVITY_OUTCOMES = {"no_answer", "busy", "rejected", "failed", "voicemail"}


def system_outcome(cfg: AgentConfig, kind: str) -> str:
    """Outcome for a call decided without an LLM (never connected, voicemail,
    callee never spoke). Uses the agent's own label when it has one with the
    same name, otherwise its "failed" label (e.g. an agent whose labels are
    completed/declined/.../failed gets "failed" for a no-answer), otherwise
    the platform's name for it."""
    labels = {o.label for o in cfg.outcome_labels}
    if kind in labels:
        return kind
    if "failed" in labels:
        return "failed"
    return kind


def _json_type(field: ExtractionField) -> dict[str, Any]:
    if field.type == "enum":
        return {"type": "string", "enum": field.options}
    if field.type == "date":
        return {"type": "string", "description": "ISO date YYYY-MM-DD"}
    if field.type == "string[]":
        return {"type": "array", "items": {"type": "string"}}
    prop: dict[str, Any] = {"type": field.type}
    if field.minimum is not None:
        prop["minimum"] = field.minimum
    if field.maximum is not None:
        prop["maximum"] = field.maximum
    return prop


def output_json_schema(cfg: AgentConfig) -> dict[str, Any]:
    data_props = {}
    for f in cfg.extraction_schema:
        prop = _json_type(f)
        desc = f.description or f.name
        prop["description"] = f"{desc}. Use null if not stated in the call."
        data_props[f.name] = {"anyOf": [prop, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "outcome": {"type": "string", "enum": [o.label for o in cfg.outcome_labels]},
            "data": {"type": "object", "properties": data_props, "required": list(data_props)},
        },
        "required": ["summary", "outcome", "data"],
    }


def build_prompt(cfg: AgentConfig, transcript: str, errors: list[str] | None = None) -> str:
    def bounds(f: ExtractionField) -> str:
        if f.minimum is None and f.maximum is None:
            return ""
        return f", {f.minimum if f.minimum is not None else '-inf'} to {f.maximum if f.maximum is not None else 'inf'}"

    fields = "\n".join(
        f"- {f.name} ({f.type}{': ' + ', '.join(f.options) if f.type == 'enum' else ''}{bounds(f)})"
        f"{' [required]' if f.required else ''}: {f.description}"
        for f in cfg.extraction_schema
    ) or "(none — return an empty object for data)"
    outcomes = "\n".join(f"- {o.label}: {o.description}" for o in cfg.outcome_labels)
    prompt = f"""You analyze transcripts of phone calls made by a voice agent.

Agent's goal (its system prompt):
<<<
{cfg.system_prompt}
>>>

Transcript (Agent = the voice agent, User = the person called; Tool lines are lookups the
agent made and their results — treat them as ground truth when a field compares against them):
<<<
{transcript}
>>>

Return ONLY a JSON object matching this JSON Schema:
{json.dumps(output_json_schema(cfg), ensure_ascii=False)}

- "summary": 1-3 plain sentences on what happened and what was agreed.
- "outcome": exactly one of:
{outcomes}
- "data": these fields, taken only from what was actually said in the call. Use null for anything
  not clearly stated — never guess. Dates must be YYYY-MM-DD.
{fields}
"""
    if errors:
        prompt += (
            "\nYour previous answer failed validation with these errors — fix them:\n"
            + "\n".join(f"- {e}" for e in errors)
        )
    return prompt


def _coerce_field(field: ExtractionField, value: Any) -> Any:
    """Returns the validated value, or raises ValueError with a readable
    reason. None is always allowed here; required-ness is checked separately."""
    value = _coerce_type(field, value)
    if value is not None and field.type in ("integer", "number"):
        if field.minimum is not None and value < field.minimum:
            raise ValueError(f"must be at least {field.minimum:g}")
        if field.maximum is not None and value > field.maximum:
            raise ValueError(f"must be at most {field.maximum:g}")
    return value


def _coerce_type(field: ExtractionField, value: Any) -> Any:
    if value is None:
        return None
    t = field.type
    if t == "string[]":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in value):
            raise ValueError("expected a list of strings")
        return [str(v).strip() for v in value if str(v).strip()]
    if t == "string":
        if isinstance(value, (dict, list)):
            raise ValueError("expected a string")
        return str(value)
    if t == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "yes", "false", "no"):
            return value.strip().lower() in ("true", "yes")
        raise ValueError("expected a boolean")
    if t == "integer":
        if isinstance(value, bool):
            raise ValueError("expected an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        raise ValueError("expected an integer")
    if t == "number":
        if isinstance(value, bool):
            raise ValueError("expected a number")
        if isinstance(value, (int, float)):
            return value
        try:
            return float(str(value).strip())
        except ValueError:
            raise ValueError("expected a number") from None
    if t == "date":
        try:
            return date.fromisoformat(str(value).strip()).isoformat()
        except ValueError:
            raise ValueError("expected a date in YYYY-MM-DD format") from None
    if t == "enum":
        if value in field.options:
            return value
        raise ValueError(f"must be one of: {', '.join(field.options)}")
    raise ValueError(f"unsupported type {t}")


def validate_output(cfg: AgentConfig, raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Validates a parsed LLM response. Returns (clean result, errors). The
    clean result always has every schema field (null when invalid/missing)
    and a valid outcome, so downstream consumers can rely on its shape."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return {"summary": "", "outcome": "unknown", "data": {f.name: None for f in cfg.extraction_schema}}, [
            "response was not a JSON object"
        ]

    labels = {o.label for o in cfg.outcome_labels}
    outcome = raw.get("outcome")
    if outcome not in labels:
        errors.append(f"outcome '{outcome}' is not one of: {', '.join(sorted(labels))}")
        outcome = "unknown"

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a non-empty string")
        summary = ""

    data_in = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    if not isinstance(raw.get("data"), dict):
        errors.append("data must be an object")

    data: dict[str, Any] = {}
    for f in cfg.extraction_schema:
        try:
            data[f.name] = _coerce_field(f, data_in.get(f.name))
        except ValueError as e:
            errors.append(f"data.{f.name}: {e} (got {json.dumps(data_in.get(f.name), ensure_ascii=False)})")
            data[f.name] = None
        if f.required and data[f.name] is None:
            errors.append(f"data.{f.name}: required field missing")

    return {"summary": summary.strip(), "outcome": outcome, "data": data}, errors


def _parse_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


async def analyze_call(
    cfg: AgentConfig, transcript: str, complete_json: CompleteJSON | None = None
) -> dict[str, Any]:
    """Runs extraction + summary + outcome. Retries once with the validation
    errors fed back to the model; whatever still fails after that is
    returned in `errors` alongside the best-effort clean result.

    "Required" fields that are simply missing aren't worth a retry on their
    own — the information may genuinely not have come up in the call — so
    only structural/type errors trigger the repair pass."""
    complete_json = complete_json or gemini_complete_json
    raw_text = await complete_json(build_prompt(cfg, transcript))
    try:
        parsed = _parse_json(raw_text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    result, errors = validate_output(cfg, parsed)

    fixable = [e for e in errors if not e.endswith("required field missing")]
    if fixable:
        raw_text = await complete_json(build_prompt(cfg, transcript, errors))
        try:
            parsed = _parse_json(raw_text)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        result, errors = validate_output(cfg, parsed)

    return {**result, "errors": errors, "raw": raw_text}


async def gemini_complete_json(prompt: str) -> str:
    """Default post-call LLM: Gemini via google-genai (installed with the
    LiveKit google plugin, same GOOGLE_API_KEY the agent uses)."""
    from google import genai
    from google.genai import types

    def _run() -> str:
        client = genai.Client(api_key=settings.env("GOOGLE_API_KEY"))
        resp = client.models.generate_content(
            model=settings.postcall_model(),
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return resp.text or ""

    return await asyncio.to_thread(_run)
