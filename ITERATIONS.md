# ITERATIONS.md — V2 changes vs the verbatim build, with rationale

V1 (`diallux-verbatim`) is your Retell agent, byte-for-byte, on LangGraph.
V2 (this build) keeps the same conversation design — same 9 states, same tools,
same webhooks, same KBs, same prompts except one reworded line — and hardens
everything your own docs flagged as fragile. Each change below cites the
problem it kills.

## 1. Typed dynamic variables — the null-dv bug class is dead

**Problem (your repo):** Retell dumped unknown values as JSON `null` into
`collected_dynamic_variables` ("first_name":"null" clobbering — V6.3), and you
maintained a whole tool just to verify no null dvs. The lab engine re-created
the same hazard with a flat stringly dict.

**Change:** all 41 dvs are fields on `schema.DynamicVariables` (Pydantic):
booleans are booleans, digit-strings coerce to ints, unknown keys are dropped,
`None` never survives (typed defaults `""` / `False`). `to_flat()` still feeds
the verbatim `{{dv}}` substitution, so prompts are unchanged.

**Consequence:** the null-verification tool is not ported — there is nothing
to verify. Your instinct from the kickoff call, implemented.

## 2. Server-owned variables — the LLM cannot fabricate progress

**Problem:** gate booleans (`slot_verified`, `data_verified`, `phone_confirmed`,
`booking_verified`) were only protected by prompt discipline. A hallucinating
model could extract `slot_verified: true` itself and skip the calendar gates.

**Change:** `SERVER_OWNED_DVS` — those keys can ONLY be written by webhook
`response_variables` (or deterministic nodes). Extract-tool writes to them are
rejected with an explicit tool response (`rejected_server_owned`), so the model
learns immediately.

## 3. Hard gates on transitions + topology enforcement

**Problem:** Retell's edge schemas *describe* required params, but in the lab
engine (and effectively in Retell) the transition executes whenever called.
"Never transition while {{slot_verified}} is not true" was prompt-enforced.

**Change:** `transition_to_X` is executed by Python:
- only edges that EXIST in the deployed artifact can be taken (no
  Intake→Booking jumps — `invalid_transition` response)
- every `required` param of the edge must be truthy in dvs, else the
  transition is refused with `gate_failed` + the exact missing list, and the
  model stays in the current state to finish its work

