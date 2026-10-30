# Feature: Retrieval Memory (MEM-002, trinity-enterprise#1022)

## Overview
Everything an agent does leaves extracted, deduplicated facts in the platform
database, and the facts most relevant to the incoming request are put back in
front of it at compose time. It sits beside MEM-001 ([write-user-memory.md](write-user-memory.md)):
MEM-001 is one blob per (agent, person) — notes the agent writes and a rolling
summary — injected whole; MEM-002 is one row per fact, captured from every
surface and recalled by relevance. Both render inside the same ent#419 envelope.

**Default off.** The platform switch is the system setting `trinity_memory_enabled`
(Settings → General → Retrieval memory); each agent can be kept out
(`agent_ownership.memory_opt_out`). Everything is fail-open: when off,
unavailable or erroring, the prompt is byte-identical to today's and no turn is
ever affected.

**Runtime-agnostic by construction.** Nothing runs inside the agent container
and nothing depends on a Claude Code hook: capture and recall are backend seams
that every runtime (Claude Code, Gemini CLI, Codex) passes through.

## User Story
As an operator, I want my agents to carry forward what people told them, what
was decided and what they attempted — across runs and surfaces — without any
one person's facts reaching anyone else, and without my data leaving the
instance.

## Scopes
A scope is `(agent_name, user_email)`. `user_email = ''` is the **agent scope**,
which the agent's **owner shares**: an owner's own turns resolve to it
(`memory_service.scope_for`), so a scheduled run can recall what the owner said
in the Workspace and vice versa. Every other person has their own scope and
never sees the agent's; recall never crosses scopes.

## Whose memory — `services/memory_subject.py`
The ONE rule, read off the execution row (the agent never names the person),
shared with the MEM-001 write endpoint (`routers/public_memory.py`):

| Row | Subject |
|---|---|
| seat-addressed schedule run (`schedule_seat_memory.seat_for_execution`) | that seat |
| channel turn with `source_channel_client` (one-to-one, verified) | that person |
| channel group / unverified turn | **skip** |
| `public` / `user` / `chat` turn with a verified `source_user_email` | that person (the owner → agent scope) |
| anonymous `public` turn | **skip** |
| `schedule` (no seat), `manual`, `webhook`, `agent`, `mcp`, `self_task`, `loop`, `fan_out`, `reminder`, `event`, `system` | agent scope |
| `voice`, `voip`, `paid`, `room`, `session`, `skill_gate`, `validation`, anything unknown | **skip** |

The agent scope is an explicit **allowlist**: "no person → agent scope" would
sweep anonymous and group turns into the shared scope.

## Capture — ONE seam
`event_dispatch_service.spawn_task_terminal_event` is called by every CAS-won
terminal writer (task, chat, pull, fan-out, cleanup). It already hangs
`_terminal_side_effects` beside the pub/sub emit because the emit returns early
without a subscriber; `memory_service.spawn_retain_for_execution(execution_id)`
hangs there too (a cheap no-op while the switch is off, strong-referenced,
never raising).

`memory_service.retain_execution`:
1. Reads the row; resolves the subject; skips when off, opted out, skipped, or empty.
2. Builds the transcript: the request's tail (last 4,000 chars) and the outcome,
   **credential-redacted** (`utils/credential_sanitizer.sanitize_text`) and
   margined (`│ `) like the summarizer's input.
3. Brakes: a platform Anthropic key must exist; per-agent daily budget
   (`trinity_memory_daily_retain_limit`, default 200 executions/24 h); per-scope
   Redis lease (`SET NX EX 60`, 3 × 2 s retries, then skip this turn);
   `asyncio.Semaphore(4)` per process; 20 s call timeout.
4. **Extract** — one Haiku call on the platform key (`services/anthropic_messages.complete`,
   the same call the summarizer makes; `claude-haiku-5-5`, thinking off, low
   effort, the first TEXT block read) → ≤ 10 one-line facts, each `sanitize_text`'d,
   clamped to 500 chars and screened by `user_memory_guard.prepare_summary`
   (a fact that trips a rule is dropped, the rule logged, and the candidate
   journaled as `screened`).
5. Embeds the candidates; finds each one's neighbours (plain cosine ≥ 0.60 —
   no recency here, whether two facts say the same thing has nothing to do
   with when) in the scope.
6. **Reconcile** — one Haiku call when any candidate has neighbours: ADD /
   UPDATE id / DELETE id / NONE. An id not in that candidate's neighbour set is
   refused → NONE. A missing or unreadable answer means "nothing is a duplicate".
