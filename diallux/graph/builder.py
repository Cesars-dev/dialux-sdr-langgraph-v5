"""LangGraph builder — V3 production graph.

V1 translated the Retell runtime 1:1. V2 kept the same 9 state nodes and
loop semantics, plus:

  1. Prompts are read from `diallux/prompts/*.md` (editable source of truth,
     same content as the deployed llm.json — verified identical at build time).
  2. A `deterministic` node runs after EVERY state round — your "fix queued in
     the deterministic layer" open items, finally real:
       - leak math: when the three inputs exist and weekly_leak is empty,
         compute it server-side (the Closer can no longer stochastically skip
         the monetization pitch — open item #1 in your AGENTS.md)
       - today prefetch: entering ConfirmSlots with an empty today_date fetches
         /today directly (time never depends on the model remembering the tool)
  3. dvs flow through schema.DynamicVariables (typed; "" defaults; server-owned
     keys protected — see graph/tools.py).
  4. Transitions are hard-gated by the executor (edge `required` params must be
     truthy); the graph structure itself is unchanged because the executor is
     the single door for state swaps.
  5. Optional Postgres checkpointer (LANGGRAPH_CHECKPOINT=postgres) for
     multi-worker durability; MemorySaver fallback per call.

V3 adds (see ITERATIONS.md):
  6. RAG knowledge bases: pgvector top-3 retrieval per turn (the DEPLOYED
     Retell kb_config was {filter_score: 0.6, top_k: 3} — server-side RAG,
     now self-hosted). Inline full-KB fallback when Postgres is unavailable.
  7. VOICE OUTPUT RULES appended to the system prompt (Cartesia's voice-agent
     starter rules) so the model writes text the TTS reads well; the
     deterministic normalizer (media/normalize.py) backs it up per sentence.

One graph invocation == one user utterance (unchanged).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from ..config import Settings
from .. import rag as ragmod
from ..schema import DynamicVariables
from ..state import GraphState, initial_state
from .llm import StreamingLLM, build_tool_schemas
from .subst import Substitutor
from .tools import ToolExecutor, calculate_monthly_leak, leak_inputs_present

# V3: TTS-friendly output rules — adapted from Cartesia's voice-agent starter
# prompt (docs.cartesia.ai, prompting tips). ON in production: the LLM itself
# writes speakable text; media/normalize.py catches what it misses.
VOICE_OUTPUT_RULES = """