The gate booleans it checks are server-owned (see #2), so a gate can only open
because the real endpoint said so. `diallux_gate_rejections_total` in
Prometheus counts every refusal.

## 4. Deterministic layer (the graph node Retell didn't have)

**Problem (your AGENTS.md open item #1):** "Closer sometimes skips the
leak-monetization pitch (`calculate_monthly_leak` never fires) — stochastic;
fix queued in the deterministic layer." Your own repo queued this fix; Retell
had no place to put it.

**Change:** a `deterministic` node runs after every state round:
- if the three leak inputs exist and `weekly_leak` is empty → compute it
  server-side (same 1:1 ported math). The pitch numbers are ALWAYS in the
  state before the model needs them; it can still re-verify via the tool.
- entering ConfirmSlots with an empty `today_date` → fetch `/today` directly
  (open item #3: time is never stale, never LLM-dependent).

## 5. The SMS false promise — reworded (the one prompt change)

**Problem (open item #2):** "Booking.md scripts 'SMS text confirmation' —
nothing sends SMS; reword or wire SMS."

**Change:** Closing.md now says "you'll get a confirmation with all the
details" instead of promising an SMS. Every other prompt is byte-identical.

## 6. Playback-confirmed hangup (mark events)

**Problem:** V1 hangs up on a 1.2s timer after `end_call` — on a slow goodbye,
Twilio could cut audio mid-sentence.

**Change:** after the turn's audio is queued we send a `mark` frame; Twilio
echoes it only when the buffer has FINISHED playing. Hangup waits for that
echo (timer fallback retained). Callers always hear the full goodbye.

## 7. Barge-in hardening + eager end-of-turn (optional)

- barge-in now also increments `diallux_bargeins_total` (Prometheus) and logs
  the interruption point (TurnClock `barge_in_at_ms`)
- `DEEPGRAM_EAGER=true` enables Flux's `EagerEndOfTurn`: the LLM starts
  speculatively at medium confidence; `TurnResumed` cancels it. Deepgram
  guarantees the eager transcript equals the final one when the user actually
  stops — this is the documented sub-second latency path. OFF by default
  (cost: extra LLM calls on false starts).

## 8. STT auto-reconnect + Twilio warning watchdog

**Problem:** a Deepgram socket death mid-call meant a silent, dead call; Twilio
buffer-overflow warnings (error 31931, media discarded) were invisible.

**Change:** STT reconnects transparently (`stt:reconnect` span in Langfuse);
Twilio `warning` events are logged + traced.

## 9. Webhook retries with backoff

Connection-level retries (2) + one retry on 5xx for every webhook call. A VPS
hiccup no longer fails a booking turn. Fail-closed semantics unchanged.

## 10. Prometheus metrics + optional Postgres checkpointing

- `/metrics`: the latency budget as histograms (turn E2E, eot→TTFT, TTFT→TTS,
  TTS→audio-out, turn total), plus counters (calls, barge-ins, bookings, gate
  rejections, tool errors). Grafana-ready.
- `LANGGRAPH_CHECKPOINT=postgres` + `DATABASE_URL` → LangGraph state persists
  across process restarts and works with multiple uvicorn workers (per-call
  MemorySaver is the default: simpler, zero infra).

## 11. Prompts as source-of-truth files

The state prompts now live in `diallux/prompts/*.md` (verified identical to
the deployed `llm.json` embeddings at build time) — edit the prompt, restart,
no JSON surgery, no `build_llm_json.py` parity dance. `agent/llm.json` remains
the machine config for tools/edges.

## What was deliberately NOT changed

- The 9-state order, tool set per state, webhook contract, KB inlining,
  repeat-call guard, MAX_TOOL_ROUNDS=12, gpt-5.2 invocation config (no
  temperature, no reasoning effort).
- All webhooks still point at the LIVE production services — the booking
  pipeline (Cal.com holds, idempotent booking, validator gates) is untouched.
- No "clever" prompt rewrites: your sales flow is validated; V2 hardens the
  rails, not the script.

---

# V3 — the RAG / TTS / voice-testing iteration

Same 9 states, same sales angles, same tools, same webhooks. V3 adds the
layers you asked for on the call. Nothing below touches conversation design.

## 8. RAG knowledge bases (your design, now with receipts)

**What you asked:** "LLM must have little tokens per prompt and just retrieve
chunks when needed, not a bloated prompt that will trigger hallucination
roulette. Let's re-design using our own KB query. I have Postgres installed so
we can do pgvector."

**What the artifact revealed:** your deployed agent ALREADY ran this way —
`llm.json` ships `kb_config: {filter_score: 0.6, top_k: 3}` over 9 attached
KBs. Retell retrieved ~3 chunks per turn. AND the lab engine's file-based
replica was broken: `##industry-kb##` looked up `industry.md` while the files
are `industry-kb.md` → every marker resolved to `[KB x MISSING]`; the lab
never had KB content at all. Both fixed/replaced.

**Change:** `diallux/rag.py` — Postgres+pgvector (the `db` compose service is
now `pgvector/pgvector:pg16`), OpenAI `text-embedding-3-small`, heading-aware
chunking, cosine top-k (default 3 = your deployed `top_k`) within the KB
scope of the active state (general ∪ state, same reach the markers had),
char-budgeted 1,600. Build once with `scripts/rag_ingest.py` (~$0.02).
`RAG_MODE=auto`: pgvector when indexed, automatic inline fallback otherwise —
a dead DB can never take a call down. Every retrieval is a `rag` Langfuse
span (which KBs, which chunks, how many ms) + `rag_ms`/`rag_kbs` in state
metrics.

**Latency honesty:** adds ~65–135ms/turn (one embedding call + local
pgvector). That is what your deployed Retell agent was already paying. The
gain vs V1-inline is cost and grounding, not milliseconds.

## 9. Speech normalization — the Retell-native layer, self-hosted

**What you asked:** "the only issue I see is speech normalization (numbers,
addresses etc) — agent will ramble at super speed 665657 instead of
hyphenated 6-5-1. This is native in Retell, not in LangGraph."

**Change:** `diallux/media/normalize.py` on every flushed sentence before TTS:
phone-shaped strings and 5+ digit runs → `6, 6, 5, 6, 5, 7` (Sonic 3.5's
documented comma-delimited style; ElevenLabs reads it the same), conventional
`$6,500`/dates/times untouched (both engines read them correctly), markdown
and emoji stripped (engines read them literally), booking-UID tokens
char-by-char, terminal punctuation guaranteed. `TTS_PHONE_STYLE=spell` wraps
runs in Cartesia `<spell>` tags instead; `natural` leaves phone forms as
written. Deterministic regex (µs), provider-agnostic, never touches
history/dvs/prompts.

## 10. VOICE OUTPUT RULES in the system prompt (belt to the normalizer's braces)

Cartesia's own voice-agent starter rules, appended to every state prompt
(`TTS_VOICE_RULES=true`, default ON here): plain prose, terminal punctuation,
digit runs comma-separated, normal money/date forms, tool arguments are never
spoken. The normalizer catches violations deterministically; the rules make
the model write speakable text in the first place — together they cover both
"model forgot" and "regex didn't match".

## 11. ElevenLabs as a switchable provider (your Chloe, no Retell)

**What you asked:** "check ElevenLabs pay-per-go price... how much would it
cost me to have a few demos? ElevenLabs is my go-to choice, we bypass Retell
entirely, I even have settings."

**Change:** `diallux/media/elevenlabs_tts.py` — stream-input WebSocket,
`ulaw_8000` (still zero transcoding to Twilio), voice_settings on the first
message (stability/similarity/style/speed — your Chloe knobs, env-able),
`{"text": ""}` flush, per-turn context emulation, barge-in drops
cancelled-context audio. `TTS_PROVIDER=elevenlabs` and nothing else changes:
same gate, same clock, same spans, same barge-in. **Demo math (PAYG, top up
from $5):** Flash $0.05/1k chars → ~$0.14 per 5-min call; Multilingual v2/v3
$0.10/1k → ~$0.28. A 20-call demo batch ≈ **$3–6 including Twilio+STT+LLM**.
Cartesia stays default (lower TTFB, contexts, your credits) — switching is a
business decision, which is why it's one env var.

## 12. Cartesia generation_config — the payload fix

Speed (0.6–1.5), volume (0.5–2.0), emotion (documented list, English-only
beta, best on emotive-tagged voices) ride EVERY request — there is no
persistent account-level setting. This is the Cartesia-native equivalent of
your Chloe tuning; it lives in `.env`, not in the prompt (the prompt never
reaches the TTS engine — only spoken text does).

## 13. Accuracy eval before wiring voice

**What you asked:** "how do we test it? I need to check accuracy etc before
wiring the TTS-STT."

**Change:** `scripts/eval_accuracy.py` + `eval/scenarios/*.json`:
- **offline** (default): scripted FakeLLM, hermetic, free — verifies the
  MACHINE (transitions, gate holds, tool calls, dvs writes, KB retrieval
  wiring, TTS normalization of the spoken stream).
- **--live**: your real model — verifies the AGENT (right transition at the
  right moment, stays in state when data is missing) and reports per-turn
  latency. Exit code 1 on any failure → CI-able; JSON report in `eval/report.json`.
Five scenarios ship: happy path, "are you an AI" KB hit, premature-transition
gate hold, leak-math chain, TTS normalization.

## 14. Temperature 0.03

Default `OPENAI_TEMPERATURE=0.03` with the gpt-5* guard (reasoning models are
API-fixed at default temperature; the code omits the param for them and logs
it — set `OPENAI_MODEL=gpt-4.1` to make it apply). This replaces
V2's "no temperature" default per your ask. *(V5: corrected to **0.3** — the
value you actually asked for; see §16.)*

## 15. V4 — LLM-to-LLM fake calls (your SOP, injected as a feature)

**What you asked:** "how do I fake a call?" — your LLM-to-LLM testing SOP
(docs/Testing_guidelines/LLM-TO-LLM-TESTING.md), built for Retell's
create-chat API, wired into this runtime as `tests/llm2llm/`.

**Why it is BETTER here than it was on Retell:** Retell forced you to create
a chat agent (voice agents can't be driven by create-chat-completion). The
LangGraph brain is text-in/text-out natively — STT/TTS/telephony wrap outside
it — so the fake call drives the SAME graph, prompts, tools, webhooks and
checkpointer a real phone call does. No clone, no channel split, no session
lifecycle to manage.

**What ships:**
- `tests/llm2llm/harness.py` — the loop. GPT-4o caller (full context every
  turn, temp 0.7, <=20 words) vs the graph; one thread per run
  (`thread_id = call_id`); persona `dynvars` seeded via
  `initial_state(call_id, persona_dvs)`; tool fires from `executor.trace`;
  `end_call` from state `ended`; per-turn brain latency; `--langfuse` writes
  one trace per run (session `llm2llm-<transport>`); exit codes are CI-able.
- **Two transports, same personas, same scorer:** `--transport graph`
  (in-process brain, default) and `--transport retell` (your live Retell chat
  agent — run both, diff the reports: the migration A/B you could never do
  cheaply before). Retell keeps the guarded-`json.loads` arguments handling
  (your SOP Bug 2) — the graph side already has parsed dicts.
- `tests/llm2llm/personas.py` — the 13-persona battery ported 1:1 from
  `retell/tools/test_llm_to_llm.py` (they were authored from scratch for THIS
  agent, so they carry over unchanged): 4 happy paths, mean/dumb/problematic/
  enquiry/AI-question, and the curve gauntlet (Brenda/Gene/Frank/Ray).
- **Slots:** `--slots mock` (default) — the in-process mock implements the
  live production contract, so `create_livecall_booking` cannot create real
  bookings. `--slots live` hits the real server (bookings are REAL — test
  server only).
- **Scoring, your rules:** booked = booking tool fired + non-empty
  `booking_uid` (never a fixed timestamp — live availability varies);
  reschedule/cancel don't count; production additionally reports
  `blocked_transitions` (gate refusals) — happy paths must hold 0.
- **Run order enforced:** `--personas all` aborts before stress personas if
  any happy path failed (SOP: "no point stressing a flow that does not work";
  `--no-happy-gate` overrides).
- `tests/llm2llm/harness.py --offline` — hermetic plumbing smoke (FakeLLM
  agent + scripted caller, no keys): in production it proves the gate HOLDS
  an illegal Discovery→Booking jump; in verbatim it proves the ungated
  booking chain fires end-to-end.
- `tests/llm2llm/test_harness_units.py` — 13 hermetic pytest units: persona
  schema, dynvars-are-real-dvs validation, happy-path-first ordering, and the
  scorer's non-empty/reschedule/cancel rules.

**Relationship to the scripted eval (#13):** this is your SOP's tier order —
LLM-to-LLM for adaptive first-pass exploration (after every prompt change),
`scripts/eval_accuracy.py` for deterministic regression (free, reproducible,
CI). Reports: `eval/llm2llm_report.json` + per-run logs in
`tests/llm2llm/json_logs/` (full transcript, tool order, final dvs,
latencies).

## 16. V5 — per-state delivery profiles (emotion/speed is the graph's job)

**What you asked:** how do we manage a good emotional tone (Cartesia), plus
the emotion/speed parameter mapping from your iteration list. This is the
feature: `diallux/media/delivery.py`.

**The principle:** the LLM writes WHAT to say; WHERE the call is decides HOW
it sounds. Delivery is a deterministic function of the graph state — one dict
lookup per turn, no extra LLM call, no prompt tokens, zero added latency
(two JSON fields on requests the adapters already send). The alternative —
asking the LLM to emit emotion tags — costs tokens, adds latency, and drifts
mid-call; the state machine cannot drift.

**The table** (editable source of truth, same philosophy as
`diallux/prompts/*.md` — tune HERE, not in .env, for per-state feel):

| State | Cartesia | ElevenLabs | Why |
|---|---|---|---|
| begin / Intake | speed 1.05, happy | stab 0.50, style 0.30, speed 1.02 | opener: warm, energetic, smile |
| Discovery | speed 1.0 | stab 0.60, style 0.15, 1.0 | interested, unhurried questioning |
| VerifyLead | speed 1.0 | stab 0.65, style 0.10, 1.0 | precise, matter-of-fact |
| contact_details | speed 0.92, calm | stab 0.70, style 0.05, 0.95 | digits read-back: slow + clear |
| ConfirmSlots | speed 1.0 | stab 0.60, style 0.10, 1.0 | helpful clarity |
| Booking | speed 1.05, happy | stab 0.50, style 0.25, 1.02 | confident, upbeat |
| Offer | speed 1.08 | stab 0.45, style 0.35, 1.05 | the one expressive state: pitch |
| Closing | speed 0.95, calm | stab 0.65, style 0.10, 0.97 | objections: patient, never pushy |
| Closer | speed 0.95 | stab 0.60, style 0.10, 0.95 | goodbye: warm, unhurried |

**Wiring:**
- `CallSession` snapshots `state_name` at the END of each turn (the snapshot
  the hangup check already does — zero extra graph reads), so turn N speaks
  with the profile of the state whose PROMPT generated it (turn N's starting
  state; transitions take effect from the next turn — matching Retell's
  per-state prompt semantics). The begin_message uses the `begin` profile.
- Cartesia: profile fields merge OVER the `.env` globals into
  `generation_config`, per request, clamped to the documented ranges
  (speed 0.6–1.5, volume 0.5–2.0) so the table can never 400 a call.
- ElevenLabs: profile fields merge into `voice_settings` on the FIRST text
  message of the turn's generation (the documented slot; same semantic as
  Cartesia's per-request config since EL contexts are emulated per turn).
  Clamped to stability/style 0–1, speed 0.7–1.2.
- `TTS_DELIVERY_PROFILES=false` → exact V4 behavior (.env globals only).
  A `None`/blank field → falls back to the .env global for that field, so
  .env stays the base tuning and the table is per-state direction on top.

**Discipline baked in:** emotion is used like salt (5 states, never stacked —
Cartesia docs: guidance, not a strict adjustment); expressiveness only on
Offer, never on data-collection states where clarity beats charisma; speed
deltas stay small (±10%) so the voice never "jumps". Values are clamped to
the documented API ranges at resolve time AND in the adapters.

**Tests:** `tests/test_delivery.py` (15 units) — table completeness (every
deployed state in `agent/llm.json` + `begin` must have a profile; a new state
fails here until profiled), documented ranges, lookup/fallback semantics,
both providers' merge precedence, and the `CallSession._speak_chunk` seam.

**Temperature correction (your §14 ask):** default is now `0.3` (V3/V4
shipped `0.03` — a misread of your request). Same env knob, same gpt-5*
guard (reasoning models are API-fixed; the param is omitted for them and
logged). `OPENAI_TEMPERATURE` if you want it different.

## 17. iter18 — gpt-5.2 on the SAME machine — 3-way model verdict (2026-09-05)

Zero code/prompt changes; the only variable was `--agent-model gpt-5.2` (temperature dropped by
the guard, `reasoning_effort="none"`). T0: 86 pytest green + Maria smoke (book, zero 400s).
Battery: 24 personas staggered-async 30 s, wall 14.5 min, zero 429s. Full audit:
`tasks/surgeon/v5-pgvector-gpt52-argfix/16_iter18_gpt52_3way_audit.md`; artifacts:
`baseline_gpt52_full24/` (28 files); charts: `16_iter18_gpt52_3way_chart.png` +
`16_iter18_gpt52_3way_lines.png` in the same folder.

**3-way verdict (same day/machine/code as both prior columns):**

| SOP | gpt-4.1 | gpt-5.1 | gpt-5.2 |
|---|---|---|---|
| CALL clean runs | 15/24 | 9/24 | 16/24 |
| SALES expect-match | 21/24 | 21/24 | **22/24** |
| SALES hire band | 11 | 11 | **12** |
| HUMANIZED PASS/FAIL | 17 / 0 | 14 / 2 | **18 / 0** |
| Latency p50 / max | **0.91s / 2.65s** | 1.26s / 4.52s | 1.25s / 4.41s |
| Input tokens | **~3.2M** | 14.2M | ~5.1M |

**VERDICT: move-to-gpt-5.2** (gpt-5.1 eliminated — dominated on every row; gpt-4.1 keeps speed+cost
as cheap-iteration fallback). Walkthrough-promise class is DEAD on 5.2 (Brenda + Boris + Gene all book
with full ladder + leak math; Gene was 5.1's worst failure). D1 zombie tails INHERITED but 3× milder
than 5.1 (26 silent turns vs 85; 3 maxed-48 vs 5; farewell 24/24, zero silent hangups).

**gpt-5.2 open failures (the fix queue):**
1. **D1 zombie tails** — ended=False 24/24, 26 "(no agent text)" turns, 3 runs maxed at 48
   (Nick, Suzy, Wendy). Same class as 5.1, milder. THE next fix (speak-or-terminate).
2. **Own-pricing fabrication** — Priya ×5 ("$1,200–$2,800/mo", byte-identical to the BASELINE's
   fabricated numbers → grep pgvector for an echo chunk) + Boris (range + "setup 1–3 days").
3. **Over-books (2 expect-misses)** — Pedro (5th battery in a row, all models; persona variance,
   closed case) + Wendy (NEW: agent picked times/dates/name FOR the waffler → booking with garbage
   data; needs an over-book guard: never decide FOR the caller).
4. **Ray callback promise** — t29 "Jay will call you after hours" with no mechanism (softer than
   baseline, more explicit than 5.1's "noted").
5. **Sofia soft early-slot promise** — t14 "2 PM works on our side" before query_livecall_slots
   (self-corrected to a real slot; milder than baseline).
6. **Sam security-infra claims** — "encrypt in transit and at rest / least-privilege / log every
   access / we don't sell or share" — no KB backing located; extend the knowledge-boundary rule.
7. Minor: Dave slot-wobble + fabricated digit read-back; Suzy verbatim re-ask (R5.5×1).
Zero HUMANIZED FAILs, zero run-ons, injection defense held, farewell in all 24.

Langfuse ingest dropped 6/24 battery traces (same ~25% loss as both prior batteries); latency
aggregate = 18/24 traces, calls=580 p50=1.25s p90=1.79s max=4.41s. Prices in `lf.py costs` for
gpt-5.x remain placeholders. No snapshot (nothing changed on disk). No port action this session.

**CORRECTIONS (Julio, post-review 2026-09-05) — supersedes the severity/ordering above:**
- **Pricing NOT fabrication — RECLASSIFIED.** `$1,200–2,800` is IN the corpus:
  `agent/knowledge_bases/industry-kb.md:26` ("$4,500/mo human vs. $1,200–2,800 AI"). gpt-5.2 read
  the KB. Real defect = POLICY CONFLICT: Closer.md:102 "No pricing — defer to ##sales-language-kb##"
  vs a Dialux price range living in industry-kb. All 3 models quoted it BECAUSE it's in the corpus.
  Fix = decide: sanction the KB range (update Closer.md:102/Offer.md:42) or scrub it from the KB.
  Drop the "grep for echo chunk" action.
- **Wendy persona is DESIGNED vague** (personas.py:429: "enthusiastically AGREE with everything but
  COMMIT to nothing… no real company name"). "Marsh" is her real name — data wasn't garbage. Defect
  is narrower: the agent answered its OWN questions (tz/time/date) instead of failing the booking.
  Over-book guard stays: agent proposes, caller decides; no commit → no booking.
- **D1 downgraded: NOT production-blocking.** Milder on 5.2 (26 silent turns, farewell 24/24); fix
  (speak-or-terminate: after a spoken farewell, a second empty end_call attempt terminates the graph
  deterministically) is small and mechanical. Fold into the same pass as the pricing decision.
- **Latency framing:** p50 1.25s is FULL-LLM-call time; with streaming TTS the caller's
  time-to-first-audio lands well under it — 5.2 latency is a non-issue for voice. Max calls (4.4s)
  are tool-heavy turns (booking), not speech turns.

**NEXT SESSION QUEUE (on gpt-5.2, in order):** (1) pricing policy decision: sanction industry-kb
range or scrub it + align Closer.md:102/Offer.md:42; (2) D1 speak-or-terminate gate; (3) over-book
guard (never decide FOR the caller — Wendy); (4) callback honesty on human-demanding personas (Ray:
`record_reach_details` never fired, promise unbacked — route to walkthrough or close honestly);
(5) Sofia soft early-slot promise; (6) Sam security-infra claims boundary. Then re-battery +
end_call nudge re-validation ×3.
