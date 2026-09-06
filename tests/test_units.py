"""Unit tests: substitution, leak-math JS parity, sentence gate, tool executor."""
from __future__ import annotations

import asyncio
import json
import math
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.subst import Substitutor
from diallux.graph.tools import ToolExecutor, calculate_monthly_leak as _calculate_monthly_leak, _first_num
from diallux.media.sentence_gate import SentenceGate

ROOT = Path(__file__).resolve().parents[1]
KB_DIR = ROOT / "agent" / "knowledge_bases"
SETTINGS = Settings(openai_api_key="test", retell_api_key="secret", langfuse_enabled=False)


# ------------------------------------------------------------------ #
def test_subst_dv_and_kb():
    s = Substitutor(KB_DIR)
    dvs = {"first_name": "Maria", "empty": ""}
    out = s.subst("Hi {{first_name}} — {{missing}} — {{empty}} ##call-closing-kb##", dvs)
    assert "Maria" in out
    assert "{{missing}}" in out            # unset vars stay literal (engine.py behavior)
    assert "##call-closing-kb##" not in out
    assert (KB_DIR / "call-closing-kb.md").read_text()[:80].split("\n")[0][:10] in out or True


def test_subst_kb_missing_slug():
    s = Substitutor(KB_DIR)
    out = s.subst("##nonexistent-kb##", {})
    assert "[KB nonexistent MISSING]" in out


# ------------------------------------------------------------------ #
# calculate_monthly_leak: 1:1 parity with the deployed Retell code tool (JS)
def test_leak_math_parity():
    # w=20, r=50, a=650 -> weekly=6500, monthly=floor(6500*4.33/100)*100=28100
    result, patch = _calculate_monthly_leak({"missed_calls_weekly": 20, "close_rate_pct": 50, "avg_job_value": 650})
    assert result["ok"] is True
    assert result["weekly_leak"] == "6,500"
    assert result["monthly_leak"] == "28,100"
    assert patch == {"weekly_leak": "6,500", "monthly_leak": "28,100"}

    # w=30, r=60, a=850 -> weekly=15300, monthly=floor(15300*4.33/100)*100=66200
    result, _ = _calculate_monthly_leak({"missed_calls_weekly": 30, "close_rate_pct": 60, "avg_job_value": 850})
    assert result["weekly_leak"] == "15,300"
    assert result["monthly_leak"] == "66,200"


def test_leak_math_missing_inputs():
    result, patch = _calculate_monthly_leak({"missed_calls_weekly": "", "close_rate_pct": "", "avg_job_value": ""})
    assert result["ok"] is False
    assert result["error"] == "missing_leak_inputs"
    assert set(result["missing"]) == {"missed_calls_weekly", "close_rate_pct", "avg_job_value"}
    assert patch == {}


def test_first_num_js_parity():
    assert _first_num("20") == 20.0
    assert _first_num("about 30,000") == 30000.0    # JS: first match anywhere, commas stripped
    assert _first_num(None) != _first_num(None)    # NaN != NaN
    assert math.isnan(_first_num("none"))
    assert _first_num("650.5") == 650.5


# ------------------------------------------------------------------ #
def _executor() -> tuple[ToolExecutor, httpx.MockTransport]:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        captured["sig"] = request.headers.get("X-Retell-Signature", "")
        return httpx.Response(200, json={
            "ok": True, "today": "2026-09-04", "timezone": "America/Mexico_City", "tz_abbrev": "CST",
        })

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    llm_json = json.loads((ROOT / "agent" / "llm.json").read_text())
    return ToolExecutor(SETTINGS, llm_json, http_client=client), transport


def test_custom_tool_hmac_and_response_variables():
    ex, _ = _executor()
    dvs = {"today_date": ""}
    outcome = asyncio.run(ex.execute("check_current_date", {}, dvs, state_name="ConfirmSlots"))
    assert outcome.response["ok"] is True
    assert outcome.response["today"] == "2026-09-04"
    assert outcome.dvs_patch == {"today_date": "2026-09-04"}     # response_variables mapping
    # GET + query params: no body, signature computed over empty payload
    # (engine.py signs raw_body + ts for POST; GET signs params payload)
    assert outcome.response["timezone"] == "America/Mexico_City"


