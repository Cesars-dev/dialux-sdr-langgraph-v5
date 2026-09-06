"""LLM access layer: streaming ChatOpenAI + Retell->OpenAI tool schema conversion.

Verbatim semantics from lab/engine.py:
  - `_openai_schema()` converts Retell `type:"enum" + choices` to OpenAI
    `type:"string" + enum` (recursively).
  - Toolset for a round = state tools + general_tools + `transition_to_<State>`
    functions built from the state's edges.
  - Model config: gpt-5.2 with reasoning off. V2: temperature defaults to
    0.3 (your ask — mild variance for liveliness) with a GUARD:
    gpt-5.x reasoning models only accept the default temperature (passing
    anything else 400s on chat-completions), so for gpt-5* we omit it. Set
    OPENAI_MODEL=gpt-4.1 / gpt-4o to make 0.3 actually bite.

Streaming: content deltas are yielded as they arrive (for TTS pipelining);
tool_call fragments are accumulated into complete calls before the executor
runs. `stream_usage=True` gives token counts on the final chunk.
"""
from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

from langchain_openai import ChatOpenAI

from ..config import Settings

# reasoning-family models: temperature is fixed by the API; sending 0.3 400s
_REASONING_MODEL_RE = re.compile(r"^(gpt-5|o[134](-|$))", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Retell -> OpenAI schema conversion (engine.py verbatim)
# --------------------------------------------------------------------------- #
def retell_to_openai_schema(params: Any) -> Any:
    """Recursively convert Retell schemas (type 'enum' + 'choices')."""
    if isinstance(params, list):
        return [retell_to_openai_schema(p) for p in params]
    if not isinstance(params, dict):
        return params
    out: dict = {}
    if params.get("type") == "enum" and params.get("choices"):
        out["type"] = "string"
        out["enum"] = params["choices"]
    else:
        for k, v in params.items():
            if k == "choices":
                continue
            out[k] = retell_to_openai_schema(v)
    return out


def _strictify(params: dict) -> dict:
    """Make a JSON schema OpenAI Structured Outputs compliant (strict:true).

    Rules enforced by the API: every property listed in `required`,
    additionalProperties:false at every object level, optional fields become
    nullable unions, nested objects/arrays recurse. This is DECODE-TIME
    enforcement — the model cannot emit malformed tool args (Julio's
    'non-roulette' requirement; no instructor needed)."""
    if not isinstance(params, dict):
        return params
    ptype = params.get("type")
    if ptype == "object" or "properties" in params:
        props = params.get("properties") or {}
        required = params.get("required") or []
        strict_props = {}
        for name, spec in props.items():
            spec = _strictify(spec) if isinstance(spec, dict) else spec
            if name not in required and isinstance(spec, dict) and "type" in spec:
                t = spec["type"]
                spec = {**spec, "type": [t, "null"] if isinstance(t, str) else t}
            strict_props[name] = spec
        return {"type": "object", "properties": strict_props,
                "required": list(props.keys()), "additionalProperties": False}
    if ptype == "array" and isinstance(params.get("items"), dict):
        return {**params, "items": _strictify(params["items"])}
    return params


def build_tool_schemas(
    state_cfg: dict,
    general_tools: list[dict],
    subst_fn,
    dvs: dict,
) -> list[dict]:
    """OpenAI tool schemas for one LLM round in the current state (engine.py verbatim)."""
    out: list[dict] = []
    for t in list(state_cfg.get("tools", [])) + list(general_tools):
        ttype = t.get("type")
        if ttype == "extract_dynamic_variable":
            props: dict = {}
            req: list = []
            for v in t.get("variables", []):
                prop: dict = {"type": v.get("type", "string"), "description": subst_fn(v.get("description", ""))}
                if v.get("choices"):
                    prop["type"] = "string"
                    prop["enum"] = v["choices"]
                props[v["name"]] = prop
                req.append(v["name"])
            out.append({"type": "function", "function": {
                "name": t["name"],
                "description": subst_fn(t.get("description", "")),
                "parameters": _strictify({"type": "object", "properties": props, "required": req}),
                "strict": True,
            }})
        elif ttype in ("custom", "book_appointment_cal", "code"):
            params = t.get("parameters") or {"type": "object", "properties": {}}
            if "properties" in params:
                params = retell_to_openai_schema(json.loads(subst_fn(json.dumps(params))))
            out.append({"type": "function", "function": {
                "name": t["name"],
                "description": subst_fn(t.get("description", "")),
                "parameters": _strictify(params),
                "strict": True,
            }})
        elif ttype == "end_call":
            # Retell exposes the built-in end_call general tool to the model.
            # (lab/engine.py skipped it in _tools_for_state — a lab gap, not
            # deployed-agent behavior; the Closing prompt explicitly uses it.)
            out.append({"type": "function", "function": {
                "name": t["name"],
                "description": subst_fn(t.get("description", "Ends the call gracefully.")),
                "parameters": _strictify({"type": "object", "properties": {}}),
                "strict": True,
            }})
    # edges -> transition_to_<State> functions
    for e in state_cfg.get("edges", []):
        out.append({"type": "function", "function": {
            "name": f"transition_to_{e['destination_state_name']}",
            "description": subst_fn(e.get("description", "")),
            "parameters": _strictify(e.get("parameters", {"type": "object", "properties": {}})),
            "strict": True,
        }})
    return out


# --------------------------------------------------------------------------- #
# Streaming LLM
# --------------------------------------------------------------------------- #
class LLMEvent(dict):
    """{'type': 'token'|'final', ...}"""

    @property
    def type(self) -> str:
        return self["type"]


class StreamingLLM:
    """Thin async wrapper around ChatOpenAI with tool-call accumulation."""

    def __init__(self, settings: Settings):
        self.settings = settings
        kwargs: dict[str, Any] = {}
        if settings.openai_temperature is not None and not _REASONING_MODEL_RE.match(settings.openai_model):
            kwargs["temperature"] = settings.openai_temperature
        elif settings.openai_temperature is not None:
            # gpt-5*: the API rejects non-default temperature; silently use
            # the platform default (logged once so it's never a surprise)
            import logging
            logging.getLogger("diallux.llm").info(
                "temperature=%.2f ignored for reasoning model %s "
                "(API-fixed; use gpt-4.1/gpt-4o to apply it)",
                settings.openai_temperature, settings.openai_model)
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        if _REASONING_MODEL_RE.match(settings.openai_model):
            mk: dict[str, Any] = {}
            if settings.openai_reasoning_effort:
                mk["reasoning_effort"] = settings.openai_reasoning_effort
            if settings.openai_verbosity:
                mk["verbosity"] = settings.openai_verbosity
            if mk:
                kwargs["model_kwargs"] = mk
        self._llm = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            stream_usage=True,
            **kwargs,
        )

    async def astream(
        self, messages: list[dict], tools: list[dict]
    ) -> AsyncIterator[LLMEvent]:
        """Yield token deltas then one 'final' event with the complete message dict.

        final message dict: {"content": str, "tool_calls": [{"id","name","arguments"}],
        "usage": {...}|None}
        """
        bound = self._llm.bind_tools(tools) if tools else self._llm
        content_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        usage: dict | None = None
        async for chunk in bound.astream(messages):
            text = _chunk_text(chunk)
            if text:
                content_parts.append(text)
                yield LLMEvent({"type": "token", "text": text})
            for tc in getattr(chunk, "tool_call_chunks", None) or []:
                acc = tool_acc.setdefault(tc.get("index") or 0, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    acc["id"] = tc["id"]
                if tc.get("name"):
                    acc["name"] = acc["name"] + tc["name"] if acc["name"] and not tc.get("id") else tc["name"]
                if tc.get("args"):
                    acc["arguments"] += tc["args"]
            if getattr(chunk, "usage_metadata", None):
                usage = dict(chunk.usage_metadata)
        tool_calls = [
            {"id": v["id"] or f"call_{i}", "name": v["name"], "arguments": v["arguments"] or "{}"}
            for i, v in sorted(tool_acc.items())
        ]
        yield LLMEvent({
            "type": "final",
            "content": "".join(content_parts),
            "tool_calls": tool_calls,
            "usage": usage,
        })


def _chunk_text(chunk) -> str:
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content blocks
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""
