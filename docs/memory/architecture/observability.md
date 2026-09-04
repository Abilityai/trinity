# Trinity Architecture — Reports, evaluations, activity stream

> Part of the Trinity architecture set. Core map, invariants and topology: [architecture.md](../architecture.md). This file is **not** auto-loaded.
>
> **Owns**: `src/backend/routers/reports.py`, `src/backend/routers/evaluations.py`, `src/backend/services/report_service.py`, `src/backend/services/report_export.py`, `src/backend/services/activity_service.py`, `src/frontend/src/components/reports/**`
>
> **Read this before changing the paths above**: Report renderer drift is silent: the write succeeds and the report degrades to a raw JSON dump. `test_1535_report_prompt_guidance.py` pins the prompt shapes against the `display_hint` enum and the renderer keys, and `shapeOk` must stay inside `ReportRenderer.vue` or that guard's matched set empties (#1535, #2162).
>
> **Write path**: changes to this area land here, not in the core (core editorial rule 4). Keep the core's map row in step if the owned paths change.

---

### Agent Reports (#918)

Agent-published structured reports (telemetry / domain results) surfaced on the dashboard
without reading chat. Three-surface clone of `agent_activities`: `routers/reports.py` →
`services/report_service.py` (create + broadcast only; reads go router→`db/reports.py`
directly). Agents call the MCP `report` tool, which POSTs to `POST /api/agents/{name}/reports`.

