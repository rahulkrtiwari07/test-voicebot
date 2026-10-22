import os
import time

import pytest

import vap.settings  # noqa: F401  — loads .env.local first, so the overrides below win

# Tests never touch the real trunk, internal secret or database
os.environ["INTERNAL_API_SECRET"] = "test-secret"
os.environ.pop("SIP_TRUNK_ID", None)


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    from vap import db

    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    db.init_engine(url)
    yield url


@pytest.fixture
def client(sqlite_db):
    from fastapi.testclient import TestClient

    from vap.api import app

    with TestClient(app) as c:
        yield c


def wait_for(predicate, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


AGENT = {
    "name": "Test agent",
    "system_prompt": "You are calling {{customer_name}} to confirm a delivery date.",
    "first_message": "Hi {{customer_name}}!",
    "language": "en-IN",
    "max_call_duration_seconds": 120,
    "end_call_conditions": ["A delivery date is confirmed"],
    "extraction_schema": [
        {"name": "delivery_date", "type": "date", "description": "Agreed date", "required": True},
        {"name": "wants_delivery", "type": "boolean", "description": "Still wants it"},
        {"name": "intent", "type": "enum", "description": "Intent", "options": ["reschedule", "refuse"]},
    ],
    "tools": [
        {
            "name": "lookup_order",
            "description": "Look up an order",
            "url": "http://tools.test/lookup",
            "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
        }
    ],
    "post_call_webhook_url": "http://receiver.test/hook",
    "post_call_webhook_secret": "shh",
}