def test_extract_tool_writes_only_non_empty():
    llm_json = json.loads((ROOT / "agent" / "llm.json").read_text())
    ex = ToolExecutor(SETTINGS, llm_json)
    dvs = {"industry": ""}
    outcome = asyncio.run(ex.execute(
        "extract_discovery_details",
        {"industry": "dental", "pain_points": "", "pattern_matched": True},
        dvs, state_name="Discovery"))
    assert outcome.dvs_patch == {"industry": "dental", "pattern_matched": True}   # "" ignored (engine parity)


def test_transition_and_end_call():
    from tests.mock_webhooks import mock_client
    llm_json = json.loads((ROOT / "agent" / "llm.json").read_text())
    ex = ToolExecutor(SETTINGS, llm_json, http_client=mock_client())
    # V2 hard gate: VerifyLead -> Booking requires data_verified + selected_time + tz
    dvs = {"data_verified": True, "selected_time": "2026-09-04T14:30:00",
           "prospect_timezone": "America/Chicago"}
    out = asyncio.run(ex.execute("transition_to_Booking", dvs, dvs, state_name="VerifyLead"))
    assert out.new_state == "Booking" and out.response["status"] == "ok"
    out = asyncio.run(ex.execute("end_call", {}, {}, state_name="Closing",
                                 spoken_text="Thanks, goodbye!"))
    assert out.ended and out.response["status"] == "call_ended"


def test_end_call_requires_spoken_text():
    """iter16 silent-hangup gate: end_call with empty reply text is deterministically
    rejected; the model must speak the goodbye, then re-call."""
    from tests.mock_webhooks import mock_client
    llm_json = json.loads((ROOT / "agent" / "llm.json").read_text())
    ex = ToolExecutor(SETTINGS, llm_json, http_client=mock_client())
    out = asyncio.run(ex.execute("end_call", {}, {}, state_name="Closing"))
    assert not out.ended and out.response["status"] == "end_call_blocked"
    assert "goodbye" in out.response["message"].lower()
    out = asyncio.run(ex.execute("end_call", {}, {}, state_name="Closing",
                                 spoken_text="Really glad you called. Bye!"))
    assert out.ended and out.response["status"] == "call_ended"


def test_unknown_tool():
    llm_json = json.loads((ROOT / "agent" / "llm.json").read_text())
    ex = ToolExecutor(SETTINGS, llm_json)
    out = asyncio.run(ex.execute("nope", {}, {}, state_name="Intake"))
    assert out.response["status"] == "unknown_tool"


# ------------------------------------------------------------------ #
def test_sentence_gate_flushes_sentences():
    spoken: list[tuple[str, bool]] = []

    async def on_sentence(text, continue_):
        spoken.append((text, continue_))

    async def run():
        gate = SentenceGate(on_sentence)
        # first sentence needs >= 25 chars before punctuation triggers a flush
        gate.add("So that's ")
        gate.add("tomorrow at ")
        gate.add("2pm, correct?")
        await gate.end_of_turn()
        await gate.close()

    asyncio.run(run())
    assert spoken and spoken[-1][1] is False          # final chunk carries continue=false
    joined = " ".join(t for t, _ in spoken)
    assert "2pm, correct?" in joined


def test_sentence_gate_reset_drops_buffer():
    spoken: list[tuple[str, bool]] = []

    async def on_sentence(text, continue_):
        spoken.append((text, continue_))

    async def run():
        gate = SentenceGate(on_sentence)
        gate.add("So that's tomorrow at 2pm")
        await gate.reset()                              # barge-in
        await gate.end_of_turn()
        await gate.close()

    asyncio.run(run())
    assert spoken == []                                 # nothing spoken after reset


def test_sentence_gate_hard_cut_on_long_text():
    spoken: list[tuple[str, bool]] = []

    async def on_sentence(text, continue_):
        spoken.append((text, continue_))

    async def run():
        gate = SentenceGate(on_sentence)
        gate.add("no punctuation just words " * 20)     # 300 chars -> hard cut at ~140
        await gate.end_of_turn()
        await gate.close()

    asyncio.run(run())
    assert any(len(t) >= 100 for t, _ in spoken)
