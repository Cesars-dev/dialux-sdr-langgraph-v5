"""Deepgram streaming STT client (raw WebSocket, no SDK).

Two backends, env-selectable:

  mode=flux   wss://api.deepgram.com/v2/listen?model=flux-general-en
              Conversational STT with model-integrated end-of-turn:
                TurnInfo.StartOfTurn   -> barge-in signal
                TurnInfo.EndOfTurn     -> user finished; full turn transcript
              (~260ms p50 end-of-turn detection at defaults)

  mode=nova3  wss://api.deepgram.com/v1/listen?model=nova-3
              Classic streaming: interim_results + endpointing(ms) + vad_events.
                Results(speech_final) -> end of utterance
                SpeechStarted         -> barge-in signal

Both take Twilio's native mulaw/8000 audio UNCHANGED (zero transcoding) and
batch 20ms Twilio frames into 80ms chunks (Deepgram's recommended size).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import websockets

log = logging.getLogger("diallux.stt")

OnEOT = Callable[[str], Awaitable[None]]            # end of turn transcript
OnStartOfTurn = Callable[[], Awaitable[None]]       # user started speaking (barge-in)
OnState = Callable[[dict], Awaitable[None]]         # raw protocol event (observability)


class DeepgramSTT:
    FRAME_MS = 80
    BYTES_PER_FRAME = 160 * (FRAME_MS // 20)        # mulaw 8k: 160 bytes / 20ms

    def __init__(self, settings, on_eot: OnEOT, on_start_of_turn: OnStartOfTurn,
                 on_state: OnState | None = None, on_disconnect=None,
                 on_eager=None, on_turn_resumed=None):
        self.settings = settings
        self.on_eot = on_eot
        self.on_start_of_turn = on_start_of_turn
        self.on_state = on_state
        self.on_disconnect = on_disconnect            # V2: mid-call reconnect hook
        self.on_eager = on_eager                      # V2: EagerEndOfTurn (speculative)
        self.on_turn_resumed = on_turn_resumed        # V2: cancel speculative turn
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._buffer = bytearray()
        self._closed = False
        self._final_buffer: list[str] = []          # nova3: accumulate finals until speech_final

    # ------------------------------------------------------------------ #
    def _url(self) -> str:
        s = self.settings
        if s.deepgram_mode == "flux":
            params = {
                "model": s.deepgram_model,
                "encoding": "mulaw",
                "sample_rate": "8000",
                "eot_threshold": str(s.deepgram_eot_threshold),
                "eot_timeout_ms": str(s.deepgram_eot_timeout_ms),
            }
            if s.deepgram_eager_eot_threshold is not None:
                params["eager_eot_threshold"] = str(s.deepgram_eager_eot_threshold)
            if s.deepgram_keyterms:
                params["keyterms"] = ",".join(s.deepgram_keyterms)
            from urllib.parse import urlencode
            return "wss://api.deepgram.com/v2/listen?" + urlencode(params)
        from urllib.parse import urlencode
        return "wss://api.deepgram.com/v1/listen?" + urlencode({
            "model": s.deepgram_model or "nova-3",
            "encoding": "mulaw",
            "sample_rate": "8000",
            "channels": "1",
            "interim_results": "true" if s.deepgram_nova_interim else "false",
            "vad_events": "true",
            "endpointing": str(s.deepgram_endpointing_ms),
            "smart_format": "true",
            "punctuate": "true",
        })

    async def connect(self):
        headers = {"Authorization": f"Token {self.settings.deepgram_api_key}"}
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                self._ws = await websockets.connect(
                    self._url(), additional_headers=headers, max_size=None, open_timeout=10
                )
                break
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(0.5 * (attempt + 1))
        else:
            raise ConnectionError(f"Deepgram connect failed: {last_exc}")
        self._recv_task = asyncio.create_task(self._recv_loop())
        if self.settings.deepgram_mode == "nova3":
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        log.info("deepgram connected (mode=%s)", self.settings.deepgram_mode)

    # ------------------------------------------------------------------ #
    async def send_audio(self, mulaw_bytes: bytes):
        """Queue Twilio mulaw frames; forwarded in 80ms batches."""
        if self._ws is None or self._closed:
            return
        self._buffer.extend(mulaw_bytes)
        while len(self._buffer) >= self.BYTES_PER_FRAME:
            chunk = bytes(self._buffer[: self.BYTES_PER_FRAME])
            del self._buffer[: self.BYTES_PER_FRAME]
            try:
                await self._ws.send(chunk)
            except Exception as exc:
                log.warning("deepgram send failed: %s", exc)

    async def close(self):
        self._closed = True
        for task in (self._recv_task, self._keepalive_task):
            if task:
                task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    async def _keepalive_loop(self):
        try:
            while not self._closed:
                await asyncio.sleep(5.0)
                if self._ws:
                    await self._ws.send(json.dumps({"type": "KeepAlive"}))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("deepgram keepalive stopped: %s", exc)

    async def _recv_loop(self):
        try:
            while not self._closed:
                raw = await self._ws.recv()
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                if self.on_state:
                    await self.on_state(msg)
                await self._handle(msg)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self._closed:
                log.error("deepgram recv loop ended: %s", exc)
                if self.on_disconnect:
                    await self.on_disconnect()

    async def _handle(self, msg: dict):
        mtype = msg.get("type")
        if mtype == "TurnInfo":                      # Flux v2
            event = msg.get("event")
            transcript = (msg.get("transcript") or "").strip()
            if event == "EndOfTurn" and transcript:
                await self.on_eot(transcript)
            elif event == "StartOfTurn":
                await self.on_start_of_turn()
            elif event == "EagerEndOfTurn" and transcript and self.on_eager:
                await self.on_eager(transcript)
            elif event == "TurnResumed" and self.on_turn_resumed:
                await self.on_turn_resumed()
            # Update / EagerEndOfTurn / TurnResumed: ignored in the simple pattern
        elif mtype == "Results":                     # nova3 v1
            if not msg.get("is_final"):
                return
            alt = ((msg.get("channel") or {}).get("alternatives") or [{}])[0]
            text = (alt.get("transcript") or "").strip()
            if text:
                self._final_buffer.append(text)
            if msg.get("speech_final"):
                full = " ".join(self._final_buffer).strip()
                self._final_buffer = []
                if full:
                    await self.on_eot(full)
        elif mtype == "SpeechStarted":               # nova3 VAD -> barge-in
            await self.on_start_of_turn()
        elif mtype == "UtteranceEnd":                # optional hard cut
            full = " ".join(self._final_buffer).strip()
            self._final_buffer = []
            if full:
                await self.on_eot(full)
        elif mtype in ("Metadata",):
            pass
        elif msg.get("type") == "Error" or "error" in str(mtype).lower():
            log.error("deepgram error event: %s", json.dumps(msg)[:400])
