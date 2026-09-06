"""Configuration for the Dialux SDR LangGraph runtime (production build).

V3 iteration (see ITERATIONS.md):
  - RAG knowledge bases on Postgres+pgvector (RAG_MODE=auto; inline fallback)
  - TTS normalization before every spoken sentence (the Retell-native feature)
  - TTS provider switch: Cartesia (default) or ElevenLabs (your Chloe settings)
  - Cartesia generation_config (speed/volume/emotion) per request
  - VOICE OUTPUT RULES appended to the system prompt (Cartesia's voice-agent
    starter rules — ON in production; the deterministic normalizer backs it)
  - openai_temperature defaults to 0.3 (your ask). NOTE: gpt-5.x reasoning
    models only accept the default temperature — the guard in graph/llm.py
    omits it for gpt-5* so the API doesn't 400; set OPENAI_MODEL=gpt-4.1 to
    make 0.3 actually apply.

V5 iteration (see ITERATIONS.md §16):
  - Per-state delivery profiles (media/delivery.py): the state machine
    controls emotion/speed — Cartesia generation_config and ElevenLabs
    voice_settings per turn, driven by the graph state, not the LLM.
    TTS_DELIVERY_PROFILES=false restores exact V4 behavior (.env globals).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- LLM -------------------------------------------------------------
    openai_api_key: str = ""
    openai_base_url: str | None = None          # override for Groq/OpenRouter/etc
    openai_model: str = "gpt-5.2"               # deployed Retell model
    openai_temperature: float | None = 0.3      # gpt-5* guard lives in graph/llm.py
    openai_reasoning_effort: str = "none"        # gpt-5.2: none|low|medium|high ("low" ≈ 0.77s/call; off=default verbosity)
    openai_verbosity: str = "low"               # gpt-5.2 output length control: speech rounds 116→~52 tok
    max_tool_rounds: int = 12                   # engine.py MAX_TOOL_ROUNDS (verbatim)

    # ---- RAG knowledge bases (pgvector) -------------------------------------
    rag_mode: str = "auto"                      # auto | rag | inline
    database_url: str = "postgresql://diallux:diallux@localhost:5432/diallux"
    rag_embedding_model: str = "text-embedding-3-small"
    rag_top_k: int = 3                          # deployed Retell kb_config.top_k
    # Retell's kb_config.filter_score=0.6 uses Retell's internal scorer — NOT
    # portable as raw cosine. Calibrated 2026-09-04 on text-embedding-3-small:
    # relevant queries 0.57-0.62, nonsense 0.24-0.29 → 0.40 sits mid-gap.
    rag_filter_score: float = 0.40
    rag_char_budget: int = 1600                 # chars of excerpts per system prompt

    # ---- Deepgram STT --------------------------------------------------------
    deepgram_api_key: str = ""
    deepgram_mode: str = "flux"                 # "flux" (v2 listen) | "nova3" (v1 listen)
    deepgram_model: str = "flux-general-en"     # or nova-3 when mode=nova3
    deepgram_eot_threshold: float = 0.7         # Flux EndOfTurn confidence
    deepgram_eot_timeout_ms: int = 2500         # silence backstop (sales cadence)
    deepgram_eager_eot_threshold: float | None = None  # off by default (V1 = simple)
    deepgram_endpointing_ms: int = 150          # nova3 fallback endpointing
    deepgram_eager_eot: bool = False            # V2: speculative LLM start on EagerEndOfTurn
    deepgram_reconnect: bool = True             # V2: auto-reconnect the STT socket mid-call
    deepgram_nova_interim: bool = True
    deepgram_keyterms: list[str] | None = None  # e.g. ["Dialux", "Jay"]

    # ---- TTS (provider-agnostic; see media/tts_factory.py) -------------------
    tts_provider: str = "cartesia"              # cartesia | elevenlabs

    # speech normalization (the Retell-native layer, re-implemented):
    tts_normalize: bool = True
    tts_phone_style: str = "digits"             # digits | natural | spell(cartesia)

    # V5: per-state delivery profiles (media/delivery.py) — the graph state
    # decides emotion/speed per turn; false = .env globals only (V4 behavior)
    tts_delivery_profiles: bool = True

    # ---- Cartesia ---------------------------------------------------------
    cartesia_api_key: str = ""
    cartesia_version: str = "2026-08-14"
    cartesia_voice_id: str = "a0e99841-438c-4a64-b679-ae501e7d6091"  # list voices: scripts/list_cartesia_voices.py
    cartesia_model_id: str = "sonic-3.6"
    cartesia_language: str = "en"
    # pcm_mulaw @ 8000 = Twilio Media Streams native format: zero transcoding.
    cartesia_sample_rate: int = 8000
    cartesia_buffering: str = "custom"          # "custom" (our gate, max_buffer_delay_ms=0) | "managed"
    cartesia_max_buffer_delay_ms: int = 0       # custom buffering -> 0
    # generation_config per request (docs: guidance, not strict; emotion is
    # English-only beta; best on emotive-tagged voices — DECISIONS.md)
    cartesia_speed: float | None = None         # 0.6-1.5 (1.05 = slightly brisk sales cadence)
    cartesia_volume: float | None = None        # 0.5-2.0
    cartesia_emotion: str = ""                  # e.g. "calm" / "trust" / "happy"

    # ---- ElevenLabs (optional provider) -----------------------------------
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""               # your Chloe voice id
    elevenlabs_model_id: str = "eleven_flash_v2_5"  # low latency; or eleven_multilingual_v2
    elevenlabs_latency_opt: int = 2             # optimize_streaming_latency 0-4
    elevenlabs_stability: float = 0.55          # Chloe tuning (your settings, env-able)
    elevenlabs_similarity_boost: float = 0.8
    elevenlabs_style: float = 0.15
    elevenlabs_speed: float = 1.0               # 0.7-1.2

    # ---- Telephony (Twilio Media Streams) ------------------------------------
    public_base_url: str = "wss://your-domain.example.com"   # used in TwiML
    twilio_account_sid: str = ""                # provisioning script only
    twilio_auth_token: str = ""                 # provisioning script only

    # ---- Webhook tools (verbatim Retell custom tools) ------------------------
    retell_api_key: str = ""                    # HMAC signing key for slots.diallux-ai.site
    webhook_timeout_s: float = 15.0

    # ---- Langfuse (self-hosted) ----------------------------------------------
    langfuse_enabled: bool = True
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3000"

    # ---- V2 production --------------------------------------------------------
    prompts_dir: str = str(ROOT / "diallux" / "prompts")   # editable source of truth
    checkpoint_backend: str = "memory"          # "memory" | "postgres"
    database_url: str = "postgresql://diallux:diallux@localhost:5432/diallux"
    metrics_enabled: bool = True                # Prometheus /metrics
    hangup_mode: str = "mark"                   # "mark" (playback-confirmed) | "timer"

    # ---- Agent artifact ------------------------------------------------------
    agent_llm_json: str = str(ROOT / "agent" / "llm.json")
    agent_begin_message: str = str(ROOT / "agent" / "begin_message.txt")
    knowledge_base_dir: str = str(ROOT / "agent" / "knowledge_bases")

    # ---- Runtime --------------------------------------------------------------
    http_port: int = 8000
    log_level: str = "info"

    # ---- V2 production --------------------------------------------------------
    prompts_dir: str = str(ROOT / "diallux" / "prompts")   # editable source of truth
    checkpoint_backend: str = "memory"          # "memory" | "postgres"
    metrics_enabled: bool = True                # Prometheus /metrics
    hangup_mode: str = "mark"                   # "mark" (playback-confirmed) | "timer"

    # ---- V3 ---------------------------------------------------------------------
    # VOICE OUTPUT RULES appended to the system prompt (Cartesia voice-agent
    # starter rules) so the model writes TTS-friendly text — ON in production.
    tts_voice_rules: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