7. Applies the ops; the partial unique index on `(agent_name, user_email, text_hash)
   WHERE deleted_at IS NULL` makes a two-worker duplicate an IntegrityError the
   writer catches. UPDATE and DELETE stamp `updated_by_execution_id` (the fact's
   `execution_id` stays where it was born). Prunes the scope to 2,000 live facts
   (oldest never-recalled first).
8. **Journals** every one of those — `add` / `update` / `delete` / `none` /
   `screened` / `prune` — as one `agent_memory_fact_events` row keyed to the
   execution, with the candidate the extractor proposed, the text it replaced
   and the neighbour ids the reconciler was shown; then drops this agent's
   journal rows older than 90 days. Recall adds a `recall` row per hit (with the
   cosine and the asking execution when known), forget a `forget` row with who.
   The journal is append-only and is what the extraction prompt is tuned
   against: `op = screened` is "what the extractor proposed that never became
   a fact", `op = none` beside its `neighbour_ids` is "what reconcile judged a
   duplicate".

## Recall
`memory_service.recall(agent_name, user_email, query, execution_id=…)` is
**async** and runs the query embedding + the ranking under `asyncio.to_thread`
— never on the event loop. Ranking is `services/memory_ranking.py`, a leaf
(numpy only): **relevance gates, recency re-orders.** A fact needs cosine ≥ 0.30
to be recalled at all; the survivors are ordered by
`cosine × (0.6 + 0.4 × 0.5^(age_days / half_life))` over `updated_at`, so the
newer wording of a superseded fact ranks first even when the old wording
matches the query somewhat better, while an old fact never loses more than
40 % — a fact at cosine ≥ 0.5 never falls below the 0.30 gate line, so a fresh
weak match never beats a stale strong one. Half-life default 30 days, operator knob
`trinity_memory_recency_half_life_days` (`0` = plain cosine). Top 5 within
1,500 chars; the scope's matrix is cached per process keyed on
`(count, max(updated_at))`; hits bump `recall_count` / `last_recalled_at`
(production is ent#634's readout corpus) and journal a `recall` row.

`recall` returns the lines the prompt sees — `(2026-10-21, scheduled run) …`,
the fact's last revision date and the kind of run it came from, so the model
can weigh staleness itself; execution ids stay out of the prompt (noise to the
model, a copy-back hazard). `recall_records` returns the records (`id`, `text`,
`source`, `created_at`, `updated_at`, `relevance`, `score`).

The dated dataset `tests/unit/fixtures/ent1022_recency_dataset.json` pins the
behaviour against the real model: `tests/unit/ent1022_recency_eval.py` runs it
offline inside the backend image (`PYTHONPATH=/app python …`) and as the
`test_real_model_recency_dataset` case wherever the model is cached, scoring
every query with recency on and off so the report shows what the weight changed.

Rendering: `user_memory_guard.envelope(notes, summary, retrieved=…)` adds a
third section `### Retrieved for this request` inside the same nonce frame.
`platform_prompt_service.format_user_memory_block(record, retrieved=…)` is the
single injection point for person-addressed turns; `format_agent_memory_block`
renders the agent scope under `## What you remember from earlier work`. The
copy-back normaliser drops the retrieved section (and `STRUCTURAL_HEADINGS`
lists both headings), so facts cannot loop notes → summary → facts.

Call sites (each awaits `recall` first, then passes `retrieved=`):
- `services/public_chat_service.py` — public link web turns
- `adapters/message_router.py` — channel one-to-one turns
- `client_portal/service.py::portal_chat` → `_build_portal_system_prompt(…, retrieved=)`
- `routers/internal.py` → `schedule_seat_memory.build_seat_caller_prompt(…, retrieved=)` (seat runs)
- `services/task_execution_service.py` — headless runs: `memory_service.task_memory_block`
  prepends the agent block to the caller prompt when the row's subject is the agent scope.

### Search on demand (trinity-enterprise#1028)
The injected block is the top of the store for the incoming message; the same
ranking is also a door the agent and the owner can open by hand.
`memory_service.search_records(agent_name, user_email, query, k, execution_id)`
is `recall_records` without the prompt-block budget and past the recall tail
(`k ≤ SEARCH_K_MAX = 50`), each record also carrying `line` — the rendered
`(date, kind of run) text` — and each hit journaled as a `search` row whose
`candidate_text` is the query, so the owner can see what an agent looked up.
The agent door (`POST …/retrieval-memory/search`, MCP `search_memory`) never
takes a person from the caller: the scope is `memory_subject.resolve` over the
execution row the agent names — the served person's scope on a person turn, the
agent scope on a headless run, a 422 `memory_no_subject` with the reason for an
anonymous or group turn — and an agent-scoped key may only search its own agent,
checked first (ent#840). The owner door (`GET …/retrieval-memory/search`) is
person-only and takes the scope by email like the facts list. Both refuse
honestly when memory is off (422 `memory_off`) or the embedder is missing
(503 `memory_unavailable`) rather than answering `[]`, which would read as
"nothing matched".

## Embeddings — `services/memory_embeddings.py`
`fastembed` (ONNX Runtime, no torch) with
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384-d, Apache-2.0,
quantised, ~235 MB). Baked into the backend image under `/opt/trinity/models`
(outside `/app`, which the dev compose bind-mounts over), sha256-verified at
build, read with `local_files_only` under `HF_HUB_OFFLINE=1`. Built once per
process under a lock — warmed on a daemon thread at boot when the switch is on
(`main._start_maintenance_services` → `memory_service.on_startup`), otherwise
lazily — with ONNX intra-op threads = 1. Unavailable ⇒ `embed` returns None,
`availability()` says why, recall returns `[]`, capture skips.

## Data
`agent_memory_facts` — `db/schema.py`, `db/tables.py`, SQLite migration
`agent_memory_facts`, Alembic `0108_agent_memory_facts`; `agent_ownership.memory_opt_out`
— SQLite `agent_ownership_memory_opt_out`, Alembic `0109_agent_ownership_memory_opt_out`.
`agent_memory_fact_events` (the journal) and `agent_memory_facts.updated_by_execution_id`
— SQLite `agent_memory_provenance`, Alembic `0110_agent_memory_provenance`.
`db/memory_facts.py::MemoryFactOperations`, `db/agent_settings/memory.py::MemoryOptOutMixin`.
Forget is a soft delete with `deleted_by`. Both tables cascade with the agent
(`db/agent_cleanup.py` `AGENT_REFS`).

## API — `routers/agent_memory.py` (`# mcp: memory.ts (search_memory)`)
| Method | Path | Gate | What |
|---|---|---|---|
| GET | `/api/agents/{name}/retrieval-memory` | AuthorizedAgentByName + `assert_own_agent` | status (a use) |
| PUT | `/api/agents/{name}/retrieval-memory` | OwnedAgentByName + `require_person` | `{opt_out}` — audit-logged |
| GET | `/api/agents/{name}/retrieval-memory/facts?user_email=&limit=&offset=` | OwnedAgentByName + `require_person` | the owner view (ent#962 readers rule) |
| DELETE | `/api/agents/{name}/retrieval-memory/facts/{id}` | OwnedAgentByName + `require_person` | forget — audit-logged, journaled with who |
| GET | `/api/agents/{name}/retrieval-memory/events?fact_id=&execution_id=&op=&limit=&offset=` | OwnedAgentByName + `require_person` | the journal: one fact's lineage, or everything one run did; `op` ∈ add/update/delete/none/screened/prune/recall/forget/search (422 otherwise) |
| GET | `/api/agents/{name}/retrieval-memory/search?q=&k=&user_email=` | OwnedAgentByName + `require_person` | the owner's search over one scope (ent#1028): hits with `line`, `relevance`, `score`, provenance; 422 `query_empty` / `invalid_email` / `memory_off`, 503 `memory_unavailable` |
| POST | `/api/agents/{name}/retrieval-memory/search` | `assert_own_agent` + `assert_agent_access` (the agent door, MCP `search_memory`) | `{execution_id, query, k≤50}` — the scope read off the execution by the whose-memory rule; 422 `memory_no_subject` with the reason when the run serves no one |
| GET | `/api/settings/memory` | admin | platform status (`routers/settings/flags.py`) |

The switch is written through the generic `PUT /api/settings/trinity_memory_enabled`.

## Frontend
`agent-settings/RetrievalMemorySearch.vue` (ent#1028) sits under the per-agent
toggle in Guardrails → Memory: a search box over the agent scope, hits with the
`(date, source)` line and relevance as a percent, one reserved footprint across
idle / skeleton / hits / empty / failed, a bounded list with its count, earlier
hits kept on a failed re-search.

- `components/settings/RetrievalMemoryPanel.vue` (Settings → General): the switch
  and an honest status line ("Currently off…", "On, but unavailable — <reason>",
  "On. N facts stored · last captured …").
- `components/agent-settings/RetrievalMemoryToggle.vue` (agent Guardrails pane →
  Memory): the per-agent opt-out; says "Platform switch is off" rather than
  pretending a saved choice is live.
- Spec: `tests/unit/retrievalMemoryPanel.spec.js` (mounted).

## Tests
`tests/unit/test_ent1022_retrieval_memory.py` — switches, the subject table,
the capture wiring, scope isolation both directions, owner-shared scope,
redaction before extraction and storage, the screen, reconcile ops and refused
ids, the duplicate index, the daily budget, the lease, ranking/threshold/budget/
counters, recall off the loop, fail-open, envelope and copy-back, route gates,
dependency-pin parity, sovereignty pins, both migration tracks; plus a
real-model check where the model is cached.
`tests/unit/test_ent1022_memory_provenance.py` — the ranking leaf (weight shape,
gate vs re-order, old-strong beats fresh-weak, recency off), the rendered line
and `recall_records`, the half-life knob, the journal for every op with the
execution and the previous text, the last-writer stamp, recall/prune/forget
rows, the journal's age pruning, the events route and its filters, the model
pin and request shape, both migration tracks and cleanup parity; plus the
dated dataset against the real model where it is cached.

**Extraction quality (paid, by hand, never in CI).** `tests/unit/eval_ent1022_extraction_quality.py`
runs the production path (`build_transcript` → `_extract` → the screen, and
`_reconcile`) over `fixtures/ent1022_extraction_gold.json` — 43 synthetic,
hand-labelled exchanges (preferences, decisions, deadlines, names, numbers,
headless outcomes, failures, expect-empty chatter, prompt injection, credentials,
clamp, multilingual) and 12 reconcile pairs — plus any file of real execution
rows (`MEMORY_EVAL_PROD_ROWS`). A different model judges recall against the
gold, grounding and worth of each extracted fact and what was missed; forbidden
substrings and the screen are checked deterministically. Metrics stay atomic
(recall / precision / specificity / leak / format / hallucination / missed),
spend and the extractor's returned model are recorded, and
`MEMORY_EVAL_DRY_RUN=1` proves the harness without a call. Run:
`cd tests && MEMORY_EVAL_API_KEY=… python -m pytest unit/eval_ent1022_extraction_quality.py -p no:randomly -s -q`.

Measured 2026-10-30 (Haiku 5.5 extractor, Opus 5.5 judge, 43 gold + 74 real rows +
12 pairs, one rep; noise floor ±9 points at n=117, judge variance flips a
borderline verdict between runs): the first prompt scored gold recall 100%,
precision 88%, specificity 83%, zero leaks; real rows precision 89%,
ungrounded 4%, and a quarter of the real rows hit the 10-fact cap. The two
systematic defects were the request restated as its own fact beside the outcome
(6 of 10 gold failures, 13 real-row facts) and routine no-op runs stored as
facts. The prompt now folds an attempt into one entry, drops no-op runs and asks
for careful attribution; retrained on gold only, it scores gold precision 98%
and specificity 100% with recall held at 100%, and on the held-out real rows
restated requests fall 13 → 4 and transient facts 7 → 2 with 12% fewer facts
stored; headline precision is unchanged (89%) because the judge's remaining
objections are inferred details on long legal / finance transcripts (dates read
off filenames, "sent" for "drafted", the wrong party's intent), which the gold
set does not exercise. Open: a real-row training split for that class, the
10-fact cap, and facts over the 200-character ask (stored intact, the clamp is
500; the judge counts them as format misses).

**Search (trinity-enterprise#1028).** `tests/unit/test_ent1028_memory_search.py`
drives `search_records` (past the recall tail, journaled with the query,
scope-bound, empty when off), the owner door (named 422s) and the agent door
(agent scope on a headless run, the person's scope on a person turn, the
refusals in order, `k` capped by the model) over the real SQLite with the
hash embedder; the route census carries the agent door in `AGENT_CALLABLE_SELF`.
`src/mcp-server/src/tools/memory.test.ts` proves the `search_memory` tool
passes `execution_id`/`query`/`k` through and hands the agent a refusal's code
and message; `src/frontend/tests/unit/retrievalMemorySearch.spec.js` mounts the
owner search box (idle / skeleton / hits / empty / failed, stale hits kept).

## Deferred (recorded)
BM25/FTS adapter behind the same port pending ent#634; a retention floor for
facts; cross-scope beyond the owner; the enterprise Workspace surface for the
owner view and the journal (ent#962); backfill of historical chat; an `as_of`
date the extractor reads off the exchange itself (relative dates in the fact
text still rot — the rendered stamp is the revision date, not the event's).
