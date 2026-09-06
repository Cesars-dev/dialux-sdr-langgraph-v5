# Dialux SDR v5 (LangGraph production build) — AGENTS.md

> Operating manual for this folder. Read before touching anything.
> Parent project: `/home/julio/projects/Retell_AI_MCP_connection` (its `AGENTS.md` is the master index).

## What this is

The LangGraph port of the deployed Dialux SDR chat agent ("Linda"): a 9-state
state machine with hard-gated transitions, typed dynamic variables, pgvector RAG,
deterministic leak math, and an LLM-to-LLM acceptance harness. Same prompts/tools/
webhooks as the deployed agent, ported 1:1 and hardened. **Snapshots = FOLDERS** in
`/home/julio/projects/Retell_AI_MCP_connection/Dialux_SDR/v5-snapshots/` (never git).

## Current status (2026-09-05)

- **Latest validated: iter15 + iter15b.** iter15 = `last_name` optional (dropped from the
  contact_details→ConfirmSlots `required`; prompt Step 2/6 + completion flag optional) +
  `MAX_TURNS_DEFAULT` 48. iter15b = contact_details Step-6 read-back now reads back EVERY
  booking dv (name — last name ONLY if given — company, confirmed number last-4 style,
  timezone). Both synced 1:1 into the embedded `agent/llm.json` `state_prompt`s.