## VOICE OUTPUT RULES (your text is spoken by a real-time TTS engine)
- Plain conversational prose. No markdown, bullet points, emoji, or special characters — they get read aloud.
- Always finish sentences with . ? or !
- Phone numbers, verification codes, and any run of 5+ digits: one character at a time, comma-separated, like 3, 1, 2, 4, 0, 0, 1, 2, 3, 4.
- Dollar amounts, dates, times: write them normally ($6,500, September 4th, 2:30 PM) — the engine reads them naturally.
- Tool arguments are never spoken; only your reply text is.
"""


class CallRuntime:
    """Everything scoped to ONE phone call: graph, llm, executor, tracer."""

    def __init__(
        self,
        settings: Settings,
        llm_json: dict,
        tracer=None,
        llm: StreamingLLM | None = None,
        http_client=None,
        checkpointer=None,
        kb_store=...,   # ellipsis = resolve lazily; None = force off; object = injected (tests/eval)
    ):
        self.settings = settings
        self.llm_json = llm_json
        self.states: dict[str, dict] = {s["name"]: s for s in llm_json.get("states", [])}
        self.subst = Substitutor(settings.knowledge_base_dir)
        self.tracer = tracer
        self.llm = llm or StreamingLLM(settings)
        self.executor = ToolExecutor(settings, llm_json, tracer=tracer, http_client=http_client)
        self.prompts = self._load_prompts()
        self._kb_store = kb_store
        self._kb_store_resolved = kb_store is not ...
        self._kb_stats = {"rag_turns": 0, "rag_ms_total": 0.0, "rag_chars": 0}
        self.graph = self._build(checkpointer)

    async def aclose(self):
        await self.executor.aclose()

    # ------------------------------------------------------------------ #
    async def _resolve_kb_store(self):
        """Lazily attach the process-wide KBStore (pgvector) once."""
        if not self._kb_store_resolved:
            self._kb_store_resolved = True
            try:
                self._kb_store = await ragmod.get_kb_store(self.settings)
            except Exception:
                self._kb_store = None
        return self._kb_store

    def kb_slugs_for(self, state_name: str) -> list[str]:
        """KBs visible in a state = general_prompt's + the state prompt's
        (mirrors Retell's KB attachment, now the retrieval scope)."""
        general = Path(self.settings.prompts_dir) / "general_prompt.md"
        general_text = general.read_text(encoding="utf-8") if general.exists() \
            else self.llm_json.get("general_prompt", "")
        slugs = ragmod.kb_slugs_in(general_text)
        slugs += [s for s in ragmod.kb_slugs_in(self.prompts.get(state_name, ""))
                  if s not in slugs]
        return slugs

    # ------------------------------------------------------------------ #
    def _load_prompts(self) -> dict[str, str]:
        """V2: prompts from diallux/prompts (source of truth, editable).
        Falls back to llm.json's embedded state_prompt when a file is absent."""
        prompts_dir = Path(self.settings.prompts_dir)
        out: dict[str, str] = {}
        for name, cfg in self.states.items():
            p = prompts_dir / f"{name}.md"
            out[name] = p.read_text(encoding="utf-8") if p.exists() else cfg.get("state_prompt", "")
        return out

    def system_message(self, state_name: str, dvs: dict, expand_kb: bool = True) -> str:
        general = Path(self.settings.prompts_dir) / "general_prompt.md"
        general_text = general.read_text(encoding="utf-8") if general.exists() \
            else self.llm_json["general_prompt"]
        return self.subst.subst(general_text + "\n\n" + self.prompts.get(state_name, ""),
                                dvs, kb=expand_kb)

    # ------------------------------------------------------------------ #
    def _make_state_node(self, state_name: str) -> Callable[[GraphState], dict]:
        runtime = self

        async def state_node(state: GraphState) -> dict:
            writer = get_stream_writer()
            dvs = DynamicVariables.from_flat(state.get("dvs", {})).to_flat()
            history = state.get("history", [])

            # ---- V3 RAG: retrieval instead of full-KB inline expansion ----
            rag_ms = 0.0
            rag_kbs: list[str] = []
            kb_store = await runtime._resolve_kb_store()
            expand_kb = kb_store is None          # inline fallback only when no store
            system = runtime.system_message(state_name, dvs, expand_kb=expand_kb)
            if kb_store is not None:
                user_msg = next((m["content"] for m in reversed(history)
                                 if m.get("role") == "user"), "")
                query = f"[state: {state_name}] {user_msg}"
                t_rag = time.perf_counter()
                chunks = await kb_store.retrieve(query, runtime.kb_slugs_for(state_name))
                rag_ms = round((time.perf_counter() - t_rag) * 1000, 1)
                rag_kbs = sorted({c["kb"] for c in chunks})
                system = ragmod.strip_kb_markers(system) + ragmod.render_knowledge_section(chunks)
                runtime._kb_stats["rag_turns"] += 1
                runtime._kb_stats["rag_ms_total"] += rag_ms
                runtime._kb_stats["rag_chars"] += sum(len(c["content"]) for c in chunks)
                if runtime.tracer:
                    runtime.tracer.span("rag", output={
                        "state": state_name, "ms": rag_ms, "kbs": rag_kbs,
                        "chunks": len(chunks), "query": query[:200]})

            if runtime.settings.tts_voice_rules:
                system += VOICE_OUTPUT_RULES

            tools = build_tool_schemas(
                runtime.states[state_name],
                runtime.llm_json.get("general_tools", []),
                lambda text: runtime.subst.subst(text, dvs, kb=expand_kb),
                dvs,
            )
            messages = [{"role": "system", "content": system}] + list(history)

            t0 = time.perf_counter()
            gen_obs = runtime.tracer.start_llm(state_name, runtime.settings.openai_model,
                                               history[-6:]) if runtime.tracer else None
            content = ""
            tool_calls: list[dict] = []
            usage = None
            separated = False   # iter16: one space between state texts in a multi-state walk
            async for ev in runtime.llm.astream(messages, tools):
                if ev["type"] == "token":
                    if state.get("turn_spoke") and not separated:
                        writer({"tts_token": " "})
                        separated = True
                    writer({"tts_token": ev["text"]})
                elif ev["type"] == "final":
                    content = ev["content"]
                    tool_calls = ev["tool_calls"]
                    usage = ev.get("usage")
            llm_s = time.perf_counter() - t0
            if runtime.tracer:
                runtime.tracer.finish_llm(
                    gen_obs, output=content, latency_s=llm_s, usage=usage,
                    tool_calls=[{"name": tc["name"], "args": tc["arguments"]} for tc in tool_calls],
                )

            updates: dict[str, Any] = {"assistant_text": content,
                                       "turn_spoke": bool(content) or bool(state.get("turn_spoke"))}
            new_history: list[dict] = []
            if tool_calls:
                new_history.append({
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for tc in tool_calls
                    ],
                })
                dvs_patch: dict[str, Any] = {}
                new_state: str | None = None
                ended = False
                executed: list[dict] = []
                t1 = time.perf_counter()
                for tc in tool_calls:
                    try:
                        args = json.loads(tc["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    outcome = await runtime.executor.execute(tc["name"], args, dvs, state_name,
                                                             spoken_text=content)
                    executed.append({"name": tc["name"], "ok": bool(outcome.response.get("ok", True))})
                    dvs_patch.update(outcome.dvs_patch)
                    dvs.update(outcome.dvs_patch)   # same-round visibility (engine parity)
                    if outcome.new_state:
                        new_state = outcome.new_state
                    ended = ended or outcome.ended
                    new_history.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(outcome.response)[:4000],
                    })
                if new_state:
                    updates["state_name"] = new_state
                if ended:
                    updates["ended"] = True
                updates["dvs"] = dvs_patch
                updates["history"] = new_history
                updates["last_round_tool_calls"] = executed
                updates["rounds_left"] = state.get("rounds_left", 0) - 1
                updates["metrics"] = {
                    "llm_s": round(llm_s, 3),
                    "tools_s": round(time.perf_counter() - t1, 3),
                    "calls": [tc["name"] for tc in tool_calls],
                    "rag_ms": rag_ms,
                    "rag_kbs": rag_kbs,
                }
            else:
                new_history.append({"role": "assistant", "content": content or ""})
                updates["history"] = new_history
                updates["last_round_tool_calls"] = []
                updates["metrics"] = {"llm_s": round(llm_s, 3), "tools_s": 0.0,
                                      "calls": ["<speak>"], "rag_ms": rag_ms,
                                      "rag_kbs": rag_kbs}
            return updates

        state_node.__name__ = f"state_{state_name}"
        return state_node

    # ------------------------------------------------------------------ #
    async def _deterministic_node(self, state: GraphState) -> dict:
        """Runs after every state round. Deterministic, no LLM, no speech.

        - leak math auto-compute (open item #1: the pitch can't be skipped)
        - /today prefetch when entering ConfirmSlots without a date (open item #3)
        """
        dvs = DynamicVariables.from_flat(state.get("dvs", {})).to_flat()
        state_name = state.get("state_name", "")
        patch: dict[str, Any] = {}

        if leak_inputs_present(dvs) and not dvs.get("weekly_leak"):
            result, leak_patch = calculate_monthly_leak(dvs)
            if result.get("ok"):
                patch.update(leak_patch)
                if self.tracer:
                    self.tracer.span("deterministic:leak_math", output=result)

        if state_name == "ConfirmSlots" and not dvs.get("today_date"):
            outcome = await self.executor.execute("check_current_date", {}, dvs, "ConfirmSlots")
            if outcome.dvs_patch.get("today_date"):
                patch["today_date"] = outcome.dvs_patch["today_date"]
                if self.tracer:
                    self.tracer.span("deterministic:today_prefetch", output=outcome.dvs_patch)

        if not patch:
            return {}
        return {"dvs": patch}

    # ------------------------------------------------------------------ #
    def _build(self, checkpointer=None):
        async def ingest(state: GraphState) -> dict:
            return {
                "history": [{"role": "user", "content": state.get("user_text", "")}],
                "turn_active": True,
                "turn_spoke": False,
                "rounds_left": self.settings.max_tool_rounds,
                "last_round_tool_calls": [],
                "user_text": "",
                "turn_index": state.get("turn_index", 0) + 1,
            }

        async def finalize(state: GraphState) -> dict:
            return {"turn_active": False}

        def route_state(state: GraphState) -> str:
            if state.get("ended"):
                return "finalize"
            name = state.get("state_name", self.llm_json.get("starting_state", "Intake"))
            return name if name in self.states else "finalize"

        def after_state(state: GraphState) -> str:
            if state.get("ended"):
                return "finalize"
            executed = state.get("last_round_tool_calls") or []
            if not executed:
                return "finalize"                    # pure speech round -> turn done
            if state.get("rounds_left", 0) <= 0:
                return "finalize"
            return "deterministic"                   # V2: deterministic layer between rounds

        builder = StateGraph(GraphState)
        builder.add_node("ingest", ingest)
        for name in self.states:
            builder.add_node(name, self._make_state_node(name))
        builder.add_node("deterministic", self._deterministic_node)
        builder.add_node("finalize", finalize)

        builder.add_edge(START, "ingest")
        builder.add_conditional_edges("ingest", route_state,
                                      {**{n: n for n in self.states}, "finalize": "finalize"})
        for name in self.states:
            builder.add_conditional_edges(name, after_state,
                                          {**{n: n for n in self.states},
                                           "deterministic": "deterministic",
                                           "finalize": "finalize"})
        # deterministic layer routes back into the (possibly new) state node
        builder.add_conditional_edges("deterministic", route_state,
                                      {**{n: n for n in self.states}, "finalize": "finalize"})
        builder.add_edge("finalize", END)
        return builder.compile(checkpointer=checkpointer or MemorySaver())

    # ------------------------------------------------------------------ #
    def initial_state(self, call_id: str, persona_dvs: dict | None = None) -> GraphState:
        return initial_state(call_id, self.llm_json, persona_dvs)


def build_checkpointer(settings: Settings):
    """Optional Postgres checkpointer (multi-worker / restart durability).
    Set LANGGRAPH_CHECKPOINT=postgres and DATABASE_URL=postgresql://...
    Falls back to per-call MemorySaver when the package or DB is unavailable."""
    if settings.checkpoint_backend != "postgres":
        return None
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async def _make():
            saver = AsyncPostgresSaver.from_conn_string(settings.database_url)
            await saver.setup()                      # one-time migrations
            return saver
        return _make()
    except Exception as exc:
        print(f"[checkpoint] postgres unavailable ({exc}); using MemorySaver")
        return None
