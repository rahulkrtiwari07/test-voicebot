"""Database models and session handling (SQLAlchemy 2.0).

Postgres in normal use (docker-compose.yml); the tests run against SQLite,
so only portable column types are used. Timestamps are stored as epoch
seconds (Float): every producer (LiveKit's ChatMessage.created_at/metrics,
the worker's report, the API) already deals in time.time(), and it keeps
latency arithmetic trivial across both databases."""

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from . import settings


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class AgentRow(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    # The full AgentConfig (see schemas.py) — the one source of truth for
    # how calls with this agent behave.
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    # Soft delete: past calls keep pointing at a real row, and their own
    # agent_snapshot is what they render from anyway.
    deleted_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class CallRow(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    agent_name: Mapped[str] = mapped_column(String(120))
    # Config frozen at call start, so editing an agent mid-call (or later)
    # never changes what a past call ran with or how it's post-processed.
    agent_snapshot: Mapped[dict] = mapped_column(JSON)
    variables: Mapped[dict] = mapped_column(JSON, default=dict)
    mode: Mapped[str] = mapped_column(String(10))  # "phone" | "web"
    to_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    room: Mapped[str] = mapped_column(String(80))
    # queued -> dialing -> in_progress -> completed | voicemail
    #                   \-> no_answer | busy | rejected | failed
    status: Mapped[str] = mapped_column(String(20), default="queued")
    end_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    sip_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # "human" | "machine" (voicemail detection) | None (detection off / never connected)
    answered_by: Mapped[str | None] = mapped_column(String(10), nullable=True)
    campaign_contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaign_contacts.id"), nullable=True, index=True
    )

    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    answered_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    ended_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Set exactly once — the guard that makes the worker's report idempotent.
    finalized_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    avg_v2v_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_v2v_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    # pending -> processing -> done | failed | skipped
    postcall_status: Mapped[str] = mapped_column(String(20), default="pending")
    postcall_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extracted_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    extraction_errors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    extraction_raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    turns: Mapped[list["TurnRow"]] = relationship(
        back_populates="call", order_by="TurnRow.idx", cascade="all, delete-orphan"
    )
    tool_invocations: Mapped[list["ToolInvocationRow"]] = relationship(
        back_populates="call", order_by="ToolInvocationRow.started_at", cascade="all, delete-orphan"
    )
    webhook_deliveries: Mapped[list["WebhookDeliveryRow"]] = relationship(
        back_populates="call", cascade="all, delete-orphan"
    )


class TurnRow(Base):
    __tablename__ = "transcript_turns"
    __table_args__ = (UniqueConstraint("call_id", "idx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(10))  # "agent" | "user"
    text: Mapped[str] = mapped_column(Text)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    ended_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    # Agent turns only: end of the user's speech -> first agent audio
    v2v_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_ttft_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    tts_ttfb_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # User turns only
    end_of_turn_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    transcription_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    call: Mapped[CallRow] = relationship(back_populates="turns")


class ToolInvocationRow(Base):
    __tablename__ = "tool_invocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(10))  # ok | error | timeout
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[float] = mapped_column(Float)
    latency_ms: Mapped[float] = mapped_column(Float)
    # Called by the platform at call start (run_at_start), not by the LLM
    at_start: Mapped[bool] = mapped_column(Boolean, default=False)

    call: Mapped[CallRow] = relationship(back_populates="tool_invocations")


class CampaignRow(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    # running -> paused <-> running -> completed
    status: Mapped[str] = mapped_column(String(12), default="running")
    concurrency: Mapped[int] = mapped_column(Integer, default=2)
    # Real dial attempts per contact (no-answer/busy/rejected/voicemail count)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    retry_delay_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    # Separate, larger budget for our own failures (agent never joined,
    # trunk/carrier 5xx, worker crash before answer) — never charged to the
    # contact's max_attempts. Same split ndr-api makes with dial_attempted.
    max_infra_attempts: Mapped[int] = mapped_column(Integer, default=5)
    infra_retry_delay_seconds: Mapped[int] = mapped_column(Integer, default=60)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    contacts: Mapped[list["CampaignContactRow"]] = relationship(
        back_populates="campaign", order_by="CampaignContactRow.id", cascade="all, delete-orphan"
    )


class CampaignContactRow(Base):
    __tablename__ = "campaign_contacts"
    __table_args__ = (UniqueConstraint("campaign_id", "phone_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    phone_number: Mapped[str] = mapped_column(String(32))
    variables: Mapped[dict] = mapped_column(JSON, default=dict)
    # pending (due at next_attempt_at) -> calling -> pending (retry) | done | exhausted
    status: Mapped[str] = mapped_column(String(12), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    infra_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[float] = mapped_column(Float, default=time.time)
    last_call_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_result: Mapped[str | None] = mapped_column(String(40), nullable=True)

    campaign: Mapped[CampaignRow] = relationship(back_populates="contacts")


class WebhookDeliveryRow(Base):
    """One row per (call, event) — never per attempt. The unique constraint
    is what makes it impossible for a retry, a worker re-report, a restart
    recovery or a manual resend to create a second delivery: they all find
    and reuse this row, and its fixed event_id/payload."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("call_id", "event_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    event_id: Mapped[str] = mapped_column(String(36), unique=True, default=new_id)
    url: Mapped[str] = mapped_column(Text)
    # Serialized once, sent byte-identical on every attempt (stable signature)
    payload: Mapped[str] = mapped_column(Text)
    # pending -> delivered | failed (failed = gave up; can be resent manually)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    delivered_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)

    call: Mapped[CallRow] = relationship(back_populates="webhook_deliveries")


_engine = None
_SessionLocal: sessionmaker | None = None


def init_engine(url: str | None = None):
    """(Re)binds the module to a database. Called on API startup and by tests."""
    global _engine, _SessionLocal
    url = url or settings.database_url()
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    _engine = create_engine(url, **kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    Base.metadata.create_all(_engine)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        init_engine()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
