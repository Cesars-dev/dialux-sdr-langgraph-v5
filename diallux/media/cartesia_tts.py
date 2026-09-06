"""Cartesia Sonic TTS client (raw WebSocket, no SDK).

Protocol (docs.cartesia.ai — /tts/websocket, cartesia_version 2026-08-14):
  - Connect:  wss://api.cartesia.ai/tts/websocket?cartesia_version=<v>
              Auth: `X-API-Key` header (server-side key); falls back to
              `?api_key=` query param if the handshake rejects headers.
  - Send:     {"model_id", "transcript", "voice", "output_format",
              "language", "context_id", "continue", "max_buffer_delay_ms",
              "generation_config"}
  - Recv:     {"type":"chunk","data":<base64 audio>,"done":bool,
              "status_code":int,"step_time":ms,"context_id":...}
  - Cancel:   {"context_id", "cancel": true}          (barge-in)
  - Finish:   last chunk with "continue": false (or empty transcript + false)

Output format is pcm_mulaw @ 8000 Hz — Twilio Media Streams' NATIVE format,
so the base64 `data` field is forwarded to Twilio UNCHANGED (zero transcoding,
zero audio processing on the speak path).

Contexts (per docs): one context per agent turn keeps prosody across the
sentence chunks; `continue: true` on every chunk except the last.

generation_config (docs: Volume, Speed, and Emotion; 2026-08-14):
  - speed   0.6–1.5   (double; default 1.0)
  - volume  0.5–2.0   (double; default 1.0)
  - emotion  one of the documented list, English only, BETA: "guidance, not a
              strict adjustment" — the model still follows the transcript's
              emotional subtext. Best results on emotive-tagged voices.
  Sent per REQUEST (there is no persistent account-level setting), so we
  attach it on every speak() — cheap (2-3 JSON fields).
  Base tuning lives in .env: CARTESIA_SPEED / CARTESIA_VOLUME / CARTESIA_EMOTION.
  V5: speak(overrides=...) from media/delivery.py per-state profiles MERGES
  OVER the .env globals (profile wins for the fields it sets; clamped to the
  documented ranges so the table can never 400 a request).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Awaitable, Callable

import websockets

log = logging.getLogger("diallux.tts")


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))

OnAudio = Callable[[str, str], Awaitable[None]]     # (base64_mulaw_payload, context_id)


class CartesiaTTS:
    def __init__(self, settings, on_audio: OnAudio):
        self.settings = settings
        self.on_audio = on_audio
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self.first_byte_ts: float | None = None

    # ------------------------------------------------------------------ #
    async def connect(self):
        base = f"wss://api.cartesia.ai/tts/websocket?cartesia_version={self.settings.cartesia_version}"
        headers = {"X-API-Key": self.settings.cartesia_api_key}
        try:
            self._ws = await websockets.connect(base, additional_headers=headers, max_size=None, open_timeout=10)
        except Exception:
            # older gateway: key as query param
            fallback = base + f"&api_key={self.settings.cartesia_api_key}"
            self._ws = await websockets.connect(fallback, max_size=None, open_timeout=10)
        self._recv_task = asyncio.create_task(self._recv_loop())
        log.info("cartesia connected (model=%s)", self.settings.cartesia_model_id)

    async def close(self):
        self._closed = True
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    async def speak(self, context_id: str, text: str, continue_: bool,
                    overrides: dict | None = None):
        """Stream one transcript chunk into a context.
        Empty text with continue_=False is the documented way to close a context.
        overrides (V5 delivery profiles): speed/volume/emotion that win over
        the .env globals for this chunk's state."""
        if self._ws is None or self._closed:
            return
        if not text and continue_:
            return
        speed = self.settings.cartesia_speed
        volume = self.settings.cartesia_volume
        emotion = self.settings.cartesia_emotion
        if overrides:
            speed = overrides.get("speed", speed)
            volume = overrides.get("volume", volume)
            emotion = overrides.get("emotion", emotion)
        req = {
            "model_id": self.settings.cartesia_model_id,
            "transcript": text,
            "voice": self.settings.cartesia_voice_id,
            "output_format": {
                "container": "raw",
                "encoding": "pcm_mulaw",
                "sample_rate": self.settings.cartesia_sample_rate,
            },
            "language": self.settings.cartesia_language,
            "context_id": context_id,
            "continue": continue_,
            "max_buffer_delay_ms": self.settings.cartesia_max_buffer_delay_ms
            if self.settings.cartesia_buffering == "custom" else self.settings.cartesia_max_buffer_delay_ms,
        }
        # per-request voice guidance (speed/volume/emotion) — docs: include on
        # EVERY request; there is no persistent setting. Profile overrides
        # (V5) already merged above; clamp so a bad value can never 400.
        gen_cfg: dict = {}
        if speed is not None:
            gen_cfg["speed"] = _clamp(speed, 0.6, 1.5)
        if volume is not None:
            gen_cfg["volume"] = _clamp(volume, 0.5, 2.0)
        if emotion:
            gen_cfg["emotion"] = emotion
        if gen_cfg:
            req["generation_config"] = gen_cfg
        try:
            await self._ws.send(json.dumps(req))
        except Exception as exc:
            log.warning("cartesia send failed: %s", exc)

    async def cancel(self, context_id: str):
        """Barge-in: stop anything not yet generated for this context."""
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(json.dumps({"context_id": context_id, "cancel": True}))
        except Exception as exc:
            log.warning("cartesia cancel failed: %s", exc)

    @staticmethod
    def new_context_id() -> str:
        return str(uuid.uuid4())

    # ------------------------------------------------------------------ #
    async def _recv_loop(self):
        try:
            while not self._closed:
                raw = await self._ws.recv()
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "chunk":
                    if self.first_byte_ts is None:
                        self.first_byte_ts = time.perf_counter()
                    data = msg.get("data")
                    if data:
                        await self.on_audio(data, msg.get("context_id", ""))
                elif mtype == "error":
                    log.error("cartesia error: %s", json.dumps(msg)[:400])
                # timestamps / done / phoneme_timestamps: not needed on the hot path
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self._closed:
                log.error("cartesia recv loop ended: %s", exc)
