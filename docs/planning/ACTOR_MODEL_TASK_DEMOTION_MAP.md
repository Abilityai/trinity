# Actor Model — Task Demotion Map

**Status:** Draft — design instrument for #945 (actor-model postcard)
**Created:** 2026-06-01
**Owner:** orchestration track

> **Purpose.** Before #945 (the actor-model postcard) can be written honestly,
> we need to know whether the four message `kind`s (`chat`, `task`, `event`,
> `reply`) can be reduced to envelope-shaped payloads. The blocker is
> `ParallelTaskRequest` — 14 fields, all but `message` optional, several
> mutually conditional. This document walks each field and decides where it
> should live in an actor-model world.

---

## Context

`docs/planning/TARGET_ARCHITECTURE.md` §"Coordination Model (Actor Model)"
defines the message envelope as:

```json
{
  "id": "<uuid>",
  "kind": "chat | task | event | reply",
  "from": "...",
  "to": "...",
  "correlation_id": "<uuid>",
  "causation_id": "<parent_message_id>",
  "idempotency_key": "<opaque>",
  "deadline": "<iso8601>",
  "payload": {}
}
```

The envelope only works as a real abstraction if the **platform** (dispatch,
projector, audit, observability) can route a message without reading inside
the payload. Today's `ParallelTaskRequest` fails that test: 13 of its 14
fields are optional, and several drive platform-layer branching (session
binding, persistence, self-execute behavior).

`#945` cannot honestly say "the postcard fits" until those fields are either
**moved out of the envelope** (to session/agent state, envelope headers, or
out-of-band storage) or **explicitly quarantined** (one declared overrides
sub-object, not 13 silent siblings).

This document is the pre-#945 work product. Its output is the demotion
sequence; once executed, #945 becomes a 30-minute writeup.

---

## Current shape (`src/backend/models.py:97-112`)

```python
class ParallelTaskRequest(BaseModel):
    message: str                                       # the one required field
    model: Optional[str] = None                        # per-call model override
    allowed_tools: Optional[List[str]] = None          # --allowedTools
    system_prompt: Optional[str] = None                # --append-system-prompt
    timeout_seconds: Optional[int] = None              # execution timeout
    max_turns: Optional[int] = None                    # --max-turns
    async_mode: Optional[bool] = False                 # fire-and-forget vs blocking
    save_to_session: Optional[bool] = False            # persist to chat_sessions
    user_message: Optional[str] = None                 # pre-context message text
    create_new_session: Optional[bool] = False         # close prior session
    chat_session_id: Optional[str] = None              # explicit session id
    resume_session_id: Optional[str] = None            # Claude --resume uuid
    inject_result: Optional[bool] = False              # self-task → chat session
    files: Optional[List[WebFileUpload]] = None        # inline file attachments
```

Three router consumers (`routers/chat.py:631, 701, 894`) plus serialization
in `services/backlog_service.py:98` and persistence to
`schedule_executions.backlog_metadata`.

---

## Demotion map