- **Validation:** 12/13 original personas expect-match (Pedro over-book = persona
  variance, agent clean); Susan/Danny (iter14's gate-dead-end FAILs) now book with
  `last_name=None`, even at the old 28-turn cap. 3-SOP audit: report
  `…/tasks/surgeon/v5-pgvector-gpt52-argfix/12_full13_sop_audit.md`.
- **Suite: 84 passed** (`pytest tests/`). Baseline was 83 before iter15.
- **Snapshot `v1.5-iter15-20260905` predates the iter15b read-back fix** (fix is in the
  working folder only, tested).
- **NOT ported to the deployed V7.7 agent** `agent_87e4d5f08475e5bc558b2f390f` — the
  deployed line still lacks iter14+15+15b.

## Key paths (all absolute)

| What | Path |
|---|---|
| Project root | `/home/julio/projects/Retell_AI_MCP_connection/Dialux_SDR/diallux-langgraph-production-v5` |
| **This AGENTS.md** | `…/v5/AGENTS.md` |
| State machine source (9 states, edges, gates, tools, dvs) | `…/v5/agent/llm.json` — loaded directly (config.py:129); **no builder script** |
| Prompts (live source of truth; read at runtime) | `…/v5/diallux/prompts/*.md` |
| Typed dynamic variables + server-owned protection | `…/v5/diallux/schema.py` |
| Graph/tool executor (gates, webhooks, transition engine) | `…/v5/diallux/graph/{builder,llm,tools,subst}.py` |
| Langfuse tracer (traces, tool/gen spans, scores) | `…/v5/diallux/observability/tracer.py` |
| .env (secrets: OPENAI_API_KEY, LANGFUSE_HOST, DATABASE_URL) | `…/v5/.env` |
| Tests (84, `pytest tests/`) | `…/v5/tests/` |
| **LLM-to-LLM harness** | `…/v5/tests/llm2llm/harness.py` |
| **Personas (25)** | `…/v5/tests/llm2llm/personas.py` |
| Mock webhooks (hermetic tests) | `…/v5/tests/mock_webhooks.py` |
| Run transcripts (one json per call) | `…/v5/tests/llm2llm/json_logs/*.json` |
| Langfuse REST CLI (health/traces/gens/tools/costs) | `…/v5/scripts/lf.py` |
| Langfuse dataset + eval ladder runner | `…/v5/scripts/lf_eval.py` |
| Call transcript SDK | `…/v5/scripts/call.py` |
| Reports (surgeon folder) | `…/Dialux_SDR/tasks/surgeon/v5-pgvector-gpt52-argfix/` |

## Services & ports (running on this VPS)

| Service | Endpoint / port | Notes |
|---|---|---|
| Langfuse (self-hosted) | `http://localhost:3001` | docker; `LANGFUSE_HOST` in `.env`; ingest lag ~5–10 s |
| Postgres (pgvector RAG) | `postgresql://diallux:diallux@localhost:5432/diallux` | container `diallux-db` |
| Live validator webhook (read-only ref) | `…/validator_endpoint/` (:8003) behind Caddy `slots.diallux-ai.site` | systemd `validator-service`; iter14 decoupled `phone_confirmed` |

## The 25 personas (personas.py)

- **13 original:** 4 `happy-path` (Maria, Danny, Susan, Marcus — expect book),
  5 `stress` (Carlos mean, Pedro dumb, Sofia problematic, Jorge enquiry-only,
  Daniel ai-question), 4 `curve` (Brenda, Gene, Frank, Ray).
- **12 adversarial (ported from `testing/runners/adversarial_suite.py`):**
  5 `gatekeeper` `(GK)` Sam, Priya, Boris, Bianca, Dave (expect book);
  7 `breaker` `(BRK)` Larry, Jamie, Rita, Nick, Suzy, Alan, Wendy (expect no-book).
- **Larry (question-loop) = the ASSASSIN** — tagged `"assassin": True`. His ONLY
  purpose is to break the agent. **NEVER run in a routine batch**; gated behind
  `lf_eval.py ladder --assassin`, runs once on purpose. The `all` group includes
  him — do NOT run `--personas all` as a normal battery without the gate.
- Persona schema: `name/type/expect/dynvars/opener/system`. `expect` ∈ `("book","no-book")`.
- New personas are PER-AGENT, authored from scratch (see `tests/llm2llm/README.md`).

## How to run

```bash
cd /home/julio/projects/Retell_AI_MCP_connection/Dialux_SDR/diallux-langgraph-production-v5
.venv/bin/python -m pytest tests/ -q          # expect 84 passed

# single persona / group (gpt-4.1 is the shipped model)
set -a; . ./.env; set +a
.venv/bin/python tests/llm2llm/harness.py --personas Maria --rag --langfuse --max-turns 48 --agent-model gpt-4.1

# SOP ladder (happy -> stress -> curve -> gatekeepers -> breakers; skips assassin)
.venv/bin/python scripts/lf_eval.py ladder --agent-model gpt-4.1
# OPT-IN assassin (Larry), once, on purpose:
.venv/bin/python scripts/lf_eval.py ladder --assassin
```

SOP: happy first; if a happy-path run fails, fix the agent before stress
(`--no-happy-gate` to override). Cancel all mock bookings after a batch
(mock bookings are fixtures — no real calendar).

## Langfuse SDK (langfuse 4.15.1, OTEL-based v4)

```bash
set -a; . ./.env; set +a
python scripts/lf.py health
python scripts/lf.py traces --name marcus --hours 24
python scripts/lf.py show <trace_id>            # short id OK (>=8 chars)
python scripts/lf.py tools <trace_id>
python scripts/lf.py costs --hours 24 [--name marcus]   # exact per-model USD
python scripts/lf_eval.py bootstrap             # idempotent dataset 'diallux-personas' (25 items)
python scripts/lf_eval.py status --since-min 40 # json_logs vs Langfuse harness_result scores
```

- Every `--langfuse` harness run: trace named `llm2llm-<transport>-<persona-slug>`
  (e.g. `llm2llm-graph-bookmaria`), plus a `harness_result` score (1.0/0.0 + comment).
- SDK gotcha: langfuse 4.15.1 SDK is stricter than the self-hosted server — dataset
  reads break on response parse; `lf_eval.py` uses plain REST for datasets, SDK for
  traces/scores. `get_trace_url(trace_id=...)` needs the keyword arg.
- Prices in `lf.py costs` for gpt-5.x are placeholders — set `LANGFUSE_MODEL_PRICES`.

## Rules

- 84 tests must stay green after any change; rerun `pytest tests/ -q`.
- Cross-call repetition (R5.6) between different calls is ACCEPTABLE for now (fix later);
  repetition INSIDE one call (R5.3/R5.5) is a defect. Per Julio, 2026-09-05.
- The 3-SOP quality-audit protocol lives in
  `…/docs/Testing_guidelines/full_call_analysys.md` (opencode skill `sop_call_analysis`).
- Snapshots are folders under `Dialux_SDR/v5-snapshots/`, never git. No `rm -rf` —
  `mv` to `/tmp/opencode/`. Before new harness runs check `df -i /` (abort if free < 50000).
- Never touch the production voice agent `agent_16985b5d087e56c35141983396`, the
  deployed chat agent `agent_87e4d5f08475e5bc558b2f390f`, or the live
  `validator_endpoint/` service unless explicitly approved.
- Secrets live in `.env`; never print or commit them.