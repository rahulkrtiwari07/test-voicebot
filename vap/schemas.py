"""Agent configuration schema — the single definition of what an agent is.

Everything the worker needs to run a call comes from an AgentConfig, so a new
agent is purely a new row: no code change. The same model validates portal
input (API) and the per-call snapshot the worker receives."""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

FieldType = Literal["string", "number", "integer", "boolean", "date", "enum", "string[]"]
LLMProvider = Literal["google", "openai", "inference"]
TTSProvider = Literal["sarvam", "deepgram", "inference"]

# Tool/field names end up as LLM function names and JSON keys.
_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
# {{name}} or {{tool_name.field}} (the latter filled from a run_at_start tool)
PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)?)\s*\}\}")
# Names the runtime registers itself — a webhook tool can't shadow them.
RESERVED_TOOL_NAMES = {"end_call"}

DEFAULT_OUTCOME_LABELS = [
    {"label": "goal_achieved", "description": "The call's goal was fully met."},
    {"label": "partially_achieved", "description": "Some progress, but the goal was not fully met."},
    {"label": "not_achieved", "description": "The caller engaged but the goal was not met (declined, refused, etc.)."},
    {"label": "callback_requested", "description": "The caller asked to be contacted again later."},
    {"label": "wrong_person", "description": "Wrong number or not the intended person."},
    {"label": "voicemail", "description": "Reached voicemail or an answering machine."},
]


class ExtractionField(BaseModel):
    name: str
    type: FieldType = "string"
    description: str = ""
    required: bool = False
    # Only for type="enum"
    options: list[str] = Field(default_factory=list)
    # Only for type="integer"/"number": inclusive bounds (e.g. a 1-5 rating)
    minimum: float | None = None
    maximum: float | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _IDENT_RE.match(v):
            raise ValueError("must start with a letter/underscore and contain only letters, digits, underscores")
        return v

    @model_validator(mode="after")
    def _enum_needs_options(self):
        if self.type == "enum" and not self.options:
            raise ValueError(f"enum field '{self.name}' needs at least one option")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(f"field '{self.name}': minimum is greater than maximum")
        return self


