# Requirements — Roadmap — Advanced, Planned, Process Engine, Future Vision

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 16. Advanced Features

### 16.1 Agent Resource Allocation
- **Status**: ✅ Implemented (2026-01-02; RES-001 system defaults 2026-04-30)
- **Description**: Per-agent memory and CPU configuration; system-wide admin defaults as fleet-level ceiling
- **Key Features**: 3-tier fallback (per-agent DB override → system default → hardcoded safe value); admin `GET/PUT /api/settings/agent-defaults/resources`; CPU as whole processors (1/2/4/8/16); memory as Docker-native strings (1g–32g)
- **Flow**: `docs/memory/feature-flows/agent-resource-allocation.md`

### 16.1a Read-Only Mode
- **Status**: ✅ Implemented (2026-02-17)
- **Description**: Per-agent code protection preventing modification of source files
- **Key Features**: Toggle in AgentHeader, PreToolUse hooks intercept Write/Edit/NotebookEdit, blocked patterns (*.py, *.js, etc.), allowed patterns (output/*, content/*)
- **Flow**: `docs/memory/feature-flows/read-only-mode.md`
- **Spec**: `docs/requirements/READ_ONLY_MODE.md`

### 16.2 SSH Access
- **Status**: ✅ Implemented (2026-01-02)
- **Description**: Ephemeral SSH credentials via MCP tool (admin-only)
- **Key Features**: ED25519 keys, configurable TTL, ops setting controlled, admin-only access
- **Flow**: `docs/memory/feature-flows/ssh-access.md`

### 16.3 Agent Info Display
- **Status**: ✅ Implemented
- **Description**: Template metadata display in Info tab
- **Flow**: `docs/memory/feature-flows/agent-info-display.md`

### 16.4 Parallel Headless Execution
- **Status**: ✅ Implemented (2025-12-22)
- **Description**: Stateless parallel task execution via `POST /task` endpoint
- **Key Features**: Bypasses queue, enables orchestrator-worker patterns
- **Flow**: `docs/memory/feature-flows/parallel-headless-execution.md`

### 16.5 System Manifest Deployment
- **Status**: ✅ Implemented (2025-12-18; resilient deploy 2026-07-23, trinity-enterprise#125)
- **Description**: Recipe-based multi-agent deployment via YAML manifest
- **Key Features**: Permission presets, shared folders, schedules, auto-start
- **Resilient deploy (trinity-enterprise#125)**: deploy is **best-effort by default** — a per-agent create failure is collected into `failed: [{name, short_name, template, reason, status_code}]` and the remaining agents still deploy; post-create configuration (folders/permissions/schedules/tags/view) is scoped to the created agents and each phase degrades to `warnings` instead of aborting. Response `status` is **five-valued**: `deployed` (all created) / `partial` (some failed, HTTP 200) / `failed` (none created, HTTP 500 with the full report as the body) / `valid` (dry-run, no blockers) / `invalid` (dry-run, blockers populated in `failed` — #1841). **`status` describes AGENT CREATION only**: a folder/permission/schedule/tag/start failure lands in `warnings[]` while `status` stays `deployed`, so a consumer that renders `status` without `warnings` can report a fleet as deployed when every schedule failed and nothing started. `strict: true` on the request restores abort-on-first-error, preserving the failing agent's original status code. Failure reasons are credential-sanitized + URL-userinfo-redacted + truncated at the exit point. A partial response warns that re-deploying the same manifest creates `_N`-suffixed duplicates (converge/`on_conflict` semantics remain deferred under the fresh-install epic trinity-enterprise#122 — the first-run seeder §16.5.1 sidesteps them by never re-deploying: flag latch + existence backstop). The global `trinity_prompt` write only happens when at least one agent was created. Prerequisite for the fresh-install seed (§16.5.1, trinity-enterprise#124) and the UI manifest install (§16.5.2, trinity-enterprise#126).
- **Deploy orchestration home (trinity-enterprise#124)**: the full deploy pipeline (parse → validate → resolve → create loop → prompt → config phases → start) lives in `services/system_service.py::deploy_manifest(manifest_yaml, current_user, request=None, *, dry_run, strict, create_agent_fn=None)` — `routers/systems.py::deploy_system` is a thin HTTP wrapper (maps `status="failed"` → HTTP 500 JSONResponse; everything else passes through). `create_agent_fn=None` resolves lazily to the `routers/agents.create_agent_internal` **facade** (which injects `ws_manager`), so `agent_created` WS broadcasts are preserved on both the HTTP and seed paths.
- **Flow**: `docs/memory/feature-flows/system-manifest.md`

#### 16.5.1 First-Run Default System Seed (trinity-enterprise#124)
- **Status**: ✅ Implemented (2026-07-24)
- **Description**: On a genuinely fresh install, auto-deploy a bundled default system manifest so a new instance comes up with a running starter fleet — the multi-agent generalization of the Cornelius seeder (ent#107, §core-agent).
- **Seeder**: `services/system_seed_service.py`. `ensure_first_run_seeded()` is the single first-run pass both call sites use (`routers/setup.py` setup-completion background task; `main.py` lifespan safety-net gated on `setup_completed`, held via a module-level strong task reference): it resolves the **persisted first-run verdict** then runs the Cornelius seeder and the fleet seeder under that one decision. **#2215:** the whole pass runs under ONE cross-worker lock (`first_run_seed:provision`, TTL 900s, via the shared `SingleFlightLock` #1920 — unique per-acquire token + compare-and-delete release, fail-open) — the two inner seeder locks serialise each seeder only against itself, so one worker's slow Cornelius clone used to run concurrently with another's fleet deploy (the burst behind the SSH-port collision and any concurrent boot transient that latches `partial`); the loser skips the entire pass without touching any flag and retries on the next trigger.
- **Persisted freshness verdict (`first_run_fresh` system-setting)**: computed ONCE — `count_non_system_agents() == 0`, forced false when `cornelius_seeded=="true"` (an established ent#107-era install, including one whose agents were later deleted) — and stored durably. Both seeders consume it (`ensure_seeded(fresh=...)`; Cornelius falls back to its legacy internal count when `fresh=None`). Without this, the first seeder's own agents poison every later pass's count: a failed fleet deploy could never retry, and a failed Cornelius would self-mark seeded after the fleet lands.
- **Idempotency & safety**: durable `default_system_seeded` flag; Redis SETNX lock `system_seed:provision` (TTL 600s, fail-open) — acquired + released through the shared `redis_breaker_util.SingleFlightLock`, so release is **ownership-checked (unique per-acquire token + compare-and-delete)**, not the pre-#1920 constant-`"1"` + unconditional `delete` that could delete a *successor's* live lock (#1920); the lock/token is LOCAL to the seeding pass (never stored on the singleton), so two overlapping in-process passes can't clobber each other's ownership state; **existence backstop** inside the locked section — if any final `{system}-{short}` agent name is already reserved (`is_agent_name_reserved`, covers soft-deleted), the seeder converges the flag WITHOUT deploying (the deploy path has no 409 backstop: name collisions become `_N` suffixes, so a fail-open lock race would otherwise double-seed). Flag policy: `deployed`/`partial` → set (a partial fleet must never be re-deployed — that suffixes duplicates); `failed` (0 created) / exception / unreadable manifest → NOT set (retry next pass; nothing to duplicate). `default_system_seed_info` (JSON: manifest name, sha256, status, created/failed counts, seeded_at) is stored beside the flag for diagnostics and future curated-fleet upgrades (ent#137).
- **Manifest resolution**: `TRINITY_DEFAULT_SYSTEM_MANIFEST` env (read at call time, `strip()`ed; empty ⇒ unset) — a path to an operator manifest, or a disable sentinel (`disabled`/`none`/`off`/`0`/`false`); otherwise the bundled `config/manifests/default-system.yaml` (baked into the backend image AND bind-mounted via the `config/manifests` directory in both compose files — the dev compose `./src/backend:/app` mount shadows image COPYs, so the mount is load-bearing locally). An explicitly-set-but-unreadable override fails loudly (ERROR log + operator-queue alert) and does not fall back to the bundled manifest.
- **Bundled starter fleet**: the in-tree acme trio (`local:scout`/`local:sage`/`local:scribe`) deployed as the coherent team its CLAUDE.md content assumes — system name `acme`, shared folders, `permissions: preset: full-mesh`; **no schedules** (zero-credential installs must not accumulate failing cron executions), **no `prompt:`** (never mutates the platform-wide `trinity_prompt`). Content is data: ent#137's curated public fleet replaces the manifest without touching the mechanism. Footprint: a fresh install runs `trinity-system` + Cornelius + 3 starters (limits, not reservations); operators opt out via the disable sentinel.
- **Honest status**: partial/failed seeds, seed-path errors (e.g. a parse-broken override), unreadable overrides, crash-interrupted partial fleets detected by the converge backstop, and — #2215 — a failed **Cornelius** creation (`create_failed`/`create_blocked`, or the belt-except when the seeder raises despite never-raises; id `system-seed-cornelius-failed`, previously log-only) all emit a platform-path operator-queue alert (direct DB create on `trinity-system`, deterministic `system-seed-*` id — a #1632 reserved prefix so an agent cannot pre-create-and-silence it; best-effort) in addition to logs.
- **Fail-open**: the seeder never raises and never blocks boot or setup completion.
- **Flow**: `docs/memory/feature-flows/system-manifest.md` (seed section)

#### 16.5.1a First-Run Front Desk (trinity-enterprise#319)
- **Status**: ✅ Implemented (2026-08-18)
- **Problem**: seeding (§16.5.1) made the onboarding wizard's own freshness test permanently false. `Dashboard.vue::maybeAutoOpenOnboarding` fires only when there are **zero non-system agents**; since a fresh install now comes up with Cornelius plus the bundled fleet already running, the ent#52 "one question → a tailored agent" wizard stopped auto-opening on exactly the install it was written for. The user meets a running fleet they did not choose and no route into anything.
- **Surface**: `components/onboarding/FrontDeskPanel.vue` + `stores/firstRun.js` — a dismissible, non-gating card on the Dashboard offering the three doors: **Show me** (opens the seeded demonstrator's chat — zero commitment), **Make me one** (emits to the existing wizard), and **Bring mine**, deliberately a de-emphasised secondary link rather than a peer button (fleet migration is the strongest capability and the worst first impression). It is a **surface, not an agent** — the issue's original "seed nothing, ship a front-desk agent" premise was reversed when trinity-enterprise#322 was closed not-planned; what a fresh install contains belongs to trinity-enterprise#137.
- **Predicate** (`GET /api/onboarding/first-run` → `services/onboarding_service.py`): `first_run` is true while every agent the caller can see is one Trinity seeded. The seed is deployed **under the admin account**, so it is indistinguishable from a human admin's own work in `audit_log` — an actor-based test cannot answer this. The seeded set is instead derived at read time from the seeder's own naming contract: `system_seed_service.seeded_agent_names()` (`{system}-{short}` from the manifest actually in force, honoring `TRINITY_DEFAULT_SYSTEM_MANIFEST` and its disable sentinels) plus `CORNELIUS_AGENT_NAME`. Read-time derivation, so no new persisted state, no migration, and an operator seeding their own manifest gets their own names.
- **DB-only read**: visibility comes from `db.get_all_agent_metadata` (owner/shared/is_system), NOT the Docker-backed `get_accessible_agents` — the question is about ownership rows, so the card renders truthfully when a container is down.
- **Two surfaces never stack**: the card requires `seeded_agents` to be non-empty, so a genuinely empty install (seeding disabled) still gets the wizard's auto-open, unchanged. Dismissal is per-browser `localStorage` (`trinity_front_desk_dismissed`), matching the wizard's existing key.
- **Fails toward silence**: any read failure resolves to `first_run: false`. A missed nudge is a non-event; a first-run card over a mature fleet is noise no user can dismiss on the install's behalf.
- **Gating**: OSS-core, un-gated — this is the open install's front door.

#### 16.5.2 UI Manifest Install (trinity-enterprise#126)
- **Status**: ✅ Implemented (2026-07-30)
- **Description**: Install a whole multi-agent system from the web UI — pick a bundled manifest, upload a file, or paste YAML; preview it; deploy it. Exposes the already-shipped `POST /api/systems/deploy` capability, which was previously reachable only via curl or the MCP `deploy_system` tool.
- **Surface**: a third stacked `<section id="systems">` on the **Library** page (`views/Library.vue`), between Agent Templates and Skills — no new NavBar entry. Shipped first as a `?tab=` strip on `Templates.vue`; ent#263 then renamed that page to Library and deliberately chose stacked sections + in-page anchors over tabs/filter pills, so ent#126 adopts the page's own model instead of competing with it. Systems sits beside Agent Templates because both **install agents** (one template = one agent; one manifest = a fleet with its permissions, schedules and folders wired), with Skills last as the kind that configures agents that already exist. Hidden entirely below `creator` rather than shown-and-disabled. `/templates` still redirects to `/library` preserving query **and** hash. Components in `components/systems/` (`SystemInstallPanel` / `ManifestPreview` / `DeployResult`) over the new domain-scoped `stores/systems.js` (Invariant #6; NOT bolted onto `systemViews.js` — a System is a manifest-deployed name-prefix group, a System View is a saved tag filter).
- **Bundled-manifest catalog (read-only)**: `GET /api/systems/manifests` and `GET /api/systems/manifests/{manifest_id}` in `routers/systems.py`, both `require_role("creator")` mirroring `POST /deploy` (AC #6; also rejects connector principals). Served from `config/manifests/` (already bind-mounted `:ro` into the backend in both compose files) via `TRINITY_MANIFESTS_DIR`, read at call time. **Both routes are declared above the parameterized routes (Invariant #4) for TWO collisions**: `/manifests` vs `GET /{system_name}`, and `/manifests/manifest` vs `GET /{system_name}/manifest`. Naming adjacency: `/api/systems/manifests` (bundled catalog) and `/api/systems/{name}/manifest` (export a **deployed** system as YAML) read alike and are unrelated. Not exported over MCP (Invariant #13) — a UI affordance; `deploy_system` already exists there.
- **Catalog semantics**: `valid` means parse + validate + the same side-effect-free template/resource preflight the dry-run runs — `parse_manifest` alone accepts invalid names, unsupported template prefixes and bogus presets, so a parse-only check would mark an undeployable manifest valid. Listing is **fail-soft per file** (an unreadable/oversized/invalid manifest is listed with `valid: false` + a short `reason`, never a 500 for the whole catalog — one bad file must not hide the others; every `reason` exits through `_failure_reason`, the same credential-sanitizing, URL-userinfo-redacting, length-capping exit the deploy report's `failed[].reason` uses, because PyYAML parse errors **echo the offending source line** and a joined blocker list grows with the manifest), and reports `already_deployed`, `sets_prompt` and `schedule_count` so the UI can warn before the user commits. Path confinement on `{manifest_id}` is layered: character allowlist, **explicit** rejection of `""`/`.`/`..`/embedded `..` (the regex does NOT reject these — `.` is inside its character class), a length cap, a suffix that is ours by construction, `resolve()` + `is_relative_to` (a symlink escaping the directory), and an `is_symlink()` check on the **unresolved** entry (a symlink whose target stays *inside* it — `resolve()` has already erased the link, so this must precede it). That last layer is catalog/read **parity**: `list_bundled_manifests` skips symlinks outright, so without it a symlink is invisible in the catalog yet readable by id, and "not listed" stops meaning "not served". Reads open once with `O_NOFOLLOW` and `fstat` that descriptor, capping at `MANIFEST_MAX_BYTES + 1` — `config/manifests` is a host bind mount, so a stat-then-read sequence has a real swap window.
- **Extended dry-run preview (AC #2)**: the response gains `permission_edges` ({source: targets}), `schedules_preview` ([{agent, short_name, name, cron, message, enabled, timezone, description}]) and `system_view_requested`. Both preview blocks are computed by **pure resolvers shared with the writers** — `resolve_permission_edges` / `resolve_schedule_previews` in `system_service.py`, which `configure_permissions` / `create_schedules` now loop over — so the preview cannot drift from what deploy does. Only the backend knows the resolved `_N`-suffixed names, so this cannot be derived client-side. `permissions_configured` / `schedules_created` deliberately stay `0` on a dry run: they mean "written". Behaviour preservation for the two refactored writers is pinned by characterization tests captured green BEFORE the refactor (`tests/unit/test_ent126_{permission,schedule}_characterization.py`) — a resolver-vs-refactored-writer parity test would be tautological, and the ent#125 suite mocks both writers.
- **Resource preflight**: `_preflight_template` now validates **merged** resources through the create path's own `normalize_cpu`/`normalize_memory`, with the create path's own template-wins precedence. Previously it checked template shape only, so a manifest with an unusable resource value previewed `valid` and then failed 100% of its agents at create — a shipped bundled manifest carried `cpu: 1.0` and did exactly that. For a `github:` template the merge needs a network call the preview refuses to make, so the declared values are validated instead (can over-report if the remote template overrides them; documented, and the fix is harmless either way).
- **Honest reporting**: `status` is switched on, never the HTTP code (`partial`/`invalid` are 200; `failed` is 500 **with the full report as the body**, which a naive `catch` would discard along with the `failed[]` list AC #3 needs). Deploy result is headed "agents created", not "success", and `warnings[]` renders prominently. A manifest that sets `prompt:` (replacing the platform-wide `trinity_prompt` for **every** agent) or carries enabled schedules (starting recurring autonomous executions) is gated behind an **explicit acknowledgement**, not a banner. `_N`-duplicate warnings are confirm-grade. A timeout or bare 5xx reports **"outcome unknown — may still be running"** and deliberately offers no retry, since cancelling the request does not cancel the serial server-side deploy and re-running duplicates every agent that succeeded.
- **Preview binding**: `preview` is bound to `previewedText` and Deploy is gated on them matching, so a user cannot preview manifest A, edit to B, and deploy B under A's preview.
- **Other guards**: manifests are capped at `MANIFEST_MAX_BYTES` (256 KB) — previously unbounded. The constant is defined **once**, in `models.py`, and imported by `system_service`, because the same number bounds two things that must agree about the same file: `parse_manifest`'s cap and the bundled-catalog file read (the request-model validator ent#126 originally added is gone — #1884 moved the cap into the parser, so an oversized manifest is a 400, not a 422). It is **byte-denominated at every one of them** (`len(encode("utf-8"))` / `st.st_size` / the UI's `file.size`) — a character-counting cap admits a multibyte manifest at up to ~4× the stated limit. Enforcement lives in `parse_manifest` alongside the alias-budget and duplicate-key guards (trinity#1884, which landed on `dev` during this branch's review and supersedes the request-model cap ent#126 originally added — an oversized manifest is now a 400 parse failure, not a 422 request-model violation). `parse_manifest` now records unrecognised top-level keys and `validate_manifest` **warns** about them (never rejects — that would 400 manifests deploying today); this is the durable guard for the class that let `trinity_prompt:`/`auto_start:` sit in a shipped manifest doing nothing. Keys are coerced with `str()` before sorting: PyYAML is YAML **1.1**, so bare `on`/`off`/`yes`/`no` parse as **booleans** and `2:` as an int — a mixed-type key set made `sorted` raise `TypeError`, and the catch-all turned it into a raw 500 on a manifest that deployed fine before the check existed, i.e. the hygiene check became the unnamed 500 it was added to prevent (AC #4). A **requested** system view that fails to create now appends a warning, since `create_system_view` swallows its exception and returns `None`, which was previously indistinguishable from "no view requested".
- **AC #5 (no dead end)**: deploy always tags every created agent with the system name, so `/?tags=<system>` always works as the next action, with `/?view=<id>` preferred when the manifest declared a `system_view:` and it was created. `views/Dashboard.vue` gained a small additive reader for both, yielding to an active system view.
- **Editor**: plain `<textarea>`. The orphaned monaco-based `components/YamlEditor.vue` (zero consumers since the Process Engine was decommissioned) was NOT revived: prod CSP is `script-src 'self'` with no `unsafe-eval` and no `worker-src` while the dev CSP allows `unsafe-eval`, so `npm run dev` cannot prove prod. Every AC is satisfiable without it.
- **Out of scope / deferred**: async deploy with a job id + reconciliation (the real fix for the timeout window — deploy is fully synchronous AND serial, so a large `github:` manifest outlives any client timeout); `SystemDetail.vue` / a deployed-systems browser; remote/registry manifest sources (ent#14, ent#108); per-agent credential setup after deploy (ent#127); `convergent`/`on_conflict` re-deploy semantics (still deferred under epic trinity-enterprise#122).
- **Flow**: `docs/memory/feature-flows/system-manifest.md`

### 16.6 Local Agent Deployment via MCP
- **Status**: ✅ Implemented
- **Description**: Deploy local agents via MCP tool
- **Flow**: `docs/memory/feature-flows/local-agent-deploy.md`

### 16.7 Agents Page UI
- **Status**: ✅ Implemented (2026-01-09)
- **Description**: Grid layout with Dashboard parity for Agents list page
- **Key Features**: 3-column grid, autonomy toggle, execution stats, context bar
- **Flow**: `docs/memory/feature-flows/agents-page-ui-improvements.md`

### 16.8 Dark Mode / Theme Switching
- **Status**: ✅ Implemented (2025-12-14)
- **Description**: Client-side theme system with Light/Dark/System modes
- **Key Features**: localStorage persistence, Tailwind class strategy
- **Flow**: `docs/memory/feature-flows/dark-mode-theme.md`

### 16.9 Events Page UI
- **Status**: ✅ Implemented (2026-02-20)
- **Description**: Dedicated page for viewing and managing agent notifications
- **Key Features**: Filter controls (status, priority, agent, type), stats cards, notification cards with actions, bulk selection, real-time WebSocket updates, navigation badge
- **Spec**: `docs/requirements/EVENTS_PAGE_UI.md`
- **Flow**: `docs/memory/feature-flows/events-page.md`

---

## 17. Planned Features

### 17.1 Horizontal Agent Scalability
- **Status**: ⏳ Not Started
- **Priority**: High
- **Description**: Agent pools with N instances for parallel workloads
- **Key Concepts**: Pool configuration, load balancing, auto-scaling triggers

### 17.2 Agent Event Subscriptions (EVT-001)
- **Status**: ✅ Implemented (2026-03-26)
- **Priority**: High (P1)
- **Description**: Lightweight SQLite-backed pub/sub for inter-agent event pipelines
- **Key Features**:
  - MCP tool `emit_event(event_type, payload)` — agents emit named events with structured data
  - CRUD API for event subscriptions (source agent, event type, message template)
  - Subscription trigger: matching event → async task to subscriber with `{{payload.field}}` interpolation
  - Permission-gated: uses existing `agent_permissions` — subscriber must be permitted to call source
  - Events persisted to `agent_events` table, subscriptions to `agent_event_subscriptions`
  - WebSocket broadcast for real-time event visibility
  - MCP tools: `emit_event`, `subscribe_to_event`, `list_event_subscriptions`, `delete_event_subscription`
- **GitHub Issue**: #169
- **Relationship to 17.2 (Redis Streams)**: This is a pragmatic first step. If Redis Streams (#22) lands later, subscriptions can migrate.

### 17.2a System-Emitted Task Completion Events (#1578)
- **Status**: ✅ Implemented (2026-07-17)
- **Priority**: Medium (P2) · `theme-reliability`
- **Description**: The **backend** deterministically emits `agent.task.completed` / `agent.task.failed` at **every CAS-won execution terminal**, delivered over the existing EVT-001 subscription-dispatch machinery, so a subscribed caller/orchestrator is **woken** when a long async task finishes instead of polling `get_execution_result`. Implements the missing half of `TARGET_ARCHITECTURE.md` §Async-First Communication and is a down-payment on Epic #1045 → #1081 (pull coordination).
- **System- vs agent-emitted**: EVT-001 (17.2) carries only **agent-emitted** events (an agent's LLM calls `emit_event`). These are the first **system-emitted** events — synthesized by the deterministic backend chokepoint with no LLM in the loop, `source_agent` = the executing agent, in the reserved `agent.task.*` namespace.
- **Key Features**:
  - Emitted from a single shared helper (`services/event_dispatch_service.py::emit_task_terminal_event`) fanned across **every** CAS-won terminal writer — `apply_result` (success + failure), `_write_terminal_and_gate` (timeout/budget/crash), the #1083 lease-reaper (`cleanup_service`), and the pull sink (`pull_coordination_service`, dark until a pilot). Bulk watchdog sweeps are a documented residual (no per-row context).
  - **Matching-subscription gated**: `find_matching_event_subscriptions` runs FIRST — zero matching subscriptions ⇒ **no** `agent_events` row and **no** dispatch (inert by default, no fleet-wide spam).
  - **CAS-won exactly-once**: emit runs strictly inside the `won` branch, so a replayed/late #1083 callback or a lease-expiry race fires nothing (no double-wake).
  - **Payload** `{execution_id, status, triggered_by, summary_or_error, duration_ms, cost, fan_out_id, loop_id}` — flat + `{{payload.field}}`-interpolable; `status` is the string value (`success`/`failed`/`cancelled`). `fan_out_id`/`loop_id` carried for the future pull fan-out join envelope.
  - **Reserved namespace + loop safety (3 layers)**: agents cannot `emit_event` into `agent.task.*` (400); self-subscription to `agent.task.*` is blocked at create + update (400); and a **recursion-break** — a task spawned by an `agent.task.*` dispatch is persisted with `triggered_by="event"` and the emit helper suppresses re-emission, breaking self / A↔B / A→B→C→A auto-emit cycles at the root.
  - **Delivery** = EVT-001 subscription dispatch (loopback async `/task`). Best-effort: wakes a **running** (incl. #1402 parked-but-running) subscriber; a stopped subscriber's 503 is swallowed (the `agent_events` row persists, the wake does not). Durable "lands in the caller's queue" is the pull migration's future queue.
  - **Fail-open**: the entire emit body is try/except-swallowed — a broken/slow emit never affects the billed terminal.
- **Additive & inert**: reuses `agent_events` / `agent_event_subscriptions` and the existing `triggered_by` TEXT column — no schema change, no migration, no feature flag, no new config.
- **GitHub Issue**: #1578 (Epic #1045 → #1081)

### 17.3 Event Handlers & Reactions
- **Status**: ✅ Partially Implemented via EVT-001 (2026-03-26)
- **Priority**: High
- **Description**: Configure automatic agent reactions to events
- **Key Concepts**: Event matching with filters, debouncing/throttling
- **Note**: Basic event → task triggering implemented in EVT-001. Advanced filtering, debouncing, and throttling are future enhancements.

### 17.4 Async MCP Chat Commands
- **Status**: ✅ Implemented (2026-01-30)
- **Priority**: High
- **Description**: Non-blocking MCP `chat_with_agent` for parallel multi-agent orchestration
- **Key Features**: `async=true` parameter (requires `parallel=true`), returns `execution_id` immediately, poll `GET /api/agents/{name}/executions/{id}` for results
- **Use Case**: Orchestrator sends tasks to 5 worker agents simultaneously, collects results as they complete

### 17.5 Fan-Out Parallel Self-Invocation (FANOUT-001)
- **Status**: ✅ Implemented
- **Description**: Dispatch N independent tasks to an agent in parallel, collect results with per-task status
- **Key Features**: `POST /api/agents/{name}/fan-out` endpoint, `fan_out` MCP tool, configurable `max_concurrency` (1-10, default 3), overall deadline with per-task timeout, best-effort policy (partial results on failure), dedicated fan-out concurrency (doesn't starve normal operations)
- **Use Case**: Agent self-invocation for batch predictions, parallel analysis, ensemble methods — each subtask gets a fresh context window
- **Execution Tracking**: All subtasks follow standard `TaskExecutionService` path — visible on dashboard with full observability (cost, tokens, logs), linked by `fan_out_id`
- **Limits**: Max 50 tasks per fan-out, max 10 concurrency, timeout 10-3600s, task IDs must be unique alphanumeric (max 64 chars)
- **Flow**: `docs/memory/feature-flows/fan-out.md`

### 17.6 Automated Git Sync
- **Status**: ⏳ Not Started
- **Priority**: Medium
- **Description**: Sync modes - Manual / Scheduled / On Stop

### 17.7 Automated Secret Rotation
- **Status**: ⏳ Not Started
- **Priority**: Medium
- **Description**: Automatic credential rotation with notifications

### 17.8 Kubernetes Deployment
- **Status**: ⏳ Not Started
- **Priority**: Low
- **Description**: Helm charts, StatefulSet for agents

---

## 18. Process Engine (Business Process Orchestration)

> **Status**: ❌ DELETED (2026-04-24, issue #430, PR #482)
> **Archive**: `archive/process-engine` git branch preserves full history
> **Reason**: The `agent_task` step handler bypassed `TaskExecutionService`, violating all orchestration invariants (slot accounting, activity tracking, backlog). Option B (delete) was chosen over Option A (fold through TES) to keep the execution stack clean.
> **What replaced it**: Agent scheduling + `TaskExecutionService` is the standard execution primitive. Human-approval use cases can be served by the Operating Room operator queue.

All subsections 18.1–18.10 were deleted with the code. Flow docs archived at `docs/memory/feature-flows/archive/process-dashboard.md`.

---

## 19. Future Vision

### 19.1 Human-in-the-Loop Improvement
- **Status**: ⏳ Concept Phase
- **Description**: Feedback collection and continuous improvement of agent behavior

### 19.2 Compliance-Ready Methodology
- **Status**: ⏳ Concept Phase
- **Description**: SOC-2 and ISO 27001-compatible development practices
- **Location**: `dev-methodology-template/`

### 19.3 Process Designer UI
- **Status**: ⏳ Concept Phase
- **Description**: Visual drag-and-drop process builder
- **Note**: Currently using YAML editor with live preview

---
