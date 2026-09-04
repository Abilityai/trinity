# Agent Canvas — a durable surface an agent renders onto (trinity-enterprise#438)

> **One idea**: a **report** is a thing published once and accumulated; a
> **canvas** is one surface the agent keeps *current*. Same output, opposite
> lifetime. The composite primary key `(agent_name, canvas_id)` is what makes
> that difference structural rather than a convention someone has to remember.

## Why

"Workspace" meant two things. There was **the** Workspace (`/workspace`) where
people work with their agents, and a per-agent workspace
(`/agents/:name/workspace`) — a Gemini voice orb beside a canvas panel, marked
BETA, gated behind `VOICE_ENABLED && GEMINI_API_KEY && WORKSPACE_ENABLED`.
Users had to know which one they were in, and capability differed between them
for no reason a user could state.

The more valuable half was the canvas. Agents produce results that are not chat
messages — tables, charts, rendered reports, dashboards — and had nowhere to
put them: they were flattened into chat text or became files someone had to go
find. The only canvas that existed was `VoiceSession.panel_state`: in memory,
writable only by the Gemini Live voice tools, on one page, and gone when the
session ended.

**OSS-core by decision.** Deliberately ungated — no `requires_entitlement`,
logic in the OSS tree. Recorded because the default for an enterprise-tracker
feature is *gated unless ruled otherwise*, so the ruling must never be inferred
later from the mere fact that it merged (the ent#326 / ent#384 / ent#392
discipline). Rationale, on operator instruction: the Workspace and everything
around it is OSS.

## The merge (AC 1)

`/agents/:name/workspace` is **deleted** and its route is a query-preserving
redirect to `/workspace?agent=<name>`.

It is only safe to delete because **ent#440 already put voice conversation
inside the Workspace**, so once the canvas moved the page had no capability of
its own left. Same shape as the ent#358 Session-surface retirement and the
ent#381 Sessions-page retirement: the surface goes, every old link still lands
somewhere true.

Two knock-on edits, both of which would otherwise leave dead behaviour:

- `AgentHeader`'s Workspace button now opens `/workspace?agent=`, is no longer
  gated on `workspaceAvailable`, and is no longer disabled while the agent is
  stopped. The Workspace reports availability itself (#2196), and a dead button
  is a worse answer than a page that says why. The now-unused
  `workspaceAvailable` prop is removed rather than left as a lie.
- `ChatPanel`'s voice overlay starts its session with `workspaceMode: true`.
  **This is load-bearing, not a bonus**: the retired page was the only caller
  that passed it, so bridging the voice panel to the canvas while leaving it
  unreachable would have been dead code wearing a fix's name.

## Storage

```sql
CREATE TABLE agent_canvases (
    agent_name TEXT NOT NULL,
    canvas_id  TEXT NOT NULL,
    title      TEXT,
    blocks     TEXT NOT NULL,     -- JSON [{kind, title?, payload}]
    audience   TEXT NOT NULL DEFAULT 'operator',   -- 'operator' | 'roster'
    schema_version INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by_execution_id TEXT,
    PRIMARY KEY (agent_name, canvas_id)
);
CREATE INDEX idx_agent_canvases_agent ON agent_canvases(agent_name, updated_at DESC);
```

- `created_at` is preserved across updates — it is the age of the **surface**;
  `updated_at` is what moves.
- Registered in `AGENT_REFS` (CASCADE). The rename half is load-bearing rather
  than tidy: `agent_name` is *half the primary key*, so an unregistered table
  would leave a renamed agent's canvas addressed to a name nothing resolves,
  and the agent's next write would silently mint a **second** canvas under the
  new name while the old one stayed visible.
- **No retention window.** The table is bounded by the composite key — one row
  per surface, replaced on write — unlike the append-only tables
  `RETENTION_OPS_KEYS` governs. It is deliberately absent from that set.
- Dual-track migration: `db/migrations.py::agent_canvases_table` +
  Alembic `0050_agent_canvases`.

## Audience — how "never widens" is made structural (AC 8)

`audience` defaults to `operator` and is a **validated column, never a key
inside `blocks`** (the ent#364 rule): `blocks` is agent-authored, so an audience
buried there would let a prompt-injected agent choose its own readers.

- `operator` → Agent Detail only.
- `roster` → additionally the agent's Workspace page, for anyone already
  rostered on that agent.

`normalize_audience` is an **allowlist**: an unrecognised stored value reads as
`operator`. The column is plain TEXT with no CHECK constraint, so the next value
someone writes by hand — or a future value this build has not heard of — must
fail closed (#2396's rule).

The Workspace read narrows **in the query** (`db.list_agent_canvases(agent,
audience='roster')`), not afterwards. A read that loads everything and filters
in Python has already put an operator-only surface in the process one edit away
from the response — the ent#365 FR-2 lesson restated. Both gates are needed and
neither is redundant: the roster gate answers *may this person reach this
agent*, the audience narrowing answers *did the agent mean this for them*.

## Staleness — derived, not a clock (AC 7)

`stale` is `last_completed_execution_at(agent) > canvas.updated_at` — the agent
finished a run and did not refresh this surface.

An age threshold was rejected: a canvas has no inherent freshness expectation,
so a clock either cries wolf on a monthly summary or stays silent on a
minute-by-minute one. "The agent has run since" is a fact about *this* canvas,
needs no configuration, and is checkable against `updated_by_execution_id`.

`db.last_completed_execution_at` is a `MAX` over the whole column rather than a
bounded scan of recent rows, deliberately: a head full of `queued`/`running`
rows would push the newest COMPLETED row out of a window and report a stale
canvas as current — the failure this AC exists to prevent.

**Fail-quiet is available here and only here.** Missing evidence reads as "not
stale", because the mark is an *addition* to an always-rendered `updated_at`,
never a replacement for it. Marking on no evidence would train the reader to
ignore the mark. It is derived once per agent, not once per canvas.

## Rendering — one layer (AC 4)

A canvas is an ordered list of `{kind, title?, payload}`:

| kind | renderer |
|---|---|
| `table` `kpi` `markdown` `timeline` `json` | delegated to the shared `components/reports/` dispatch |
| `chart` | `TrendLineChart`, reused as-is |
| `html` | DOMPurify via `utils/markdown.js::sanitizeHtml` (H-005) |

The five shared kinds **delegate, never fork** — those renderer keys are
CI-pinned as the canonical contract (`test_1535_report_prompt_guidance.py`), and
forking them is what §5.11 and §5.14 both refused. The report `display_hint`
enum is deliberately **not** widened to add `chart`/`html`: a canvas is a
superset of a report's rendering, not a change to what a report is.

`sanitizeHtml` is added to the *existing* markdown util rather than a new
module, so it shares the DOMPurify instance and its configured link hardening —
a second sanitizer is a second policy to keep in step, which is H-005 one level
up.

An unknown kind resolves to `json`, never to nothing: a silently dropped block
is the one failure a canvas must not have, because the surface would look
complete while missing content.

## Write path

`PUT /api/agents/{name}/canvas/{canvas_id}` — PUT because the operation is
idempotent on the key, which is the surface's whole contract.

- **Self-gated**: `AuthorizedAgent` proves the key's *owner* can reach the path
  agent; it does not stop an agent-scoped key writing as a *sibling* the same
  owner shares. That is a disclosure surface as well as a correctness one,
  because a `roster` canvas is client-visible (the #918 rule).
- Per-agent rate limit; `canvas_id` charset-validated with a **named** 400;
  blocks capped at 50 and 512 KiB with a 413. The byte cap is what the count
  cap cannot express.
- `execution_id` is resolved through `resolve_and_validate_execution` (MEM-001).
  A foreign id **degrades to None rather than refusing the write** — it is
  provenance, not authorization, and losing a stamp is a smaller harm than
  losing the canvas the agent just rendered.
- Reads are **not** self-gated: an operator reads as a user-scoped principal
  with no `agent_name`, and the `{self} ∪ permitted` narrowing for agent keys
  lives at the MCP layer.

## The voice panel moved (AC 2 / FR-7)

`gemini_voice._execute_panel_tool` still updates the in-session `panel_state`
the live overlay reads, and now additionally persists it to canvas
`voice` via `_persist_panel_to_canvas`. Mermaid and image panels map onto
markdown blocks — a fenced ```mermaid block and an image link — so no new block
kind was needed.

Fixed `audience="operator"`: a voice session always ran on an
operator-authenticated surface, and a voice panel that silently became
client-visible is exactly the widening the audience default exists to prevent.
Fail-soft: a canvas write that fails must not break the panel in front of the
operator or the tool result the model is waiting on.

## Surfaces

| Surface | Route | Sees |
|---|---|---|
| Agent Detail → **Canvas** tab | `GET /api/agents/{name}/canvas[/{id}]` | every canvas |
| Workspace agent page → **Canvas** tab | `GET /api/enterprise/client-portal/agents/{name}/canvas[/{id}]` | `audience='roster'` only |
| MCP | `set_canvas` · `get_canvas` · `list_canvases` · `clear_canvas` | its own |

There is **no `append_to_canvas` tool, by design**: `set_canvas` replaces, so
read-change-write is the only sequence that leaves the surface in a state the
agent chose. `get_canvas`'s description says so.

Empty states differ by viewer (AC 6) because the next action does: an operator
can make an agent write a canvas, a client cannot — offering them a tool call
would be an instruction they cannot follow, so they get the chat instead.

## Key files

| Layer | File |
|---|---|
| DDL | `db/schema.py`, `db/tables.py`, `db/migrations.py`, `migrations/versions/0050_agent_canvases.py` |
| DB | `db/canvas.py` (`CanvasOperations`, `normalize_audience`) |
| Service | `services/canvas_service.py` (validation, bounds, derived staleness) |
| Router | `routers/canvas.py` |
| Workspace | `client_portal/agent_page.py::canvases`/`canvas_detail`, `client_portal/router.py` |
| Voice bridge | `services/gemini_voice.py::_persist_panel_to_canvas` |
| Frontend | `components/canvas/{canvasUtils.js,CanvasBlock.vue,CanvasPanel.vue,AgentCanvasTab.vue}` |
| MCP | `src/mcp-server/src/tools/canvas.ts`, `client.ts` |
| Tests | `tests/unit/test_ent438_agent_canvas.py` |

## Change Log

| Date | Author | Change |
|------|--------|--------|
| 2026-09-02 | claude | Initial — canvas surface, workspace merge, voice-panel bridge (ent#438) |