| # | Field | What it does today | Demote to | Current feature/owner | PR sequence |
|---|-------|--------------------|-----------|------------------------|-------------|
| 1 | `timeout_seconds` | Per-call execution timeout override | **Envelope `deadline`** computed from `agent.execution_timeout_seconds` (#665) or `schedule.timeout_seconds` (#913); per-task override deleted | Schedules (#913), MCP `chat_with_agent`, fan-out branches | PR 1 (cheap) |
| 2 | `model` | Per-call model override | **Session attribute** (one model per session) or per-schedule (MODEL-001, already exists); per-task override deleted | Fan-out, scheduled tasks | PR 2 (cheap) |
| 3 | `files` | Inline file attachments | **Out-of-band reference**: payload carries `file_ids: [...]` referencing the existing FILES-001 shared-files volume; agent reads from storage | Web file upload (#364) | PR 3 (cheap) |
| 4 | `async_mode` | Caller picks sync vs fire-and-forget | **Disappears.** Actor model is async by definition. Sync is an edge-adapter concern: routers expose `?wait=true` query param that internally polls or subscribes to completion event | Every `/task` dispatch site | PR 4 (medium) |
| 5 | `allowed_tools` | --allowedTools restriction | **Agent template + explicit `task_overrides` quarantine sub-object** for fan-out branches that genuinely need per-call variance | Fan-out (`services/fan_out_service.py:139`) | PR 5 (medium) |
| 6 | `system_prompt` | --append-system-prompt | Same as `allowed_tools` — `task_overrides` sub-object only | Fan-out, occasional one-shot tasks | PR 5 (medium) |
| 7 | `max_turns` | --max-turns guardrail | **Per-agent guardrails** table already exists (`db/migrations.py:889`). Per-task override appears dead — only `backlog_service.py:98` serializes it; no consumer reads it. **Delete with a `git grep` audit.** | Guardrails config (GUARD-001) | PR 5 (medium) — bundled |
| 8 | `save_to_session` | Persist task messages to `chat_sessions` | **Envelope `session_id`**. Presence of `session_id` ⇒ persist. Absence ⇒ don't. Removes the boolean. | Chat tab persistence (`routers/chat.py:767, 1377`) | PR 6 (hard) |
| 9 | `chat_session_id` | Explicit `chat_sessions.id` to persist to | **Folded into envelope `session_id`** — one id, one lookup, one source of truth | Chat tab, self-execute | PR 6 (hard) |
| 10 | `resume_session_id` | Claude Code session UUID to `--resume` | **Resolved server-side** from `agent_sessions.cached_claude_session_id` via envelope `session_id` (the SESSION_TAB_2026-04 pattern already works this way for the Session tab) | EXEC-023 mid-execution resume | PR 6 (hard) |
| 11 | `create_new_session` | Close prior active session, start fresh | **Separate envelope `kind`** (`session.reset`) or a `?new=true` router param. Removes the flag from the task payload. | Chat tab "New chat" button | PR 6 (hard) |
| 12 | `user_message` | Original message text before context-prompt prepending | **Session journal entry**: agent writes the original user text to the session journal as part of the turn record. Context-prompt construction stays server-side; the envelope carries the constructed message in `message` and the original in journal metadata, not as a peer envelope field. | Chat tab message log (`routers/chat.py:671, 678`) | PR 6 (hard) |
| 13 | `inject_result` | Self-task injects its result back into originating chat session | **Causation_id semantics**: if a task's `causation_id` points to a chat session message, the completion event projector writes the result into that session. No flag needed on the task payload — it's a property of the causal graph. | SELF-EXEC-001 (`routers/chat.py:826, 834, 875, 1030, 1047`) | PR 6 (hard) |

After all six PRs, `ParallelTaskRequest` collapses to roughly:

```python
class TaskEnvelopePayload(BaseModel):
    message: str
    session_id: Optional[str] = None            # ← unifies #8, #9, #10, #11, #12, #13
    file_ids: Optional[List[str]] = None        # ← replaces #3
    task_overrides: Optional[TaskOverrides] = None  # ← explicit quarantine for #5, #6
```

Three fields, one quarantine sub-object. The envelope owns `deadline` (#1),
`idempotency_key`, `correlation_id`, `causation_id` (which carries #13's
semantics for free), `from`, `to`, `kind`. Model selection (#2) moves to
session/agent. Async (#4) disappears.

The four payloads then draw themselves:

```
chat   → { message, session_id, file_ids? }
task   → { message, session_id?, file_ids?, task_overrides? }
event  → { event_type, data }
reply  → { in_reply_to, content }
```

That fits on a postcard. #945 becomes the writeup of what we already shipped.

---

## Sequencing (6 PRs)

Each PR is independently shippable, leaves the system in a working state, and
follows the additive-first rule from `docs/archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md`
§"Additive-first migration" — new behavior lands alongside the old field, old
field deleted only after one release of soak.

### Cheap (new home already exists)

**PR 1 — Drop per-task `timeout_seconds` override.**
Replace with envelope-level `deadline` computed from `agent.execution_timeout_seconds`
(#665) or `schedule.timeout_seconds` (#913). Callers that pass it today: audit
and confirm none rely on per-call variance beyond what the agent/schedule cap
already provides. Single migration: deprecate field with a header warning for
one release, then delete.

**PR 2 — Drop per-task `model` override.**
Per-schedule `model` (MODEL-001) stays. Per-task removed from
`ParallelTaskRequest`. Fan-out branches that need model variance use a
session-scoped model attribute instead — the session, not the message, picks
the model.

**PR 3 — Move `files` to out-of-band reference.**
Caller uploads to FILES-001 shared-files volume (already exists), passes
`file_ids: [...]` in the payload, agent reads from storage by id. Removes the
inline-bytes anti-pattern. Existing `WebFileUpload` becomes the upload-API
shape; the task envelope only carries ids.

### Medium (need a feature decision)

**PR 4 — Collapse `async_mode`.**
Make async the only path. Add `?wait=true` query parameter at the router
layer for synchronous semantics — internally subscribes to the completion
event with a timeout. The dispatch path becomes uniform; sync semantics are
an edge adapter, not a core branch.

**PR 5 — Quarantine `allowed_tools` / `system_prompt` / `max_turns` into `task_overrides`.**
Same fields, but grouped under one explicit `task_overrides: Optional[TaskOverrides]`
sub-object. Doesn't remove the override capability (fan-out branches need it
genuinely), but makes the conditional surface explicit. **Audit `max_turns`
during this PR** — if no consumer reads it (current grep shows only
serialization, no use), delete it outright.

### Hard (touches every chat call site)

**PR 6 — Unify the six session fields under `session_id`.**
This is the real work. Introduce a single envelope `session_id` field;
deprecate `save_to_session`, `chat_session_id`, `resume_session_id`,
`create_new_session`, `user_message`, `inject_result`. The agent resolves
resume/persist/inject behavior from session state lookup plus causal graph
(causation_id pointing to a chat message ⇒ inject). One release behind a
deprecation header. **Soak in dev for at least a week** before main — this is
the PR most likely to break things.

Touchpoints to verify in PR 6:
- `routers/chat.py:631, 701, 894` — all three task entry points
- `routers/chat.py:826-1107` — self-execute path (SELF-EXEC-001)
- `routers/sessions.py` — Session tab equivalents
- `services/task_execution_service.py:559` — payload-to-agent serialization
- `services/backlog_service.py:98` — persisted backlog metadata schema (add migration)
- MCP server `src/mcp-server/src/tools/chat.ts` — `chat_with_agent` tool shape
- `schedule_executions.backlog_metadata` JSON migration for in-flight queued rows

---

## What this is not

- **Not the postcard.** This is the pre-postcard. #945 still ships as a
  separate doc once the demotions land or are confidently scoped.
- **Not a feature reduction.** Every feature these fields support today
  (Session tab persistence, self-execute, EXEC-023 resume, file uploads,
  fan-out tool restrictions) keeps working — the fields just move to where
  they belong.
- **Not a flag-day rewrite.** Each PR is reversible and shippable on its own.
  The platform stays load-bearing throughout.

---

## Decision gate

If PRs 1–5 land cleanly and PR 6's design review surfaces no blocking
complexity, **the actor model is real for this codebase** — #945 ships and
#946 (Phase 2 MCP boundary experiment) can be scheduled.

If PR 6 turns up irreducible session/causation complexity that the envelope
can't absorb without sprouting its own conditionals, **the postcard fit-test
has failed honestly** and Sprint D′ + the planned incremental path remains
the best direction. That's still a successful outcome — we learned
empirically rather than by hand-waving.

---

## Cross-references

- `docs/planning/TARGET_ARCHITECTURE.md` §"Coordination Model (Actor Model)",
  §"Key Open Questions" #1
- `docs/archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md` §"Target architecture:
  actor model", §"Pre-experiment artifact", §"Additive-first migration"
- Tracking issues: #945 (postcard), #946 (Phase 2 experiment), #927
  (replica groups — needs journal projection contract)
