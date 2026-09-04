# Requirements — Lifecycle & Observability — Soft-Delete, Compatibility, First-Run, Reports

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 33. Agent Soft-Delete & Retention Lifecycle (#834)

### 33.1 Agent Soft-Delete + Retention Purge (#834 — Phase 1a)
- **Implements**: Issue #834 Phase 1a
- **Description**: `DELETE /api/agents/{name}` no longer hard-deletes the
  `agent_ownership` row. It marks `agent_ownership.deleted_at = NOW`
  (NULL = live) and preserves every per-agent child row, so an
  accidentally-deleted agent's history, schedules, and config remain
  recoverable until the retention window expires. The container and
  runtime resources are still torn down on delete — only the database
  rows are retained.
- **Retention purge**: the Cleanup Service (`cleanup_service.py`, 5-min
  loop) hard-purges `agent_ownership` rows whose `deleted_at` is older
  than `agent_soft_delete_retention_days` (default **180**, `0` =
  disabled — soft-deleted rows then persist until manually purged).
  Purge runs the #816 `purge_agent_ownership` → `cascade_delete`
  primitive so all per-agent child rows are removed in one transaction;
  `KEEP`-policy tables (`schedule_executions`, `nevermined_payment_log`)
  survive per their own retention discipline. Each purge additionally
  removes the agent's Docker data volumes (#1581) and is therefore
  **unrecoverable** — so the #1644 blast-radius guard floors this sweep
  at **0**: any purge at all requires an explicit admin acknowledgement
  before it runs. (`RETENTION_CHUNK_SIZE_PER_CYCLE` bounds each
  *transaction*, not the call — there is no per-cycle row cap; see #1644.)

- **Name reservation**: `is_agent_name_reserved()` is intentionally
  unfiltered — it sees soft-deleted rows so a soft-deleted name cannot
  be reused (and silently clobbered) before purge.
- **Scheduler gap closed**: `list_all_enabled_schedules()` (backend +
  the standalone scheduler process) joins `agent_ownership` and filters
  `deleted_at IS NULL`, so a soft-deleted agent's enabled schedules stop
  firing immediately rather than generating a `schedule_executions`
  failure row per cron tick for up to 180 days.
- **Canary**: soft-deleted agents are intentionally *kept* in the
  canary snapshot's `known_agents` set (NOT filtered by `deleted_at`) so
  L-03 (delete-cascade) does not false-positive on the child rows that
  are legitimately preserved until the retention purge runs.
- **Setting**: `agent_soft_delete_retention_days` in the ops settings
  block (default `"180"`, `"0"` disables).
- **Storage**: `agent_ownership.deleted_at TEXT` + partial index
  `idx_agent_ownership_deleted_at ON agent_ownership(deleted_at) WHERE
  deleted_at IS NOT NULL`. Migration
  `agent_ownership_soft_delete`.

### RETENTION-GUARD-001: Retention prunes are blast-radius guarded
- **Implements**: Issue #1644 (follow-up to #1638)
- **Description**: before any window-driven destructive prune,
  `services/retention_guard.py` counts the candidate set (bounded, so the
  cost is O(threshold) not O(candidates)) and **refuses** the prune if it
  exceeds the threshold, logging at ERROR and raising an `operator_queue`
  alarm naming the setting, the window, the window's source
  (`db-row`/`code-default`), and the counts. The prune proceeds only after
  an admin acknowledges it. Covers all **8** window-driven prunes
  (`RETENTION_OPS_KEYS` has carried 8 since #1296 added
  `agent_reminders_retention_days`; `cleanup_service` has 8 `_guard_allows`
  sites). `cleanup_service` is not the only consumer:
  `GET /api/settings/retention` re-runs the same `evaluate` live so the
  Settings panel can offer an approve control, and that call is unwrapped.
- **Why**: #1638 fixed one *mechanism* (a retroactive default change).
  It left every other route to a destructive window open — an unvalidated
  `PUT /api/settings/ops/config`, a future default regression, a direct DB
  write. The guard does not care how the bad window arrived.
- **Acknowledgement**: `POST /api/settings/retention/acknowledge`
  (admin **and** human-only) is **the gate**; the operator-queue item is an
  alarm and authorizes nothing — which is what makes it safe to RETRY a
  failed alarm write every cycle for the life of the refusal episode
  (#1834; the memo used to be written before the attempt, so one failed
  write permanently suppressed its own retry and the durable half of the
  signal was lost until a restart or a window change). There is deliberately
  no give-up: this sink is the platform's own DB, whose outages routinely
  outlast any budget, so abandoning would ship #1834's symptom inside
  #1834's fix. The per-attempt WARNING escalates **once** to ERROR past
  `ALARM_ESCALATION_AGE_SECONDS`. 'Delivered' means the call did not raise,
  never 'a row was inserted' — `create_item` returns the id of the row that
  exists, so a second worker's conflict no-op would otherwise retry forever.
  An ack is **bound to the window in force**
  (409 on mismatch — approving a prune at 30 days does not approve one at
  1 day) and **single-use** (consumed once the prune runs, so the guard
  re-arms and one approval can never authorize an unboundedly larger
  future delete at the same window).
- **Threshold**: a **fixed constant** (`retention_guard.MAX_ROWS_PER_SWEEP`,
  1000) — deliberately NOT an operator setting. It was briefly configurable via
  Settings; that was wrong twice over: nobody can reason about the right value
  (it depends on per-cycle churn they cannot see — the panel needed a caption
  explaining that *bigger is worse*, and a control that must explain which way is
  safe is the wrong control), and a mutable constant read at action time gating a
  destructive operation is #1638 one level up — raising it would silently disarm
  the guard fleet-wide. Deleting the knob deleted its clamp, its endpoint, its
  blocklist entry, and a whole fail-closed branch. Chosen against **steady state,
  not table size**: only rows crossing the cutoff within one 5-min cycle are
  candidates, so four digits means something changed. Lowering is always safe;
  raising is a code change with a reviewer, not a text box. Surfaced read-only at
  `GET /api/settings/retention` → `guard.max_rows`. Per-sweep floors: rows →
  the constant, schedules → 100, agents → **0**.
- **Fail-closed**: any error **refuses** the prune — the count throws
  (`count_failed`), the count cannot be compared to the threshold
  (`count_uninterpretable`, #1833), the count is negative, i.e. an error
  sentinel rather than a count (`count_negative`, #1833 — `-1 <= threshold`
  is True and `-1` is the module's own 'unknown' value, so this was a real
  fail-OPEN), the ack lookup throws (`ack_lookup_failed`). A guard that
  fails open is worse than no guard because it manufactures confidence —
  and one that RAISES instead of refusing keeps the data (control never
  reaches `db.prune_*`) while losing the alarm, so #1833 moved the
  comparisons inside the try rather than documenting the raise. The
  comparison result is type-checked, not coerced: a `__le__` returning a
  truthy non-bool would make a bare `bool(...)` True and authorize a prune
  on a count the guard never understood. `verdict.candidates` is always an
  int (`-1` = unknown) because it reaches the alarm message, the alarm
  `context` via `json.dumps`, and `GET /api/settings/retention`, where a
  bare `NaN` is valid to Python and rejected by a browser's `JSON.parse`.
  A refusal an ack cannot clear says so instead of prescribing one, and the
  endpoint reports it under `blocked_sweeps` rather than showing a clean
  'nothing pending' for a sweep that is blocked forever — scoped, like the
  `pending_acknowledgements` list beside it, to the two ack-gated sweeps
  that endpoint re-runs (agents, schedules). The other six windows are not
  evaluated there; their refusals reach an operator through the durable
  operator-queue alarm only. (There is no 'threshold unreadable' path: the
  threshold is a constant, so that failure mode does not exist.)
- **Expected behaviour**: a legitimate first-enable of retention on a
  mature install *will* trip the guard once, and that is intended — the
  guard cannot distinguish a large legitimate backlog from a mistyped
  window, so it asks once and the operator acknowledges once.

### 33.2 Schedule Soft-Delete (#834 — Phase 1b)
- **Implements**: Issue #834 Phase 1b (PR #839)
- **Description**: `DELETE /api/agents/{name}/schedules/{id}` marks
  `agent_schedules.deleted_at = NOW` instead of hard-deleting. The row
  and its `schedule_executions` are preserved for the retention window
  so an accidentally-deleted schedule (and its run history) is
  recoverable.
- **Read paths**: every schedule read filters `deleted_at IS NULL` —
  including the cron-firing `list_all_enabled_schedules()` in **both**
  the backend (`db/schedules.py`) and the standalone scheduler process
  (`src/scheduler/database.py`), so a soft-deleted schedule stops firing
  immediately. That firing query also retains the Phase 1a
  `agent_ownership` join (`ao.deleted_at IS NULL`), so a schedule is
  skipped if **either** it or its agent is soft-deleted.
- **Idempotency**: `delete_schedule()` on an already-soft-deleted row is
  a no-op success (no double-soft-delete, no error).
- **Retention purge**: the Cleanup Service hard-purges `agent_schedules`
  rows past `schedule_soft_delete_retention_days` (default **30** —
  shorter than the 180-day agent window because schedules are
  higher-churn; `0` = disabled). `purge_schedule()` refuses to purge a
  live row and cascades the schedule's `schedule_executions` delete
  alongside the parent row — consistent with the previous hard-delete
  behavior and with agent-purge `cascade_delete`. No #816 chain
  (schedules have no #816-registered child tables). Bounded by the
  shared 5000-row/cycle cap.
- **Execution-row ownership**: pre-purge, a soft-deleted schedule's
  `schedule_executions` are #772's responsibility (its 90-day
  terminal-row sweep ages them out independently); at purge they are
  deleted with the row.
- **Setting**: `schedule_soft_delete_retention_days` in the ops
  settings block (default `"30"`, `"0"` disables).
- **Storage**: `agent_schedules.deleted_at TEXT` + partial index
  `idx_agent_schedules_deleted_at ON agent_schedules(deleted_at) WHERE
  deleted_at IS NOT NULL`. Migration in `db/migrations.py`.

### 33.3 Admin Recovery Endpoints (#834 — Phase 1c)
- **Implements**: Issue #834 Phase 1c (PR #840)
- **Description**: Admin-only surface to list and recover soft-deleted
  agents/schedules before the retention purge hard-deletes them.
  Replaces the prior shell-only workaround (manual `UPDATE ... SET
  deleted_at = NULL`), which required DB access and was unauditable.
- **Endpoints** (all `require_admin`, all audit-logged):
  - `GET /api/admin/soft-deleted/agents` — list soft-deleted agents,
    newest first. Each row carries a computed `purge_eta` (when the
    retention sweep would hard-purge it; `null` when
    `agent_soft_delete_retention_days = 0`). `limit` capped at 500.
  - `POST /api/admin/soft-deleted/agents/{name}/recover` — clear
    `deleted_at`. 404 if not in the soft-deleted set. **Metadata-only**:
    the Docker container is *not* recreated (removed at soft-delete);
    the agent shows `status=stopped` / `needs_container_recreate=true`.
    Operator brings it back via `POST /api/agents/{name}/start` from the
    preserved workspace volume. Container recreate-on-recover is #834
    Phase 2.
  - `GET /api/admin/soft-deleted/schedules` — list soft-deleted
    schedules (optionally `?agent_name=`-scoped), with `purge_eta` from
    `schedule_soft_delete_retention_days`. `limit` capped at 500.
  - `POST /api/admin/soft-deleted/schedules/{id}/recover` — clear
    `deleted_at`. 404 if not soft-deleted. The schedule rejoins the
    scheduler firing list on the next poll if it was enabled.
- **Recovery semantics**: flips `deleted_at` back to NULL; child rows
  already survived the soft-delete so the entity is immediately usable
  via the regular (deleted_at-filtered) read paths.
- **Audit**: every recovery emits an `agent_lifecycle:recover` /
  `agent_lifecycle:schedule_recover` platform-audit event.
- **Models**: `SoftDeletedAgent` / `SoftDeletedSchedule` response
  models live in `models.py` (Architectural Invariant #14).

---

## 42. Agent Compatibility Validation (#668)

### 42.1 Server-Side Compatibility Checks with Auto-Fix (#668)

**Description**: Agents deployed to Trinity that don't follow Trinity
best-practices (no playbooks, missing YAML, `.claude/` excluded from
`.gitignore`, no `template.yaml`) fail silently at runtime in ways that are hard
to diagnose. Trinity runs **server-side compatibility checks** against a running
agent's workspace and surfaces actionable recommendations — **without blocking
deployment**. Canonical check list: **`docs/agent-validation-spec.md`** (100
checks, 11 categories), the single source of truth kept in lockstep with
`services/compatibility/spec.py` by a sync test.

- **FR-1 — Surface**: results render in the Agent Detail **Overview tab**
  (`components/CompatibilityPanel.vue`, reusing the "needs attention" idiom —
  count hidden when clean, expandable to the full grouped checklist) and via the
  MCP tool `get_agent_compatibility_report`. Re-runnable on demand. Non-blocking.
- **FR-2 — Severity**: each check is **HARD** (will likely break Trinity),
  **SOFT** (best practice), or **INFO**, with `pass`/`fail`/`skipped` status.
  HARD is reserved for deterministic STATIC checks; **AI-evaluated checks are
  capped at SOFT** (an LLM verdict never drives the HARD count).
- **FR-3 — Check types**: `[STATIC]` deterministic file/pattern analysis (run
  always, free); `[AI]` LLM-evaluated quality judgments (Claude Haiku, batched by
  category, persisted so they show on every load; `include_ai` forces a re-run).
- **FR-4 — Collection**: ONE `docker exec` runs an in-container Python script
  that emits a single JSON workspace snapshot (per-file binary/size/truncation
  handling, secret-bearing files existence-only); pure check functions evaluate
  the snapshot (unit-testable, no Docker). Stopped/unreadable container → a
  degraded `unavailable` report (showing the last persisted result), never a 500.
- **FR-5 — Auto-fix**: the 10 gitignore-related checks are auto-fixable via
  `POST /api/agents/{name}/compatibility/fix` (owner/admin); the fix edits the
  in-container `.gitignore` only (atomic write, per-agent Redis lock) and is
  **uncommitted until the agent's next git sync** (no auto-commit).
- **FR-6 — Runtime-aware**: Claude-specific checks (`CLAUDE.md`, `.claude/`
  skills) are omitted for non-Claude runtimes (Codex/Gemini, #1187).
- **FR-7 — Reuse/consolidate**: builds on the #950/#982 deploy-local logic
  (`_is_platform_injected`, the `${VAR}`/`.env.example` parsing) for the
  C-001/C-002 and K-001/T-015 overlaps, and on `git_service._GITIGNORE_PATTERNS`
  + `_detect_git_dir` for the fixes.

**API**: `GET /api/agents/{name}/compatibility?include_ai=` (read; STATIC live +
persisted AI), `POST /api/agents/{name}/compatibility/fix` (owner/admin).
**MCP**: `get_agent_compatibility_report(agent_name, include_ai?)`.

**Persistence decision (departs from the issue's "no DB table" note).** The
original issue specified transient results with no table. Implementation **adds
`agent_compatibility_results`** (latest-snapshot-per-agent, dual-track SQLite +
Alembic) because AI verdicts are **not** cheaply recomputable (they cost API
calls): persistence lets AI findings show on every Overview load without
re-spending tokens, unlocks fleet aggregation ("N agents have HARD findings"),
and enables cheap post-fix re-checks. STATIC checks still recompute live each
read; persisted AI verdicts merge in until a re-run. History/trend retention is a
fast-follow (latest-only for now).

**Out of scope (fast-follow)**: broken-agent **boot** triage (a stopped/failing
container can't be exec'd — this validates *running* agents); AI-verdict trend
history; the forward-looking template-level checks (#927 replica-safety, #1084
side-effect profile).

---

## 43. First-Run Operator Profile — Intake + Admin Email Login (trinity-enterprise#38, #82)

### 43.1 Operator Intake at First-Run Setup (trinity-enterprise#38)

**Description**: At first-run setup (the admin-creation step), the operator may
provide their **email + company** (plus optional name/role/use-case) and **opt
in** to "occasionally receive important security & product updates." On that
affirmative consent, the details are submitted **once** to an Ability.ai-operated
hosted intake endpoint — a sibling endpoint on the same Cloudflare-fronted intake
app as #1116's in-app bug reporter (`/v1/report-bug` → `/v1/operator-intake`).
This is **identifiable, explicit opt-in contact capture**, distinct from the
anonymous usage telemetry tracked separately (#758 / trinity-enterprise#12).

- **FR-1 — Capture & consent**: **required `email`** (the admin sign-in identity,
  trinity-enterprise#49) plus optional `company`/`name`/`role`/`use_case` on
  `POST /api/setup/admin-password`; an **affirmative, unchecked-by-default**
  consent checkbox (`consent_updates`). Declining the updates opt-in (or skipping
  the optional profile fields) never blocks completing setup; only the email and
  password are mandatory. The form shows exactly what is sent and to whom.
- **FR-2 — Hosted intake, no email needed**: the submission is a fire-and-forget
  HTTPS POST (`services/operator_intake_service.py`, `httpx`, 5s) — it does **not**
  use the email provider, so it works on a fresh install with no Resend key. A
  blocked/failed/air-gapped POST never delays or breaks setup.
- **FR-3 — At-most-once**: a server-side `operator_intake_submitted` marker in
  `system_settings` is claimed **before** the POST, so restarts / re-runs /
  concurrent workers never double-submit. A stable random `installation_id`
  (also in `system_settings`, the seed for future #758 telemetry) correlates the
  submission.
- **FR-4 — Off switch**: `OPERATOR_INTAKE_ENABLED=false` (or the cross-tool
  `DO_NOT_TRACK=1`) fully disables the outbound submission for air-gapped /
  privacy-strict installs — the consent box still appears, nothing leaves the box.
  `OPERATOR_INTAKE_URL` repoints the endpoint (self-host). Consent fires only on
  `consent_updates && email`.

### 43.2 Admin Email Login — Phase 1 (#82)

**Description**: The email captured at setup becomes the admin's **sign-in
identity** — the operator can log in with **email + password** instead of the
fixed `admin` username. **No verification email is sent**: a fresh install has no
email provider configured, so the email is simply *bound* to the admin account
(not verified via a code). The code-based second factor (email OTP after
password) is **Phase 2**, gated on a configured email provider and the existing
`mfa_gate`/`SecondFactorProvider` seam (#5/#388) — out of scope here.

- **FR-1 — Resolve by username OR email**: `dependencies.authenticate_user`
  resolves the identifier by username, then (when it looks like an email and no
  username matches) by email. The password check still runs, so only an account
  with a password hash (the admin) can authenticate — email-code-only users
  (no password) never can.
- **FR-2 — Setup binding**: `POST /api/setup/admin-password` **requires** the email
  (missing → 422 at the model layer; blank/typo → 400, validated before any write
  so setup never half-completes) and binds it to the admin via
  `db.update_user('admin', {'email': …})`. Login UI exposes an editable
  "Username or email" field (default `admin`). The setup token (#1165/SEC #177) is
  removed (trinity-enterprise#49) — no token field, no Redis dependency for setup.
- **FR-3 — Existing-admin transition**: an admin created before #82 (stored email
  = placeholder `admin`) registers a real email via `PUT /api/users/me/email`
  (own-account scoped; 409 if the email belongs to another account), surfaced as
  an **Admin sign-in email** card in Settings → General. No verification email is
  sent; existing `admin`+password login keeps working until/unless an email is set.

---

## 44. Agent-Reported Structured Reports (#918)

**Description**: A generic **agent report** primitive — agents publish typed-but-flexible
structured reports (telemetry, domain results: leads found, KPI snapshots, weekly summaries)
via an MCP tool. Reports are persisted, surfaced on the Agent Detail "Reports" tab and a
fleet-wide Reports view, so users see what each agent produces without reading chat
transcripts. Three-surface feature (backend router, MCP tool, frontend); no agent-server
endpoint — reports flow agent → MCP → backend.

- **FR-1 — MCP tool `report`**: `report(report_type, title, payload, display_hint?,
  schema_version?, period_start?, period_end?)`. The reporting agent + author are resolved
  **server-side** from the MCP auth context (agent-scoped key → bound agent); the tool
  requires an agent-scoped key so a report cannot be attributed to another agent.
- **FR-2 — Storage**: `agent_reports` table (id, agent_name, user_id, report_type, title,
  payload JSON, display_hint, schema_version, period_start/end, created_at). Indexes on
  `(agent_name, created_at DESC)`, `(report_type, created_at DESC)`, and `(created_at)` for
  the retention sweep. Dual-track migration (SQLite `migrations.py` + Alembic `0006`).
- **FR-3 — Backend API** (access control mirrors `/api/executions`): self-gated `POST
  /api/agents/{name}/reports` (agent-scoped key must equal the path agent; payload capped at
  5 MiB → 413; fields strictly validated), `GET /api/agents/{name}/reports` (metadata only),
  `GET /api/reports` (fleet, accessible-agent filtered; `agent`/`report_type`/`hours`/`search`),
  `GET /api/reports/stats` (total / by_type / agents KPI counts), `GET /api/reports/{id}`
  (full payload; 404 on no-access), `DELETE /api/agents/{name}/reports/{id}` (owner; scoped by
  agent_name + id).
- **FR-4 — Real-time**: a **thin** `agent_report` WebSocket trigger (agent_name, report_id,
  report_type, created_at — never title/payload, since `/ws` is unfiltered SCOPE_ALL); the
  frontend refetches via the access-controlled REST endpoints.
- **FR-5 — Frontend**: **three** surfaces share one renderer set — Agent Detail "Reports" tab,
  Operations → "Reports" fleet tab, and the **Workspace agent page's Reports tab** (§5.11 of
  `core-agent.md`). Typed renderers (table / KPI tiles / markdown / timeline) chosen by
  `display_hint`, then `report_type` prefix; each validates payload shape and falls back on
  mismatch. List shows metadata; full payload lazy-loads on expand.

  **The fallback is per-surface, and the client-facing one is stricter (#2162).** The shared
  default stays the JSON viewer: an operator reading a malformed report is debugging an agent's
  own output, where the raw payload is the useful answer. For an external client it is not —
  `payload` is free-form agent-authored JSON of the same class as an ask's `context`, which the
  Workspace refuses to expose at all (a known credential-leak surface, canary G-04). So
  `ReportRenderer` takes a `fallbackComponent` override (default `ReportJson`, so every operator
  call site is untouched) and the Workspace passes `ReportSummary`: a bounded, humanised key-value
  view (≤40 entries, truncated values, depth 1, credential-shaped tokens redacted) with **no raw
  payload reachable behind it at all**. The override covers an agent-chosen
  `display_hint: "json"` as well as a shape mismatch — `json` is a valid value in the MCP tool's
  enum, so replacing only the mismatch path would leave an agent able to dump on request. A typed
  renderer reads only the keys its hint declares, so routing a client through the shared set
  **strictly reduces** what is exposed; the summary bounds and humanises the residual rather than
  eliminating it (a boundary-side scrub is the general fix).

  **Enumerating the surfaces here is load-bearing**: FR-5 said "two" while a third shipped a raw
  dump to clients for two releases, which is how #2162 happened. A fourth consumer belongs in
  this list before it ships.
- **FR-5a — Client-facing row windowing (#2162)**: the Workspace cannot use FR-9's
  `GET /api/reports/{id}/rows` — that route is `Depends(get_current_user)` and a portal principal
  is a verified email with no `users` row (the #2128 structural fact). Rather than clone the
  route on a client-facing prefix, the **existing** portal detail route takes two optional query
  params, `rows_offset` / `rows_limit`, and windows `payload["rows"]` **only when the payload
  really is `{columns, rows}`**, attaching `row_meta {total, offset, limit}` when it does.
  `rows_limit` absent → byte-identical to before, so the change is purely additive; a non-tabular
  payload with `rows_limit` set returns whole with no `row_meta` — never a 400, because the
  server holds the payload and the client should not have to guess its shape from an
  agent-authored `display_hint` that can disagree with it. Inherits the detail route's roster
  gate and its uniform 404 rather than re-deriving either. Same honest limit as FR-9: the slice
  is Python-side after the whole blob is read, so it bounds the **response**, not the read — and
  because it is client-reachable and loopable it is **rate-limited** per (client, agent).
- **FR-6 — Retention**: cleanup sweep deletes `agent_reports` older than
  `agent_reports_retention_days` (default 90; `0` disables), chunked like the #772 sweeps.
- **FR-8 — Agent read-back** (#1538, epic #1534): MCP `list_reports` (metadata; filters
  `agent_name`/`report_type`/`hours`/`search`, paged) and `get_report` (full payload by id)
  over the **existing** FR-3 endpoints — no new endpoint, no new tenant-boundary logic. The
  MCP layer adds the one gate the backend structurally cannot: an agent-scoped key resolves to
  its **owner**, so the backend scopes reads to everything the owner sees; the tool narrows a
  broad listing to `{self} ∪ permitted` (the #1104 operator-queue rule) and re-checks the
  owning agent on `get_report`. A denied `get_report` returns the backend's own
  `Report not found` shape, so the deliberate 404-not-403 id-privacy choice (FR-3) is not
  widened for agent keys. Closes the write-only loop: an agent can see what it already filed
  and continue a series rather than duplicate or contradict it. The FR-7 prompt block points
  at it, so read-back is discoverable in the same breath as publishing.
- **FR-9 — Search & filter, per-agent parity** (#1539, epic #1534): the per-agent list
  (`GET /api/agents/{name}/reports`) gains `hours` + `search`, matching the fleet list it
  had drifted from — the Agent Detail Reports tab was a flat unfilterable list, and any
  caller scoping to one agent (including FR-8's `list_reports`) had both filters silently
  dropped. Both routes build their WHERE through the SAME `_fleet_conditions`, with one
  parameterized difference: `search` matches `agent_name` on the fleet list (that is how
  you find "everything scout published") but NOT on a single-agent list, where every row
  carries that name and a matching term would return the whole history looking like search
  was ignored. `hours` is whitelist-validated (`_VALID_HOURS`) on both, falling back to the
  7-day default rather than erroring so an old client keeps working. UI: the same filter
  bar as the fleet view minus the agent picker, with the empty state distinguishing "no
  reports yet" from "no reports match these filters". **Payload contents are deliberately
  NOT searched** — a `LIKE` over a multi-MiB TEXT blob with no index degrades exactly as the
  feature succeeds; an FTS answer belongs with #1537's storage rework.
- **FR-10 — Large payloads: raised ceiling + row windowing** (#1537, epic #1534):
  `REPORT_PAYLOAD_MAX_BYTES` 256 KiB → **5 MiB**, and `GET /api/reports/{id}/rows`
  (`offset`/`limit`, default 100, max 1000) returns a WINDOW of a `table` payload —
  columns once, a slice of rows, and the true `total` — so expanding a card never ships
  the whole blob. The frontend fetches `table` reports through it (branching on the
  `display_hint` already in the summary, so no extra request decides) with a
  "Showing N of M · Load more" footer; every other hint is a bounded document and still
  fetches whole. Create gains a Content-Length pre-check that refuses an oversized body on
  the header before the parsed payload is re-serialized; the exact byte check still
  enforces. Non-tabular payloads answer **400** on the rows route (no row axis to slice)
  and no-access answers **404**, matching `GET /reports/{id}` so an id stays unprobeable.
  **Storage is unchanged — single TEXT blob, no migration.** That is a measured decision,
  not a deferral by default: on a live fleet the existing reports averaged 201 bytes and
  the largest was 683, so an off-row rows table would have been a schema commitment made
  against a hypothetical. The honest residual: the row slice happens in Python after the
  whole blob is read, so it bounds the RESPONSE, not the read — moving the slice into SQL
  requires the off-row model, and the trigger for that should be a payload distribution
  that actually approaches this ceiling.
- **FR-11 — Export to .xlsx / .pdf** (#1536, epic #1534):
  `GET /api/reports/{id}/export?format=xlsx|pdf` renders a stored report as a real
  spreadsheet (cells, typed values, `{columns, rows}` honouring both positional and
  column-keyed rows) or a formatted PDF (table stays a table, markdown stays prose).
  Builders live in `services/report_export.py` as pure `(payload, hint, title) -> bytes`
  functions; the router owns access, format validation and headers.
  **Shape mismatch degrades, never 500s**: `kpi` → label/value/unit sheet, `timeline` →
  event columns, anything unrecognized → pretty-printed JSON in one cell. Access reuses the
  detail route's **404-not-403**, so an export URL cannot become the existence oracle that
  route refuses to be; `Content-Disposition` is built from a sanitized title (quotes,
  newlines and separators stripped, not escaped) and carries `X-Content-Type-Options:
  nosniff` like the FILES-001 download. PDF caps at `PDF_MAX_ROWS` (2000) **with a visible
  note** pointing at the spreadsheet — a 12,000-row PDF is not a document anyone reads.
  Agent-authored text is escaped before reportlab parses its mini-HTML dialect.
  **Dependencies** (`openpyxl`, `reportlab`) are pure-Python wheels — no system libraries,
  so the image build is otherwise unchanged (WeasyPrint was rejected for exactly that
  reason). They are imported **lazily**, so an instance that upgrades code without
  rebuilding the image (#1814) gets **503 with a rebuild hint** on that one endpoint
  instead of an import error taking the whole reports router down.
- **FR-7 — Discoverability via the platform prompt** (#1535, epic #1534): `PLATFORM_INSTRUCTIONS`
  carries a "Publishing Reports" block, so reporting is a default fleet behaviour instead of
  something only agents whose own CLAUDE.md mentions it ever do. Documents the call, when to
  reach for it (results a human re-reads: scheduled-run findings, batch summaries, KPI
  snapshots), the payload shape per `display_hint` — the shapes FR-5's renderers dispatch on,
  since a mismatch fails silently into the fallback (a bounded key-value summary since #2162,
  and on the client-facing Workspace surface that is *all* the reader gets, with no raw payload
  behind it — so a wrong shape costs the intended presentation outright) — the
  aggregate-before-publishing
  expectation given the FR-3 cap, and the reports-are-one-way boundary against §26's operator
  queue. Runtime-aware for free via `_adapt_instructions_for_runtime` (#1187: Codex gets bare
  `report`, not `mcp__trinity__report`), and the Codex orientation note lists the tool.
  Additive — templates that already instruct reporting are unaffected. Budget: the block ships
  on every turn of every agent, so it is capped (~1.3 KB) and CI-pinned to the MCP tool's
  `display_hint` enum and the renderers' payload keys, which is where silent drift would live.

**Deferred**: effect-guard dedup on `report()` for at-least-once pull-mode re-delivery
(#1084/Epic #1045); audit-log entry on write; per-report sharing distinct from agent access.

---

## 45. Local Product-Event Capture — Activation Funnel, Tier-1 (ent#184)

**Description**: A **local-only** product-event capture layer — the **Tier-1**
half of the two-tier telemetry model (Tier-2 = opt-in anonymized fleet sharing,
#758 / trinity-enterprise#12, which builds on this). Tier-1 records
activation/usage events **on the operator's own instance, default-ON, with zero
network egress**, so the operator can see where their own first-run users drop
off. It is *not* a sovereignty concern — nothing leaves the box — and is distinct
from the identifiable opt-in operator intake (§43.1): this is anonymous,
instance-local instrumentation keyed by the same `installation_id`.

**Open-core split** (product decision, gating confirmed ent#184): the **capture**
is OSS-core (the edition-agnostic instrumentation primitive, default-on); the
operator-facing **activation-funnel view** is an entitlement-gated enterprise
surface (`telemetry` feature-id). The generic seam is documented here; the funnel
module's design lives in the private submodule.

- **FR-1 — Event set v1 (OSS capture)**: the genuinely-new client beacons are the
  onboarding-wizard step transitions — `setup_started`, `setup_step_intro`,
  `setup_step_create`, `setup_step_credential`, `setup_completed`,
  `setup_dismissed` — emitted by `components/OnboardingWizard.vue` through
  `stores/productTelemetry.js` → `POST /api/product-events`. **First-value
  events** (`first_agent_created`, `first_chat`, `first_schedule_created`,
  `first_channel_connected`) are **derived on read** from the rows Trinity
  already writes (`audit_log`, `agent_activities`, `schedule_executions`), never
  re-emitted — so they survive restart by construction and add no write path.
- **FR-2 — Storage (OSS)**: a local SQLite/Postgres table `product_events`
  (`installation_id`, `event_type`, `event_context` optional small JSON,
  `created_at`; dual-track migration + `db/tables.py` MetaData). The emit
  endpoint accepts only a **fixed allow-list** of `event_type` values (unknown →
  422) so the table can't be spammed with arbitrary strings. Rows carry the
  stable `installation_id` (§43.1) and a UTC timestamp so Tier-2's opt-in
  **retroactive backfill at consent** can serialize history — the mechanism that
  rescues early-funnel data despite consent arriving late.
- **FR-3 — Zero egress**: the capture layer NEVER phones home; the emit endpoint
  writes one local row and returns. All sharing/consent lives in Tier-2 (#12).
  Verifiable and documented as local-only in user docs.
- **FR-4 — Operator funnel view (enterprise-gated)**: an operator-facing
  activation/funnel panel on an existing admin surface (Settings, admin-only)
  shows step-by-step activation counts + drop-off with an honest empty state when
  there's no data yet. It reads a gated enterprise endpoint
  (`requires_entitlement("telemetry")`) that aggregates `product_events` +
  derives the first-value events from the OSS tables above. The **panel Vue**
  ships in the OSS bundle but is hidden unless `telemetry` is in
  `enterprise_features` (the standard feature-flag gating). Explicitly **NOT** a
  new standalone analytics dashboard in v1.

**Deferred**: auto-retention sweep for `product_events` (volume is negligible —
a handful of rows per install); per-user (vs per-install) funnel cohorts.

### 45.1 Tier-2 — Opt-in Fleet Sharing (ent#12)

**Description**: the **opt-in egress** layer on top of Tier-1 (§45). On
**explicit, default-off, reversible** operator consent, Trinity periodically
shares **anonymized aggregates** with the Ability-operated hosted intake in
exchange for reciprocal value (fleet benchmarks). The hosted aggregation/benchmark
service is a **separate issue**; this covers the client consent + egress +
backfill + the gated benchmark status surface.

**Open-core split** (gating confirmed ent#12): the **consent + egress + backfill**
are OSS-core (the sovereignty primitive — the operator's choice to share is
edition-agnostic, and it mirrors the OSS operator-intake #38 credential-free
transport); only the **reciprocity benchmark view** is entitlement-gated
(`telemetry`).

- **FR-1 — Two-gate egress, never without consent**: egress fires only when BOTH
  the stored `telemetry_sharing_enabled` consent (system_settings, default-off)
  AND the config switch `TELEMETRY_SHARING_ENABLED` (honors `DO_NOT_TRACK`) are
  on. Either off ⇒ nothing leaves the box. Both re-checked in `share_now`.
- **FR-2 — Anonymized aggregates only**: `services/telemetry_sharing_service.py`
  `build_aggregate_payload` — `installation_id` (anonymous), version/edition/
  platform/python, coarse `enterprise_features`, agent + execution **counts**, and
  the Tier-1 activation-funnel counts. **No PII, no content, no prompts, no
  emails, no agent names.** The exact payload is **inspectable before send** via
  `GET /api/settings/telemetry-sharing` → `payload_preview` (the Settings panel).
- **FR-3 — Periodic heartbeat + reversibility**: `TelemetrySharingService` is a
  sleeps-first background loop (default 24h, jittered) that shares when consent is
  on; opt-out stops egress at the next heartbeat. Fail-open (a blocked/failed/
  air-gapped POST never affects the platform). Reuses the operator-intake httpx
  fire-and-forget transport.
- **FR-4 — Retroactive backfill at consent**: on the off→on transition the router
  schedules an immediate fire-and-forget backfill share over a disclosed window
  (`backfill_days`, default 30) sourced from Tier-1 `product_events`, so late
  consent still yields accurate benchmarks. Disclosed at the moment of consent.
- **FR-5 — Consent surfaces**: a value-framed, optional, non-blocking ask in the
  onboarding wizard (`OnboardingWizard.vue`, hidden when hard-disabled) + a
  reversible default-off toggle in Settings → General
  (`components/settings/TelemetrySharingPanel.vue`), each stating exactly what is
  shared. `PUT /api/settings/telemetry-sharing` is admin + human-only, audit-logged.
- **FR-6 — Reciprocity carrot (gated, v1 status surface)**: `GET
  /api/enterprise/telemetry/benchmark` (entitlement-gated) reports whether the
  operator is sharing and that benchmarks are `pending_hosted_service` until the
  hosted service lands; the OSS `ActivationFunnelPanel` renders it. Percentiles
  are computable only for participants, so sharing is structurally the price of
  the comparison.

**Deferred**: the hosted aggregation/benchmark service (separate issue); v2/v3
carrots (targeted alerts, live in-app benchmark panel, roadmap influence).
The warm-ask-after-value prompt shipped with §45.2.

### 45.2 Opt-in Instance Telemetry — prominent ask, share id, outcome mix (trinity-enterprise#437)

**Description**: the second cut of the Tier-2 channel (§45.1): a consent ask
that every install actually reaches, an anonymous share identity that cannot be
joined to the identified operator record, an enforced payload schema with the
sent payloads inspectable afterwards, and an outcome mix that says how autonomous
work ends. Sovereignty is unchanged and re-checked at every gate: **off by
default, nothing leaves the box without consent, no agent content, no prompts, no
credentials, no agent names.**

**Open-core split** (**OSS-core by decision**, ent#437 — consistent with the
§45.1 ruling, recorded here so it is never inferred from the merge): the ask,
the share id, the schema, the send log and the outcome mix are all OSS code with
no entitlement gate; only the reciprocity benchmark view stays gated (`telemetry`).

- **FR-1 — A reachable, prominent, non-nagging ask**: the wizard ask (§45.1
  FR-5) only renders on a zero-agent install, which first-run seeding makes
  permanently false, and #2385 removed the welcome form on every install with a
  pre-provisioned admin. The ask now lives in the post-login **"Finish setup"**
  card on the Dashboard (`components/onboarding/FinishSetupCard.vue`, admin +
  `profileVerified`-gated, one section per open item: the sign-in-email nudge
  from #2381 and usage sharing). **Not now** is a 14-day per-browser snooze;
  **Don't ask again** writes the server marker `telemetry_sharing_dismissed_at`
  (`POST /api/settings/telemetry-sharing/ask/dismiss`, admin + human-only,
  audit-logged); consent writes it too. A **warm re-ask** returns once per
  browser with value-framed copy after the install's first successful autonomous
  execution (schedule/webhook), derived on read and memoised in
  `telemetry_sharing_first_value_at` — no hook in the dispatch path. The card
  reads four booleans from `GET /api/settings/feature-flags`
  (`telemetry_sharing_enabled` / `_hard_disabled` / `_dismissed` /
  `_first_value`) and calls the admin status route only when it will render;
  the payload preview loads lazily on expand (`?preview=0` skips the builder).
  Steady-state cost on a Dashboard load: zero telemetry queries.
- **FR-2 — Share identity is separate from the install identity**:
  `installation_id` (§43.1) travels with the operator's email and company in the
  intake POST, so a share keyed on it is linkable to a person. The aggregate
  carries **`sharing_id`** (UUID4) instead — minted on the off→on consent
  transition with `insert_setting_if_absent` (atomic across workers), unchanged
  while consent stays on, **deleted on revoke**, re-minted on re-consent (revoke
  = forget locally; anything already sent stays with the receiver until a
  deletion signal exists — an ent#190 contract item). `installation_id` is banned
  from the payload by the validator, the id is logged only as an 8-char prefix,
  and `instance.trinity_version` is the release version (never a commit SHA) so
  adoption timing cannot re-join this stream to the presence/intake streams.
  Honest scope: the payload is not traceable **by itself**; unlinkability also
  depends on the receiver keeping the streams apart (ent#190, ent#466).
- **FR-3 — Documented and enforced schema (`schema_version: 2`)**:
  `PAYLOAD_SCHEMA_V2` is a nested allow-list; `validate_payload` raises on any
  unknown key, wrong type, `installation_id`, or non-UUID share id, and
  `share_now` **refuses to send** on a violation (fail-closed egress, ERROR log,
  recorded as a failed send). Vocabularies that reach the wire are
  telemetry-owned enums — trigger buckets map to `chat | mcp | channel | public |
  schedule | loop | reminder | room | operator_queue | agent | voice | other`,
  funnel steps derive from `_FUNNEL_STEPS` — with parity tests, so a new product
  bucket lands in `other` instead of halting telemetry fleet-wide.
- **FR-4 — Outcome mix, install lane, release version**: `outcomes.by_trigger`
  (`{total, success, failed}` per wire bucket, projected from the executions
  timeline reader — cost and context never ship), `outcomes.by_status`
  (terminal rows by status), `outcomes.provider_failures` (`rate_limit` / `auth`
  counts from `subscription_rate_limit_events`, retention-bounded even for an
  all-time backfill), `instance.install_source` (#2380, `unknown` stays
  `unknown`). Labelled **outcome mix**, never "failure taxonomy" — the
  error-class taxonomy is ent#418's.
- **FR-5 — Inspect afterwards**: the last 5 send attempts (success and failure,
  with HTTP status or error class, never `str(e)`) are kept in
  `telemetry_sharing_recent_sends` and rendered in Settings → Usage sharing. A
  404 from the default URL is worded as "the hosted service is not live yet"
  unless `TELEMETRY_SHARING_URL` was overridden — the receiver (ent#190) does
  not exist at the time of writing, so every send fails and the UI says so.
- **FR-6 — Delivery that survives a missing receiver**: the consent-time backfill
  is retried by the 24h heartbeat until the first 2xx
  (`telemetry_sharing_backfill_delivered_at`), then windows are cumulative from
  `last_shared_at`; a Redis tick marker (`telemetry_share:tick`, TTL half the
  interval, never released, fail-open) makes one worker send per interval.
- **FR-7 — Reset paths**: every consent-family key sits under the
  `telemetry_sharing_` prefix the generic `PUT /api/settings/{key}` already
  refuses; the generic `DELETE` stays open for it by design — deleting a key
  only moves toward off / ask again / re-mint. The builder runs off the event
  loop (`asyncio.to_thread`) and every reader is fenced so a stubbed or failing
  source degrades a field, never the payload.

**Deferred**: feature-usage / click-through coverage (PR2, child issue); an
edition-differentiated ask (when ent#190 exists); the taxonomy field (ent#418);
a deletion signal on revoke (ent#190); `main.py` adopting
`utils/app_version.py` (debt inbox `2026-09-03-main-version-resolver-adopt-util`).

---

## Ephemeral "Ghost" Agents (trinity-enterprise#69)

**Description**: A disposable-agent lifecycle — an agent is created with a hard
**budget** (`max_executions` and/or `ttl_seconds`) and is **hard-discarded** when
the budget is exhausted: container removed, DB rows purged via the cascade
primitive, Redis runtime state cleared. Ghosts never enter soft-delete/retention
(no 180-day name reservation) and are volume-less (container writable layer only —
they never recreate, so nothing needs to survive a recreate). Every requirement
below is OSS code; creating an agent *with a budget* additionally requires the
`ephemeral_agents` entitlement (registry read — the registering module is
private). Scoped to **heterogeneous-workspace jobs**
(different repo/config per ghost); same-agent burst parallelism stays with
`fan_out` and, post-pull, replica groups.

- **FR-1 — Budgeted creation**: `POST /api/agents` accepts an optional
  `ephemeral {max_executions?, ttl_seconds?}` block (≥1 required;
  `ephemeral_expires_at` is ALWAYS stamped, defaulting to the TTL ceiling, so no
  ghost is immortal). Ghost names are server-suffixed (`{name}-{rand}`) —
  unique-by-construction. Defaults: `max_parallel_tasks=1`, no credential
  injection (opt-in), git auto-sync off, no avatar seed, no workspace volume.
  Gates, in order: entitlement (403) → ephemeral-caller refusal (an ephemeral
  agent cannot spawn ephemeral agents, 403) → atomic per-owner ephemeral quota
  (Redis INCR-with-cap, 429) → per-parent spawn rate limit (429, agent-scoped
  callers). Labels: `trinity.ephemeral=true`, `trinity.ephemeral-expires-at`,
  `trinity.spawned-by`.
- **FR-2 — Budget enforcement**: admission gate at the TOP of
  `CapacityManager.acquire` (beside the dispatch-breaker gate — nothing is
  enqueued for an exhausted/expired ghost; predicate counts terminal + running +
  queued rows). Terminal-side: an `apply_result` post-CAS-win hook counts ALL
  terminal statuses and background-triggers discard at budget (fail-open, after
  slot release). `/chat` finalizes outside `apply_result` — its exhaustion is
  admission-gated immediately and discard lags to the GC sweep (≤5 min),
  documented. Pull-mode note: the #1081 claim endpoint must re-check the same
  predicate.
- **FR-3 — Hard discard**: `discard_ephemeral_agent(name)` under a per-name Redis
  SETNX lock, crash-convergent ordering: (0) durable intent marker
  (`ephemeral_expires_at = now`) → (1) cancel queued + CAS-fail all non-terminal
  rows (`ghost_discarded`) + close activities → (2) remove container
  (force, NotFound-tolerated) → (3) `clear_agent_runtime_state` (BEFORE purge —
  the name must never free while slots/heartbeat keys survive) → (4) purge via
  `cascade_delete` (executions KEEP; age out via the 90d retention sweep) →
  (5) audit `ephemeral_discard`. `DELETE /api/agents/{name}` routes ephemeral
  agents here (branch BEFORE the container lookup; a half-discarded ghost is
  force-discardable, never 404).
- **FR-4 — GC**: `cleanup_service._sweep_ephemeral_agents` (5-min): DB pass
  (expired/exhausted rows → discard) + Docker-as-truth orphan pass
  (`trinity.ephemeral` containers with no live ownership row, older than a
  ~15-min newborn grace window → removed). Capped per cycle; folds into the
  consolidated lease reaper later (#429).
- **FR-5 — Ghost key containment**: a ghost's key stays `scope="agent"` (a new
  scope value would break heartbeat/report/callback auth, which key off
  `User.agent_name` = scope-"agent"-only); containment is a `(method, path)`
  allowlist enforced at the single auth entry point (the connector-fence
  pattern), keyed off the agent row's `is_ephemeral` — the flag dies with the
  ghost. Allowed: heartbeat, execution result callback, reports, notifications,
  own info; everything else 403. v1 has NO trusted opt-out (a parent needing a
  fully-capable worker creates a durable agent); fail-open on DB read error.
- **FR-6 — Spawn provenance + parent control (Part 2)**: any agent-spawned
  creation (durable or ephemeral) auto-writes the `agent_permissions`
  parent→child edge (`created_by="spawn:{parent}"`) and persists
  `spawned_by_agent` + `spawned_by_key_id` on `agent_ownership` — the parent can
  immediately chat/list/info the child. Agent-scoped callers may
  start/stop/delete ONLY agents whose `spawned_by_agent` AND `spawned_by_key_id`
  match the calling key (interim until #948 capability tokens); sharing,
  permission grants, rename, and credential ops stay human-only (403 for
  agent-scoped callers). Fleet-wide narrowing of agent-key breadth on other
  mutating routes is an accepted-risk follow-up.
- **FR-7 — Fleet hygiene**: ghosts are excluded from the heartbeat watch loop and
  fleet health polling (no stale-alerts for discarded ghosts); operator-queue
  polling keeps them (a ghost may escalate). Execution/cost stats stay inclusive
  (billing truth). Schedule creation on a ghost → 400
  `schedule_on_ephemeral_agent`. `is_ephemeral` surfaced on `GET /api/agents` +
  MCP `list_agents`. Post-discard, KEEP execution rows are admin-only visible
  (owner visibility derives from the purged ownership row) — documented.

**Deferred**: non-LLM command-runner runtime; gVisor/microVM isolation lane;
per-ghost egress control; creation UI (MCP-first); `is_ephemeral` filter on
`/api/executions` if stats skew materializes; durable-agent volume-leak fix
(separate public bug — `volume_remove` has no callers).

---

## 46. Behavioral Evaluation — Referee Surface + Completion Relabel (ent#206)

**Description**: The **referee surface** for agent behavioral evaluation, plus the
honesty fix it exists to enable. `status='success'` is a clean process exit, but was
rendered to users as "Success rate" — as though it meant the answer was correct. This
separates the two axes: `completion` (the run finished without erroring) and `quality`
(the work was good), and gives `quality` a home the graded agent cannot write to.

**The load-bearing rule**: *a score is only trustworthy if the graded agent cannot write
it.* `agent_reports` (§44) was evaluated as the surface and **rejected** — its create is
self-gated by design (an agent publishes its own reports), which is precisely the wrong
boundary for a grade. This surface inverts it.

- **FR-1 — Table `agent_evaluations`**: `id`, `agent_name`, `execution_id` (nullable —
  an evaluation may concern the agent rather than one run), `archetype` (what "good"
  means here, per-archetype rubric), `completion` (nullable mirror of the clean-exit
  axis), `quality` (nullable — **null means "not graded yet", not zero**; the axes are
  independent), `checks_json` (Tier-0 deterministic results), `judge_json` (Tier-1
  output), `evaluator`, `created_at`. Indexes on `(agent_name, created_at DESC)` and
  `(execution_id)`. Dual-track migration (Invariant #3: SQLite `agent_evaluations_table`
  + Alembic `0033_agent_evaluations`); registered in `AGENT_REFS` so rename re-keys and
  purge cascades, and canary L-03's orphan scan covers it.
- **FR-2 — Write fence** (`POST /api/agents/{name}/evaluations`): **human-admin-only** —
  `require_admin` **AND** `reject_agent_principal`. The second gate is the load-bearing
  one: an agent-scoped key resolves to its owner and inherits the owner's role, so on a
  default admin-owned install `require_admin` alone would let a graded agent write its
  own grade (the trinity-ops-agent#232 trap). No agent-writable route exists on this
  surface, and a test asserts none appears later.
- **FR-3 — Read is access-scoped, not fenced**: `GET /api/agents/{name}/evaluations`
  (`AuthorizedAgentByName` — owner/admin/agent-self), `GET /api/evaluations` (fleet,
  `accessible_agent_names`-filtered), `GET /api/evaluations/{id}`. An agent reading that
  it scored badly is the point; read ≠ write.
- **FR-4 — Completion relabel**: Overview (§#1107), the schedules rollup (#1115) and
  fleet stats (EXEC-022) render **"Completion"** with a tooltip ("finished without
  erroring — not answer quality"). **Additive**: the `success_rate` API field is
  unchanged, so existing clients keep working; only the user-facing label moves.
- **FR-5 — Three-layer**: `routers/evaluations.py` → `db/evaluations.py`
  (`EvaluationOperations`) → facade in `database.py` (Invariant #1). Models in
  `models.py` (Invariant #14).

**Open-core**: OSS-core — decided at the strategy gate (trinity-enterprise#206 §10,
merged), not by omission. The enforcement primitive (table + write fence), the Tier-0
deterministic runner, the agent-owned case runner and this relabel are edition-agnostic:
the load-bearing rule must hold in every edition, and deterministic checks make no
external call. The managed grading experience — judge panels, calibration, rubric
management UI — is the paid layer, mirroring §42 (#668) where STATIC is free and the
AI tier is not.

**Not included** (later children of the epic): the Tier-0 evaluator that *populates*
`quality`, the agent-owned case runner, the Tier-1 judge (enterprise), and
replay/shadow evaluation (blocked on #1084 fail-closed + #1408).

See [agent-evaluations.md](../feature-flows/agent-evaluations.md).
