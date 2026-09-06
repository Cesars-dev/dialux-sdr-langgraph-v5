"""Tool executor — V2 production hardening of the verbatim executor.

Everything from V1 (repeat guard, extract writes, HMAC webhooks, leak-math
port, transitions, end_call) PLUS:

  1. SERVER_OWNED_DVS protection — extract tools can no longer write the
     server-owned booleans/values (slot_verified, data_verified, phone_confirmed,
     booking_verified, today_date, ...). The LLM cannot fabricate progress.
  2. Hard gates on transitions — `transition_to_X` only swaps state when every
     `required` param of the edge schema is truthy in dvs. Otherwise the tool
     answers {"status": "gate_failed", "missing": [...]} and the state holds.
     Retell's intent, finally enforced deterministically.
  3. Webhook retries — connection-level retries with backoff (httpx transport)
     so a VPS hiccup doesn't fail a booking turn.
  4. Typed dvs — patches flow through schema.DynamicVariables for validation
     and coercion ("" not null; ints coerced).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from typing import Any

import httpx

from ..schema import SERVER_OWNED_DVS, DynamicVariables


class ToolOutcome(dict):
    """Result of one tool call: what the model sees + state side effects."""

    @property
    def response(self) -> dict:
        return dict(self.get("response") or {})

    @property
    def dvs_patch(self) -> dict:
        return dict(self.get("dvs_patch") or {})

    @property
    def new_state(self) -> str | None:
        return self.get("new_state")

    @property
    def ended(self) -> bool:
        return bool(self.get("ended"))


# iter14: LLM-facing gate-failure strings — name the property, state its current
# value, give the exact remedy step. One per param that can fail a gate.
GATE_FAIL_MESSAGES: dict[str, str] = {
    "is_calling_best_number": (
        "is_calling_best_number is unanswered — ask if the number they're calling from is their "
        "best contact number, then call record_reach_details with true or false."
    ),
    "phone_confirmed": (
        "phone_confirmed is false — read the number back, get a clear yes, then call "
        "set_callback_number with the digits."
    ),
    "contact_details_completed": (
        "contact_details_completed is false — first name, last name, company, timezone and best "
        "number aren't all done; finish what's open, then set it."
    ),
    "first_name": "first_name is empty — ask for it, then call extract_person_details.",
    "last_name": "last_name is empty — ask for it, then call extract_person_details.",
    "company_name": "company_name is empty — ask for it, then call extract_person_details.",
    "prospect_timezone": (
        "prospect_timezone is empty — get their city or local time, then call extract_contact_timezone."
    ),
    "callback_number": (
        "callback_number is empty — confirm their best number, then call set_callback_number."
    ),
    "slot_verified": (
        "slot_verified is false — fix each problem in validate_lead's actions list, then re-call "
        "validate_lead."
    ),
    "data_verified": (
        "data_verified is false — speak each action from verify_lead_data, correct the details, "
        "re-call until ok."
    ),
    "booking_verified": (
        "booking_verified is false — no accepted booking yet; retry create_livecall_booking, then "
        "record_booking_uid."
    ),
}


class ToolExecutor:
    def __init__(
        self,
        settings,
        llm_json: dict,
        tracer=None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.llm_json = llm_json
        self.states = {s["name"]: s for s in llm_json.get("states", [])}
        self.general_tools = llm_json.get("general_tools", [])
        self.tracer = tracer
        if http_client is not None:
            self.http = http_client
            self._owns_http = False
        else:
            transport = httpx.AsyncHTTPTransport(retries=2)   # V2: connection retries
            self.http = httpx.AsyncClient(timeout=settings.webhook_timeout_s, transport=transport)
            self._owns_http = True
        self._last_call: tuple[str, str] | None = None
        self.trace: list[dict] = []
        self.gate_rejections: list[dict] = []      # observability: every refused transition
        self.end_call_blocks: list[dict] = []      # observability: refused booking-claim hangups

    async def aclose(self):
        if self._owns_http:
            await self.http.aclose()

    # ------------------------------------------------------------------ #
    async def execute(self, name: str, args: dict, dvs: dict, state_name: str = "",
                      spoken_text: str = "") -> ToolOutcome:
        t0 = time.perf_counter()
        obs = self.tracer.start_tool(name, args, state_name) if self.tracer else None
        try:
            outcome = await self._execute_inner(name, args, dvs, state_name, spoken_text)
        except Exception as exc:
            outcome = ToolOutcome(response={
                "ok": False, "error": "tool_exception", "message": f"{type(exc).__name__}: {exc}"[:200]
            })
        if self.tracer:
            self.tracer.finish_tool(obs, outcome.response, time.perf_counter() - t0)
        self.trace.append({"tool": name, "args": args, "resp": outcome.response,
                           "dvs_patch": outcome.dvs_patch,
                           "new_state": outcome.new_state, "ended": outcome.ended})
        return outcome

    async def _execute_inner(self, name: str, args: dict, dvs: dict, state_name: str = "",
                             spoken_text: str = "") -> ToolOutcome:
        # 1. Repeat-call guard (verbatim from engine.py)
        # iter16: end_call is exempt — a blocked end_call (silent-hangup gate) must be
        # re-callable with identical args in the next round.
        key = (name, json.dumps(args, sort_keys=True))
        if name != "end_call":
            if self._last_call == key:
                return ToolOutcome(response={
                    "status": "already_captured",
                    "message": ("Values are already stored as dynamic variables. Do NOT call this "
                                "tool again — proceed to the next step in the flow."),
                })
            self._last_call = key

        # 2-4. state tools + general tools
        for t in list(self.states.get(state_name, {}).get("tools", [])) + list(self.general_tools):
            if t.get("name") != name:
                continue
            if t.get("type") == "extract_dynamic_variable":
                return self._exec_extract(t, args)
            if t.get("type") == "custom":
                return await self._exec_custom(t, args)
            if t.get("type") == "code":
                return self._exec_code(t, dvs)

        # 5. transitions — HARD GATED (V2)
        if name.startswith("transition_to_"):
            dest = name[len("transition_to_"):]
            if dest not in self.states:
                return ToolOutcome(response={"status": "unknown_state", "state": dest})
            edge = next((e for e in self.states.get(state_name, {}).get("edges", [])
                         if e.get("destination_state_name") == dest), None)
            if edge is None:
                # the state machine itself is enforced: only edges that exist
                # in the deployed artifact can be taken
                return ToolOutcome(response={
                    "status": "invalid_transition",
                    "message": (f"There is no transition from {state_name} to {dest}. "
                                "Follow the conversation flow."),
                })
            ok, missing = self._gate_check_edge(edge, dvs)
            if not ok:
                self.gate_rejections.append(
                    {"tool": name, "state": state_name, "dest": dest, "missing": missing})
                return ToolOutcome(response={
                    "status": "gate_failed",
                    "missing": missing,
                    "message": ("Transition NOT taken: " + " ".join(
                        GATE_FAIL_MESSAGES.get(
                            p, f"{p} is not set yet — complete that step in the state prompt first.")
                        for p in missing)),
                })
            return ToolOutcome(response={"status": "ok", "state": dest}, new_state=dest)

        # 6. end_call — booking-claim truth gate (iter8, Surgeon v5-pgvector-gpt52-argfix).
        # Live-observed: gpt-4.1 said "I've got you down for 3:30" + end_call with ZERO
        # booking tools fired (Danny/Marcus). The model must never be able to hang up
        # claiming a booking it didn't make — deterministic, not prompt-trust.
        # iter16: silent-hangup gate — 10/13 audit calls ended with end_call and NO
        # spoken goodbye (prompt rule alone didn't hold: 2/5, then 4/5 silent in the
        # iter16 batteries). end_call with empty reply text is deterministically
        # rejected; the model must speak the goodbye, then re-call.
        if name == "end_call":
            if dvs.get("livecall_agreed") and not (dvs.get("slot_verified") or dvs.get("booking_confirmed")):
                self.end_call_blocks.append({"state": state_name})
                return ToolOutcome(response={
                    "status": "end_call_blocked",
                    "message": ("The caller agreed to a live call but no booking has been "
                                "completed. You may NOT end the call as if it were booked. "
                                "Either complete the booking flow now (ConfirmSlots → verify "
                                "→ booking) or honestly tell the caller the booking did not "
                                "happen."),
                })
            if not (spoken_text or "").strip():
                self.end_call_blocks.append({"state": state_name, "reason": "empty_text"})
                return ToolOutcome(response={
                    "status": "end_call_blocked",
                    "message": ("You called end_call with no spoken text. FIRST write the "
                                "warm goodbye sentence (per ##call-closing-kb##) as your reply "
                                "text in this same response, THEN call end_call again."),
                })
            return ToolOutcome(response={"status": "call_ended"}, ended=True)

        return ToolOutcome(response={"status": "unknown_tool", "name": name})

    # ------------------------------------------------------------------ #
    def _exec_extract(self, t: dict, args: dict) -> ToolOutcome:
        wrote: dict = {}
        rejected: list[str] = []
        for v in t.get("variables", []):
            val = args.get(v["name"])
            if val in (None, ""):
                continue
            if v["name"] in SERVER_OWNED_DVS:           # V2 hard gate: server-owned
                rejected.append(v["name"])
                continue
            wrote[v["name"]] = val
        # validate through the typed model (coercion + type safety)
        merged = DynamicVariables.from_flat({**{"__x": 1}, **wrote})
        clean = {k: merged.to_flat()[k] for k in wrote}
        resp = {"status": "ok", "written": clean}
        if rejected:
            resp["rejected_server_owned"] = rejected
            resp["note"] = ("These values are set by the system automatically and cannot be "
                            "captured manually: " + ", ".join(rejected))
        return ToolOutcome(response=resp, dvs_patch=clean)

    # ------------------------------------------------------------------ #
    def _gate_check_edge(self, edge: dict, dvs: dict) -> tuple[bool, list[str]]:
        """`required` params must be truthy in dvs; `presence_required` params
        must be non-null (answered — false is a legitimate value, e.g. the
        caller said the calling number is NOT their best number)."""
        params = (edge or {}).get("parameters", {})
        required = params.get("required", [])
        presence = params.get("presence_required", [])
        missing = [p for p in required if not dvs.get(p)]
        unanswered = [p for p in presence if dvs.get(p) is None or dvs.get(p) == ""]
        return (not missing and not unanswered, missing + unanswered)

    # ------------------------------------------------------------------ #
    async def _exec_custom(self, t: dict, args: dict) -> ToolOutcome:
        """HMAC-signed webhook (verbatim contract) + retries."""
        from urllib.parse import urlencode

        url = t["url"]
        method = t.get("method", "POST").upper()
        payload = args if t.get("args_at_root") else {"args": args}
        raw = json.dumps(payload).encode()
        sign_target = raw if method != "GET" else b""
        headers = {
            "Content-Type": "application/json",
            "X-Retell-Signature": self._sign(sign_target),
        }
        if method == "GET":
            query = dict(t.get("query_params") or {})
            query.update(payload)
            url = url + ("&" if "?" in url else "?") + urlencode(query)
        elif t.get("query_params"):
            url = url + "?" + urlencode(t.get("query_params"))
        timeout = (t.get("timeout_ms") or 15000) / 1000.0

        body: dict = {}
        for attempt in range(2):                     # V2: one retry on 5xx/transport error
            try:
                if method == "GET":
                    resp = await self.http.get(url, headers=headers, timeout=timeout)
                else:
                    resp = await self.http.post(url, content=raw, headers=headers, timeout=timeout)
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                if resp.status_code >= 500 and attempt == 0:
                    await _sleep_backoff(attempt)
                    continue
                if resp.status_code >= 400:
                    body = dict(body) or {}
                    body.setdefault("ok", False)
                    body.setdefault("error", f"http_{resp.status_code}")
                break
            except httpx.RequestError as exc:
                body = {"ok": False, "error": "request_failed", "message": str(exc)[:200]}
                if attempt == 0:
                    await _sleep_backoff(attempt)
                    continue

        patch: dict = {
            dv_name: body[resp_key]
            for dv_name, resp_key in (t.get("response_variables") or {}).items()
            if isinstance(body, dict) and resp_key in body and body[resp_key] not in (None, "")
        }
        return ToolOutcome(response=body, dvs_patch=patch)

    def _sign(self, raw: bytes) -> str:
        ts = str(int(time.time() * 1000))
        digest = hmac.new(self.settings.retell_api_key.encode(), raw + ts.encode(), hashlib.sha256).hexdigest()
        return f"v={ts},d={digest}"

    # ------------------------------------------------------------------ #
    def _exec_code(self, t: dict, dvs: dict) -> ToolOutcome:
        if t["name"] != "calculate_monthly_leak":
            return ToolOutcome(response={"ok": False, "error": "unsupported_code_tool", "name": t["name"]})
        result, patch = calculate_monthly_leak(dvs)
        return ToolOutcome(response=result, dvs_patch=patch)


async def _sleep_backoff(attempt: int):
    await __import__("asyncio").sleep(0.3 * (attempt + 1))


# --------------------------------------------------------------------------- #
def _first_num(s: Any) -> float:
    """JS parity: String(s||'').replace(/,/g,'').match(/\\d+(\\.\\d+)?/) -> Number(m[0]) or NaN."""
    text = str(s if s not in (None, "") else "").replace(",", "")
    m = re.search(r"(\d+(\.\d+)?)", text)
    return float(m.group(1)) if m else float("nan")


def leak_inputs_present(dvs: dict) -> bool:
    w = _first_num(dvs.get("missed_calls_weekly"))
    r = _first_num(dvs.get("close_rate_pct"))
    a = _first_num(dvs.get("avg_job_value"))
    return all(math.isfinite(x) for x in (w, r, a)) and w > 0 and r > 0 and a > 0


def calculate_monthly_leak(dvs: dict) -> tuple[dict, dict]:
    """The deployed Retell code tool, ported 1:1 (parity-tested)."""
    w = _first_num(dvs.get("missed_calls_weekly"))
    r = _first_num(dvs.get("close_rate_pct"))
    a = _first_num(dvs.get("avg_job_value"))
    missing = []
    if not (math.isfinite(w) and 0 < w <= 500):
        missing.append("missed_calls_weekly")
    if not (math.isfinite(r) and 0 < r <= 100):
        missing.append("close_rate_pct")
    if not (math.isfinite(a) and 0 < a <= 100000):
        missing.append("avg_job_value")
    if missing:
        return {"ok": False, "error": "missing_leak_inputs", "missing": missing}, {}
    weekly = w * (r / 100) * a
    monthly = math.floor(weekly * 4.33 / 100) * 100
    result = {
        "ok": True,
        "weekly_leak": f"{math.floor(weekly):,}",
        "monthly_leak": f"{int(monthly):,}",
        "inputs_used": {"missed_calls_weekly": w, "close_rate_pct": r, "avg_job_value": a},
    }
    return result, {"weekly_leak": result["weekly_leak"], "monthly_leak": result["monthly_leak"]}
