import json

from conftest import AGENT

from vap.postcall import analyze_call, validate_output
from vap.schemas import AgentConfig

CFG = AgentConfig(**AGENT)


def test_validate_coerces_and_reports_per_field():
    result, errors = validate_output(
        CFG,
        {
            "summary": "Customer agreed to tomorrow.",
            "outcome": "goal_achieved",
            "data": {"delivery_date": "2026-10-23", "wants_delivery": "yes", "intent": "maybe"},
        },
    )
    assert result["data"] == {"delivery_date": "2026-10-23", "wants_delivery": True, "intent": None}
    assert errors == ['data.intent: must be one of: reschedule, refuse (got "maybe")']


def test_validate_bad_outcome_and_missing_required():
    result, errors = validate_output(CFG, {"summary": "x", "outcome": "made_up", "data": {}})
    assert result["outcome"] == "unknown"
    assert any("outcome 'made_up'" in e for e in errors)
    assert "data.delivery_date: required field missing" in errors


def test_validate_non_object():
    result, errors = validate_output(CFG, ["nope"])
    assert result["data"] == {"delivery_date": None, "wants_delivery": None, "intent": None}
    assert errors == ["response was not a JSON object"]


async def test_analyze_retries_once_with_validation_errors():
    prompts = []
    responses = [
        "```json\n" + json.dumps({"summary": "ok", "outcome": "goal_achieved", "data": {"delivery_date": "tomorrow"}}) + "\n```",
        json.dumps({"summary": "ok", "outcome": "goal_achieved", "data": {"delivery_date": "2026-10-23", "intent": "reschedule"}}),
    ]

    async def fake_llm(prompt):
        prompts.append(prompt)
        return responses[len(prompts) - 1]

    out = await analyze_call(CFG, "[00:01] User: tomorrow works", complete_json=fake_llm)
    assert len(prompts) == 2
    assert "failed validation" in prompts[1] and "YYYY-MM-DD" in prompts[1]
    assert out["data"]["delivery_date"] == "2026-10-23"
    assert out["errors"] == []


async def test_analyze_missing_required_alone_does_not_retry():
    calls = []

    async def fake_llm(prompt):
        calls.append(prompt)
        return json.dumps({"summary": "Wrong person.", "outcome": "wrong_person", "data": {}})

    out = await analyze_call(CFG, "[00:01] User: wrong number", complete_json=fake_llm)
    assert len(calls) == 1
    assert out["outcome"] == "wrong_person"
    assert out["errors"] == ["data.delivery_date: required field missing"]
