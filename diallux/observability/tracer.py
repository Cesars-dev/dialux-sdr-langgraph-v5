"""Langfuse observability — port of the team's `lab/langfuse_bridge.py` (v3/v4-compatible).

One trace per call (session id `diallux-call-<hex>`), containing:
  - a generation per LLM round      (state, model, messages preview, output,
                                     tool_calls, token usage, exact latency)
  - a tool span per tool call       (args, full JSON response, latency, state)
  - media spans per turn (added by the session): stt_eot, llm_first_token,
    tts_first_byte, e2e_turn — the latency budget, measured
  - on finish(): the full transcript + final dvs + final state on the root trace

Fail-safe: if Langfuse is unreachable, tracing disables itself and the call
continues (same contract as the original bridge).
"""
from __future__ import annotations

import os
import uuid


class Tracer:
    def __init__(self, session_name: str = "diallux-call", metadata: dict | None = None, enabled: bool = True):
        self.enabled = enabled
        self.root = None
        self.lf = None
        self.trace_id = None                     # set at init; used by score()
        self.session_id = f"{session_name}-{uuid.uuid4().hex[:8]}"
        self._meta = metadata or {}
        if not enabled:
            return
        try:
            from langfuse import Langfuse, propagate_attributes

            self.lf = Langfuse(
                public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
                secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
                host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
            )
            with propagate_attributes(session_id=self.session_id, trace_name=session_name):
                self.root = self.lf.start_observation(
                    name=session_name,
                    as_type="agent",
                    metadata={"session_id": self.session_id, **self._meta},
                )
                self.trace_id = getattr(self.root, "trace_id", None)
        except Exception as exc:
            print(f"[langfuse] disabled ({type(exc).__name__}: {exc})")
            self.enabled = False

    def score(self, name: str, value: float, comment: str | None = None):
        """Attach a NUMERIC score to this call's trace (e.g. harness_result). No-op when disabled."""
        if not (self.lf and self.trace_id):
            return
        try:
            self.lf.create_score(trace_id=self.trace_id, name=name, value=value,
                                 data_type="NUMERIC", comment=comment)
        except Exception as exc:
            print(f"[langfuse] score skipped: {type(exc).__name__}: {exc}")

    # ---------------- LLM generation ---------------- #
    def start_llm(self, state: str, model: str, messages: list):
        if not self.root:
            return None
        try:
            return self.root.start_observation(
                name=f"llm:{state}",
                as_type="generation",
                model=model,
                input=messages,
                metadata={"state": state},
            )
        except Exception as exc:
            print(f"[langfuse] gen open skipped: {type(exc).__name__}: {exc}")
            return None

    def finish_llm(self, obs, *, output: str, latency_s: float, usage: dict | None = None, tool_calls=None):
        if not obs:
            return
        try:
            usage_details = None
            if usage:
                usage_details = {
                    "input": usage.get("input_tokens", usage.get("input", 0)),
                    "output": usage.get("output_tokens", usage.get("output", 0)),
                    "total": usage.get("total_tokens", usage.get("total", 0)),
                }
            obs.update(
                output={"text": output, "tool_calls": tool_calls or []},
                metadata={"latency_s": round(latency_s, 3)},
                usage_details=usage_details,
            )
            obs.end()
        except Exception as exc:
            print(f"[langfuse] gen close skipped: {type(exc).__name__}: {exc}")

    # ---------------- tool span ---------------- #
    def start_tool(self, name: str, input_data, state: str):
        if not self.root:
            return None
        try:
            return self.root.start_observation(
                name=f"tool:{name}",
                as_type="tool",
                input=input_data,
                metadata={"state": state},
            )
        except Exception as exc:
            print(f"[langfuse] tool open skipped: {type(exc).__name__}: {exc}")
            return None

    def finish_tool(self, obs, output_data, latency_s: float):
        if not obs:
            return
        try:
            obs.update(output=output_data, metadata={"latency_s": round(latency_s, 3)})
            obs.end()
        except Exception as exc:
            print(f"[langfuse] tool close skipped: {type(exc).__name__}: {exc}")

    # ---------------- media spans (new: the latency budget) ---------------- #
    def span(self, name: str, *, input=None, output=None, metadata: dict | None = None):
        """Fire-and-forget span under the call trace (stt_eot, tts_first_byte, e2e_turn...)."""
        if not self.root:
            return
        try:
            obs = self.root.start_observation(name=name, as_type="span", input=input)
            obs.update(output=output, metadata=metadata or {})
            obs.end()
        except Exception:
            pass

    def flush(self):
        if not self.lf:
            return
        try:
            self.lf.flush()
        except Exception:
            pass

    def finish(self, output=None):
        """Close the root observation with the FULL transcript (bridge contract)."""
        if not self.root:
            return
        try:
            if output is not None:
                self.root.update(
                    input={"session_id": self.session_id, **(self._meta or {})},
                    output=output,
                )
            self.root.end()
            self.flush()
        except Exception as exc:
            print(f"[langfuse] finish skipped: {type(exc).__name__}: {exc}")
