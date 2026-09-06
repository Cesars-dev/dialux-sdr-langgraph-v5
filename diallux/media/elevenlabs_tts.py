"""ElevenLabs streaming TTS adapter (raw WebSocket, no SDK).

You asked for the ElevenLabs option ("my go-to choice, we just bypass Retell
entirely, I even have settings"). This is it: same interface as CartesiaTTS,
so CallSession/SentenceGate/barge-in code is provider-agnostic.

Protocol (elevenlabs.io/docs, /v1/text-to-speech/<voice>/stream-input):
  - Connect:  wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input
              ?model_id=<model>&output_format=ulaw_8000&optimize_streaming_latency=N
              Auth: `xi-api-key` header.
              ulaw_8000 = Twilio Media Streams' native format: the base64
              audio frames are forwarded to Twilio UNCHANGED, exactly like
              Cartesia's pcm_mulaw path. Zero transcoding both ways.
  - Send:     {"text": chunk} repeatedly (stream text in as it arrives);
              the first message may carry {"voice_settings": {...}};
              {"text": ""} finalizes the current generation (flush).
  - Recv:     {"audio": <base64>, "isFinal": bool} (+ "inference_details",
              "generationId" — informational).

Context semantics emulation (Cartesia has native contexts; EL does not):
  - continue=true  -> send {"text": chunk} (no flush: generation continues)
  - continue=false -> send {"text": chunk} then {"text": ""} (flush + finalize)
  - cancel(ctx)    -> mark that context dropped (audio swallowed until the
                      next isFinal) + send a flush so the socket returns to a
                      clean state; audio that already reached Twilio is
                      cleared by the `clear` frame the session already sends.
    EL has no server-side "stop generating" on this endpoint; the drop flag
    guarantees nothing cancelled is ever played. The next turn's generation
    reuses the same socket.

Latency notes (why Cartesia stays the default):
  - ElevenLabs Flash (eleven_flash_v2_5) is their low-latency tier; Multilingual
    v2/v3 sounds best (your Chloe settings) but TTFB is higher.
  - PAYG rates (2026): $0.10 / 1k chars (v2/v3), $0.05 / 1k chars (Flash).
    A 5-min call with ~55% agent talk time is ~2.8k chars, roughly $0.14 Flash
    / $0.28 v2 — see DECISIONS.md for the demo-cost table.

V5 per-state delivery profiles (media/delivery.py): speak(overrides=...)
    attaches merged voice_settings (stability/style/speed over the .env base)
    to the FIRST text message of each new context — the same documented slot
    used at connect. Cartesia has native contexts, EL emulates them per turn,
    so a per-turn voice_settings message is the exact semantic equivalent of
    Cartesia's per-request generation_config. Clamped to 0-1 / 0.7-1.2.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Awaitable, Callable

import websockets

log = logging.getLogger("diallux.tts.elevenlabs")


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))

OnAudio = Callable[[str, str], Awaitable[None]]     # (base64_ulaw_payload, context_id)


class ElevenLabsTTS:
    """Drop-in CartesiaTTS replacement over the stream-input WebSocket."""

    def __init__(self, settings, on_audio: OnAudio):
        self.settings = settings
        self.on_audio = on_audio
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self.first_byte_ts: float | None = None
        self._send_lock = asyncio.Lock()
        self._dropped_contexts: set[str] = set()
        self._live_context: str | None = None
        self._generation_open = False      # text sent, flush not yet sent
        self._settings_ctx: str | None = None   # ctx that already carried voice_settings (V5)

    # ------------------------------------------------------------------ #
    async def connect(self):
        url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{self.settings.elevenlabs_voice_id}"
            f"/stream-input?model_id={self.settings.elevenlabs_model_id}"
            f"&output_format=ulaw_8000"
            f"&optimize_streaming_latency={self.settings.elevenlabs_latency_opt}"
        )
        headers = {"xi-api-key": self.settings.elevenlabs_api_key}
        try:
            self._ws = await websockets.connect(url, additional_headers=headers,
                                                max_size=None, open_timeout=10)
        except Exception:
            # some gateways dislike custom headers on upgrade: key as query param
            fallback = url + f"&xi-api-key={self.settings.elevenlabs_api_key}"
            self._ws = await websockets.connect(fallback, max_size=None, open_timeout=10)
        # voice settings ride on the first message (documented slot)
        await self._send_json({
            "text": " ",
            "voice_settings": {
                "stability": self.settings.elevenlabs_stability,
                "similarity_boost": self.settings.elevenlabs_similarity_boost,
                "style": self.settings.elevenlabs_style,
                "use_speaker_boost": True,
                "speed": self.settings.elevenlabs_speed,
            },
        })
        self._recv_task = asyncio.create_task(self._recv_loop())
        log.info("elevenlabs connected (model=%s voice=%s)",
                 self.settings.elevenlabs_model_id, self.settings.elevenlabs_voice_id)

    async def close(self):
        self._closed = True
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _send_json(self, obj: dict):
        if self._ws is None or self._closed:
            return
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(obj))
        except Exception as exc:
            log.warning("elevenlabs send failed: %s", exc)

    # ------------------------------------------------------------------ #
    async def speak(self, context_id: str, text: str, continue_: bool,
                    overrides: dict | None = None):
        """Stream one chunk into the current generation. Empty text with
        continue_=False is the flush that finalizes the generation.
        overrides (V5 delivery profiles): voice_settings for THIS context —
        merged over the .env base and sent with the first text message of
        the context (documented slot; providers that ignore it just use the
        connect-time base, which is the graceful fallback)."""
        if self._ws is None or self._closed:
            return
        if not text and continue_:
            return
        self._live_context = context_id
        self._dropped_contexts.discard(context_id)
        if text:
            if overrides and context_id != self._settings_ctx:
                await self._send_json({
                    "text": text,
                    "voice_settings": self._merged_voice_settings(overrides),
                })
                self._settings_ctx = context_id
            else:
                await self._send_json({"text": text})
            self._generation_open = True
        if not continue_:
            await self._send_json({"text": ""})     # documented flush
            self._generation_open = False

    def _merged_voice_settings(self, overrides: dict) -> dict:
        """Profile fields win over the .env base; clamped to API ranges."""
        s = self.settings
        stability = _clamp(overrides.get("stability", s.elevenlabs_stability), 0.0, 1.0)
        style = _clamp(overrides.get("style", s.elevenlabs_style), 0.0, 1.0)
        speed = _clamp(overrides.get("speed", s.elevenlabs_speed), 0.7, 1.2)
        return {
            "stability": stability,
            "similarity_boost": s.elevenlabs_similarity_boost,
            "style": style,
            "use_speaker_boost": True,
            "speed": speed,
        }

    async def cancel(self, context_id: str):
        """Barge-in: swallow this context's audio and reset the socket."""
        if self._ws is None or self._closed:
            return
        self._dropped_contexts.add(context_id)
        self._live_context = context_id
        if self._generation_open:
            await self._send_json({"text": ""})     # finalize: clean slate
            self._generation_open = False

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
                await self._handle_message(json.loads(raw))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self._closed:
                log.error("elevenlabs recv loop ended: %s", exc)

    async def _handle_message(self, msg: dict):
        """{"audio": b64, "isFinal": bool} frames (test seam + recv loop)."""
        audio = msg.get("audio")
        final = bool(msg.get("isFinal"))
        if audio:
            ctx = self._live_context
            if ctx and ctx in self._dropped_contexts:
                return                          # cancelled turn: swallow
            if self.first_byte_ts is None:
                self.first_byte_ts = time.perf_counter()
            await self.on_audio(audio, ctx or "")
        if final:
            self._dropped_contexts.clear()
