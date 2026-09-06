"""LangGraph state — V2 typed build.

Same shape as V1 (state_name / dvs / history / turn scratch), but dvs are
seeded through schema.DynamicVariables: typed defaults ("" / False), numeric
coercion, and no null passthrough — the Retell null-dv bug class is
structurally impossible, which is why V2 needs no "verify no null dvs" tool.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from .schema import DynamicVariables


def merge_dict(a: dict | None, b: dict | None) -> dict:
    out = dict(a or {})
    out.update(b or {})
    return out


class GraphState(TypedDict, total=False):
    # identity
    call_id: str

    # Retell runtime replica
    state_name: str
    dvs: Annotated[dict[str, Any], merge_dict]
    history: Annotated[list[dict], operator.add]

    # per-turn scratch
    turn_active: bool
    turn_spoke: bool
    rounds_left: int
    last_round_tool_calls: list[dict]
    assistant_text: str
    ended: bool

    # user input channel
    user_text: str

    # observability
    turn_index: int
    metrics: Annotated[dict[str, Any], merge_dict]


def begin_message(llm_json: dict) -> str:
    msg = (llm_json.get("begin_message") or "").strip()
    return msg or "Hello! You have reached Dialux, this is Linda speaking, how may I help you today?"


def initial_state(call_id: str, llm_json: dict, persona_dvs: dict | None = None) -> GraphState:
    """Typed seed: defaults come from DynamicVariables (no stale dates, no nulls)."""
    flat_defaults = DynamicVariables().to_flat()
    # only accept known keys from persona overrides (e.g. Twilio <Parameter> seeds)
    persona = {k: v for k, v in (persona_dvs or {}).items() if k in DynamicVariables.model_fields}
    dvs = DynamicVariables.from_flat({**flat_defaults, **persona}).to_flat()
    return GraphState(
        call_id=call_id,
        state_name=llm_json.get("starting_state", "Intake"),
        dvs=dvs,
        history=[{"role": "assistant", "content": begin_message(llm_json)}],
        turn_active=False,
        turn_spoke=False,
        rounds_left=0,
        ended=False,
        turn_index=0,
        metrics={},
    )
