"""Environment-driven settings shared by the API and the worker.

Read through functions (not module constants) so tests can override the
environment, and so the worker sees values loaded by load_dotenv() regardless
of import order (the same import-order trap src/db.py in the NDR bot works
around)."""

import os

from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv(".env")


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def database_url() -> str:
    return env("DATABASE_URL", "postgresql://vap:vap_local_dev@localhost:5434/vap")


def internal_secret() -> str:
    return env("INTERNAL_API_SECRET", "change-me")


def platform_api_url() -> str:
    return env("PLATFORM_API_URL", "http://localhost:8080").rstrip("/")


def agent_name() -> str:
    return env("AGENT_NAME", "vap-agent")


def sip_trunk_id() -> str | None:
    return env("SIP_TRUNK_ID")


def postcall_model() -> str:
    return env("POSTCALL_MODEL", "gemini-2.5-flash")


def max_concurrent_calls() -> int:
    """Global cap on simultaneous outbound phone calls started by campaigns.
    LiveKit Cloud's Build plan allows 5 concurrent sessions; 4 leaves room
    for a manual test call (same reasoning as ndr-api)."""
    return int(env("MAX_CONCURRENT_CALLS", "4"))
