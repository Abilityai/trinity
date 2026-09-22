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
- **Composed by system teardown** (roadmap §16.5.3, trinity-enterprise#454):
  a system teardown is N of *these* soft-deletes, performed through this same
  endpoint rather than a bespoke bulk path — so the window, the name
  reservation, the schedule freeze and the per-agent `agent_lifecycle:delete`
  audit row are all preserved by construction, and per-agent recovery (§33.3)
  is unchanged. Nothing about this section is edition-specific; the teardown
  verb that composes it is entitlement-gated.

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
  submission. The id has exactly three writers — this consent POST, the
  product-event emit (§45), and the canary alert label (a documented, deliberate
  write, #1987) — all through `get_or_create_installation_id`; **every
  read-shaped path** (a GET, a status readback) uses the non-minting twin
  `get_installation_id()` and reports `None` honestly when nothing has minted
  it yet (ent#545), because a `get_or_create_*` on a read path is a durable
  write with a race (learnings 2026-08-05). That writer set is pinned in CI:
  `tests/unit/test_2669_minting_accessor_callers.py` fails the build on any use
  of a minting accessor (`get_or_create_installation_id`,
  `get_or_mint_sharing_id`, `get_instance_label`) outside its reasoned
  allowlist, and on any write of the identity keys outside their home modules
  (#2669; the private tree's twin is trinity-enterprise#575). The last such caller, the
  enterprise activation-funnel read, adopts the twin in trinity-enterprise#570;
  a public tree ahead of that submodule pointer still mints on the tab's first
  open. The mint itself is a **write-once
  claim** (`insert_setting_if_absent`, the #2380 primitive), so two workers
  that SELECT-miss together land ONE id and the loser reads the winner's back —
  the race #1987 recorded as pre-existing in the accessor is closed (ent#545).
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
  derives the first-value events from the OSS tables above. **The read is pure
  (ent#545)**: it reports the stored `installation_id` or `null` through the
  non-minting accessor (§43.1) and never mints one — the first admin open of the
  tab must not create the install's identity — and the panel footer renders an
  honest "no install id yet" state rather than a blank, pointing at the one
  writer an operator can actually reach (the updates opt-in, Settings →
  General; the wizard's first product event only fires on an empty fleet or an
  explicit `?onboarding=1`). A value that is neither an id nor the explicit
  `null` renders as "unavailable", never as a claim about minting. The **panel Vue**
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
  on. Either off ⇒ nothing leaves the box. Both re-checked in `share_now`. The
  same two gates front the **benchmark read** (FR-6, ent#190): the gated view
  asks the hosted service on the operator's behalf, carrying only the share id,
  and that request leaves the box exactly when a heartbeat would.
- **FR-2 — Anonymized aggregates only**: `services/telemetry_sharing_service.py`
  `build_aggregate_payload` — `sharing_id` (the anonymous share identity, §45.2
  FR-2), version/edition/
  platform/python, coarse `enterprise_features`, agent + execution **counts**, and
  the Tier-1 activation-funnel counts. **No PII, no content, no prompts, no
  emails, no agent names.** The exact payload is **inspectable before send** via
  `GET /api/settings/telemetry-sharing` → `payload_preview` (the Settings panel).
- **FR-3 — Periodic heartbeat + reversibility**: `TelemetrySharingService` is a
  sleeps-first background loop that shares on the configured cadence (default
  24h) when consent is on; opt-out stops egress at the next wake. Since #2618 the
  loop wakes every 10 minutes (+ ≤10 min jitter) and decides from the persisted
  `last_shared_at` whether a send is due — empty, unparseable, in the future, or
  older than the interval — so a backend restart never resets the cadence (an
  install that restarted daily used to share its consent-time backfill and never
  again). Fail-open (a blocked/failed/air-gapped POST never affects the platform;
  after five consecutive failures attempts fall to one per half-interval, measured
  from the persisted send log). Reuses the operator-intake httpx fire-and-forget
  transport.
- **FR-4 — Retroactive backfill at consent**: on the off→on transition the router
  schedules an immediate fire-and-forget backfill share over a disclosed window
  (`backfill_days`, default 30) sourced from Tier-1 `product_events`, so late
  consent still yields accurate benchmarks. Disclosed at the moment of consent.
- **FR-5 — Consent surfaces**: a value-framed, optional, non-blocking ask in the
  onboarding wizard (`OnboardingWizard.vue`, hidden when hard-disabled) + a
  reversible default-off toggle in Settings → General
  (`components/settings/TelemetrySharingPanel.vue`), each stating exactly what is
  shared. `PUT /api/settings/telemetry-sharing` is admin + human-only, audit-logged.
- **FR-6 — Reciprocity carrot (gated; wired to the hosted service, ent#190)**:
  `GET /api/enterprise/telemetry/benchmark` (entitlement-gated, admin-only) asks
  the hosted benchmark service for this instance's standing — keyed on the share
  id, never the install id — and answers one of five honest statuses:
  `not_sharing`, `pending`, `not_enough_data`, `ready` (per-metric value,
  percentile and fleet quartiles), `unavailable`, each with a `reason` naming the
  class. The read never mints identity, fails open (a slow or absent receiver is
  a status, never a 500), is bounded, and is memoised briefly. The OSS
  `ActivationFunnelPanel` renders what it is given (`FleetBenchmarkCard`, fetched
  once per view and independently of the funnel). Percentiles exist only for
  participants — the receiver serves them once five instances have shared in its
  45-day window — so sharing is structurally the price of the comparison.

**Deferred**: v2/v3 carrots (targeted alerts, roadmap influence); the
fleet-composition tallies in the benchmark card, once a `ready` answer is
observable in the wild. The hosted service went live on 2026-09-04 (ent#190) and
the warm-ask-after-value prompt shipped with §45.2.

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
  **Amended (ent#581):** the card is gone; the ask is the `sharing` step of the
  first-run overlay (`components/onboarding/steps/StepSharing.vue`), same server
  terms and the same `CONSENT_COPY`. **Not now** is the overlay's per-step Skip
  (an unexpired legacy snooze still reads as skipped). The warm copy renders when
  the step renders after the first autonomous success, and is spent on render;
  it does **not** reopen an overlay the operator already closed — that would be
  the "separate dialog that appears afterwards" ent#581 exists to remove.
  Steady-state cost on a Dashboard load: zero telemetry queries.
- **FR-2 — Share identity is separate from the install identity**:
  `installation_id` (§43.1) travels with the operator's email and company in the
  intake POST, so a share keyed on it is linkable to a person. The aggregate
  carries **`sharing_id`** (UUID4) instead — minted on the off→on consent
  transition with `insert_setting_if_absent` (atomic across workers), unchanged
  while consent stays on, **deleted on revoke**, re-minted on re-consent (revoke
  = forget locally; anything already sent stays with the receiver — by design
  the client sends no deletion on revoke, and the receiver offers an
  operator-initiated forget-me deletion keyed on the share id, ent#190).
  `installation_id` is banned
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
  404 from the default URL is worded as a 404 at the default address (the
  receiver has been live since 2026-09-04, ent#190, so that is an anomaly to look
  at); from an overridden `TELEMETRY_SHARING_URL` as that receiver answering 404.
  Since #2571 each entry records the origin it was posted to (scheme + host +
  port; never path, query or userinfo), the last-shared stamp records the origin
  that acknowledged it (`telemetry_sharing_last_shared_host`), and the receiver
  sentence is decided from that record rather than from the URL configured at
  read time: it names the host that answered and says plainly when the newest
  entry's origin differs from the configured one (an entry without a recorded
  origin reads as unknown and never as a mismatch); `share_url` is scrubbed
  before it reaches the panel.
- **FR-6 — Delivery that survives a missing receiver**: the consent-time backfill
  is retried at every due wake until the first 2xx
  (`telemetry_sharing_backfill_delivered_at`), then windows are cumulative from
  `last_shared_at` in whole days; a Redis tick marker (`telemetry_share:tick`,
  TTL half the interval, a fresh lock per claim, released only when the receiver
  did not acknowledge, fail-open) makes one worker send per interval, and an
  acknowledged send counts as delivered even if the local stamp write fails
  (#2618).
- **FR-7 — Reset paths**: every consent-family key sits under the
  `telemetry_sharing_` prefix the generic `PUT /api/settings/{key}` already
  refuses; the generic `DELETE` stays open for it by design — deleting a key
  only moves toward off / ask again / re-mint, or, for `last_shared_at`, one
  re-share at the next wake (#2618; consent still gates), or, for `last_shared_host`,
  a delivery line that reads "to an unknown receiver" (#2571). The builder runs off the event
  loop (`asyncio.to_thread`) and every reader is fenced so a stubbed or failing
  source degrades a field, never the payload.

**Deferred**: feature-usage / click-through coverage (PR2, child issue); an
edition-differentiated ask (ent#496, unblocked by ent#190); the taxonomy field
(ent#418); a destination change starting a new delivery episode and the
benchmark read using the recorded origin (both deferred from #2571); `main.py`
adopting `utils/app_version.py` (debt inbox
`2026-09-03-main-version-resolver-adopt-util`).

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

---

## 47. Declared Metric Registry (trinity-enterprise#477)

**Epic**: ent#476 (agent-declared metrics). **Siblings**: ent#478 (`record_metrics` + `metric_points`), ent#479 (read + freshness), ent#482 (wizards), ent#483 (validator parity), ent#80 (cross-agent query).

### 47.1 The gap

`template.yaml metrics:` has been a documented block since the Custom Metrics section was written, and it has **never had a backend reader**. The only code that looked at it lived inside the agent container (`agent_server/routers/info.py`), parsing it afresh on every read and validating nothing — which is why compatibility check `D-006` ("`metrics:` has no backend reader") was retired rather than implemented. The consequence: nothing on the platform side knows what an agent claims to measure, so there is no schema to validate a recorded point against and no definition to render a value with.

### 47.2 What ships

A per-agent **metric registry** (`metric_definitions`) built from the declared block by one tolerant reader, reconciled at every point the template can change.

**Declared fields** (frozen, operator ruling 2026-09-21). Documented before: `name`, `type` (`counter` | `gauge` | `percentage` | `status` | `duration` | `bytes`), `label`, `description`, `unit`, `warning_threshold`, `critical_threshold`, and status `values[]` (`{value, color, label}`). Added here:

| Field | Meaning | Grammar |
|---|---|---|
| `cadence` | expected interval between points; ent#479's stale rule is "no point within 2× cadence" | `<n>(s\|m\|h\|d\|w)` or ISO 8601 `PnW` / `P[nD][T[nH][nM][nS]]`, normalized to `cadence_seconds`, `60s ≤ c ≤ 366d`. **Years and months are rejected** — they are not fixed durations, so they cannot normalize to a seconds count a freshness rule can compare against |
| `direction` | which way is good | `up_good` \| `down_good` \| `neutral` (default `neutral`) |
| `aggregation` | how a window collapses | `last` \| `sum` \| `avg` (default `last`) |
| `dimensions[]` | the dimension keys ent#478 will accept on a point | each matching the `name` charset, ≤ 10 |

`x-` prefixed keys pass through untouched and are preserved in `extensions_json` (≤ 20 keys, ≤ 1 KB), so a tool that annotates a declaration survives a round-trip.

**Caps**: 50 metrics per agent, 50 status values, 10 dimensions; `label` ≤ 200, `description` ≤ 1000, `unit` ≤ 32, status `value` ≤ 64. `name` matches `^[a-z][a-z0-9_]{0,63}$` — it is the join key ent#478 stores points under and ent#479 renders, so a case-only variant is a validation error, never a second series.

### 47.3 Reader contract — total, never fatal

`services/template_metrics.py` is a stdlib-only leaf, a third instance of the ent#89 `schedules:` / #1704 `plugins:` idiom: two public functions (`metric_shape_errors`, `normalize_declared_metrics`) over one private `_parse`, so the reported errors and the accepted entries are structurally unable to disagree.

- **Never raises.** A `template.yaml` is untrusted input (bundled, arbitrary `github:` repos, or uploaded `local:`), and the creation path runs inside the destructive rollback fence — one raise there would cost a successful agent creation.
- **An entry with any error is DROPPED** and named. The registry never holds a half-valid definition, because ent#478 validates incoming points against these rows and a row assembled from the half that parsed would accept points its author never declared.
- **Unknown keys are named with a did-you-mean**; a bad type never becomes a boot failure.
- **Errors echo indices, key names, type names and closed-enum values only** — never `name`, `label` or `description`. The list is persisted into `agent_compatibility_results.checks_json` and rendered in the UI (the ent#89 `_safe_echo` rule).

### 47.4 Reconcile — set-diff, template-is-truth

`MetricDefinitionOperations.reconcile(agent_name, declared, source)` is one SELECT, a batched conflict-safe upsert and one retire UPDATE inside a single transaction. Four outcomes per metric: **insert**, **update** (the `definition_hash` moved), **revive** (a retired name declared again), **retire** (an active row the template no longer declares).

Update-in-place is correct here where the ent#89 schedules materializer had to be skip-by-name, because **the template is the only writer** — there is no operator edit surface for a definition, so an update cannot clobber a human choice.

Rows are **never deleted** by reconcile, only retired: points recorded under a name still need a definition to interpret them.

**A `type` change is REFUSED** (ruling T5). `metric_points` are keyed by `(agent_name, name)`, so flipping `status` → `gauge` would leave every prior point uninterpretable. The stored row keeps its type, the declared-but-refused type is recorded in `type_conflict` (cleared the moment the template agrees again), and the refusal is named in the reconcile summary, logged at WARNING, and surfaced by the definitions read. **Changing a metric's type is a new metric name; the old one retires** — an honest series break.

### 47.5 Triggers

| Trigger | Source of the template | `source` |
|---|---|---|
| Agent creation — `github:`, `local:`, snapshot import | the dict the creation resolver already parsed (one read, no second source that can disagree) | `create` |
| `POST …/git/pull` success | live `template.yaml`, read from the running container | `pull` |
| `POST …/git/reset-to-main-preserve-state` success | same | `reset` |
| `POST …/git/sync` success **with `strategy=pull_first`** | same | `sync` |
| Container start | same, fire-and-forget beside the #2069 readiness-gated slot | `start` |
| `POST …/metrics/definitions/refresh` | same | `refresh` |

The start hook and the explicit refresh route exist because of the **dominant** staleness path: an agent edits its own `metrics:` block in-container and the 15-minute auto-sync **pushes** it, so no backend `pull` ever fires and no git hook ever sees the change.

**Every hook is non-fatal**, and the live read never uses the stopped-agent volume path (which spawns a throwaway container — no request-triggered route may create a container as a side effect of a read). Transport is `docker_service.execute_command_in_container` (fixed argv, output capped at 256 KB before the parse), the compatibility collector's own door, and the parse is the backend's hardened loader — the same parse creation used.

**Never retire on absence of evidence** (#2196). A failed exec, an empty read, an oversized file, an unparseable template and a stopped agent are all *unreadable*, and the registry is left exactly as it was. Only a template that **parsed** and genuinely carries no `metrics:` retires its rows.

### 47.6 API

- `GET /api/agents/{name}/metrics/definitions?include_retired=` — `AuthorizedAgentByName`. Returns `{agent_name, declared, definitions[], message, policy}`. `declared` keys on active rows; the empty state names the next action rather than returning a bare `[]`. Retired definitions are served only on request. `type_conflict` is surfaced here — D-009 is a pure static check with no DB read, so this response and the refresh summary are the only two places an author learns a declaration is being refused.
- `POST /api/agents/{name}/metrics/definitions/refresh` — `AuthorizedAgentByName`, running agent only. **409 `agent_not_running`**, **503 `template_unreadable`** (named reasons on `X-Refresh-Unavailable`, never a generic 500). A *use*, not a grant: it re-reads the caller's own accessible agent's file and can reach no other agent, so an agent's own scoped key may refresh its own registry and a shared user who can already `pull` may too. Idempotent by construction, so Invariant #18 does not apply (no execution is created).

No MCP tool ships here (ruling T3): ent#479's `get_metrics` returns definitions + values + staleness in one tool, and a second read tool would be folded or orphaned. The `# mcp:` header on `routers/agent_files.py` names it, so `/validate-architecture` reads the two routes as deliberate.

### 47.7 Compatibility — D-009

**`D-009` "template.yaml `metrics:` entries are well-formed"** — SOFT, STATIC, fail-CLOSED, delegating to `metric_shape_errors`. A finding is exactly an entry the registry refused to hold, because both answers come out of the same `_parse`.

SOFT on the T-018 precedent: a malformed entry is dropped and ent#478 then rejects its points with a named 422, so the author is told twice; HARD would flip a whole agent to incompatible over a mistyped label.

**D-006 is not revived.** Its premise ("`metrics:` has no backend reader") expired with this issue, but a retired id is never reissued — persisted `checks_json` rows would be re-read as a verdict about a different check. D-006 → D-009 is recorded as a mapping in `spec.py` and the spec doc's retired table.

### 47.8 Retention / cap contract — enforced as of ent#478

Both knobs are minted and **enforced** (this section originally recorded the
deliberate absence under ruling T2 — a window with no sweeper and a cap with no
write boundary are controls that change a number nothing reads). The
definitions response publishes the live values with `enforced: true` and a
`source` per knob.

| Name | Default | Bounds | Enforced by |
|---|---|---|---|
| `metrics_retention_days` | `365` | `0-3650`, where `0` = disabled | `cleanup_service._sweep_metric_points`, guarded with `floor=FLOOR_METRIC_POINTS` |
| `metrics_daily_point_cap` | `100000` | `0-10000000`, where `0` = unlimited | the `record_metrics` write boundary (429 `daily_point_cap_exceeded`) |

Both resolve `system_settings` row → environment (`METRICS_RETENTION_DAYS`,
`METRICS_DAILY_POINT_CAP`) → code default. The env tier is **new in ent#478**
and opt-in per key (`config.ENV_BACKED_OPS_KEYS`): making every ops key
env-backed would silently change precedence for ~20 keys that
`GET /api/settings/retention` documents as env-less. Env is a **live**
fallback, not a one-time seed — the #2085 boot seeder skips a key whose
variable is set, so a later change to the variable is still honoured, and a
`PUT /api/settings/ops/config` row still wins over both.

`0` keeps its sibling meaning on the window (disable the sweep, keep forever)
and takes the `ops_cost_limit_daily_usd` meaning on the cap (unlimited), so an
operator can lift the cap without typing a huge number.

`RETENTION_OPS_KEYS` membership requires a `_guard_allows` sweep site
(`test_1771a_retention_edges`) and is mirrored by the private retention
module — `metrics_retention_days` is registered together with its sweeper. The
cap is deliberately NOT a retention key: it is a write budget, not a window.

### 47.9 Legacy `metrics.json` — retired as a source, named as a finding (ent#479)

`GET /api/agents/{name}/metrics` keeps its URL and is **re-backed by the point
store** (§49). The agent-server `metrics.json` read behind it is gone from the
backend: the route no longer contacts a container at all, which is also why it
now answers for a *stopped* agent — the legacy proxy returned "Agent must be
running to read metrics", making every number disappear at the moment an
operator most wanted to know what it had been.

`metrics.json` is not served as a fallback when the store is empty, and that is
deliberate rather than an omission. Two sources for one number is the condition
ent#476 exists to remove, and "serve the file when we have nothing better" is
exactly how the two drift apart unnoticed. The file becomes a **compatibility
finding** instead: **D-010** (SOFT, static) reports `metrics.json is superseded
and no longer served — record these values with `record_metrics``, and its
detail names the file's keys *and separately* the keys with no `template.yaml
metrics:` entry, because "you still write this file" is advice while "these
numbers are declared nowhere" is a fix. The read route echoes the persisted
finding into `findings[]` with `findings_evaluated_at`, so an empty list before
the first compat run reads as *not evaluated* rather than *clean*.

The agent-server `GET /api/metrics` route stays in place with a superseded note
in its docstring — removing it is a base-image change nothing here needs, and
an agent still writing the file is told through D-010 rather than through a
broken endpoint. There is no third write path.

### 47.10 Lifecycle

`metric_definitions` is registered in `AGENT_REFS` with `Policy.CASCADE` on both halves. A purge must not leave definitions addressed to a name that is gone (ent#478 would then accept points against a reused agent name — cross-tenant), and a rename must carry them or the agent's next reconcile mints a second full set under the new name while the old set stays visible.

### 47.11 Known gap

No backend hook covers an agent that runs `git pull` **itself** without restarting. The remedy is the explicit refresh route, reachable by the agent as the `refresh_metric_definitions` MCP tool (ent#478) — which is what the undeclared-metric 422's hint names, so the hint points at something the reader can actually call.

### Acceptance

- [x] `template.yaml metrics:` has a backend reader that is total, bounded, and names every malformed entry
- [x] A per-agent registry persists declared definitions on both DB tracks, with `UNIQUE(agent_name, name)`
- [x] Creation (all three resolver branches), pull, reset, sync-`pull_first`, container start and an explicit refresh all reconcile it
- [x] A removed metric retires; a re-declared one revives the same row; an unreadable template changes nothing
- [x] A `type` change is refused, recorded in `type_conflict`, and surfaced
- [x] `GET`/`POST` definitions routes gate on `AuthorizedAgentByName` with named 409/503 reasons
- [x] `D-009` reports what the registry refused; `D-006` stays retired
- [x] The retention/cap contract is written down with its owner (both knobs minted and enforced in ent#478)

---

## 48. Recorded Metric Points (trinity-enterprise#478)

The push half of declared business metrics: the store, the write path, and the
wire shape everything downstream binds to. The registry (§47) says which
metrics exist; this says what their values are.

**`record_metrics` is the only write path.** Not a second `report` type, not a
file the agent edits, not a `dashboard.yaml` field. A metric value that did not
come through here is not in the series, and nothing downstream has to ask which
of several sources to believe.

### 48.1 The frozen wire shape

One `MetricPoint` shape is used identically on write (this issue) and read
(ent#479), and is what #536's canvas `chart`/`kpi` payloads bind to:

```
{ "metric": "<declared name>",
  "value":  <finite number> | "<declared status label>",
  "ts":     "2026-09-22T08:00:00Z",        // RFC 3339, explicit offset
  "dims":   { "<declared key>": "<label>" } }
```

The series envelope ent#479 returns is `{metric, type, label, unit, points[],
last_point_at}` with `stale` reserved for that issue. It is documented here so
the consumers deferring to "the shape #478 defines" have one to build against;
only the point shape is coded here.

`value` is `number | string` because a `status` metric observes a label. It is
**not coerced**: `true` is not `1` (Pydantic's union was measured to make it
one), `"42"` is not `42`, and `NaN`/`inf` are refused before the dialects can
disagree about them (SQLite stores NaN as NULL, PostgreSQL as NaN).

`dims` values are **strings only**. Allowing `3` and `"3"` and `3.0` would fork
one label into several series that render identically.

### 48.2 Identity, and what a correction is

`idempotency_key = sha256(metric \0 ts \0 canonical_dims)` is the point's
identity and the tail of its primary key `(agent_name, ts, idempotency_key)`.
The same observation posted twice is therefore **one row** with no client key,
no Redis and no execution id.

`value` is deliberately **outside** the identity: one observation of one metric
at one instant with one set of dimensions is one fact, so a re-post with a
different number deduplicates rather than double-counting. **A correction is a
new `ts`** — this is stated in the tool description because it is the one rule
an author can get wrong in a way the platform cannot detect.

The on-disk `dims` need not be byte-identical to the canonical form the hash
was taken over; the canonical form exists so the identity is stable across
clients, and the column's serialisation belongs to the driver.

### 48.3 Batches, idempotency and honesty

A batch is 1..1000 points and ≤ 2 MiB encoded, **all-or-nothing**: a caller
never has to reconcile a partial write against what it meant to send. The 201
returns `recorded`, `deduplicated` and `replayed` as **separate** counts, plus
each accepted point's assigned `{index, ts, idempotency_key}` — an "we already
had this" must not read as a write that happened.

Two idempotency layers, for two different failures:

* the **row** key above, which needs nothing and holds always;
* a **batch** key (`Idempotency-Key` header or body field) via
  `idempotency_service`, which replays the first result. Where no client key is
  given but `execution_id` resolves to the calling agent, the batch key is
  derived from that execution — which is what dedups a batch of `ts`-less
  points on a re-delivered turn, since those take a fresh server-now timestamp
  and would otherwise hash to something new.

**The batch key identifies a BATCH, not a caller.** Both branches bind the
canonical points payload into the claim (`record_metrics:{client_key}:{
sha256(points)}` for a client key; `derive_effect_key` for the execution-derived
one), because `idempotency_keys` stores no request fingerprint. Without that
binding an agent stamping a constant `idempotency_key` on every turn — the
realistic LLM failure — would have every later batch answered with the first
one's snapshot for 24 hours: `replayed: true`, no 4xx, nothing written. A retry
of the *same* batch still replays; a *different* batch under a reused key is a
fresh claim and is recorded.

With **neither** a key nor an `execution_id`, a `ts`-less retry is a new
observation. That is stated rather than papered over with a body hash: the same
numbers an hour later are usually a genuine new observation, and treating them
as a duplicate would silently drop real data.

### 48.4 Reason codes (frozen; ent#483 validates parity against this list)

Batch level: `batch_empty` · `batch_too_large` · `payload_too_large` (413) ·
`rate_limited` (429) · `daily_point_cap_exceeded` (429) ·
`idempotency_in_flight` (409) · `metric_store_unavailable` (503) ·
`metric_store_rejected_batch` (500, non-retryable).

Per point (`errors[i].code`): `metric_name_invalid` · `metric_undeclared`
(+ hint naming `refresh_metric_definitions`) · `metric_retired` ·
`type_mismatch` (+ hint when a type change was refused) ·
`status_value_undeclared` · `value_invalid` · `value_too_long` ·
`dimension_undeclared` · `dimensions_too_many` · `dimension_value_invalid` ·
`ts_invalid` · `ts_out_of_range` · `ts_in_future` · `ts_before_retention` ·
`duplicate_in_batch`.

`value_too_long` and `dimensions_too_many` each exist because the nearest
alternative sends the caller somewhere that cannot help: an over-long text
value is not a type problem, and eleven *declared* dimensions is not an
undeclared one. A text `value` is bounded at `METRIC_VALUE_TEXT_MAX_LEN`
(1024) — the 2 MiB figure is a **batch** bound that a single field could
otherwise spend on its own.

Messages carry codes, indices, closed-enum names and dimension **keys** only —
never a value and never a dimension value, which may be a customer name or an
email. This output lands in an LLM's context and in logs.

### 48.5 Timestamps

`ts` must match the RFC 3339 shape with an explicit offset. `fromisoformat` is
not the gate: it accepts week dates, bare dates, and year `0999` — which
normalises to `999-01-01T…` and then sorts as the newest row **forever** under
the lexicographic ISO index this table is read by. Bounds are
`[2000-01-01T00:00:00Z, now + 300 s]`, compared as datetimes rather than
strings for the same three-digit-year reason.

When the retention window is non-zero, a point older than it is refused
(`ts_before_retention`) rather than accepted and deleted minutes later — and,
more importantly, so agent input cannot steer the retention guard into
permanent refusal by dropping a year of expired points into the table.

### 48.6 The daily write cap

`metrics_daily_point_cap` counts `created_at`, not `ts`: it is a **write**
budget, so backfilling last year's points still spends today's. Crossing it is
a 429 with `Retry-After` to the next UTC midnight — the input is not wrong, so
it is not a 422. The count is `LIMIT`-bounded (the question is "does this batch
cross", not "how many did today hold"), which means two concurrent batches can
each pass and overshoot by at most one batch. That is accepted and documented
rather than serialised.

One audit row per (agent, UTC day) on the **first** refusal — a quota event is
the security-relevant signal. No row per accepted batch: `audit_log` is
append-only and undeletable for a year, so that would be up to 86 000
permanent rows per agent per day at the cap.

### 48.7 Storage, retention and lifecycle

`metric_points` has no surrogate id — the identity IS the primary key, which
keeps the partition key inside the only unique constraint so ent#80 can
partition by month without a table rebuild. `dims` is JSONB on PostgreSQL and
TEXT on SQLite through a `/* pg:JSONB */` marker in the shared DDL, so the
fresh-PG, upgraded-PG and SQLite paths converge with no `ALTER … USING`.
`value_numeric` is `DOUBLE PRECISION`, because `REAL` is float4 on PostgreSQL
and would round a revenue metric's cents.

The retention sweep uses `floor=FLOOR_METRIC_POINTS` (100 000, one agent-day at
the default cap) rather than the sibling default of 1000: at any real ingest
rate more than a thousand rows fall out of a 365-day window every five-minute
cycle, so the default floor would refuse every cycle and then sit blocked
behind single-use acknowledgements. The prune is bounded per call, and the
acknowledgement is consumed only once the remaining backlog is under the floor.

`execution_id` is **provenance only** — no foreign key, and
`execution_row_retention_days` (90) is shorter than the point window (365), so
ent#479 must never join on it.

`AGENT_REFS` carries `metric_points` as CASCADE. The identity hash excludes
`agent_name`, so a rename re-keys losslessly.

### 48.8 What is deliberately absent

No WebSocket broadcast on write (Invariant #10 wants a thin trigger plus a
refetch route, and that route is ent#479's); no partial-accept mode; no
per-point caller-supplied key; no refresh-on-miss inside the write path — the
remedy is the `refresh_metric_definitions` tool the 422's hint names.

### Acceptance

- [x] `record_metrics` records validated points and is the only write path
- [x] A batch is all-or-nothing with a named reason code per rejected point
- [x] The same observation posted twice is one row, with or without a key
- [x] A re-delivered turn replays rather than recording twice
- [x] The daily cap refuses with 429 + `Retry-After` and audits once a day
- [x] A store outage is retryable and never fails the agent's turn; a rejected
      batch is explicitly not retryable
- [x] Both knobs are Settings-surfaced with an env bootstrap, and the sweep
      enforces the window
- [x] Purge and rename cascade

---

## 49. Reading Declared Metrics — the one read, and one staleness rule (trinity-enterprise#479)

§47 says which numbers an agent may have and §48 records them. This section is
how anybody — an operator, the agent itself, a dashboard widget, a future
objective join — **reads** one back, and how the platform decides whether what
it is showing is still current.

The binding constraint is singularity. ent#476 exists because the same business
number was reachable through several paths that could disagree; adding a second
read, or a second staleness rule, reintroduces the defect this epic removes. So:
**one route**, **one stale rule**, and every consumer imports the same
function.

### 49.1 The one stale rule

```
stale  ⟺  cadence_seconds is declared  AND  now − last_point_at > 2 × cadence_seconds
```

`services/metric_read_service.freshness(cadence_seconds, last_point_at, now)` is
the platform's single definition. It is **pure** (no DB, no clock of its own —
`now` is injected), exported, and imported by the read route, the health block,
the dashboard binding and the role card. A second implementation anywhere is a
defect regardless of whether it currently agrees.

It returns `{stale, freshness, stale_after}` over exactly four outcomes:

| `freshness` | `stale` | When |
|---|---|---|
| `fresh` | `false` | a point arrived within 2× cadence |
| `stale` | `true` | no point within 2× cadence; `stale_after` = `last_point_at + 2 × cadence` |
| `no_cadence` | `null` | the declaration carries no `cadence:` — **never stale**, because there is nothing to be late against. `null`, not `false`: "not stale" and "unanswerable" are different claims |
| `no_points` | `false` | declared, nothing recorded yet — an agent that has never reported is not *late*, it has not started |

Two boundaries are pinned by test because they are the ones a reader guesses
wrong: exactly `2 × cadence` is **not** stale (strict `>`, per the issue's AC),
and a point in the **future** (the write path tolerates ≤ 300 s of clock skew)
clamps its age to zero and reads fresh rather than going negative.

`cadence_seconds` on the definition row is the only cadence this rule reads.
`cadence` (the author's string) is never re-parsed at read time — two parsers is
two rules.

### 49.2 The read: `GET /api/agents/{name}/metrics`

Same URL as the `metrics.json` proxy it replaces (§47.9), re-backed by the point
store. **Store-only: no container is contacted**, so a stopped agent answers
exactly like a running one.

**Gate order** (Invariant #8): `AuthorizedAgentByName` decides access first —
uniform 404 for both an absent and an inaccessible agent — then the agent
self-gate, `current_user.agent_name and != name → 403`, the same spelling the
write path uses so read and write agree on who "itself" is. Cross-agent reads
are ent#80's grant, not an oversight here. Rate-limited at 240/min per agent
(`rate_limiter.enforce`, the write path's spelling): enough for N open tabs at a
30 s poll, not enough for a runaway loop.

**Query**: `window ∈ {auto, 24h, 7d, 30d, 90d}` plus optional `since`/`until`
ISO bounds; `metric=` (one declared name); `include_retired`; `series_limit`
(≤ 2000, single-metric path only). `auto` is `max(24h, 12 × cadence)` capped at
90 d, because a cadence can be anything from 60 s to a year and a fixed 24-hour
window shows a weekly metric at most four points. Named 422s, never a generic
500: `window_invalid`, `metric_undeclared`. A store outage is **503
`metric_store_unavailable` + `Retry-After: 30`**.

**Response**, one object per declared metric, carrying its definition, its
latest value, its freshness and its series:

```
{agent_name, declared, window: {kind, since, until}, generated_at,
 metrics: [{ …definition fields…,
             latest: {value, ts, dims} | null,
             latest_by_series: [{dims, value, ts, stale, freshness}],
             last_point_at, stale, freshness, stale_after, series_count,
             series: [{dims, buckets: [{i, ts, value}], points?, truncated}],
             chart: {basis, aggregation, series_count, dims, buckets} | null,
             stats, message }],
 findings: [{code, message, detail}], findings_evaluated_at, policy,
 stale_rule, message}
```

Four shape decisions are load-bearing:

- **A bucket is the WINDOW, not the newest N.** The store hands the composer
  the newest 200 points per metric, which for any metric with history older
  than the window is a superset of it. Points outside `[since, until]` are
  **dropped** before bucket indexing — never clamped into bucket 0, which is
  what fabricated an opening spike (the sum of points the window excludes,
  stamped before `since`) and then computed `stats` from it. Each bucket
  carries its index `i`, which is what makes "the same moment in two dimension
  series" well defined.
- **Bucketed by default, raw only on request.** 50 metrics × 2 000 raw points is
  a ten-megabyte "read". The all-metrics path ships ≤ 120 buckets per series;
  raw `points` appear only on the single-metric (`metric=`) path, capped at
  `series_limit`, and truncation keeps the **newest** — a series that dropped
  today's points would be worse than no series.
- **Dimensioned metrics keep their identity.** Points are grouped by
  `canonical_dims` (the write path's own spelling), the tile value is the folded
  aggregate per the declared `aggregation` (`last`/`sum`/`avg`), and
  `latest_by_series[]` (≤ 50) carries each series with its own freshness. A
  region that stopped reporting keeps contributing to a `sum` **and** is
  visibly flagged, rather than silently dropping out of the total.
- **`has_metrics` is gone.** One spelling of "this agent declares metrics", and
  it is `declared`.
- **`chart` is the one bucket list the sparkline and the trend arrow share**,
  so they cannot describe a different thing from the number above them.
  `latest.value` is the fold across every dimension series, so for `sum` / `avg`
  the chart is that same fold across series **by bucket index**
  (`basis: "folded"`). A cross-series fold is undefined for `last` — the last
  value of two regions is not one number — so there the chart is the most
  recently updated series and says so (`basis: "series"` + its `dims`), which
  the tile renders as a chip rather than leaving a total's trend arrow drawn
  from one region's history.

**Empty states name the next action** rather than returning a bare list. Zero
declarations → "no metrics: block in template.yaml — declare one and pull,
restart the agent, or POST .../metrics/definitions/refresh". A declared metric
with no points → "declared, no points yet — record points with `record_metrics`
(or schedule `/update-dashboard` if the agent has that playbook)". The copy
names **both** actions rather than branching on whether the agent holds the
playbook: this read is store-only, the playbook catalog is a container probe
(`GET /api/agents/{name}/playbooks`, 503 on a stopped agent), and the persisted
`agent_skills` rows know only *library* assignments while every bundled template
carries `/update-dashboard` in `.claude/commands/` — a conditional built on that
table would tell exactly those agents they lack the playbook they ship with. A
branch no caller could compute left the `/update-dashboard` half unreachable
dead copy, which is worse than naming one action too many.

**`metric=` naming a retired definition is 422 `metric_undeclared` with "retired
at T — pass `include_retired=true`"**, not a 200 with the retired row: a retired
metric silently reading as current for a consumer that never asked is the
failure worth preventing.

### 49.3 MCP `get_metrics(metric?, window?, since?, until?)`

Agent-scoped, resolved through the existing `getAgentName`, self-gated by the
backend. Returns the route body verbatim — the shape IS the contract — and
never throws: a 422 `metric_undeclared` maps to
`{success: false, undeclared: true, hint: "declare it in template.yaml metrics:
and call refresh_metric_definitions"}`. The tool description states the stale
rule, that the default read is bucketed, and how to get raw points, because an
agent that has to guess will guess a second rule. A user-scoped key reads
metrics through the REST route or the health block, not this tool
(`access.ts` records that as `kind: "none"` with the reason).

### 49.4 Freshness in `get_agent_health` — informational, never a verdict

`AgentHealthDetail.metrics` carries
`{declared, with_points, stale: [], no_cadence: [], no_points: [],
retired_with_points: [], last_point_at, rule: "2x cadence"} | null`.

It **never** touches `aggregate_status` or `issues`. A business metric going
stale is a fact about the agent's *work*, not about the agent's *health*, and
folding it into the health verdict would make "agent unhealthy" mean two
unrelated things. A store read failure yields `metrics: null` — the health check
never fails because the metric store did. Attached at request time in the
router, not inside `perform_health_check`, so the scheduled fleet loop does not
grow a per-agent store read per cycle.

### 49.5 Declared metrics are the default agent dashboard

An agent that declares metrics gets tiles with **no `dashboard.yaml` at all**
(ent#439's sensible default).

- `GET /api/agent-dashboard/{name}/exists` answers **two** flags —
  `{has_dashboard, has_declared_metrics}` — in one DB-only request, and the
  Dashboard tab appears for **either**. It is gated with the uniform-404
  dependency, which it was not before: a bare `get_current_user` made it a
  fleet-wide existence oracle for any logged-in principal.
- `DeclaredMetricsTiles.vue` mounts as a **sibling** of `DashboardPanel`, not an
  arm inside it. The panel's state machine terminates in "Agent Not Running" and
  "No Dashboard Defined"; the tiles are store-backed and must render in exactly
  those states.
- Every tile states **when** its point was recorded (relative on the face,
  absolute on hover). A stale tile keeps its last known value with a warning
  chip naming the cadence it was late against — **marked stale, never rendered
  as current**. A metric with no declared cadence gets a neutral "No cadence
  declared" chip.
- The tiles never recompute staleness. They render `stale` / `freshness` as the
  route decided them (§49.1).
- Refresh is ent#253: loading means "no data yet", a background poll swaps values
  in place with no scroll, selection or DOM reset, and a **failed** refresh keeps
  the numbers on screen under a stale banner rather than replacing them with
  "no points yet" — which would be a claim about the agent produced by a failed
  request.

### 49.6 `dashboard.yaml` widgets may bind a declared metric

A `metric` / `status` / `progress` widget carrying `metric: <name>` is filled
from the registry on every read: `value`, `color` (from the declared status
`values[].color`, or from thresholds for a numeric type), `history`,
`last_point_at`, `stale`, `freshness`, `bound: true`.

- Order is **cache → snapshot → bind → history-enrich**, on both the live and
  the cached path, and the snapshot writer and the history enrichment both
  **skip** bound widgets through one shared `is_bound` predicate. A bound number
  in `agent_dashboard_values` would be a second source for a value the registry
  owns, and the two would disagree the moment the poll and the recording cadence
  drift apart.
- An undeclared name yields `binding_error` and **no value** — a wrong number is
  worse than no number — and the panel renders the reason, never a bare dash.
  A **retired** metric is refused the same way (`binding_error_code:
  metric_retired`, with `retired_at`): TD-10 refuses `metric=<retired>` on the
  route so a retired metric never silently reads as current, and a widget is
  that same read with nobody there to pass `include_retired`. Each refusal
  carries a machine `binding_error_code` beside its sentence
  (`metric_store_unavailable` / `metric_undeclared` / `metric_retired`), the
  route's `{reason, message}` pair spelled for a widget.
- A bound widget's `history` is built from `chart`, the same fold its `value`
  comes from, so its sparkline and trend arrow describe the metric the number
  names.
- A store outage degrades **per widget**; a dashboard is never 5xx'd because one
  widget named a metric.
- The agent-server `validate_widget` no longer requires `value` (or `color` on a
  status widget) when `metric:` is set, so an author stops having to invent a
  number. **Older base images keep the strict rule**, so an author targeting one
  keeps a placeholder `value:` in the file; the backend **overwrites** it when
  the binding resolves, so the placeholder is never what an operator sees.
- **Unbound widgets are untouched.** Snapshotting an author-written `value:` is
  the deprecated path, documented as such: it remains supported and is not the
  way to publish a business number.

### 49.7 What is deliberately absent

The objective ↔ metric join (ent#666). Cross-agent and fleet reads (ent#80,
ent#94) — the self-gate above is the boundary they will lift, deliberately, with
a grant. A WebSocket `metrics_updated` trigger: §48.8 deferred it for want of a
refetch route, that route now exists, and it is ent#538's to add with
coalescing, because a per-batch broadcast at the write cap is a storm. Deleting
the agent-server `/api/metrics` route from the base image — it is **retired in
place** instead: the route still exists but reads neither `template.yaml` nor
`metrics.json`, answering `410` with
`{has_metrics: false, superseded_by, finding: "D-010", message}`. Keeping it
serving the file would have left the agent half of a deleted backend read alive
(Invariant #5) and made `metrics.json` a second source of truth for a number the
registry owns. `series_limit` and the bucket count as Settings rows — they are
read bounds on one query, not operator policy.

### Acceptance

- [x] One stale rule, pure and exported, imported by every consumer; `2 ×
      cadence` strict, no cadence → never stale, future point clamps to fresh
- [x] `GET /api/agents/{name}/metrics` keeps its URL, is backed by the point
      store, and answers for a **stopped** agent
- [x] Uniform 404 then the agent self-gate; named 422s; 503 + `Retry-After` on a
      store outage; 240/min per agent
- [x] Bucketed by default, raw only with `metric=`, truncation keeps the newest
- [x] Dimensioned metrics fold by the declared `aggregation` and keep
      per-series freshness
- [x] Every empty state names the next action the agent can actually take
- [x] Buckets cover the window: an out-of-window point is dropped, not folded
      into bucket 0, and `stats` is computed from the chart it labels
- [x] `metrics.json` is retired as a source and named by D-010, echoed on the
      read with `findings_evaluated_at`
- [x] MCP `get_metrics` is agent-scoped, never throws, and maps `undeclared`
- [x] `get_agent_health` carries the freshness block and never changes
      `aggregate_status` or `issues`
- [x] Declared metrics render as tiles with no `dashboard.yaml`, for a stopped
      agent, with the point time and stale mark on every tile
- [x] `/exists` answers both flags and is gated with the uniform-404 dependency
- [x] A bound widget is filled from the registry and skipped by the snapshot
      writer; an undeclared binding shows the reason and no number

---

## 50. Objective ↔ Metric Join — one read of target vs actual with freshness (trinity-enterprise#666)

§47 says which numbers an agent may have, §48 records them and §49 reads them
back. This section is how the platform answers the question those three leave
open: **what is this agent supposed to move, where is it now, and is that
number still true?**

The binding constraint is the same singularity that drives the whole epic. An
objective's target and a metric's actual were reachable through paths that
could disagree — the role card (ent#527) computed its own gap from
`metrics.json` with its own 30-day staleness rule while the tiles read the
point store with the §49.1 rule. So there is **one join**, in
`services/objective_join_service.py`, and every consumer — the role card, the
project hub (ent#661), proactivity (ent#605) — calls it rather than growing
its own. A second join anywhere is a defect regardless of whether it currently
agrees.

### 50.1 The grammar it reads (Tandem framework §3.4)

```yaml
# <x-canon.clone_path>/objectives/<id>.yaml
schema_version: 1
id: q4-close-rate
statement: Lift close rate to 35% by the end of Q4.
horizon: day | week | month | quarter | year
owner: role:revenue-lead
supporting_agents: [sales-companion]
metrics:
  - name: close_rate          # the §47 registry's metrics[].name — by NAME
    direction: up | down | hold
    target: 35
    by: 2026-12-31
    tolerance: 1              # optional; `hold` only
status: active | achieved | dropped
review_by: 2026-10-15
```

**Reader tolerance** (the §47 rule): unknown keys are accepted and never fatal,
so a field a later `schema_version` adds does not take the objective down with
it. `schema_version` itself is read but not enforced.

**Only `status: active` objectives are joined.** A file with no `status` reads
as active (the oldest canon files predate the field); a value outside the enum
reads as *not* active and is excluded — an unrecognised state is not a licence
to keep nagging. A non-active objective produces no findings: it is not a
defect, it is finished.

**Findings belong to the objectives the read RETURNS.** The same suppression
covers another role's objectives: in a shared fleet canon every agent reads
every role's files, so publishing their parse defects would put every other
role's YAML mistakes on this agent's card (and #2927 copies `findings` onto the
card verbatim). Each objective's findings are held beside it while it is
parsed and published only if the concern filter keeps it — the truncated tail
beyond `MAX_OBJECTIVES` included, since those are not shown either. **The two
file-level codes are the deliberate exception**: `objective_invalid` and
`objective_unreadable` are always published, because a file that did not parse
carries nothing that says whose concern it is, and silence there is how a file
disappears from a read that claims to show them all.

An objective reaches an agent two ways: `owner: role:<id>` matching the
template's `x-role.role` (**owned**), or the agent's own name in
`supporting_agents` (**supporting**). An agent with no `x-role` can still
support.

### 50.2 The four sources, and which one owns what

| Fact | Source | Why that one |
|---|---|---|
| metric name, `target`, `by`, `tolerance`, `direction` (fallback) | the objective file | files are truth (framework E7/E13); the platform keeps no copy |
| `direction` (first), `unit`, `type`, `label`, `status`/`retired_at` | the §47 registry | the platform's own declaration of what the name means |
| `actual`, `last_point_at` | the §48 point store, via `metric_read_service.latest_by_metric` | the same folded value the tile shows, by construction |
| `stale`, `freshness`, `stale_after` | `metric_read_service.freshness` (§49.1) | the ONE stale rule; a second one is a defect |

`actual` is the **tile's folded latest**, not a window aggregate. Both the read
route and the join go through `_latest_entry`, so "the objective's actual" and
"the number on the tile" are one computation rather than two that happen to
agree — pinned by a parity test including a dimensioned `sum` metric, where a
re-implemented fold would plausibly diverge. A `counter` is monotonic by the
guide's own definition, so "quarter-to-date" is the author's push contract, not
something this read invents.

**Two doors to one file, deliberately.** The registry reads `template.yaml` via
`docker exec cat` at its six triggers; the join reads it through the agent door
(`AgentClient`). Both use the same hardened loader (`utils/safe_yaml`), and the
join accepts a pre-parsed `template` so the role card does not read it twice.

### 50.3 `gap` — position, never pace

```
up_good    → behind when actual < target, ahead when actual > target
down_good  → the mirror
neutral    → the HOLD arm (a declared `hold`, see 50.4):
             on_target when |actual − target| <= tolerance (default: exact),
             off_target otherwise — NEVER behind/ahead
```

`gap.status ∈ {behind, on_target, ahead, off_target, not_computable}` and
`delta = actual − target`, signed, numeric only.

**`behind` is a position word here, not a pace word.** It says the number is on
the wrong side of the target right now; it says nothing about whether the agent
is late against `by`. `by` and `horizon` ride every metric row precisely so a
consumer that wants a pace judgment can compute one — ent#605 owns the ramp
maths and will add its own field. Nothing in this read computes pace.

`hold` is its own arm because a value that should be held has **no good side to
be on**: drifting up is as wrong as drifting down, so `off_target` with the
signed delta is the honest answer and `ahead` would be a lie.

`not_computable` always names its reason: `no_target`, `non_numeric`,
`no_points`, `no_direction`, `undeclared`, `retired`, `declared_elsewhere`.

**Stale is orthogonal to gap.** A stale metric still has its gap computed, with
`stale: true` beside it — the card renders "30 / 35 · stale" and ent#605
refuses to act on it. Collapsing a stale metric to `not_computable` would hide
the number the card exists to show. The join reports; the consumer decides.

### 50.4 Direction: the registry first, the objective as the fallback

`resolve_direction(registry_direction, objective_direction)` returns
`(direction, direction_source, mismatch)`:

| registry | objective | direction | `direction_source` |
|---|---|---|---|
| `up_good` / `down_good` | anything | the registry's | `registry` |
| `neutral` / absent | `up` | `up_good` | `objective` |
| `neutral` / absent | `down` | `down_good` | `objective` |
| `neutral` / absent | `hold` | `neutral` | `objective` |
| `neutral` / absent | absent | `null` | `none` |

**`direction` ranges over the REGISTRY's three values and nothing else** —
`up_good`, `down_good`, `neutral`, or `null` when nobody said. A declared
`hold` resolves to `neutral` rather than to a self-describing fourth token, so
a consumer that reuses the registry-direction formatter (`utils/metricFormat.js`
is direction-aware) cannot meet a value it has never heard of. The author's own
word is kept verbatim beside it as `objective_direction`, and the *comparison* a
declared hold selects lives in `gap` (50.3), not in a fourth direction.

`neutral` is the registry column's **default**, indistinguishable from a
template that never said — and no bundled template declares `direction:` — so
a registry-only rule would have shipped a feature where every gap is
`not_computable` on day one. Hence the fallback.

The objective file's `hold` is the registry's `neutral` **declared on purpose**;
`direction_source` is what tells a declared `hold` apart from silence, which is
why that field exists — and why the wire needs no fourth direction value to
carry the distinction. Two *declared* directions that disagree (registry
`up_good` vs objective `down`, or vs `hold`) resolve to the registry's and
raise a `direction_mismatch` finding — one of the two files is wrong and the
read says so rather than silently picking. Neither declaring one leaves
`direction: null`, `gap.reason: no_direction` and a `direction_undeclared`
finding naming the one-line fix.

### 50.5 Findings — never a blank

Every failure is a named finding carrying a sentence a person can act on. The
one thing this read will not do is render an empty cell where a number was
expected: that is how "we are measuring it" survives having stopped measuring
it. Findings appear twice — flat in `findings[]` with `objective_id` / `metric`
/ `path`, and as `finding: {code, message}` on the metric row a card renders.

| Code | When | The fix it names |
|---|---|---|
| `metric_undeclared` | an **owned** objective names a metric this agent does not declare | declare it in `template.yaml metrics:`, call `refresh_metric_definitions` |
| `metric_not_declared_here` | a **supporting-only** objective names a metric this agent does not declare | *nothing* — the owning role's agent declares it; cross-agent metric reads are ent#80. Counted under `summary.declared_elsewhere`, **not** `undeclared` |
| `metric_retired` | the name is declared but retired | re-declare and refresh; the last value is **withheld**, because a retired number rendering as current is the §49.2 failure |
| `metric_name_invalid` | a `metrics:` entry is not a mapping, or its name is not a valid id | fix the objective file |
| `metric_duplicate` | one objective lists a name twice | first entry wins; drop the rest |
| `direction_mismatch` | registry and objective both declare a direction and disagree | registry wins; fix whichever file is wrong |
| `direction_undeclared` | neither declares one | add `direction:` to the template metric or the objective entry |
| `objective_invalid` | the file is not a YAML mapping | fix the YAML; §3.4 names the fields |
| `objective_unreadable` | the agent answered, but not with that file (retryable — a transport fault is not an author error) | retry |
| `objective_id_duplicate` | two files declare one id | both are shown; give one its own id |
| `objective_id_invalid` | a file's `id:` is not a valid id | the **file name** is used instead and the finding says so — a *missing* `id` falls back silently, an id the author wrote and this read refused does not, because ent#661 keys objectives by id across agents |
| `objective_file_skipped` | a `*.yaml` in `objectives/` whose NAME is not a plain path segment (a space, a non-ASCII character) | rename it; the file is never fetched, and `source.objectives_skipped` counts them so "not there" can be told from "there under a name this read will not open" |
| `objectives_read_timeout` | the fan-out exceeded `OBJECTIVES_READ_BUDGET_SEC` | retry; the agent is answering, just too slowly — `source.objectives_dir: "timeout"`, no objective joined |
| `role_id_invalid` | `x-role.role` is not a valid id | fix `template.yaml`; no owned objective can match until then |
| `canon_path_invalid` | `x-canon.clone_path` is not a plain path | fix it; **no file is read with that path** |

Every code in this table above the file-level pair is published **only for the
objectives the read returns** (50.1): another role's parse defect, and a
finished objective's, are not this agent's to fix. `objective_invalid`,
`objective_unreadable`, `objective_file_skipped`, `objectives_read_timeout`,
`role_id_invalid` and `canon_path_invalid` are file- or read-level and are
always published — there is no objective there to decide whose they are.

### 50.6 The read: `GET /api/agents/{name}/objectives`

**Not store-only**, unlike §49's read. Objectives live in the agent's own
container and nothing is copied platform-side (E7/E13), so this route contacts
the agent door: one container-state read, one `template.yaml` read, one
directory listing, and up to 100 small file reads.

Gate order (Invariant #8), copied verbatim from `/metrics`:

1. `AuthorizedAgentByName` — the uniform 404 for an absent **or** inaccessible
   agent (owner / shared / admin).
2. The agent self-gate: `current_user.agent_name and != name → 403`. An
   agent-scoped key reads only its own objectives; cross-agent reads are
   ent#80's grant, not an oversight here. **403, not 404** — the caller already
   knows the agent exists, because the dependency let it through.
3. `rate_limiter.enforce("agent_objectives_read:{name}", …)` on the name the
   gate has already **validated** — no limiter-key amplification from an
   unvalidated path param.

`OBJECTIVES_READ_RATE_LIMIT` (env, default **60**/min per agent, window 60 s)
is its **own** knob, not `/metrics`'s 240. That route is store-only; this one
drives a container. Ten open role cards polling at 30 s is 20/min, so 60 clears
normal traffic with room and still stops a loop from pinning an agent-server the
platform also needs for chat.

A store outage is `503 metric_store_unavailable` + `Retry-After: 30`.
**Everything below transport is a named field on a 200** — an agent that is
stopped is an *answer*, not an error.

### 50.7 The response

```
{agent_name, generated_at, stale_rule: "2x cadence",
 role: {id, path} | null,          # from x-role; null → objectives can only be supporting
 canon_root: "canon" | null,
 unavailable: null | agent_stopped | agent_missing | agent_unreachable,
 source: {template, objectives_dir, objectives_listed, objectives_scanned,
          objectives_unscanned, objectives_skipped, objectives_truncated},
 objectives: [{id, path, schema_version, statement, horizon, status, owner,
               review_by, owned, supporting, metrics_truncated,
               metrics: [{name, target, target_text, tolerance, by, horizon,
                          objective_direction, declared, declared_elsewhere,
                          direction, direction_source, unit, type, label,
                          actual, last_point_at, stale, freshness, stale_after,
                          gap: {status, delta, reason},
                          finding: {code, message} | null}]}],
 findings: [{code, objective_id, metric, path, message}],
 summary: {objectives, metrics, behind, ahead, on_target, off_target,
           not_computable, stale, undeclared, declared_elsewhere},
 message: str | null}
```

**The model is the contract.** `models.ObjectiveJoinRead` is the route's
`response_model` and the service returns its dict unchanged; a key-parity test
walks the service's output against the model's fields recursively, so an
additive service key fails the build rather than being silently filtered out of
every response.

`unavailable` distinguishes `agent_stopped` (start it), `agent_missing` (no
container — recreate it) and `agent_unreachable` (the door did not answer),
because those are three different actions.

`source.*` exists so "no objectives" can be told from "not read":
`objectives_listed` / `objectives_scanned` / `objectives_unscanned` are
separate counts because the **filter runs after the read** (see 50.8), and
`objectives_skipped` counts the `*.yaml` refused by NAME before any fetch.
`objectives_dir: "timeout"` is the fan-out's budget having run out — an
answer, like `absent` and `unreadable`, never an error.

### 50.8 Bounds, and why they are where they are

| Bound | Value | Why |
|---|---|---|
| `MAX_OBJECTIVE_FILES_SCANNED` | 100 | files read before filtering |
| `MAX_OBJECTIVES` | 20 | objectives **returned** — the cap is on the OUTPUT |
| `MAX_METRICS_PER_OBJECTIVE` | 12 | per objective; `metrics_truncated` states it |
| `MAX_TEXT` | 400 | statements; ids 64, owner 128, `by`/`review_by` 32, `horizon`/`direction` 16, `target_text` 64 |
| `READ_CONCURRENCY` | 2 | objective reads in flight |
| `OBJECTIVE_READ_TIMEOUT_SEC` | 5 s | one objective file — a few hundred bytes |
| `OBJECTIVES_READ_BUDGET_SEC` | 30 s | the whole fan-out |

**Scan, then filter, then cap.** Capping the *listing* first (the shape #2927
shipped) hides an agent's own objective behind twenty foreign ones in a shared
fleet canon — the files sort by name and nothing makes an agent's own sort
early. The cap therefore applies to what survives the concern filter.

**The fan-out is deliberately narrow.** `AgentClient`'s circuit breaker trips
at three failures and it is the same breaker chat rides on, so objective reads
run ≤ 2 in flight and **abort to `unavailable: agent_unreachable`** on the
first typed transport failure (`AgentNotReachableError`,
`AgentCircuitOpenError`) rather than spending nineteen more reads driving it
open. The join does its own transport rather than `AgentClient.read_file`,
which flattens every `AgentClientError` into `{"success": False}` — and the
difference between "this file is unreadable" and "this agent is gone" is
exactly whether the remaining reads are worth attempting.

**A slow agent is bounded by the clock, not by the abort.** The unreachable
abort only fires on *typed transport death*; an agent-server that answers
slowly raises nothing, so the fan-out carries a wall-clock budget
(`OBJECTIVES_READ_BUDGET_SEC`, 30 s) and each read a 5 s timeout of its own.
Past the budget the read returns `objectives_dir: "timeout"` with the
`objectives_read_timeout` finding and no objectives — a part answer that says
what happened, rather than a backend task held for `100 / 2` slow reads while
the 60/min limiter admits the next fan-out behind it.

**The known cost**: a fleet canon with 60 objective files and ten polling role
cards is 60 file reads per request against a single-process agent-server. The
60/min knob, the ≤ 2 concurrency, the budget and the unreachable-abort are what
bound it;
the long-run relief is a consumer composing `read_objective_files` once with
`join_objectives` per agent (50.10), not a cache.

**Author values are normalised at parse.** `target` becomes a **finite**
`int|float` or `null`, with anything else kept as bounded `target_text`:
`.nan` / `.inf` are legal YAML that the hardened loader passes through and
`json.dumps` emits as bare `NaN` / `Infinity` — which is not JSON, so
`JSON.parse` throws and the consumer renders a blank card. `bool` is excluded
too (`isinstance(True, int)` is `True`, and `true` is not `1`).

`x-canon.clone_path` is validated segment by segment with `..` refused
**before** any file is read with it.

### 50.9 Zero config

An agent with no `x-role` and no `x-canon` costs one container-state read and
one `template.yaml` read — **no store query at all** — and answers
`{objectives: [], message: "no x-role or x-canon in template.yaml — nothing to
join…"}`. Every empty state carries `message` naming the next action: the
`objectives/` directory that was not found, the agent that is stopped, the fact
that no active objective names this agent.

An agent with objectives but no `metrics:` block gets a row per referenced
metric, every one `declared: false` with its `metric_undeclared` finding and
`summary.undeclared: n` — loud, not blank.

### 50.10 Consumers — and the no-second-join rule

| Consumer | How |
|---|---|
| `GET /api/agents/{name}/objectives` | the operator/agent door |
| MCP `get_objectives` | agent-scoped (no agent parameter), returns the route body verbatim, never throws |
| Role card (ent#527, PR #2927) | calls `read_objective_join(agent, template=…, client=…)` **in process** behind its own roster gate — one implementation, two doors |
| Project hub (ent#661) | composes `read_objective_files` (one file read) with `join_objectives` per participating agent over store-only reads — the agent door stays out of its loop |
| Proactivity (ent#605) | consumes `summary.behind` and per-row `gap.status == "behind" and not stale`; it owns the "never act on a stale number" rule and the pace maths |

Deliberately **not** here: a platform-side copy of objective files; an
objective-centric cross-agent read (ent#661's design pass, with cross-agent
metric access owned by ent#80); a proactivity evaluator; a `metrics.json`
fallback for `actual` (retired by §49's D-010); a projection cache.

### Acceptance

- [x] One join, in one module, imported by every consumer — no second gap
      computation anywhere
- [x] `actual` is the tile's folded latest via the extracted
      `latest_by_metric`, parity-tested including a dimensioned `sum` metric
- [x] `freshness()` is imported, never re-derived — the §49.1 rule is the only
      stale rule in the join
- [x] An objective naming an undeclared metric is a named finding with the fix,
      never a blank; a supporting-only agent gets the informational code instead
- [x] A retired metric withholds its value and says why
- [x] `hold` is `on_target` within `tolerance` and `off_target` otherwise —
      never behind/ahead; only `active` objectives are joined, and neither a
      non-active nor another role's objective puts a finding on this read
- [x] `direction` never leaves the registry's three values: a declared `hold`
      is `neutral` with `direction_source: objective`
- [x] Direction falls through to the objective file when the registry is
      `neutral`, with `direction_source`; two declared directions that disagree
      are a finding
- [x] `gap` is position, never pace; `by` and `horizon` ride the row
- [x] `stale` and `gap` are orthogonal — a stale metric keeps its gap
- [x] Uniform 404 → agent self-gate → its own 60/min limiter on the validated
      name; 503 + `Retry-After` on a store outage; every other failure is a
      named field on a 200
- [x] Scan 100, filter, cap 20; ≤ 2 reads in flight; abort to
      `agent_unreachable` on the first transport death; a 30 s fan-out budget
      and a 5 s per-read timeout for an agent that is merely slow
- [x] A file name this read refuses is counted and named, and an `id` the
      author wrote and this read refused is a finding, not a silent rename
- [x] Author-shaped targets (`.nan`, `.inf`, lists, bools, long strings) cannot
      reach the wire as non-JSON
- [x] Zero config: no role and no canon costs no store query and names the next
      action
- [x] The model is the contract, pinned by key parity
- [x] MCP `get_objectives` is agent-scoped, takes no agent parameter, and never
      throws
