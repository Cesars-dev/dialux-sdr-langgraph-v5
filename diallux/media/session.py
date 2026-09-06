"""CallSession — V3 production orchestration.

V1 flow (Twilio -> Deepgram -> LangGraph -> gate -> Cartesia -> Twilio) plus:

  - mark-based hangup: after end_call audio, a `mark` frame confirms Twilio
    finished PLAYING the goodbye before we close the stream (timer fallback)
  - STT auto-reconnect: Deepgram socket death mid-call transparently reconnects
  - buffer-overflow watchdog: Twilio warning 31931 (media discarded) is logged
    + counted, not silently ignored
  - eager end-of-turn (optional): EagerEndOfTurn starts the LLM speculatively;
    TurnResumed cancels it (Deepgram guarantees the eager transcript equals
    the final EndOfTurn transcript when no resume happens)
  - Prometheus metrics per turn + per call (observability/metrics.py)
  - V3: provider-agnostic TTS (media/tts_factory.py — Cartesia OR ElevenLabs,
    both native ulaw/mulaw @ 8000) and deterministic speech normalization on
    every spoken sentence (media/normalize.py — the Retell-native layer).
  - V5: per-state delivery profiles (media/delivery.py — the state machine
    controls emotion/speed; the profile follows the state whose prompt
    generated the speech: snapshotted at turn END, applied from the next
    turn, exactly like Retell's per-state prompt semantics).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..config import Settings
from ..graph.builder import CallRuntime
from ..observability.latency import TurnClock
from ..observability.tracer import Tracer
from .deepgram_stt import DeepgramSTT
from .delivery import resolve_delivery
from .normalize import normalize_for_tts
from .sentence_gate import SentenceGate
from .tts_factory import create_tts, tts_label

log = logging.getLogger("diallux.session")

HANGUP_GRACE_S = 1.2   # fallback when hangup_mode=timer or no mark arrives


class CallSession:
    def __init__(
        self,
        settings: Settings,
        ws,
        stream_sid: str,
        call_sid: str,
        custom_parameters: dict | None = None,
        llm_json: dict | None = None,
        tracer: Tracer | None = None,
    ):
        self.settings = settings
        self.ws = ws
        self.stream_sid = stream_sid
        self.call_sid = call_sid
        self.llm_json = llm_json or json.loads(open(settings.agent_llm_json).read())
        self.custom_parameters = custom_parameters or {}

        self.tracer = tracer
        self.stt: DeepgramSTT | None = None
        self.tts: CartesiaTTS | None = None
        self.gate: SentenceGate | None = None
        self.runtime: CallRuntime | None = None

        self._turn_task: asyncio.Task | None = None
        self._writer_task: asyncio.Task | None = None
        self._outbox: asyncio.Queue[dict | None] = asyncio.Queue()
        self._clock = TurnClock()
        self._tts_context: str | None = None
        self._turn_state: str | None = None   # V5: state whose profile voices the current turn
        self._first_turn = True
        self._initial_payload: dict | None = None
        self._stopped = False
        self._ended = False
        self._pending_end_mark: str | None = None
        self._turn_mark_seq = 0
        self.turn_reports: list[dict] = []

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self):
        known = set(self.llm_json.get("default_dynamic_variables", {}))
        persona = {k: v for k, v in self.custom_parameters.items() if k in known}

        self.tracer = self.tracer or Tracer(
            session_name="diallux-call",
            metadata={
                "agent": "diallux-langgraph-production",
                "llm_model": self.settings.openai_model,
                "call_sid": self.call_sid,
                "stream_sid": self.stream_sid,
                "stt_mode": self.settings.deepgram_mode,
                "eager_eot": self.settings.deepgram_eager_eot,
                "tts": tts_label(self.settings),
                "rag_mode": self.settings.rag_mode,
            },
            enabled=self.settings.langfuse_enabled,
        )
        self.runtime = CallRuntime(self.settings, self.llm_json, tracer=self.tracer)
        self._initial_payload = dict(self.runtime.initial_state(self.call_sid, persona))
        self._turn_state = self._initial_payload.get("state_name") or self.llm_json.get("starting_state", "Intake")

        # eager end-of-turn wiring
        eager = self.settings.deepgram_eager_eot or self.settings.deepgram_eager_eot_threshold is not None
        self.stt = DeepgramSTT(
            self.settings,
            on_eot=self._on_eot,
            on_start_of_turn=self._on_start_of_turn,
            on_disconnect=self._on_stt_disconnect,
            on_eager=self._on_eager_eot if (eager and self.settings.deepgram_eager) else None,
            on_turn_resumed=self._on_turn_resumed if (eager and self.settings.deepgram_eager) else None,
        )
        if eager and self.settings.deepgram_eager:
            self.settings.deepgram_eager_eot_threshold = self.settings.deepgram_eager_eot_threshold or 0.6

        self.tts = create_tts(self.settings, on_audio=self._on_tts_audio)
        self.gate = SentenceGate(self._speak_chunk, managed=self.settings.cartesia_buffering == "managed")

        self._writer_task = asyncio.create_task(self._writer())
        await self.stt.connect()
        await self.tts.connect()

        # begin_message (start_speaker: agent) — spoken immediately, no LLM.
        # V5: the greeting gets the "begin" delivery profile (warm, energetic).
        begin = (self._initial_payload.get("history") or [{}])[0].get("content", "")
        ctx = self.tts.new_context_id()
        self._tts_context = ctx
        await self.tts.speak(ctx, begin, continue_=False,
                              overrides=resolve_delivery(self.settings, "begin"))
        if self.settings.metrics_enabled:
            from ..observability import metrics
            metrics.CALLS.inc()
        log.info("call %s started (sid=%s)", self.call_sid, self.stream_sid)

    async def stop(self, reason: str = "stopped"):
        if self._stopped:
            return
        self._stopped = True
        if self._turn_task:
            self._turn_task.cancel()
        if self.gate:
            await self.gate.close()
        if self.stt:
            await self.stt.close()
        if self.tts:
            await self.tts.close()
        await self._outbox.put(None)
        if self.runtime:
            try:
                state = await self.runtime.graph.aget_state({"configurable": {"thread_id": self.call_sid}})
                values = state.values or {}
                convo = [
                    {"role": m.get("role"), "content": m.get("content", "")}
                    for m in values.get("history", []) if m.get("role") in ("user", "assistant")
                ]
                booked = bool((values.get("dvs") or {}).get("booking_verified"))
                if booked and self.settings.metrics_enabled:
                    from ..observability import metrics
                    metrics.BOOKINGS.inc()
                self.tracer.finish(output={
                    "conversation": convo,
                    "tool_trace": self.runtime.executor.trace,
                    "final_state": values.get("state_name"),
                    "ended": values.get("ended", self._ended),
                    "dynamic_variables": values.get("dvs", {}),
                    "gate_rejections": self.runtime.executor.gate_rejections,
                    "turn_reports": self.turn_reports,
                    "stop_reason": reason,
                })
            except Exception:
                self.tracer.finish(output={"stop_reason": reason, "turn_reports": self.turn_reports})
            await self.runtime.aclose()
        log.info("call %s stopped (%s)", self.call_sid, reason)

    # ------------------------------------------------------------------ #
    # Twilio events
    # ------------------------------------------------------------------ #
    async def on_media(self, payload_b64: str):
        if self.stt:
            await self.stt.send_audio(_b64decode(payload_b64))

    async def on_twilio_mark(self, msg: dict):
        """Playback-confirmed hangup: end_call audio has finished PLAYING."""
        name = ((msg.get("mark") or {}).get("name") or "")
        if self._pending_end_mark and name == self._pending_end_mark:
            self._pending_end_mark = None
            await self.ws.close(code=1000)
            await self.stop(reason="end_call_played")

    async def on_twilio_warning(self, msg: dict):
        """Twilio 31931 etc. — media discarded downstream (buffer overflow)."""
        log.warning("twilio stream warning: %s", json.dumps(msg)[:300])
        self.tracer.span("twilio:warning", output=msg)

    # ------------------------------------------------------------------ #
    # STT callbacks
    # ------------------------------------------------------------------ #
    async def _on_eot(self, transcript: str):
        if self._stopped or self._ended:
            return
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        self._clock = TurnClock(turn_index=self._clock.turn_index + 1)
        self._clock.mark_user_end()
        self._turn_task = asyncio.create_task(self._run_turn(transcript))

    async def _on_eager_eot(self, transcript: str):
        """Speculative LLM start (optional): runs the turn early; EndOfTurn keeps it."""
        if self._stopped or self._ended:
            return
        if self._turn_task and not self._turn_task.done():
            return                      # a turn is already running
        log.info("eager turn started (speculative)")
        self._clock = TurnClock(turn_index=self._clock.turn_index + 1, extra={"eager": True})
        self._clock.mark_user_end()
        self._turn_task = asyncio.create_task(self._run_turn(transcript))

    async def _on_turn_resumed(self):
        """User kept talking: the speculative turn is cancelled."""
        if self._turn_task and not self._turn_task.done() and self._clock.extra.get("eager"):
            self._turn_task.cancel()
            await self.gate.reset()
            if self.tts and self._tts_context:
                await self.tts.cancel(self._tts_context)
            self._send({"event": "clear", "streamSid": self.stream_sid})
            log.info("eager turn cancelled (user resumed)")

    async def _on_start_of_turn(self):
        if self._stopped:
            return
        interrupted = bool(self._turn_task and not self._turn_task.done())
        if interrupted:
            self._clock.barge_in = True
            self._clock.t_barge_in = time.perf_counter()
            self._turn_task.cancel()
            if self.settings.metrics_enabled:
                from ..observability import metrics
                metrics.BARGEINS.inc()
        if self.gate:
            await self.gate.reset()
        if self.tts and self._tts_context:
            await self.tts.cancel(self._tts_context)
        self._send({"event": "clear", "streamSid": self.stream_sid})
        if interrupted:
            log.info("barge-in on call %s", self.call_sid)

    async def _on_stt_disconnect(self):
        """V2: transparent STT reconnect (audio gap, call continues)."""
        if self._stopped or not self.settings.deepgram_reconnect:
            return
        log.warning("deepgram dropped mid-call; reconnecting")
        self.tracer.span("stt:reconnect", output={"call_sid": self.call_sid})
        try:
            await self.stt.close()
        except Exception:
            pass
        eager = self.settings.deepgram_eager
        self.stt = DeepgramSTT(
            self.settings,
            on_eot=self._on_eot,
            on_start_of_turn=self._on_start_of_turn,
            on_disconnect=self._on_stt_disconnect,
            on_eager=self._on_eager_eot if eager else None,
            on_turn_resumed=self._on_turn_resumed if eager else None,
        )
        try:
            await self.stt.connect()
        except Exception as exc:
            log.error("stt reconnect failed: %s", exc)

    # ------------------------------------------------------------------ #
    # The brain
    # ------------------------------------------------------------------ #
    async def _run_turn(self, transcript: str):
        clock = self._clock
        config = {"configurable": {"thread_id": self.call_sid}}
        self._tts_context = self.tts.new_context_id() if self.tts else None
        try:
            payload: dict[str, Any] = {"user_text": transcript}
            if self._first_turn:
                payload.update(self._initial_payload or {})
                self._first_turn = False
            async for mode, data in self.runtime.graph.astream(
                payload, config=config, stream_mode=["custom", "updates"]
            ):
                if mode == "custom" and isinstance(data, dict) and "tts_token" in data:
                    if clock.t_llm_first is None:
                        clock.mark_llm_first()
                    self.gate.add(data["tts_token"])
        except asyncio.CancelledError:
            await self.gate.reset()
            return
        except Exception as exc:
            log.exception("turn failed on call %s: %s", self.call_sid, exc)
            return

        await self.gate.end_of_turn()
        clock.mark_turn_done()
        report = clock.report()
        self.turn_reports.append(report)
        self.tracer.span(f"turn:{clock.turn_index}", output=report)
        if self.settings.metrics_enabled:
            from ..observability import metrics
            metrics.observe_turn_report(report)
        log.info("turn %d report: %s", clock.turn_index, json.dumps(report))

        # mark frame: Twilio echoes it back once the audio finished PLAYING
        self._turn_mark_seq += 1
        self._send({"event": "mark", "streamSid": self.stream_sid,
                    "mark": {"name": f"turn-{clock.turn_index}-done"}})

        try:
            snap = await self.runtime.graph.aget_state(config)
            vals = snap.values or {}
            if vals.get("ended"):
                self._ended = True
                if self.settings.hangup_mode == "mark":
                    self._pending_end_mark = f"turn-{clock.turn_index}-done"
                else:
                    asyncio.get_event_loop().create_task(self._hangup_after_drain())
            # V5: the post-transition state becomes the delivery profile for
            # the NEXT turn — turn N's speech was generated by turn N's
            # starting state's prompt, so that is whose profile voiced it.
            if vals.get("state_name"):
                self._turn_state = vals["state_name"]
        except Exception:
            pass

    async def _hangup_after_drain(self):
        await asyncio.sleep(HANGUP_GRACE_S)
        try:
            await self.ws.close(code=1000)
        except Exception:
            pass
        await self.stop(reason="end_call")

    # ------------------------------------------------------------------ #
    # TTS plumbing
    # ------------------------------------------------------------------ #
    async def _speak_chunk(self, text: str, continue_: bool):
        if self.tts and self._tts_context:
            # V3 speech normalization: what Retell did in its platform layer,
            # now deterministic and self-hosted. Runs per flushed sentence —
            # µs of regex, never touches history/dvs/prompts.
            if self.settings.tts_normalize and text:
                text = normalize_for_tts(text, phone_style=self.settings.tts_phone_style)
            # V5 delivery profile: dict lookup on the turn's state — the
            # state machine, not the LLM, decides how it sounds.
            await self.tts.speak(
                self._tts_context, text, continue_,
                overrides=resolve_delivery(self.settings, self._turn_state),
            )

    async def _on_tts_audio(self, b64_payload: str, context_id: str):
        clock = self._clock
        if clock.t_tts_first is None:
            clock.mark_tts_first()
        self._send({"event": "media", "streamSid": self.stream_sid,
                    "media": {"payload": b64_payload}})
        if clock.t_audio_out is None:
            clock.mark_audio_out()

    # ------------------------------------------------------------------ #
    # outbound writer (single sender for the Twilio WS)
    # ------------------------------------------------------------------ #
    def _send(self, msg: dict):
        try:
            self._outbox.put_nowait(msg)
        except Exception:
            pass

    async def _writer(self):
        try:
            while True:
                msg = await self._outbox.get()
                if msg is None:
                    break
                await self.ws.send_text(json.dumps(msg))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("twilio writer stopped: %s", exc)


def _b64decode(s: str) -> bytes:
    import base64
    return base64.b64decode(s)
