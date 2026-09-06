"""Mock webhook server for hermetic tests — the LIVE production contract
(slots.diallux-ai.site) simulated in-process via httpx.MockTransport.

Response shapes follow ENDPOINTS.md exactly, so tests exercise the real
response_variables -> dvs paths (slot_verified, data_verified, booking_uid,
phone_confirmed ...) without touching production.
"""
from __future__ import annotations

import json

import httpx

BOOKING_UID = "qeTqHuZ1EDzH8bxEdhPQ6H"


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "/today" in url:
        return httpx.Response(200, json={
            "ok": True, "today": "2026-09-04", "timezone": "America/Mexico_City", "tz_abbrev": "CST"})
    if "/check_availability" in url:
        return httpx.Response(200, json={
            "ok": True,
            "slots": [
                {"day": "2026-09-04", "time": "2026-09-04T13:00:00",
                 "iso": "2026-09-04T18:00:00.000Z", "reservation_uid": "2c4b75b4-546f-46fc-bb36-b3a600eb4c65"},
                {"day": "2026-09-04", "time": "2026-09-04T14:30:00",
                 "iso": "2026-09-04T19:30:00.000Z", "reservation_uid": "a1f76ae0-6009-4887-90d4-41794a7e949d"},
            ],
            "taken": [], "reissued": False, "preferred_unavailable": False,
            "slot_reservation_uids": "2c4b75b4-546f-46fc-bb36-b3a600eb4c65,a1f76ae0-6009-4887-90d4-41794a7e949d"})
    if "/validator-function/validate_lead" in url:
        args = _args(request)
        # live contract: the validator computes the leak from the SENT inputs
        # and echoes verified data — never a frozen fixture (that masked
        # per-run DVS as cross-run contamination on 2026-09-04).
        try:
            # verbatim contract from validator_endpoint/validate.py:205-212:
            # first_num extraction, clamps, int() truncation, 2-sig floor, 4.33;
            # ANY input missing -> null leak (executor skips None -> DVS keeps
            # the deterministic calculate_monthly_leak value).
            import re as _re

            def _num(v):
                if v is None or v == "":
                    return None
                m = _re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
                return float(m.group(0)) if m else None

            calls = _num(args.get("missed_calls_per_week"))
            close = _num(args.get("close_rate_percent"))
            job = _num(args.get("avg_job_value"))
            weekly_leak = monthly_leak = None
            if calls is not None and close is not None and job is not None:
                calls = max(0, min(calls, 500))
                close = max(0, min(close, 100))
                job = max(0, min(job, 100000))
                weekly = int(calls * (close / 100.0) * job)

                def _round_2sig(n: int) -> int:
                    n = int(n)
                    if n <= 0:
                        return 0
                    mag = 10 ** (len(str(n)) - 2)
                    return (n // mag) * mag

                weekly_leak = f"${_round_2sig(weekly):,}"
                monthly_leak = f"${_round_2sig(int(weekly * 4.33)):,}"
        except Exception:
            weekly_leak = monthly_leak = None
        return httpx.Response(200, json={
            "ok": True, "data_complete": True, "slot_verified": True, "problems": [], "actions": [],
            "weekly_leak": weekly_leak, "monthly_leak": monthly_leak, "inputs": args})
    if "/validator-function/verify-lead-data" in url:
        args = _args(request)
        # live contract: echo the lead data SENT for verification
        return httpx.Response(200, json={
            "ok": True, "data_verified": True, "problems": [], "actions": [],
            "first_name": args.get("first_name", ""),
            "callback_number": args.get("callback_number", ""),
            "prospect_timezone": args.get("prospect_timezone", "")})
    if "/book-livecall" in url:
        args = _args(request)
        if args.get("booking_intent") == "reschedule":
            return httpx.Response(200, json={
                "ok": True, "status": "reschedule_options", "reissued": True,
                "slots": [{"day": "2026-09-05", "time": "2026-09-05T13:00:00", "iso": "x", "reservation_uid": "r1"},
                          {"day": "2026-09-05", "time": "2026-09-05T15:00:00", "iso": "y", "reservation_uid": "r2"}],
                "slot_reservation_uids": "r1,r2"})
        if args.get("booking_intent") == "cancel":
            return httpx.Response(200, json={"ok": True, "status": "released"})
        return httpx.Response(200, json={
            "ok": True, "status": "booked", "booking_uid": BOOKING_UID,
            "recovered": False, "booking_verified": True})
    if "/validator-function/record-booking-uid" in url:
        return httpx.Response(200, json={"ok": True, "booking_uid": BOOKING_UID})
    if "/validator-function/record-reach-details" in url:
        args = _args(request)
        # live contract (verify.py record_reach_details, iter14): a NO answer
        # NEVER writes phone_confirmed — only a YES mirrors it true.
        best = bool(args.get("is_calling_best_number"))
        body = {"status": "ok", "is_calling_best_number": best}
        if best:
            body["phone_confirmed"] = True
        return httpx.Response(200, json=body)
    if "/validator-function/set-callback-number" in url:
        return httpx.Response(200, json={"status": "ok", "callback_number": "+15123120001",
                                         "phone_confirmed": True})
    return httpx.Response(404, json={"ok": False, "error": "not_mocked", "url": url})


def _args(request: httpx.Request) -> dict:
    try:
        body = json.loads(request.content.decode() or "{}")
        return body.get("args", body)
    except Exception:
        return {}


def mock_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
