"""Config -> LiveKit plugin construction, shared by the worker (to run calls)
and the API (to reject a broken config at save time, and to list voices)."""

from livekit.agents import inference
from livekit.plugins import deepgram, google, openai, sarvam
from livekit.plugins.sarvam.tts import MODEL_SPEAKER_COMPATIBILITY as SARVAM_SPEAKERS

from .runtime import sarvam_language, stt_language
from .schemas import AgentConfig


def build_llm(cfg: AgentConfig):
    if cfg.llm_provider == "google":
        kwargs = {}
        # Same fix as the NDR bot: with thinking on, Gemini 2.5 Flash's
        # reasoning leaked into speech and added latency.
        if "flash" in cfg.llm_model:
            kwargs["thinking_config"] = {"thinking_budget": 0}
        return google.LLM(model=cfg.llm_model, temperature=cfg.llm_temperature, **kwargs)
    if cfg.llm_provider == "openai":
        return openai.LLM(model=cfg.llm_model, temperature=cfg.llm_temperature)
    # LiveKit Inference, e.g. "openai/gpt-4.1-mini", "google/gemini-2.5-flash"
    return inference.LLM(model=cfg.llm_model)


def build_tts(cfg: AgentConfig):
    if cfg.tts_provider == "sarvam":
        return sarvam.TTS(
            target_language_code=sarvam_language(cfg.language),
            model=cfg.tts_model or "bulbul:v3",
            speaker=cfg.tts_voice or None,
            min_buffer_size=30,
        )
    if cfg.tts_provider == "deepgram":
        # Deepgram Aura's voice *is* the model name, e.g. aura-2-thalia-en
        return deepgram.TTS(model=cfg.tts_voice or cfg.tts_model)
    return inference.TTS(model=cfg.tts_model, voice=cfg.tts_voice or None)


def build_stt(cfg: AgentConfig):
    return deepgram.STT(model=cfg.stt_model or "nova-3", language=stt_language(cfg.language))


def config_problems(cfg: AgentConfig) -> list[str]:
    """Checks that can only be made against the provider plugins themselves.
    A config that fails these would crash the worker at call start."""
    problems = []
    if cfg.tts_provider == "sarvam":
        model = cfg.tts_model or "bulbul:v3"
        voices = SARVAM_SPEAKERS.get(model, {}).get("all")
        if voices is None:
            problems.append(f"tts_model: unknown Sarvam model '{model}' (known: {', '.join(SARVAM_SPEAKERS)})")
        elif cfg.tts_voice and cfg.tts_voice.lower() not in voices:
            problems.append(f"tts_voice: '{cfg.tts_voice}' is not a {model} voice (choose from: {', '.join(voices)})")
    return problems


def voice_catalog() -> dict:
    return {"sarvam": {model: info["all"] for model, info in SARVAM_SPEAKERS.items()}}
