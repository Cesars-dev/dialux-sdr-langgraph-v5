"""FastAPI app — V2 production entrypoints.

  POST/GET /twiml    TwiML bridge (same as V1: inbound_track + From-seeded dv)
  WS       /media    bidirectional Media Streams session (media/session.py)
  GET      /health   liveness (docker healthcheck / uptime monitoring)
  GET      /metrics  Prometheus (latency histograms, counters) — V2
"""
from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response

from .config import get_settings
from .media.session import CallSession
from .media.tts_factory import tts_label

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("diallux.app")

app = FastAPI(title="Dialux SDR — LangGraph production", version="2.0.0")


@app.get("/health")
async def health():
    s = get_settings()
    return {
        "ok": True,
        "version": "2.0.0",
        "stt_mode": s.deepgram_mode,
        "llm": s.openai_model,
        "tts": tts_label(s),
        "rag_mode": s.rag_mode,
        "checkpoint": s.checkpoint_backend,
        "eager_eot": s.deepgram_eager_eot,
        "langfuse": s.langfuse_enabled,
    }


@app.get("/metrics")
async def metrics_endpoint():
    s = get_settings()
    if not s.metrics_enabled:
        return PlainTextResponse("metrics disabled\n")
    from .observability import metrics
    return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")


@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(request: Request):
    s = get_settings()
    base = s.public_base_url.rstrip("/")
    from_number = (request.query_params.get("From") or "").strip()
    params_xml = ""
    if from_number:
        from xml.sax.saxutils import escape
        params_xml = f'          <Parameter name="callback_number" value="{escape(from_number)}" />\n'
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{base}/media" track="inbound_track">
{params_xml}    </Stream>
  </Connect>
  <Hangup/>
</Response>"""
    return PlainTextResponse(xml, media_type="application/xml")


@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    settings = get_settings()
    session: CallSession | None = None
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event = msg.get("event")
            if event == "start":
                start = msg.get("start", {})
                session = CallSession(
                    settings=settings,
                    ws=ws,
                    stream_sid=start.get("streamSid", ""),
                    call_sid=start.get("callSid", ""),
                    custom_parameters=start.get("customParameters") or {},
                )
                await session.start()
            elif event == "media" and session:
                payload = (msg.get("media") or {}).get("payload")
                if payload:
                    await session.on_media(payload)
            elif event == "mark" and session:
                await session.on_twilio_mark(msg)          # V2: playback-confirmed hangup
            elif event == "warning" and session:
                await session.on_twilio_warning(msg)       # V2: 31931 watchdog
            elif event == "stop" and session:
                break
            elif event == "connected":
                pass
    except WebSocketDisconnect:
        log.info("twilio disconnected")
    except Exception:
        log.exception("media session crashed")
    finally:
        if session:
            await session.stop(reason="stream_closed")