class WebhookTool(BaseModel):
    name: str
    description: str
    # JSON Schema object for the tool's arguments
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    url: str
    method: Literal["POST", "GET"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    # Run automatically when the call starts (while it's still ringing), with
    # start_arguments rendered from the call's {{variables}}. The result is
    # given to the LLM before its first turn and its top-level fields can be
    # used in the first message as {{tool_name.field}}.
    run_at_start: bool = False
    start_arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _IDENT_RE.match(v):
            raise ValueError("must start with a letter/underscore and contain only letters, digits, underscores")
        if v in RESERVED_TOOL_NAMES:
            raise ValueError(f"'{v}' is reserved by the platform")
        return v

    @field_validator("url")
    @classmethod
    def _valid_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("must be an http(s) URL")
        return v

    @field_validator("parameters")
    @classmethod
    def _valid_parameters(cls, v: dict[str, Any]) -> dict[str, Any]:
        if v.get("type") != "object":
            raise ValueError('parameters must be a JSON Schema with "type": "object"')
        if not isinstance(v.get("properties", {}), dict):
            raise ValueError('"properties" must be an object')
        return v


class OutcomeLabel(BaseModel):
    label: str
    description: str = ""

    @field_validator("label")
    @classmethod
    def _valid_label(cls, v: str) -> str:
        if not _IDENT_RE.match(v):
            raise ValueError("must be a snake_case identifier")
        return v


class AgentConfig(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    system_prompt: str = Field(min_length=1)
    first_message: str = ""
    llm_provider: LLMProvider = "google"
    llm_model: str = "gemini-2.5-flash"
    llm_temperature: float = Field(default=0.4, ge=0, le=2)
    tts_provider: TTSProvider = "sarvam"
    tts_model: str = "bulbul:v3"
    tts_voice: str = "simran"
    # BCP-47-ish; "multi" = code-switched (Deepgram multi + Hindi TTS)
    language: str = "en-IN"
    stt_model: str = "nova-3"
    max_call_duration_seconds: int = Field(default=180, ge=15, le=3600)
    # Natural-language conditions; the LLM calls end_call when any is met
    end_call_conditions: list[str] = Field(default_factory=list)
    extraction_schema: list[ExtractionField] = Field(default_factory=list)
    outcome_labels: list[OutcomeLabel] = Field(
        default_factory=lambda: [OutcomeLabel(**o) for o in DEFAULT_OUTCOME_LABELS]
    )
    tools: list[WebhookTool] = Field(default_factory=list)
    post_call_webhook_url: str | None = None
    # Used to HMAC-sign post-call webhooks so receivers can verify them
    post_call_webhook_secret: str | None = None
    # Answering-machine detection: listen briefly before speaking; a long
    # unbroken greeting is treated as voicemail.
    voicemail_detection: bool = False
    voicemail_action: Literal["hang_up", "leave_message"] = "hang_up"
    voicemail_message: str = ""

    @field_validator("post_call_webhook_url")
    @classmethod
    def _valid_webhook(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if not v.startswith(("http://", "https://")):
            raise ValueError("must be an http(s) URL")
        return v

    @model_validator(mode="after")
    def _unique_names(self):
        for label, items in (
            ("extraction field", [f.name for f in self.extraction_schema]),
            ("tool", [t.name for t in self.tools]),
            ("outcome label", [o.label for o in self.outcome_labels]),
        ):
            dupes = {n for n in items if items.count(n) > 1}
            if dupes:
                raise ValueError(f"duplicate {label} name(s): {', '.join(sorted(dupes))}")
        if not self.outcome_labels:
            raise ValueError("at least one outcome label is required")
        if self.voicemail_detection and self.voicemail_action == "leave_message" and not self.voicemail_message.strip():
            raise ValueError("voicemail_message is required when voicemail_action is leave_message")
        start_tools = {t.name for t in self.tools if t.run_at_start}
        for text in (self.system_prompt, self.first_message, self.voicemail_message):
            for name in PLACEHOLDER_RE.findall(text or ""):
                if "." in name and name.split(".")[0] not in start_tools:
                    raise ValueError(
                        f"{{{{{name}}}}} refers to tool '{name.split('.')[0]}', which is not a run_at_start tool"
                    )
        return self


class StartPhoneCall(BaseModel):
    agent_id: str
    to_number: str
    # Optional per-call values substituted into {{placeholders}} in the
    # system prompt / first message (e.g. customer_name)
    variables: dict[str, str] = Field(default_factory=dict)


class StartWebCall(BaseModel):
    agent_id: str
    variables: dict[str, str] = Field(default_factory=dict)


# --- worker -> API report ---


class TurnReport(BaseModel):
    speaker: Literal["agent", "user"]
    text: str
    started_at: float | None = None
    ended_at: float | None = None
    interrupted: bool = False
    # Raw ChatMessage.metrics from LiveKit (e2e_latency, llm_node_ttft, ...)
    metrics: dict[str, Any] = Field(default_factory=dict)


class ToolInvocationReport(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["ok", "error", "timeout"]
    http_status: int | None = None
    response_excerpt: str | None = None
    error: str | None = None
    started_at: float
    latency_ms: float
    at_start: bool = False


class CallReport(BaseModel):
    connected: bool
    connected_at: float | None = None
    ended_at: float
    end_reason: str
    answered_by: Literal["human", "machine"] | None = None
    turns: list[TurnReport] = Field(default_factory=list)
    tool_invocations: list[ToolInvocationReport] = Field(default_factory=list)


class CallEvent(BaseModel):
    type: Literal["answered", "agent_joined"]
    at: float


class CreateCampaign(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    agent_id: str
    # CSV text: a phone column (phone_number/phone/number/...) plus one column
    # per {{variable}} the agent uses
    csv: str
    concurrency: int = Field(default=2, ge=1, le=20)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_delay_minutes: float = Field(default=60, ge=0, le=7 * 24 * 60)
