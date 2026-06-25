# Feature Flows Index

> **Purpose**: Maps features to detailed vertical slice documentation.
> Each flow documents the complete path from UI → API → Database → Side Effects.
>
> For detailed change history, see `git log`.

---

## Recent Updates

| Date | ID | Feature | Flow |
|------|-----|---------|------|
| 2026-06-22 | trinity-enterprise#38, #82 | feat(setup): first-run operator intake + admin email login. Optional **email + company** at first-run setup with an **unchecked-by-default** opt-in ("occasionally receive important security & product updates"); on consent → one **fire-and-forget, at-most-once** POST to a new `/v1/operator-intake` endpoint on #1116's Cloudflare intake app (`intake.abilityai.dev`, already CSP-allowed — **one added endpoint**, Worker out-of-repo). The same email **binds as the admin's sign-in identity** (log in with email + password) — **no verification email is sent** (a fresh install has no Resend key; bound, not code-verified); the code-based email second factor stays **Phase 2** on the existing `mfa_gate` seam (#5/#388), untouched here. Backend: `services/operator_intake_service.py` (DO_NOT_TRACK/`OPERATOR_INTAKE_ENABLED` off-switch, `operator_intake_submitted` marker claimed before the POST, owns `installation_id` — the #758 seed, never raises/logs the email); `routers/setup.py` captures profile + binds email (validates shape before any write, schedules intake via `BackgroundTasks` so it never delays setup); `dependencies.authenticate_user` resolves admin by username **OR** registered email (password-hash guard keeps email-code-only users off the password path); `PUT /api/users/me/email` (own-account, 409 on collision) for the existing-admin transition. Frontend: `SetupPassword.vue` fields + consent, `Login.vue` "Username or email", `Settings.vue` → General "Admin sign-in email" card. `OPERATOR_INTAKE_ENABLED`/`OPERATOR_INTAKE_URL` in `.env.example`. 16 unit tests. | [first-time-setup.md](feature-flows/first-time-setup.md) |
| 2026-06-21 | #1159 | fix(security): authenticate the in-container agent server on the shared agent network — the agent server (`:8000`, FastAPI) historically had **zero auth** on the shared `trinity-agent-network`, so any agent could call a sibling's chat/files/credentials (a now-removed unauthenticated `/ws/chat` route even ran `runtime.execute` — arbitrary Claude). Every backend→agent call now carries a per-agent `X-Trinity-Agent-Token = HMAC-SHA256(AGENT_AUTH_SECRET, "trinity-agent-auth:v1:"+name)` — **derived, not stored** (no DB column; the master lives only in the backend env, so a compromised agent holds its own token but cannot compute a sibling's). New `services/agent_auth.py` helpers (`derive_agent_token`/`build_agent_auth_headers`/`merge_auth_headers`/`agent_httpx_client`), fail-closed (raises on empty `AGENT_AUTH_SECRET`); `start.sh` auto-generates the hex32 master and both compose files forward it. Enforced by a **pure-ASGI** middleware (`agent_server/middleware/auth.py`, `hmac.compare_digest`) on **all** HTTP **and** WebSocket routes (scope-complete, never buffers SSE), exempting only exact `/health` + `OPTIONS`; WS rejects with close 1008 before `accept()`. Grace path: empty `TRINITY_AGENT_AUTH_TOKEN` → allow (old-image rollout); `check_agent_auth_token_env_matches` forces a one-pass recreate to re-inject a missing/stale (renamed-agent) token. Dead `/ws/chat` route + CORS `*` removed (agent server is internal-only). ~10 backend routers/services migrated through the helpers; static guard `test_agent_auth_header_guard.py` fails any new raw `agent-{name}:8000` caller. | [agent-server-authentication.md](feature-flows/agent-server-authentication.md) |
| 2026-06-21 | #946 | feat(orchestration): **pull-pilot routing** for agent→agent MCP chat (Phase 2 PoC for pull/work-stealing, Epic #1045 / umbrella #1081). Behind `MCP_AGENT_CHAT_PULL_ENABLED` (**default OFF**): a **sequential** agent→agent (`scope='agent'`, non-self) `chat_with_agent` is routed by the MCP server through the durable async `/task` path instead of the synchronous held `/chat`; the caller gets an immediate `{status:accepted\|queued, execution_id}` receipt and polls `get_execution_result` (polling is the contract — no completion event). `scope='user'`, self-tasks (SELF-EXEC-001 inject-result/chat-session semantics), and `parallel=true` stay unchanged. `tools/chat.ts` now reads `authContext` unconditionally so scope is decidable (behaviour-equivalent in prod); **D8** route-token (`chat-pull-task`) folds the dispatch route into the idempotency identity so a mid-soak flag flip can't replay a wrong-shape snapshot across `/chat`↔`/task` under the shared `agent:{name}` scope. `config.py` holds the canonical `MCP_AGENT_CHAT_PULL_ENABLED` registry entry (both services read the SAME env key → no single-`.env` drift; actual gate is MCP-side); `settings.py` surfaces `mcp_agent_chat_pull_enabled` in `GET /api/settings/feature-flags` (auth-gated, observability-only, **not** a UI surface); **T5** `chat.py` releases the idempotency claim on the two `/task` dispatch-breaker-open (`CircuitOpen`) deny paths (mirrors `/chat` + CapacityFull — else a breaker-open reject blocks same-key retries 24h). Rollback = flag flip + MCP routing revert. `chat.test.ts` (routing fork + key) + `test_946_task_idempotency_on_deny.py` + feature-flag exposure tests. | [agent-to-agent-collaboration.md](feature-flows/agent-to-agent-collaboration.md), [mcp-orchestration.md](feature-flows/mcp-orchestration.md) |
| 2026-06-21 | #1088 | refactor(reliability): unify the SUB-003 auth-class failure classifier into one shared module. The `is_auth_failure` + `AUTH_INDICATORS` + `NON_AUTH_KILL_MARKERS` (#904) logic, previously duplicated inline in `subscription_auto_switch.py` and `scheduler/service.py`, now lives canonically in `src/backend/services/failure_classifier.py`; `subscription_auto_switch.py` **re-exports** `is_auth_failure` unchanged so existing importers + their test patch targets keep working. The scheduler runs in a separate container (can't import `backend.services`) so the classifier is **vendored byte-identically** at `src/scheduler/failure_classifier.py` — scheduler usage is **log-labelling only** (picks `logger.error` wording, never gates a switch). Byte-identity between canonical + mirror is enforced by `tests/unit/test_904_sigkill_no_false_auth.py::TestBackendSchedulerParity` (edit backend copy, regenerate mirror — no hand-sync). Pure refactor, no behavioral change. | [subscription-auto-switch.md](feature-flows/subscription-auto-switch.md) |
| 2026-06-20 | #1169 | feat(infra): agent runtime data — declared **`data_paths`** + on-demand **export/import** over the **existing durable home volume** (no separate volume, no schema change). Verified premise correction: `/home/developer` is already a persistent named volume (`agent-{name}-workspace`) surviving recreate/upgrade/template-repull/sub-switch, so `data/` is already durable; the feature is the declaration + snapshot surface. **Declaration:** `template.yaml data_paths:` surfaced by `template_service` (github+local) → `crud.py` → `git_service.materialize_data_paths()` writes `~/.trinity/data-paths.yaml` (quoted heredoc) + appends `data/` to the **agent's own** `.gitignore` (idempotent `grep -qxF`, opt-in/no-op when empty). DRY: persistent_state (S4) + data_paths share one extracted `materialize_trinity_yaml_list`/`_read_trinity_yaml_list` primitive (heredoc delimiter parameterized so #383/#384's `PSTATE_EOF` tests stay green). **Export** (`routers/agent_data.py`, `POST .../data/export`, owner/admin): `container_get_archive("/home/developer/data")` (workspace never mounted) → stream to temp on `/data` → `StreamingResponse` (BackgroundTask cleanup); `AGENT_DATA_EXPORT_MAX_BYTES` 5 GiB → 413; embeds `manifest.json`; missing `data/` → manifest-only tar; `?format=base64` inline ≤10 MiB for MCP. **Import** (`POST .../data/import`): proxies the tar to the agent-server `POST /api/agent-server/restore` (`restore_from_tar` enforces `data/**` allowlist + `..`/absolute traversal guard); `Idempotency-Key` dedup. Both serialized per agent by a cross-worker Redis op lock (`agent:data_op:{name}`, SETNX+TTL, fail-open, 409). MCP `export_agent_data`/`import_agent_data` carry the base64 tar. PR2 deferred (scheduled snapshots + `~/.trinity/pre-snapshot` SQLite-quiesce + retention + rename/purge cascade). Schema-free (decoupled from #1183). New `DP-001..DP-005` validation checks. ~25 unit tests incl. the export↔import tar round-trip through the real `restore_from_tar`. | [agent-data-volumes.md](feature-flows/agent-data-volumes.md) |
| 2026-06-20 | #668 | feat: agent deployment compatibility validation — non-blocking server-side checks (~100, 11 categories) against a **running** agent's workspace, surfaced in the Agent Detail Overview tab (`CompatibilityPanel.vue`, "needs attention" idiom + grouped checklist) with one-click auto-fix for the 10 gitignore checks, and an MCP tool. Collector does ONE docker exec → in-container python3 → one JSON snapshot (secret files existence-only, size/binary caps); pure STATIC check functions (HARD-only) + category-batched AI checks (Haiku, iterate-expected, fail-open, **capped at SOFT**, secret-redacted); runtime-aware (claude-only checks skipped for Codex/Gemini). Latest report persisted in `agent_compatibility_results` (dual-track migration; departs from the issue's "no DB table" note so AI findings show without re-spend + fleet aggregation). Auto-fix edits in-container `.gitignore` only (per-agent Redis lock, atomic write, **uncommitted until next sync**). Canonical check list `docs/agent-validation-spec.md`, sync-tested against `spec.py`. 35 fixture-driven unit tests. | [agent-compatibility-validation.md](feature-flows/agent-compatibility-validation.md) |
| 2026-06-19 | #1231 | fix(agent): agent `/tmp` tmpfs size configurable via `AGENT_TMP_SIZE` — the RAM-backed `/tmp` tmpfs size (previously a hardcoded `100m`) is now operator-tunable via `AGENT_TMP_SIZE` on the backend service (e.g. `512m`, `2g`), **default `512m`**, validated `^\d+[mg]$` with invalid/empty → default (`_resolve_agent_tmp_size()` in `services/agent_service/capabilities.py`). Only the **size** is configurable — `noexec,nosuid` stay hardcoded (security invariant); the size counts against the agent memory cgroup and is applied at container **creation**, so existing agents pick it up on recreate not restart. Heavy scratch (pip/npm/ML wheels) is already redirected off `/tmp` via a default `TMPDIR=/home/developer/.tmp` (disk-backed, exec-capable home volume, created by `startup.sh`, #1098). Declared in `.env.example` + both compose files. Doc-sync (feature shipped f2d68381). | [container-capabilities.md](feature-flows/container-capabilities.md) |
| 2026-06-19 | #1115 | feat(ui): per-schedule performance scorecards on Agent Detail — a **Schedules performance** section on the Overview tab lists every non-deleted schedule as one compact scorecard (command/name + cron, terminal success rate, avg duration, cost, context, tool-call total, last-run outcome) and the Schedules tab gains inline per-schedule stats — both fed by **one** call `GET /api/agents/{name}/schedules/analytics-summary?window=` (7d/14d/30d) → `db/schedules.py:get_agent_schedules_summary` (generalises #868; one row per schedule incl. zero-run, no N round-trips), cached per `${name}:${window}` in `executions.js:fetchSchedulesSummary` and shared by both surfaces. Route declared before `/{id}` (Invariant #4). Doc updated in-commit (bd00f609); index Recent Updates row was missing. | [agent-overview-dashboard.md](feature-flows/agent-overview-dashboard.md) |
| 2026-06-19 | #1231 | fix(agent): agent `/tmp` tmpfs size operator-configurable via **`AGENT_TMP_SIZE`** (backend env, default `512m`, validated `^\d+[mg]$` → invalid falls back to default; `noexec,nosuid` stay fixed; counts against the agent memory cgroup; creation-time, so existing agents pick it up on **recreate** not restart). Wired through `docker-compose.yml`/`.prod.yml` + `.env.example`; mount spec centralised as `AGENT_TMPFS_MOUNT` in `services/agent_service/capabilities.py` so create/recreate/system-agent paths can't drift. Heavy scratch (pip/npm/ML wheels) redirected via default `TMPDIR=/home/developer/.tmp` (`AGENT_DEFAULT_TMPDIR`) on the disk-backed home volume, created at start by `startup.sh` (#1098). | [container-capabilities.md](feature-flows/container-capabilities.md) |
| 2026-06-19 | #1116 | feat(ui): in-app bug reporting **+ feature requests + private feedback** from the floating Help widget. New **Bug**, **Feature**, and **Feedback** tabs in `HelpChatWidget.vue` (no second button, shared form): form → **see-before-send review** (scrubbed preview + public/private warning) → success w/ issue link (or "sent" for feedback). Diagnostics auto-captured (version/commit via `useBuildInfo`, route/url, UA/viewport/OS, recent console via a new `utils/consoleBuffer.js` ring buffer installed at bootstrap) and **scrubbed client-side** (`utils/scrub.js`) so the review shows exactly what's sent. **Optional reporter contact email** (not scrubbed, shown as private in review) — never published; rides a **Resend team-notification email** (`ctx.waitUntil`, reply-to = reporter) sent to the team on each new report. Returned `issue_url` link-rendered only if `https://github.com/` (href guard). Build-time knobs `VITE_BUG_INTAKE_URL` / `VITE_BUG_REPORTING_ENABLED`. `intake.abilityai.dev` added to CSP `connect-src` in **both** `vite.config.js` + `security-headers.conf`. Credential-less by design: reports POST to a hosted Cloudflare Worker intake (`trinity-ops-agent/bug-intake/`, out-of-repo) on the stable `https://intake.abilityai.dev/v1/report-bug` domain, which holds the GitHub token + Resend key server-side, **re-scrubs**, soft per-install caps, dedupes by `(type,content)`, and for **bug/feature** files a labelled issue (`type-bug`\|`type-feature` + `source:in-app`); **feedback** files no issue (private team email only). Worker: 21 unit tests; verified live e2e against `abilityai/trinity` (bug + feature filing, feedback files no issue, labels, scrub, email-kept-private, dedupe, Resend delivery). Screenshot deferred. | [in-app-bug-reporting.md](feature-flows/in-app-bug-reporting.md) |
| 2026-06-19 | #1115 | feat(ui): per-schedule performance scorecards on Agent Detail. New `GET /api/agents/{name}/schedules/analytics-summary?window=` (∈ {7d,14d,30d}→168/336/720h, 422 otherwise) returns **one row per non-deleted schedule** (zero-run schedules included) for the whole agent in a single call: terminal `success_rate` (`None`→`—` when zero terminal), `avg_duration_ms` (NULL-skip), `cost_total`, `context_avg`, `tool_call_total` (parsed over the newest 5,000 rows agent-wide, `tool_calls_sampled` flag), and last-run outcome. Backs **BOTH** the Overview "Schedules performance" section (`OverviewPanel.vue`) AND the Schedules-tab inline mini-stats (`SchedulesPanel.vue`) from **one** `executions.js` `fetchSchedulesSummary(name, window)` fetch (cached per `${name}:${window}`, never polled) — no N round-trips. Route **declared before `/{id}`** in `routers/schedules.py` so the literal `analytics-summary` isn't captured as a `schedule_id` (Invariant #4). DB: `db/schedules.py:get_agent_schedules_summary` generalises the #868 per-schedule query. `tests/unit/test_1115_schedules_summary.py`. | [agent-overview-dashboard.md](feature-flows/agent-overview-dashboard.md), [scheduling.md](feature-flows/scheduling.md) |
| 2026-06-19 | #1231 | fix(agent): make the agent `/tmp` tmpfs size configurable via **`AGENT_TMP_SIZE`** (env on the backend service, which builds the agent mount spec), default `512m` — replacing the hardcoded `100m`. Validated `^\d+[mg]$` with empty/invalid → default; `noexec,nosuid` stay fixed (only size is tunable, and it stays bounded — counts against the agent memory cgroup). `AGENT_TMPFS_MOUNT` in `services/agent_service/capabilities.py` is the single source of truth, so create (`crud.py`) and recreate (`lifecycle.py`) can't drift. Closes the gap (built on #1098 TMPDIR redirect) where tools that hardcode `/tmp` (e.g. `gh` CLI install artifacts ~38 MB) exhaust the cap and silently break later `/tmp` writes incl. git commit scratch → autonomous scheduled runs "complete" but fail to persist. Wired `AGENT_TMP_SIZE=${AGENT_TMP_SIZE:-512m}` on the backend service in both compose files + `.env.example`. Creation-time: existing agents pick up a new size on recreate, not restart. `tests/unit/test_1231_agent_tmp_size.py`. | [container-capabilities.md](feature-flows/container-capabilities.md) |
| 2026-06-19 | #1083 | feat(exec): fire-and-forget dispatch — a hung autonomous turn holds **zero** backend coroutine/slot beyond its lease. Eligible `{schedule,webhook}` turns are dispatched to the agent with a **202 ACK** (Claude-runtime only) and finalized via a new callback `POST /api/agents/{name}/executions/{id}/result`; `execute_task` returns right after the ACK and hands the slot to the callback (or the TTL reaper). Flag `DISPATCH_ASYNC` (default OFF) + non-202 sync fallback keep mixed fleets safe. Core: `apply_result` — the single terminal applier shared by the sync path and the callback, **CAS-gating every side-effect** (activity/breaker/slot-release) so a replayed/late callback is a no-op (+ `slot_service` drain gated on ZREM, no over-admit, Codex #12); a **durable `dispatched_async` marker** the callback finalizes only (fail-closed cross-path guard); agent-side persist-to-disk + retry-to-lease + startup re-send (`result_callback.py`); lease reaper FAILs with `LEASE_EXPIRED` + closes the activity; stale-sweep buffer fix (per-agent `timeout+SLOT_TTL_BUFFER`). Hardened callback (MCP-key auth + ownership + marker + 413 caps + idempotent replay). v1 boundaries: lease-expiry=FAIL, async empty-result=FAIL, SUB-003 inline-only; 504/503 metadata parity is the #1201 fast-follow. ~120 unit tests (apply_result golden + CAS matrix, callback auth/marker/replay/size, dispatch-return, lease reaper boundary, agent result-callback). | [task-execution-service.md](feature-flows/task-execution-service.md), [capacity-management.md](feature-flows/capacity-management.md), [cleanup-service.md](feature-flows/cleanup-service.md) |
| 2026-06-19 | #1082 | refactor(reliability): status-as-projection — `schedule_executions.status` is codified as a **CAS-guarded projection** of an execution's terminal event; no reader treats `status='running'` as the standalone authority for "is running" (the agent process registry is). **Step 1** closes the one CAS gap: `db/schedules.py:update_execution_to_queued` (overflow re-queue) gained `AND status == RUNNING`, mirroring `release_claim_to_queued`, so a stale/duplicate re-queue can no longer resurrect a terminal row into `queued` (the E-02 phantom-reversal class); the `backlog_service.enqueue` caller treats the now-possible `False` ("gone or already terminal") as a clean rejection. **Step 2** adds a regression guard in `tests/unit/test_schedule_status_observability.py` — a static AST guard enumerates every `update(schedule_executions)` site, asserts the inventory matches a curated allowlist (a new blind write fails CI until classified) and that every status writer carries a `schedule_executions.c.status` predicate, plus behavioural no-op proofs that drive a row to each terminal state. **Step 3** reader audit: all `status='running'` readers are authority-crosschecking (cleanup watchdog candidate filters, confirmed against the agent registry / Redis before any destructive write — `#1082` comments added at the 3 sites), race-guard writers (the projection guard itself), or read-only display; **none act destructively on status alone**. **Step 4** downgrades canary S-01 `critical`→`major` (redundant under single-owner status; retires with the slot ZSET in #1081 Phase 5). No schema/endpoint/model change. | [status-as-projection.md](feature-flows/status-as-projection.md), [cleanup-service.md](feature-flows/cleanup-service.md), [persistent-task-backlog.md](feature-flows/persistent-task-backlog.md) |
| 2026-06-18 | #1160 | fix(db): atomic table rebuilds + cross-process lock in the SQLite migration runner — `_atomic_rebuild` (rename-swap inside an explicit `BEGIN`/`COMMIT`) closes the DROP-rebuild data-loss window, a stdlib `flock` (`db/migration_lock.py`) serialises the suite across uvicorn workers + scheduler, and a failed migration is named on its traceback and surfaced as `/health` `first_pending` (503). | [database-migration-runner.md](feature-flows/database-migration-runner.md) |
| 2026-06-14 | #1187 | feat(agent-runtime): **OpenAI Codex** as the third agent runtime ("harness == runtime") alongside Claude Code + Gemini. New `codex_runtime.py` implements `AgentRuntime` on the existing per-runtime primitives (NOT a shared helper → never inherits Gemini's blanket `kill_cgroup_orphans()`): `codex exec --json` JSONL parse (`thread.started`/`turn.completed.usage`/`item.completed`; `reasoning_output_tokens` ⊂ `output_tokens`, no double-count), `-o/--output-last-message` authoritative result (read-then-delete in `finally`), `codex exec resume <thread_id>` continuity, estimated cost (`CODEX_PRICING`), concurrency-safe `_drain_bounded` orphan cleanup, error→HTTP (auth **503** / rate **429** / runtime-unavailable **500**-not-503 / pipe-drop **502**). **Safety parity** (blocking): platform system-prompt prepended + `CLAUDE.md`→`AGENTS.md` mirror, **sandbox** normal→`--sandbox danger-full-access` (drops Codex's inner bwrap sandbox — it can't create a user namespace in the hardened container, which blocks every tool — leaving the Trinity container as the sole boundary, same as Claude) / read-only→`--sandbox read-only` (reads `~/.trinity/read-only-config.json`; fail-closed enforcement a fast-follow), guardrails honored/surfaced, credential sanitizer over output+logs. **Runtime-aware platform prompt** (E2E fix): Codex strips the Claude-only `mcp__trinity__` tool prefix (else the model emits `unknown MCP server`) via `platform_prompt_service.{get_platform_system_prompt,compose_system_prompt}(runtime=…)`, threaded from `chat.py` + `task_execution_service.py` through the `trinity.agent-runtime` label (`docker_service.get_agent_runtime`). New `RuntimeCapabilities` dataclass + per-runtime `capabilities()`; `get_runtime()` validates `AGENT_RUNTIME` and fails loud on unknown. Codex skips Claude-subscription assign (`is_claude_runtime`, crud+lifecycle); `OPENAI_API_KEY` read from `.env`; `CODEX_HOME` relocated under `$TMPDIR` + gitignored. MCP via `$CODEX_HOME/config.toml` (`bearer_token_env_var` — token never persisted). Session tab gated off via one backend constant (`RUNTIMES_WITHOUT_SESSION_TAB_RESUME`); frontend `RuntimeBadge`/default-model/terminal map + hidden Session tab; `test-codex` template; `@openai/codex@0.139.0` pinned. E2E-verified live (boot/chat/cost/resume/read-only/no-leak); resume arg-order bug fixed (exec flags must precede the `resume` sub-subcommand). | [codex-runtime.md](feature-flows/codex-runtime.md) |
| 2026-06-14 | #1022 | fix(scheduler): persist a descriptive `error` on dispatch timeout — a dispatch `httpx.TimeoutException` (whose `str()` is `''`) previously landed in the cron path's generic handler and persisted a **blank** `error`. Now re-raised before that handler as a named non-blank message (`"dispatch to /api/internal/execute-task timed out after {N}s — outcome unknown"`); outcome is genuinely UNKNOWN (backend spawns the bg task before replying → may already be running → orphan recovered by cleanup). New `_describe_exception()` helper (type-name fallback) normalizes any blank-stringifying exception across all execution/retry/process-schedule error paths. Dispatch + pre-check HTTP deadlines lifted from literals to config: `DISPATCH_TIMEOUT` (default 30s) and `PRE_CHECK_TIMEOUT` (default 70s). Scheduler-only (`src/scheduler/`); +270 lines of tests (incl. pre-check config-deadline + retry-path blank-error regressions). | [scheduler-service.md](feature-flows/scheduler-service.md), [scheduler-pre-check.md](feature-flows/scheduler-pre-check.md) |
| 2026-06-13 | #1089 | feat: subscription token rotation via **hot-reload, not container recreate** — a dedicated agent-server `POST /api/credentials/reload-token` mutates the running container's `CLAUDE_CODE_OAUTH_TOKEN` env so the next claude subprocess uses the rotated token while in-flight turns finish (closes the #1037 collateral-kill class). Three producer paths converted under the #799 `agent_switch_lock`: auto-switch (SUB-003), manual sub→sub reassignment (auth-mode changes still recreate), and key-rollover fan-out on `POST /api/subscriptions` upsert. Durable writable-layer override (`/var/lib/trinity/oauth-token` + `startup.sh` read) survives a plain restart; recreate self-reconciles to the DB token. Falls back to the old `_restart_agent` recreate on a 404 (old base image). | [subscription-auto-switch.md](feature-flows/subscription-auto-switch.md) |
| 2026-06-11 | #858 | fix: first-time setup token silently lost — `docker/backend/Dockerfile` had drifted and lost `ENV PYTHONUNBUFFERED=1` (which `docker/scheduler/Dockerfile` still set), so CPython block-buffered the lifespan's stdout to the Docker log pipe (~8KB) and the printed setup token never reached `docker logs`, deadlocking fresh installs (the only documented path through the `routers/setup.py` token gate). Two-layer fix: (1) restore `PYTHONUNBUFFERED=1` (catches every `print()`); (2) the setup-token block + ~76 other lifespan `print()` calls now emit via the structured `logger` — the token as a single multi-line `logger.warning` **relocated to immediately after `setup_logging()`**, before the event-bus/audit-write startup that could otherwise hang and suppress it (the `StreamHandler` flushes per record, so it's immune to future Dockerfile drift and flows through Vector). `setup_opentelemetry()`'s import-time print + the `register_enterprise` prints stay `print(..., flush=True)` (they run before `setup_logging()`). New `unit/test_858_dockerfile_unbuffered.py` backend↔scheduler parity guard (2 tests). Note: stdout→stderr stream move for the converted lines (Docker/Vector capture both). Known follow-up #1165: prod runs uvicorn `--workers 2`, so the per-process token is still ~50% flaky until unified. | [first-time-setup.md](feature-flows/first-time-setup.md) |
| 2026-06-10 | #1130 | fix: retired `gemini-2.0-flash` replaced with env-configurable models — `GEMINI_TEXT_MODEL` (image-gen prompt refinement) + `GEMINI_TRANSCRIPTION_MODEL` (Telegram voice), both default `gemini-3.5-flash`, defined in `config.py`, empty-string-safe wiring in both compose files (#1076 pattern). | [image-generation.md](feature-flows/image-generation.md), [telegram-integration.md](feature-flows/telegram-integration.md) |
| 2026-06-10 | #1108 | feat(ui): Agent Detail **Guardrails** tab renamed to **Settings** — sectioned config home. New `components/settings/SettingsPanel.vue` renders `GuardrailsPanel` unchanged as section #1; future per-agent settings land as additive sections, not new tabs. `?tab=guardrails` deep links alias to `settings` via `TAB_ALIASES`. Pure frontend. | [agent-guardrails.md](feature-flows/agent-guardrails.md) |
| 2026-06-10 | #1114 | feat(ui): Agent Detail tabs overflow into a **"More ▾"** dropdown instead of horizontal scroll. New reusable `components/OverflowTabs.vue` ("priority+" pattern): a hidden, zero-layout mirror row measures every `{id,label,badge?}` tab's width (+ a worst-case "More" button) so the visible row renders as many tabs as fit and collapses the trailing remainder into a right-aligned disclosure menu. Re-measures on container resize (`ResizeObserver` on the outer wrapper, width-diff-guarded + rAF-debounced) and after `document.fonts.ready`; re-measures on tab/label/badge changes via a derived-signature `watch` (`flush:'post'`). Defaults to all-inline before the first measure (no first-paint snap; no "More" when everything fits). Active-in-overflow reflected on the trigger (active underline + dot), tab order never reshuffled. Plain `<button>` disclosure (NOT `role="menu"`): Tab traverses, Escape closes + returns focus, outside-`pointerdown` closes; dark-mode aware. `v-model` over `AgentDetail`'s `activeTab` string ref, so `?tab=` deep-linking is unaffected. Pure frontend; no backend/store changes. Generic enough for `Operations.vue` to adopt next. 6 Playwright e2e behaviors. | [agent-detail-tab-overflow.md](feature-flows/agent-detail-tab-overflow.md), [agent-overview-dashboard.md](feature-flows/agent-overview-dashboard.md) |
| 2026-06-09 | #1109 | refactor(ui): unify Health + Ops + Executions into a single **Operations** nav area — the three separate top-nav entries (Health `/monitoring`, Ops `/operating-room`, Executions `/executions`) collapse into one **Operations** link (`views/Operations.vue`, route `/operations`), a `?tab=`-driven 5-tab view: **Needs Response · Notifications · Health · Executions · Resolved**. `views/OperatingRoom.vue`→`Operations.vue`; Health/Executions content extracted into tab-embeddable `components/MonitoringPanel.vue`/`ExecutionsPanel.vue` (mirroring the #1107 `*Panel.vue` pattern), and the standalone `views/Monitoring.vue`/`views/Executions.vue` deleted. Tabs toggle by `v-if` so each panel's store-owned polling tears down on leave; operator-queue polling stays container-level. **Health** tab is admin-gated at the tab level (`authStore.role === 'admin'`) with non-admin `?tab=health` deep links coerced to the default tab. NavBar carries **one** unified badge (operator-queue + notifications, critical-pulse); the separate Executions running-count badge is dropped (running count lives in the Executions tab strip). Legacy `/monitoring`→`?tab=health`, `/executions`→`?tab=executions`, `/operating-room` (function-form, query-preserving), and `/events`→`?tab=notifications` all redirect. Per-execution detail route (`ExecutionDetail.vue`) unchanged. Pure frontend IA change; no backend endpoints change. | [operating-room.md](feature-flows/operating-room.md), [executions-dashboard.md](feature-flows/executions-dashboard.md), [agent-monitoring.md](feature-flows/agent-monitoring.md) |
| 2026-06-09 | #1107 | feat(ui): Agent Detail **Overview** dashboard as the default landing tab + Info-tab redesign. New `OverviewPanel.vue` leads with About, a compact "needs attention" count+link, and **multi-day trend charts** (executions stacked by user-facing type bucket, terminal success-rate %, duration avg+p95, context) over a 7/14/30-day selector — plus a health panel (current status + heartbeat + restart/OOM; uptime/latency trend lines clamped ≤7d by `agent_health_checks` retention), a recent-activity drill-in, and footprint chips. Strict **non-duplication** of `AgentHeader` (it owns now+cost; Overview owns trend). Charts: `StackedBarChart.vue` (CSS/flexbox — not uPlot bars) + `TrendLineChart.vue` (uPlot, dark-mode-aware, custom cursor tooltip replacing the layout-shifting built-in legend); analogous-cool design-system palette. New agent-scoped analytics endpoint `GET /api/agents/{name}/analytics?window=` (`routers/analytics.py` → `db/schedules.py:get_agent_analytics`) generalises the #868 per-schedule query: `triggered_by` bucketing (+`Other` fallback), **full-set AVG vs sampled p95** correctness, terminal success rate, NULL-skipping context AVG, gap-filled UTC timeline. `executions.js` caches per `${name}:${window}`, never polled. `InfoPanel.vue` leads with About; `template.yaml` metadata behind a collapsible "Technical details". 12 unit tests. | [agent-overview-dashboard.md](feature-flows/agent-overview-dashboard.md), [agent-info-display.md](feature-flows/agent-info-display.md) |
| 2026-06-06 | #1080 | feat: model-list refresh — **Claude Opus 4.8** (`claude-opus-4-8`) added as flagship across `ModelSelector.vue` PRESET + admin platform-default dropdown (`Settings.vue`); the two models retiring 2026-06-15 (`claude-opus-4-20250514`, `claude-sonnet-4-20250514`) removed from presets; current/legacy tiers relabeled. `TasksPanel.vue` localStorage fallback bumped legacy→`claude-sonnet-4-6`. MCP `model` param examples refreshed (`chat.ts`/`schedules.ts`/`loops.ts`). Backend/agent-server defaults already current (`claude-sonnet-4-6`, `claude-haiku-4-5-20251001`) — unchanged, no base-image rebuild. Graceful degradation: a removed preset stays valid free-text until Anthropic's retirement date, then fails with a clear execution error (not silently). Both model test files refreshed. | [model-selection.md](feature-flows/model-selection.md), [scheduling.md](feature-flows/scheduling.md) |
| 2026-06-04 | VOIP-001 (#1056) | VoIP telephony Phase 1 (outbound) — an agent places a phone call over the **unmodified** Gemini Live bridge via Twilio Programmable Voice + Media Streams. New OSS three-layer module (`routers/voip.py` → `services/voip_service.py` → `db/voip.py`) + `voip_bindings`/`voip_call_logs` tables; a Media Streams WS adapter (`adapters/transports/twilio_media_stream.py` + pure `voip_audio.py`) with stateful `audioop` resampling (direct 8k↔16k/24k→8k), paced 160-byte μ-law sender, `clear`-on-barge-in; outbound trigger (REST + MCP `call_user`) with per-`(owner,dest)` rate limit + durable daily cap + Idempotency-Key; call-bound single-use WSS ticket (`ws_ticket_service` gains `ttl_seconds`); two-id namespace (`call_id` vs `vs_`); Redis staged-intent `GETDEL` consume-once; SETNX-guarded transcript save (`source="voice"`); **default-on post-call transcript processing** dispatched to the main agent (`execute_task(triggered_by="voip")`). Feature flag `voip_available` **OFF by default**. `voip_*` tables registered in the agent cascade registry. 27 unit tests; `audioop-lts` pinned for py≥3.13. | [voip-telephony.md](feature-flows/voip-telephony.md) |
| 2026-06-02 | #1033 | Backend prod-image packaging guard — `redis_breaker_util.py` (added by #526) was never added to the Dockerfile's **enumerated** `COPY` list, so it was silently absent from the baked prod image and crash-looped the backend on boot (`ModuleNotFoundError`). No CI caught it: `container-security.yml` boots the **dev** stack (bind-mounts `src/backend`, masking the missing COPY), unit tests run against source, and `docker-compose.prod.yml` has no bind-mount so it only bit on real deploys. Fix: collapse the seven enumerated top-level `COPY` lines to one glob (`COPY ../../src/backend/*.py /app/`) so a new top-level module can never be dropped again; relocate the stray `src/backend/staging-acceptance.py` (#678 test) → `tests/integration/staging_acceptance.py` so the glob only bakes runtime modules. New `backend-image-smoke.yml` CI job exercises the **baked prod image** (no dev bind-mount): asserts the module is present, runs the exact `redis_breaker_util → services.agent_client → main` import chain, and boots `redis`+`vector`+`backend` to a `/health` gate. Path-filtered (not label-gated), least-priv `contents: read`. Server-side counterpart to the `/verify-local` skill. | [backend-image-packaging.md](feature-flows/backend-image-packaging.md) |
| 2026-06-02 | RELIABILITY-006 (#525) | Idempotency keys at trigger boundaries — optional `Idempotency-Key` header at every execution-creating boundary, one execution per `(scope,key)` in 24h, duplicates replay original result + `X-Idempotent-Replay: true` (409 if in-flight), fail-open; new `idempotency_keys` table; Architectural Invariant #18 | [idempotency-keys.md](feature-flows/idempotency-keys.md) |
| 2026-06-02 | GUARD-001/002/003 | Agent guardrails — defense-in-depth safety layer baked into every agent container. Platform baseline (`guardrails-baseline.json`, root-owned 0444): bash deny-list regex, file path deny-list globs, credential output scanner (log-only), turn/timeout/disallowed-tools budgets. Owner-only narrow overrides via `GET/PUT /api/agents/{name}/guardrails` (numeric caps + literal deny lists only; regex + credential scanner not overridable). Injected as `AGENT_GUARDRAILS` env on create/restart, merged into `/opt/trinity/guardrails-runtime.json` by root at boot, enforced via Claude Code PreToolUse/PostToolUse hooks + `--max-turns`/`--disallowedTools` CLI flags. Fail-closed throughout. Owner-only Guardrails tab in Agent Detail (`GuardrailsPanel.vue`, #967/#992) edits the turn caps; deny-lists/timeout remain API-only. | [agent-guardrails.md](feature-flows/agent-guardrails.md) |
| 2026-05-30 | #526 | feat(reliability): per-agent **dispatch** circuit breaker (RELIABILITY-007) — producer-side breaker that fast-fails new executions with HTTP 503 (`X-Circuit-Open`/`Retry-After`) when an agent is auth-dead, instead of poisoning the persistent backlog. New `services/dispatch_breaker.py` (consecutive-failure machine, `agent:dispatch:{name}`, **AUTH-only** counting per D10, default threshold 3) reusing the proven `CircuitState` Lua pattern; shared Redis plumbing extracted to top-level `redis_breaker_util.py` (R1: keeps `agent_client`'s standalone-loaded test suites green). `CapacityManager.acquire(breaker_enabled=…)` raises `CircuitOpen` BEFORE the overflow branch (no-enqueue invariant); `task_execution_service` records outcomes at the terminals and backgrounds `db.fail_queued_for_agent` (→FAILED) + audit on `→open`; 60s `run_maintenance` re-drain backstop. Unified `GET/PUT /api/agents/{name}/circuit-breaker` + reset-both; health + `/slots` badge field (pipelined HGETALL, no SCAN); distinct ⚡ "circuit open" badge in `AgentNode`/`AgentHeader`. Per-agent `circuit_breaker_enabled` (default OFF) + global `DISPATCH_BREAKER_ENABLED` master switch = opt-in canary. Exposes `record_failure("missed_heartbeat")` as the #307 seam. 17 unit + 15 integration + 2 fail_queued tests; R1/R2/R3 guards; Playwright badge spec. | [dispatch-circuit-breaker.md](feature-flows/dispatch-circuit-breaker.md), [capacity-management.md](feature-flows/capacity-management.md), [task-execution-service.md](feature-flows/task-execution-service.md) |
| 2026-05-30 | RELIABILITY-004 (#307) | Agent push-heartbeat liveness layer — agent-side 5s loop (`agent_server/heartbeat.py`, gated on `TRINITY_BACKEND_URL`+`TRINITY_MCP_API_KEY`, sleeps-first, swallows all exceptions) POSTs `{memory_mb, active_executions, uptime_s}` to `POST /api/agents/{name}/heartbeat`. Option-B auth: the agent's own agent-scoped MCP key, validated with `track_usage=False` so a 12×/min beat doesn't amplify `usage_count`. Backend `heartbeat_service.py` owns the Redis key family (`agent:heartbeat:{name}` 15s SETEX, persistent `seen` backward-compat hinge, `misses` counter); `heartbeat_status_bulk` (one pipelined round-trip) annotates `GET /api/monitoring/status` with five `heartbeat_*` fields. New backend watch loop (5s, staggered +10s) fires a soft, cooldown-debounced `alert_heartbeat_lost`/`recovered` via `monitoring_alerts` only on the alive→stale (and recovery) transition after a 3-miss guard — writes no health row; the 30s monitoring loop stays authoritative. `clear_heartbeat()` wired into agent delete + rename (the no-TTL `seen` key would otherwise leak). Old-image agents resolve to `unsupported` and are ignored. 5 test files (~1228 lines). | [agent-heartbeat-liveness.md](feature-flows/agent-heartbeat-liveness.md), [agent-monitoring.md](feature-flows/agent-monitoring.md) |
| 2026-05-29 | #950 | deploy-local deferred hardening — `is_trinity_compatible()` now **requires** a non-empty, UTF-8-readable `CLAUDE.md` (blocking 400 `NOT_TRINITY_COMPATIBLE`, previously a non-fatal warning; binary/non-UTF-8 yields a clean 400 not a 500). New `collect_mcp_credential_warnings()` scans `.mcp.json.template`/`.mcp.json` for `${VAR}` refs absent from the post-merge `.env` and not platform-injected (static allowlist mirroring `crud.py`), returning them as advisory `DeployLocalResponse.warnings[]` (also added to the MCP `deploy_local_agent` response type). Docs reconciled: `credentials` request field + `MAX_DEPLOY_CREDENTIALS`, dedicated credential-merge step, `DEPLOYED_TEMPLATES_DIR_UNWRITABLE`/`WORKSPACE_PREPOP_FAILED` codes, `require_role("creator")`. 3 unit-test files. | [local-agent-deploy.md](feature-flows/local-agent-deploy.md), [template-processing.md](feature-flows/template-processing.md) |
| 2026-05-25 | #914 | MCP `chat_with_agent` gateway-timeout receipt — `TrinityClient.chat()` wraps the backend fetch in an `AbortController` bounded by `MCP_CHAT_TIMEOUT_MS` (default 25000ms, under the typical 30-60s MCP gateway ceiling). On abort, `findRecentMcpExecution` queries `/api/agents/{name}/executions` and returns `{status:"queued_timeout", agent, execution_id, message}` so callers poll `get_execution_result` instead of triggering Trinity's concurrent-duplicate guard on retry. New `pickRecentMcpExecution` exported pure helper + 9 `node:test` cases. Live-verified through the FastMCP JSON-RPC transport. Companion to #418 (per-agent execution_timeout enforcement) and the MCP-client surface of #408/#428's long-running dispatch family. | [mcp-orchestration.md](feature-flows/mcp-orchestration.md) |
| 2026-05-25 | #912 | fix(orphan-sweep): drain-time cgroup sweep now forwards an allowlist of in-flight execution pids/pgids so concurrent legitimate claude subprocesses don't get SIGKILLed when a sibling task drains in the same agent cgroup. Single canonical source via `ProcessRegistry.active_execution_pids(exclude_execution_id=…)` — used by the periodic orphan sweeper (#817), `ProcessRegistry.terminate()`, and the new `subprocess_pgroup._active_execution_pids_for_drain()` helper. Fixes the silent SIGKILL of multi-minute tasks visible as "exit code -9 / 0 tool calls / 0 turns" whenever any other task finished in the same container. 8 unit tests + in-container behavioural check. | [execution-termination.md](feature-flows/execution-termination.md), [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) |
| 2026-05-22 | #868 | feat(analytics): per-schedule execution analytics — `GET /api/agents/{name}/schedules/{id}/analytics` (24h / 7d / 30d) returns counts, success rate, duration p50/p95/p99 (Python `statistics.quantiles`, capped 5000-row pool), cost total, tool-call top-5 weighted by total wall time, UTC daily timeline with gap-fill; `ScheduleAnalyticsCard.vue` (pure-CSS, no Chart.js) inline in `SchedulesPanel.vue` expanded row, threshold-ladder stat tiles, sampled badge; tenant boundary in DB layer (`agent_name` required); per-agent rollup deferred to #18, per-chat-session deferred. 12 unit tests. | [scheduling.md](feature-flows/scheduling.md) |
| 2026-05-20 | #740 | feat: `run_agent_loop` MCP tool + backend loop service — sequential bounded task execution with `{{run}}`/`{{previous_response}}` substitution, optional `stop_signal` early exit, graceful stop. New `agent_loops` + `agent_loop_runs` tables, `loop_id` column on `schedule_executions` for timeline tagging. Cleanup-service startup hook flips orphaned loops to `interrupted`. 16 unit tests. | [run-agent-loop.md](feature-flows/run-agent-loop.md) |
| 2026-05-18 | #887 | fix(read-only): guard moved to base image (`/opt/trinity/hooks/`, root-owned 0555); MultiEdit bypass fixed; fail-closed via `run_hook()`; lifecycle always syncs config on start (stale-volume fix); config file protected by `path_deny` + `bash_deny` in guardrails-baseline.json; 18 unit tests | [read-only-mode.md](feature-flows/read-only-mode.md) |
| 2026-05-18 | #888 | write_user_memory MCP tool — per-user memory write with server-side email resolution, fixing PII cross-user memory leak | [write-user-memory.md](feature-flows/write-user-memory.md) |
| 2026-05-17 | #35d4e78 | fix(credentials): map agent-server connect errors to 503 on `import_credentials` and `export_credentials` — `httpx.RequestError` (ConnectError/TimeoutException/ReadError) now surfaces 503 instead of 500 when the agent container is up but its FastAPI server isn't reachable yet. Mirrors the inject/agent-files pattern. | [credential-injection.md](feature-flows/credential-injection.md) |
| 2026-05-17 | #862 | fix(cleanup): execution retention sweeps were no-ops — `prune_execution_logs`/`prune_execution_rows` queried `status IN ('completed','failed','terminated')` but `TaskExecutionStatus` uses `'success'/'failed'/'cancelled'/'skipped'`; only `'failed'` rows ever pruned; fixed SQL predicates + `idx_executions_completed_terminal` partial index + migration to drop/recreate existing wrong index on live installs | [cleanup-service.md](feature-flows/cleanup-service.md) |
| 2026-05-15 | EXEC-022 (#18) | Unified Executions Dashboard — `GET /api/executions` + `/api/executions/stats` fleet-level endpoints with per-agent access control; `/executions` Vue page with stat cards, filter bar, running strip, load-more; NavBar running-count badge; 30s polling + WS refresh guard | [executions-dashboard.md](feature-flows/executions-dashboard.md) |
| 2026-05-13 | #586 | obs(agent-runtime): `[METRIC] drain_outcome` emissions on the slow path of `drain_reader_threads` — two reachable sites surface `outcome=natural`/`force_close`/`leaked`, `stuck_initial`, `drain_elapsed_ms`, optional `leaked_count`, plus vestigial `orphan_kill_count=0` (since the #817 cgroup-sweep refactor, actual orphan counts are logged separately as `Cgroup sweep killed N orphan(s)`). Fast path stays silent. New Stop-hook authoring guidance in `TRINITY_COMPATIBLE_AGENT_GUIDE.md` shows how to release the inherited stdout FD before blocking I/O so hooks avoid the slow path entirely. Fleet audit at `scripts/586-fleet-check.sh` gates close-out by scanning Vector agent logs for residual "still stuck after Ns" / "no result message after" events. 2 new unit tests. | [execution-termination.md](feature-flows/execution-termination.md) |
| 2026-05-13 | #602/#830 | sec: drop SYS_PTRACE / MKNOD / NET_RAW / FSETID from `FULL_CAPABILITIES` (Phase 3c). SYS_PTRACE closes the AISEC-C2 heap-read OAuth-exfil path. FULL set is now 9 caps (was 13). Constants extracted to stdlib-only `services/agent_service/capabilities.py`; `lifecycle.py` re-exports. | [container-capabilities.md](feature-flows/container-capabilities.md), [agent-lifecycle.md](feature-flows/agent-lifecycle.md) |
| 2026-05-13 | #831 | feat: platform default model — admin sets `platform_default_model` in Settings General tab; `task_execution_service.execute_task()` resolves `model=None` → platform default (TTL-cached, write-through invalidation); `GET /api/settings/feature-flags` exposes value for frontend; SchedulesPanel shows "platform default (X)" when no model set; PRESET_MODELS updated to canonical Anthropic list (Opus 4.7 / Sonnet 4.6 / Haiku 4.5) | [model-selection.md](feature-flows/model-selection.md), [platform-settings.md](feature-flows/platform-settings.md), [task-execution-service.md](feature-flows/task-execution-service.md) |
| 2026-05-12 | #808 | fix(orphan-killer): `_set_idle_priority()` (SCHED_IDLE/nice) + `_scan_deadline` 8s per-iteration budget — prevents orphan-killer daemon thread from starving uvicorn health probes on 1-CPU containers and triggering circuit breaker | [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) |
| 2026-05-12 | #474 | fix(circuit-breaker): only TCP unreachability counts toward the circuit — `agent_client._request()` now classifies via shared `is_circuit_failure()` helper backed by `CIRCUIT_FAILURE_EXCEPTIONS` (`ConnectError`, `ConnectTimeout`). `TRANSIENT_TRANSPORT_EXCEPTIONS` (`ReadTimeout`/`WriteTimeout`/`PoolTimeout`/`WriteError`/`ReadError`/`RemoteProtocolError`) still raise `AgentNotReachableError` but no longer increment the 3-failure threshold; raw `OSError` subclasses (`BrokenPipeError`/`ConnectionResetError`) propagate uncaught; `asyncio.CancelledError` is re-raised explicitly. `monitoring_service.check_network_health()` lazy-imports the same tuples so the /health probe and inline `/api/*` agree on what "unreachable" means; any HTTP response (200..599) records success — symmetric with `_request()` so stale counters clear. `aggregate_health()` adds explicit `status_code >= 500 → UNHEALTHY` branch so a wedged-but-listening agent isn't silently HEALTHY under the new rule. 12 unit + 13 integration tests on the classifier + 1 new monitoring-service integration suite. | [agent-monitoring.md](feature-flows/agent-monitoring.md), [execution-queue.md](feature-flows/execution-queue.md), [scheduling.md](feature-flows/scheduling.md) |
| 2026-05-11 | #759 | fix(session-tab): reattach to in-flight turn after KeepAlive deactivation — new `session_inflight:{session_id}` Redis sentinel (covers cold + warm turns) drives `turn_in_progress` field on GET sessions/{id}; old static 300s lock TTL replaced with dynamic per-agent `execution_timeout + 30s` (capped 7230s) so >5-min turns don't drop the lock mid-flight; lock key extracted to `_session_lock_key()` helper. Frontend `inFlightBySession`/`errorBySession`/`fallbackNoticeBySession` move from local refs to Pinia store keyed by sessionId (fixes cross-agent state bleed); `onActivated` reattaches via polling (2s→5s→15s backoff, 60-attempt cap); optimistic insert dropped — `loadSession` is the canonical source. 13 AST/structural unit tests. Cold-turn JSONL race tracked separately as #779. | [session-tab.md](feature-flows/session-tab.md) |
| 2026-05-08 | #692 | sec/config: close `changeme` propagation paths — prod compose `ADMIN_PASSWORD`/`TRINITY_PASSWORD` switch to fail-loud `${VAR:?...}`; MCP server drops `\|\| "changeme"` fallback and throws on startup when no usable cred in legacy non-API-key mode; `gcp-deploy.sh` refuses to write `.env` when `ADMIN_PASSWORD` is unset or literally `"changeme"`; `deploy.config.example` drops `"changeme"` default; `.env.example` collapses duplicate `FRONTEND_URL` and adds `GOOGLE_API_KEY`, `LOG_*`, `TRINITY_DATA_PATH`, `HOST_TEMPLATES_PATH`. Default `MCP_REQUIRE_API_KEY=true` mode unaffected. | [mcp-orchestration.md](feature-flows/mcp-orchestration.md) |
| 2026-05-08 | #708 | fix(slack): startup recovery supervisor — when ALL initial Socket Mode connect attempts fail at backend boot (e.g. transient DNS slowness exceeds the 10s connect ceiling), `start()` now spawns a recovery supervisor that retries with the watchdog's exponential backoff (60→120→240→300s cap) until at least one client connects, then graduates to the per-client watchdog model. Pre-#708 behavior was a silent permanent-offline state with no retry path. `stop()` cancels the supervisor cleanly so admin-disconnect / shutdown stays bounded. main.py keeps the transport reference even on initial failure so the supervisor's task isn't orphaned. Bad-creds case (typo/expired/revoked token): supervisor retries forever but backend HTTP stays fully responsive — ERROR log "STARTUP UNREACHABLE" fires after 3 consecutive failures for operator paging. Bad-format token (no `xapp-` prefix) takes the existing early-return path — no supervisor, no retry. 10 new unit tests + 1 flipped existing test. | [slack-channel-routing.md](feature-flows/slack-channel-routing.md) |
| 2026-05-06 | #244 | fix(slack): multi-connection Socket Mode — N concurrent WebSockets (default 2, env `SLACK_SOCKET_CONNECTION_COUNT` clamped 1–10) per Slack's documented multi-connection guidance, eliminating the brief reconnect gap when one client half-closes. Each client gets an independent watchdog with its own backoff counter; envelope-ID dedup ring (OrderedDict cap 1024 + `asyncio.Lock`) defends against possible cross-connection duplicate delivery and emits a measurable `dedup_hits` counter. `is_connected` returns "any client healthy" and a new `connected_count` exposes degraded mode. 56 unit tests pass. Reconnect-call timeout deferred to #683. | [slack-channel-routing.md](feature-flows/slack-channel-routing.md) |
| 2026-05-05 | #453 | sec: Encrypt SLACK-001 bot tokens at rest — `db/slack.py` adopts the AES-256-GCM JSON-envelope pattern from `slack_channels.py`/`telegram_channels.py`/`whatsapp_channels.py`. New `_migrate_slack_bot_token_encryption` walks BOTH `slack_link_connections` and `slack_workspaces` and re-encrypts any plaintext `xoxb-*` rows. Read-path plaintext fallback keeps runtime working pre-migration. Architecture Invariant #12 reworded to acknowledge the documented exception (channel/subscription tokens persisted but mandatorily encrypted). 14 unit tests. Test backfill for slack_channels.py + TG + WA tracked in #664. | [slack-integration.md](feature-flows/slack-integration.md) |
| 2026-05-17 | #865 (revert SITE-001) | Remove agent website reverse-proxy (`routers/site.py`, `SITE_PORT`, nginx `/site/` block, frontend "Website" link-type option); `link_type='site'` creation now returns 400. DB schema column kept for SITE-002 companion redesign. | [public-agent-links.md](feature-flows/public-agent-links.md) |
| 2026-05-03 | #250 | Token usage display — per-agent cost/token stats (24h, 7d, lifetime) from `schedule_executions` DB, shown in AgentHeader as amber sparkline + today's cost + trend vs 7-day average | [token-usage-display.md](feature-flows/token-usage-display.md) |
| 2026-05-01 | SESSION_TAB_2026-04 | Session tab — `--resume`-default chat surface. New `agent_sessions`/`agent_session_messages` tables, six `/api/agents/{name}/sessions*` endpoints, `SessionPanel.vue` + `stores/sessions.js`, parser fix + `persist_session` plumbed through agent stack, resume-failure fallback, Redis lock per `(agent, claude_uuid)`, JSONL cleanup service, JSONL fallback recovery for stdout pipe race, JSONL-side compact event capture, validator canonical-trinity allowance. Default ON since GA 2026-05-04 (`session_tab_enabled` flag, settable to false to disable). | [session-tab.md](feature-flows/session-tab.md) |
| 2026-05-01 | #293 | fix(slack): replace `slackify-markdown 0.2.2` with own `services.slack_mrkdwn` renderer — fixes 5 layout bugs that produced "ugly" output: nested-list flattening, headings crammed against preceding content, blockquote `>` only on first line, raw-pipe table passthrough, dropped `---` rules. 35 unit tests + 13 ported. | [slack-channel-routing.md](feature-flows/slack-channel-routing.md) |
| 2026-04-29 | #584 | feat(slack): UI + API to change Slack DM-default agent — `set_slack_dm_default()` DB method (single-tx clear-then-set), `PUT /api/agents/{name}/slack/channel/dm-default` (owner-only, audit-logged), "Make default" button + tooltip in `SlackChannelPanel.vue`, unbind refuses 409 when target is DM default with siblings remaining | [slack-channel-routing.md](feature-flows/slack-channel-routing.md) |
| 2026-04-30 | #598 | sec: AISEC-C2 Layer 2 — restored `.mcp.json` post-deploy editing via structure validation (`services.mcp_validator`). Closed schema, command/transport allowlists, SSRF guard for http/sse, reserved env-ref blocklist, literal-secret detection. 88 unit tests + 22 integration tests. UI placeholder updated; `trinity` server name reserved. | [credential-injection.md](feature-flows/credential-injection.md) |
| 2026-04-30 | #590 | sec: AISEC-C2 Layer 1 — backend `ALLOWED_CREDENTIAL_PATHS` tightened; backend `update_agent_file_logic` adds defense-in-depth deny check before proxy; agent-server `EDIT_PROTECTED_PATHS` adds `.mcp.json` and `.credentials.enc`. | [credential-injection.md](feature-flows/credential-injection.md), [file-browser.md](feature-flows/file-browser.md) |
| 2026-04-30 | #364 | Web chat file upload — drag-drop/picker in ChatPanel and PublicChat; base64 JSON encoding; shared upload_service; images via vision blocks, non-images via Docker put_archive | [web-chat-file-upload.md](feature-flows/web-chat-file-upload.md) |
| 2026-04-27 | #539 | fix: public chat context duplication — `build_public_chat_context()` now called before `add_public_chat_message(role="user")`, preventing current message appearing twice in every agent prompt | [public-agent-links.md](feature-flows/public-agent-links.md) |
| 2026-04-26 | #428 | CapacityManager facade — single public surface for capacity (admit / release / overflow policy / status / reclaim) replacing ExecutionQueue + SlotService + BacklogService trio | [capacity-management.md](feature-flows/capacity-management.md) |
| 2026-04-26 | #516, #520 | Agent error classification — `_classify_signal_exit()` (504 for SIGINT/SIGKILL/SIGTERM, was misread as 503 auth) and `_classify_empty_result()` (502 when clean exit drops the final result message, was silent 200 + watchdog reap) | [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md), [task-execution-service.md](feature-flows/task-execution-service.md) |
| 2026-04-26 | #498 | Sync `/task` long-poll on backlog — sync parallel calls at capacity now spill to BACKLOG-001 (same backlog as async) and long-poll the open HTTP connection until terminal status (cap `2 × effective_timeout`); new `services/sync_waiter.py` owns the in-process registry + event/poll-fallback wait helper | [persistent-task-backlog.md](feature-flows/persistent-task-backlog.md), [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) |
| 2026-04-24 | FILES-001 (#295) | Outbound file sharing — per-agent opt-in publish volume, `share_file` MCP tool, public download URL (`?sig=` token), UI panel with toggle/list/revoke. Agents publish files to `/home/developer/public/`; backend extracts via Docker SDK `get_archive` on demand; URL format `/api/files/{id}?sig={token}` | [file-sharing-outbound.md](feature-flows/file-sharing-outbound.md) |
| 2026-04-24 | WEBHOOK-001 (#291) | Webhook triggers — token-authenticated public URL fires schedule executions | [webhook-triggers.md](feature-flows/webhook-triggers.md) |
| 2026-04-25 | #496 | Backlog drain spawn fix — repair `_spawn_drain` lazy import after #95 renamed `_execute_task_background` → `_run_async_task_with_persistence`; AST-based regression tests pin the contract | [persistent-task-backlog.md](feature-flows/persistent-task-backlog.md) |
| 2026-04-25 | #487 | Telegram file upload Phase 2 — workspace delivery hardening: NFKC sanitizer with collision dedup, spec injection format `[File uploaded by {uploader}]: {name} ({size}) saved to {path}`, all-writes-failed channel error + abort. Same code path benefits Slack inbound. | [telegram-integration.md](feature-flows/telegram-integration.md), [slack-file-sharing.md](feature-flows/slack-file-sharing.md) |
| 2026-04-23 | #476 | SQLite lexicographic cutoff bug fix — new `iso_cutoff(hours)` helper replaces `datetime('now', ...)` in 15 sites across rate-limit / dashboard / schedules; `max_retries` default flipped `1 → 0`; `cleanup_old_rate_limit_events` wired into `CleanupService` (phase 6, hourly) | [subscription-auto-switch.md](feature-flows/subscription-auto-switch.md), [cleanup-service.md](feature-flows/cleanup-service.md), [scheduler-service.md](feature-flows/scheduler-service.md) |
| 2026-04-22 | #458 | `.gitignore` init fix — `initialize_git_in_container` now appends missing patterns instead of truncate-and-write; adds `.env`, `.env.*`, `.mcp.json` to the default list and runs for both `/home/developer` and legacy `/home/developer/workspace` (stops credential leak on first GitHub sync) | [github-repo-initialization.md](feature-flows/github-repo-initialization.md) |
| 2026-04-22 | SCHED-COND-001 (#454) | Conditional schedule pre-check — backend `docker exec`s the template's executable `~/.trinity/pre-check` (language-agnostic, interpreter from shebang) before scheduler fires a cron chat; empty stdout + exit 0 records a skipped execution; fail-open; reuses `ExecutionStatus.SKIPPED` (no schema change, no HTTP edge from scheduler to agent) | [scheduler-pre-check.md](feature-flows/scheduler-pre-check.md) |
| 2026-04-21 | RELIABILITY-003 (#306) | WebSocket event bus on Redis Streams — replaces in-process broadcast with XADD/XREAD, adds reconnect replay via `?last-event-id=`, 3-failure eviction, MAXLEN trim (tunable) | [websocket-event-bus.md](feature-flows/websocket-event-bus.md) |
| 2026-04-20 | #420 | Scheduler sync loop fix — `update_schedule_run_times` no longer bumps `updated_at`, stopping the self-triggering re-register of every schedule per tick | [scheduler-service.md](feature-flows/scheduler-service.md) |
| 2026-04-20 | #418 | Inter-agent timeout honors per-agent `execution_timeout_seconds` — removed 600s hardcoded defaults in MCP `chat_with_agent`/`fan_out` tools and fan-out service; HTTP client ceiling bumped to platform max (7200s) | [fan-out.md](feature-flows/fan-out.md), [mcp-orchestration.md](feature-flows/mcp-orchestration.md), [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) |
| 2026-04-19 | #211 | Auto-propagate global GitHub PAT to running agents on update — per-agent PAT holders and agents without `GITHUB_PAT` in `.env` are skipped; delete does NOT propagate | [github-sync.md](feature-flows/github-sync.md), [platform-settings.md](feature-flows/platform-settings.md) |
| 2026-04-19 | #378 | Cleanup service Phase 3 just-in-time re-verify + parallel per-agent fan-out — eliminates phantom stale-slot failures for still-running tasks; adds residual-race observability log | [cleanup-service.md](feature-flows/cleanup-service.md) |
| 2026-04-19 | S4 (#383) | Persistent-state allowlist primitive — materialize `.trinity/persistent-state.yaml` at agent creation; readers with default fallback on backend + agent-server | [persistent-state-allowlist.md](feature-flows/persistent-state-allowlist.md) |
| 2026-04-19 | #389, #390 | Git sync health observability — auto-sync heartbeat, dual ahead/behind (P6 fix), `agent_sync_state` + `sync_failing` operator-queue, dashboard dot, `/api/fleet/sync-audit` with `duplicate_binding` flag | [git-sync-health.md](feature-flows/git-sync-health.md) |
| 2026-04-18 | #384 (S3) | Reset-to-main-preserve-state operation — snapshot persistent-state allowlist, reset to `origin/main`, overlay back, force-with-lease push; guardrails for agent-busy / no-git-config / no-remote-main | [github-sync.md](feature-flows/github-sync.md) |
| 2026-04-18 | DOCS-QA-001 | Trinity Docs Q&A — public Vertex AI Search endpoint + in-app floating help widget (#391) | [trinity-docs-qa.md](feature-flows/trinity-docs-qa.md) |
| 2026-04-17 | #376 | Proactive messaging UI toggle — SharingPanel shows allow_proactive switch per shared user | [proactive-messaging.md](feature-flows/proactive-messaging.md), [agent-sharing.md](feature-flows/agent-sharing.md) |
| 2026-04-16 | #321 | Proactive agent messaging — agents send messages to users by verified email via Telegram/Slack/web | [proactive-messaging.md](feature-flows/proactive-messaging.md) |
| 2026-04-16 | #354 | Telegram file upload Phase 1 — photo/document extraction, download, size/MIME validation | [telegram-integration.md](feature-flows/telegram-integration.md) |
| 2026-04-15 | #311 | Group auth mode — require at least one verified member before bot responds in Telegram groups | [unified-channel-access-control.md](feature-flows/unified-channel-access-control.md), [telegram-integration.md](feature-flows/telegram-integration.md) |
| 2026-05-26 | #941 | Audit trail Phase 5 — admin dashboard UI at `/enterprise/audit` (`Audit.vue` + `auditLog.js`), `/api/audit-log/distinct/{event-types,actor-types}` for filter dropdowns; entitlement-gated frontend route, backend stays OSS | [audit-trail.md](feature-flows/audit-trail.md) |
| 2026-04-14 | #20 | Platform audit trail (SEC-001) Phases 1–4 — append-only `audit_log` table, `PlatformAuditService`, admin query API at `/api/audit-log`, MCP tool-call audit, hash chain verify, CSV/JSON export | [audit-trail.md](feature-flows/audit-trail.md) |
| 2026-04-14 | #171 | Execution context injection — per-invocation metadata (mode/trigger/timeout/schedule/collaborators) added to every agent system prompt, with sanitization and operator kill-switch | [execution-context-injection.md](feature-flows/execution-context-injection.md) |
| 2026-04-23 | WHATSAPP-001 Phase 2 (#467) | WhatsApp access control — `/login`/`/logout`/`/whoami` commands, access-gate inlined into /login, `_deliver_whatsapp` explicit-channel proactive delivery with chunking, markdown→WhatsApp conversion | [whatsapp-integration.md](feature-flows/whatsapp-integration.md), [unified-channel-access-control.md](feature-flows/unified-channel-access-control.md) |
| 2026-04-22 | WHATSAPP-001 (#299) | WhatsApp via Twilio — per-agent binding, HMAC-SHA1 webhook, SSRF-gated media, encrypted AuthToken, DMs only | [whatsapp-integration.md](feature-flows/whatsapp-integration.md) |
| 2026-04-14 | VALIDATE-001 (#294) | Business task validation — post-execution clean-context auditor verifies task completion | [business-validation.md](feature-flows/business-validation.md) |
| 2026-04-14 | SELF-EXEC-001 (#264) | Agent self-execute — background task on itself during chat, optional result injection | [self-execute.md](feature-flows/self-execute.md) |
| 2026-04-13 | BACKLOG-001 (#260) | Persistent async task backlog — async `/task` spills to SQLite FIFO at capacity, drains on slot release, restart-durable | [persistent-task-backlog.md](feature-flows/persistent-task-backlog.md) |
| 2026-04-13 | #314 | Whitelist-driven role on first email login — fixes silent promotion to `creator` for cross-channel access grants | [email-authentication.md](feature-flows/email-authentication.md), [agent-sharing.md](feature-flows/agent-sharing.md), [cli-tool.md](feature-flows/cli-tool.md) |
| 2026-04-12 | #311 | Unified cross-channel access control — verified email as identity, per-agent policy, access requests | [unified-channel-access-control.md](feature-flows/unified-channel-access-control.md) |
| 2026-04-12 | #309 | Telegram webhook back-fill when `public_chat_url` is saved (self-healing config order) | [telegram-integration.md](feature-flows/telegram-integration.md) |
| 2026-04-11 | #297 | Telegram group chat support — @mention triggers, welcome messages, per-group config | [telegram-integration.md](feature-flows/telegram-integration.md) |
| 2026-04-10 | #296 | Telegram bot connection UI — TelegramChannelPanel in Agent Detail Sharing page | [telegram-integration.md](feature-flows/telegram-integration.md) |
| 2026-04-08 | #69 | Owner filter dropdown on Dashboard and Agents pages | [agents-page-ui-improvements.md](feature-flows/agents-page-ui-improvements.md), [agent-network.md](feature-flows/agent-network.md) |
| 2026-04-06 | QUOTA-001 | Per-role agent creation quotas with admin exemption | [agent-quotas.md](feature-flows/agent-quotas.md) |
| 2026-04-04 | DASH-001 | Dashboard reliability: DB-persisted cache, retry backoff, partial YAML tolerance, decoupled tab visibility | [dynamic-dashboards.md](feature-flows/dynamic-dashboards.md) |
| 2026-04-04 | CLI-001 | CLI UX: URL normalization + retry, table default output, tag-driven auto-versioning | [cli-tool.md](feature-flows/cli-tool.md) |
| 2026-04-01 | CLI-001 | Trinity CLI tool — Python Click CLI mirroring core MCP tools as shell commands | [cli-tool.md](feature-flows/cli-tool.md) |
| 2026-04-01 | SUB-004 | Per-subscription rolling token/cost usage windows (5h, 7d) across chat and executions | [subscription-usage-tracking.md](feature-flows/subscription-usage-tracking.md) |
| 2026-03-31 | #222 | Slack inbound file sharing — images via vision, text files via container copy (SLACK-FILES) | [slack-file-sharing.md](feature-flows/slack-file-sharing.md) |
| 2026-03-31 | TELEGRAM-001 | Telegram bot integration — per-agent bots, webhook transport, encrypted tokens | [telegram-integration.md](feature-flows/telegram-integration.md) |
| 2026-03-30 | FANOUT-001 | Fan-out parallel task dispatch and result collection | [fan-out.md](feature-flows/fan-out.md) |
| 2026-03-27 | #182 | Subscription BOLA fix — restrict assign/clear to owner/admin via `can_user_share_agent` | [subscription-management.md](feature-flows/subscription-management.md) |
| 2026-03-27 | SEC-179 | SSRF prevention — skills library URL validation against github.com allowlist | [skills-library-sync.md](feature-flows/skills-library-sync.md) |
| 2026-03-27 | #137 | Fix cleanup service: SQLite datetime format mismatch, skipped terminal state, empty session ID | [cleanup-service.md](feature-flows/cleanup-service.md) |
| 2026-03-26 | #189 | Password complexity requirements — OWASP ASVS 2.1 validation on admin setup | [first-time-setup.md](feature-flows/first-time-setup.md) |
| 2026-03-26 | EVT-001 | Agent Event Subscriptions — lightweight pub/sub for inter-agent pipelines | [agent-event-subscriptions.md](feature-flows/agent-event-subscriptions.md) |
| 2026-03-25 | #129 | Active watchdog — reconcile DB state against agent process registries, recover orphans, auto-terminate timeouts | [cleanup-service.md](feature-flows/cleanup-service.md) |
| 2026-03-25 | #148 | Fix silent subscription registration failure — encryption key auto-generation, status endpoint, frontend warning | [subscription-management.md](feature-flows/subscription-management.md) |
| 2026-03-25 | #76 | Configurable MCP Server URL in Admin Settings | [platform-settings.md](feature-flows/platform-settings.md), [api-keys-page.md](feature-flows/api-keys-page.md) |
| 2026-03-25 | #74 | Auto-assign subscription to new agents (round-robin, rate-limit aware) | [subscription-management.md](feature-flows/subscription-management.md) |
| 2026-05-07 | #699 | Voice Workspace (BETA) — full-page workspace with orb + canvas panel; panel tools (show_markdown/update_panel/append_to_panel/clear_panel); `voice_available` feature flag | [voice-chat.md](feature-flows/voice-chat.md) |
| 2026-03-23 | VOICE-001 | Voice Chat — real-time voice conversations with agents via Gemini Live API | [voice-chat.md](feature-flows/voice-chat.md) |
| 2026-03-23 | SLACK-002 | Channel adapter abstraction + multi-agent Slack routing | [slack-channel-routing.md](feature-flows/slack-channel-routing.md) |
| 2026-03-21 | SUB-003 | Auto-switch subscriptions on repeated rate-limit errors — setting, tracking, orchestration | [subscription-auto-switch.md](feature-flows/subscription-auto-switch.md) |
| 2026-03-20 | ROLE-001 | 4-tier role model (admin/creator/operator/user), require_role() helper, user management API + Settings UI | [role-model.md](feature-flows/role-model.md) |
| 2026-03-19 | CHAT-AC | Playbook autocomplete in chat input — slash-command dropdown, ghost text, arg hints | [playbook-autocomplete.md](feature-flows/playbook-autocomplete.md) |
| 2026-03-14 | MOB-001 | Mobile Admin — agent chat, autonomy toggle, task sending | [mobile-admin-pwa.md](feature-flows/mobile-admin-pwa.md) |
| 2026-03-14 | MOB-001 | Mobile Admin PWA — standalone `/m` page with Agents/Ops/System tabs, installable PWA | [mobile-admin-pwa.md](feature-flows/mobile-admin-pwa.md) |
| 2026-03-12 | TIMEOUT-001 | Per-agent configurable execution timeout (default 15 min), dynamic slot TTL | [task-execution-service.md](feature-flows/task-execution-service.md), [parallel-capacity.md](feature-flows/parallel-capacity.md) |
| 2026-03-12 | #90 | Fix stuck executions on slot acquisition failure — try block covers all execution steps | [task-execution-service.md](feature-flows/task-execution-service.md) |
| 2026-03-11 | #81 | Default model for headless tasks — prevents misleading "token expired" errors when agent settings contain incompatible model | [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) |
| 2026-03-11 | SCHED-ASYNC-001 | Scheduler async fire-and-forget with DB polling, status overwrite guard, cleanup timeout 30→120 min | [scheduler-service.md](feature-flows/scheduler-service.md), [cleanup-service.md](feature-flows/cleanup-service.md) |
| 2026-03-11 | CLEANUP-001 | Background cleanup service with active watchdog reconciliation, stale recovery, and slot cleanup | [cleanup-service.md](feature-flows/cleanup-service.md) |
| 2026-03-10 | AVATAR | Avatar display in Dashboard Timeline tiles (lg size, border ring) | [agent-avatars.md](feature-flows/agent-avatars.md), [dashboard-timeline-view.md](feature-flows/dashboard-timeline-view.md) |
| 2026-03-09 | AVATAR | Avatar image optimization — WebP conversion via Pillow, stable emotion cache keys | [agent-avatars.md](feature-flows/agent-avatars.md) |
| 2026-03-09 | CAPACITY-001 | Scheduled tasks route through TaskExecutionService — capacity meter now tracks cron/manual executions | [parallel-capacity.md](feature-flows/parallel-capacity.md), [scheduler-service.md](feature-flows/scheduler-service.md), [task-execution-service.md](feature-flows/task-execution-service.md) |
| 2026-03-09 | SEC | Security hardening: WS auth, internal API secret, agent ACL on chat/credentials, DOMPurify | [credential-injection.md](feature-flows/credential-injection.md), [scheduler-service.md](feature-flows/scheduler-service.md) |
| 2026-03-08 | AVATAR-003 | Default avatar generation — admin button in Settings, robot/android aesthetic | [agent-avatars.md](feature-flows/agent-avatars.md), [platform-settings.md](feature-flows/platform-settings.md) |
| 2026-03-08 | OPS-001 | Operating Room — consolidated Events + Cost Alerts into 4-tab layout | [operating-room.md](feature-flows/operating-room.md) |
| 2026-03-08 | OPS-001 | Operating Room — restart-resilient sync, refresh button, stale prompt detection | [operating-room.md](feature-flows/operating-room.md) |
| 2026-03-08 | — | Fix `--session-id` UUID validation failure in headless task execution | [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) |
| 2026-03-07 | OPS-001 | Operating Room — full implementation (backend, sync service, frontend, meta-prompt) | [operating-room.md](feature-flows/operating-room.md) |
| 2026-03-08 | AVATAR-002 | Emotion avatar variants with 30s cycling on AgentDetail page | [agent-avatars.md](feature-flows/agent-avatars.md) |
| 2026-03-08 | AVATAR-001 | Agent avatars with reference image system, variation regeneration, dark mode style | [agent-avatars.md](feature-flows/agent-avatars.md) |
| 2026-03-07 | IMG-001 | Platform image generation via Gemini two-step pipeline | [image-generation.md](feature-flows/image-generation.md) |
| 2026-03-06 | — | Headless task session isolation + permission mode validation | [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) |
| 2026-03-04 | TMPL-001 | Admin-configurable GitHub template repositories via Settings UI | [platform-settings.md](feature-flows/platform-settings.md), [templates-page.md](feature-flows/templates-page.md) |
| 2026-03-04 | THINK-001 | Dynamic thinking status extended to Public Chat (async mode + SSE) | [public-agent-links.md](feature-flows/public-agent-links.md) |
| 2026-03-04 | NVM-001 | Nevermined x402 payment integration for agent monetization | [nevermined-payments.md](feature-flows/nevermined-payments.md) |
| 2026-03-04 | EXEC-024 | Unified task execution service for all callers | [task-execution-service.md](feature-flows/task-execution-service.md) |
| 2026-03-03 | SUB-003 | Agent assign/unassign controls in subscription expanded rows | [subscription-management.md](feature-flows/subscription-management.md) |
| 2026-03-03 | SUB-002 | Subscription management rewrite: token-based auth via env var | [subscription-management.md](feature-flows/subscription-management.md) |
| 2026-03-03 | THINK-001 | Dynamic thinking status labels in Chat tab via SSE streaming | [authenticated-chat-tab.md](feature-flows/authenticated-chat-tab.md) |
| 2026-03-03 | CAPACITY-001 | Capacity meter UI on Agents page and Dashboard timeline (Phase 2) | [parallel-capacity.md](feature-flows/parallel-capacity.md) |
| 2026-03-03 | #60 | Success rate bar replaces context bar on Dashboard nodes | [agent-network.md](feature-flows/agent-network.md) |
| 2026-03-03 | #60 | Success rate bar replaces context bar in timeline tiles | [dashboard-timeline-view.md](feature-flows/dashboard-timeline-view.md) |
| 2026-03-03 | #60 | Success rate bar replaces context bar on Agents page | [agents-page-ui-improvements.md](feature-flows/agents-page-ui-improvements.md) |
| 2026-03-03 | #55 | Agents page filtering by name, status, and tags | [agents-page-ui-improvements.md](feature-flows/agents-page-ui-improvements.md) |
| 2026-03-03 | #54 | Two-row agent tiles, gap spacing, persistent tag filter | [agents-page-ui-improvements.md](feature-flows/agents-page-ui-improvements.md) |
| 2026-03-03 | #51 | Per-agent Files tab restored with two-panel file manager | [file-browser.md](feature-flows/file-browser.md) |
| 2026-03-03 | #52 | Templates restored to main NavBar | NavBar component change (no new flow) |
| 2026-03-03 | #53 | Agent Detail: removed sub-nav, widened panel, reduced padding | Layout change to AgentDetail.vue (no new flow) |
| 2026-03-02 | SUB-001 | Subscription credential priority fix (superseded by SUB-002) | [subscription-management.md](feature-flows/subscription-management.md) |
| 2026-03-02 | MON-001/SUB-001 | Subscription credential health monitoring and auto-remediation | [subscription-credential-health.md](feature-flows/subscription-credential-health.md) |
| 2026-03-02 | MODEL-001 | Model selection for tasks and schedules | [model-selection.md](feature-flows/model-selection.md) |
| 2026-03-02 | FILTER-001 | Dashboard filter persistence (time range, quick tags) | [dashboard-timeline-view.md](feature-flows/dashboard-timeline-view.md) |
| 2026-03-01 | RENAME-001 | Agent rename via UI pencil icon, MCP tool, or REST API | [agent-rename.md](feature-flows/agent-rename.md) |
| 2026-02-28 | GIT-002 | Git branch support for agent creation | [github-sync.md](feature-flows/github-sync.md) |
| 2026-02-28 | CAPACITY-001 | Per-agent parallel execution capacity | [parallel-capacity.md](feature-flows/parallel-capacity.md) |
| 2026-02-27 | PLAYBOOK-001 | Playbooks Tab - invoke agent skills from UI | [playbooks-tab.md](feature-flows/playbooks-tab.md) |
| 2026-02-27 | REFRESH-001 | Dashboard Timeline refresh fix | [dashboard-timeline-view.md](feature-flows/dashboard-timeline-view.md) |
| 2026-02-25 | ORG-001 | Tag Clouds visualization | [tag-clouds.md](feature-flows/tag-clouds.md) |
| 2026-02-25 | SLACK-001 | Slack Integration for Public Links | [slack-integration.md](feature-flows/slack-integration.md) |
| 2026-02-24 | DOCKER-001 | Async Docker operations | [async-docker-operations.md](feature-flows/async-docker-operations.md) |
| 2026-02-23 | DASH-001 | Dynamic Dashboards with sparklines | [dynamic-dashboards.md](feature-flows/dynamic-dashboards.md) |
| 2026-02-23 | MON-001 | Agent Monitoring Service | [agent-monitoring.md](feature-flows/agent-monitoring.md) |
| 2026-02-22 | SUB-001 | Subscription Management | [subscription-management.md](feature-flows/subscription-management.md) |
| 2026-02-21 | PERF-001 | Task List Performance | [tasks-tab.md](feature-flows/tasks-tab.md) |
| 2026-02-20 | EXEC-023 | Continue Execution as Chat | [continue-execution-as-chat.md](feature-flows/continue-execution-as-chat.md) |
| 2026-02-20 | NOTIF-001 | Agent Notifications | [agent-notifications.md](feature-flows/agent-notifications.md) |
| 2026-02-20 | AUDIT-001 | Execution Origin Tracking | [AUDIT-001-execution-origin-tracking.md](feature-flows/AUDIT-001-execution-origin-tracking.md) |
| 2026-02-19 | CHAT-001 | Authenticated Chat Tab | [authenticated-chat-tab.md](feature-flows/authenticated-chat-tab.md) |
| 2026-02-17 | ORG-001 | Agent Tags & System Views | [agent-tags.md](feature-flows/agent-tags.md) |
| 2026-02-17 | CFG-007 | Read-Only Mode | [read-only-mode.md](feature-flows/read-only-mode.md) |
| 2026-02-17 | PUB-005 | Public Chat Session Persistence | [public-agent-links.md](feature-flows/public-agent-links.md) |

---

## Documented Flows

### Core Agent Features

| Flow | Document | Description |
|------|----------|-------------|
| Agent Lifecycle | [agent-lifecycle.md](feature-flows/agent-lifecycle.md) | Create, start, stop, delete Docker containers |
| Agent Rename | [agent-rename.md](feature-flows/agent-rename.md) | Rename agents via UI, MCP, or API (RENAME-001) |
| Agent Terminal | [agent-terminal.md](feature-flows/agent-terminal.md) | Browser-based xterm.js terminal with Claude/Gemini/Bash modes |
| Credential Injection | [credential-injection.md](feature-flows/credential-injection.md) | CRED-002: Direct file injection, encrypted git storage |
| Agent Scheduling | [scheduling.md](feature-flows/scheduling.md) | Cron-based automation with APScheduler |
| Webhook Triggers | [webhook-triggers.md](feature-flows/webhook-triggers.md) | Token-authenticated public URL to fire schedule executions (WEBHOOK-001) |
| Scheduler Service | [scheduler-service.md](feature-flows/scheduler-service.md) | Standalone scheduler with Redis distributed locks |
| Execution Queue | [execution-queue.md](feature-flows/execution-queue.md) | Redis-based parallel execution prevention |
| Execution Termination | [execution-termination.md](feature-flows/execution-termination.md) | Stop running executions via process registry |
| Parallel Headless Execution | [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) | Stateless parallel task execution via POST /task |
| Parallel Capacity | [parallel-capacity.md](feature-flows/parallel-capacity.md) | Per-agent parallel execution slot tracking |
| Persistent Task Backlog | [persistent-task-backlog.md](feature-flows/persistent-task-backlog.md) | SQLite-backed FIFO backlog for async tasks at capacity (BACKLOG-001) |
| Capacity Management | [capacity-management.md](feature-flows/capacity-management.md) | Unified facade for per-agent execution capacity (#428) |
| Task Execution Service | [task-execution-service.md](feature-flows/task-execution-service.md) | Unified execution lifecycle for all task callers (EXEC-024) |
| Idempotency Keys | [idempotency-keys.md](feature-flows/idempotency-keys.md) | `Idempotency-Key` dedup at every execution trigger boundary — one execution per `(scope,key)` in 24h, fail-open (RELIABILITY-006, #525, Invariant #18) |
| Business Validation | [business-validation.md](feature-flows/business-validation.md) | Post-execution auditor verifies task completion (VALIDATE-001) |
| Fan-Out | [fan-out.md](feature-flows/fan-out.md) | Parallel task dispatch and result collection via semaphore (FANOUT-001) |
| Sequential Agent Loops | [run-agent-loop.md](feature-flows/run-agent-loop.md) | `run_agent_loop` server-side sequential bounded task execution with stop-signal + graceful stop (#740) |

### Dashboard & Monitoring

| Flow | Document | Description |
|------|----------|-------------|
| Agent Network (Dashboard) | [agent-network.md](feature-flows/agent-network.md) | Real-time visual graph at `/` |
| Dashboard Timeline View | [dashboard-timeline-view.md](feature-flows/dashboard-timeline-view.md) | Graph/Timeline mode toggle with execution boxes |
| Replay Timeline | [replay-timeline.md](feature-flows/replay-timeline.md) | Waterfall-style timeline visualization |
| Activity Stream | [activity-stream.md](feature-flows/activity-stream.md) | Centralized persistent activity tracking |
| Activity Monitoring | [activity-monitoring.md](feature-flows/activity-monitoring.md) | Real-time tool execution tracking |
| Agent Monitoring (Health) | [agent-monitoring.md](feature-flows/agent-monitoring.md) | Fleet-wide health checks (MON-001) |
| Agent Heartbeat Liveness | [agent-heartbeat-liveness.md](feature-flows/agent-heartbeat-liveness.md) | Push-based 5s liveness layer, watch loop + soft alerts (RELIABILITY-004 / #307) |
| Subscription Credential Health | [subscription-credential-health.md](feature-flows/subscription-credential-health.md) | Credential health monitoring, auto-remediation, alerts |
| Host Telemetry | [host-telemetry.md](feature-flows/host-telemetry.md) | Host CPU/memory/disk in Dashboard header |
| Agent Logs & Telemetry | [agent-logs-telemetry.md](feature-flows/agent-logs-telemetry.md) | Live metrics in AgentHeader |
| Agent Dashboard | [agent-dashboard.md](feature-flows/agent-dashboard.md) | Agent-defined dashboard via dashboard.yaml |
| Dynamic Dashboards | [dynamic-dashboards.md](feature-flows/dynamic-dashboards.md) | Historical widget values with sparklines (DASH-001) |
| Token Usage Display | [token-usage-display.md](feature-flows/token-usage-display.md) | Per-agent cost/token stats from DB in AgentHeader: sparkline, today vs 7-day avg trend (#250) |

### Agent Detail UI

| Flow | Document | Description |
|------|----------|-------------|
| Overview Tab | [agent-overview-dashboard.md](feature-flows/agent-overview-dashboard.md) | Default landing tab — multi-day trend charts + analytics endpoint (#1107) |
| Tab Overflow (More ▾) | [agent-detail-tab-overflow.md](feature-flows/agent-detail-tab-overflow.md) | Reusable `OverflowTabs.vue` — tabs collapse into a "More" dropdown instead of horizontal scroll (#1114) |
| Tasks Tab | [tasks-tab.md](feature-flows/tasks-tab.md) | Task execution UI with history |
| Playbooks Tab | [playbooks-tab.md](feature-flows/playbooks-tab.md) | Invoke agent skills from UI (PLAYBOOK-001) |
| Authenticated Chat Tab | [authenticated-chat-tab.md](feature-flows/authenticated-chat-tab.md) | Simple chat UI with dynamic status labels (CHAT-001, THINK-001) |
| Playbook Autocomplete | [playbook-autocomplete.md](feature-flows/playbook-autocomplete.md) | Slash-command autocomplete for playbooks in chat input |
| Voice Chat + Workspace | [voice-chat.md](feature-flows/voice-chat.md) | Voice conversations via Gemini Live API; Workspace mode with canvas panel tools (BETA) |
| Execution Log Viewer | [execution-log-viewer.md](feature-flows/execution-log-viewer.md) | Modal for viewing execution transcripts |
| Execution Detail Page | [execution-detail-page.md](feature-flows/execution-detail-page.md) | Dedicated page for execution details |
| Continue Execution as Chat | [continue-execution-as-chat.md](feature-flows/continue-execution-as-chat.md) | Resume executions as interactive chat (EXEC-023) |
| Agent Avatars | [agent-avatars.md](feature-flows/agent-avatars.md) | AI-generated avatars with reference images, emotion variants, and default generation (AVATAR-001/002/003) |
| Agent Info Display | [agent-info-display.md](feature-flows/agent-info-display.md) | Info tab: About leads; `template.yaml` metadata behind a collapsible "Technical details" (#1107) |
| Per-Agent File Manager | [file-browser.md](feature-flows/file-browser.md) | Two-panel file manager in Agent Detail Files tab |
| File Manager (Deprecated) | [file-manager.md](feature-flows/file-manager.md) | Former standalone `/files` page — replaced by per-agent Files tab |

### Collaboration & Permissions

| Flow | Document | Description |
|------|----------|-------------|
| Agent-to-Agent Collaboration | [agent-to-agent-collaboration.md](feature-flows/agent-to-agent-collaboration.md) | Inter-agent communication via MCP |
| Agent Event Subscriptions | [agent-event-subscriptions.md](feature-flows/agent-event-subscriptions.md) | Lightweight pub/sub for inter-agent event pipelines |
| Agent Permissions | [agent-permissions.md](feature-flows/agent-permissions.md) | Agent communication permissions |
| Agent Sharing | [agent-sharing.md](feature-flows/agent-sharing.md) | Cross-channel email allow-list (web/Slack/Telegram) with access policy and pending requests |
| Agent Shared Folders | [agent-shared-folders.md](feature-flows/agent-shared-folders.md) | File collaboration via shared volumes |
| Outbound File Sharing | [file-sharing-outbound.md](feature-flows/file-sharing-outbound.md) | Agents publish files to public download URLs (FILES-001) |
| Agent Tags & System Views | [agent-tags.md](feature-flows/agent-tags.md) | Tagging and saved filters (ORG-001) |
| Tag Clouds | [tag-clouds.md](feature-flows/tag-clouds.md) | Visual grouping on Dashboard |

### Authentication & Security

| Flow | Document | Description |
|------|----------|-------------|
| Email Authentication | [email-authentication.md](feature-flows/email-authentication.md) | Passwordless email login |
| Admin Login | [admin-login.md](feature-flows/admin-login.md) | Password-based admin auth |
| First-Time Setup | [first-time-setup.md](feature-flows/first-time-setup.md) | Admin password wizard |
| MCP API Keys | [mcp-api-keys.md](feature-flows/mcp-api-keys.md) | API key management |
| Execution Origin Tracking | [AUDIT-001-execution-origin-tracking.md](feature-flows/AUDIT-001-execution-origin-tracking.md) | Track who triggered executions |
| Agent-Server Authentication | [agent-server-authentication.md](feature-flows/agent-server-authentication.md) | Per-agent inbound auth for the in-container agent server (`:8000`) — derived `X-Trinity-Agent-Token` (HMAC over `AGENT_AUTH_SECRET`) enforced by a pure-ASGI middleware on every HTTP/WS route (#1159) |

### Public Access & Monetization

| Flow | Document | Description |
|------|----------|-------------|
| Public Agent Links | [public-agent-links.md](feature-flows/public-agent-links.md) | Shareable public links: chat type only (SITE-001 reverse-proxy retired in #865; SITE-002 redesign pending) |
| Slack Integration | [slack-integration.md](feature-flows/slack-integration.md) | Slack as delivery channel for public links (SLACK-001) |
| Slack Channel Routing | [slack-channel-routing.md](feature-flows/slack-channel-routing.md) | Channel adapter abstraction + multi-agent Slack routing (SLACK-002) |
| Slack File Sharing | [slack-file-sharing.md](feature-flows/slack-file-sharing.md) | Inbound file uploads: images via vision, text via container (SLACK-FILES) |
| Telegram Integration | [telegram-integration.md](feature-flows/telegram-integration.md) | Per-agent Telegram bots with webhook transport, group chat support, and `/login` email verification (TELEGRAM-001, TGRAM-GROUP) |
| Unified Channel Access Control | [unified-channel-access-control.md](feature-flows/unified-channel-access-control.md) | Cross-channel access gate keyed on verified email — policy, /login, access requests (#311) |
| VoIP Telephony | [voip-telephony.md](feature-flows/voip-telephony.md) | Outbound phone calls over the Gemini Live bridge via Twilio Media Streams; per-agent voice binding, ticket-authed WS, post-call transcript processing. Flag-gated default OFF (VOIP-001, #1056) |
| OpenAI Codex Runtime | [codex-runtime.md](feature-flows/codex-runtime.md) | Third agent runtime ("harness == runtime") — `codex exec` engine with full safety parity (system prompt, read-only sandbox, guardrails, sanitization), `RuntimeCapabilities`, Session-tab gate, MCP via config.toml. See also the [Harness Authoring Guide](harness-authoring-guide.md) (#1187) |
| Nevermined x402 Payments | [nevermined-payments.md](feature-flows/nevermined-payments.md) | Per-agent paid API via x402 payment protocol (NVM-001) |

### Mobile & PWA

| Flow | Document | Description |
|------|----------|-------------|
| Mobile Admin PWA | [mobile-admin-pwa.md](feature-flows/mobile-admin-pwa.md) | Standalone mobile admin at `/m` with agent chat, autonomy toggle, Ops/System tabs (MOB-001) |

### Platform Services

| Flow | Document | Description |
|------|----------|-------------|
| Image Generation | [image-generation.md](feature-flows/image-generation.md) | Gemini-powered two-step image generation pipeline (IMG-001) |

### MCP & Integration

| Flow | Document | Description |
|------|----------|-------------|
| MCP Orchestration | [mcp-orchestration.md](feature-flows/mcp-orchestration.md) | 62 MCP tools for agent orchestration |
| Trinity CLI | [cli-tool.md](feature-flows/cli-tool.md) | Python Click CLI with multi-instance profiles, mirroring core MCP tools as shell commands |
| Trinity Connect | [trinity-connect.md](feature-flows/trinity-connect.md) | Local-remote agent sync via WebSocket |

### GitHub Integration

| Flow | Document | Description |
|------|----------|-------------|
| GitHub Sync | [github-sync.md](feature-flows/github-sync.md) | Source mode (pull-only) or Working Branch mode |
| GitHub Repo Initialization | [github-repo-initialization.md](feature-flows/github-repo-initialization.md) | Initialize GitHub sync for existing agents |
| Persistent-State Allowlist | [persistent-state-allowlist.md](feature-flows/persistent-state-allowlist.md) | `.trinity/persistent-state.yaml` primitive for reset-preserve-state (S4, #383) |
| Git Sync Health | [git-sync-health.md](feature-flows/git-sync-health.md) | Auto-sync heartbeat, dual ahead/behind, dashboard dot, `/api/fleet/sync-audit` |

### Skills Management

| Flow | Document | Description |
|------|----------|-------------|
| Skills CRUD | [skills-crud.md](feature-flows/skills-crud.md) | Admin CRUD for platform skills |
| Skill Assignment | [skill-assignment.md](feature-flows/skill-assignment.md) | Owner assigns skills to agents |
| Skill Injection | [skill-injection.md](feature-flows/skill-injection.md) | Automatic injection on agent start |
| Skills on Agent Start | [skills-on-agent-start.md](feature-flows/skills-on-agent-start.md) | Detailed startup injection flow |
| MCP Skill Tools | [mcp-skill-tools.md](feature-flows/mcp-skill-tools.md) | 8 MCP tools for skill management |
| Skills Management UI | [skills-management.md](feature-flows/skills-management.md) | Frontend UI documentation |
| Skills Library Sync | [skills-library-sync.md](feature-flows/skills-library-sync.md) | GitHub repository sync |

### Notifications & Events

| Flow | Document | Description |
|------|----------|-------------|
| Agent Notifications | [agent-notifications.md](feature-flows/agent-notifications.md) | Agent-to-platform notifications (NOTIF-001) |
| Events Page UI | [events-page.md](feature-flows/events-page.md) | Consolidated into Operating Room Notifications tab |
| Operating Room | [operating-room.md](feature-flows/operating-room.md) | Unified operator command center: queue, notifications, resolved (OPS-001) |

### Configuration & Settings

| Flow | Document | Description |
|------|----------|-------------|
| Autonomy Mode | [autonomy-mode.md](feature-flows/autonomy-mode.md) | Agent autonomous operation toggle |
| AutonomyToggle Component | [autonomy-toggle-component.md](feature-flows/autonomy-toggle-component.md) | Reusable Vue toggle component |
| Read-Only Mode | [read-only-mode.md](feature-flows/read-only-mode.md) | Code protection via hooks (CFG-007) |
| Agent Guardrails | [agent-guardrails.md](feature-flows/agent-guardrails.md) | Baseline bash/path deny-lists, credential output scanner, turn/timeout/tool budgets; owner-only narrow overrides (GUARD-001/002/003) |
| Agent Resource Allocation | [agent-resource-allocation.md](feature-flows/agent-resource-allocation.md) | Per-agent memory/CPU limits + system-wide admin defaults (RES-001) |
| Container Capabilities | [container-capabilities.md](feature-flows/container-capabilities.md) | Full capabilities mode |
| Model Selection | [model-selection.md](feature-flows/model-selection.md) | LLM model selection for terminal, tasks, and schedules |
| Agent Quotas | [agent-quotas.md](feature-flows/agent-quotas.md) | Per-role agent creation limits (QUOTA-001) |
| Platform Settings | [platform-settings.md](feature-flows/platform-settings.md) | Admin settings page |
| SSH Access | [ssh-access.md](feature-flows/ssh-access.md) | Ephemeral SSH credentials |
| Subscription Management | [subscription-management.md](feature-flows/subscription-management.md) | Claude Max/Pro subscription tokens via env var (SUB-002) |
| Subscription Usage Tracking | [subscription-usage-tracking.md](feature-flows/subscription-usage-tracking.md) | Rolling 5h/7d token and cost usage per subscription (SUB-004) |

### System & Infrastructure

| Flow | Document | Description |
|------|----------|-------------|
| Internal System Agent | [internal-system-agent.md](feature-flows/internal-system-agent.md) | Platform operations manager (trinity-system) |
| System Manifest | [system-manifest.md](feature-flows/system-manifest.md) | Recipe-based multi-agent deployment |
| System-Wide Trinity Prompt | [system-wide-trinity-prompt.md](feature-flows/system-wide-trinity-prompt.md) | Admin-configurable prompt injection |
| Vector Logging | [vector-logging.md](feature-flows/vector-logging.md) | Centralized log aggregation |
| OpenTelemetry Integration | [opentelemetry-integration.md](feature-flows/opentelemetry-integration.md) | OTel metrics export |
| Async Docker Operations | [async-docker-operations.md](feature-flows/async-docker-operations.md) | Non-blocking Docker SDK wrappers |
| Backend Image Packaging Guard | [backend-image-packaging.md](feature-flows/backend-image-packaging.md) | Dockerfile `COPY` glob + `backend-image-smoke.yml` boot-of-baked-prod-image CI — closes the source→image packaging gap that crash-looped the backend (#1033) |
| Cleanup Service | [cleanup-service.md](feature-flows/cleanup-service.md) | Active watchdog reconciliation + passive stale recovery for executions, activities, and slots (CLEANUP-001, #129) |
| Database Migration Runner | [database-migration-runner.md](feature-flows/database-migration-runner.md) | Cross-process `flock` + atomic rename-swap rebuilds make the SQLite migration suite crash-safe and concurrency-safe; failed migration named in the `/health` 503 (#1160 / #1183) |
| WebSocket Event Bus | [websocket-event-bus.md](feature-flows/websocket-event-bus.md) | Redis Streams transport for `/ws` + `/ws/events` with reconnect replay, per-client eviction, `MAXLEN` trim (RELIABILITY-003 / #306) |

### Templates & Pages

| Flow | Document | Description |
|------|----------|-------------|
| Template Processing | [template-processing.md](feature-flows/template-processing.md) | GitHub and local template handling |
| Templates Page | [templates-page.md](feature-flows/templates-page.md) | `/templates` route for browsing |
| API Keys Page | [api-keys-page.md](feature-flows/api-keys-page.md) | `/api-keys` page UI flow |
| Agents Page UI | [agents-page-ui-improvements.md](feature-flows/agents-page-ui-improvements.md) | Horizontal row tiles with success rate bars, filtering, responsive breakpoints |
| Alerts Page | [alerts-page.md](feature-flows/alerts-page.md) | Removed in #430 (process engine deletion; cost alerts were PE-only) |

### File Management

| Flow | Document | Description |
|------|----------|-------------|
| Web Chat File Upload | [web-chat-file-upload.md](feature-flows/web-chat-file-upload.md) | Drag-drop/picker for authenticated and public chat; shared upload_service (#364) |

### Chat & Sessions

| Flow | Document | Description |
|------|----------|-------------|
| Persistent Chat Tracking | [persistent-chat-tracking.md](feature-flows/persistent-chat-tracking.md) | Database-backed chat persistence |
| Session Tab | [session-tab.md](feature-flows/session-tab.md) | `--resume`-default chat surface — each turn reattaches to the same Claude memory (SESSION_TAB_2026-04) |
| Web Terminal | [web-terminal.md](feature-flows/web-terminal.md) | Browser-based terminal for System Agent |

### Testing & Development

| Flow | Document | Description |
|------|----------|-------------|
| Testing Agents Suite | [testing-agents.md](feature-flows/testing-agents.md) | Automated pytest suite (1460+ tests) |
| Local Agent Deployment | [local-agent-deploy.md](feature-flows/local-agent-deploy.md) | Deploy local agents via MCP |
| Dark Mode / Theme | [dark-mode-theme.md](feature-flows/dark-mode-theme.md) | Client-side theme system |

---

## Archived Flows

Preserved in `feature-flows/archive/` for historical reference.

| Flow | Status | Document | Reason |
|------|--------|----------|--------|
| Auth0 Authentication | REMOVED | [archive/auth0-authentication.md](feature-flows/archive/auth0-authentication.md) | Replaced by email auth (2026-01-01) |
| Agent Chat | DEPRECATED | [archive/agent-chat.md](feature-flows/archive/agent-chat.md) | Replaced by Agent Terminal |
| Agent Vector Memory | REMOVED | [archive/vector-memory.md](feature-flows/archive/vector-memory.md) | Templates should define their own |
| Agent Network Replay | SUPERSEDED | [archive/agent-network-replay-mode.md](feature-flows/archive/agent-network-replay-mode.md) | Replaced by Dashboard Timeline |
| System Agent UI | CONSOLIDATED | [archive/system-agent-ui.md](feature-flows/archive/system-agent-ui.md) | Uses regular AgentDetail.vue |
| Skills Management | SPLIT | [archive/skills-management.md](feature-flows/archive/skills-management.md) | Split into dedicated flows |

---

## Requirements Specs

### Implemented

| Document | Status | Description |
|----------|--------|-------------|
| [DEDICATED_SCHEDULER_SERVICE.md](../requirements/DEDICATED_SCHEDULER_SERVICE.md) | ✅ | Standalone scheduler service |
| [EXTERNAL_PUBLIC_URL.md](../requirements/EXTERNAL_PUBLIC_URL.md) | ✅ | External URL for public links |
| [EXECUTION_ORIGIN_TRACKING.md](../requirements/EXECUTION_ORIGIN_TRACKING.md) | ✅ | Track who triggered executions |
| [AGENT_SYSTEMS_AND_TAGS.md](../requirements/AGENT_SYSTEMS_AND_TAGS.md) | ✅ | Tags and System Views |
| [NEVERMINED_PAYMENT_INTEGRATION.md](../requirements/NEVERMINED_PAYMENT_INTEGRATION.md) | ✅ | Per-agent x402 payment monetization |

### Pending

| Document | Priority | Description |
|----------|----------|-------------|
| [PUBLIC_EXTERNAL_ACCESS_SETUP.md](../requirements/PUBLIC_EXTERNAL_ACCESS_SETUP.md) | MEDIUM | Infrastructure setup for public access |

---

## Core Specifications

| Document | Purpose |
|----------|---------|
| [TRINITY_COMPATIBLE_AGENT_GUIDE.md](../TRINITY_COMPATIBLE_AGENT_GUIDE.md) | Creating Trinity-compatible agents |
| [MULTI_AGENT_SYSTEM_GUIDE.md](../MULTI_AGENT_SYSTEM_GUIDE.md) | Building multi-agent systems |

---

## Flow Document Template

Save flows to: `docs/memory/feature-flows/{feature-name}.md`

```markdown
# Feature: {Feature Name}

## Overview
Brief description of what this feature does.

## User Story
As a [user type], I want to [action] so that [benefit].

## Entry Points
- **UI**: `src/frontend/src/views/Component.vue` - Action trigger
- **API**: `METHOD /api/endpoint`

## Frontend Layer
### Components
- `Component.vue:line` - handler() method

### State Management
- `stores/store.js` - action name

## Backend Layer
### Endpoints
- `src/backend/routers/file.py:line` - endpoint_handler()

### Business Logic
1. Step one
2. Step two

## Data Layer
### Database Operations
- Query: Description
- Update: Description

## Side Effects
- WebSocket broadcast: `{type, data}`

## Error Handling
- Error case → HTTP status

## Testing
### Prerequisites
- Services running
- Test user logged in

### Test Steps
1. **Action**: Do X
   **Expected**: Y happens
   **Verify**: Check Z

## Related Flows
- [related-flow.md](feature-flows/related-flow.md)
```

---

## How to Create a Flow Document

1. Run `/feature-flow-analysis {feature-name}`
2. Or manually trace: UI → API → Backend → Database → Side Effects
3. Add Testing section with step-by-step verification
4. Update this index after creating

See `docs/TESTING_GUIDE.md` for testing template and examples.
