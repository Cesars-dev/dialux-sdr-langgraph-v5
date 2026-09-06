"""Fake streaming LLM for deterministic graph tests.

Same interface as graph.llm.StreamingLLM.astream(messages, tools). Each round
pops the next scripted response. Scripts can depend on the system prompt so we
can verify state swaps (mid-turn transition semantics).
"""
from __future__ import annotations

import json
from typing import AsyncIterator


class FakeLLM:
    def __init__(self, rounds: list[dict] | None = None):
        # round: {"tokens": [...], "tool_calls": [{"id","name","arguments"}], "match_system": "Discovery"}
        self.rounds: list[dict] = list(rounds or [])
        self.seen_systems: list[str] = []
        self.seen_tools: list[list[str]] = []

    def add_round(self, round_: dict):
        self.rounds.append(round_)

    async def astream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[dict]:
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        self.seen_systems.append(system)
        self.seen_tools.append([t["function"]["name"] for t in tools])
        script = self.rounds.pop(0)
        if script.get("match_system"):
            assert script["match_system"] in system, (
                f"expected state prompt {script['match_system']!r} in system, got: {system[:200]}"
            )
        for tok in script.get("tokens", []):
            yield {"type": "token", "text": tok}
        usage = script.get("usage") or {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        yield {
            "type": "final",
            "content": "".join(script.get("tokens", [])),
            "tool_calls": script.get("tool_calls", []),
            "usage": usage,
        }


def tool_call(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {"id": call_id, "name": name, "arguments": json.dumps(args)}