- **Prompt discoverability** (#1535): `PLATFORM_INSTRUCTIONS` carries a "Publishing Reports"
  block (`services/platform_prompt_service.py`) — the call, when to reach for it, and the
  payload shape per `display_hint` — so reporting is a fleet-wide default instead of a
  per-template opt-in. Runtime-aware for free via `_adapt_instructions_for_runtime` (#1187:
  Codex gets the bare `report` name). The documented shapes are CI-pinned to the MCP tool's
  `display_hint` enum and the `components/reports/` renderer keys
  (`tests/unit/test_1535_report_prompt_guidance.py`) because that drift is silent — the write
  succeeds and the report falls back to the raw JSON viewer.
- **Self-gated create**: `AuthorizedAgent` checks owner-access to the path agent, but an
  agent-scoped key could otherwise report as a *sibling* agent the owner shares; the endpoint
  additionally requires `current_user.agent_name == name` for agent-scoped callers (mirrors
  `authorize_heartbeat`). Payload capped at `REPORT_PAYLOAD_MAX_BYTES` (5 MiB, #1537) → 413;
  fields strictly validated in
  `ReportCreate`. Create is rate-limited per agent (`REPORT_RATE_LIMIT`/30 per 60s, shared
  `services/rate_limiter.py`, fail-open) so a runaway agent can't flood the table between
  retention sweeps → 429.
- **Thin WS trigger**: `/ws` is `SCOPE_ALL`, and until ent#467 it was unfiltered, so the `agent_report` broadcast
  carries only `{agent_name, report_id, report_type, created_at}` — never `title`/`payload`
  (which can be sensitive). The frontend store refetches via the access-controlled REST
  endpoints (the `notifications` pattern). Regression-guarded by
  `tests/unit/test_918_report_broadcast.py`.
- **Search & filter parity** (#1539): the per-agent list takes `report_type`/`hours`/
  `search` like the fleet list, both built from the same `_fleet_conditions`. One
  parameterized difference: `search` matches `agent_name` on the fleet list but not on a
  single-agent list, where every row carries that name and a matching term would return
  the agent's whole history. Payload contents are not searched — that needs the #1537
  storage rework, not an unindexed `LIKE` over a multi-MiB blob.
- **Large payloads** (#1537): cap raised to 5 MiB (measured first — the fleet's reports
  averaged 201 bytes, so the old 256 KiB cap was the wall the first real table would hit,
  not one agents were meeting). `GET /api/reports/{id}/rows` windows a `table` payload
  (`offset`/`limit`, true `total`); the UI fetches tabular reports through it, so expanding
  a 1.2 MB report transfers ~8 KB. Storage stays a single TEXT blob — no migration — and
  the slice is Python-side, so it bounds the response, not the read; off-row storage waits
  on a payload distribution that justifies it. The **portal** detail route carries the same
  window as two optional query params (`rows_offset`/`rows_limit`, #2162) rather than a second
  route: `/rows` is `Depends(get_current_user)`, which a portal principal cannot satisfy, and
  a clone on a client-facing prefix would need its own copy of the uniform-404 contract. There
  the *server* decides tabularity from the real payload (a non-tabular one returns whole with
  no `row_meta`, never a 400), and because paging re-reads the blob per request it is
  rate-limited per (client, agent).
- **Export** (#1536): `GET /api/reports/{id}/export?format=xlsx|pdf` →
  `services/report_export.py` (pure builders, lazily-imported `openpyxl`/`reportlab`, both
  pure-Python wheels). Shape mismatch degrades to a sensible sheet or JSON rather than
  erroring; access reuses the detail route's 404-not-403; missing libraries answer **503**
  with a rebuild hint so an un-rebuilt image (#1814) fails legibly on one endpoint instead
  of at router import.
- **An audience makes a report a deliverable (ent#365)**: `addressed_to_email` +
  `portal_session_id` (both nullable; NULL = operator-only, tied to no chat) turn a
  report into something a Workspace user can see. The address is validated against the
  publishing agent's own roster at the create route (never a key in the agent-authored
  `payload`, the ent#364 rule) and the session is resolved server-side from the
  publishing turn (`resolve_and_validate_execution` + the ent#286 in-flight marker), so
  an agent can neither choose a stranger's Workspace nor post into a chat it was not
  part of. The portal read is reader-scoped — `db.get_reports_for_client` — which
  replaced a call to the operator accessor that had shown every client of a shared agent
  every report it ever published (the ent#428 defect on the sibling ask surface). See
  [workspace-deliverables.md](../feature-flows/workspace-deliverables.md).
- **List = metadata, detail = payload**: list endpoints return `ReportSummary` (no payload);
  `GET /api/reports/{id}` returns the full payload, lazy-loaded when a card expands.
- **Fleet access**: `GET /api/reports` + `GET /api/reports/stats` filter via
  `accessible_agent_names` + `_narrow_to_agent` (admin = all). Renderers (`components/reports/`)
  pick by `display_hint` → `report_type` prefix → fallback, with a shape check per hint.
- **Three renderer surfaces, a per-surface fallback** (#2162): Agent Detail, the Operations fleet tab,
  and the **Workspace agent page** all mount the same `ReportRenderer`. The third was added
  after it shipped `JSON.stringify(payload)` to external clients — a disclosure defect
  (`payload` is free-form agent JSON of the class `client_portal/agent_page.py` refuses to
  expose for an ask's `context`, canary G-04), which a typed renderer narrows because it
  reads only the keys its hint declares. This matters to the CI pin above: `test_1535`
  regexes `payload.X` out of `ReportRenderer.vue`, so a third consumer widens that drift
  guard's blast radius and **`shapeOk` must stay in that file** — extracting it is the
  natural refactor and it empties the pinned set. The fallback is **per-surface**: the default
  stays `ReportJson`, so both operator surfaces render exactly what they always did, and only the
  Workspace passes `:fallback-component="ReportSummary"` (bounded, humanised, credential-shaped
  tokens redacted at value level, no raw payload reachable behind it). AC #2 asks for a client
  fallback "deliberately stricter than the operator side", so the split IS the design — a global
  summary would erase it, and a raw dump is a FEATURE when you are debugging an agent's own
  output. The override deliberately catches an agent-chosen `display_hint: "json"` as well as a
  shape mismatch, since `json` is a valid enum value and replacing only the mismatch path would
  leave an agent able to request a dump in front of a client.
- **Agent read-back** (#1538): `list_reports` / `get_report` MCP tools over the existing
  access-controlled REST endpoints — no new endpoint, no new tenant-boundary logic. The
  MCP layer adds the narrowing the backend cannot do (agent key → owner scope → `{self} ∪
  permitted`, the #1104 rule), and a denied `get_report` returns "Report not found" so the
  backend's deliberate 404-not-403 id-privacy choice isn't widened for agent keys. Closes
  the write-only loop: an agent can continue a series instead of duplicating it.
- **Retention**: `cleanup_service` `_sweep_retention_772` prunes rows past
  `agent_reports_retention_days` (default 90, `0` disables) via `db.prune_agent_reports`
  (chunked, `idx_agent_reports_created`). Table `agent_reports`; dual migration (SQLite
  `agent_reports_table` + Alembic `0006_agent_reports`).

### Agent Canvas — a durable surface an agent renders onto (ent#438)

A **report** (#918) is published once and accumulates; a **canvas** is one
surface the agent keeps *current*. `agent_canvases` is keyed on the composite
`(agent_name, canvas_id)`, so a write is an upsert and the surface is
addressable — that key is the whole difference, and it is why the canvas needs
**no retention window** (bounded by construction, deliberately absent from
`RETENTION_OPS_KEYS`) while `agent_reports` needs one. Router
`routers/canvas.py` → `services/canvas_service.py` → `db/canvas.py`; dual-track
migration (`agent_canvases_table` + Alembic `0050`); `AGENT_REFS`-registered,
where the rename half is load-bearing rather than tidy — `agent_name` is *half
the primary key*, so an unregistered table would leave a renamed agent's canvas
addressed to a name nothing resolves and its next write would mint a SECOND
canvas under the new name while the old one stayed visible.

- **The workspace merge.** `/agents/:name/workspace` — a Gemini voice orb beside
  an in-memory panel, gated on `VOICE_ENABLED && GEMINI_API_KEY &&
  WORKSPACE_ENABLED` — is **deleted**, its route a query-preserving redirect to
  `/workspace?agent=` (the ent#381 shape). Safe only because ent#440 had already
  put voice conversation inside the Workspace, so once the canvas moved the page
  had no capability of its own left. Two knock-on edits are load-bearing rather
  than cosmetic: `AgentHeader`'s button drops its `workspaceAvailable` gate (the
  Workspace needs no Gemini key, so gating would hide a working link on every
  install without one) and its stopped-agent disable (#2196 — the page reports
  availability itself, and a dead button is a worse answer than a page that says
  why); and `ChatPanel`'s voice overlay now passes `workspaceMode: true`, because
  the retired page was the ONLY caller that did — bridging the voice panel to the
  canvas while leaving it unreachable would be dead code wearing a fix's name.
- **Audience is how "never widens" is made structural.** `audience` ∈ `operator`
  (default) | `roster`, a validated COLUMN and never a key inside `blocks` (the
  ent#364 rule — `blocks` is agent-authored, so an audience buried there lets a
  prompt-injected agent choose its readers). `normalize_audience` is an
  ALLOWLIST, so an unrecognised stored value reads as `operator` (#2396's rule;
  the column is plain TEXT with no CHECK constraint). The Workspace read narrows
  **in the query**, not afterwards — a read that loads everything and filters in
  Python has already put an operator-only surface one edit away from the
  response (ent#365 FR-2). Roster gate and audience narrowing are both needed:
  one answers *may this person reach this agent*, the other *did the agent mean
  this for them*.
- **Staleness is derived, not a clock.** `stale` = the agent finished a run
  after the canvas was last written. An age threshold was rejected — a canvas
  has no inherent freshness expectation, so a clock cries wolf on a monthly
  summary or stays silent on a minute-by-minute one, whereas "the agent has run
  since" is a fact about *this* canvas, checkable against
  `updated_by_execution_id`. `last_completed_execution_at` is a `MAX` over the
  whole column rather than a windowed scan **because** a head full of
  `queued`/`running` rows would push the newest completed row out of a window
  and report a stale canvas as current. Fail-QUIET is available here and only
  here: the mark is an addition to an always-rendered `updated_at`, so missing
  evidence costs the mark, not the honesty — marking on no evidence would train
  the reader to ignore it. Derived once per agent, never once per canvas.
- **One rendering layer.** Blocks are `{kind, title?, payload}`;
  `table`/`kpi`/`markdown`/`timeline`/`json` **delegate** to the shared
  `components/reports/` dispatch (reused, never forked — those keys are CI-pinned
  by `test_1535_report_prompt_guidance.py`), and the canvas adds `chart`
  (`TrendLineChart` as-is) and `html` (DOMPurify via the EXISTING
  `utils/markdown.js`, so it shares the configured link hardening — a second
  sanitizer is a second policy to keep in step). The report `display_hint` enum
  is deliberately NOT widened: a canvas is a superset of a report's rendering,
  not a change to what a report is. An unknown kind resolves to `json`, never to
  nothing — a silently dropped block would leave the surface looking complete.
- **Writes are self-gated** (`AuthorizedAgent` proves the key's OWNER can reach
  the path agent, not that an agent-scoped key is writing its own canvas — the
  #918 rule, and here it is a disclosure surface too because a `roster` canvas is
  client-visible), rate-limited, id-charset-validated with a named 400, and
  capped at 50 blocks / 512 KiB (the byte cap being what the count cap cannot
  express). `execution_id` runs through `resolve_and_validate_execution`
  (MEM-001) and a foreign id **degrades to None rather than refusing** — it is
  provenance, not authorization. Reads are NOT self-gated: an operator is a
  user-scoped principal with no `agent_name`, and the `{self} ∪ permitted`
  narrowing for agent keys lives at the MCP layer.
- **The voice panel moved rather than being dropped.**
  `gemini_voice._execute_panel_tool` still updates the live `panel_state` and now
  persists it to canvas `voice` at fixed `audience="operator"` (a voice session
  always ran on an operator-authenticated surface). Mermaid and image panels map
  onto markdown blocks, so no new kind was needed; the write is fail-soft, since
  a canvas failure must not break the panel in front of the operator or the tool
  result the model is waiting on.
- **MCP**: `set_canvas` / `get_canvas` / `list_canvases` / `clear_canvas`. There
  is deliberately **no** `append_to_canvas` — `set_canvas` replaces, so
  read-change-write is the only sequence that leaves the surface in a state the
  agent chose.
- **OSS-core by decision (ent#438): deliberately ungated** — no
  `requires_entitlement`, logic in the OSS tree. Recorded explicitly because
  CLAUDE.md's default for an enterprise-tracker feature is *gated unless ruled
  otherwise*, so the ruling must never be inferred later from the mere fact that
  it merged (the ent#326 / ent#384 / ent#392 discipline). Rationale, on operator
  instruction: the Workspace and everything around it is OSS.

See [agent-canvas.md](../feature-flows/agent-canvas.md).

### Agent Evaluations — the referee surface (ent#206)

The `quality` axis for agent work, kept structurally apart from `completion` (a clean
process exit). Router `routers/evaluations.py` → `db/evaluations.py` → table
`agent_evaluations`; dual-track migration (SQLite `agent_evaluations_table` + Alembic
`0031`), `AGENT_REFS`-registered so rename re-keys and purge cascades.

- **The rule**: *a score is only trustworthy if the graded agent cannot write it.*
  `agent_reports` (#918) was rejected as the surface precisely because its create is
  self-gated — right for an agent's own output, wrong for a grade.
- **Write fence** (`POST /api/agents/{name}/evaluations`): `require_admin` **AND**
  `reject_agent_principal`. The second gate carries the weight — an agent-scoped key
  resolves to its owner and inherits the owner's role, so on a default admin-owned
  install `require_admin` alone would let a graded agent grade itself (the
  trinity-ops-agent#232 trap). The surface has exactly one write route and no
  agent-writable path.
- **Read is access-scoped, deliberately unfenced**: `AuthorizedAgentByName` for an
  agent's own evaluations, `accessible_agent_names` for the fleet list. An agent seeing
  its own bad score is the feedback loop; read ≠ write.
- **`quality` is nullable and null ≠ 0** — the axes are independent, so a run can carry
  `completion` before anything has graded it. The Tier-0 evaluator that populates
  `quality` is a later child; this is the surface it writes to.
- **Completion relabel**: Overview (#1107), schedules rollup (#1115) and fleet stats
  (EXEC-022) now say "Completion", not "Success rate". Additive — the `success_rate`
  API field is unchanged; only the label moved.
- **Workspace ratings amend the write fence (ent#366)**: a one-click thumb (message) or
  Useful/Not-what-I-needed (deliverable) writes here under `evaluator = workspace:<email>`
  — the fence exists so the graded agent never writes its own grade, and a user rating is
  precisely the score that must not pass through the thing being scored, so a Workspace
  principal is admitted rather than the rule bent. Targets are checked against the READER
  (ids are global) with a uniform 404; `UNIQUE(evaluator, target_kind, target_id) WHERE
  target_id IS NOT NULL` makes a re-rate a correction, so a tally counts people not
  clicks. **The rated agent reads tallies and never the comment**
  (`_redact_for_agent_principal` → `comment_withheld`): a readable score is a loop an
  agent may optimise for, and client free text handed verbatim to the agent being
  criticised is a prompt-injection path into it. See
  [workspace-ratings.md](../feature-flows/workspace-ratings.md).
- **OSS-core by decision** (strategy gate ent#206 §10): the enforcement primitive and
  the deterministic tier are edition-agnostic; the managed grading experience is the
  paid layer, mirroring #668.

See [agent-evaluations.md](../feature-flows/agent-evaluations.md).

