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
   the same call the summarizer makes) → ≤ 10 one-line facts, each `sanitize_text`'d,
   clamped to 500 chars and screened by `user_memory_guard.prepare_summary`
   (a fact that trips a rule is dropped and the rule logged).
5. Embeds the candidates; finds each one's neighbours (cosine ≥ 0.60) in the scope.
6. **Reconcile** — one Haiku call when any candidate has neighbours: ADD /
   UPDATE id / DELETE id / NONE. An id not in that candidate's neighbour set is
   refused → NONE. A missing or unreadable answer means "nothing is a duplicate".
7. Applies the ops; the partial unique index on `(agent_name, user_email, text_hash)
   WHERE deleted_at IS NULL` makes a two-worker duplicate an IntegrityError the
   writer catches. Prunes the scope to 2,000 live facts (oldest never-recalled first).

## Recall
`memory_service.recall(agent_name, user_email, query)` is **async** and runs the
query embedding + the cosine ranking under `asyncio.to_thread` — never on the
event loop. Top 5 above 0.30 cosine, within 1,500 chars; the scope's matrix is
cached per process keyed on `(count, max(updated_at))`; hits bump
`recall_count` / `last_recalled_at` (production is ent#634's readout corpus).

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
`agent_memory_facts`, Alembic `0107_agent_memory_facts`; `agent_ownership.memory_opt_out`
— SQLite `agent_ownership_memory_opt_out`, Alembic `0108_agent_ownership_memory_opt_out`.
`db/memory_facts.py::MemoryFactOperations`, `db/agent_settings/memory.py::MemoryOptOutMixin`.
Forget is a soft delete with `deleted_by`.

## API — `routers/agent_memory.py` (`# mcp: none`)
| Method | Path | Gate | What |
|---|---|---|---|
| GET | `/api/agents/{name}/retrieval-memory` | AuthorizedAgentByName + `assert_own_agent` | status (a use) |
| PUT | `/api/agents/{name}/retrieval-memory` | OwnedAgentByName + `require_person` | `{opt_out}` — audit-logged |
| GET | `/api/agents/{name}/retrieval-memory/facts?user_email=&limit=&offset=` | OwnedAgentByName + `require_person` | the owner view (ent#962 readers rule) |
| DELETE | `/api/agents/{name}/retrieval-memory/facts/{id}` | OwnedAgentByName + `require_person` | forget — audit-logged |
| GET | `/api/settings/memory` | admin | platform status (`routers/settings/flags.py`) |

The switch is written through the generic `PUT /api/settings/trinity_memory_enabled`.

## Frontend
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

## Deferred (recorded)
BM25/FTS adapter behind the same port pending ent#634; a retention floor for
facts; cross-scope beyond the owner; the enterprise Workspace surface for the
owner view (ent#962); backfill of historical chat.
