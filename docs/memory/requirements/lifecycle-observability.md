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
  survive per their own retention discipline. Bounded by the shared
  5000-row/cycle cap so a backlog drains gradually.
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
  C-001/C-002 and K-001/K-002 overlaps, and on `git_service._GITIGNORE_PATTERNS`
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
  256 KB → 413; fields strictly validated), `GET /api/agents/{name}/reports` (metadata only),
  `GET /api/reports` (fleet, accessible-agent filtered; `agent`/`report_type`/`hours`/`search`),
  `GET /api/reports/stats` (total / by_type / agents KPI counts), `GET /api/reports/{id}`
  (full payload; 404 on no-access), `DELETE /api/agents/{name}/reports/{id}` (owner; scoped by
  agent_name + id).
- **FR-4 — Real-time**: a **thin** `agent_report` WebSocket trigger (agent_name, report_id,
  report_type, created_at — never title/payload, since `/ws` is unfiltered SCOPE_ALL); the
  frontend refetches via the access-controlled REST endpoints.
- **FR-5 — Frontend**: Agent Detail "Reports" tab + Operations → "Reports" fleet tab. Generic
  + typed renderers (table / KPI tiles / markdown / timeline / JSON) chosen by `display_hint`,
  then `report_type` prefix, then JSON; each renderer validates payload shape and falls back to
  the JSON viewer on mismatch. List shows metadata; full payload lazy-loads on expand.
- **FR-6 — Retention**: cleanup sweep deletes `agent_reports` older than
  `agent_reports_retention_days` (default 90; `0` disables), chunked like the #772 sweeps.

**Deferred**: effect-guard dedup on `report()` for at-least-once pull-mode re-delivery
(#1084/Epic #1045); audit-log entry on write; per-report sharing distinct from agent access.

---
