# Trinity - Autonomous Agent Orchestration Platform - Architecture

> **Purpose**: Documents the CURRENT system design. Update only when implementing changes.
>
> **Editorial rules**: (1) **One home per feature** — each cross-cutting subsystem is described exactly once, in [Cross-Cutting Subsystems](#cross-cutting-subsystems); every other mention is a pointer. (2) **Catalogs are catalogs** — router/service/endpoint entries are ≤2 lines; deeper behavior lives in the subsystem block or `docs/memory/feature-flows/`. (3) No changelog narration — git history records what was replaced and when; issue tags (`#526`) are kept as lookup keys.

## System Overview

**Trinity** is an **autonomous agent orchestration and infrastructure platform** — sovereign infrastructure for deploying, orchestrating, and governing fleets of autonomous AI agents on your own hardware. Each agent runs as an isolated Docker container with standardized interfaces for credentials, tools, and MCP server integrations.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Trinity Agent Platform                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │   Frontend   │  │   Backend    │  │  MCP Server  │  │    Vector    │    │
│  │   (Vue.js)   │  │  (FastAPI)   │  │  (FastMCP)   │  │   (Logs)     │    │
│  │   :80        │  │   :8000      │  │   :8080      │  │   :8686      │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
│         │                 │                 │                 │             │
│         └─────────────────┼─────────────────┼─────────────────┘             │
│                           │                 │                               │
│                    ┌──────┴──────┐   ┌──────┴──────┐                       │
│                    │    Redis    │   │   Docker    │                       │
│                    │    :6379    │   │   Engine    │                       │
│                    └─────────────┘   └──────┬──────┘                       │
│                                             │                               │
│         ┌───────────────────────────────────┼───────────────────────────┐  │
│         │                                   │                           │  │
│    ┌────┴────┐    ┌─────────┐    ┌─────────┴┐    ┌─────────┐           │  │
│    │ Agent 1 │    │ Agent 2 │    │ Agent 3  │    │ Agent N │           │  │
│    │ :8000   │    │ :8000   │    │ :8000    │    │ :8000   │           │  │
│    └─────────┘    └─────────┘    └──────────┘    └─────────┘           │  │
│         Agent Network (172.28.0.0/16)                                   │  │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Technology Stack

| Layer | Technologies |
|-------|-------------|
| Frontend | Vue.js 3 (Composition API), Tailwind CSS 3, Pinia 2, Vite 5 |
| Backend | FastAPI 0.100+, Python 3.13, Docker SDK 7.x, SQLite 3, Redis 7, httpx 0.24+ |
| Agent runtime | Python 3.13, Node.js 20, Go 1.21, Claude Code (latest) |
| Infrastructure | Docker, nginx (prod reverse proxy), Cloudflare Tunnel (public endpoints), Tailscale (private VPN), GCP, Vertex AI Search (docs Q&A) |

**Python version is a single declaration, guarded (#1891).** The three image Dockerfiles (`docker/{backend,scheduler,base-image}/Dockerfile`) are the source of truth; every `python-version:` in `.github/workflows/` must equal that pin, enforced by `tests/unit/test_1891_python_version_parity.py` (scans *all* workflows, so a new one with a stale pin fails too; `publish-cli.yml` is allowlisted — PyPI packaging is governed by CLI consumers, not the image runtime). CI previously ran 3.11 against 3.13 images, which by construction cannot catch the stdlib-removal class that had already shipped twice (`crypt` → #1615, `audioop` → the `audioop-lts` VoIP pin). **The same principle covers the tz database one layer down (#1823):** the backend and scheduler images must ship the IANA backward-compatibility links (`tzdata-legacy` + the `tzdata` wheel — `zoneinfo` reads the system database first and falls back to the wheel only for keys the system lacks), because the floating `python:3.13-slim` tag silently migrated bookworm→trixie, where those links were split out of `tzdata`, and every schedule stored under a legacy alias (`Europe/Kiev`) stopped resolving — 500 on create, silently non-firing in the scheduler — while CI stayed green because `tests/requirements-test.txt` already declared `tzdata` (#1771), i.e. the test environment was *more* capable than production; guarded by `tests/unit/test_1823_tz_capability_parity.py`, so a future image slim-down that drops either source fails CI instead of re-shipping it.

---

## Component Details

### Backend (`src/backend/`)

**Core modules:**

| Module | Purpose |
|--------|---------|
| `main.py` | FastAPI app initialization, WebSocket manager, router mounting |
| `config.py` | Centralized configuration constants |
| `models.py` | All Pydantic request/response models (Invariant #14) |
| `dependencies.py` | FastAPI dependencies (auth, token validation, role hierarchy, agent access control) |
| `database.py` | SQLite persistence facade — orchestrates 27 domain operation classes from `db/` |
| `logging_config.py` | Structured JSON logging (captured by Vector); OTel trace ID in log entries for log-trace correlation (RELIABILITY-002) |
| `error_handlers.py` | App-level exception handlers, registered in `main.py`. Owns the 422 shape: Pydantic's `input` (the rejected value) is stripped from every validation-error entry, so a failed `SecretStr` validation cannot return the secret (ent#109) |
| `utils/safe_yaml.py` | **The** hardened parser for every author-controlled YAML — size cap + alias policy + duplicate-key rejection (ent#314). Two policies, both required arguments (no default): `AliasPolicy.BUDGET` bounds expansion cost (manifests, `template.yaml` catalog — a legitimate anchor still parses), `AliasPolicy.REJECT` refuses any alias (skills frontmatter, the live agent-writable `template.yaml`, whose consumer walks every field). Consolidates #1884's manifest loader and two `_NoAliasSafeLoader` copies; the catalog path had **no** guard, which is the ent#314 hole. Bare `safe_load` leaves alias amplification that only blows up at SERIALIZATION (measured 416 B → 110 MB via `json.dumps`; the parse is 0.001 s, so an input cap cannot close it) and silent last-wins duplicate keys. Rejects, never truncates. |
| `utils/url_validation.py` | The SSRF module. `_validate_public_https_url(url, *, label, …)` (#736) is the ONE public-HTTPS-destination gate — HTTPS-only, no userinfo, IDNA/A-label normalised once, every resolved address vetted, DNS failure fatal — shared by `validate_template_registry_url` and the new `validate_a2a_endpoint_url`. Extracted rather than cloned a third time: a third ~90%-identical copy inside the module whose job is centralising this policy would be the Invariant #5 failure happening in the file that exists to prevent it. `validate_skills_library_url` stays separate **by design** (github.com-allowlisted, DNS-failure-tolerant — pinned by a test that explains why unifying it would be wrong). The A2A form returns the **validated addresses**, so the caller can pin the connection to one this function approved |
| `redis_breaker_util.py` | Shared Redis plumbing (fail-open client, Lua `ScriptCache`, decode helpers) used by both circuit breakers. Also home to the **`SingleFlightLock`** primitive (#1920) — the one shared fail-open, ownership-checked single-flight lock (`acquire` / `refresh_if_owned` → `LeaseState{OWNED,REACQUIRED,LOST,DEGRADED}` / `release_if_owned`) reusing `lock_token_matches` as the ownership predicate; mints a unique per-acquire `uuid` token and **releases via GET-then-DELETE compare-and-delete**. That release is **deliberately non-atomic (accepted GET→DELETE TOCTOU window, the same one `ops.py` shipped), NOT atomic-Lua**, so it stays fakeredis-unit-testable; the async `ResumeLock`'s Lua compare-and-delete (`session_turn_service.py`) is the stronger, **deliberately-separate** primitive — do NOT merge `ResumeLock` onto this weaker release. Consumers: `system_seed_service`, `agent_service/ephemeral`, `routers/ops` (adds `refresh_if_owned`), `skill_service` ×2 (injected client), `cornelius_agent_service`, `compatibility/fixes` (the last two were verbatim twins of system_seed's constant-"1" + unconditional-delete bug, fixed with it). Deliberate non-consumers: `ResumeLock` (async + Lua + blocking-poll), the `monitoring`/`operator_queue` leader leases (stable cross-cycle worker id), and canary's Lua-CAD leader lease |

**OpenTelemetry tracing** (RELIABILITY-002): auto-instrumentation for FastAPI/httpx/Redis; `traceparent` propagated through inter-agent calls; OTLP/gRPC export to `trinity-otel-collector:4317`; `OTEL_ENABLED=1`, `OTEL_SAMPLE_RATE` (default 10%).

**Routers (`routers/`)** — 63 router modules:

*Core Agent:*
- `agents.py` - Core CRUD, start/stop, logs, stats, queue, activities, terminal (1054 lines)
- `agent_config.py` - Per-agent settings: autonomy, read-only, resources, capabilities, capacity, timeout, api-key
- `agent_files.py` - Files, info, playbooks, permissions, metrics, shared folders, file-sharing toggle + list/revoke (FILES-001)
- `agent_data.py` - Runtime-data export/import (`data_paths`) over the durable home volume (#1169)
- `agent_brain_orb.py` - Brain Orb proxy: `/brain-orb/data` + `/scopes` + `/tool` (read) + `/scope` (owner mutation) + `/voice-token` (Phase 3 ephemeral Gemini mint) — see [Brain Orb](#brain-orb--self-rendering-mind-page-58-trinity-enterprise) (#58, #60)
- `loops.py` - Sequential agent loops: start/get/stop + agent-scoped list (#740)
- `reminders.py` - Agent self-reminders: create/list/cancel (self-gated) (#1296) — see [Agent Self-Reminders](#agent-self-reminders-1296)
- `files.py` - Public download endpoint for outbound agent file sharing (FILES-001)
- `agent_rename.py` - Rename endpoint (RENAME-001)
- `a2a.py` - A2A protocol: the authenticated per-agent card (#737) plus the **inbound server** on a separate prefix-less `a2a_server_router` — public `GET /a2a/{name}/.well-known/agent-card.json` (per-IP rate limited) + `POST /a2a/{name}` JSON-RPC (message/send, message/stream SSE, tasks/get, tasks/cancel). Exposure is opt-in per agent (`agent_ownership.a2a_exposed`, default OFF); non-exposed/inaccessible → uniform 404 (Invariant #8). `messageId` dedup is scoped per (agent, caller principal) — the field is peer-controlled and only unique per-client (ent#157). **Also hosts the OUTBOUND client (#736):** `POST /{name}/a2a/call` + `POST /{name}/a2a/task`, a Trinity agent tasking an EXTERNAL A2A agent. The target is never caller-supplied — it is a name resolved through `services/a2a_outbound.py` — and the routes are thin (auth + HTTP error map + audit); orchestration lives in `services/a2a_outbound_service.py`. Default OFF (`A2A_OUTBOUND_ENABLED`), both routes 404 when off
- `agent_ssh.py` - SSH access endpoint
- `credentials.py` - Credential injection/export/import (CRED-002)
- `chat.py` / `chat/` - Agent chat/activity monitoring
- `internal.py` - Internal endpoints for agent startup, scheduler task execution (no auth; see Container Security). Also hosts the **dark** pull/work-stealing seams `GET /api/internal/next-task` (atomic claim + lease) + `POST /api/internal/tasks/{id}/result` (CAS) on a separate `pull_router` with dual-auth (internal secret OR the calling agent's own scoped MCP key) — inert until `PULL_MODE_PILOT_AGENTS` is set (#1081 Phase 1)
- `templates.py` - Template listing and GitHub repo fetching; `local:` ids resolve through `template_service.contained_template_dir` (name-allowlist + `resolve()`/`is_relative_to`), the same barrier shape the create path has used since #950 (#1900)
- `sharing.py` - Agent sharing between users
- `git.py` - Git sync endpoints (status, sync, log, pull)

*Auth & Security:*
- `auth.py` - Admin login (username OR registered email + password, #82), email auth, token validation. **`POST /api/auth/email/verify` re-checks the allow-list before redeeming a code (#2381).** Codes live in one shared table keyed on `(email, code)` with nothing binding a code to the channel that minted it: `/email/request` checks the allow-list before minting, but `telegram_adapter` / `whatsapp_adapter` `/login <email>` mint after a shape check only, and `mcp_auth_service` mints for any address `users` already knows. Their own redeemers grant channel- or connector-scope; **this** redeemer issues a full platform JWT carrying the matched account's role, and `get_or_create_email_user` resolves by the email column alone — so an attacker-controlled address bound to the admin row turned any of those mints into an admin session that no password reset or restart revokes. The refusal shares the bad-code branch verbatim (status, detail, audit action, rate-limit accounting) so it is not a membership oracle, and fails **closed** on a read error. Narrows nothing legitimate: web codes already passed this check at mint, channel users redeem in their own channel, and every account that can email-login is allow-listed by construction (sharing, access-request approval, the admin allow-list UI). The deeper question — whether an email-column lookup may return the admin row at all — is deliberately **not** closed here
- `users.py` - User management, roles (ROLE-001); `PUT /me/email` self-service sign-in email (#82 transition)
- `mcp_keys.py` - MCP API key management
- `connector.py` - Per-agent MCP connector: config + scoped-key mint/regenerate/revoke + exposed-playbook allow-list (`/api/agents/{name}/connector*`). OSS-core (ent#46 → #118); see [mcp-connector.md](feature-flows/mcp-connector.md)
- `mcp_auth.py` - #848 inline email auth internal surface (`/api/internal/mcp-auth/{request,verify,playbooks,chat}`): lets a keyless MCP client sign in with the 6-digit email code and then reach the connector playbooks of agents shared with that address. Dual-gated — `X-Internal-Secret` **AND** `MCP_INLINE_AUTH_ENABLED` (404s the whole surface when off, so a disabled deploy doesn't advertise it). The secret authenticates the *caller*, never the *action*: every data call re-gates on `assert_email_may_reach_agent` → `db.email_has_agent_access(agent, email)` + connector-enabled, so a compromised MCP server cannot reach an agent the asserted email cannot. See [mcp-connector.md](feature-flows/mcp-connector.md)
- `agent_mcp_key.py` - The agent's OWN `scope='agent'` Trinity MCP key: read + container config-truth probe + rotation (`/api/agents/{name}/mcp-key*`); see [Agent MCP Key](#agent-mcp-keys-agent--trinity-mcp) and [agent-mcp-key.md](feature-flows/agent-mcp-key.md) (#1854)
- `setup.py` - First-time setup wizard; **required** admin email (sign-in identity) + opt-in hosted intake (trinity-enterprise#38, #82). Setup token removed — no token, no Redis dependency for setup (trinity-enterprise#49). **The endpoint's precondition is now its own, not a derived flag (#2381):** `POST /admin-password` refuses whenever a usable admin account already exists, so it can only ever provision the FIRST account. ent#49 accepted the tokenless first-run window on the premise *there is no admin yet*; that premise is false for every install booting with `ADMIN_PASSWORD` set (mandatory in `docker-compose.prod.yml`, always present after `start.sh`), where `_ensure_admin_user` has already created one — and `setup_completed` said `false` anyway, because the `setup_completed_backfill` migration asks "does an admin exist?" during `run_all_migrations`, i.e. **before** `_ensure_admin_user` populates `users`, then records itself as applied so it never asks again (PostgreSQL had no writer for the key at all). So an unauthenticated caller could overwrite a live admin's password hash and bind their own email as its sign-in identity. The check is **fail-closed** (a DB read error refuses) and runs **above** the bcrypt hash, on a route with no auth and no rate limit. Username comes from `utils/admin_identity.admin_username()`, not a literal — `_ensure_admin_user` honours `ADMIN_USERNAME`, and against the old hardcoded `"admin"` an `ADMIN_USERNAME=root` install missed the real admin entirely and `update_user_password` (an upsert) INSERTed a *second* `role='admin'` account for the caller. The residual window is now exactly the installs the wizard exists for: no admin, no way in, wizard is the only door — see [First-Run Provisioning](#first-run-provisioning-honest-setup_completed-2381)

*Scheduling & Execution:*
- `schedules.py` - Schedule CRUD and control
- `executions.py` - Fleet execution list/stats (EXEC-022)
- `analytics.py` - Agent-scoped Overview analytics (#1107)
- `compatibility.py` - Agent compatibility validation: report + auto-fix (#668) — see [Agent Compatibility Validation](#agent-compatibility-validation-668)

*Organization & Tags:*
- `tags.py` - Agent tagging
- `system_views.py` - Saved system views
- `systems.py` - System manifest deployment; also the read-only bundled-manifest catalog `GET /manifests` + `/manifests/{id}` backing the ent#126 UI install surface — both `require_role("creator")` and both declared **above** the parameterized routes (Invariant #4 twice: `/{system_name}` and `/{system_name}/manifest`)

*Monitoring & Operations:*
- `monitoring.py` - Fleet health monitoring (MON-001)
- `telemetry.py` - Host telemetry (CPU/memory/disk)
- `activities.py` - Activity timeline
- `agent_dashboard.py` - Agent-defined dashboard (dashboard.yaml)
- `alerts.py` - Cost threshold alerts
- `notifications.py` - Agent notifications
- `reports.py` - Agent-published structured reports (#918) — see [Agent Reports](#agent-reports-918)
- `evaluations.py` - Behavioral-evaluation referee surface (ent#206): write is human-admin-only (`require_admin` + `reject_agent_principal`), read is access-scoped — see [Agent Evaluations](#agent-evaluations--the-referee-surface-ent206)
- `operator_queue.py` - Operating Room queue (OPS-001)
- `ops.py` - Operating Room sync service
- `logs.py` - Container log endpoints
- `observability.py` - Observability data
- `audit.py` - Platform audit log (SEC-001)

*Public Access & Monetization:*
- `public_links.py` - Public agent link management
- `public.py` - Public chat endpoints
- `paid.py` - x402 payment-gated chat (NVM-001)
- `nevermined.py` - Nevermined payment config (NVM-001)
- `slack.py` - Slack integration: OAuth, events, multi-agent channel routing, per-agent binding (SLACK-001/002)
- `telegram.py` - Telegram bot integration: webhook receiver, bot binding, group config (TELEGRAM-001)
- `whatsapp.py` - WhatsApp via Twilio: webhook receiver, binding CRUD + test (WHATSAPP-001)
- `voip.py` - VoIP telephony: binding CRUD, outbound call trigger, Media Streams WS entrypoint — see [VoIP](#voip-telephony-voip-001-1056)
- `webhooks.py` - Public webhook trigger endpoint + JWT-auth webhook management (WEBHOOK-001)
- `messages.py` - Proactive agent-to-user messaging (#321)
- `public_memory.py` - Per-user memory write endpoint for channel sessions (MEM-001, #888)

*Subscriptions & Skills:*
- `subscriptions.py` - Subscription management (SUB-002); #471 usage observability — extended `GET /{id}/usage` (failure counters + one-gate `rate_limited_now` + `headroom` block, `source: anthropic|observed`), `GET /{id}/usage/breakdown` (per-agent, cost-ranked), `POST /{id}/usage/refresh` (click probe), `GET/PUT /settings/headroom-auto-refresh` — all `assert_admin`; ent#433 adds `GET /{id}/headroom/history?window=24h|7d|30d` (bounded `last`-per-bucket series, `bucket_start` + real `fetched_at`, `coverage_pct`; 422 on an unknown window, id-OR-name + 404 parity with `/usage`; read-only, never probes)
- `skills.py` - Skill CRUD and assignment
- `settings.py` - Platform admin settings (incl. Slack transport connect/disconnect/install)

*Content & Files:*
- `image_generation.py` - Image generation REST endpoints (IMG-001)
- `avatar.py` - Agent avatar generation and serving (AVATAR-001)
- `docs.py` - Documentation endpoints

*System:*
- `system_agent.py` - System agent management
- `sessions.py` - Session endpoints (rows + turn API; the Agent Detail surface retired in ent#358) — see [Resumable Turns](#resumable-turns)

**Services (`services/`)** — 67 service modules:

*Core:*
- `docker_service.py` - Docker container management (single point of Docker interaction, Invariant #11). Also owns the **tri-state** container-state pair `agent_container_states()` (batch, one `sparse=True` `containers.list()`) / `agent_container_state(name)` (single) added by #2196: `None` means *Docker could not be asked*, distinct from `{}` / `"missing"` meaning *Docker answered: no container*. Every older helper collapses those two into one falsy value, which is why a naive "drop agents with no container" reconcile empties a customer's roster on any Docker fault. `sparse=True` is load-bearing and constrains the code: the SDK default full-inspects **every** container, and under sparse `.name` returns `None` while `.labels` raises — so the key comes from `attrs["Names"][0]` and the status is classified **explicitly** to `running`/`stopped` rather than reusing `list_all_agents_fast`'s raw-status fall-through (which would pass `paused`/`removing` through to a consumer's `Literal`). `list_all_agents_fast`'s `[]`-on-fault contract is unchanged. **#2215:** `get_next_available_port(exclude=None)` is atomic-with-Redis-up — a per-port SETNX reservation (`port_alloc:{port}`, TTL 600s, no release: the container's `trinity.ssh-port` label becomes the durable truth) is the LAST allocation gate, and the label scan is **strict** (a Docker listing fault raises instead of degrading to the empty set, which would allocate 2222 over an existing fleet; `docker_client is None` demo mode keeps the empty set) — deliberately the OPPOSITE resolution to #2196's tri-state readers above, because a roster read must degrade to "unchanged" while an allocator must never degrade to "allocate over the fleet". Redis down ⇒ fail-open to today's racy allocation, converged by crud's bounded bind-conflict retry (D2) — never build on "atomic, unconditionally". `reserve_port_for_recreate` (SET no-NX, fail-open) re-asserts a recreating agent's own port across `recreate_container_with_updated_config`'s remove→create gap. `is_port_available` is netns-blind in production (documented weak filter)
- `docker_utils.py` - Docker utility helpers
- `template_service.py` - GitHub template cloning and processing; owns the tolerant `credentials:` readers (`credential_shape_errors` / `credential_mcp_server_names` / `credential_env_file_names` / `credential_mcp_env_vars` / `declared_credential_names`) — read paths degrade + surface `credential_errors`, the `.env` writer raises `CredentialDeclarationError` → 400 (ent#128) — see [template-processing.md](feature-flows/template-processing.md). **ent#89:** both builders also surface the declared `schedules:` (normalized) + `schedule_errors` via `services/template_schedules.py`, the same tolerant-reader shape; **both GitHub catalog list paths are now fenced** per-template (`_safe_build_github_template`) — they were bare list comprehensions, so a raise in the untrusted GitHub builder 500'd the whole GitHub half of `GET /api/templates` (ent#128 PR-A fenced only the local path). `fetch_template_metadata_for_create(repo, pat, ref)` is the **creation-path** metadata read: resolved PAT + parsed ref, cache-bypassed, loud on any failure — the catalog cache uses the global platform PAT off the default branch and would silently return `{}` for a private repo read with a per-user token. **ent#14:** the GitHub half of the catalog resolves **admin override → remote registry (`template_registry_service`) → `DEFAULT_GITHUB_TEMPLATE_REPOS`** in **both** `get_all_templates()` and `get_github_template()` (the second feeds `GET /api/templates/{id}` *and* agent creation, so one ladder or the display fields diverge by surface); registry entries arrive as `admin_override` dicts and are fenced so any failure degrades to the bundled floor. The per-repo fetch *reason* is now cached beside the metadata and surfaced as `metadata_unavailable`, because "declares no `template.yaml`" and "could not read it" were the same value — and `fork_to_own: required` silently read as absent under a rate limit. `fetch_template_metadata_result_for_create` is the reason-preserving form of the creation read (the plain wrapper drops the reason, which is fine for schedules and wrong for a security gate): the `fork_to_own` gate decides from IT, not from `gh_template`, because the catalog read answers 404 for a private repo the platform PAT cannot see and would classify a `required` template as absent
- "credential declaration standard" (ent#128): `credentials:` is FROZEN names-only; the sibling `credential_setup:` enriches it per variable and `normalize_credential_requirements(data, *, source_trust)` emits base-set-plus-overlay records as `credential_requirements` (never raises, never mutates — `_build_template` runs unfenced in `get_all_templates()`). Contract: [docs/schemas/trinity-agent-credentials.schema.json](../schemas/trinity-agent-credentials.schema.json)
- `template_registry_service.py` - Remote template registry (TMPL-002, ent#14): byte-capped **streaming** fetch (the ceiling counts WIRE bytes via `iter_raw()` — `Content-Length` is absent on chunked responses and trivially lied about, and `iter_bytes()` yields *decoded* chunks, so with httpx's default `Accept-Encoding: gzip, deflate` a legal-looking 199 KiB body inflated ~1030:1 to a 458 MB event-loop-thread allocation before the running total was consulted; any `Content-Encoding` is now refused before the body is read) + `follow_redirects=False` → `utils/safe_yaml.load_template_registry_yaml` (`AliasPolicy.REJECT`, the ent#314 rule pinned at the `utils/` layer) → allowlisted four-field parse → own TTL cache (3600 s + jitter, deliberately unaligned with the 600 s per-repo TTL to avoid a correlated herd; 7-day serve-stale cap; 60 s negative cache; cross-worker **generation counter** because a per-process invalidate half-applies under `--workers 2`; durable last-known-good as sanitized parsed JSON). Never raises — every failure returns `[]`. See [platform-settings.md](feature-flows/platform-settings.md)
- `template_schedules.py` - Tolerant `schedules:` reader (ent#89): `schedule_shape_errors` / `normalize_declared_schedules` over one private `_parse`, consumed by both `template_service` builders, the `crud` materializer, and compatibility check T-018. **Total by contract** — a raise would empty the catalog, enter the creation rollback fence, or fail-open T-018. Bounds (`MAX_DECLARED_SCHEDULES=20`, name/description/message limits), intra-block name dedupe, and strict cron/timezone via `schedule_validation.validate_cron_expression`. Stdlib + `schedule_validation` only — `template_service` imports it, so it must not import back. See [template-processing.md](feature-flows/template-processing.md)
- `agent_client.py` - HTTP client for agent container communication (chat, session, injection); hosts the transport circuit breaker — see [Circuit Breakers](#circuit-breakers-transport--dispatch-526)
- `settings_service.py` - Centralized settings retrieval (API keys, ops config, agent quotas)
- `a2a_gate.py` - Open-core seam for the A2A inbound allow-list: OSS registers no provider → any authenticated owner/shared caller is allowed; a private module can register one to further restrict caller identities. **Fails open** (a provider error never blocks an authenticated caller), so it is a restriction layered on auth, not a security boundary. A seam file — its comments describe the mechanism only and are grepped by `enterprise-docs-guard.yml` (#1461 class) (ent#157)
- `a2a_outbound.py` - Open-core seam for the OUTBOUND A2A target registry, and the **deliberate inverse of `a2a_gate`: it FAILS CLOSED** — no provider, a provider that raises, or a provider returning a malformed object all refuse the call, because this seam decides *where a credential is sent* whereas `a2a_gate` only restricts an already-authenticated caller. The `isinstance(ResolvedEndpoint)` check on the return value is load-bearing rather than defensive: under a stubbed `sys.modules` a `MagicMock` module returns a truthy endpoint with a mock `.url`, silently inverting fail-closed *inside the suite that proves it closed*. Ships the **OSS provider** — admin-managed named endpoints in `system_settings` as one AES-256-GCM envelope (Invariant #12's `elevenlabs_api_key_encrypted` shape), so OSS is functional with **no new table, no migration, no Alembic revision**. Shipping a working source rather than only the seam is deliberate: a seam with no registered provider resolves nothing, so the tool would answer "no targets configured" on every install. A private per-agent provider may register and take precedence (#736)
- `a2a_outbound_service.py` - Outbound orchestration: kill switch → rate bounds (per-agent **and** a fleet Redis key — a per-agent limit bounds one agent, the fleet is the exhaustion path) → resolve → validate → `effect_guard` → call → `agent_activities` row. Ordering is load-bearing: bounds before resolution so a flood can't become a DNS amplifier, and validation before the guard so a refused URL never burns an effect claim (#736)
- `a2a_client.py` - The protocol client, FastAPI-free (raises `A2ACallError`, mapped 1:1 at the router). One resolution → one validated IP → **pinned for both hops**, with the registered hostname carried for `Host` + SNI + cert verification, so DNS rebinding is *closed* rather than accepted as a residual (the template-registry validator can accept it; this request carries a credential). `trust_env=False` — every other control reasons about the target IP and a proxy makes the target irrelevant — with the CA context rebuilt explicitly, since that flag also disables httpx's `SSL_CERT_FILE`/`SSL_CERT_DIR`. Wire-byte ceilings over `aiter_raw()`; any `Content-Encoding` refused, not decoded; a 3xx is a failure on both hops; a wall-clock deadline over cancellable awaits, because httpx's `read` timeout is per-read and a trickling tarpit resets it forever. The card is a **hint**: its `url` must be same-origin (default-port-equivalent — Trinity's own card emits no port) and `securitySchemes` never selects the credential, which is why #736 ships while ent#159 is blocked. Errors ride the body on **HTTP 200**, so the body is parsed for `error` even on 200 (#736)
- `a2a_protocol.py` - Shared JSON-RPC/A2A vocabulary — error codes, the dialect table, envelope helpers — imported by **both** `routers/a2a.py` (inbound) and `a2a_client.py` (outbound) so the two cannot drift. Dialect defaults to **v0.3** (an absent version is the spec's back-compat rule); `1.x` is defined and deliberately **refused**, since no peer exists to verify it against. "Target v1.0 only" was rejected on evidence: Trinity's own card pins `0.3.0` and its server dispatches slash names, so a v1.0-only client cannot talk to Trinity and #738 federation would be dead on arrival (#736)
- `operator_intake_service.py` - Fire-and-forget, once-per-install opt-in operator intake POST at first-run; owns `installation_id` (trinity-enterprise#38)
- `mcp_auth_service.py` - #848 inline-login issue/verify + the per-call access gate behind `routers/mcp_auth.py`. Owns the **enumeration-safety contract** (#186): `/request` answers ONE constant 202 on every path — known, unknown, malformed, rate-limited, backend-threw — with **no audit row**, and does no branch-dependent work on the request path at all (the known-check, the cap read and the code INSERT all run in a Starlette `BackgroundTasks` task after the response flushes, because a committing write on only one branch measured ~1.9× even on in-process SQLite). `_email_is_known` **fails closed**, so a lookup error reads as "unknown" and mails nothing. Rate limits are **account-scoped, never per-IP** — `client_ip` is always the MCP server, so a per-IP bucket collapses every user into one and 30 wrong codes would lock inline login out fleet-wide (the #591 DoS); a global ceiling checked *before* the known-branch bounds the unknown side without becoming its own differential. `assert_email_may_reach_agent` is the load-bearing gate: the internal secret authenticates the caller, the asserted email's own standing authorizes the action
- `agent_mcp_key_service.py` - Agent MCP-key detection / self-heal / rotation (#1854): the in-container digest probe + verdict interpretation, `heal_agent_mcp_key_env` for the start-time drift path, and the rotation orchestration (fail-closed lock, capture-before-mint, spawn-id reconcile before delivery, captured-id DELETE, no plaintext returned) — see [agent-mcp-key.md](feature-flows/agent-mcp-key.md)
- `agent_runtime_state.py` - Single enumeration point for every name-keyed per-agent Redis keyspace; `clear_agent_breakers` (safe on a live container) vs `clear_agent_runtime_state` (adds slots; teardown only) — see [Circuit Breakers](#circuit-breakers-transport--dispatch-526) (#1560)
- `instance_identity.py` - `get_instance_label()`: the short label naming WHICH instance an outbound alert came from (`TRINITY_INSTANCE_NAME` → `FRONTEND_URL` host's first DNS label → `installation_id[:8]` → `None`). Stdlib-only leaf, never raises — consumed by the canary Slack sink today, reusable by any future webhook sink (#1987) — see [Canary Harness](#canary-invariant-harness-canary-001-411)

*Execution & Scheduling:*
- `task_execution_service.py` - Unified task execution lifecycle: slot mgmt, activity tracking, sanitization (EXEC-024); #678 reader-race auto-retry + #792 SUB-003 switch-retry (pre-raise 429/auth interception → single retry with the same execution_id after a successful subscription switch, so one-shot triggers recover instead of FAILED; see [task-execution-service.md](feature-flows/task-execution-service.md)); records dispatch-breaker outcomes (see [Circuit Breakers](#circuit-breakers-transport--dispatch-526)); hosts `apply_result` + the 202 dispatch path (see [Fire-and-Forget Dispatch](#fire-and-forget-dispatch-1083)). **Single terminal applier** — the chat split (#1483) delegates `/task` sync/async to `execute_task`/`apply_result` and never introduces a second applier. **#1853:** the FAILED branch of `apply_result` now **mirrors the SUCCESS branch's telemetry**, persisting `execution_log` (the sanitized stream-json transcript, via the same `sanitize_execution_log`), a `tool_calls` summary (#1741), and `claude_session_id` (UUID-validated agent-side) alongside the already-salvaged cost/context — so an `error_during_execution` (502) / timeout (504) FAILED row is diagnosable via the same API as a SUCCESS row, surviving agent stop+delete under the 30-day `execution_log_retention_days` window. The columns are added to the *existing* `db.update_execution_status` call **above** the `if won` gate — no new CAS writer, `_write_terminal_and_gate` untouched, and every side-effect stays gated on `won` (#1578/#1804). `_extract_agent_error` returns the transcript (3-tuple) that the agent's structured 502/504 body now carries (`_execution_error_502_detail` / `_timeout_504_detail` gained a validated `session_id` + `execution_log`). Residual: `_write_terminal_and_gate` terminals (backend timeout/budget/crash) and standalone-scheduler RETRY-001 FAILED writes still land bare
- `chat_execution_service.py` - Chat execution lifecycle behind `routers/chat.py` (#1483, Invariant #1): `/chat` setup + the **transitional** sync-chat `run_chat_turn` applier (the ONE divergent terminal writer — sync-chat does its own `chat_sessions` persistence + `mode="chat"` prompt `execute_task` doesn't; convergence onto `execute_task` is a tracked follow-up), plus the `/task` dispatch orchestration (derive/spoof, file upload, row+activity creation, async/sync fork delegating to `execute_task`) and `terminate_execution`. HTTP-free — raises `ChatDispatchError`, mapped 1:1 at the thin router
- `dispatch_admission_service.py` - Request admission for both `/chat` and `/task` (#1483): idempotency begin/replay + audit, the `/chat` pure-state breaker read (#526 F1), and `CapacityManager.acquire` for `/chat`. HTTP-free — returns `ChatAdmission`/`ChatAdmissionReplay`, raises the already-domain `CapacityFull`/`CircuitOpen`/`EphemeralBudgetExhausted`
- `chat_persistence_service.py` - Authenticated `/task` chat-session persistence (#1444): `persist_chat_session` (SUCCESS-guard + IDOR owner-check + fail-loud no-user-content ERROR log) + `persist_and_broadcast_chat_session` (chat_response_ready WS)
- `chat_signals.py` - Dependency-free leaf: the chat domain signals (`ChatAdmission`/`ChatExecutionContext` NamedTuples, `ChatAdmissionReplay`, `ChatDispatchError`) the chat services return/raise and the router maps to HTTP (#1483)
- `capacity_manager.py` - Unified capacity facade for admit/release/status — see [Capacity & Backlog](#capacity--backlog-428)
- `slot_service.py` - Internal to `CapacityManager`: atomic N-ary capacity counter (Redis ZSET, dynamic per-agent TTL) (CAPACITY-001)
- `backlog_service.py` - Internal to `CapacityManager`: persistent SQLite FIFO overflow store with drain-on-release (BACKLOG-001)
- `dispatch_breaker.py` - Per-agent dispatch circuit breaker (RELIABILITY-007, #526) — see [Circuit Breakers](#circuit-breakers-transport--dispatch-526)
- `scheduler_service.py` - APScheduler-based scheduling
- `cleanup_service.py` - Watchdog reconciliation + retention sweeps — see [Soft Delete & Retention](#soft-delete-retention--recovery-834-772)
- `idempotency_service.py` - Trigger-boundary dedup (`begin`/`complete`/`fail`) (RELIABILITY-006, #525; Invariant #18)
- `rate_limiter.py` - Shared sliding-window request limiter (#1023; see Container Security)

*Real-time delivery:*
- `event_bus.py` - Redis Streams transport for WebSocket delivery — see [Real-time Delivery](#real-time-delivery-reliability-003-306)
- `ws_ticket_service.py` - Single-use WebSocket auth tickets (C-002, #550) — see [Real-time Delivery](#real-time-delivery-reliability-003-306)
- `event_dispatch_service.py` - EVT-001 subscription dispatch primitives (`trigger_subscription`, extracted from the router) + the shared system-emit helper `emit_task_terminal_event`/`spawn_task_terminal_event` — see [Task Completion Events](#task-completion-events-1578) (#1578)

*Monitoring & Activities:*
- `activity_service.py` - Activity tracking and timeline; owns `close_execution_activity` / `spawn_close_execution_activity`, the single closer every CAS-won terminal writer calls (#1804) — see [Terminal-Activity Close Contract](#terminal-activity-close-contract-1804)
- `monitoring_service.py` - Fleet-wide health monitoring, 30s loop — authoritative for aggregate status; lifespan-resumed from persisted `monitoring_config`, default OFF (MON-001, #1121)
- `monitoring_alerts.py` - Alert threshold configuration
- `heartbeat_service.py` - Agent push-heartbeat liveness layer — see [Heartbeat Liveness](#heartbeat-liveness-reliability-004-307)
- `operator_queue_service.py` - Operating Room sync with agent containers (OPS-001); the agent-authored ingestion boundary enforces the per-agent depth/rate/size caps + reserved-id guard + leader lock (#1632)
- `sync_health_service.py` - Git sync health polling — see [Git Sync Health](#git-sync-health-389390)
- `canary_service.py` - Orchestration-invariant watcher — see [Canary Harness](#canary-invariant-harness-canary-001-411)
- `compatibility/` - Agent compatibility validation package (spec/collector/static_checks/ai_checks/fixes) — see [Agent Compatibility Validation](#agent-compatibility-validation-668)

*Auth & Credentials:*
- `credential_requirements_service.py` - Per-agent credential checklist (ent#127): a bounded `docker exec` probe for live `.env` key names joined against `normalize_credential_requirements`; `degraded` dominates the empty state — see [guided-credential-setup.md](feature-flows/guided-credential-setup.md)
- `setup_url_display.py` - UTS-46 nontransitional canonical host + eTLD+1 for an author-supplied `setup_url`, failing **closed** to inert text (ent#127). Leaf; zero deps on `template_service`
- `credential_encryption.py` - AES-256-GCM encryption for `.credentials.enc` and DB-persisted tokens (CRED-002, Invariant #12). Supports **online key rotation** (#267): an optional decrypt-only `CREDENTIAL_ENCRYPTION_KEY_SECONDARY` (the previous key) keeps old-key ciphertext readable while new writes use the primary; `rewrap()` + `scripts/deploy/rotate-credential-key.py` re-encrypt persisted DB secrets onto the new key (runbook `docs/migrations/CREDENTIAL_KEY_ROTATION.md`)
- `subscription_service.py` - Subscription management (SUB-002); owns `derive_auth_mode` — the ONE auth-mode enum derivation shared by `AgentAuthStatus` and the #471 pressure batch endpoint
- `subscription_headroom_service.py` - Live subscription headroom (#471): actual 5h/7d utilization % + reset times read from the `anthropic-ratelimit-unified-*` headers of a `max_tokens=1` micro-ping probe (the `/api/oauth/usage` endpoint is 403-scope-dead for stored `sk-ant-oat01-` setup tokens — the closed-PR #2170 mechanism, established 2026-08-19). Click-to-refresh (floored ≥60s/subscription) + default-ON ambient refresh (`subscription_headroom_auto_refresh` setting, 15-min floor, demand-driven). **Fail-CLOSED without Redis** — `_read_snapshot` is tri-state (`(redis_ok, snapshot)`; a client object existing ≠ a reachable server), so a Redis outage yields observed-only, never a probe storm; the dashboard batch path uses `wait=False` (stale-while-revalidate via strong-ref background task) so a hung provider can't wedge the 60s poll. Snapshot: Redis `subscription:headroom:{id}` (7d TTL); probes single-flighted (#1920); a probe 429 updates snapshot status only, never `subscription_rate_limit_events`. `services/subscription_headroom_service.resolve_rate_limited_now` is the ONE `rate_limited_now` derivation (#2157 one-gate rule), consumed by both `decorate_usage` and `pressure_states`. **It is three-state, not an OR (#447):** fresh provider verdict says limited → limited; fresh verdict says allowed → NOT limited; no usable verdict → the 2h event predicate. It shipped as `db_predicate OR fresh_verdict`, which made a fresh, authoritative *"allowed, 32% used, resets 19:10"* structurally powerless to clear the badge — because **nothing clears a failure row on success**: `clear_rate_limit_events` has had zero production callers since #444 removed the one call (clearing was destroying auto-switch's detection signal), so the event half only ever decays with the clock. Observed live: two subscriptions wearing `LIMIT` while every agent on them answered normally. The db predicate is an *inference from past failures*; a probe is *ground truth about now*, so it wins in both directions. `_headroom_indicates_healthy` is deliberately **not** the negation of `_headroom_indicates_limited` — "not limited" is also true for a stale snapshot, a rejected token and a transport error, none of which are evidence of headroom, so all three fall through to the predicate rather than clearing it (#2353's rule, preserved). **Both predicates judge a window against ONE named allowlist, `NON_BLOCKING_WINDOW_STATUSES` (#2396)** — an ALLOWLIST of statuses meaning "requests are being served", never a blocklist of blockers, so an unrecognised status still reads as limited (the inverse mistake is the #848 deny-check lesson). It shipped as the bare literal `("allowed",)` in one predicate and `(None, "allowed")` in the other, which made the provider's own near-the-limit tier `allowed_warning` read as a hard rate limit on every surface — a healthy subscription approaching its weekly window wore `LIMIT` while the provider was still serving it (observed live: a snapshot recording `seven_day: 90%, allowed_warning` beside 47 successful executions and zero `subscription_rate_limit_events`). `allowed_warning` now also counts as **positive proof of headroom** — the quota was reached and the answer was yes — so it clears a stale 2h db predicate; excluding it would be #447 returning in a narrower window. Note the ordering that bounds the blast radius: a real HTTP 429 sets the top-level snapshot `status` and is checked BEFORE any window, and the db predicate is a third independent detector, so the window arm is the weakest of the three. This is the DISPLAY predicate only: auto-switch candidate filtering reads the kind-blind `has_recent_subscription_failures`, so a just-recovered subscription is still skipped as a switch target and #444's ping-pong cannot return. **The event half of that predicate counts `failure_kind = 'rate_limit'` ONLY (#2352)** — it was kind-blind, so an auth failure (401/403: a dead, expired, or `.env`-shadowed token) set the flag and every surface reported a credential problem as quota exhaustion, sending the operator to wait out a window that was never full; the display layer had honoured the split since #471 (`rateLimitEventCount` reads the `rate_limit` kind alone) and the predicate was the layer that had not caught up. NULL `failure_kind` (pre-#471) is excluded — unknown is never promoted to "429". This is a **split, not a filter**: `db.has_recent_subscription_failures` preserves the kind-BLIND semantics for the two *candidate-selection* readers (`select_best_alternative_subscription`, `select_subscription_for_new_agent`), which must keep skipping a subscription that failed for ANY reason — narrowing them in place would have auto-switched agents onto subscriptions whose token had just been rejected (the #444 class). Two predicates, two meanings; re-merging them is how the bug happened. `pressure_states` therefore also emits `auth_failures_24h` beside the total, since after the split a dead-token subscription is no longer `rate_limited_now` and a bare total cannot tell the badge which word to use. The observed (DB-derived) arm is always populated; SUB-004's windows are **deduped since #471** (executions = sole cost/context/turn source; `chat_messages` contributes `output_tokens` only — a `/chat`/persisted-`/task` turn writes cost into BOTH tables). **ent#433 adds the durable half** — `_probe_and_store` now also writes each probe to `subscription_headroom_history`, so utilization TRENDS survive a snapshot that overwrites itself every probe. Three properties are load-bearing: the history write runs **after** `_store_snapshot` and **off the event loop** (`asyncio.to_thread`; a sync SQLAlchemy write on the loop stalls everything for up to the 30s busy timeout when it lands during the 03:30 backup or 04:30 VACUUM, and `try/except` handles errors but not blocking) — order pinned by test; it catches `Exception`, never `BaseException`, so shutdown's `CancelledError` still propagates; and it adds **no probe**, inheriting #471's entire rate-bounding envelope (60s floor, single-flight, fail-closed ambient) by sitting inside `_probe_and_store`. `get_history(subscription_id, window)` is the read — pure DB, never probes, so viewing a trend costs no quota
- `ssh_service.py` - Ephemeral SSH credential generation
- `email_service.py` - Email sending for verification codes

*Git & GitHub:*
- `git_service.py` - Git sync operations for GitHub-native agents; persistent-state allowlist primitive (S4, #383); `rebind_origin_and_push` + `inspect_container_git` — the in-container half of the ent#109 repo binding (push committed history by explicit URL, THEN repoint `origin`, so a push failure leaves the agent untouched; reads `origin` back to prove the rewire took)
- `github_service.py` - GitHub API client (repo creation, validation, org detection, branch listing)
- `agent_service/repo_binding.py` - Post-creation repo binding orchestration (ent#109) — see [Post-Creation Repo Binding](#post-creation-repo-binding-ent109). HTTP-free: raises `BindError`, mapped 1:1 at the thin `routers/git.py` endpoint (Invariant #1)
- `agent_service/fork_to_own.py` - Fork-to-own template copy (trinity-enterprise#93): a template declaring `fork_to_own: required` is copied at creation into a **user-owned** repo (private by default; the user's PAT creates + pushes, then persists as the per-agent PAT #347 so recreates never fall back to the platform PAT). Origin = the user's repo; `GIT_UPSTREAM_REPO` env + a credential-less `upstream` remote (startup.sh) keep template updates one `git pull upstream` away. Destination collisions: empty or template-tip SHA-match → reuse; already bound to a live agent or holding other data → 409. **ent#109** extracted the destination half into `inspect_or_create_destination_repo()` (`created | empty | branches`) + `validate_destination_pat()`, shared with the post-creation rebind; the reuse/refuse POLICY stays in each caller, because this path's reuse branch IS the template-tip SHA comparison
- `agent_service/crud.py` - Agent create/delete. `create_agent_internal` is a **thin orchestrator over fenced phase-helpers** (#1484): CC-trivial gates + the `if docker_client: try/except/else` stay inline (so *what* is caught is byte-identical), while each fat phase (ephemeral pre-gate, template/fork resolution, config staging, env build, volume mounts, container create, register, materialize) is a private `_*` helper returning into the orchestrator's locals. The except/else read a single orchestrator-populated `_RollbackHandles` dataclass (agent MCP key + git-config reservation + ephemeral slot + the ent#313 `container_floor_ts`). **Container reclaim on failure (ent#313):** `_rollback_failed_creation` stays DB/quota-only, but the except-path now also awaits `_reclaim_failed_creation_container`, which removes the container and clears the name-keyed Redis keyspace (`clear_agent_runtime_state`, #1560). The old docstring deferred both to "the cleanup watchdog" — none exists for a **non-ephemeral** agent (`_sweep_ephemeral_agents` is gated on the `trinity.ephemeral` label), so a post-`containers.run` failure leaked a running, ownerless container that also pinned its workspace volume, blocking #1581's unattached-strike counter forever (a phantom in the Docker-as-truth listing, Invariant #11). When the create returned a handle the removal is unambiguous; when it did not (the observed 60s Docker read timeout — the daemon created it, the client never got the handle) the container is re-derived by name behind three fail-closed gates: not a 409 name conflict, no `agent_ownership` row, and a `trinity.created` label at or after `container_floor_ts`. Anything unprovable refuses — a false negative costs one manual `docker rm -f`, a false positive deletes a running agent (possibly another install's, on a shared daemon). **Do not re-monolith it** and keep every helper < 100 SLOC / CC < 20. The `github:`+fork phase stays OUTSIDE the try (so `FORK_*` 4xx don't flatten to 500), and the network is hard-coded in `_create_agent_container` (AC #5). Module split to `creation_phases.py` is the deferred #1028 follow-up. **Tokenless public templates (ent#123):** a `github:owner/repo` create with NO resolvable PAT is allowed when the repo is public — source-mode only (`_gate_tokenless_request`: non-source-mode → named 400; also normalizes the resolver's `""` to None), validated via a credential-less `git ls-remote` probe (`git_service.probe_anonymous_repo_access` — same transport as the clone, immune to the anon REST cap; definitive not-found/auth-challenge → combined "not found or private" 400, transient → fail-closed 502), env carries repo+sync flags but no token vars (`_apply_github_env`; the rebuild seams gate on repo the same way — see `lifecycle._apply_git_env_from_db` below), startup.sh clones anonymously (`GIT_TERMINAL_PROMPT=0`, blackholed push remote, workspace-`.env` PAT fallback on restart), and backend push paths (`sync_to_github`/reset) pre-check baked env OR the per-agent PAT row → `no_write_credentials` 409 — whose message now points at **Bind to your own repo** on the agent's own Git tab (ent#109) rather than the retired create-a-new-agent-and-import workaround. Requires a rebuilt base image (old images silently skip the tokenless clone — release-note ordering requirement). **Template-declared schedules (ent#89):** `_TemplateResolution.declared_schedules` is a normalized carrier populated by **both** resolver branches — the `github:` branch reads its own fresh `template.yaml` via `fetch_template_metadata_for_create` (resolved PAT + parsed `@branch` ref, cache-bypassed), the `local:` branch normalizes `tr.template_data`. It is deliberately NOT folded into `template_data`, which is raw YAML on the `local:` path and `{}` on the `github:` path (which has never populated it — hence #383's `persistent_state` and #1169's `data_paths` being effectively `local:`-only) and whose truthiness gates `_stage_config_files`' credential-file generation. `reconcile_declared_schedules(agent_name, declared, owner_username)` runs inside `_materialize_agent_files` beside the `persistent_state`/`data_paths` steps — non-fatal, ghost-skipped at the caller, name-match idempotent, and it CHECKS `db.create_schedule`'s falsy return (it returns `None` on three paths and never raises).
- `agent_service/lifecycle.py` - Agent start/stop + the two container-rebuild paths. **`_apply_git_env_from_db` is the single owner of the GitHub-sync env block across both** (ent#109): `recreate_container_with_updated_config` (config drift — production callers: `start_agent_internal`, fired by nine predicates **and** base-image drift at cold start, so a base-image rebuild reaches the whole fleet; plus ent#109's bind path, which calls it directly and so carries its own `clear_agent_breakers` — the #1560 clear lives at the call site, not in the helper, because `start_agent_internal` gates it on `needs_recreation or not was_already_running` and moving it in would clear breakers on paths that today do not) and `_apply_persisted_auth_env` → `recreate_missing_container` (rebuild from nothing). Before this the block lived only on the second path and the first re-derived just `GITHUB_PAT`, replaying whatever `GITHUB_REPO`/`GIT_SYNC_*` the old container carried. Three properties, each load-bearing: (1) the **PAT gate is a required per-call-site parameter, never a default** — `per_agent_only` (config drift) preserves #211 verbatim so a global-only platform PAT is never injected into a previously-tokenless container, while `effective` (rebuild-from-nothing, no old container to inherit from) uses the 2-tier per-agent→global resolver; sharing one gate would bake the platform PAT into tokenless agents, `configure_push_remote` would clear the push blackhole, and a private KB could reach the shared public upstream (the ent#162 class). An AST guard in `tests/unit/test_ent109_git_env_seam.py` pins the **writer set** — `{recreate_container_with_updated_config: per_agent_only, _apply_persisted_auth_env: effective}` exactly — so CI fails both on a flipped gate and on a *third* writer appearing on any container-seeded path (the bug ent#109 fixes was two writers with one wrong). (2) **Set-or-clear** over `_GIT_ENV_KEYS`, since the config-drift path writes into a dict seeded from the old container — a deleted `agent_git_config` row pops the whole set (incl. an orphaned `GITHUB_PAT`: the per-agent token is a column *on* that row, so no row ⇒ no per-agent credential and no repo), a `source_mode` flip clears the mode/branch pair; `GITHUB_PAT` stays set-only *while a repo is bound*. (3) **`GIT_SYNC_AUTO` = DB `auto_sync_enabled` OR baked env**, derive-only — the two creation writers in `crud.py` genuinely disagree (`and not config.ephemeral` sits inside a swallowing try/except on the DB side only; column default `0`), so DB-only derivation would silently stop auto-push for that slice of the fleet. **No write-back**: `PUT /{agent}/git/auto-sync` writes only the row while the agent gates on env, so "baked true / DB 0" is *also* exactly an owner's explicit disable — a backfill would erase that intent, and since `PUT .../auto-sync` is `OwnedAgentByName` while `POST .../start` is `AuthorizedAgentByName`, it would let a shared non-owner (or an agent key carrying its owner's role, trinity-ops-agent#232) flip an owner-only flag. The disagreement is logged, not resolved. (4) **Correct, never introduce** on the config-drift path: the block is written only when the old container already carried `GITHUB_REPO` or a PAT resolves. Repo-gating the block while per-agent-gating the PAT would otherwise hand startup.sh `GIT_SYNC_ENABLED=true` with no token for an agent bound via `POST /{agent}/git/initialize` on the *global* platform PAT — whose only credential lives in the container's `.git/config` — and the restart branch would rewrite origin to the credential-less URL and blackhole its push remote. `effective` is exempt (no old container; not introducing the block there is the #843/#1439 silently-empty-agent bug). `GIT_WORKING_BRANCH` and `GIT_UPSTREAM_REPO` are deliberately **not** owned here (only read by startup.sh's clone branch / no DB column). Making the #389 toggle authoritative is a tracked follow-up.

*Integrations:*
- `slack_service.py` - Slack API client (OAuth, messaging, verification) (SLACK-001)
- `nevermined_payment_service.py` - x402 payment verification and settlement (NVM-001)
- `proactive_message_service.py` - Agent-to-user proactive messaging with rate limiting and audit (#321)
- `channel_completion_report.py` - Reports a delegated/background execution's terminal back to its originating channel chat/thread (ent#224 Slack, ent#265 Telegram): inherited-context-only (never inline turns), binding-agent consent + delivery, effect-guarded at-most-once — see [channel-completion-report.md](feature-flows/channel-completion-report.md)
- `channel_history.py` - Persists a delivered proactive **group/channel** broadcast into the channel session (#1649), so the agent has a record of its own outreach. Session keys are derived by driving the channel adapter's own `get_session_identifier()` (never re-implemented — that drifts). **Slack = real recall**: channel sessions are thread-scoped, so a broadcast filed at its own `ts` IS the session an in-thread reply resolves to (needs `slack_service.send_message_detailed()` to return the ts). **Telegram = bookkeeping only**: group sessions are per-(sender, chat) with no group branch, so a broadcast uses a synthetic agent-sender key nothing else writes to — recorded but NOT recalled; real recall needs a per-chat group session (a behaviour change for existing inbound groups). `#903` shared-thread attribution (`sender_email=None`); persist on confirmed delivery only; fail-soft
- `tts_service.py` - Shared outbound-voice TTS layer (epic #24): ElevenLabs synth → ffmpeg OGG/Opus transcode; shared char cost-cap; fail-soft (any error → text fallback). Key resolved at call time via `settings_service.get_elevenlabs_api_key()` (stored setting → env, ent#117), not the frozen config value. Consumed by `voice_reply_service` (ent#117) and the STT path. Also owns the **one voice gate** every surface shares (#2157): `resolve_voice_id(agent)` / the pure `resolve_voice_from_config(...)` = platform key AND agent-level `tts_voice_replies_enabled` AND (own `tts_voice_id` else platform default) — read by the channel path, the Workspace roster card's `voice_available`, the portal `/tts` endpoint, and the narrated-surface prompt, so the surfaces can no longer disagree about whether an agent may be spoken aloud. **Voice INPUT is a separate bit (#2212):** dictation needs the platform key only — nothing is spoken back — so the Workspace card also carries `stt_available` = `tts_service.is_available()`, exactly the `/stt` gate. The two cannot be collapsed: on a key-but-no-effective-voice agent `voice_available` is false while transcription works. The client prefers the server path (MediaRecorder → `POST /stt`, which answers with real statuses and messages) over the browser Web Speech API whenever `stt_available` is set — Web Speech is a browser-hosted service that reports no event at all in Chromium (measured) and ends at the first pause; it stays the no-key fallback, and with neither path available the mic is not rendered
- `voice_reply_service.py` - Per-message voice-reply delivery (ent#117): backs the `send_voice_reply` MCP tool. Given an agent + resolved channel destination (channel/chat id/thread from the execution) + text, gates on TTS availability + agent-level enable + the per-channel flag, wraps delivery in `effect_guard("voice_reply", …)` (#1084), synthesizes, and delivers via each channel's send primitive (Telegram `_send_voice`, Slack `slack_service.upload_file`, WhatsApp `create_share_from_bytes` + Twilio `MediaUrl`). Fail-soft → not-delivered so the agent falls back to text. Replaces the old always-voice adapter path (`_maybe_send_voice` removed) — replies are TEXT by default, voice is a per-message agent choice
- `agent_shared_files_service.py` - Outbound file sharing — see [Outbound File Sharing](#outbound-file-sharing-files-001)
- `loop_service.py` - Sequential agent loop runner — see [Sequential Agent Loops](#sequential-agent-loops-740-ui-1106)
- `reminder_service.py` - Agent self-reminder create (bounds + timeout clamp + provenance + relative→absolute) — see [Agent Self-Reminders](#agent-self-reminders-1296) (#1296)
- `client_roster_service.py` - Aggregates external channel clients (Telegram + WhatsApp) into the Sharing-tab roster; cross-channel sort + per-channel failure degradation (#20)
- `voip_service.py` - VoIP outbound-call orchestration — see [VoIP](#voip-telephony-voip-001-1056)

*Content & Media:*
- `image_generation_service.py` / `image_generation_prompts.py` - Platform image generation via Gemini (IMG-001)

*Skills & System:*
- `skill_service.py` - Skills library sync + full-directory package injection (ent#183): `git archive`-sourced tars via the existing agent-server restore primitive, tree-SHA versioning, manifest prune, declaration-only dep check — see [skill-injection.md](feature-flows/skill-injection.md). **ent#236** adds the removal half — `remove_skills` (manifest-driven, takes the same per-agent inject lock) and `reconcile_agent_skills` (start-path diff of the agent's platform-managed skill dirs against the assignment set, so a removal reaches an agent that was stopped when it happened), plus durable sync status in `system_settings` (the in-process `_last_sync` is invisible to the other uvicorn worker). **ent#237** makes it multi-source: `sync_library` iterates every enabled `skill_sources` row under ONE library-wide lock, one failing source never blinds the others (per-source outcomes, aggregate succeeds if any synced), and `commit_changed` — the gate ent#236's fleet re-inject fires on — is computed per source against that source's own durable `last_commit_sha` and OR'd. `services/skill_source_clone.py` owns one checkout's git lifecycle (clone/fetch/reset/checkout, tag-pin refusal on **both** the update and clone paths, non-repo quarantine); `skill_service` orchestrates N of them. **Editing a source has to reach disk**, which took three fixes of one shape (row moved, checkout didn't): (1) `_update_*` fetches **`origin`**, written at clone time, so a repointed `url` kept pulling the OLD repo forever while reporting `success` with a moving commit that fed the fleet re-inject — `sync` now compares `origin` against the configured remote (`canonical_remote`, credential-stripped so a PAT-bearing `origin` isn't a false repoint) and **discards + re-clones** on a genuine mismatch, since `remote set-url` would leave the old repo's tag refs behind and a tag name present in both at different commits then reads as a moved pin forever; an unreadable `origin` counts as a match, because the action gated on it is an `rmtree`. (2) `update_source` clears the sync bookkeeping on a `url`/`ref`/`ref_type` change — without it the documented tag-bump path (`v1` → `v2`) compared v2 against v1's recorded SHA and was refused as `moved_tag`, and on the fresh-clone path that refusal `rmtree`s first, so bumping a tag emptied the source. Non-identity edits (name/enabled/priority) must NOT clear it: a cleared baseline on disable would let a tag moved during the disabled window be adopted silently on re-enable. (3) `discard_source_checkout` reclaims `<source_id>/` + `<source_id>.broken` on delete, with `_reclaim_orphan_checkouts` (full sweeps only, **fail-closed** on the row read, id-shape-scoped so the legacy checkout and operator dirs are out of scope, logs what it reclaims per #1644) as the backstop for the crash window and for installs predating the fix. **ent#332** makes each source's layout root resolvable — `catalog.yaml` `skills_root:` (ent#314 hardened parse, segment-wise validation, realpath containment) → evidence-gated `skills/` probe (dual-layout keeps legacy + flags `layout_conflict`) → `.claude/skills/` fallback; every invalid tier falls through, and `skill_packaging.filter_skill_archive(source_root=…)` rewrites tar arcnames to the canonical agent-side `.claude/skills/` destination so manifests/prune/removal stay destination-canonical with zero migration (requirements §21.1.4)
- `skill_packaging.py` - Pure skill-package primitives (ent#183): hardened frontmatter contract parse, archive member vetting, injection-tar assembly, prune diff; `compute_removal` (ent#236) is `compute_prune` against an empty new manifest, so confinement + cap live in one place
- `skills_sync_service.py` - Scheduled skills-library auto-sync + fleet-wide re-inject (ent#236). Leader-locked (`skills:sync:leader`), both flags default OFF; sweeps only when the library commit actually changed, over **running** non-ghost agents at bounded concurrency with skip-and-report on inject-lock contention. Backend-hosted rather than in the standalone scheduler because the sweep must reach agent containers and the scheduler is platform-network-only
- `system_agent_service.py` - System agent lifecycle. **#1816:** `ensure_deployed` is read-only when the container is running (3-state `check_base_image_state` → `base_image_state`, WARNING + an edge-triggered `base-image-stale-` operator alarm on `stale` only, **never** on `unknown`) and delegates to `start_agent_internal` when it is stopped, so the cold boundary adopts a rebuilt base image through the shared lifecycle instead of a bare `container_start`. Creation converges on the recreate path's contract (`TRINITY_AGENT_AUTH_TOKEN`, `trinity.full-capabilities`) so no predicate is permanently false. The generic `recreate_missing_container` rebuild **refuses** `trinity-system` (409, ADOPT-006) — it reconstructs a *regular* agent and would irreversibly downgrade the orchestrator (system-scoped MCP key deactivated for an agent-scoped one); `ensure_deployed`'s create branch is the only supported rebuild
- `cornelius_agent_service.py` - First-run auto-seed of the default "Cornelius" second-brain agent (public `github:Abilityai/cornelius`, cloned anonymously on the ent#123 tokenless path, Brain Orb enabled) — see [Brain Orb](#brain-orb--self-rendering-mind-page-58-trinity-enterprise) (trinity-enterprise#107, source-seeded #1656). Invoked via the ent#124 first-run orchestrator; accepts an optional precomputed `fresh` verdict. **#2215:** a `create_failed`/`create_blocked` result (or a belt-except raise) is no longer log-only — the orchestrator raises a `system-seed-cornelius-failed` operator-queue alert; the seed flag stays unset, so the next boot retries (#1790 asymmetry untouched)
- `system_seed_service.py` - First-run seed of the default system manifest (trinity-enterprise#124): `ensure_first_run_seeded()` (both call sites: setup-completion bg task + lifespan safety-net) resolves a **persisted first-run verdict** (`first_run_fresh` — computed once BEFORE Cornelius provisions, so sibling-seeded agents can't poison later passes), runs the Cornelius seeder, then deploys the bundled `config/manifests/default-system.yaml` (env override/disable via `TRINITY_DEFAULT_SYSTEM_MANIFEST`) through `system_service.deploy_manifest` — durable `default_system_seeded` flag + `system_seed:provision` lock (via the shared `SingleFlightLock` #1920: ownership-checked acquire with a unique per-acquire token + compare-and-delete release, the lock kept LOCAL to the seeding pass — never on the module-level singleton — so two overlapping in-process passes can't clobber each other's ownership state and leak the loser's lock for the TTL) + reserved-name existence backstop (the deploy path suffixes collisions instead of 409ing); partial/failed seeds raise an operator-queue alert; fail-open, never blocks boot. **#2215:** the whole pass additionally runs under ONE pass-level lock (`first_run_seed:provision`, TTL 900s, via the shared `SingleFlightLock` #1920 — unique per-acquire token + compare-and-delete release in `finally`; fail-open) — the two inner seeder locks serialise each seeder only against itself, so worker A's slow Cornelius clone used to run concurrently with worker B's fleet deploy (the port-collision / concurrent-transient burst that latches `partial`); the loser skips the ENTIRE pass and writes no flags, and a failed Cornelius seed raises `system-seed-cornelius-failed`. **The #1920 lock fix is class hygiene, NOT the double-seed guard**: the real double-seed window is a >600s deploy outliving the 600s TTL — a double-*acquire* that a token + compare-and-delete does not touch (system_seed adopts **no** lease refresh, so its lock sits near-expiry for the whole deploy, unlike ops which refreshes every iteration), and the **reserved-name existence backstop** remains the actual convergence guard. See requirements §16.5.1 (roadmap.md)
- `system_service.py` - System manifest operations; owns the full deploy orchestration `deploy_manifest()` (moved out of `routers/systems.py` in ent#124 — Invariant #1; the router is a thin HTTP wrapper, and the default `create_agent_fn` lazily resolves the `routers/agents` ws-broadcasting facade). ent#126 adds the pure resolvers `resolve_permission_edges`/`resolve_schedule_previews` that BOTH the dry-run preview and the writers (`configure_permissions`/`create_schedules`) consume, so the preview cannot drift from the deploy on the fields it models (ent#89's duplicate-name skip is a deploy-side read the pure resolver does not make — see `resolve_schedule_previews`); `_preflight_template` also validates **merged** resources through the create path's own `normalize_cpu`/`normalize_memory`; plus the `TRINITY_MANIFESTS_DIR`-rooted catalog readers `list_bundled_manifests`/`read_bundled_manifest` (layered path confinement — incl. an `is_symlink()` check on the **unresolved** entry, for parity with the listing that skips symlinks outright — open-once + `fstat` + `O_NOFOLLOW`, fail-soft per file with every `reason` exiting through the deploy report's own `_failure_reason` sanitizer)
- `log_archive_service.py` / `archive_storage.py` - Log archival + storage backend
- `session_cleanup_service.py` - Session JSONL reaper — see [Resumable Turns](#resumable-turns)
- `db_vacuum_service.py` / `audit_retention_service.py` - Daily maintenance jobs (see Background Services)
- `db_backup_service.py` - Daily database backup for BOTH backends (#2216) — see [Automatic Database Backups](#automatic-database-backups-2216); its stdlib-only copy/verify/prune/boot-hook leaf is `db/backup_primitives.py` (import-free of `database`/services — it runs inside `init_database()` at import time)

**Channel Adapters (`adapters/`)** — pluggable external messaging (SLACK-002, Invariant #9):

- `base.py` - `ChannelAdapter` ABC, `NormalizedMessage`, `ChannelResponse` models. Default-no-op `record_inbound_activity(message, agent_name)` hook (#1533) — the router calls it once per *delivered* DM at step 5c (after the access gate, skipped for groups, best-effort) so Telegram/WhatsApp can upsert their chat link and bump the `message_count`/`last_active` the Sharing-tab roster reads; Slack/VoIP inherit the no-op. In-flight **progress-indicator seam** (ent#264): default-no-op `indicate_progress(message, elapsed_seconds)` hook + `progress_threshold_seconds`/`progress_interval_seconds` capability attrs (`None` threshold ⇒ the router never arms a driver — Slack/WhatsApp/VoIP today); all per-turn indicator state rides `NormalizedMessage.metadata` (adapters are long-lived singletons handling concurrent turns)
- `message_router.py` - `ChannelMessageRouter`: rate limiting, agent resolution, execution pipeline; injects MEM-001 per-user memory into `execute_task(system_prompt=…)` gated on `verified_email and not is_group` (#895), and MEM-001 summarization is **sender-filtered** — `get_recent_public_chat_messages(session_id, sender_email=user_email)` so a thread-scoped multi-participant session never feeds one user's turns into another's durable memory (#903); calls the adapter's async `enrich_message` hook then prepends a `[Channel: #x]\n[From: …]` identity prefix for the **current** turn (#350) while **history** is attributed per stored `sender_label` (persisted with each channel user turn, #903 — replayed as `Alice:`/`Bob:` in `build_public_chat_context`); passes the agent's public avatar URL as `agent_avatar_url` so channels with a per-message bot icon render it (Slack `icon_url`, best-effort — #292); owns the per-turn **progress driver** (ent#264) — armed after the step-8 `indicate_processing` (both wrapped so a raising hook never aborts the turn), ticks `adapter.indicate_progress` past the adapter's threshold, and resolves at all three terminals via `_resolve_indicator` (cancels AND awaits the driver dead, settles the shielded in-flight placeholder send, THEN `indicate_done`) with an idempotent `finally` backstop — inline-sync coupling: bound to channel turns executing inline (`execute_task` awaited); if #1081/#1083 ever move channel triggers off the inline path, the successor is the durable seam ent#265 rides
- `slack_adapter.py` - DMs, @mentions, thread replies, agent identity via `chat:write.customize`; inbound file downloads SSRF-gated two-tier like WhatsApp (#1951, the #1932 shape): credentialed hop 1 `*.slack.com` only (the bot token's blast radius), validated 30x targets also `*.slack-edge.com` / `*.slack-files.com`, `follow_redirects=False` with every hop re-validated before it is issued and a bounded budget; https-only; refusals log at ERROR (a quiet fail-closed media gate is how #1932 hid a three-month outage); `enrich_message` resolves sender display name + channel name via `users.info`/`conversations.info` so the agent sees who/where (best-effort, #350); outbound voice replies are per-message (ent#117) via the `send_voice_reply` tool → `voice_reply_service` (Slack path = inline MP3 through `slack_service.upload_file`); the adapter no longer speaks replies unconditionally
- `transports/slack_socket.py` - Socket Mode: N concurrent WebSockets per `SLACK_SOCKET_CONNECTION_COUNT` (default 2, range 1–10), per-client watchdog, envelope-ID dedup ring (#244)
- `transports/slack_webhook.py` - HTTP webhook transport (production fallback)
- `telegram_adapter.py` - DMs, group chats (@mention/observe modes), voice transcription, /login flow; in-flight status indicator (ent#264): 👀 reaction ack at dispatch (`setMessageReaction` — ✅/⚙️ are NOT in the allowed bot reaction set, so the Slack ⏳→✅ pattern doesn't port; reaction CLEARED at every terminal, no success-👍 swap) + 30s-threshold elapsed-time placeholder maintained via `editMessageText` at 60s cadence (`disable_notification`, static template text only, deleted at terminal with neutral edit-to-done fallback), gated by a default-ON per-binding toggle (`telegram_bindings.progress_indicator_enabled`, `PUT /api/agents/{name}/telegram/progress-indicator`, human-only) and in groups on @mention/reply triggers or `all` trigger mode (observe stays typing-only), all fail-soft with a 2-consecutive-failure degraded quiesce; outbound voice replies are per-message (ent#117) via the `send_voice_reply` tool → `voice_reply_service` (`sendVoice`); the adapter no longer speaks replies unconditionally
- `transports/telegram_webhook.py` - Telegram Bot API webhook (inbound POST + setWebhook registration)
- `whatsapp_adapter.py` - DMs via Twilio (WHATSAPP-001); media downloads SSRF-gated two-tier: source/credentialed hop `*.twilio.com` only, validated 30x targets also `*.twiliocdn.com` (the media CDN, #1932); `/login`/`/logout`/`/whoami` commands + markdown→WhatsApp syntax conversion (#467); outbound voice replies are per-message (ent#117) via the `send_voice_reply` tool → `voice_reply_service` (`audio/ogg` note hosted via `create_share_from_bytes(require_sharing_enabled=False)` → Twilio `MediaUrl`); the adapter no longer speaks replies unconditionally
- `transports/twilio_webhook.py` - Twilio webhook: HMAC-SHA1 signature validation, MessageSid dedup, form-encoded body
- `transports/twilio_media_stream.py` + `transports/voip_audio.py` - VoIP Media Streams bridge (a voice transport, NOT a text `ChannelAdapter`) — see [VoIP](#voip-telephony-voip-001-1056)

Channel DB modules: `db/slack_channels.py` (workspace connections, channel-agent bindings, active threads), `db/telegram_channels.py` (bindings, group configs incl. the per-group `allow_proactive` completion-report consent ent#265, chat links), `db/whatsapp_channels.py` (bindings, chat links, verified-email lookup), `db/voip.py` (voice bindings, call logs, daily-cap window). All persisted tokens AES-256-GCM encrypted (Invariant #12).

### Frontend (`src/frontend/`)

**Key directories:** `src/views/` (page components), `src/stores/` (Pinia state), `src/components/` (reusable UI), `src/utils/` (WebSocket client, helpers, `markdown.js` with DOMPurify).

**Stores (domain-scoped, Invariant #6):**
- `stores/agents.js` - Agent CRUD, chat, activity
- `stores/auth.js` - Email/admin authentication + JWT
- `stores/collaborations.js` - Collaboration graph state, WebSocket integration
- `stores/loops.js` - Sequential agent loops UI state, agent-scoped, WebSocket-driven (#1106)
- `stores/executions.js` - Fleet execution list/stats + agent Overview analytics (`fetchAgentAnalytics`, cached per `${name}:${window}`, never polled) (#1107) + per-schedule performance rollups (`fetchSchedulesSummary`, same `${name}:${window}` cache; one fetch shared by the Overview "Schedules performance" section and the Schedules-tab inline stats) (#1115)
- `stores/sessions.js` - Session tab state

**Real-time:** WebSocket client at `utils/websocket.js` with auto-reconnect; tracks `_eid` and replays via `last-event-id` — see [Real-time Delivery](#real-time-delivery-reliability-003-306).

**Top-nav IA — Operations (#1109):** former Health/Ops/Executions nav entries are one **Operations** entry (`views/Operations.vue`, `/operations`) — a `?tab=`-driven view: Needs Response · Notifications · Health · Executions · Resolved. Tab content in embeddable `components/MonitoringPanel.vue` / `ExecutionsPanel.vue`; tabs toggle by `v-if` so store-owned polling tears down on leave. Health tab admin-gated. NavBar carries one unified badge (pending operator-queue + notifications, critical-pulse). Legacy `/monitoring`, `/executions`, `/operating-room`, `/events` redirect (query-preserving) to the matching tab.

**Library system-install section (ent#126):** the install surface is a third stacked `<section id="systems">` on `views/Library.vue`, between Agent Templates and Skills. It originally shipped as a `?tab=`-driven strip on `Templates.vue`; ent#263 renamed that page to **Library** ("installable assets for your fleet") and chose stacked sections with jump anchors over tabs/filter pills, so ent#126 conforms to the page's model rather than reintroducing a competing one — the ordering puts Systems next to Agent Templates because both *install agents* (a template creates one, a manifest creates a wired fleet), leaving Skills last as the one that configures agents that already exist. The section is hidden outright below `creator` (gated on `hasMinRole('creator')`, mirroring `POST /api/systems/deploy`) rather than shown-and-disabled, since a browse surface gains nothing from a dead panel. It hosts `components/systems/` — `SystemInstallPanel` (bundled cards / upload / paste + Preview + Deploy) → `ManifestPreview` (agents, permission topology, schedules, blockers, acknowledgement gate) → `DeployResult` (all five `status` values) — over the new domain-scoped `stores/systems.js` (Invariant #6: a *System* is a manifest-deployed name-prefix group, a *System View* is a saved tag filter — different domains). The store's `normalizeError` collapses six response shapes and switches on `status`, never the HTTP code (`partial`/`invalid` are 200; `failed` is **500 with the full report as the body**), and binds `preview` to `previewedText` so Deploy is disabled after an edit. Plain `<textarea>`: the orphaned monaco `components/YamlEditor.vue` stays unrevived because prod CSP is `script-src 'self'` with no `unsafe-eval`/`worker-src` while the dev CSP allows `unsafe-eval`. `views/Dashboard.vue` reads `?view=`/`?tags=` so a fresh deploy lands filtered to its own fleet.
**Top-nav IA — Library (trinity-enterprise#263):** the former Templates nav entry is one **Library** entry (`views/Library.vue`, `/library`) — a single surface for installable assets: an **Agent Templates** section (the existing Starter/GitHub/Custom card grids) and a **Skills** section (`components/LibrarySkillsSection.vue` — fleet-level browse over the shared skills library, backed by the fleet-scoped `stores/skillsLibrary.js`, deliberately separate from the agent-scoped `stores/skills.js` whose KeepAlive'd Skills-tab consumer never unmounts on nav-away). Stacked sections with in-page jump anchors (no `?kind=` machinery); per-section failure isolation; per-kind empty states. The skills half reads only the existing `GET /api/skills/library` (+`/status`) surfaces and renders the #183 contract via the shared `components/skills/` chips seam consumed by both the Library and the per-agent Skills tab; assignment stays on Agent Detail (ent#182: one skill model). **No new backend endpoints.** Legacy `/templates` redirects (query+hash-preserving). See [library-page.md](feature-flows/library-page.md).

**Library tabs + fleet assignment read (trinity-enterprise#384):** the three stacked sections become an `OverflowTabs` strip — **Agent Templates · Systems · Skills** — deliberately reversing ent#263's stacked-sections choice for *all* sections at once rather than special-casing Skills (a tabs-plus-stacked hybrid would leave the page with two competing models). `?tab=` URL state, with a `route.query.tab` watch so the RENDER follows the URL for any navigation that changes it — an external link, a deep link, a history entry. Reading the query once at setup (`Operations.vue`'s shape) leaves those cases changing the address bar while the panel stays put. Tab *clicks* use `router.replace`, so switching tabs deliberately does not push history entries and Back leaves the page rather than walking back through tabs; the legacy `#agent-templates`/`#systems`/`#skills` anchors migrate to the matching tab and are then cleared, with `?tab=` winning over a hash. The Systems tab stays creator-gated with **both** arms of a late-role watch, since `stores/auth.js` reports `user` until `/api/users/me` lands and a creator hard-loading `?tab=systems` would otherwise land on Templates and never recover. Panels **lazy-mount-once** (`v-if` visited + `v-show` active): a plain `v-if` refetches status+library+assignments on every switch — the #1109 teardown rationale does not apply, `stores/skillsLibrary.js` owns no poll — while a plain `v-show` mounts Skills and Systems for a Templates-only visitor; it also preserves `SystemInstallPanel`'s editor state. Each skill block now lists **which agents already hold it** (chips → `/agents/{name}?tab=skills`, bounded with a counted overflow), plus a bounded **orphaned-assignments** list for rows whose skill left the library — ent#237's revocation model is "cut a new tag without the offending skill", after which the operator's first question is *who still has it*, and a page keyed by the library listing answers "nothing" forever. Assignment itself stays a **write on the per-agent Skills tab** (ent#182: one skill model); the Library gained a read, not a second write path — the assign/unassign writes were split to ent#386 and delivery semantics remain ent#385. **OSS-core by decision (ent#384): deliberately ungated — no `requires_entitlement`, logic stays in the OSS tree.** Recorded explicitly because CLAUDE.md's default for an enterprise-tracker feature is *gated unless ruled otherwise*, so the ruling must never be inferred later from the mere fact that it merged (the ent#326 discipline). Rationale: generic fleet telemetry over OSS tables, on a page and a skills stack that already shipped OSS-core (ent#183/#236/#237/#263/#126). See [library-page.md](feature-flows/library-page.md).

**Tab overflow — `components/OverflowTabs.vue` (#1114):** reusable "priority+" tab strip for Agent Detail: a hidden mirror row measures each tab's width plus a worst-case "More" button; the visible row renders what fits and collapses the rest into a "More ▾" menu. Re-measures on resize + `document.fonts.ready`; all-inline before first measure (no first-paint snap). The trigger reflects an overflowed active tab. Keyboard/touch accessible, dark-mode aware; `v-model` over `activeTab` so `?tab=` deep-linking is unaffected.

**Agent Detail Overview tab (#1107):** `components/OverviewPanel.vue` is the default landing tab — owns "trend over the last few days" while the persistent `AgentHeader` owns "now + cost" (no duplicate live gauges). Sections: About lead, needs-attention count + Operations link (hidden at zero), trend charts, health panel (uptime/latency clamped ≤7d by `agent_health_checks` retention), recent-activity drill-in, footprint chips. Charts: `StackedBarChart.vue` (CSS/flexbox) for executions-by-type; `TrendLineChart.vue` (uPlot) for line series. `InfoPanel.vue` leads with About + "What You Can Ask", `template.yaml` metadata behind a `<details>`.

**First-run front desk (ent#319):** `components/onboarding/FrontDeskPanel.vue` + `stores/firstRun.js` — a dismissible Dashboard card offering the three doors (**Show me** → the seeded demonstrator's chat, **Make me one** → the existing wizard, **Bring mine** → a de-emphasised secondary link). It exists because first-run seeding made the wizard's own freshness test permanently false: `maybeAutoOpenOnboarding` fires only at zero non-system agents, and a fresh install now boots with a seeded fleet, so the wizard stopped auto-opening on the install it was written for. The card requires a non-empty seeded set, so a genuinely empty install still gets the wizard auto-open and the two surfaces never stack. Backed by `GET /api/onboarding/first-run`.
**Ambient onboarding — activation checklist + empty states (ent#238):** `components/onboarding/ActivationChecklist.vue` (+ `stores/onboarding.js`) is a dismissible, non-gating card on the Dashboard listing the four first-value milestones (agent → chat → schedule → channel). It is **derivation-only on the client**: the store holds no progress of its own, so an item unticks when the state behind it goes away, and it renders **nothing** when the server-driven surface it reads is absent (404) or not entitled (403) — the OSS bundle ships the component either way and stays silent, logging only on a genuine failure. It hides itself once every milestone is reached. Paired empty-state work on the first-hour path: the Schedules panel's zero-state carries the same primary action as its header (Dashboard, Chat and Operations already had explainer + action).

**Dashboard Grid view (trinity-enterprise#47):** one of three dashboard modes (Timeline / Grid / List; Timeline stays default, choice in `localStorage['trinity-dashboard-view']`). The legacy **Graph** mode (Vue Flow node canvas) was decommissioned in #1689 — a persisted stale mode (`'graph'`, or `'list'` on an older bundle) degrades to the default via the `VIEW_MODES.includes()` guard. Magnetic tile canvas: `components/FleetGrid.vue` (pan/zoom viewport + drag/swap/tidy/keyboard on an unbounded lattice — no Vue Flow) renders `components/AgentTile.vue` five-zone tiles (composing AgentAvatar/RuntimeBadge/Running+Autonomy toggles); `stores/fleetGrid.js` owns the per-user self-healing layout (localStorage v1) and lazy per-tile analytics hydration (viewport-gated, concurrency-capped, stale-while-revalidate over the `executions` store `${name}:${window}` cache); lattice math in `utils/gridLayout.js`. Chip data from batch endpoints (sync-health #389, operator-queue pending) on a visibility-aware poll active only while mounted. `network.js` additions: 3-state `viewMode`, `circuitBreakers` map, WS-driven `workingState`. A `/` **type-to-filter** (trinity-enterprise#261) layers a non-persisted query predicate (slug + display label via `agentDisplayName()`) into the store `visibleAgents` seam covering all three modes — the timeline consumes it via its `:agents` prop, so the persisted **owner filter now applies to timeline rows too**; node rebuilds stay on the pre-query `ownerFilteredAgents`. **No new backend endpoints.** A second occupant type — **info tiles** on the ent#325 widget chassis (`components/InfoTile.vue` + the `GRID_WIDGETS` registry in `utils/gridWidgets.js`, `widget:*` keys in the same layout map) — shares the lattice with agent tiles; the chassis itself is documented under #2126. The first data tile is **Recent failures** (ent#100): newest failed executions fleet-wide plus the 24h total, from the existing `GET /api/executions?status=failed` + `/api/executions/stats`, ridden on the one 60s batch poll and gated on the tile being enabled — so the no-new-endpoints property still holds. Its green "No failures in 24h ✓" is treated as a **positive claim needing positive evidence** and is unreachable from a failed rows GET, a failed `/stats` GET, or an unenumerable fleet (`accessible_agent_names` → `list_all_agents_fast()` returns `[]` on any Docker fault, which for a non-admin yields HTTP 200 + zeros — the ent#384 hazard, mitigated client-side here by requiring a non-empty roster); the rule is the pure `utils/executionFailure.js::failuresTileState`. Tiles receive the **unfiltered** roster (`orgAgents || agents`) so the type-to-filter cannot degrade a fleet tile's labels, and the shared 1s tick only when the catalog entry declares `wantsTick`. **Subscription pressure** (ent#259) is the first `adminOnly` tile: one row per SUBSCRIPTION (the inverse unit of #471's per-agent chip, which cannot say *which* subscription is the bottleneck since agents share one 5h window), composing the existing `GET /api/subscriptions` + `/{id}/usage` in `stores/subscriptions.js::fetchPressureData` on the same batch poll — **still no new endpoints**, per the operator ruling that it be "a small build" on #471's surfaces; a batched route was designed and rejected (it optimizes ~0.5 q/s and `assert_admin` rejects agent principals, so ent#351 could not reuse it anyway). `adminOnly` is load-bearing rather than cosmetic: every endpoint it reads is admin-gated because the payload carries per-subscription spend, and the flag doubles as the fetch gate (the widget key is absent for non-admins, so no 403 loop) — with a key-appearance watch because `isAdmin` is only confirmed once `fetchUserProfile()` lands. Its display rules live in `utils/subscriptionPressureTile.js`: a real utilization % only while the provider snapshot is fresh (#471 made the number real, so the AC's "never a fake-precise X% left" governs *framing*, not disclosure), "429s" from the `rate_limit` kind alone (never `failure_events_24h`, which also carries `auth` + pre-#471 `unknown` — the conflation migration 0040 exists to fix), and `input_tokens` kept off the row face as **context occupancy, not tokens consumed** — the same ruling this document already records for the sibling ent#101 tile against `/executions/timeline`. **A rejected provider token is its own state (#2352/#2353), never a limit:** the tile shipped claiming "rate-limited" for a dead credential from BOTH directions — the backend predicate counted auth failures (fixed by the split above), and `pressureHeadline` tested `rate_limited_now` before it ever read `headroom.status`, so `invalid_token` — described in the row tooltip as "the most actionable state the payload can carry" — was reachable only by hovering. Precedence is now `unavailable → token invalid → rate-limited → N× 429 → failures → no provider data → nearing limit → ok` (#2396 added the penultimate tier: a forecast, not a failure, so it ranks below everything that has actually gone wrong and above `ok`, which the row is not — reached on the tile only when `row.windows` is null, since the bars are otherwise the reading; the operator-visible half of that fix is the lead chip going from red `limit` to amber `near`), with a rejected token outranking `rate_limited_now` because a probe that could not authenticate learned nothing about the quota, and a probe `error` ranked below the failure states but ABOVE `ok`, which would assert health on no evidence. `headroomStatus` is read regardless of snapshot freshness, the deliberate inverse of `windowReadings`: a *number* decays, "this credential was refused" does not. A new `auth` severity outranks `crit` in the row sort (the row needing a person above the row needing a wait) and carries its own chip word, as does the warn-tier `warnReason: "auth"` for an auth-only 24h history — after the predicate split that history no longer reaches `crit`, so without the word it would have rendered as "429s", trading one wrong label for another. **When the limit comes back is on the row face (#447):** `resetReading` reads `headroom.*.resets_at` **directly**, deliberately bypassing `windowReadings` and therefore its freshness gate — the same asymmetry `headroomStatus` documents, for the same reason one level along: a *number* decays, an *instant* does not. That gate is precisely what hid the reset on the rows that need it, since a 429 probe sets `status: 'rate_limited'`, so `decorate_usage` never promotes `source` to `anthropic`, so `windowReadings` returns null — while `resets_at` sits populated in the same object. The binding window is chosen by `representative_claim` first (live-verified populated; both windows can report the SAME utilization, so a "fullest wins" tiebreak has no answer there and would pick by array order), then any window the shared vocabulary calls BLOCKING (#2396 — `allowed_warning` is near the limit, not blocked by it, so it reaches binding status on the merits below rather than by being mistaken for a block), then the fullest. A lapsed instant renders as `reset due`, never a stale future time; a limited row with no reset says `reset unknown` rather than blank; a rejected token gets none at all (a dead credential has no quota clock). Placement is decided in the pure module so the SFC needs no change: the reset takes whichever text slot is free — joining the headline on the primary line when there are no bars, leading the second line when there are, because the fixed-width bars cannot take more without overflowing a row that clips silently. See [dashboard-grid-view.md](feature-flows/dashboard-grid-view.md).

**Dashboard List view (trinity-enterprise#260):** the third dashboard mode — the retired standalone Agents page (`views/Agents.vue`, deleted) consolidated into the chassis. `components/AgentListPanel.vue` is the page's row list extracted as a **props-driven panel**: `:agents` comes from the store-level `visibleAgents` computed in `stores/network.js` (server-side tag filter ∘ client-side owner filter ∘ the ent#261 type-to-filter query — the named **ent#261 seam**, now feeding all three panes including ReplayTimeline's `:agents` prop), `:available-tags` from the chassis `/api/tags` fetch. Rows read `tags`/`read_only_enabled`/`display_label` off the fleet payload — **both Agents-page N+1 mount loops were deleted, not migrated** (zero per-row HTTP; also more correct: the old per-agent read-only GET 404'd on stopped containers and was coerced to `false`). Run/Autonomy toggles rewired to networkStore actions (`isTogglingRunning` map; result-object `{success,error}` toasts); system rows hide the Run toggle (grid-tile guard adopted — stopping `trinity-system` stays on its detail page). agentsStore is composed in for exactly `sortBy` + `syncHealth`/`fetchSyncHealth` (60s visibility-aware refresh while mounted); the sort comparator is the pure `utils/agentSort.js::sortAgents` (system rows re-pinned first, #1642 display-name sort, zero-task tiebreak); `networkStore.fetchAgents` write-throughs the fleet to `agentsStore.agents` (#1643 tab titles; gated — a quick-tag-filtered fetch narrows the response server-side and never clobbers the full-fleet store; shallow copy, so rows are shared but the arrays aren't). Name/status filters persist under NEW `trinity-dashboard-list-filter-*` keys; tag/owner filtering migrated to the chassis quick-tag/owner controls (Clear-all clears both layers via a `clear-chassis-filters` emit). Chassis-level **Create Agent** button + modal (all modes); `/agents` is a query-preserving redirect to `/?view=list` — a one-shot, **non-persisting** intent the Dashboard applies via a route watch then strips (`setViewMode(mode, {persist:false})` skips only the localStorage write); NavBar's Agents entry removed (the Dashboard link inherits the agent-detail highlight). **No new backend endpoints, zero backend changes.** See [dashboard-list-view.md](feature-flows/dashboard-list-view.md).

**Grid org overlay (trinity-enterprise#305, OSS-core):** departments and reporting lines over the same lattice, stored as **namespaced tags** (`dept-<name>` on the agent; `reports-to-<agent>` on the REPORT row — no schema change). Zones are *derived* hull frames (`utils/gridOrg.js` pure module + `composables/useOrgOverlay.js` state machine); lattice gaps are sized to absorb zone chrome (unit-pinned contract), so the free-drag model is untouched. Org namespaces are **human-only at both writers** — `routers/tags.py` rejects agent-principal writes to the reserved prefixes (#1578 pattern) and the system-manifest validator rejects org-prefixed manifest tags (`deploy_system` is creator-gated, which an agent key satisfies via its owner's role); `rename_agent` rewrites `reports-to-*` VALUES in-transaction (PK-collision-safe) and the hard-purge cascade deletes dangling refs. The `agent_tags_changed` broadcast is a **thin trigger** (`{type, agent_name}` — `/ws` is SCOPE_ALL/unfiltered, the #918 rule; listeners refetch via the access-controlled per-agent route; pinned by `test_305_tags_broadcast.py`), and `GET /api/tags` filters the org prefixes for non-admins (fleet-wide GROUP BY — org tags would expose every department + manager name to any authenticated user). Generic tag surfaces hide the namespaces (`isOrgTag`); the AgentDetail tag editor shows all. See [dashboard-grid-view.md](feature-flows/dashboard-grid-view.md) § Org overlay.

**Agent-to-agent collaboration data** (`stores/network.js`): the Vue Flow node-graph rendering of collaboration (the Dashboard's Graph mode + `AgentNode.vue`) was **decommissioned in #1689**; the underlying collaboration data still flows and feeds the Timeline replay. Detection: the backend chat endpoint accepts `X-Source-Agent` and broadcasts `agent_collaboration` WS events; `activity_service` broadcasts `agent_activity` (`activity_type`: chat_start/chat_end/tool_call/schedule_start/schedule_end/agent_collaboration; `activity_state`: started/completed/failed/cancelled — a user-cancelled terminal is recorded as `cancelled`, distinct from `failed`, #1332).

### MCP Server (`src/mcp-server/`)

FastMCP, Streamable HTTP transport, port 8080. API-key auth via `Authorization: Bearer` header; FastMCP `authenticate` callback validates keys against the backend and stores an `McpAuthContext` in session: `{userId, userEmail, keyName, agentName?, scope: "user"|"agent", mcpApiKey}`. Agent-to-agent collaboration uses agent-scoped keys for access control.

**Tools** across 22 tool modules (`src/tools/`):

| Module | Tools | Description |
|--------|-------|-------------|
| `agents.ts` (22) | `list_agents`, `get_agent`, `get_agent_info`, `get_agent_compatibility_report`, `create_agent`, `rename_agent`, `delete_agent`, `start_agent`, `stop_agent`, `list_templates`, `get_credential_status`, `inject_credentials`, `export_credentials`, `import_credentials`, `get_credential_encryption_key`, `get_agent_ssh_access`, `deploy_local_agent`, `initialize_github_sync`, `get_agent_github_pat_status`, `set_agent_github_pat`, `export_agent_data`, `import_agent_data` | Agent lifecycle, credentials, SSH, local deploy, GitHub sync, per-agent PAT (#347), runtime-data export/import (#1169), compatibility report (#668) |
| `chat.ts` (3) | `chat_with_agent`, `get_chat_history`, `get_agent_logs` | Chat (enforces sharing rules), history, logs. Sync mode applies `MCP_CHAT_TIMEOUT_MS` (default 25000); on abort the client queries `/api/agents/{name}/executions`, matches the in-flight MCP row, and returns `{status:"queued_timeout", execution_id, message}` so callers poll instead of duplicate-queueing (#914) |
| `schedules.ts` (8) | `list_agent_schedules`, `create_agent_schedule`, `get_agent_schedule`, `update_agent_schedule`, `delete_agent_schedule`, `toggle_agent_schedule`, `trigger_agent_schedule`, `get_schedule_executions` | Schedule CRUD and execution history |
| `executions.ts` (3) | `list_recent_executions`, `get_execution_result`, `get_agent_activity_summary` | Execution queries, async result polling, activity monitoring (MCP-007) |
| `skills.ts` (7) | `list_skills`, `get_skill`, `get_skills_library_status`, `assign_skill_to_agent`, `set_agent_skills`, `sync_agent_skills`, `get_agent_skills` | Skill management and assignment |
| `tags.ts` (5) | `list_tags`, `get_agent_tags`, `tag_agent`, `untag_agent`, `set_agent_tags` | Agent tagging |
| `systems.ts` (4) | `deploy_system`, `list_systems`, `restart_system`, `get_system_manifest` | System manifest deployment |
| `subscriptions.ts` (6) | `register_subscription`, `list_subscriptions`, `assign_subscription`, `clear_agent_subscription`, `get_agent_auth`, `delete_subscription` | Subscription management |
| `monitoring.ts` (3) | `get_fleet_health`, `get_agent_health`, `trigger_health_check` | Fleet health monitoring |
| `nevermined.ts` (4) | `configure_nevermined`, `get_nevermined_config`, `toggle_nevermined`, `get_nevermined_payments` | x402 payment configuration |
| `notifications.ts` (1) | `send_notification` | Agent-to-platform notifications |
| `events.ts` (4) | `emit_event`, `subscribe_to_event`, `list_event_subscriptions`, `delete_event_subscription` | Agent event pub/sub (EVT-001). The `agent.task.*` namespace is **reserved** for backend-emitted task-completion events (#1578) — `emit_event` rejects it; `subscribe_to_event` to a source agent's `agent.task.completed`/`failed` yields an automatic report-back task (no self-subscription) — see [Task Completion Events](#task-completion-events-1578) |
| `docs.ts` (2) | `get_agent_requirements`, `ask_trinity` | Agent documentation; `ask_trinity` proxies the public DOCS-QA-001 Q&A endpoint (Vertex AI Search + Gemini) so any MCP consumer — agents on agent-scoped keys, external clients — gets grounded answers about Trinity itself. Endpoint overridable via `ASK_TRINITY_ENDPOINT`; `session_id` is an opaque STRING (values exceed `Number.MAX_SAFE_INTEGER`) and expiry is silent, so a changed id is surfaced as a context-lost warning. The adapter is vendored from `src/helper-mcp` (`@abilityai/trinity-docs-mcp`, #1579) with a behavioural parity test — separate npm packages, no workspace (ent#328) |
| `channels.ts` (2) | `list_channel_groups`, `send_group_message` | Channel group discovery and proactive group messaging — Telegram (#349) and Slack channels (#350); `channel_type: telegram\|slack`, Slack send accepts optional `thread_ts` |
| `messages.ts` (1) | `send_message` | Proactive user messaging by verified email (#321) |
| `voice.ts` (1) | `send_voice_reply` | Speak one reply of the current channel turn as a voice note (ent#117); backend resolves the channel destination from `execution_id`, gates on the agent + per-channel voice flags, effect-guarded (#1084). Fail-soft → agent falls back to text. A portal turn returns `portal_client_narrated` + a `guidance` sentence the agent acts on (#2157) — the Workspace narrates text client-side, so a tool refusal there is NOT evidence the surface is mute |
| `files.ts` (1) | `share_file` | Publish file from `/home/developer/public/`, return download URL (FILES-001) |
| `pipelines.ts` (2) | `list_agent_pipelines`, `get_agent_pipeline_state` | Read-only introspection of an agent's self-published pipelines (`~/.trinity/pipelines/*.yaml` + `~/.trinity/pipeline-state/<id>/<instance>.json`) over the **existing** `agent_files` surface — no backend/DB change (Invariant #8). Strict `^[A-Za-z0-9._-]+$` id validation (path-traversal guard), hardened YAML parse (size cap + dup-key + alias guard), latest-instance by mtime, only-404→empty (#919) |
| `loops.ts` (3) | `run_agent_loop`, `get_loop_status`, `stop_loop` | Sequential bounded task execution (#740) |
| `reminders.ts` (3) | `set_reminder`, `list_reminders`, `cancel_reminder` | Durable one-shot deferred self-trigger; self-scoped; fires a normal execution of the same agent (`triggered_by="reminder"`) via the scheduler (#1296) |
| `memory.ts` (1) | `write_user_memory` | Per-user memory blob; user email resolved server-side from execution_id (MEM-001, #888) |
| `reports.ts` (3) | `report`, `list_reports`, `get_report` | Publish a structured report (self-only, backend self-gates the path agent, #918); read back what was reported (#1538) — `list_reports` returns metadata, `get_report` the payload, mirroring the REST split. Read is gated at the MCP layer to `{self} ∪ permitted` (an agent key resolves to its OWNER, so the backend's scoping is wider than the calling agent); a non-permitted `get_report` returns the backend's own not-found shape rather than a distinguishable 403 |
| `voip.ts` (1) | `call_user` | Outbound phone call via Twilio Media Streams; server-gated + rate-limited (VOIP-001, #1056) |
| `operator_queue.ts` (3) | `list_operator_queue`, `get_operator_queue_item`, `respond_to_operator_queue` | Read the Operating Room queue (broad or `agent_name`-scoped) and **resolve** a pending item — answer / approve / deny via `POST /{id}/respond`. The respond tool resolves the item's `agent_name`, then applies the same MCP-layer gate before writing (non-`pending` → structured error). Agent-scoped keys gated to `{self} ∪ permitted`. `cancel` deferred. (OPS-001, #1101 read / #1104 respond) |
| `git.ts` (6) | `get_git_status`, `git_sync`, `get_git_log`, `git_pull`, `get_git_sync_state`, `reset_to_main_preserve_state` | Direct, deterministic (non-LLM) git operations — bypass `chat_with_agent` for status/sync/log/pull/sync-state and the destructive `reset_to_main_preserve_state` recovery. Conflicts stay LLM-mediated: a 409 surfaces `X-Conflict-Type`/`X-Conflict-Class` verbatim + a `chat_with_agent` hint (except `no_write_credentials` — a credentials gap chat can't fix; the hint says fork-to-own/add-a-token instead, ent#123). Mutating ops (`git_sync`/`reset`) are `OwnedAgentByName` (owner-only; a shared key gets read+pull only); agent-scoped keys gated to `{self} ∪ permitted` at the MCP layer. Each call mints a `requestId` it stamps on its `mcp_operation` audit row AND forwards as `X-Request-ID`, so the paired backend `git_operation` row joins via `GET /api/audit-log?request_id=` (#905) |
| `a2a_call.ts` (2) | `call_a2a_agent`, `get_a2a_task` | **Outbound** A2A runtime (#736) — task an external A2A agent through an operator-registered endpoint chosen **by name**; a URL parameter is deliberately absent (an agent's tool args are LLM-generated and prompt-injectable, so it would be a server-side-request primitive). `dedup_label` is REQUIRED — the effect guard keys on the endpoint + conversation, never the message, so a reused label replays the earlier answer. Separate module from `a2a.ts`, which scopes itself to the entitlement-gated management plane; this is OSS-core. Agent-scoped gate is **self-only**, matching the backend — `{self} ∪ permitted` here would deny a strict subset of what the backend denies, i.e. block nothing at the cost of a round-trip. Own `AbortController` (40s) so the MCP server gives up before its gateway does and can report `possibly_delivered` |
| `auth.ts` (2) | `request_login`, `verify_login` | #848 inline email auth — sign in from an MCP client with **no** pre-minted API key. Registered ONLY when `MCP_INLINE_AUTH_ENABLED` is on, and advertised ONLY to the `anonymous` session tier (`anonymousOnly`). `request_login(email)` mails the standard 6-digit code and returns one **constant** receipt on every path (enumeration-safe, no audit row); `verify_login(code)` upgrades the session **in place** — the object FastMCP hands every tool — recording the verified email + the agents it may reach. `scope` deliberately stays `"anonymous"` after login (the session still holds no credential and must never satisfy `operatorOnly`; pinned by test). The advertised tool list is **identical before and after login** — login flips *behaviour*, not *visibility*, because `toolsListChanged` re-filters live sessions and the #846 reconciler fires it every ~20s, which would make a login-keyed gate flip non-deterministically. Sessions are per-connection, so a client restart requires signing in again. **The in-place upgrade only survives because the context is memoized (#2035):** streamable HTTP is discrete POSTs, `mcp-proxy` re-runs `authenticate` on each and `FastMCPSession#updateAuth` REPLACES rather than merges, so a fresh context per request discarded every login — `verify_login` succeeded and the next call answered `login_required`. `createAnonymousSessionStore` returns the same object per `Mcp-Session-Id`; anonymous tier only (a keyed session re-validates its key every request, pinned by a source guard), bounded, 30 min idle / 4 h absolute. See [mcp-connector.md](feature-flows/mcp-connector.md) |

### Vector Log Aggregator (`config/vector.yaml`)

Vector 0.43.1 (`timberio/vector:0.43.1-alpine`). Captures all container stdout/stderr via Docker socket; routes platform logs to `/data/logs/platform.json` and agent logs to `/data/logs/agents.json`; enriches with container metadata; parses JSON logs. Health: `http://localhost:8686/health`. Query: `docker exec trinity-vector sh -c "tail -50 /data/logs/platform.json" | jq .` (same for `agents.json`).

**Docker Desktop local override (#1432):** the `docker_logs` source busy-loops on Docker Desktop / VM-based runtimes (the virtualized log relay closes each `follow` stream after backlog flush; `docker_logs` reconnects with no backoff → a storm that pegs the Docker VM). Native Linux dockerd is unaffected, so **prod is unchanged**. For local dev, `config/vector.local.yaml` swaps to a `file` source tailing `/var/lib/docker/containers/*/*-json.log` (immune to the follow-close bug), applied via a gitignored `docker-compose.override.yml` (template `docker-compose.override.example.yml`) that `start.sh` auto-creates when it detects Docker Desktop (`TRINITY_LOCAL_LOG_SOURCE=docker|file` overrides). The file source keys by container ID → a single `/data/logs/local-*.json` instead of the name-split files. See [docs/QUERYING_LOGS.md](../QUERYING_LOGS.md).

### Agent Containers

**Base image** `trinity-agent-base:latest`: Python 3.13, Node.js 20, Go 1.21, Claude Code (latest), common Python packages.

**Internal server** `agent-server.py` (FastAPI, port 8000):
- `/api/chat` - Claude Code execution (messages persisted to database)
- `/health` - Health check. Returns `{status}` plus `active_tasks` (concurrent executions across `/api/chat` + `/api/task`), `last_task_at`, `consecutive_failures` (reset on success — consumed by the dispatch breaker #526 and fleet health #307), the #333 `diagnostics` gauges (#1020), and `clone_status` (`ok`|`failed`, #1439) — a coarse, server-computed identity-clone signal read defensively from the untrusted `.git-clone-status` marker (enum only, never the agent-supplied repo/branch/error strings, since `/health` is unauthenticated) that lets `monitoring_service` mark a silently-failed GitHub-template clone **unhealthy** instead of reporting a running-but-empty agent healthy. `mailbox_depth` intentionally NOT emitted — no agent-side mailbox until the actor model (#945); the backend derives queue depth from `CapacityManager`. Counters live in `agent_server/state.py`; backend reads them in `monitoring_service.py` with graceful defaults for older images.
- `/api/credentials/reload-token` - Surgical subscription-token hot-reload (#1089): mutates the agent-server process `os.environ["CLAUDE_CODE_OAUTH_TOKEN"]` so the NEXT claude subprocess uses the rotated token while in-flight subprocesses keep theirs; persists to the writable-layer override `/var/lib/trinity/oauth-token` (0600). Does NOT touch `.env`/`.mcp.json`. See [Subscription Token Rotation](#subscription-token-rotation-via-hot-reload-1089)
- `/api/chat/session` - Context window stats
- `/api/files`, `/api/files/download` (100MB limit), `/api/files/mkdir` (workspace-confined, #37)
- `/api/brain-orb/data` - Streams the agent's `resources/agent-visualization/data.json` (Brain Orb read surface, #58); 404 when absent. `/api/brain-orb/scopes` + `/api/brain-orb/scope` run the agent's `~/.trinity/brain-orb/{scopes,scope}` convention hooks for live scope control (#58 Phase 2); `/api/brain-orb/tool` runs the read-only `~/.trinity/brain-orb/search` hook (#60 Phase 3)

The agent server also runs two loops: the 15-min git `auto_sync` heartbeat (see [Git Sync Health](#git-sync-health-389390)) and the 5s liveness heartbeat (see [Heartbeat Liveness](#heartbeat-liveness-reliability-004-307)).

**Execution environment assembly (#1999):** a spawned runtime subprocess no longer inherits the agent-server's long-lived `os.environ`. `agent_server/services/execution_env.py::build_execution_env` rebuilds it per spawn from three inspectable inputs — `INITIAL_ENV` (the container baseline captured at import, i.e. what `docker exec` shows) → `~/.env` parsed fresh (**authoritative for credentials**, so a removed key is removed) → `RUNTIME_OVERRIDES` (values that are deliberately not `.env` credentials; the #1089 token rotation and the #2114 subscription-shadow arm, applied AFTER the file so a stale token in `.env` can't beat an explicit rotation, with `None` meaning force-unset) → the caller's `extra` (last, so `EXECUTION_TAG_NAME` #407 can't be displaced). All five spawn sites (`claude_code`, `headless_executor`, `codex_runtime`, `gemini_runtime` ×2) route through it. The credential endpoints still mirror `.env` into `os.environ` for **in-process** readers (error classifier, `AGENT_RUNTIME`, the sanitizer's redaction set) but now with a **delete phase** that restores the container baseline rather than popping blind, and only for keys the mirror itself wrote. `.env` may not set loader/exec-redirecting names (`PATH`, `LD_PRELOAD`, `BASH_ENV`, …) — it is agent-writable and is now read at every spawn. `GET /api/credentials/status` reports per-key `env_drift` (**names only, never values**). Before this, the file and the execution environment were independent channels that synced one-way with no delete phase, so a key removed from `.env` outside the credentials API kept reaching every execution until container restart — invisible to both `/proc/<pid>/environ` and `docker exec`, i.e. silent failure of credential revocation. **`.env` is deliberately NOT authoritative for API-key-style Claude auth while subscription auth is active (#2114):** #1999's re-read made a stale `.env` `ANTHROPIC_API_KEY` (preferred by Claude Code over the OAuth token, and never cleaned by any recreate — it lives on the workspace volume) shadow subscription auth at every spawn, which SUB-003 then mis-attributed to each healthy subscription in turn. At boot, `arm_subscription_auth_guard()` force-unsets `ANTHROPIC_API_KEY` + `ANTHROPIC_AUTH_TOKEN` through the same override layer when the baseline carries a truthy `CLAUDE_CODE_OAUTH_TOKEN` on a Claude runtime — restart-durable because `startup.sh` exports the rotated override token *before* the server launches, so it is always baseline; non-Claude runtimes never arm (a vestigial token must not strip a key their scripts may use). The suppression is **data, not just a memoized per-key WARNING**: `env_drift_report` marks force-unset keys `suppressed_for_spawn` (iteration set includes override-only keys), so the one surface built to expose file/spawn divergence cannot show all-green over an active suppression.

**Durable subscription-token override (#1089):** `startup.sh` exports `CLAUDE_CODE_OAUTH_TOKEN` from `/var/lib/trinity/oauth-token` (when present, non-empty) **before** launching the agent server, so a token rotated via hot-reload survives any plain stop+start of the same container (historically that included `routers/ops.py`'s fleet restart, which bypassed `start_agent_internal` entirely; since #1860 a fleet restart routes through `lifecycle.restart_agent_internal` — a no-drift agent keeps its container and the override survives, a drifted agent is recreated and cleanly re-bakes `Config.Env` from the DB). The path is deliberately on the writable layer, **not** under the persisted `/home/developer` volume: it survives `stop`→`start` (same container) but is wiped on recreate (fresh layer), so a DB-driven recreate cleanly re-bakes `Config.Env` from the DB and the stale override is gone — self-reconciling, no marker logic. Dir created+chowned to UID 1000 in the base-image Dockerfile.

**`.mcp.json.template` rendering (#2007):** `startup.sh` runs `agent_server/mcp_template.py` after the credential-import steps — the missing implementation of the contract `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` publishes. It renders `~/.mcp.json.template` into `~/.mcp.json`, substituting `${VAR}` / `${VAR:-default}` from `.env` **inside `env` blocks only** (the only form `mcp_validator` accepts — a `${VAR}` in `args` is rejected as a shell metacharacter and `command` must be a literal allowlist entry, so substituting there is the #590 RCE-by-config class). It lives in-container because that is where the files are: a `github:` agent's template is cloned by `startup.sh`, so the backend renderer (`template_service.generate_credential_files`, `local:`-only and reads `.mcp.json` not the `.template`) never saw it and every declared server was silently absent. Each candidate server is validated individually through the **vendored** `mcp_validator` (byte-identical to the backend copy, Invariant #5); a server whose placeholders don't resolve, or that the validator rejects, is **withheld with a named reason on stdout** rather than blanked (the #1929 contract), and the rest still install. Merge-only-missing, so the `trinity` entry and any owner edit survive; idempotent across restarts and order-independent with respect to `inject_trinity_mcp_if_configured()`. Never fails the boot.

**Template-supplied pre-check** (SCHED-COND-001, #454): if the template ships an executable `~/.trinity/pre-check`, the backend's internal endpoint `POST /api/internal/agents/{name}/pre-check` runs it via `docker exec` before a cron-triggered chat. Language-agnostic — interpreter selected by shebang. The hook's stdout becomes the chat message; empty stdout + exit 0 records a skipped execution (Claude never invoked). Uses the same `execute_command_in_container` primitive as `git_service.py`, `ssh_service.py`, and the agent terminal — no agent-server HTTP endpoint.

**Persistent chat:** all chat messages auto-saved to SQLite (`chat_sessions`, `chat_messages`) with full observability (costs, context, tool calls, execution time); sessions survive container restarts/deletions; users see only their own messages (admins see all).

**File structure:**
```
/home/developer/           # Agent home directory (WORKDIR, all files live here)
├── CLAUDE.md              # Agent instructions (from template)
├── template.yaml          # Agent metadata (+ declarative blocks: persistent_state #383,
│                          #  data_paths #1169, schedules ent#89, credentials ent#128,
│                          #  plugins #1704)
├── .env                   # Credentials (KEY=VALUE)
├── .mcp.json              # Generated MCP config
├── .mcp.json.template     # Template with ${VAR} placeholders
├── .claude/               # Claude Code config
├── .trinity/              # Trinity-specific files
│   ├── persistent-state.yaml  # S4 allowlist (#383): paths surviving reset
│   └── plugins.yaml       # #1704: declared Claude Code plugins (COMMITTED —
│                          #  in _TRINITY_AUTHORED_PATHS; re-installed at boot)
├── content/               # Generated assets (gitignored)
└── [template files...]    # Any other files from template
```

### Background Services

Services that run continuously in the backend process:

| Service | Module | Description |
|---------|--------|-------------|
| **Cleanup Service** | `cleanup_service.py` | Every 5 min: active watchdog reconciliation against agent process registries (orphan recovery, auto-terminate timeouts) + passive stale recovery (CLEANUP-001, #129). Also runs retention + soft-delete purge sweeps, the **expired-SSH sweep** (`_sweep_expired_ssh_credentials` → `SshService.cleanup_expired_credentials` — removes an expired ephemeral key's line from the container `authorized_keys` sshd reads; TTL was previously enforced only on Redis metadata, #1616), and the #740 startup orphan-loop hook, and the **agent_reminders retention sweep** (`_sweep_agent_reminders_retention` — DELETEs terminal `fired`/`cancelled`/`failed` reminders past `agent_reminders_retention_days`, #1296) — see [Soft Delete & Retention](#soft-delete-retention--recovery-834-772). Runs the additive **lease-reaper** (`lease_reaper_service`) each cycle — re-queues (preserving `execution_id`) or poison-parks expired pull leases (#1081 Phase 3, #429/#1402; inert until an agent is piloted). #1804: every recovery path also closes its execution's dispatch activity (`_close_bulk_swept_activities` for the bulk sweeps, the shared helper elsewhere), counted in `activities_closed_on_recovery`; the 120-minute activity backstop now runs **last** in the cycle |
| **Operator Queue Sync** | `operator_queue_service.py` | Polls running agents every 5s, reads `~/.trinity/operator-queue.json`, syncs to DB, writes responses back (OPS-001). The item `id` is a platform-minted uuid; the agent's correlation string is `request_id` with `(agent_name, request_id)` uniqueness, so all sync reads/writes (exists, acknowledge, response write-back) are agent-scoped and two agents can't collide (#1631). **Leader-locked (#1632):** only the holder of `opqueue:leader` (SET NX, TTL `max(3×interval, 30s)` floor so a slow-write cycle can't flap leadership, own-lease refresh, fail-open — mirror monitoring #1464) runs a cycle, so `--workers 2` doesn't double-charge the ingestion rate limiter or double-broadcast the flood alert. Ingestion is capped per agent (depth + rate + fleet + field hygiene, #1632) — see [Operator Queue](#operator-queue-ops-001) |
| **Sync Health Service** | `sync_health_service.py` | Polls git-enabled agents every 60s — see [Git Sync Health](#git-sync-health-389390) |
| **Skills Library Sync** | `skills_sync_service.py` | Scheduled skills-library `git pull` + optional fleet-wide skill re-inject (ent#236). Runs in every worker but only the `skills:sync:leader` lease-holder performs a cycle (fail-open, mirrors #1464); self-gates on the default-OFF `skills_library_auto_sync_enabled` setting, re-read each cycle so an interval change needs no restart. A sweep fires only on a changed library commit, targets running non-ghost agents at `SKILLS_FLEET_INJECT_CONCURRENCY` (5), and persists an honest per-agent report + raises an operator alarm on any failure |
| **Subscription Recovery Probe** | `subscription_recovery_service.py` | #447: every `SUBSCRIPTION_RECOVERY_PROBE_SECONDS` (300s) re-probes only the subscriptions currently PRESENTED as rate-limited, so a recovered one stops claiming it is limited without anyone watching. It is the **only** producer of the fresh "allowed" verdict `resolve_rate_limited_now` needs — the ambient refresh is demand-driven, so an unwatched instance never re-asked at all, and no success path clears a failure row. Reuses the `max_tokens=1` Haiku probe (~a dozen tokens) and the Settings-surfaced `subscription_headroom_auto_refresh` toggle rather than adding a second knob, since that setting already answers *may Trinity probe on its own?*. Leader-locked (`subscription:recovery:leader`, fail-open — a duplicated probe is a dozen wasted tokens, failing closed would silently stop recovery detection); the probe itself stays fail-CLOSED on Redis (no readable cache ⇒ the result could not be stored for anyone to see). Cannot feed itself: `_probe` records a 429 into the snapshot only, never `subscription_rate_limit_events` |
| **Monitoring Service** | `monitoring_service.py` | Fleet-wide health checks on configurable interval (30s default); authoritative for aggregate status. **Lifespan-resumed (#1121):** boot reads the persisted `monitoring_config` (staggered +12s) and starts the loop only when `enabled` — the flag is the single source of truth, **defaults OFF**, persisted by `enable`/`disable`/`PUT /config` (which also reconcile the running loop) so the choice survives restarts; `*_check_interval` rejects non-positive values (422), loop clamps sleep ≥1s (MON-001). **Cross-worker leader lock (#1464):** the loop runs in every uvicorn worker but only the holder of the Redis `monitoring:leader` lease (SET NX, TTL 3×interval, own-lease-only refresh; fail-open to leader when Redis is down) performs each probe cycle, so `--workers 2` no longer double-probes the fleet or double-feeds the circuit breaker; leadership fails over automatically when the holder dies |
| **Heartbeat Watch Loop** | `heartbeat_service.py` | 5s loop acting on missed agent heartbeats — see [Heartbeat Liveness](#heartbeat-liveness-reliability-004-307) |
| **Scheduler Service** | `scheduler_service.py` | APScheduler cron execution; async fire-and-forget with DB polling for status. On each cron fire, optionally invokes the agent's `~/.trinity/pre-check` (see Agent Containers). Also owns one-shot `DateTrigger`s for RETRY-001 retries and **agent self-reminders** (#1296): `_reconcile_reminders` arms pending reminders + reclaims stale `firing` rows at boot, in the 60s sync loop (own try/except), and on full reload — see [Agent Self-Reminders](#agent-self-reminders-1296) |
| **Capacity Maintenance** | `capacity_manager.py` | `run_maintenance()` every 60s — see [Capacity & Backlog](#capacity--backlog-428) |
| **Audit Retention** | `audit_retention_service.py` | Daily 04:15 UTC: DELETEs `audit_log` rows past retention. `AUDIT_LOG_RETENTION_DAYS` (default 365, floored at 365 — the `audit_log_no_delete` trigger refuses younger rows). Pruning ages out hash-chain history past the cutoff by design (#552) |
| **DB Vacuum** | `db_vacuum_service.py` | Daily 04:30 UTC: `VACUUM` on `/data/trinity.db` to reclaim pages freed by retention sweeps. `DB_VACUUM_ENABLED`/`DB_VACUUM_HOUR`/`DB_VACUUM_MINUTE`. Autocommit connection (VACUUM can't run in a transaction); accepts rare BUSY rather than retrying (#772) |
| **DB Backup** | `db_backup_service.py` | Daily **03:30 UTC** (before the destructive 04:15/04:30 jobs — capture-more-data ordering): a verified recovery point under `/data/backups/` for **both** backends — SQLite via to_thread'd stdlib `Connection.backup()`, PostgreSQL via `pg_dump -Fc` (`postgresql-client-17` baked into the backend image). Day-keyed artifacts + a fail-open SETNX lease (`db_backup:running`, duplicate-I/O suppression only). Prune (window + fixed `MIN_KEEP=3` floor) + staleness check run in the tail of EVERY attempt. `DB_BACKUP_ENABLED`/`DB_BACKUP_HOUR`/`DB_BACKUP_MINUTE`/`DB_BACKUP_PG_DUMP_TIMEOUT_SECONDS` (forwarded in all three compose files — dev, prod and the #2280 hosted file, whose prod parity is CI-guarded by `tests/unit/test_2280_hosted_compose_parity.py`). Default ON — see [Automatic Database Backups](#automatic-database-backups-2216) (#2216) |
| **Session Cleanup** | `session_cleanup_service.py` | Periodic JSONL reaper — see [Resumable Turns](#resumable-turns) |
| **Canary Watcher** | `canary_service.py` | 5-min invariant harness cycle. **Cross-worker leader lease (#1881):** the loop runs in every uvicorn worker but only the holder of the Redis `canary:leader` lease (SET NX; atomic Lua compare-and-delete/expire so no path can touch a sibling's lease; fail-open to leader when Redis is down; released on `stop()`, which is async so the release cannot overlap a still-unwinding cycle) runs a cycle — mirror `monitoring:leader` #1464 / `opqueue:leader` #1632 — so `--workers 2` no longer runs R-01's per-agent `docker exec` sweep twice per 5 min, double-persists `canary_violations`, or gives every cross-cycle marker two writers. **Unlike both precedents the lease is re-armed by a 60s heartbeat, not by the cycle**, because a canary cycle has no upper bound (R-01's sweep carries no timeout) — that split lets TTL = 180s answer only "how long before a dead leader is noticed" (**worst-case failover ≈780s** = interval + TTL + interval, `_max_failover_seconds`; it was ~1200s when one TTL had to cover a whole cycle) while `_MAX_CYCLE_LEASE_SECONDS` = 900s answers "how long may one cycle run" — past which the heartbeat stops refreshing and logs ERROR, so a **wedged** leader yields rather than holding a lease nobody is cycling behind. `run_cycle()` (the on-demand `POST /api/canary/run-cycle`) is deliberately **not** gated, and does drive the alert sink, so a manual cycle on a non-leader cannot swallow a green→red. See [Canary Harness](#canary-invariant-harness-canary-001-411) |

---

## Cross-Cutting Subsystems

Canonical home for each multi-component feature. Endpoint signatures live in [API Endpoints](#api-endpoints); table DDL in [Database Schema](#database-schema).

### Agent Runtimes — multi-runtime / "harness == runtime" (#1187)

A Trinity **harness IS an `AgentRuntime`** — the pluggable execution engine inside the agent container. Three ship today: **Claude Code** (default), **Gemini CLI**, and **OpenAI Codex** (#1187). `AGENT_RUNTIME` (container env, set from `template.yaml runtime:` via `crud.py`; also a `trinity.agent-runtime` label) selects one; `runtime_adapter.get_runtime()` is the factory — it **validates** the value against `KNOWN_RUNTIMES` and raises on an unknown one rather than silently defaulting to Claude.

**ABC** (`agent_server/services/runtime_adapter.py`): `execute` (chat), `execute_headless` (stateless task), `configure_mcp`, `is_available`, `get_default_model`, `get_context_window`, plus a non-abstract `capabilities()` returning a `RuntimeCapabilities` dataclass (`chat_continuity`, `session_tab_resume`, `mcp_support`, `cost_reporting: "native"|"estimated"`) — conservative by default (an un-overridden runtime is least-capable). Each runtime is a singleton (`get_<name>_runtime()`).

**Context-window catalog (#1521):** the `% context used` denominator is model-specific, not a flat 200K. The runtime-reported `modelUsage.contextWindow` is the **primary** value (the only source that knows the effective plan/auth/beta window) — but `modelUsage` carries **one entry per model the turn touched** (Claude Code bills side work like tool-permission checks to a cheap Haiku), so `model_context.py::pick_context_window` matches the entry to the model that *answered* (`metadata.model_name`, from the latest assistant message) instead of taking an arbitrary one; an unidentifiable model keeps the seeded fallback rather than guessing, since picking the largest window is the direction that hides a compaction wall (#1840 — taking the first entry made the same agent report 1M and 200K on alternating runs). When the runtime value is absent, `services/model_context.py::resolve_context_window` is the **fallback** — `[1m]`→1M, Gemini→1M, Codex `gpt-5.6`→1.05M (a verified window, not a guess) / older Codex families→272K, bare Claude→200K *safe floor* (a 1M-capable model shows real 1M via the primary value; the floor is deliberate so an unknown tier never hides an imminent compaction wall), unknown id→200K + logged warning. It seeds `metadata.context_window` at construction in the Claude/headless paths (`get_context_window` is dead-code for Claude — the stream value overrides the seed) and backs `get_context_window` for the other runtimes + the non-abstract ABC default. Pure-stdlib, **vendored byte-identically** into `docker/base-image/agent_server/model_context.py` (Invariant #5, parity-tested); backend downstream fallbacks use the shared `DEFAULT_CONTEXT_WINDOW` constant. **The available-model picker is NOT this map** — the *selectable* catalog (id, label, note, and the `public_channel`/`admin_default_selectable`/`recommended` policy flags) is centralized in `services/model_catalog.py` (#2086, a stdlib-only leaf), which emits the checked-in `src/frontend/src/constants/modelCatalog.js` via `scripts/gen_model_catalog.py`; `ModelSelector.vue`, the `Settings.vue` admin dropdown, and `settings_service.PUBLIC_CHANNEL_MODELS` all derive from it, and `tests/unit/test_2086_model_catalog_parity.py` byte-matches + structurally validates the mirror on every PR. `model_catalog.py` is deliberately separate from this context-window map: it is not vendored (not consumed by the agent runtime), so folding the two together would break the Invariant #5 vendoring contract.

**Codex** (`codex_runtime.py`, built independently on the per-runtime primitives — NOT a shared helper, so it never inherits Gemini's blanket `kill_cgroup_orphans()`): `codex exec --json` → JSONL events (`thread.started`→session id, `turn.completed.usage`→tokens with `reasoning_output_tokens` ⊂ `output_tokens`, `item.completed`→activity, `turn.failed`/`error`); `-o/--output-last-message` is the authoritative result (read-then-delete in `finally`); `codex exec resume <thread_id>` for continuity; cost estimated via `CODEX_PRICING`. Concurrency-safe orphan cleanup via `_drain_bounded` (`kill_cgroup_orphans(extra_pids=…)` preserves siblings). Error→HTTP: auth→503, rate→429, runtime-unavailable→**500** (avoids the AUTH collision), pipe-drop→**502** (SUB-003 guard).

**Parity surface** — every runtime must wire these (Codex specifics in [codex-runtime.md](feature-flows/codex-runtime.md); contract in the [Harness Authoring Guide](harness-authoring-guide.md)): platform **system prompt**, **sandbox** (`_resolve_sandbox_mode`: normal → `--sandbox danger-full-access` since Codex's bubblewrap can't namespace inside the hardened container; read-only → `--sandbox read-only` from `~/.trinity/read-only-config.json` — fail-closed Codex read-only is a fast-follow), **guardrails** (`_load_guardrails()`; unmapped Claude tool-names logged, not dropped), **credential sanitization** (`utils/credential_sanitizer`). A runtime lacking `session_tab_resume` runs a **stateless turn** instead of a `--resume` one (backend constant `RUNTIMES_WITHOUT_SESSION_TAB_RESUME` in `services/session_turn_service.py`; the Workspace additionally keeps its history-replay prefix for those agents, ent#358). The platform prompt is **runtime-aware** (`platform_prompt_service.get_platform_system_prompt(runtime=…)`/`compose_system_prompt(runtime=…)`, threaded from `routers/chat.py` + `task_execution_service.py` via the `trinity.agent-runtime` label): for Codex it strips the Claude-only `mcp__trinity__` prefix (else `unknown MCP server`) and uses bare `trinity` tool names. Backend reads nothing runtime-specific in MVP (infers AUTH from HTTP 503; `ExecutionMetadata.status`/`error_code` unused — fast-follow). Codex agents skip Claude-subscription auto-assign (`is_claude_runtime`).

### Capacity & Backlog (#428)

`CapacityManager` (CAPACITY-CONSOLIDATE) is the single public API for admit/release/status across `/chat` (`max_concurrent=max_parallel_tasks`, `queue_in_memory`) and `/task` (`queue_persistent`). It composes two private internals — `slot_service.py` (atomic N-ary counter, Redis ZSET `agent:slots:{name}`, dynamic per-agent TTL) and `backlog_service.py` (SQLite FIFO over `schedule_executions.status='queued'`, drain-on-release) — and owns the in-memory overflow store (Redis LIST, depth 3). See [capacity-management.md](feature-flows/capacity-management.md).

`run_maintenance()` every 60s: expires stale queued tasks (>24h), drains orphans after restart, runs the #526 breaker-aware backstop, and on each successful sweep writes a unix-timestamp heartbeat to Redis `canary:drain_tick_at` (read by canary B-02; written at sweep END so a mid-sweep crash leaves the cursor stale and trips the check).

**Physical-occupancy shadow meter (#1081 Phase 3, dark):** for a `PULL_MODE_PILOT_AGENTS` agent, `count_active_leased_by_agent` (SQL `running` rows with a non-NULL `lease_expires_at`) is summed into the meter methods `get_all_states`/`get_slot_state` **only** — metering, not admission (`acquire`/`release`/`slot_service` untouched). A pull claim is a pure SQL lease with no ZSET `ZADD`, so the ZSET-occupancy (push) and lease-occupancy (pull) terms are disjoint and can't double-count. Inert when no agent is piloted; physical **admission** is Phase 5.

**Fleet-wide ceiling (#506):** per-agent `max_parallel_tasks` is a two-tier model — an admin sets a fleet-wide ceiling (`max_parallel_tasks_ceiling` in `system_settings`, default 10, range 1–32; no migration), owners pick within it. The runtime clamp is **clamp-on-use** (`settings_service.clamp_to_ceiling`, no per-process cache — `--workers 2` consistency): the `CapacityManager` facade clamps inside `acquire` / `get_slot_state` / `get_all_states` (covering chat ×3, `task_execution_service`, the dashboard, and any future facade reader), and the two genuine facade-bypasses (`backlog_service` drain, `agent_call_limiter`) clamp via `get_effective_max_parallel_tasks`. Stored values are never rewritten; only the *effective* admit limit is capped. The getter is fail-open (settings-read failure → default 10, never crashes dispatch) and read-side range-clamps a stray out-of-range stored value into `[1,32]` (so a `0` can't fail-close the fleet and a `999` can't defeat the cap). Canary **B-02** compares against the **effective** cap (so a lowered ceiling doesn't false-fire); **S-02** keeps the stored cap as a valid upper bound. The `agent_config` GET surfaces `ceiling` + `effective_max_parallel_tasks` for the owner UI. *Known limitation:* `agent_call_limiter` freezes its per-agent semaphore cap at first access — a live agent's semaphore doesn't shrink on a ceiling/cap drop until restart (new agents get the clamped cap immediately).

**Status-as-projection (#1082):** `schedule_executions.status` is a CAS-guarded *projection* of an execution's terminal event — the agent process registry is the runtime authority for "is running"; no backend reader treats `status='running'` as standalone authority (cleanup-watchdog readers use it as a candidate filter, then confirm against the registry/Redis before any destructive write). In `db/schedules.py` every `update(schedule_executions)` writing `status` carries a status precondition in its `WHERE` (incl. `update_execution_to_queued`'s `AND status == RUNNING` guard, closing the E-02 phantom-reversal gap); kept by `tests/unit/test_schedule_status_observability.py`. **Not yet covered (#1082 follow-up):** the standalone scheduler (`src/scheduler/`) writes the same DB with raw-SQL, non-CAS status writers — a late backend `SUCCESS` can still be clobbered on the retry-failure path. See [status-as-projection.md](feature-flows/status-as-projection.md).

### Circuit Breakers (transport + dispatch, #526)

Two independent per-agent breakers, separate Redis namespaces and separate Lua, so they never contaminate each other's counters. Both reuse the `CircuitState` Lua pattern and the shared `redis_breaker_util.py` plumbing; both fail open on Redis down.

**Transport breaker** (`agent_client.py`, key `agent:circuit:{name}`, #631): exponential backoff + dormant state. Only TCP/connection failures count — HTTP 4xx/5xx (incl. 502/503/504) are application errors and skip the failure counter (#474). It reflects **transport health only** — never an administrative state: disabling an agent's autonomy does **not** park it dormant (that #631 AC#5 hook was removed in #1557 because the `execute_task` gate consults this breaker for every trigger, so it fast-failed all inbound chat on a healthy paused agent; proactive work is paused via the schedules instead). The gate's fast-fail message names which breaker fired (`_circuit_breaker_error`, #1557): transport → *unreachable*, dispatch → *auth-dead*.

**Dispatch breaker** (`dispatch_breaker.py`, key `agent:dispatch:{name}`, RELIABILITY-007): producer-side, fed *only* by execution outcomes in `task_execution_service` — counts **AUTH only** (`error_code == AUTH`, agent answers HTTP 503), NOT TIMEOUT/AGENT_ERROR (D10). Consecutive-failure machine `closed → open → half-open(probe) → closed`; default threshold 3, base cooldown 30s, exponential backoff (D9). `record_outcome(error_code)` returns the `(prior, new)` transition; the **caller** backgrounds the drain on `→open` (no `capacity`/`db` import in the breaker → no circular dep, D3). Never raises. `record_failure("missed_heartbeat")` is the #307 seam. `record_success` is a no-op write (Lua early-return) when already closed with zero failures, so healthy agents don't churn Redis. Gating: per-agent `circuit_breaker_enabled` (default OFF) AND global `DISPATCH_BREAKER_ENABLED` must both be on.

**Execution-path flow** (details in [dispatch-circuit-breaker.md](feature-flows/dispatch-circuit-breaker.md)):
- `CapacityManager.acquire(...)` gates the breaker at the TOP (before overflow). A deny raises `CircuitOpen` before any slot/overflow work — a doomed task is never enqueued (**no-enqueue invariant**, D2). A half-open **probe** is admitted ONLY into a free slot (full → fast-fail, never a verdict-less backlog row that stalls backoff, F1).
- `task_execution_service` (single execution path) records every outcome: `record_outcome(None)` at success (resets), `record_outcome(AUTH)` at the HTTP-error terminal (counts). On `→open` it backgrounds `_fail_backlog_and_audit` via `_spawn_bg` (`db.fail_queued_for_agent` → FAILED + clear queue + audit); catches `CircuitOpen` → `TaskExecutionResult(CIRCUIT_OPEN)` + FAILED row. The step-3b pre-dispatch check fast-fails on `state == "open"` only on the backlog-drain path (`slot_already_held and not dispatch_gate_checked`), never blocking an already-admitted probe.
- **Backstop**: if the inline drain is lost, the 60s `run_maintenance` sweep (`_backstop_open_breaker_backlog`) re-fails queued backlog for any still-open breaker (~60s, not the 24h generic expiry).

**Lifecycle clearing (#1560):** both breakers are keyed by agent **name**, carry no TTL, and are therefore inherited by the next container to hold that name — a fresh, healthy agent fast-failed as "unhealthy" without ever being contacted. `services/agent_runtime_state.py` is the single enumeration point for every name-keyed per-agent keyspace (heartbeat, both breakers, slots) and is called from six lifecycle points: `start_agent_internal` (the load-bearing one — every config-drift recreate passes through it, as does the #1809 image-drift recreate: a rebuilt `trinity-agent-base` is adopted on the next **cold** start via the lazily-evaluated `check_base_image_matches` predicate (the start path passes `require_running=False` to `recreate_container_with_updated_config` — #2186: it recreates stopped containers by design and starts them on the next line, and #2092's `True` default made cold-start adoption unreachable and any stopped-agent drift a 500) — fail-open, never **acted on** for a running agent or an ephemeral ghost (#1816 splits the same comparison into a 3-state `check_base_image_state` so a running agent's drift can be *reported* without being acted on; the boolean wrapper is `state != "drift"`); cleared *before* the recreate, since `containers_run(detach=True)` brings the replacement up; guarded on `needs_recreation or not was_already_running` so a no-op start can't reset a live breaker; **#1816** additionally makes the whole `needs_recreation` block a no-op for a **running** `trinity-system` — a structural `is_system AND was_already_running` gate that returns `recreate_deferred="system_agent_running"` instead of replacing the orchestrator's container mid-operation, independent of how many config predicates exist), agent create, the `trinity-system` bootstrap (a permanently-recycled fixed name), delete, rename (old **and** new name), and the retention purge (the instant `is_agent_name_reserved` stops matching and the name becomes reusable). Slots are cleared only where the container is provably gone or stopped — `force_clear_slots` wholesale-`DEL`s `agent:slots:{name}` and would drop capacity accounting for an in-flight #1083 async execution on a running agent. `tests/unit/test_1560_agent_redis_key_parity.py` fails CI when a new `agent:*` keyspace ships unregistered.

API: `GET`/`PUT /api/agents/{name}/circuit-breaker` (owner-only toggle), `POST .../circuit-breaker/reset` (admin-only; resets BOTH breakers) — see API Endpoints.

### Fire-and-Forget Dispatch (#1083)

Removes backend-thread pinning for autonomous turns by construction: an eligible turn is dispatched with a **202 ACK** and runs in the agent's background, then POSTs its terminal back to a callback that finalizes the row. `execute_task` returns right after the ACK, so a wedged turn holds **zero** backend coroutine and the slot becomes a **lease** (released by the callback or reclaimed by the existing TTL reaper). Flag-gated `DISPATCH_ASYNC` (default OFF) AND Claude-runtime only (decision: the typed terminal envelope is Claude-specific); non-202 responses (old image / non-Claude / flag off) fall through to today's **synchronous** handling — the safe mixed-fleet fallback. v1 eligible triggers: **`{schedule, webhook}`** only (the triggers reaching `execute_task` with no synchronous `result.response` consumer; `loop`/`fan_out` stay sync, `event` bypasses `execute_task`).

- **Terminal applier** (`task_execution_service.apply_result`): the single point that finalizes an execution — shared by the inline sync path and the callback. Derives every persisted field from a normalized `TerminalEnvelope` and **gates ALL side-effects on the CAS bool** (`db.update_execution_status` → bool): a CAS-lost write (replay / late callback) completes no activity, records no breaker outcome, and releases no slot. `slot_service.release_slot` also gates the BACKLOG-001 drain on the ZREM result, so a replayed release can't admit past `max_parallel_tasks`.
- **Durable async marker**: `mark_execution_dispatched(async_dispatch=True)` writes `claude_session_id='dispatched_async'` (both sentinels non-NULL, so the no-session sweep / E-05 treat them identically). The callback finalizes **only** RUNNING rows carrying it (fail-closed cross-path guard — never terminal-writes a sync/interactive execution mid-await).
- **Callback endpoint** (`routers/agents.py`, `POST /api/agents/{name}/executions/{id}/result`): agent's own MCP key (mirrors `authorize_heartbeat`) + ownership (404) + marker gate (409) + idempotent replay (an **authoritative** terminal — SUCCESS/CANCELLED/SKIPPED — short-circuits `{replayed:true}`; a FAILED row falls through so a late SUCCESS can still overwrite a reaper `LEASE_EXPIRED` via the CAS, Codex #2) + body-size 413 caps + the #1085 re-delivery-governor gate (**503 + Retry-After** when the shared-cause pause is armed or a fleet/per-agent re-delivery cap is exceeded — placed after replay-ACK/marker-409 so only an accepted async terminal is throttled; 503 is retryable, never a drop). On accept → `apply_result(..., release_slot=True)`, closing the activity via the filtered `get_open_activity_id_for_execution` (chat/schedule_start + `started`, never a shared-eid tool_call row).
- **Agent side** (`agent_server/services/result_callback.py`): `try_spawn_async` gates on async + Claude + execution_id + callback creds; `_run_and_report` runs the headless turn, builds the typed envelope (success → `completed`; HTTPException → status-mapped `error_code`/`terminal_reason`, metadata salvaged from the structured 502 body), **persists** it to `~/.trinity/pending-results/<eid>.json`, and delivers with capped backoff up to the lease deadline (dispatch + `timeout + SLOT_TTL_BUFFER`), deleting on a 2xx / permanent 4xx. A strong-ref `_inflight` set defeats the asyncio GC footgun; a **startup sweep** (`main.py`) re-sends leftover envelopes so a crash/restart mid-callback doesn't lose completed work (a late SUCCESS still overwrites a reaper `LEASE_EXPIRED` via CAS).
- **Lease reaper** (`cleanup_service`): an expired lease (no callback before the slot TTL) FAILs the row with the `lease_expired` tag (`TaskExecutionErrorCode.LEASE_EXPIRED`) and closes its open activity. The stale-execution sweep uses each agent's `timeout + SLOT_TTL_BUFFER` window (not the flat 120-min default) so a legitimately-running max-timeout async turn isn't failed early.

**v1 boundaries**: lease-expiry = FAIL (not re-queue); async empty-result = FAIL (the #678 inline auto-retry stays sync-only); SUB-003 auto-switch stays inline-only (the breaker still protects the fleet); 504/503 async failures write a null-cost row until `execute_headless_task` exposes `ctx.metadata` on those paths (T8 / #1201 fast-follow).

Every CAS-won terminal on this path (the callback's `apply_result` and the lease-reaper) additionally emits a system **completion event** — see [Task Completion Events](#task-completion-events-1578) — and **closes the paired dispatch activity** — see [Terminal-Activity Close Contract](#terminal-activity-close-contract-1804). The close is a property of *winning the CAS*, not of holding the `activity_id` local: `apply_result` is one writer among eight, and the recovery writers around it (watchdog, startup recovery, both bulk sweeps, both shutdown handlers, the lease reaper, the pull sink) each own their own close.

### Task Completion Events (#1578)

The **backend** deterministically emits `agent.task.completed` / `agent.task.failed` at **every CAS-won execution terminal**, delivered over the existing EVT-001 subscription-dispatch machinery ([agent-event-subscriptions.md](feature-flows/agent-event-subscriptions.md)), so a subscribed caller/orchestrator is **woken with a report-back task** when a long async task finishes instead of polling `get_execution_result`. Implements the missing half of `TARGET_ARCHITECTURE.md` §Async-First Communication; a down-payment on Epic #1045 → #1081. Full flow: [task-completion-events.md](feature-flows/task-completion-events.md).

- **System- vs agent-emitted.** EVT-001 carries only **agent-emitted** events (an agent's LLM calls `emit_event`, `source_agent` from the MCP auth context). These are the first **system-emitted** events — synthesized by the deterministic backend chokepoint with no LLM in the loop, `source_agent` = the executing agent, reserved `agent.task.*` namespace. Same tables (`agent_events` :1528, `agent_event_subscriptions`), same `find_matching → trigger_subscription` delivery; different producer.
- **Shared emit helper** (`services/event_dispatch_service.py`): `emit_task_terminal_event` (async, fail-open, matching-sub gated — empty ⇒ no `agent_events` row, no dispatch) + `spawn_task_terminal_event` (strong-ref `create_task` wrapper every terminal writer calls). Status→event maps on the **status string** (never `TaskExecutionErrorCode` identity — the fieldless `@dataclass` `__eq__` #1085 footgun); the payload `status` is `.value` (`"success"`, not `"TaskExecutionStatus.SUCCESS"`). Flat payload `{execution_id, status, triggered_by, summary_or_error, duration_ms, cost, fan_out_id, loop_id}` (`fan_out_id`/`loop_id` carried for the future pull fan-out join envelope). Dispatch primitives (`trigger_subscription`/`_interpolate_template`/`_get_internal_token`) were moved verbatim out of `routers/event_subscriptions.py` so a service can reuse them without importing a router (Invariant #1; verified cycle-free).
- **CAS-won terminal-writer coverage** (the fix): `apply_result` success + failure branches, `_write_terminal_and_gate` (**timeout / budget / crash** + inline circuit-open/capacity/ephemeral — the terminals that never reach `apply_result`, the exact wedge case the feature exists for; `agent_name` threaded from its `execute_task` callers), the #1083 **lease-reaper** (`cleanup_service::_process_stale_slot_reclaims`), and the **pull sink** (`pull_coordination_service::apply_task_result`, dark until a pull pilot). Both #1083 caller paths (inline sync + async callback) converge on `apply_result`. **Bulk watchdog sweeps are now covered too (#1714):** `cleanup_service._sweep_stale_executions` / `_sweep_no_session_executions` collect the CAS-won `(execution_id, agent_name)` rows (the bulk-fail db fns take a `collect_failed` list) and `_emit_bulk_terminal_events` emits `agent.task.failed` per row — subscriber-gated (a cheap `has_task_terminal_subscribers()` short-circuit makes a no-subscriber sweep free; per-agent matching still in the emit helper), paced in batches (no thundering herd, no row dropped), and fail-open (never affects the already-committed terminal write). Only genuinely-uncovered residual now: bulk COUNT sweeps elsewhere with no per-row row set.
- **Reserved namespace + 3-layer loop safety** (`routers/event_subscriptions.py`): agents cannot `emit_event` into `agent.task.*` (400 on both emit routes); reserved self-subscription (`source==subscriber`) is blocked on create **and** update (400 — the PUT guard closes the "PUT a benign self-sub into the reserved namespace" bypass); and the decisive **recursion-break** — `trigger_subscription` tags a reserved-namespace loopback with `X-Event-Trigger`, `routers/chat.py` persists that spawned execution's `triggered_by="event"` (already a reserved value in `_AUTONOMOUS_TRIGGERS`), and the emit helper suppresses re-emission when the terminating execution carries it. Breaks self / A↔B / A→B→C→A auto-emit cycles at the root (each hop = a full LLM turn + spend).
- **Delivery = best-effort, pull-transitional (honest).** `trigger_subscription` is an HTTP loopback (`POST /api/agents/{subscriber}/task`, admin JWT). It wakes a **running** subscriber (incl. a #1402 parked-but-running orchestrator); a *stopped* subscriber's 503 is swallowed — the `agent_events` row persists, the wake does not. NOT durable and NOT the WS `event_bus` (a broadcast can't wake a parked agent); the durable queue is the pull migration's future. `summary_or_error` (worker output, credential-sanitized at the emit chokepoint + truncated ~2000) is the same content-trust interpolation surface EVT-001 already has, now deterministic. The recursion-break `X-Event-Trigger` header is honored only with a valid backend-internal `X-Internal-Secret` (C-003), so an external `/task` caller can't spoof it to suppress a real completion.
- **Additive & inert**: reuses `agent_events`/`agent_event_subscriptions` + the existing `triggered_by` TEXT column — no schema change, no migration, no feature flag, no config. Zero matching subs ⇒ zero rows, unchanged behavior.
- **The emit set is NOT the close set (#1804).** The #1578 writer list is *approximately* the terminal-activity-close list, and the difference is a bug class: `task_execution_service`'s and `routers/internal`'s backend-shutdown `CancelledError` handlers write a terminal and emit nothing. The #1804 parity guard is therefore anchored on **terminal writes**, not on emission — see [Terminal-Activity Close Contract](#terminal-activity-close-contract-1804).

### Terminal-Activity Close Contract (#1804)

**Every writer that wins a terminal CAS on `schedule_executions` closes the paired `agent_activities` dispatch row.** Before this the close was gated on holding the `activity_id` local *and* winning the CAS in the same coroutine, so every out-of-band terminal writer wrote the terminal and walked away: the activity stayed `activity_state='started'` (the Dashboard Timeline rendered the agent as still working, `ReplayTimeline.vue`) until the generic 120-minute sweep closed it with a fabricated `duration_ms = now − started_at` that nothing ever recomputes — a 15-minute run permanently recorded as a ~120-minute failure. Third appearance of the class after #45 (tool-call activities) and #767 (CB probes), both of which patched a single producer and left the ownership model alone. Full flow: [activity-stream.md](feature-flows/activity-stream.md).

- **One owner** — `activity_service.close_execution_activity(execution_id, terminal_status, *, error, activity_id)` (+ the sync `spawn_close_execution_activity` wrapper for the synchronous pull sink), structurally the twin of `event_dispatch_service.spawn_task_terminal_event`. It maps the terminal via the shared `models.activity_state_for_terminal` (#1332 — never a second mapping), delegates to `complete_activity` so the `agent_activity` WS broadcast + subscriber notify survive (a db-layer close would lose them), and is **fail-open**: it runs after a committed terminal and can never affect it.
- **The close is itself a CAS.** `db.complete_activity` returns `ActivityCloseOutcome` (`UPDATED` / `ALREADY_CLOSED` / `NOT_FOUND`) — one bool cannot answer both "did this row exist" (`routers/internal.py` 404s on it) and "did anything change" (the broadcast gate), and once idempotent no-op closes are designed behaviour the two answers diverge routinely. "The CAS winner owns it" only holds if a *second* closer trying is safe; the unconditional `UPDATE` let a later writer overwrite an earlier one's `completed_at`/`duration_ms`/`error` — the wrong-duration symptom by a new route.
- **The lattice mirrors the execution predicate** (`db/activities.py::_close_predicate`, diagram inline), stated as the authority ordering rather than copied literally: incoming COMPLETED → `activity_state IN ('started','failed')` (an authoritative close MAY upgrade a provisional FAILED — the #1083 late-SUCCESS-after-lease-expiry path); incoming CANCELLED/FAILED → `activity_state = 'started'` (nothing overwrites an authoritative close). Authority ordering: `started < failed < {completed, cancelled}`. The COMPLETED arm is deliberately **tighter** than the execution row's own `!= CANCELLED`, which also admits `success → success`: inheriting that edge would let a second COMPLETED close rewrite a real 15-minute `duration_ms` as `now − started_at` — the #1804 symptom re-entering through #1804's own fix, reachable from `_write_terminal_and_gate`'s lost-CAS branch (it passes an explicit `activity_id`, so the narrower lookup cannot shield it). The **lookup** is widened to agree (`get_open_activity_id_for_execution(..., include_failed=True)` for authoritative terminals) — a lookup narrower than the write makes the whole fix inert.
- **Split by cardinality.** Single-row recovery paths use the per-row helper (WS broadcast preserved); the two bulk sweeps use `db.close_open_activities_for_executions` — set-wise (a re-queued execution can own more than one open dispatch activity), one transaction, no per-row WS, chunked at `_SQLITE_MAX_IN_VARS`. `cleanup_service._close_bulk_swept_activities` is a **sibling** of `_emit_bulk_terminal_events`, never folded into it: that method short-circuits on `has_task_terminal_subscribers() is False`, which would skip the close on every install with no event subscribers. It consumes the `collect_failed` rows #1714 already collects — no new query.
- **Wired writers**: `_write_terminal_and_gate` (won **and** lost — the lost branch was the missing mirror of the SUCCESS applier's own reconcile), `apply_result` (both branches), both backend-shutdown `CancelledError` handlers, watchdog + startup `_recover_execution` (the latter's CAS bool was previously *discarded*), the two bulk sweeps, the lease reaper (park → FAILED, re-queue → **CANCELLED**: a superseded attempt is not a failure), the pull sink, and `terminate_execution`. Guarded by `tests/unit/test_1804_terminal_activity_parity.py`, anchored on terminal writes with an explicit justified allowlist for admission-path terminals (no activity exists before `execute_task` step 3).
- **Observability**: `CleanupReport.activities_closed_on_recovery`. `stale_activities` should trend to ~0 while it picks up the volume; non-zero `stale_activities` means a producer is still unowned. The 120-minute `mark_stale_activities_failed` backstop is demoted to *a backstop for the unclaimed* and moved to run **after** `_sweep_stale_slots` (it used to run one line before the reaper that legitimately closes activities, so within a single cycle the duration fabricator could beat a real closer). #429 deletes the backstop entirely — a contract survives that, per-site patches would not.

### Correlated-Failure / Thundering-Herd Controls (#1085)

Makes the live #1083 re-delivery path safe at fleet scale (and structured as
reusable primitives the future pull-mode re-delivery, Epic #1045/#1081, consumes
unchanged). A backend restart re-sends ~N persisted terminal envelopes plus
in-flight callback retries; without controls they hammer the callback endpoint in
lockstep. Three primitives — **jitter**, **re-delivery rate caps**, and a
**shared-cause pause** — all **fail-open**; the backend controls are
**default-OFF** behind one master flag (`REDELIVERY_GOVERNOR_ENABLED`). No DB
schema change (all state is Redis). Details in [redelivery-governor.md](feature-flows/redelivery-governor.md).

- **Jitter (agent-side, unflagged)** — `agent_server/services/result_callback.py`. `_deliver` uses **decorrelated jitter** (`min(cap, uniform(base, prev*3))`, AWS pattern: self-paces *and* spreads, vs lockstep exponential) and honors a server `Retry-After` as a **floor**. `resend_pending_results` adds a one-shot **initial jitter** (≤60s) so a restart smears the t≈0 sweep burst over a minute, plus a small per-envelope jitter. Backend loop periods in `main.py` (capacity maintenance) are jittered so replicas don't realign. The jitter helper is duplicated agent-side, **not** vendored — Invariant #5 governs mirrored API/policy contracts, not utility math (the backend never inspects the agent's backoff).
- **Re-delivery rate caps (backend)** — the callback endpoint gates, after the fail-closed checks + replay-ACK + marker-409, on two `services/rate_limiter.check` keys: `redelivery:fleet` (≈10/s) and `redelivery:agent:{name}`. Over-limit → **503 + Retry-After** (NOT 429 — 503 ∉ `result_callback._PERMANENT_STATUSES`, so a throttled callback stays persisted and retries; the startup sweep + lease reaper are the never-drop backstops).
- **Shared-cause pause (`services/redelivery_governor.py`)** — a leaf service over the shared fail-open breaker Redis client (`redis_breaker_util.get_breaker_redis`), singleton `get_redelivery_governor()`. `apply_result` records AUTH/BILLING terminals **on the CAS-`won` branch only** (no replay double-count) into a Redis ZSET (`governor:corr_failures`, member=agent_name) — **counting distinct agents, not events** (`ZCARD`), so one crash-looping agent can't arm it. At `≥ CORRELATED_FAILURE_THRESHOLD` distinct agents it sets `governor:pause` with a TTL (`CORRELATED_PAUSE_TTL_SECONDS=300`, well under the lease window) — **auto-expiry, no explicit unpause** (no stuck-pause failure mode). Three read points, all flag-gated: the callback endpoint → 503 while paused; the lease reaper (`cleanup_service._sweep_stale_slots`) → hold off (keep async rows RUNNING, not FAILED→LEASE_EXPIRED, so a throttled-then-resumed callback still lands); the capacity drain (`capacity_manager.run_maintenance`) → skip `drain_orphans_all`/breaker-backstop (keep the 24h `expire_stale`).
- **BILLING populated (#1085)** — `result_callback._STATUS_MAP` now maps an agent `429 → ("billing", "rate_limit")` (the enum existed but was never set) so the detector catches a fleet-wide Claude-API 429 storm alongside AUTH. `terminal_reason` stays `rate_limit`, so the cancel-relabel guard still treats it as auth/rate (never a clean cancellation).

`redelivery_governor_enabled` is surfaced in `GET /api/settings/feature-flags` for operator observability during soak (mirrors `mcp_agent_chat_pull_enabled`; not a UI surface).

### Heartbeat Liveness (RELIABILITY-004, #307)

Additive push-heartbeat layer; the 30s `monitoring_service` loop (lifespan-resumed, default-off, #1121) stays authoritative for aggregate status when enabled.

**Agent side** (`agent_server/heartbeat.py`): 5s loop, gated on `TRINITY_BACKEND_URL` + `TRINITY_MCP_API_KEY`. POSTs `{memory_mb, active_executions, uptime_s}` to `POST /api/agents/{name}/heartbeat`, authenticated with the agent's own agent-scoped MCP key (least privilege, no master secret). `memory_mb` from `/proc/self/status` VmRSS (no psutil). Sleeps-first and swallows **all** exceptions — a failed beat is silent by design; the backend watch loop acts on absence.

**Backend side** (`heartbeat_service.py`): owns all Redis heartbeat keys — `record_heartbeat` (SETEX 15s + persistent `seen` marker), `read_heartbeat`, `heartbeat_status`/`heartbeat_status_bulk` (one pipelined round-trip, D4), `authorize_heartbeat` (403 unless the key is agent-scoped and its `agent_name` matches the path; user/system/null rejected; validated `track_usage=False`). Keys:

```
agent:heartbeat:{name}        → STRING, 15s TTL. JSON {ts, memory_mb, active_executions, uptime_s}
agent:heartbeat:seen:{name}   → STRING "1", no TTL. Absent ⇒ unsupported (old image, never marked dead);
                                present + TTL-key alive ⇒ alive; present + TTL-key gone ⇒ stale
agent:heartbeat:misses:{name} → STRING(int), ~60s TTL. Consecutive-miss counter; never persisted to SQLite
```

**Watch loop**: 5s (staggered +10s), batched Redis pipeline over `seen`-marked agents, 3-miss guard. Fires a soft, cooldown-debounced operator alert (`monitoring_alerts` path) **only on the alive→stale transition**, plus a recovery notification when beats resume after a prior downgrade — one alert per loss episode. Writes no health-check rows. `clear_heartbeat(name)` deletes all three keys, best-effort on agent delete and rename (old name) — `seen` has no TTL, so otherwise it leaks one permanent key per agent. The five `heartbeat_*` fields surface on `GET /api/monitoring/status` via one batched Redis read.

### Idempotency (RELIABILITY-006, #525)

Trigger-boundary dedup — policy in Architectural Invariant #18, table DDL under `idempotency_keys`, details in [idempotency-keys.md](feature-flows/idempotency-keys.md). `services/idempotency_service.py` (`begin`/`complete`/`fail`) over `db/idempotency.py`. The `(scope, key)` PRIMARY KEY is the atomic claim: `claim()` INSERTs an `in_flight` row; a concurrent loser catches `IntegrityError` and reads the surviving row (cross-process safe over the shared SQLite file). Lifecycle: `claim` → (`attach_execution`) → `complete` (stores `response_snapshot` for replay) or `release` (deletes the in_flight row so retry is possible; never deletes a `completed` row). Rows >24h expire and re-claim (cleanup purges via `idempotency_purge_expired`). Duplicates within 24h short-circuit with the original result + `X-Idempotent-Replay: true`; an in-flight duplicate returns 409. Fail-open.

**Effect-scoped extension (#1084):** trigger-boundary dedup stops a re-POSTed `/chat`/webhook from creating a *second execution*; it does NOT reach an agent's individual outbound tool calls. So a re-delivered turn (the at-least-once semantics pull-mode / work-stealing will introduce, Epic #1045/#1081) re-emits the same side effect (re-sends a message, re-charges a payment). The same `idempotency_service` adds a per-sink guard — `effect_guard(effect_type, identifying_args, *, execution_id, agent_name, dedup_label, payment_request_id)` — enforced at the SINK, per resolved action identity. Scopes: `effect:{execution_id}` for messages/voip/share_file (after `resolve_and_validate_execution` confirms the execution belongs to the agent — generalizing MEM-001), `payment:{agent_request_id}` for Nevermined settles (a Nevermined observability id, **not** a provider exactly-once token — this local guard, not the provider, enforces at-most-once per id; residual at-least-once retry tracked by #1408). Key = `{effect_type}:sha256(execution_id ∥ effect_type ∥ resolved_identifying_args ∥ dedup_label)` on **resolved, immutable** identity only (recipient/channel/account) — **never the LLM-generated body** (non-deterministic across a re-run → would defeat dedup); `dedup_label` lets an agent intentionally repeat an effect to the same target in one turn. `in_flight ≠ completed`: a completed replay returns the stored sanitized snapshot (no re-emit); an in-flight replay raises `EffectInProgressError` (router → 409, never a silent skip-and-succeed). Reuses the 24h default TTL (already exceeds the lease window, so a completed row outlives a late re-delivery — no new TTL plumbing). Wired sinks: `proactive_message_service.send_message`, `voip_service.place_outbound_call`, `agent_shared_files_service.create_share`, `nevermined_payment_service.settle_payment_once`; agents pass `execution_id`+`dedup_label` as MCP tool args (`messages.ts`/`voip.ts`/`files.ts`), **fail-open when absent** (safe today — pull-mode re-delivery is OFF). Trusted runtime injection of `execution_id` + fail-closed-when-absent is a **BLOCKING prerequisite** on Epic #1045/#1081 before pull-mode default-ON for side-effect agents (git push is idempotent-by-construction and needs no key). **Target-direction note:** `TARGET_ARCHITECTURE.md` v2 reframes this from a **per-agent** gate to **per-effect** — read/analysis-only + reversible + capability-confined-irreversible effects default on, only irreversible-**un-confineable** effects gate via the async operator queue (#1402); `effect_guard` (this section) is the reversible/backend-sink slice and retry-with-prior-trace (#1401) is the general recovery. See [effect-idempotency.md](feature-flows/effect-idempotency.md).

### Subscription Token Rotation via Hot-Reload (#1089)

Rotating an agent's subscription token used to recreate the container, making "rotate a credential" and "kill every in-flight turn" the same operation (#1037). Rotation now hot-reloads the running container; recreate is reserved for image/template/auth-**mode** changes. The agent server authenticates Claude purely from `CLAUDE_CODE_OAUTH_TOKEN` and is a single uvicorn worker, so mutating its process env makes the **next** subprocess use the new token while in-flight ones finish on the old.

Backend orchestration in `services/subscription_auto_switch.py`: `_hot_reload_subscription_token(agent_name)` POSTs the DB token to the agent-server `POST /api/credentials/reload-token`, falling back to `_restart_agent` on 404/transport failure/missing token. Three producer paths converted, all under the #799 `agent_switch_lock`: **auto-switch** (`_perform_auto_switch`, SUB-003), **manual sub→sub reassignment** (`PUT /api/subscriptions/agents/{name}`; auth-mode changes still recreate), and **key rollover** (`reload_subscription_for_all_agents(sub_id)` fans a best-effort reload across running agents). Durable override (`/var/lib/trinity/oauth-token`) + `startup.sh` read make a rotation survive a plain restart. **#2114:** the helper sends `remove_api_key=True` for Claude-runtime agents (`trinity.agent-runtime` label, claude-code default; non-Claude keep `False` — their scripts may legitimately use a `.env` `ANTHROPIC_API_KEY`, which never shadows anything there); the endpoint force-unsets `ANTHROPIC_AUTH_TOKEN` alongside `ANTHROPIC_API_KEY` and returns `env_shadow` (names of force-unset keys the current `.env` still carries), which the backend logs at WARNING — so a `.env` key shadowing subscription auth is diagnosed from the backend log at switch time instead of a container-log line nobody tails. Agent-server mirroring follows Invariant #5.

### Real-time Delivery (RELIABILITY-003, #306)

**Transport** (`event_bus.py`, details in [websocket-event-bus.md](feature-flows/websocket-event-bus.md)): Redis Streams. `ConnectionManager`/`FilteredWebSocketManager` are thin shims that `XADD` to the MAXLEN-trimmed `trinity:events` stream; one `StreamDispatcher` per backend process runs `XREAD BLOCK` and fans out, evicting a client after 3 consecutive delivery failures. New broadcast sites keep calling `manager.broadcast(...)` / `filtered_manager.broadcast_filtered(...)` — never publish to the stream directly (Invariant #10).

**Reconnect replay**: `/ws` and `/ws/events` accept `?last-event-id=<stream_id>`, regex-gated (`^\d+-\d+$`) by `validate_last_event_id()` before `XRANGE`. Catchup capped at `REPLAY_GAP_LIMIT=5000` — larger gaps return `{"type": "resync_required", "reason": "gap_too_large"}`. Authorization (`accessible_agents` for `/ws/events`) is re-applied on replay. The frontend tracks `_eid` per message, appends `&last-event-id=` on reconnect, and on `resync_required` clears the cursor and refetches via REST.

**WebSocket auth** (C-002, #550): `/ws` uses single-use opaque tickets, not a JWT in the URL: `POST /api/ws/ticket` mints a 32-byte urlsafe ticket (Redis, 30s TTL); client connects `/ws?ticket=...`; backend atomically `GETDEL`s then accepts. Closes the JWT-leak surface (nginx logs, history, proxies); CSWSH mitigated because minting needs the JWT in an `Authorization` header (CORS-blocked cross-origin). `/ws/events` still accepts `?token=trinity_mcp_*` for external scripts (scoped, revocable). `mint_ticket` optional `ttl_seconds` (default 30s, ceiling 600s); VoIP mints call-bound tickets (`scope="voip:{call_id}"`, 180s) since PSTN dial+ring exceeds 30s. Impl: `services/ws_ticket_service.py` + `routers/ws_tickets.py`.

### Soft Delete, Retention & Recovery (#834, #772)

**Agent soft-delete (Phase 1a):** `DELETE /api/agents/{name}` sets `agent_ownership.deleted_at` (child rows preserved, recoverable until purge). `is_agent_name_reserved()` sees soft-deleted rows, so the name can't be reused before purge. The scheduler's `list_all_enabled_schedules()` filters `deleted_at IS NULL`, so schedules stop firing immediately.

**Schedule soft-delete (Phase 1b):** `DELETE .../schedules/{id}` sets `agent_schedules.deleted_at` (row + `schedule_executions` preserved). All read paths — incl. cron firing in backend and standalone scheduler — filter `deleted_at IS NULL`. `delete_schedule()` is idempotent on an already-soft-deleted row.

**Admin recovery (Phase 1c):** metadata-only (`deleted_at → NULL`) via `/api/admin/soft-deleted/*`. Agent recovery does NOT recreate the container (`needs_container_recreate=true`; operator runs `POST /api/agents/{name}/start`); schedule recovery rejoins the firing list next poll if enabled. Audit `agent_lifecycle:recover` / `schedule_recover`. Models `SoftDeletedAgent`/`SoftDeletedSchedule`.

**Cleanup Service sweeps** (every 5 min): #772 retention — nulls `schedule_executions.execution_log` past `execution_log_retention_days` (default 30), DELETEs terminal `schedule_executions` past `execution_row_retention_days` (default 90), DELETEs `agent_health_checks` past `health_check_retention_days` (default 7). #834 purges — hard-deletes `agent_ownership` rows soft-deleted past `agent_soft_delete_retention_days` (default 180; `0`=off), cascading children via the #816 `purge_agent_ownership`/`cascade_delete` primitive; hard-deletes `agent_schedules` past `schedule_soft_delete_retention_days` (default 30; `0`=off) via `purge_schedule()`, cascading its `schedule_executions`. #1449 backlog-metadata PII scrub — NULLs `schedule_executions.backlog_metadata` (the drain-replay blob carrying `user_message`/`user_email`/`system_prompt`) on authoritative-terminal rows (`success`/`cancelled`/`skipped`) via `db.scrub_terminal_backlog_metadata` (chunked, each chunk its own txn). **FAILED is excluded** — a FAILED row is resurrectable to SUCCESS via a late token-gated CAS, so its intent must survive (FAILED PII stays bounded by the 90-day row DELETE). Runs **unconditionally** (not age-gated, no ops-settings key — a security invariant, not an operator knob, per the #1638 floor-by-seed trap); count-only logging (the blob is never logged); the scrubbed count feeds the WAL-checkpoint sum. Nothing reads terminal `backlog_metadata` — the drain claims only `queued` rows, the #1083/#1081 callbacks read the POST payload, and canary E-04/G-04 are queued-scoped. (The sibling #1444 carve-out — callback/pull-path chat-session persistence — is deferred to the #1081 single-applier work.)

**Blast-radius guard (#1644).** #1638 made the *defaults* fail-safe; it added no guard on the prune. So every other route to a destructive window stayed open — `PUT /api/settings/ops/config` (which until ent#297 took a raw `Dict[str, str]` with no type/range/clamp), a future default regression, a direct DB write. **Write-path hardening (ent#297):** the windows now have exactly ONE API write path — `PUT /api/settings/ops/config`, which type- and range-validates every value (`config.validate_ops_setting`, all-or-nothing, 422 on the first bad one) and audit-logs the change (`ops_settings_change`, naming which retention windows moved; neither this route nor `/ops/reset` logged anything before, so the one route that could shrink a window was also the one that left no trace). **`/ops/reset` audits too, as of #1966** — ent#297 wrote the sentence above but wired the entry into `/ops/config` only, so the asymmetry it objected to survived one route over. It emits `ops_settings_reset` carrying the `reset`/`skipped` key lists — **keys and counts only**, since every one of those rows is being deleted and the durable fact is which keys reverted to their code default, not what they held on the way out — and it logs **unconditionally**, not gated on having deleted something: an admin resetting already-default settings is still an administrative act, and its absence from the log would be indistinguishable from never having been attempted. Retention windows genuinely cannot be reset here (#1638 `continue`s over them, and the entry reports them under `skipped`); what could silently revert with no trace was everything else — `ssh_access_enabled`, which decides whether ephemeral SSH credentials may be minted at all, included. The generic `PUT /api/settings/{key}` catch-all now 422s `RETENTION_OPS_KEYS` and points at it, closing the second unvalidated path — same shape as the `max_parallel_tasks_ceiling` (#506), `PROACTIVE_RATE_LIMIT_DEFAULTS` (#1609) and `telemetry_sharing_*` (ent#12) redirects. Note what validation does NOT buy, since the asymmetry is counter-intuitive: garbage always failed *safe* (unparseable → `0` → sweep disabled → retain forever), and **a small valid integer is the catastrophic input** — `"1"` is well-typed and in-range and is exactly the PoC. No range check can separate it from an operator who genuinely wants a one-day window, so validation buys a loud failure instead of a silent coercion; the controls that stop the *attack* are the admin gate rejecting agent principals (ent#293/#297) and this guard. The community floor is deliberately NOT applied as a clamp here (#1039/#1638: it is a fresh-install seed plus an enterprise entitlement clamp, never an OSS hard limit). `services/retention_guard.py` gates all **10** window-driven destructive prunes (`RETENTION_OPS_KEYS` carried 8 from #1296's `agent_reminders_retention_days` until ent#433 added `subscription_headroom_retention_days` and `subscription_failure_event_retention_days`; `cleanup_service` has 10 `_guard_allows` sites): it takes a **bounded** count of the candidate set (`LIMIT threshold+1` → O(threshold), not O(candidates)) and, if it exceeds the threshold, **refuses**, logs ERROR on the green→red transition, and raises an `operator_queue` alarm. `cleanup_service._guard_allows` is not its only consumer — `GET /api/settings/retention` re-runs the same `evaluate` **live** so the Settings panel can offer an approve control, and that call is **unwrapped**, which is why the guard must refuse rather than raise (#1833). Key properties: **stateless detection** — no watermark of the last-seen window, because that row would be deletable through the same endpoints that cause the bug (`learnings.md` #1638 Lesson 2); the ack *is* state, but deleting an ack fails safe. **Absolute counts, no percentage** — there is no denominator for `execution_log` (its predicate *includes* `execution_log IS NOT NULL`), a total would cost a full scan and divide-by-zero on an empty table, and a percentage *inverts* on the agent sweep (3 purged agents ≈ 0% of any table but 3 destroyed volume sets); steady state is a trickle and any anomaly is large, so table size is irrelevant. **Fail-closed** — every error path refuses: the count throws (`count_failed`), the count cannot be compared to the threshold (`count_uninterpretable`, #1833), the count is negative i.e. an error sentinel rather than a count (`count_negative`, #1833 — `-1 <= threshold` is True and `-1` is the module's own "unknown" value, so this was a genuine fail-OPEN), the ack lookup throws (`ack_lookup_failed`). There is **no** "threshold unreadable" path, because the threshold is a constant. A guard that fails open manufactures confidence — and one that RAISES instead of refusing keeps the data (control never reaches `db.prune_*`) while losing the alarm, which is why #1833 moved the comparisons inside the try rather than documenting the raise. The comparison result is **type-checked**, not coerced: an object whose `__le__` returns a truthy non-bool would make a bare `bool(...)` True and authorize a prune on a count the guard never understood. `verdict.candidates` is normalised to an int before publication (`-1` = unknown) because it reaches the alarm message, the alarm `context` via `json.dumps`, and `GET /api/settings/retention` — where a bare `NaN` is valid to Python and rejected by a browser's `JSON.parse`. A refusal for a reason an ack cannot clear says so instead of prescribing one, and `GET /api/settings/retention` reports it under `blocked_sweeps` rather than rendering a clean "nothing pending" for a sweep that is blocked forever — **scoped, like the `pending_acknowledgements` list it sits beside, to the two ack-gated sweeps that endpoint re-runs** (`agent_soft_delete_retention_days`, `schedule_soft_delete_retention_days`); the other eight windows are never evaluated there, so their refusals reach an operator only through the durable operator-queue alarm. Widening that loop to all 10 is a follow-up, not an oversight — it would change what the endpoint costs and what an "approve" control means for a sweep whose floor is 1000. **The ack endpoint is the gate; the queue item is only an alarm** — `create_item` is a blind `INSERT ... ON CONFLICT DO NOTHING`, so a load-bearing queue item would wedge shut permanently once it reached any terminal state (e.g. Clear All → `cancelled`), and `prune_terminal_items` would delete the approval at 90 days — the sweep deleting its own authorization. **A failed alarm write is re-attempted every cycle for the life of the refusal episode (#1834)** — the memo used to be written *before* the attempt, so one failed write permanently suppressed its own retry and the durable half of the signal was lost until a restart. Never abandoned, which is the deliberate divergence from #1897's give-up: that sink is an external webhook, this one is the platform's own DB and its outages routinely outlast any budget, so a give-up would ship #1834's symptom inside #1834's fix. The per-attempt WARNING escalates **once** to ERROR past `ALARM_ESCALATION_AGE_SECONDS`, worded to claim only what that worker knows (`cleanup_service` runs in every uvicorn worker with no leader lease, so a sibling may have landed the row). Safe precisely because the queue item authorizes nothing, and because "delivered" means "the call did not raise" — never "a row was inserted", or the second worker's conflict no-op would retry forever. `POST /api/settings/retention/acknowledge` (admin **+ `reject_agent_principal`**, since an agent-scoped key resolves to its owner *carrying the owner's role* and would otherwise pass `require_admin` on a default admin-owned install — see trinity-ops-agent#232) records an ack **bound to the window in force** (409 on mismatch) and **single-use** (consumed by `cleanup_service` once the prune actually runs, so the guard re-arms). The threshold (`retention_guard.MAX_ROWS_PER_SWEEP`, 1000) is a **fixed constant, deliberately not a setting** — it was briefly an operator knob with a Settings panel, which was wrong twice over: nobody can reason about the right value (it depends on per-cycle churn the operator can't see, so the panel needed a caption explaining that *bigger is worse* — a control that must explain which way is safe is the wrong control), and a mutable constant read at action time gating a destructive op is structurally identical to `OPS_SETTINGS_DEFAULTS`, i.e. #1638 one level up. Deleting the knob deleted its clamp, its range-validated endpoint, its catch-all blocklist entry, and one whole fail-closed branch (a constant cannot fail to read). It is chosen against **steady state, not table size**: at any sane window only rows crossing the cutoff within one 5-min cycle are candidates — a trickle of tens; four digits means something changed. Lowering is always safe; raising weakens every install at once, so `tests/unit/test_1644_retention_guard.py` pins the *direction* (`MAX_ROWS_PER_SWEEP <= PREVIOUS`), not the value, and asserts it is not settings-backed. Reported read-only via `GET /api/settings/retention` → `guard.max_rows`. Per-sweep floors: rows→`MAX_ROWS_PER_SWEEP`, schedules→100, **agents→0** (every purge destroys volumes, #1581 — always acked). Each count **shares its prune's predicate by construction** (`_execution_row_prune_predicate` et al.) so the two can never drift; the alarm hosts on the reserved sentinel `_retention-guard`, uncreatable because `sanitize_agent_name` strips the leading `_`, and excluded from canary L-03's `operator_queue` orphan scan (it is not a ghost agent). Alarm `context` carries **counts and identifiers only, never sample rows** (canary G-04's lesson).

**Retention resolution — floor-by-seed, not floor-by-default (#1638).** Each window resolves **DB row → code default**; there is **no env layer** for these keys (only log archival reads `LOG_*`). Because the code default is the fallback for every install that never wrote a row — the default state — it is read at *prune* time and must stay at the **widest** value: `OPS_SETTINGS_DEFAULTS` is a **safety floor, not a policy knob**. Lowering one retroactively hard-DELETEs the existing data of every un-configured install seconds after its next boot, silently (#1638, which cost a real instance ~3 months of history and shipped green because a test asserted the destructive default). The #1039 `COMMUNITY_RETENTION_FLOOR_DAYS=5` community floor therefore reaches **new installs only**, by *seeding* explicit rows into a fresh DB (`config.COMMUNITY_FRESH_INSTALL_SEED` → `database._seed_fresh_install_retention{,_engine}`, run inside `init_database()` after `init_schema` and **before** `_ensure_admin_user` — an empty `users` table is the fresh-install signal). The seed is fail-safe (never raises: `init_database()` runs at import, so raising would crash-loop boot) and idempotent (`INSERT OR IGNORE` / `on_conflict_do_nothing` — both migration locks fail open). `agent_soft_delete_retention_days` is **exempt from the floor in every edition**: its expiry destroys agent data volumes (#1581), making it a *recovery* window, not a log window. The floor is **not enforced** — any admin may widen a window via `PUT /api/settings/ops/config` (no clamp); the enterprise `retention` module clamps only *unentitled writes* to a **minimum** of 5 and sells the managed panel (audit/`updated_by`/hot-reload), not the capability. `POST /api/settings/ops/reset` **skips** `RETENTION_OPS_KEYS` (#1638 — "reset to defaults" must not silently change how much operator data is kept). **`RETENTION_OPS_KEYS` carries all 11 keys — 10 row windows + #2216's file-artifact `backup_retention_days`** — #1644 added `agent_reports_retention_days` and `operator_queue_retention_days`, which had been missing, so `/ops/reset` **deleted** those two rows while reporting `"retention windows unchanged"`, `GET /api/settings/retention` hid them, and boot logging never showed them; #1296 then added `agent_reminders_retention_days`, taking the row set to 8 (this doc said 7 in two places until #1833), and ent#433 took it to 10 with `subscription_headroom_retention_days` (the new probe-history table) and `subscription_failure_event_retention_days` (which **converted** `subscription_rate_limit_events`' hardcoded 24h sweep into a real window — that table held the only durable record of real agent work hitting a provider rate limit and destroyed it daily with none of the three protections every sibling had). Membership means "is a retention window", NOT "gets the community floor" (that set is `COMMUNITY_FRESH_INSTALL_SEED`, still 4 — both ent#433 keys are deliberately excluded, since the 5-day floor would truncate the 7-day default read window while the UI labelled it 7 days). Surfaced read-only at `GET /api/settings/retention`, which reports per-key `source` (`db-row` vs `code-default`) plus the effective (clamped) `guard.max_rows`; `cleanup_service.log_effective_retention_windows()` logs the same at boot *before* the first sweep, and a prune hitting the per-transaction chunk size logs at WARNING (a routine trickle stays INFO). Runbook: [docs/migrations/RETENTION_DEFAULTS_2026-07.md](../migrations/RETENTION_DEFAULTS_2026-07.md). **#1142 operator_queue retention** — DELETEs terminal `operator_queue` rows (acknowledged/cancelled/expired) past `operator_queue_retention_days` (default 90, `0`=off); `responded` rows use a more generous fixed floor (`OPERATOR_QUEUE_RESPONDED_MIN_RETENTION_DAYS`, 30d — they still carry an operator answer the 5s write-back loop must deliver), `pending` never deleted (`db.prune_operator_queue_terminal_items`, select-ids-then-delete, capped). This is the automatic complement to #1017's manual Clear-All (which only *hid* via `cleared_at`). `0` disables; `PRAGMA wal_checkpoint(TRUNCATE)` when any sweep reclaims rows. Also purges expired `idempotency_keys`. **There is no per-cycle row cap (#1644 — this doc previously claimed one).** `RETENTION_CHUNK_SIZE_PER_CYCLE=5000` bounds each *transaction*, and only sometimes the call. Counted at #1833 (this doc said "six of the seven", wrong in both terms — the denominator was 8, and the split 5/3; ent#433 makes it 10 and 7/3): **seven** accessors loop `while True` and drain the **entire** candidate set in one sweep — `prune_execution_logs`, `prune_execution_rows`, `cleanup_old_health_records`, `prune_agent_reports`, `prune_agent_reminders`, and ent#433's `prune_headroom_history` + `cleanup_old_rate_limit_events`. **Three** are genuinely capped per cycle: `prune_operator_queue_terminal_items` takes `limit=` directly, and the two soft-delete sweeps select their victims through `find_soft_deleted_{agents,schedules}_past_retention(limit=RETENTION_CHUNK_SIZE_PER_CYCLE)` before purging one by one. #1638 deleting 5352 rows in one startup sweep is the proof that chunking is not a cap on the first five — a real 5000 cap could not produce that number. So for those five the bound on destruction is the blast-radius guard below, not chunking; for the other three it is both, and the guard is still the one that stops at the *threshold* rather than at 5000. **#1581 volume reclaim** — at the #834 hard-purge (the instant the agent becomes unrecoverable, NOT at soft-delete) the agent's `agent-{name}-{workspace,public,shared}` Docker volumes are removed via `docker_utils.remove_agent_volumes` (double-guarded: exact name AND `trinity.agent-name`+`trinity.platform` labels must match, fail-closed; NotFound/in-use tolerated → retry). A separate bounded Docker-as-truth orphan sweep (`_sweep_orphan_agent_volumes`, ≤100 agents/cycle, 1h creation-grace) reclaims agent-data volumes whose ownership row is gone. Closes the unbounded volume leak (`volume_remove` previously had zero lifecycle callers). **Orphanhood is a DB question, not a volume-label question (#1664):** rename keeps the agent's volumes, so their name AND immutable `trinity.agent-name` label both stay at the pre-rename name — reading that as ownership marked a LIVE agent's home volume an orphan and force-removed it during any container-recreate gap (silent #1169 `data_paths` loss). Ownership is now resolved through `agent_ownership.volume_base_name` (NULL ⇒ `agent_name`; pinned to the pre-rename name in the same statement as the rename, frozen across re-renames) via `db.is_volume_base_reserved` — soft-deleted rows still match, so the recovery window is respected for a renamed agent too. The predicate is the **union of both identities** (`agent_name` OR `volume_base_name`), because a renamed agent owns volumes under **two** bases: its workspace keeps the old one, while `get_public_volume_name`/`get_shared_volume_name` name off the LIVE name, so `agent-{new}-public` appears if file-sharing is enabled after a rename (and that volume is unmounted whenever sharing is off, so the attached-check can't cover it). The union is strictly safer than the pre-#1664 `is_agent_name_reserved` it replaces — that predicate is exactly its first branch. The purge removes both bases for the same reason (deduped; one call for an un-renamed agent). **One row per base is the invariant, enforced at BOTH producers (#1671):** creation gates on `is_volume_base_reserved` (`crud.py`), and rename gates on it too — at the router (actionable 409, raised before the container is touched) and inside `db.rename_agent`'s transaction (the chokepoint that closes the check-then-write gap, #1445 pattern). Rename passes `exclude_agent=<itself>` so an agent renamed `B`→`A` (pin `B`) can still be renamed back to `B` — it already owns that base and the result has a single claimant. Leaving rename ungated let an ordinary swap (`x`→`x-old`, then `y`→`x`) mint two claimants of base `x`, which is the #1667 silent-adopt disclosure via `get_public_volume_name` (it names off the LIVE name) AND strands the volumes forever — with two claimants the purge guard skips both bases and the orphan sweep never reclaims them. Two further fail-safe gates (#1638 principle) stand between the sweep and a `volume rm -f`: a candidate mounted by ANY container is skipped (`docker_utils.list_attached_volume_names`, fail-closed — an unreadable mount table skips the whole cycle rather than reclaiming blind), and it must be observed unattached for `ORPHAN_VOLUME_UNATTACHED_STRIKES` (3 ≈ 15 min) consecutive cycles, since `recreate_container_with_updated_config` removes the old container before creating the new one and leaves a live volume briefly unattached. Agents renamed before the column existed are healed at boot from Docker's own mount table (`_heal_renamed_volume_bases`, one-shot, idempotent, runs before the startup sweep). **Startup hook (#740):** one-shot `mark_orphan_loops_interrupted()` flips `agent_loops` rows left `queued`/`running` after a restart to `interrupted` (`stop_reason="interrupted"`); no auto-resume.

### Automatic Database Backups (#2216)

Every install gets on-disk recovery points for the platform DB — both backends, zero setup, default ON. Before this, `scripts/deploy/backup-database.sh` shipped but nothing invoked it (a workstation GCP pull doing a naive live `cp`); a real instance lost its DB to a header overwrite with no recovery point. Full flow: [database-backup.md](feature-flows/database-backup.md); requirements §8.2a (`infrastructure.md`).

- **Two producers, one leaf.** `services/db_backup_service.py` (daily 03:30 UTC, backend lifespan, `db_vacuum` shape — but with NO `is_sqlite()` no-op arm) and the boot-time hook `db/backup_primitives.maybe_backup_before_migrations` (called from `init_database()` inside the `migration_lock` window BEFORE the first migration pass; SQLite only, gated on `migration_health` reporting a pending migration). The leaf is **stdlib-only and import-free of `database`/services** — it runs at import time (the `migration_lock.py` precedent). **Fail-open by contract**: fresh install → SILENT INFO skip; corrupt DB → ERROR + best-effort status row + return, and the subsequent `run_all_migrations` raises the byte-identical incident fingerprint (pinned by `tests/unit/test_2216_boot_pre_migration_backup.py`).
- **Copy primitive = the online-backup API, never `cp`.** SQLite: one-shot `sqlite3.Connection.backup()` — a single read transaction ⇒ consistent snapshot, standalone `.db`, no sidecars; both connections open AND close inside the sync worker (`asyncio.to_thread`, thread-affinity contract). The platform DB runs SQLite's default **DELETE journal mode** (verified live: `PRAGMA journal_mode` → `delete`; nothing sets `journal_mode`, and the per-cycle `wal_checkpoint(TRUNCATE)` in `cleanup_service` is a no-op today) — so the copy holds a read lock for its duration; a WARNING past 20s names the 30s busy-timeout wall. PG: `pg_dump -Fc` with the operator's `DATABASE_URL` as conninfo, **only** the driver suffix normalized and the password stripped to subprocess-env `PGPASSWORD` (query params like `sslmode` pass through — re-parsing into flags would silently break managed-PG installs); timeout → `kill()` → `await wait()` (reap) → tmp unlink; `postgresql-client-17` **major-pinned** in `docker/backend/Dockerfile` (#1823 rationale). Verify before `os.replace` (`quick_check` + `sqlite_master`; `PGDMP` magic + size) — a corrupt copy never enters the artifact namespace.
- **Retention inverts the #1638 fail-safe direction.** For rows "keep forever" is safe; for backups it fills the disk (#1871 class), while over-pruning deletes the only recovery points. Both bounds are explicit: `backup_retention_days` in the ops model (`OPS_SETTINGS_DEFAULTS` 14, `OPS_SETTINGS_VALIDATION` **1**–3650 — `0` is INVALID here; disabling backups is `DB_BACKUP_ENABLED=false`; in `RETENTION_OPS_KEYS` for the write-path protections, NOT in `COMMUNITY_FRESH_INSTALL_SEED`) and a fixed `BACKUP_MIN_KEEP = 3` floor (a constant, not a knob — #1644). **Prune runs on EVERY attempt** (success/failure/space-skip — prune-only-after-success is a disk-full Catch-22); the floor + window + pattern scope carry the safety. Free-space preflight (≥1.2× source) skips loud and **never prunes-to-make-room**. Aged `*.tmp.*` sweep covers the SIGKILL-mid-copy window. **One reader, one number:** `effective_backup_retention_days()` coerces garbage → 14 (never → 0/keep-forever); `GET /api/settings/retention` excludes the key from the generic `windows` map and the boot retention log special-cases it, so no two surfaces can disagree. The backup prune is deliberately NOT a #1644 `_guard_allows` ack-gated count sweep (`test_1771a` carve-out) — an ack-gated refusal would fail in the inverted direction.
- **Double-fire (`--workers 2`).** Day-keyed artifact names (`trinity-backup-YYYYMMDD.{db,dump}`; second worker sees today's file → skip, works with Redis down) + a fail-open SETNX lease `db_backup:running` (TTL comment-linked to `PG_DUMP_TIMEOUT_SECONDS + 300`; own-token compare-and-delete release; >80%-TTL WARNING). The lease is **duplicate-I/O suppression, never a correctness boundary** — correctness is pid-suffixed tmps + atomic `os.replace` + day-keyed names + pattern-scoped prune.
- **Observability.** Durable `system_settings` keys (`db_backup_last_status|success_at|error|path|size_bytes|duration_ms|trigger`, plus `first_attempt_at` / `last_staleness_alarm_at`) surfaced as a `backup` block on the admin `GET /api/settings/retention` (`scope: "same-disk"` — machine-readable boundary; artifacts count/bytes/newest age; `stale`). Alarms on sentinel `_db-backup` (uncreatable — `sanitize_agent_name` strips the `_`; excluded from canary L-03 via `_PLATFORM_ALARM_SENTINELS`, generalized from the single `_retention-guard` literal, service↔canary parity-tested): **edge** on ok→{`failed`,`skipped_no_space`} once per episode, **staleness** when the last success exceeds 3d, re-fired at most weekly. `db-backup-` registered in `_RESERVED_ID_PREFIXES` so an agent cannot pre-create + silence its own alarm. Context carries status/paths/sizes only (G-04).
- **Scope, stated honestly:** same-disk under `/data/backups/` — protects against corruption/slips/bad migrations, NOT disk loss. Off-site (`ArchiveStorage` seam), PG boot backup, compression, a manual backup-now endpoint, and WAL migration are flagged follow-ups.

### First-Run Provisioning — honest `setup_completed` (#2381)

`setup_completed` decides two unrelated things: whether **login is permitted at all** (`routers/auth.py::is_setup_completed` gates `POST /token`, `/auth/email/request`, `/auth/email/verify`, `/access/request`) and whether the browser shows the **first-run wizard** (`router/index.js` redirects every route to `/setup` while it is false). On a fresh install it said `false` while a real admin existed, so the unauthenticated `POST /api/setup/admin-password` — which gated only on that flag, and calls the upserting `update_user_password` — could overwrite that admin's password hash and bind a stranger's email as its sign-in identity.

**Why the flag lied.** `init_database` orders the fresh-install path `run_all_migrations` → `init_schema` → `run_all_migrations` → `_ensure_admin_user`. The `setup_completed_backfill` migration's only job is answering *does an admin already exist*, and both passes ask it while `users` is still empty — the second pass then **records it in `schema_migrations`**, so it can never answer correctly for anyone. It was written for *upgrading* installs and is structurally a no-op on a fresh one. PostgreSQL was worse: **no** Alembic revision touches the key, so PG installs simply ran with it absent forever.

**Two halves, one policy.** `utils/admin_identity` is a stdlib-only leaf holding the two facts both halves need — `admin_username()` (honours `ADMIN_USERNAME`, which `_ensure_admin_user` has always respected and two callers hardcoded around) and `is_usable_password_hash()` (not `is not None`: the column is nullable and an empty string is equally unloggable-into). They cannot share a *query* — the router runs per-request on the dialect-agnostic ORM, the boot half runs at import time on a raw sqlite3 cursor — so they share the policy, and `tests/unit/test_2381_setup_fail_closed.py::test_predicate_halves_agree` pins that they cannot drift. Drift here re-creates the bug.

- **`routers/setup.py` (the security half)** refuses whenever a usable admin exists, **above** the flag check and **above** the bcrypt hash — the route is unauthenticated and unrate-limited, so an expensive hash before the gate is both a DoS lever and a timing signal. **Fail-closed**: a DB read error refuses, because failing open would restore the vulnerability on exactly the transient conditions an attacker can retry against.
- **`database.py::_mark_setup_completed_if_provisioned{,_engine}` (the honesty half)** writes the flag when a usable admin exists. Deliberately **not a migration**: a new one would inherit the same once-only semantics, and the population that matters has already booted with the migration recorded. A boot-time reconciliation re-runs every start, so an already-exposed install converges on its next restart. It asks about the **result** rather than the action, so `_ensure_admin_user`'s create branch, its env-password re-sync branch, and the already-correct case all land in the same state. **Never raises** — `init_database` runs at import, so a raise crash-loops the backend (the `_seed_fresh_install_*` contract).

**What the wizard is now for.** It renders exactly where it has work to do: an install with **no** admin account — blank `ADMIN_PASSWORD` dev, hand-rolled — where login is blocked by the same flag and the wizard is the only way in. It disappears where `ADMIN_PASSWORD` provisioned an admin at boot: production compose (mandatory `:?`), `start.sh` (refuses blank), `--unattended` (auto-generates), hosted/marketplace images. This closes ent#49's tokenless first-run window **without reinstating the token**: ent#49 priced that tradeoff on the premise *there is no admin yet*, which is true only for the installs that still get the wizard.

**Collateral the wizard used to carry.** It was the only capture point for the admin sign-in email and for the ent#38 product-updates opt-in. The email moves to a dismissible post-login prompt (`components/onboarding/AdminEmailNudge.vue`, admin-only, `profileVerified`-gated, derivation-only so it vanishes the moment an email exists anywhere) — strictly better placed, since an unauthenticated wizard's "admin email" could be typed by whoever loaded the page first on a hosted install. The opt-in has no second home and needs one: `abilityai/trinity-enterprise#463`. Note the sibling telemetry-sharing consent (ent#12) is unaffected — it has always had its own Settings surface.

**Adjacent fix, same PR.** `docker-compose.prod.yml` never passed `ADMIN_USERNAME` (present in `docker-compose.yml` and `.env.example`), so the variable was inert in production — the #1707 packaging-gap class. Proven by `docker compose config` resolution, not grep.

### Ephemeral "Ghost" Agents (trinity-enterprise#69)

Disposable agents with a hard budget (`max_executions` and/or TTL — `ephemeral_expires_at` is ALWAYS stamped, default ceiling 24h via `ephemeral_ttl_ceiling_seconds`) that are **hard-discarded** at budget: container removed, rows purged via `cascade_delete`, Redis state cleared — no soft-delete, no 180-day name reservation, **volume-less** (container writable layer; ghosts never recreate). Every mechanic below is OSS code; creating an agent *with a budget* additionally requires the `ephemeral_agents` entitlement (registry read in `crud.py` — the registering module is private). Scoped to heterogeneous-workspace jobs; same-agent burst stays with `fan_out`/replica groups. Full flow: [ephemeral-agents.md](feature-flows/ephemeral-agents.md).

- **Creation** (`crud.py`): gates in order — entitlement (403) → ephemeral-caller refusal (chain-spawn kill, 403) → per-parent spawn rate limit (`agent_spawn:{parent}`, 429) → TTL ceiling (400) → server-suffixed name (`{base}-{hex8}`, unique-by-construction) → atomic per-owner quota (Redis INCR-with-cap `ephemeral:quota:{owner_id}`, DB fallback; `max_ephemeral_agents_per_owner` default 5, no admin exemption). Ghost defaults: `max_parallel_tasks=1` (bounds check-then-act overshoot), no workspace volume, no avatar seed, no credential auto-injection at start, git auto-sync off. Labels `trinity.ephemeral=true` + `trinity.ephemeral-expires-at` (+ `trinity.spawned-by`).
- **Ghost key fence** (`dependencies._enforce_ephemeral_key_fence`): a ghost's own agent-scoped key is confined at the single auth entry point (connector-fence pattern) to heartbeat / result callback / reports / notifications / own info — closes the agent-key→owner REST breadth (skeleton key for a prompt-injected untrusted workspace) and blocks REST chain-spawn. Keyed off the row's `is_ephemeral` (dies with the ghost); fail-open on DB error.
- **Budget enforcement**: gate at the TOP of `CapacityManager.acquire` (beside the breaker; no-enqueue invariant) — expired OR `terminal+running+queued ≥ max` raises `EphemeralBudgetExhausted` → routers map to **410 Gone**, `execute_task` to FAILED (`EPHEMERAL_EXHAUSTED`). Terminal side: `_maybe_discard_exhausted_ephemeral`, `_spawn_bg`'d post-CAS-win in `apply_result` after slot release (fail-open). `/chat` finalizes outside `apply_result` — admission-gated immediately, discard lags to GC (≤5 min, documented). Pull-mode (#1081) must re-check the predicate at the claim endpoint.
- **Hard discard** (`services/agent_service/ephemeral.py`, SETNX lock `ephemeral:discard:{name}`), crash-convergent: (0) intent marker `expires_at=now` → (1) cancel queued + CAS-fail ALL non-terminal rows (`ghost_discarded`; keeps L-03/E-01 green, defuses late-writer breaker resurrection) → (2) rm container force → (3) `clear_agent_runtime_state` BEFORE purge (#1560 ordering) → (4) `purge_ephemeral_agent_ownership` (refuses non-ephemeral rows; `schedule_executions` KEEP, admin-only visible post-purge) → (5) audit `ephemeral_discard`. `DELETE /api/agents/{name}` routes ephemeral agents here BEFORE the container lookup (half-discarded state force-discardable, never 404); MCP `delete_agent` is the discard surface.
- **GC** (`cleanup_service._sweep_ephemeral_agents`, 5-min): DB pass (expired/over-budget → discard; capped 10/cycle, 60s per-discard timeout) + Docker-as-truth orphan pass (`trinity.ephemeral` containers with no ownership row, 15-min newborn grace on `trinity.created` — creation writes the row LAST). Folds into the #429 consolidated reaper later.
- **Part 2 — spawn provenance + parent control**: ANY agent-spawned creation persists `spawned_by_agent`/`spawned_by_key_id` and auto-grants the `agent_permissions` parent→child edge (`created_by="spawn:{parent}"`) so the parent immediately chats/lists/infos its child. `enforce_agent_spawn_scope` (interim until #948): agent-scoped callers start/stop/delete ONLY children matching name AND key-id. `reject_agent_principal`: share/permissions/rename/credential ops are human-only (403 for agent keys). Fleet-wide narrowing of remaining agent-key breadth = accepted-risk follow-up.
- **Fleet hygiene**: heartbeat watch + fleet health exclude ghosts (no per-discard stale-alerts); operator-queue polling keeps them; exec/cost stats inclusive (billing truth); schedules on ghosts → 400; `AgentStatus.ephemeral` on `GET /api/agents`/`list_agents` + GHOST badge.

### Sequential Agent Loops (#740, UI #1106)

Bounded sequential task execution against one agent. Runner is an in-process `asyncio.Task` spawned by `loop_service.py`; each iteration dispatches through `task_execution_service.execute_task()` with `triggered_by="loop"` and the parent `loop_id` carried on the resulting `schedule_executions` row — iterations go through the standard `capacity_manager` admit/slot path, sharing the agent's `max_parallel_tasks` budget. Message template supports `{{run}}` and `{{previous_response}}`; `max_runs` 1–100 hard cap; optional `stop_signal` (until-mode), `delay_seconds`, `timeout_per_run`, `max_duration_seconds`, `model`, `allowed_tools`. Stop is cooperative: `POST /api/loops/{id}/stop` flips an in-process `should_stop` flag; the current iteration finishes and the runner exits with `stop_reason="user_stopped"`. **Wall-clock deadline (#1156):** optional `max_duration_seconds` (≤7 days) measured from `started_at`, checked only at iteration boundaries (before the next run and before/after the inter-run delay, which is capped to the remaining budget) — an in-flight run is never killed mid-turn, so overshoot is bounded by one `timeout_per_run`; expiry stops the loop with `stop_reason="deadline_exceeded"`. Rejected at create (400) when smaller than the effective per-run timeout (`timeout_per_run`, else the agent's `execution_timeout_seconds`). **Cost budget (#1155):** optional `max_cost_usd` (`gt=0`, no upper cap) — an iteration-boundary gate enforced *after* the deadline check: the runner accumulates each completed run's cost (only finite, positive values; NULL/unknown counts as 0 fail-open; NaN/inf ignored so it can't poison the accumulator; both unusable-cost cases WARN under an active budget) and stops *before the next run* with `stop_reason="budget_exhausted"` once accumulated cost meets/exceeds the budget. **No-progress / doom-loop detection (#1157):** optional `no_progress_threshold` (`0` disables; **default 3** for new loops via the API/MCP; NULL ⇒ disabled, so in-flight loops created before this change are unaffected). The runner fingerprints each successful run's full response — SHA-256 of normalized text (`" ".join(text.split())`, so word boundaries are preserved and whitespace-only/empty all collapse to one fingerprint) — and stops the loop with `stop_reason="no_progress"` (status `stopped`) once K consecutive runs share a fingerprint. Counter + last-fingerprint are runner-local (no persistence). Detection is **exact-hash only** (no fuzzy/semantic similarity). The validator rejects `1` (422 — "repeated identical" needs ≥2). **Boundary-only precedence** (per iteration: `user_stopped` → `deadline_exceeded` → `budget_exhausted` → run → `stop_signal_matched` → `no_progress`; natural exit `max_runs_reached`): the current run always finishes, so one run — including the first — can overshoot; a run that crosses the budget but is also the final `max_runs` run or matches `stop_signal` yields those reasons instead, and a pending `user_stopped`/`deadline_exceeded` outranks `no_progress`. `GET /api/loops/{id}` returns `max_duration_seconds` + computed `elapsed_seconds`, plus `max_cost_usd` + `total_cost` (computed on read = sum of `agent_loop_runs.cost`, NULL→0; `0.0` for a zero-run loop). Restart recovery via the cleanup-service startup hook (above); no auto-resume. WS events `loop_run_completed`/`loop_completed`.

**Failure policy (#1167):** per-loop `on_failure` — `abort` (default; fail-fast, first failed iteration ends the loop `failed`/`stop_reason=error`) or `continue` (tolerate a failed iteration and proceed). Both failure surfaces are gated: a raised exception from `execute_task` and a non-success `TaskExecutionResult`. Continue mode is bounded by `max_consecutive_failures` (default 3) — once that many iterations fail in a row the loop aborts `failed`/`stop_reason=max_consecutive_failures`; a success resets the streak. A continue-mode loop that reaches `max_runs` (or matches its stop-signal) with ≥1 tolerated failure finalizes as `completed_with_errors`, with the `failed_runs` count surfaced. `{{previous_response}}` always carries the last *successful* response (a failed iteration never overwrites it).

**Workspace (ent#458):** the same runtime, reachable from the conversation — `components/portal/PortalLoops.vue` is a collapsed strip above the composer showing each chat participant's active loops, guardrail headroom, Start (with the bounds visible first) and a Stop that is always available. **No new backend surface**: ent#458 scopes it to the platform-authenticated door (ent#78), so it calls the existing operator loop endpoints with the operator's own JWT and an external client never mounts it. Live over the EXISTING fleet-wide loop broadcast — `utils/websocket.js` routes each event to two stores, `stores/loops.js` (filtered to Agent Detail's agent) and `stores/portalLoops.js` (filtered to the chat's participants), the `reportsStore` + `fleetReportsStore` shape (#918) — with a 12s backstop poll armed only while something is active. Decidable rules (status vocabulary, headroom, strip text, form pre-flight) live in `portalLoopUtils.js`; `stop_reason` is never flattened to "Stopped", and `max_runs_reached` reads as **Done**. Loop history is deferred to ent#457's Activity tab by decision, not omission. **ent#338** closes a real bypass alongside it: `timeout_per_run` was never bounded by `agent_ownership.execution_timeout_seconds` (nothing downstream re-applies it), so a loop could run iterations longer than its owner's ceiling — now a 400 with a structured `agent_cap_seconds`, refusing rather than clamping because the bounds are on screen before Start. See [workspace-loops.md](feature-flows/workspace-loops.md).

**Web UI (#1106):** a **Loops** tab on Agent Detail (`components/LoopsPanel.vue` + agent-scoped `stores/loops.js`; `setAgent(name)` on mount, `clear()` on unmount). The global WS handler routes the fleet-wide loop events to the store, which filters by mounted agent and targeted-refreshes only the affected loop; a 12s backstop poll runs while any loop is `queued`/`running` to recover a missed terminal event. Last full response rendered via `utils/markdown.js` (DOMPurify).

### Agent Self-Reminders (#1296)

Durable one-shot deferred self-trigger — the time-deferred sibling of loops (§Sequential Agent Loops). While running, an agent schedules a **future re-invocation of itself** with a message it picks; on fire, Trinity dispatches a normal execution of that same agent through the standard `capacity_manager` admit/slot path (`triggered_by="reminder"`, shares `max_parallel_tasks`). The agent lists/cancels via 3 self-scoped MCP tools. Full flow: [agent-self-reminders.md](feature-flows/agent-self-reminders.md).

- **Storage = dedicated `agent_reminders` table** (NOT an overload of the cron `agent_schedules` table, whose every consumer assumes a NOT-NULL `cron_expression`). 5-state machine: `pending → firing → fired` (delivered), `firing → pending` (transient-failure release for retry), `firing → failed` (bounded-attempts terminal), `pending → cancelled`. Reminder executions still land in `schedule_executions` (via the standard dispatch), so Executions/Overview visibility is preserved. Three-layer backend: `routers/reminders.py` → `services/reminder_service.py` (create only: resolved-window bound + timeout clamp + pending/daily caps + provenance + relative→absolute) → `db/reminders.py` (`RemindersOperations`; tenant-scoped by-id ops, CAS cancel, retention prune). The new `triggered_by="reminder"` is wired into all three trigger constants (`_TRIGGER_BUCKETS`→first-class "Reminders" bucket, `_AUTONOMOUS_TRIGGERS`, `_VALID_TRIGGERS`).
- **Fire home = the standalone `src/scheduler/` container** (single-instance), NOT the `--workers 2` backend (an in-backend timer double-fires without a leader lock). A near-clone of the RETRY-001 one-shot machinery: `_schedule_reminder_job` arms an APScheduler `DateTrigger` per pending reminder; `_execute_reminder` fires it; `_reconcile_reminders` (fail-open, its OWN try/except independent of the cron-sync try) arms pending + reclaims stale `firing` rows — wired into `initialize()` (boot recovery, after `_recover_pending_retries`), the 60s `_sync_schedules()` loop, and `reload_schedules()` (the full-reload path). On fire the scheduler CREATES the `schedule_executions` row itself (real id, `schedule_id="__manual__"` — PG-safe, no FK) then dispatches over the existing idempotency-keyed `_call_backend_execute_task` → `POST /api/internal/execute-task`.
- **Delivery = at-least-once, bounded, observable** (AC #3). The `firing` intermediate + committed single-fire CAS (`claim_reminder_firing`, `WHERE status='pending'`) make the fire atomic while letting a fire that did NOT land be retried by the reconcile. A dispatch **TimeoutException → outcome-unknown → assume-dispatched** (`firing→fired`, execution row NOT force-FAILED — the poll finalizes it) to avoid a double-execution on the "backend slow, task ran" case; only a clean pre-start failure (non-200 503-warmup/5xx or connection error — task never started) marks the attempt FAILED (status-guarded) and retries, bounded at `MAX_REMINDER_FIRE_ATTEMPTS` (default 3) → terminal `failed`.
- **Self-only auth (AC #5)**: agent-scoped key set/list/cancel ONLY for itself — the reports self-gate (`current_user.agent_name == name`) on top of `AuthorizedAgent`; a sibling → 403. Connector keys rejected (`_reject_connector_principal` + reminders OFF the connector auth-entry allowlist — a consumption-only key must not schedule future budget-consuming executions); ghost keys 403'd (reminders deliberately OUT of the #69 ghost-key fence, since a pending reminder can outlive a discarded ghost). Tenant-scoped by-id (foreign id → uniform 404). **Abuse bounds** (env-tunable, at create): `MAX_PENDING_REMINDERS_PER_AGENT` (25→429), a **durable** `MAX_REMINDERS_PER_AGENT_PER_DAY` (100→429, the non-fail-open self-perpetuation backstop), `REMINDER_MIN_DELAY_SECONDS` (60, ≥ the reload interval), `REMINDER_MAX_DELAY_SECONDS` (30d, < the 180-day name reservation), message ≤4000 (422), a per-agent create rate-limit, and a timeout clamp to the agent cap (#929 parity). **Idempotency** (Invariant #18): caller `Idempotency-Key` wins, else a key over the RAW input (message + literal fire spec, NOT the resolved instant); terminal stored rows excluded from replay (cancel-then-recreate-identical → a fresh pending reminder).
- **Autonomy hold + cascade**: `get_active_reminders` filters `autonomy_enabled=1` AND `deleted_at IS NULL` (disabling autonomy holds pending reminders; they resume, past-due-fire, when re-enabled). `agent_reminders` is registered in `AGENT_REFS` (CASCADE) — CI-blocking (`test_agent_cleanup_parity`) — so rename re-keys and purge wipes; L-03's orphan scan covers it. **Retention**: `agent_reminders_retention_days` (default 90, `0`=off) — the cleanup sweep DELETEs terminal (`fired`/`cancelled`/`failed`) rows past the window (`pending`/`firing` never deleted), chunked, gated through the #1644 blast-radius guard; in `RETENTION_OPS_KEYS`, surfaced at `GET /api/settings/retention`.

### Resumable Turns

`claude --print --resume <uuid>` reattaches a turn to a live Claude session, preserving tool-result memory, mid-skill state, and reasoning state — strictly more than replaying prior messages as prompt text, which recovers only what was *said*. The engine is `services/session_turn_service.py` (`run_resumable_turn`) and is shared by **both** conversation surfaces; each caller owns only its own persistence:

| Caller | Rows | Identity |
|--------|------|----------|
| `routers/sessions.py` | `agent_sessions` / `agent_session_messages` | platform user |
| `client_portal/service.py` (Workspace) | `enterprise_portal_sessions` / `_messages` | verified client email |

`cached_claude_session_id` is the load-bearing field on both tables. The engine: runtime gate (drop the cached id for a runtime without `--resume`, i.e. Codex) → resume lock → `execute_task(persist_session=True, resume_session_id=…)` → **one** cold retry when Claude reports the JSONL missing. Callers pass `on_resume_failure` (clear the cache, count the failure, inside the lock) and may pass a distinct `cold_message` for that retry. `ResumeLockBusy` subclasses `HTTPException(429)` so the Session router surfaces it unchanged while the Workspace catches the precise type and re-raises `ClientPortalError(429)`.

**Workspace absorbed the Session surface (ent#358).** Agent Detail no longer renders a Session surface: `SessionPanel.vue` and the Session-mode toggle are gone, the Chat tab is stateless-only (plus a "Continue in Workspace" link), and `?tab=session` **redirects** to `/workspace?agent=<name>` (query-preserving, `router.replace`, guarded first in both `onMounted` and `onActivated` since AgentDetail is KeepAlive-cached). The removal was gated on continuity parity, not on streaming — the Session surface never streamed (synchronous POST + reattach poller, #1376/#759), so Workspace streaming (ent#286) was **not** a prerequisite. What *was* a prerequisite: Workspace chat ran stateless with a history prefix, so it moved onto this engine first. `agent_sessions` rows, endpoints and store stay readable; only the entry point went away.

**Streaming a turn (ent#286).** The Workspace could not show live tool activity for one structural reason: the client never learned an execution id, because `portal_chat` only returns once the turn is over. The agent has streamed its log all along (`GET /api/executions/{id}/stream`, live subscribe + buffered replay + `stream_end`) and the backend already proxies it for public links — so the fix is an id, early. `start_portal_turn` creates the execution row FIRST, returns `{execution_id, session_id}` as a **202**, and runs the same `portal_chat` coroutine as an in-process background task (strong-ref set, so it cannot be GC'd mid-flight). `GET /api/enterprise/client-portal/agents/{name}/executions/{id}/stream` proxies the agent SSE behind three gates: roster scope, execution-belongs-to-agent, and **execution-started-by-this-caller** (`source_user_email`) — the last is load-bearing, since executions are agent-scoped and two clients of one shared agent can otherwise reach each other's ids. Deliberately **not** the #1083 fire-and-forget path: that is `DISPATCH_ASYNC`-gated and Claude-only (so streaming would ship dark and be absent on other runtimes), and it would have forced the resume lock into a lease, split the cold retry across a callback, and moved three terminal writes. In-process keeps every one of those unchanged. `POST .../chat` stays **synchronous** — ent#83 documented it as the headless integration surface — so streaming is an additive route (`POST .../chat/stream`), and the frontend falls back to the synchronous send on any streaming failure. Since #2214 the portal turn bound is **per-agent** (`execution_timeout_seconds`, resolved once per turn via the engine's `resolve_turn_timeout` — clamp 60–7200, fail-open 3600) with the in-flight marker TTL and both client wait budgets — the 202's `wait_budget_seconds` and the history response's reattach `in_flight_wait_budget_seconds` (the marker's remaining TTL) — derived from that same resolution, replacing the flat 300s.

**History replay is cold-turn-only.** `client_portal/service.py` composes two messages: the turn message omits `_format_history_context` when resuming (the session already holds it — replaying re-pays for that context and sets a summary beside the record it summarises), while `cold_message` always keeps it and is what the engine sends on a cold retry, a first turn, or a Codex agent. Both directions are pinned by `tests/unit/test_ent358_workspace_absorbs_session.py`.

**Turn semantics** (`POST .../sessions/{id}/message`, synchronous; details in [session-tab.md](feature-flows/session-tab.md)): always passes `persist_session=True`. Resume-failure fallback: missing cached-UUID JSONL → clear cache, increment `consecutive_resume_failures`, retry once cold (reset on next success). Two Redis gates, dynamic TTL = `get_execution_timeout(agent) + 30s` capped 7230s: (1) resume lock `session_lock:{agent}:{uuid}` (`session_lock:cold:{session_id}` for cold, #779) serialises `--resume` to prevent JSONL corruption (429 on contention); (2) in-flight sentinel `session_inflight:{session_id}` drives `turn_in_progress` for UI reattach (#759).

**Access & gating:** all endpoints per-user scoped (owners cannot see other users' sessions) and return 404 — not 403 — on mismatch to avoid leaking session-id existence. All return 404 when `is_session_tab_enabled()` is false; flag `system_settings.session_tab_enabled` (or `SESSION_TAB_ENABLED` env), default ON.

**JSONL reaping** (`session_cleanup_service.py`): default 6h cycle diffs each running agent's `~/.claude/projects/-home-developer/<uuid>.jsonl` set against the keep set, deleting JSONLs outside it with mtime older than `min_age_seconds` (default 1h race guard). The keep set is the **union** of `agent_sessions.cached_claude_session_id` and `enterprise_portal_sessions.cached_claude_session_id` (ent#358) — both surfaces resume, so both are live; omitting the Workspace half deletes live session files an hour after they are written and every thread goes cold with no error anywhere, which is why a failure reading either half **aborts the sweep** instead of reaping against a partial set. Synchronous `reap_jsonl()` also fires on reset/delete. Uses `execute_command_in_container` (no agent-server endpoint). Headless-task JSONLs (timeout > 600s auto-enables persistence for the #678 stdout-race recovery in `agent_server/services/jsonl_recovery.py`) are in neither table, so they fall out of the keep set and the same sweep removes them.

### Outbound File Sharing (FILES-001)

Per-agent opt-in (`agent_ownership.file_sharing_enabled`). The agent writes to `/home/developer/public/` (Docker volume `agent-{name}-public`); on share, the backend extracts the named file via Docker SDK `get_archive` (never mounts the workspace — isolated blast radius) and stores bytes at `/data/agent-files/{file_id}`. `agent_shared_files_service.py` handles path validation, MIME blocklist, quota, extraction, URL building.

Download URL: `{public_chat_url}/api/files/{file_id}?sig={token}` — `?sig=` (NOT `?download_token=`) so the credential sanitizer's `.*TOKEN.*` pattern doesn't redact it in transcripts. Cascades manual per platform convention: agent delete removes rows + files + volume; `rename_agent()` updates `agent_name` across 17 tables. MCP tool `share_file`.

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
- **Thin WS trigger**: `/ws` is `SCOPE_ALL` and unfiltered, so the `agent_report` broadcast
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
  [workspace-deliverables.md](feature-flows/workspace-deliverables.md).
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
  [workspace-ratings.md](feature-flows/workspace-ratings.md).
- **OSS-core by decision** (strategy gate ent#206 §10): the enforcement primitive and
  the deterministic tier are edition-agnostic; the managed grading experience is the
  paid layer, mirroring #668.

See [agent-evaluations.md](feature-flows/agent-evaluations.md).

### Agent Runtime Data — `data_paths` + Snapshot/Export (#1169)

Declared runtime data (SQLite DBs, datasets) over the **existing durable home volume** — **no separate volume, no platform schema change** (snapshots are filesystem artifacts; audit rides `audit_log`). The agent home (`/home/developer`) is already a persistent named Docker volume (`agent-{name}-workspace`) that survives recreate/upgrade/template-repull/sub-switch, so data under `/home/developer/data` is already durable; this feature adds only the **declaration + export/import** surface.

**Declaration:** a template's `template.yaml data_paths:` (globs under `data/`) is surfaced by `template_service` and materialized at creation by `crud.py` → `git_service.materialize_data_paths()`: writes `~/.trinity/data-paths.yaml` (quoted-heredoc) AND appends `data/` + each path to the agent's **own** `.gitignore` (idempotent `grep -qxF`, never the fleet-wide `_GITIGNORE_PATTERNS`). Opt-in — empty list is a no-op. Shares one primitive with S4 persistent-state (`materialize_trinity_yaml_list`/`_read_trinity_yaml_list`, heredoc delimiter parameterized).

**Export** (`routers/agent_data.py`, `POST /api/agents/{name}/data/export`, owner/admin): streams `container_get_archive("/home/developer/data")` → temp file under `/data/agent-data-tmp` → `StreamingResponse` (temp removed via `BackgroundTask`); `AGENT_DATA_EXPORT_MAX_BYTES` (default 5 GiB) → 413; the tar embeds a self-describing `manifest.json`. Missing `data/` → manifest-only tar, not 500. `?format=base64` returns the tar inline as JSON up to `AGENT_DATA_INLINE_MAX_BYTES` (default 10 MiB) for MCP. Naturally-idempotent read (accepts `Idempotency-Key`; creates no execution).

**Import** (`POST /api/agents/{name}/data/import`, owner/admin): proxies the uploaded tar to the agent-server `POST /api/agent-server/restore` primitive (`restore_from_tar` enforces the `data/**` allowlist, rejects absolute/`..`); deduped via `Idempotency-Key`. Both endpoints serialized per agent by a cross-worker Redis op lock (`agent:data_op:{name}`, SETNX+TTL, fail-open, 409 on contention). MCP tools `export_agent_data`/`import_agent_data` carry the base64 tar — "move an agent" = template URL + `.credentials.enc` + data tar. System agents out of scope. **PR2 (deferred):** scheduled snapshots + `~/.trinity/pre-snapshot` quiesce hook + retention + rename/purge cascade.

### Agent Plugin Manifest — declared, committed, self-healing (#1704)

An agent's Claude Code marketplace-plugin selection is a first-class, **committed**, secret-free, self-healing piece of config — the agent-local half of the incubating global plugin-management model (trinity-enterprise#192). **Reframe:** the issue's literal premise ("a recreate loses plugins") is not reproducible — HOME (`/home/developer`) IS the durable `agent-{name}-workspace` volume, no recreate removes it, and startup.sh preserves untracked files, so a plain recreate keeps both `~/.claude.json` and the `~/.claude/plugins/` cache. The real gap is a **git-based reconstitution** onto a fresh/empty volume or a new host (the #1169 move exports `data/` only; the #834/#1581 hard-purge removes the volume), where the gitignored manifest + cache are exactly what a clone drops — a gap #1705 completed by removing the last incidental crutch (the cache used to be auto-committed).

**Declaration + materialization:** a template's `template.yaml plugins:` block (`{marketplaces: [{name, source}], installed: ["plugin@marketplace"]}`, or an `enabledPlugins:` mapping mirroring Claude's `settings.json`) is read by the **total** `services/template_plugins.py` (never raises — the ent#89 reader shape; both catalog builders surface `plugins` + `plugin_errors`) into a `_TemplateResolution.declared_plugins` carrier fed by **all three** resolver branches (github source metadata, local `template_data`, copy snapshot — the `declared_schedules` shape, deliberately NOT `template_data`, which is `{}` on the `github:` path). `crud._materialize_agent_files` → `git_service.materialize_plugins()` writes nested `~/.trinity/plugins.yaml` via the shared injection-safe heredoc writer (`_write_trinity_yaml_file`, `sort_keys=True` for byte-stability so the 15-min auto-sync loop never re-commits a churning manifest). Opt-in (empty = no-op), ghost-skipped, non-fatal inside the creation rollback fence.

**Committed — the portability divergence:** `.trinity/plugins.yaml` is in `_TRINITY_AUTHORED_PATHS`, so it rides the #2070 contents-only `!` re-include AND the `git rm --cached` exemption — COMMITTED, unlike the volume-local `persistent-state.yaml`/`data-paths.yaml`. `.claude.json` and `.claude/plugins/` stay gitignored (#1705 intact — the manifest is a distilled, plugin-only, secret-free declaration).

**Self-heal at boot:** `startup.sh` runs `python3 -m agent_server.plugins_reinstall` **after** credential injection (a private marketplace needs a git credential at install time, resolved from the agent's `GITHUB_PAT` env — never the manifest). It hardened-parses the untrusted `plugins.yaml` (`AliasPolicy.REJECT`), re-charset-validates every name and the marketplace `source` (**https-only** — rejects non-`https://` schemes (`ftp://`/`file://`/`ssh://`/…) / `user:token@` userinfo / traversal / leading `-`; a **re-implemented** (not vendored) parity of the backend `services/template_plugins._validate_source`, so the untrusted-input gate stays at least as strict as the writer — pinned by `tests/unit/test_1704_source_validator_parity.py`, since a re-implemented cross-boundary policy has no byte-for-byte guard and silently diverged once), reads current state via `claude plugin [marketplace] list --json`, and re-installs only what is missing (`marketplace add`, `install` — `--yes` feature-detected via the CLI's own `--help`, never unconditional: 2.1.227 rejects it, 2.1.235+ requires it for non-TTY command-installs, #2305) — **zero subprocesses** when the declared set is present (volume-persisting restart). **Source-mode / Cornelius fallback:** when the committed `.trinity/plugins.yaml` never materialized, `load_manifest` falls back to the re-cloned `template.yaml`'s `plugins:` block (same nested shape, BUDGET alias policy) — the startup.sh guard fires on the manifest OR a top-level `plugins:` key so the fallback is reachable. Subprocess arg-lists (no shell), hard `timeout` + `stdin=DEVNULL` (a no-TTY prompt hangs), non-fatal, one observability summary line. Base-image change → old images silently skip the hook (release-note ordering). **Deferred:** capturing runtime `/plugin install`s (a distill of Claude's own `known_marketplaces.json`/`enabledPlugins`, undocumented + version-drifting) and a commit-pinned (`auto_update: off`) mode.

**Platform-provided plugin set (ent#411).** Reading the plugin list only from a declaration is chicken-and-egg for the agent that needs it most: a bare `github:owner/repo` with no `template.yaml` declares nothing, so nothing installs, so `trinity@abilityai` — the plugin whose `/trinity:onboard` would *write* that `template.yaml` — is absent, and the only escape was a prose instruction to the agent. So the base image **pre-installs** it (`docker/base-image/Dockerfile`, `ARG TRINITY_PREINSTALL_PLUGINS=1`, never fatal — an unreachable marketplace at build time logs and continues) and `plugins_reinstall` **ensures it every boot regardless of what is declared**. Docker populates an empty named volume from the image on first mount, so a new agent inherits the pre-install and boots with **zero subprocesses**; an agent whose volume predates the image self-heals through the hook. Properties: **additive, never subtractive** (a `plugins:` block that omits it does not uninstall it — nothing here ever uninstalls); the platform marketplace **name is pinned to its source** (the manifest is agent-writable, so a redefinable `abilityai` would be an arbitrary-code-fetch primitive); operator opt-out `TRINITY_PLATFORM_PLUGINS=0` (status `no_manifest`, distinct from a failure). Each reconcile is recorded to `~/.trinity/plugins-state.json` (`status`, `platform_defaults_enabled`, installed/skipped/withheld) — the honest-status surface read by compatibility check **I-006**, which reports installed / withheld-with-reason / switched-off, because "the marketplace was unreachable" and "it was never wanted" are different facts a bare presence flag cannot separate. The file is agent-writable, so I-006 cross-checks the claim against the recorded lists rather than trusting a free-text status. Same base-image ordering caveat as the hook: an old image lacks the pre-install and pays one install at next boot. Enables *deploy-as-is → onboard-in-place* ([TRINITY_COMPATIBLE_AGENT_GUIDE.md](../TRINITY_COMPATIBLE_AGENT_GUIDE.md)).

### Git Sync Health (#389/#390)

**Agent side:** 15-min `auto_sync` heartbeat loop in the agent server (gated by `GIT_SYNC_AUTO`; default-on for non-source-mode GitHub-template agents) stages/commits/pushes in-container changes and writes the outcome to `.trinity/sync-state.json` (S1a; atomic tmp+`os.replace` write). **Maintenance ownership (#1595):** git's own auto-gc can NEVER complete in an agent container — it detaches to PID 1 and the #817 orphan sweep SIGKILLs it every time (44 GB / 97%-garbage repos observed) — so the base image disables it (`gc.auto=0`, `gc.autoDetach=false`, `maintenance.auto=false` in `/etc/gitconfig`; also in `git_service` setup for older images) and the auto-sync loop is the **single maintenance owner** for the agent's home repo (sub-repos cloned into the workspace get no maintenance — documented blind spot). The cycle runs in a worker thread (`asyncio.to_thread` — a long repack no longer starves `/health`/heartbeats), every subprocess is sweep-registered via `utils/registered_run.py` (`ProcessRegistry.add_transient_pid`, process-group timeout kill), and a non-blocking repo `threading.Lock` serializes it against the mutating git endpoints (`sync`/`pull`/reset → 409 `agent_busy` on contention; busy cycle skips). Each cycle reaps stale lock litter age-gated (`gc.pid`/`index.lock` >1h, `tmp_pack_*` > repack budget; `startup.sh` reaps unconditionally at container start — the stale-`index.lock` 12-day-freeze class). **Bloat controls (#1596/#1595):** after a successful push the loop runs a consolidating **repack** (`git repack -A -d -l --unpack-unreachable=1.hour.ago` + `git gc --prune=1.hour.ago` — 1h grace so concurrent Claude-run git ops are never corrupted; `pack.threads=1` + `pack.windowMemory=128m` bound RSS) when packs ≥ `GIT_MAINTENANCE_PACK_THRESHOLD` (20) OR loose objects ≥ `GIT_MAINTENANCE_LOOSE_THRESHOLD` (6700 — with auto-gc off, garbage accumulates loose); guarded by a free-disk preflight (skip < 1.1× pack bytes), an exponential failure backoff (1h→24h, persisted in sync-state), and an env-tunable budget (`GIT_MAINTENANCE_TIMEOUT_SECONDS`, default 1800, read at call time). Non-destructive: does NOT rewrite reachable history. It measures `.git` size (`du -sb`) plus `git count-objects -v` pack/loose counts into sync-state on **every** terminal path (failing repos must not go dark). The fleet-wide default `.gitignore` (`git_service._GITIGNORE_PATTERNS`, **merged into each agent on sync and at creation** — #2069, readiness-gated) also excludes bulk data/deps/caches (`node_modules/`, `.venv/`, `__pycache__/`, `*.sqlite`/`*.db`, …) so churny non-source files aren't auto-committed. **Creation-time seed (#2069):** the list was applied only on Push/init, never at creation, so an agent whose 15-min auto-sync loop is on from birth (the `GIT_SYNC_AUTO` set — non-source/fork `github:` agents, ephemeral ghosts included) auto-committed `.trinity/` state + root `.env`/`.mcp.json` into its user-owned repo before any Push could migrate the list. `git_service.spawn_gitignore_merge_after_clone` fires a fire-and-forget, agent-`/health`-readiness-gated (server launches at `startup.sh:517` after ALL git setup, ~900s before the first cycle) merge — reusing `_build_gitignore_merge_command` (no fourth pattern list), merge-only (PREVENT: stops `git add -A` staging the untracked generated creds), gated on the shared `_git_auto_sync_baked` ENV predicate, idempotent (#953). Wired at creation (`_materialize_agent_files`) and at start/recreate (`start_agent_internal`, gated on the DB `auto_sync_enabled` flag — fleet remediation for existing leakers). **`.trinity/` is ignored contents-only (#2070):** `.trinity/*` plus a `!` re-include per path in `_TRINITY_AUTHORED_PATHS` (`pre-check`, `post-check`, `pre-snapshot`, `setup.sh`, `persistent-processes.allow`, `brain-orb/`, `pipelines/`, `plugins.yaml` #1704) — the files a TEMPLATE commits and the platform reads back. The wholesale `.trinity/` exclusion made every Push's `git rm --cached` untrack them and push the deletion, guarded only by a hardcoded two-entry pathspec that was wrong three times (brain-orb ent#76, `setup.sh`, then `pre-check` #2070); the symptom is silent because only the index is touched, surfacing one re-clone/recreate later as an agent with no hook. Now the default under `.trinity/` is *ignored*, so a new runtime file needs no action, while authored content is tracked by construction — and the `rm --cached` exemptions are DERIVED from the same constant so the two cannot drift. The merge also **removes** the superseded exact line `.trinity/` (git never descends into a dir-form exclusion, so the re-includes under it would be inert on every pre-#2070 agent). Maintenance bounds *garbage*, not *history* — unbounded history growth remains the deferred opt-in squash / geometric-repack follow-up.

**Backend side** (details in [git-sync-health.md](feature-flows/git-sync-health.md)): `SyncHealthService` polls git-enabled agents every 60s, upserts `agent_sync_state` (`consecutive_failures` ++ on fail / reset on success; `ahead_working`/`behind_working` expose working-branch divergence, P6; `git_dir_bytes` #1596 + `pack_count`/`loose_objects`/`maintenance_failures` #1595 carry the repo-health curve — all agent-supplied ints coerced at the boundary, the `/health clone_status` convention), emits `sync_failing` operator-queue entries at ≥3 failures (S1) and edge-triggered `git_bloat` entries when `git_dir_bytes` crosses `GIT_DIR_ALERT_BYTES` (default 10 GiB) or maintenance fails 3× running (#1595 — the failure class was previously silent until the disk filled). Powers the dashboard sync dot, `GET /api/agents/sync-health` (now incl. `git_dir_bytes`), and `GET /api/fleet/sync-audit` — whose `duplicate_binding` flag marks agents sharing a `(github_repo, working_branch)` pair (§P5 silent-clobber setup) (S6, #390).

### Post-Creation Repo Binding (ent#109)

"Bind this agent to a GitHub repo you own" — the ownership retrofit ent#123 left open. A tokenless public-template agent (the default Cornelius) accumulates a knowledge base it cannot push anywhere, and the only documented escape was "create a new agent with fork-to-own and import your data", which discards the agent's identity, its 180-day name reservation and its history. `POST /api/agents/{name}/git/bind-to-own-repo` creates a user-owned repo from the agent's **current workspace volume** (not from its template — that is the reason it cannot share ent#93's copy step), repoints `origin` in place, and re-bakes the container env. It is a **rebind, not a fork verb**: an already-writable agent is an ordinary rebind, which is what makes AC #3 ("works for any agent") literally true. Owner-only **and** human-only (`reject_agent_principal` — an agent-scoped key resolves to its owner *carrying the owner's role*, so a role gate alone is satisfied by any agent's injected key on a default admin-owned install). Requirements §11.12; flow [agent-repo-binding.md](feature-flows/agent-repo-binding.md).

- **Orchestration in `services/agent_service/repo_binding.py`, not the router** (Invariant #1): it raises `BindError` and never `HTTPException`, mapped 1:1 at the thin router (the `chat_execution_service` #1483 shape). The router owns only the two locks, the idempotency claim, and the audit row.
- **Classification partitions on `source_mode`** — the column `idx_git_config_repo_branch_unique` actually keys on (`WHERE source_mode = 0`) — **not** on write-credential state, which is an *orthogonal* column: a credential-less `source_mode = 0` row would pass a credentials gate and be rebound *within* the index. Everything unsupported is refused **by name**: no row → 400 `BIND_NO_GIT_CONFIG` (also how `local:` agents and the `is_system` `trinity-system` are refused, so neither reaches the recreate that bypasses #1816's running-system gate); `source_mode = 0` → 409 `BIND_WORKING_BRANCH_MODE_UNSUPPORTED`; container `.git` unreadable or `origin` ≠ the row → 409 `BIND_STATE_UNCLASSIFIED` reporting both observed values. `source_mode` stays **1** and `working_branch` is untouched — no branch re-reservation.
- **Concurrency: destination-scoped lock + CAS + compensating restore.** `agent:bind_dest:{sha256(lower(dest))}` is the lock that serializes the actual collision (two *different* agents, one destination repo); `agent:bind_op:{name}` only guards double-submit. Both **fail CLOSED (503 + `Retry-After`)** — `_agent_data_op_lock`'s fail-open is calibrated for a tar round-trip, whereas a lost lock here means two repo creates, two CAS writes and two concurrent recreates of one container. The commit point is a single CAS (`db.rebind_git_config`, predicate `agent_name = :a AND github_repo = :expected_old`, named in its docstring); rowcount 0 → 409 `BIND_CONCURRENT_MODIFICATION` with nothing partial. The ent#93 post-write re-check is kept as a **belt**, and its loser path is a compensating `UPDATE` restoring captured previous values — **never `delete_git_config`**, which on a *pre-existing* row is destruction: it strips a live agent's binding so the next recreate drops `GITHUB_REPO` (#843/#1439).
- **The PAT is persisted LAST and strictly before the recreate.** Earlier makes `_agent_has_write_credentials` report the agent already-writable on a retry (the 409-on-retry contradiction) and lets a mid-window manual Push hit the OLD repo with the NEW token; later would bake a repo-bound container with no token, because the config-drift recreate resolves the PAT with `pat_gate="per_agent_only"` (see `_apply_git_env_from_db`) and `startup.sh`'s `configure_push_remote` would then blackhole its push remote.
- **A recreate is mandatory**, not cosmetic: `startup.sh`'s restart branch rewrites `origin` **unconditionally** from the baked `GITHUB_REPO`, and the workspace-`.env` fallback covers `GITHUB_PAT` only — so a DB-only rebind is silently reverted by the next plain restart. It is **not** a re-provision: the same volumes are reused via the `volume_base_name` pin (#1664), `agent_ownership` and the 180-day reservation are untouched, and the S4 persistent-state allowlist is therefore preserved *by construction* (the volume is never detached).
- **Resumption is an explicit branch, not assumed idempotence.** After the CAS the row names the destination while the container's `origin` still names the old repo, and both pre-flight gates read that skew as a refusal (`BIND_STATE_UNCLASSIFIED`; and on a later retry `BIND_DESTINATION_EXISTS`, because the destination now holds the agent's own pushed history) — so every post-commit message promised a retry that returned 409. A row already naming the requested destination relaxes both: `origin` never selects what is pushed (step 4 pushes by explicit URL, writes `origin` after), and the push carries no `--force`/`+` refspec so unrelated history is rejected non-fast-forward. `previous_repo=None` on a resume leaves `upstream` alone rather than repointing it at the destination itself. A mismatch against any *other* repo stays unclassified.
- **The recreate clears name-keyed breaker state first** (#1560): this is `recreate_container_with_updated_config`'s **second** production call site, and both breakers are agent-name-keyed with no TTL, so the replacement container would otherwise inherit its predecessor's verdict. `clear_agent_breakers` runs *before* the recreate (after would reset a breaker the fresh container legitimately tripped); slots are untouched (`force_clear_slots` would drop capacity accounting for an in-flight execution).
- **The user PAT is header-validated at the model** (`models._validate_pat_secret`, both `BindAgentRepoRequest` and `ForkToOwnRequest`): h11 rejects an illegal header value by **echoing** it, so a token with a trailing `\r`/`\n` — the routine paste artifact — would surface raw in a 500 body and the platform log. Whitespace is stripped, not rejected. Paired with `error_handlers.validation_error_without_input`, which strips Pydantic's `input` from **every** 422 entry — without it the guard would merely move the leak from the 500 into the 422.
- **No new drift predicate.** Decision #17's `check_github_repo_env_matches` was cut: the only drift-proof way to write it is to call `_apply_git_env_from_db` itself, which turns PR 1's AST writer-set guard (`tests/unit/test_ent109_git_env_seam.py`) red, and an independent re-implementation is exactly the writer/checker feedback loop `lifecycle.py` documents. Idempotent retry supplies the convergence instead, and `BIND_RECREATE_FAILED` says so — including a warning **against** a plain restart, which would re-run `startup.sh` and undo the rebind.
- **Shared destination primitive** (AC #4): `fork_to_own.inspect_or_create_destination_repo()` returns `created | empty | branches` and never decides; reuse/refuse **policy** stays in each caller, because the create path's reuse branch *is* its template-tip SHA comparison and the rebind has no template to compare against. `validate_destination_pat` is a sibling rather than folded in, preserving the create path's validate-before-resolve-template ordering.
- **Secret hygiene**: `SecretStr` unwrapped once at the router; every message built from foreign text passes `scrub_secret` **and** `redact_url_userinfo` (git stderr can embed a *stale baked* token that is not the request's PAT). **No MCP tool** — it would push a user PAT through the MCP layer.

**Recovery (S3, #384):** `POST /api/agents/{name}/git/reset-to-main-preserve-state` adopts `origin/main`, snapshots the S4 persistent-state allowlist first, overlays it back, force-with-lease pushes — safe recovery for parallel-history deadlock (P2/P3). 409 with `X-Conflict-Type: agent_busy | no_git_config | no_remote_main | no_write_credentials` (the last: tokenless ent#123 agents — the recovery ends in a push). Per-agent toggles: auto-sync flag and freeze-schedules-if-sync-failing flag (see API Endpoints).

### VoIP Telephony (VOIP-001, #1056)

Outbound phone calls from agents via Twilio Media Streams + Gemini Live (details in [voip-telephony.md](feature-flows/voip-telephony.md)). Feature-flag gated: `voip_available = VOIP_ENABLED && bool(GEMINI_API_KEY)`, default OFF; also requires a per-agent `voip_bindings` row (Twilio-voice creds, validated via Twilio Account fetch, AuthToken AES-256-GCM encrypted). A voice transport, NOT a text `ChannelAdapter`.

**Call flow:** MCP tool `call_user` → `POST /api/agents/{name}/voip/call` → `voip_service.py`: gate checks (flag/binding) + abuse controls (rate limit per `(owner, destination)`, durable per-agent daily cap), stages a Gemini session intent in Redis keyed by `call_id` (distinct from the `vs_` VoiceSession id), mints a call-bound WSS ticket, calls Twilio `calls.create(<Connect><Stream>)`. Never calls `connect_and_stream` itself (cross-worker safety — the WS handler does). Optional `Idempotency-Key` honored (Invariant #18).

**Media bridge** (`transports/twilio_media_stream.py`, WS `/api/voip/voice/{call_id}`): `accept()`-then-authenticate — Twilio does NOT forward the `<Stream url>` query string, so the call-bound ticket arrives as `start.customParameters.ticket` in the first `start` frame, read after handshake (#1073); `?ticket=` fallback for non-Twilio clients. Then scope check (`voip:{call_id}`), `GETDEL` staged intent (consume-once), create the Gemini `VoiceSession` on the connecting worker, run the unmodified `connect_and_stream`. Per-connection `_CallBridge`: inbound μ-law→PCM resample, outbound paced 20ms 160-byte μ-law sender, `clear`-on-barge-in, `streamSid` capture; teardown ties Gemini-end→Twilio-close + SETNX-guarded single transcript save + post-call dispatch. Codec helpers in `transports/voip_audio.py` (stdlib `audioop`, per-direction `ratecv` state for anti-click; `audioop-lts` pinned for Python ≥ 3.13).

**Post-call:** transcript persisted to `chat_messages` (`source="voice"`) and dispatched to the main agent via `task_execution_service.execute_task(triggered_by="voip")` (default ON). Phase 2 column `inbound_number` reserved in `voip_bindings`.

### Canary Invariant Harness (CANARY-001, #411)

Continuous orchestration-invariant watcher. Deterministic library (`src/backend/canary/`) shared between the 5-min watcher service and the on-demand admin endpoint — the library reads state (Redis × the configured SQL backend, SQLite OR PostgreSQL via `DATABASE_URL`, #300/#1093 × agent registries) but writes nothing; the service persists violations to `canary_violations` and classifies green→red transitions. All SQL-tier collector reads route through the `get_engine()`/`DATABASE_URL` seam (#1540) so the harness is backend-consistent on PostgreSQL — previously the raw-sqlite collectors read a stale `/data/trinity.db` on PG and the SQL checks went vacuously green. No LLM reasoning anywhere — the canary's value depends on determinism. Disabled by default; `CANARY_ENABLED=1` on staging/dev. **Alert sink:** one Slack Block Kit webhook POST per green→red transition (`CANARY_SLACK_WEBHOOK_URL` env; unset = silent sink — cycles still run, violations persist; continuing-red doesn't re-post **except to complete an undelivered alert**). **Delivery is honoured, not assumed (#1897):** `emit_transition` returns DELIVERED/SKIPPED/FAILED, only a non-FAILED outcome counts the transition (`cumulative_transitions`) or lists it under the run-cycle response's `transitions` — the undelivered set rides beside it as `undelivered_invariant_ids`, so a webhook outage cannot read as a green cycle — and a FAILED one is re-attempted on a later cycle *while the invariant is still red*, at most once per interval, for up to `MAX_ALERT_PENDING_AGE_SECONDS` (1800s) per contiguous failure run — the budget is checked *after* each attempt (so the ERROR quotes the elapsed and error of the attempt that just failed), which means a run ends on the first attempt whose age *exceeds* the window: **8 POSTs over 35 min** at the 5-min default, not 6 over 30; and the dual of the run-decay is that an invariant flapping on a period longer than 3× the interval re-anchors every episode, so it never reaches a give-up — but it never retries either (green before the floor opens), costing one POST per detected flip exactly as before #1897, because a delivery budget does not bound its own detector — then dropped with a distinct ERROR naming the invariant, the elapsed seconds and the last webhook error (`cumulative_alerts_dropped`; `cumulative_transitions_detected` keeps the detection count, so one counter never means two things). Per-invariant retry state is the Redis hash `canary:alert_pending`, **armed before the POST and `HDEL`'d on success** — `asyncio.CancelledError` is not an `Exception` and `stop()` cancels a live cycle, so arm-on-failure loses the alert on every deploy that lands inside the 5s webhook await — and deliberately NOT the cycle-global `canary:last_cycle_at` cursor, whose withholding retries nothing (the invariant's own freshly-inserted row already post-dates it) while silently swallowing an unrelated red→green→red flip; both pending reads/writes fail open to exactly the pre-#1897 behaviour, and the evidence never leaves SQL. Guard: `tests/unit/test_1897_canary_alert_delivery.py`. Every registry invariant must carry **all four** per-invariant surfaces in `services/canary_alerts.py` (name, runbook, `_render_message` branch, `_render_forensic` branch) or its alert degrades to a bare-id fallback; CI-guarded bidirectionally by `tests/unit/test_1880_canary_alert_parity.py` (#1880). Names come from each invariant module's docstring title — **not** the catalog, whose ids are not the registry's. Both render fallbacks are deliberately state-free so an un-rendered invariant cannot echo `observed_state` (E-04/G-04 scrub at the check, reporting a reason code / pattern name only). **Instance attribution (#1987):** a webhook carries no sender identity, so with more than one instance posting to a channel an alert names *what* fired and never *where* — and since continuing-red doesn't re-post, what the one-shot omits is unrecoverable. `services/instance_identity.py::get_instance_label()` resolves a short label — `TRINITY_INSTANCE_NAME` override → first DNS label of `FRONTEND_URL`'s host (`https://eu2.abilityai.dev` → `eu2`; an IP literal keeps its full host, since the first label of `10.0.0.5` is `10`) → `installation_id[:8]` → `None` — which `_build_slack_payload` renders as a `[eu2]` prefix on **both** the header and the `text` fallback (the fallback is what a mobile push shows). Tier 2 is why attribution improves fleet-wide with no `.env` rollout: managed instances already set `FRONTEND_URL` and both composes already forward it. Every tier degrades rather than raising — an unlabelled alert is today's behaviour, a lost one is the failure the sink exists to prevent — and the resolver is a stdlib-only leaf so the operator-queue / retention-guard alarms can reuse it if either grows a webhook. Sanitization (ASCII-alnum + hostname punctuation, 32 chars) runs at the render boundary as well as at resolution, per `_mrkdwn_safe`'s own argument: `<!channel>` mass-pings the channel and an over-long `header` is a 400 that drops the whole message. **Operational:** a rendered alert carries agent names, execution/schedule ids, and — for G-04 — which agent and row hold a credential-shaped value and that it is stored plaintext. No secret *value* is ever sent, but that is a precise pointer, and it reaches the `text` fallback too (mobile push, connected integrations). Channel membership behind `CANARY_SLACK_WEBHOOK_URL` is not tied to the Trinity admin role, so **point it at a restricted channel** — before #1880 the same information required an admin query against `canary_violations`. **Fleet:** `config/canary-fleet.yaml` deploys synthetic load generators (`canary-fleet-burst`, `canary-fleet-long`) via the systems-deploy API — without traffic the checks are trivially green.

**One cycle per fleet, not per worker (#1881).** The harness shipped with two chained defects that had to be fixed together, because fixing either alone makes things worse. (1) *(part 1, shipped as #1876)* `CANARY_ENABLED` / `CANARY_SLACK_WEBHOOK_URL` were wired into `docker-compose.yml` only, and prod compose launches **standalone** (no base merge, no `env_file:`) — so on the deployment the harness is documented for (staging/dev runs prod compose) the knob was inert and the watcher could not be turned on at all: the #1039/#1056 packaging-gap class, and a silent-green one level above H-01, which no invariant can catch because invariants only run inside the thing that isn't running. Pinned by `tests/unit/test_canary_env_prod_parity.py`. (2) The lifespan starts `canary_service` in **every** uvicorn worker (`--workers 2`) and the service held only a per-process `asyncio.Lock`, so turning (1) on meant two full cycles per interval: R-01 `docker exec`ing into every running agent container twice per 5 min, every violation double-persisted (11,942 rows in 24h measured on eu2), and two independent writers on `canary:last_cycle_at`, `canary:last_cycle_red`, `canary:e02:terminal_seen` and `canary:h01:suspect_since`. The scheduled loop now takes a `canary:leader` lease — SET NX, TTL `max(3×interval, 900s)`, own-lease-only refresh, best-effort release on `stop()` — mirroring #1464/#1632. The TTL **floor** is the one deviation from `interval × 3` and is deliberate: a canary cycle's cost is dominated by R-01's `container.exec_run` sweep, which is bounded by no timeout and scales with *fleet size*, not with how often we look, so a shortened interval must not shorten the lease below one sweep. **Fail-open to leader** when Redis is unreachable — and unlike the precedents the duplicate is *not* inert here (it re-runs the sweep and double-persists), so the reasoning is explicitly different: this is the subsystem whose whole job is noticing that something went quiet, so a noisy-and-visible failure beats a silent one every time; a Redis outage is also already a state the harness is built to announce (`sources_unavailable`, H-01 firing unconfirmed on an unreadable marker), and failing closed would suppress exactly those paths. Consequently the lease is **best-effort, not mutual exclusion**, and H-01's `CONFIRMATION_MIN_SECONDS` / R-01's `DWELL_SECONDS` elapsed-wall-clock gates stay load-bearing — they must not be relaxed to "seen in a second cycle" on the strength of it (both also ride out a real-time transient, which is a single-worker property). One knock-on: a leader failover leaves up to ~1200s (TTL + interval) with nobody cycling, which exceeds R-01's `_MAX_OBSERVATION_GAP_SECONDS` (600) and restarts its dwell — correct, since a crashed leader is a genuine observation outage, and restarting is the fail-safe direction. `run_cycle()` is **not** gated: `POST /api/canary/run-cycle` lands on an arbitrary worker, so gating it would make an explicit admin request return an empty payload ~half the time — indistinguishable from a green cycle, the exact ambiguity the 409 contract removes. Non-leaders log on the leadership **transition** only, never per cycle. Guard: `tests/unit/test_1881_canary_leader_lease.py`.

**Run-state observability (#2217).** A disabled canary emits zero violations — byte-for-byte identical to a clean fleet — so nothing reported whether the harness was even running. This is the H-01 class **one level up, applied to the detector itself**: H-01 catches a blind collector *while a cycle runs*; it structurally cannot catch "no cycle is running at all" (a dead loop emits nothing). `GET /api/canary/status` (admin-only) → `CanaryService.get_run_status()` (Invariant #1) closes that disjoint gap: `{enabled, status, last_cycle_at, seconds_since_last_cycle, interval_seconds, stale_after_seconds, alert_sink_configured, redis_available}`. `status` is `disabled|healthy|stale|unknown` — all three non-healthy states distinct from enabled+fresh+zero-violations. It reports the **shared** `canary:last_cycle_at` cursor (NOT the per-worker `self.last_run_at`/`cumulative_cycles`, which are stale/zero on a non-leader under #1881's lease). Note the cursor is written at cycle END carrying `snapshot.snapshot_time` — the collection-**start** instant — so it lags real completion by up to one cycle's duration; `/status` reports that instant and derives staleness from its age. **Fail-open, so default-OFF never alarms:** `disabled` short-circuits before Redis is read (`redis_available=None`); any Redis/parse failure is `unknown` (`redis_available=False` on a raised read, `True` on a clean missing-cursor) — never `stale`. The threshold `stale_after_seconds = _max_failover_seconds() + _MAX_CYCLE_LEASE_SECONDS` (≈1680s) is provably above BOTH the ~780s failover window AND a maxed-but-healthy ≤900s cycle whose collection-start-instant cursor age reaches `interval + cycle_duration`; both terms are the file's own constants so the bound cannot drift out of step with the timing it guards. `alert_sink_configured` (whether `CANARY_SLACK_WEBHOOK_URL` is set) is a **separate** field, never folded into `status` — a cycling canary with no sink persists violations but pushes nothing (a silent-green that must not read as unqualified `healthy`), and liveness vs can-it-alert are orthogonal. A boolean `canary_enabled` also rides `GET /api/settings/feature-flags` (any authed user, the observability-only flag home beside `mcp_agent_chat_pull_enabled`/`redelivery_governor_enabled`; boolean only — detail stays admin-only), backed by the public `CanaryService.is_enabled()`. A manual `POST /api/canary/run-cycle` **also** advances the cursor, so `/status` is a last-cycle probe across scheduled AND on-demand cycles, not a scheduled-loop-liveness probe. Backend-agnostic since #1540 — safe to keep `CANARY_ENABLED=1` on PostgreSQL. An **active push** liveness alarm (an always-on process pushing on `enabled && stale`) is a deferred follow-up — it needs its own default-OFF gating so it never alarms an install that never opted in. Guard: `tests/unit/test_2217_canary_status.py`.

Lookup keys: S-01/E-02/L-03 shipped via #653; S-02/E-01/E-05/B-01 (Phase 2) and S-03/B-02/R-01 (Phase 3) via #882; E-03/G-03/E-04/G-04 (Phase 4) via #1077 (E-04/G-04 stacked on #1450's queued-read rework); H-01 (Phase 5) via #1813.

**`H-` is the harness-health family (#1813)** — the only invariants whose violation means *the observer is blind*, not *the observed system is broken*. Filed apart from `G-` (global/cross-cutting) on purpose: a detector outage triaged as a platform defect sends the on-call after the wrong bug, and an H-01 violation invalidates every other green in that cycle.

| ID | Tier | Severity | Invariant (bug class guarded) |
|----|------|----------|-------------------------------|
| S-01 | A | major | Slot–row bijection: per agent, execution_ids in `agent:slots:{name}` (drain sentinels filtered) ≡ execution_ids of `running` rows (PR #378/#403 class). Severity `critical`→`major` (#1082): redundant under #1082 single-owner status — the slot ZSET is no longer a competing authority — and retires with the slot ZSET in #1081 Phase 5 |
| S-02 | A | critical | No overbooking: `ZCARD(agent:slots:{name})` ≤ `max_parallel_tasks` — distinct from S-01 because Redis and SQL can agree on N+1 (`acquire_slot` concurrency bypass) |
| S-03 | A | critical | Slot TTL floor: every `agent:slot:{name}:{eid}` HASH created with ≥ **that slot's own** `timeout_seconds + 300s` TTL. Kinds: `missing` (-2, HASH expired ahead of ZSET — #226 class), `no_expiry` (-1), `below_floor`. Decay-invariant (#913): reconstructs *initial* TTL as `ttl + age` (age = **the instant that slot's TTL was read** − ZSET score, the ZADD epoch) vs `floor − 1` (1s wire-rounding tolerance), so natural decay never fires. **Both terms must come from one instant (ent#372):** `age` was measured against `snapshot_time`, stamped at the top of `collect_snapshot()` — but the per-slot TTL pipeline runs after the docker and roster collectors (1.0–1.9s on eu2, tail to 9s), and that elapsed time came off `ttl` without going into `age`, so the reconstruction landed short by exactly the collector's cycle work and S-03 paged `below_floor` on **every live slot, every cycle** (208 critical violations / 85 cycles / 24h). `_collect_redis_slot_state` now stamps `AgentSnapshot.slot_ttl_read_at[eid]` immediately before each slot's pipeline — **per slot, not per collector**, because the loop's own elapsed time grows with the fleet and a collector-level stamp would re-open the identical gap at scale; taken *before* `execute()`, so `age` can only be under-counted by one RTT (the conservative direction — it can never manufacture a green) well inside the 1s tolerance. Widening the tolerance was rejected (collector elapsed time is unbounded), as were re-stamping `snapshot_time` (other invariants measure against it) and reordering the collectors (#1813 puts docker first so H-01 has independent evidence on the roster-failure path). A TTL with no read time is unreachable at runtime (both are written in one try-block) and **skips** the floor arm rather than falling back to `snapshot_time` — the fallback is the bug. **The floor is the slot's stored timeout, NOT `agent_ownership.execution_timeout_seconds` (ent#336)** — `acquire_slot` sets the TTL from *this execution's* timeout, which since #929 is legitimately below the cap (a schedule's explicit shorter `timeout_seconds`, a 900s public turn, a loop's `timeout_per_run`), so the agent-cap floor paged critical on every normally-configured scheduled run (378 in 13h on eu2). Mirrors `SlotService._cleanup_stale_slots_for_agent`, which already read the field back for the same reason (#869). An unobservable `timeout_seconds` **skips** the slot rather than falling back to the cap (the fallback re-arms the same false positive in a narrower window); `-1`/`-2` are floor-independent and still fire. **Honest scope:** `acquire_slot` derives the EXPIRE and the HSET from one local three lines apart and is the sole writer, so `below_floor` is now an internal-coherence check — it does NOT catch a caller passing the wrong timeout (the #913 class), which needs corroboration against the *declared* timeout (ent#336 residual). A green S-03 is not evidence that dispatch timeouts are correct |
| E-01 | B | critical | Terminal-state closure: no `running` row older than `execution_timeout_seconds + 300s` (matches `SLOT_TTL_BUFFER`, so it fires after cleanup's window). **#1081 pull-CLAIMED rows (`lease_expires_at IS NOT NULL`) get a BOUNDED grace, not an exclusion (#1990)** — a claimed row is `running` but owned exclusively by the lease-reaper, which re-queues (`redelivery_count` ++, `started_at` reset) or poison-parks it at `MAX_REDELIVERY`, so its age is not evidence of a stuck execution. The overlap was exact, not merely awkward: `claim_next_queued` stamps `lease_expires_at = started_at + (execution_timeout_seconds + SLOT_TTL_BUFFER)` — the identical window — so E-01 fired at the *instant* the reaper's recovery window opened, with zero head-room, on every re-delivery. The db layer already encoded the split on the very sweep whose failure E-01 detects (`mark_stale_executions_failed` carries `lease_expires_at IS NULL`, as do `get_running_executions`, `get_running_executions_with_agent_info`, `fail_stale_slot_execution`, `mark_no_session_executions_failed`); the canary was the layer that hadn't caught up. Mirrors S-01 and E-05 (#1982). **The silence is time-bounded**: the row is skipped only while its lease is overdue by ≤ `LEASE_REAPER_GRACE_SECONDS` = **600s** = 2 × `cleanup_service.CLEANUP_INTERVAL_SECONDS` (a healthy reaper resolves an overdue lease within one 300s cycle — `requeue_expired_lease` clears the lease and resets `started_at` in one atomic UPDATE — so one interval is the worst case and the second is head-room). Past it the row FIRES as a **lease-reaper failure**, reported with `lease_expires_at` / `lease_overdue_seconds` in `observed_state` and split in the Slack runbook hint, because it is a different diagnosis than a NULL-lease violation (`cleanup_service`'s watchdog). That makes E-01 the automated owner of `PULL_MIGRATION_TESTING.md` §9 **M4**, a #1766 abort criterion; a blanket exclusion would have left a stuck/dead reaper with none. Keyed on the lease, **not** a blanket silencing — a NULL-lease (push) row of identical age still fires, and an absent/empty/unparseable lease fails OPEN. **E-02, the fourth reader of `running_exec_ids`, deliberately gets no such grace:** a terminal→non-terminal reversal is corruption regardless of ownership, the reaper's `status='running'` CAS cannot produce one, and re-delivery preserves the `execution_id` — so excluding leased rows there would blind E-02 on exactly the late-result-vs-reaper race #1081 introduces |
| E-02 | A | critical | No phantom reversal: a row terminal in the previous cycle never reappears non-terminal (Redis state key `canary:e02:terminal_seen`) |
| E-03 | A | major | Completed rows populated: every terminal row (`success`/`failed`/`cancelled`) has `completed_at IS NOT NULL`. Predicate is `completed_at`-only — the catalog's `+ duration_ms` clause false-fires on healthy queue-terminated rows (cancel/fail/expire set `completed_at` but never `duration_ms`). Leading-edge tripwire over a shared terminal-row collector windowed on `started_at` (`max timeout + 300s`, `LIMIT 5000`), not a backfill auditor (#1077) |
| E-04 | A | major | Queued-row metadata integrity: every `queued` row has `queued_at IS NOT NULL` AND a non-NULL, JSON-parseable `backlog_metadata` (the `backlog_service.drain_next` replay contract — a malformed blob raises `JSONDecodeError` and stalls the FIFO). Reads the queued-row metadata `_collect_executions` captures, scoped STRICTLY to `status='queued'` (never terminal, so #1449's deferred terminal `backlog_metadata` NULL-out can't false-fire). **SECURITY:** `observed_state`/`signal_query` report only the failed-predicate reason (`queued_at_null`/`backlog_metadata_null`/`backlog_metadata_invalid_json`) + ids — never the raw metadata (may carry credentials; violations persist). Older-image (columns absent) → skip eid (#1077) |
| E-05 | B | major | Dispatched rows have session: no `running` row >60s with `claude_session_id IS NULL` (#106) |
| E-06 | B | major | No overdue `next_run_at`: no enabled, non-deleted schedule **of a live agent** whose `next_run_at` is older than `now − misfire_grace_time` — the "Next: Nd ago" stale-projection bug (#1472). Detection net for any residual after the fire-time-advance / add-retry root fixes; tz-aware UTC comparison. The collector INNER-joins `agent_ownership` and requires `agent_ownership.deleted_at IS NULL` (ent#335) — it mirrors **all** of `db/schedules/crud.py::list_all_enabled_schedules`, the list the scheduler actually arms, having previously copied only the schedule's own `deleted_at` filter. A soft-deleted agent's schedules are preserved by #834 Phase 1a for up to 180 days with `enabled = 1 AND deleted_at IS NULL` and a frozen `next_run_at` — frozen because the scheduler *correctly* stopped registering them — so E-06 flagged all of them every cycle, forever: 6,220 of 6,605 total violations (94%) in 13h on eu2. The matched pair to `_collect_known_agents`, which deliberately *includes* soft-deleted agents so L-03 doesn't report those same preserved rows as orphans. The join also drops schedules whose `agent_ownership` row is gone entirely — L-03's orphans, covered by its unfiltered `agent_schedules` scan |
| G-03 | A | minor | Clock sanity: terminal rows have `started_at ≤ completed_at` (~1s cross-worker skew tolerance). Reduced from the catalog's `created_at ≤ started_at ≤ completed_at` (`schedule_executions` has no `created_at`). UTC-aware parse (E-06 `_to_utc` shape) so a #1474 mixed naive/`Z` pair compares without raising; E-03 owns the NULL-`completed_at` case (#1077) |
| G-04 | A | critical | No credentials in backlog metadata: a `queued` row's `backlog_metadata` matches no known secret prefix (`sk-`/`ghp_`/`gho_`/`xoxb-`/`xoxp-`/`AKIA`/…, word-boundary anchored). Rides the same queued-row `backlog_metadata` E-04 collects — the field is persisted plaintext and read into E-04's violations, so a leaked secret leaks into durable operator-visible state. **SECURITY:** reports only the matched pattern NAME + ids — never the secret, surrounding bytes, or raw metadata (one violation per row, stops at first match) (#1077) |
| B-01 | A | critical | Queue-status coherence: `db.get_queued_count` ≡ an independently-collected queued id-count — regression guard against a future cache layer or status-filter drift. Both sides now read through the SAME `get_engine()`/`DATABASE_URL` seam (#1450): Side B (`queued_ids_via_engine`, a `SELECT id`/literal `'queued'`) moved off the raw-sqlite `queued_exec_ids` set so it stays backend-consistent with the accessor on Postgres (not raw-sqlite vs engine, the #300/#1093 gap). The collector does one confirm-re-read so a concurrent enqueue/drain landing between the two reads self-heals instead of false-firing; a persistent drift survives it and fires. Independent code path preserved (COUNT/enum vs SELECT/literal) so it's not a tautology. On an engine-read failure B-01 skips (never compares an engine count to a raw-sqlite id-set). The collector-wide migration landed (#1540): **all** SQL-tier collector reads (`_collect_known_agents`, `_collect_executions`, `_collect_terminal_executions`, `_collect_terminal_rows`, `_collect_enabled_schedules`, `_collect_orphan_refs`) now honor `DATABASE_URL` — so the whole harness, not just B-01, is un-blinded on PostgreSQL (the `sqlite.*` `sources_unavailable` labels are kept verbatim as an internal skip-contract prefix for L-03/E-02, not a backend claim) |
| B-02 | B | critical | No queued without slots-full: queued > 0 ⇒ slots full OR a drain tick fired <60s ago (`canary:drain_tick_at` heartbeat) |
| L-03 | A | crit/major | Delete cascades: no live row in any cross-cutting table (sharing, schedules, non-terminal executions, skills, tags, shared files, public links, pending operator queue/access requests, agent-scoped MCP keys, active chat sessions) referencing an `agent_name` absent from `agent_ownership`; no orphan `agent:slots:{name}` (critical for orphaned executions/slots, major otherwise; #129 class) |
| R-01 | A | critical | No **persisting** zombie Claude processes: per running agent container, `ps -eo stat,pid,comm` (awk `$1 ~ /^Z/ && $3 == "claude"`, anchored `^Z` — procps-ng emits STAT left-aligned; guards PR #407) must show no zombie pid that has survived a **dwell window** (`DWELL_SECONDS` 600 = 2× the canary interval). A transient zombie between child-exit and the parent's `wait()` is normal and produces **no violation row at all** (ent#337 — 6 critical pages in 13h on eu2, every one `zombie_count: 1` and self-resolved by the next cycle). The dwell is per-**pid**, not per-count: three cycles catching three *different* transients would satisfy a count-based dwell, and under sustained load no zero is ever observed to clear it. Measured in **elapsed wall-clock** from `snapshot.snapshot_time` (never `time.time()`) so it is multi-worker-safe (H-01's precedent — a "seen twice" rule self-confirms in <1s across two uvicorn workers; the #1881 `canary:leader` lease narrows that window but fails open, so the wall-clock rule stays load-bearing) and structurally testable at the boundary (#1909). State is a per-agent HASH `agent:canary_zombie:{name}` (`{pid: first_seen:first_count:last_seen}`), first-write-wins — rewriting `first_seen` each cycle would reset the clock and leave a permanently blind critical invariant. Deliberately `agent:`-prefixed and registered in `agent_runtime_state.CLEARED_KEYSPACES` (#1560): it is the first *name-keyed* canary key, so a recycled name would otherwise inherit its predecessor's dwell (E-02's `canary:e02:*` are legitimately unregistered — global, not per-agent). A `last_seen` gap over 2× the interval restarts the dwell, so a docker-exec outage can't be counted as dwell time; unparseable `ps` output is `sources_unavailable`, never a silent green. A pid is only an identity within one **pid namespace**, so the marker also carries a reserved `__started_at` field holding the container's `State.StartedAt` and drops the whole marker when it moves: a restart *outside* the backend (`docker restart`, a restart policy after an OOM/crash) never reaches `clear_agent_breakers`, and a fresh namespace hands out low pids immediately, so an unrelated transient would otherwise inherit a dwell-old `first_seen` and page on its first sample. The `last_seen` gap check cannot cover this — a restart inside one cycle leaves no observation gap at all. Costs nothing: docker-py's `containers.list()` defaults to `sparse=False` and already full-inspects each container. Only an *observed mismatch* invalidates — an unreadable `StartedAt` leaves the dwell alone, since restarting on a non-signal every cycle would blind the invariant exactly as rewriting `first_seen` would. Docker-exec source; per-container failures land in `sources_unavailable` so one unhealthy container doesn't kill the cycle |
| H-01 | A | crit/major | **Collector blindness (harness self-check, #1813):** the SQL roster read (`_collect_known_agents`) returned zero rows — or raised — while an **independent, non-SQL** source proves the fleet is alive. #1540 repointed the collectors at the live engine but left the failure *shape*: zero rows is indistinguishable from a clean fleet, so a re-blinded collector reports all-clear (verified: a 2-agent fleet on a diverged backend yields `known_agents=∅`, `sources_unavailable=[]`, **zero violations** across all invariants). Prior coverage was worse than none — L-03 fires only when an execution happens to hold a slot, and then blames a *ghost agent*, misdiagnosing a blind detector as a delete-cascade bug. Evidence: `docker_agent_names` (running agent containers, taken from the container LIST — not `zombie_counts`, which is keyed by `exec_run` success and thins on a degraded container) ∪ `orphan_redis_slots`. Reasons: `roster_read_failed` / `roster_empty_contradicted` (both critical) / `roster_empty_unverifiable` (major — the "dead smoke detector chirps" branch: the evidence source was unreachable, was never read, or only Redis had anything to say). **The two evidence sources are not interchangeable for severity.** Docker alone confirms; **Redis alone never does** — `orphan_redis_slots` is by definition slot keys whose agent is absent from `agent_ownership`, i.e. the leaked-slot state L-03 exists to report, so a genuinely empty fleet holding one leaked key would otherwise page critical over a correct roster and an unrelated Redis leak. Redis names still ride in `evidence_sample`; only the ladder differs. **`docker_available` / `redis_available` are TRI-STATE** (`True` ran-and-fine / `False` ran-and-failed / `None` never ran), backed by `Snapshot.collectors_ran`: `sources_unavailable` structurally cannot express "never ran" (a skipped collector records nothing), and `collect_snapshot` returns early on a roster-read failure — so the two-state form reported `docker=up · redis=up · 0 vs 0 agent(s)` on the one arm where neither had been consulted. The Docker collector therefore runs **before** the roster read (it has no dependency on it), so `roster_read_failed` carries real evidence; Redis needs `known_agents` and honestly reports `None` there. **Confirmed on elapsed wall-clock** (`CONFIRMATION_MIN_SECONDS`, marker `canary:h01:suspect_since` — E-02's cross-cycle-state precedent) so the last-agent delete race — DB row gone, container still tearing down — cannot false-fire; cost is at most one extra cycle to alarm. Deliberately **not** "a second cycle": prod runs `--workers 2`, and when the gate was written `canary_service` held no leader lease, so both loops shared the marker and worker B would confirm worker A's sighting seconds later, collapsing the gate inside the very window it exists to ride out. The `canary:leader` lease (#1881) does **not** retire this rule — it fails open to leader when Redis is down, which restores concurrent loops over the shared marker exactly in one of the states the harness exists to report, and the thing being ridden out is a real-time transient (a container finishing teardown), which is a single-worker property regardless. The gate applies to **every** firing arm including `roster_read_failed`: that arm has no delete race to ride out, but a raised roster read is very often a momentary DB blip (connection reset, PG restart, pool exhaustion), and paging critical on one of those is how a safety net gets muted. An unreadable **or unwritable** marker fires **unconfirmed** rather than skipping (a guard that cannot self-check must say so). The marker carries a **24h TTL refreshed every suspicious cycle** — `_clear_marker` is best-effort and an `invariant_ids`-filtered `run-cycle` never reaches it, so an orphaned marker would otherwise stay armed forever and make the next genuine episode confirm on its first cycle; refreshing makes it an idle timeout rather than an absolute lifetime, so a long episode cannot silently re-arm and re-alert. A **whole-database** outage now reaches the check at all: `_run_cycle_inner`'s pre-cycle `get_latest_canary_violation_per_invariant()` read is fail-open, with transition detection falling back to `canary:last_cycle_red` (a Redis-held record of the previous cycle's red set — a separate failure domain from the DB) so a persistent outage still chirps once rather than every cycle. Scoped to the roster read ONLY: on a live-but-quiet fleet `terminal_rows`/`enabled_schedules`/`orphan_refs`/`terminal_exec_statuses` are all legitimately zero, so a general "any collector reads zero" rule would false-alarm on every idle install. Dual-track by construction (pure function over the Snapshot, issues no SQL). **Residual:** an entirely *stopped* fleet holds no containers and no slots, so it reaches `roster_empty_unverifiable` at most; partial blindness (roster returns 1 of 20) is out of scope — a count comparison would false-fire on create/stop races |

### Agent Compatibility Validation (#668)

Advisory, non-blocking server-side validation of a **running** agent's workspace
against 88 best-practice checks (12 categories, #2137) — surfaced in the Agent Detail
Overview tab (`components/CompatibilityPanel.vue`, reusing the "needs attention"
idiom), via `GET /api/agents/{name}/compatibility`, and the MCP tool
`get_agent_compatibility_report`. The canonical check list is
`docs/agent-validation-spec.md` (single source of truth, sync-tested against
`spec.py`).

Package `services/compatibility/` mirrors the deterministic `canary/` library (`spec.py` catalog, `collector.py`, `static_checks.py`, `ai_checks.py`, `fixes.py`, `__init__.py` → `build_report`/`apply_fix`). Details in [agent-compatibility-validation.md](feature-flows/agent-compatibility-validation.md).

- **Collector**: ONE `docker exec` runs a base64-injected `python3` script walking a FIXED path allowlist → ONE JSON snapshot (per-file `{exists,size,binary,truncated,content}`, 256 KB/file + 2 MB/total caps); secret-bearing files (`.env`, `.mcp.json`) are **existence-only**. Backend `json.loads` once → `unavailable` on any failure (never 500); a stopped container → degraded report from the last persisted result.
- **Checks**: pure `(snapshot)→[Check]` functions. `[STATIC]` deterministic (always, free); `[AI]` LLM-judged (Claude Haiku, batched by category, tool-use structured output, **iterate-expected**, fail-open on no-key/error). **AI severity capped at SOFT** — HARD reserved for STATIC. Claude-only checks skipped for non-Claude runtimes (#1187). Secret values never echoed; AI payloads redacted.
- **Persistence** (`agent_compatibility_results`, latest-snapshot-per-agent, upsert): STATIC recomputes live; persisted AI verdicts merge in so findings show on every Overview load without re-spending tokens (`?include_ai=true` / "Re-run" forces fresh AI; requirements §41). Cascade/rename via `AGENT_REFS`.
- **Auto-fix** (`POST .../compatibility/fix`, owner/admin): the 9 gitignore checks (G-002 retired in #2137); reuses `git_service._GITIGNORE_PATTERNS`; per-agent Redis lock (`compat_fix:{name}`); atomic base64 write-back; G-001 removes a blanket `.claude/` line by exact-line match. **No auto-commit** — uncommitted until next git sync. Creates no execution.
- **T-018 + the fail-open class (ent#89).** `T-018` (soft, static) reports the `schedules:` block's **structure**, sharing the ent#89 reader with the materializer so the report cannot drift from what creation does; **cron stays A-002's** (two checks disagreeing on one field is worse than either). It is the one check that **fails closed**: `run_static` converts a raise into `skipped` and `_counts` counts only `fail`, so a raising *soft* check drops `soft_count` 1→0 and — since `overall` is a bare `> 0` test — flips `issues → compatible` exactly when its finding was the only failure, then `_report_from_persisted` replays that from `checks_json` on every stopped-agent read. `detail` carries `type(e).__name__` only (it is persisted and UI-rendered). Two live instances fixed with it: **`c_p006`** (a HARD check) iterated `schedules` with no `isinstance(..., list)` guard unlike its four siblings, so `schedules: 5` silently vanished from `hard_count`; and **`_valid_cron`** (A-002) was a per-field regex wrong in *both* directions — it rejected `0 9 * * MON` and accepted `99 99 * * *` — now delegating to `schedule_validation.validate_cron_expression`, the scheduler's own parser. `run_static`'s swallow now logs (it was silent for all ~100 checks); converting it to `fail` platform-wide is a measured follow-up, not this change.

### MCP Exposure — Dedicated Dynamic Tools (#846)

Per-agent owner-toggled flag (`agent_ownership.mcp_exposed`, default 0) that
publishes an agent as a first-class MCP tool. When enabled, the Trinity MCP
server **dynamically registers** a dedicated `chat_with_<slug>` tool —
functionally identical to `chat_with_agent` with the agent name pre-filled —
**at runtime, no MCP-server restart**. The flag publishes a *surface* only;
execution always runs the same `checkAgentAccess` gate, so ownership/sharing is
never bypassed.

- **Refresh = poll, not WS.** The MCP server polls `GET /api/internal/mcp-exposed-agents`
  (existing `X-Internal-Secret` path, ~20s, `tools/dynamic-agents.ts`
  reconciler), diffs an `agentName→toolName` map, and calls FastMCP
  `addTool`/`removeTool`. FastMCP fans `notifications/tools/list_changed` to live
  sessions, so a connected client sees/loses the tool within ~one poll. The
  reconciler is **fail-open** (mutates only on a valid 200; keeps last-known set
  on error/parse-failure/timeout) and holds an in-flight mutex so startup-sync
  and the interval can't race.
- **Slug = single backend source of truth.** `services/agent_service/mcp_tool_names.py::compute_tool_names`
  computes the deterministic, collision-free name over the **full set** (sorted;
  `_<sha1(name)[:4]>` suffix on agent-vs-agent base-slug collision). The MCP
  server consumes it verbatim and applies one final guard against its own
  built-in tool names. The per-agent GET uses the same helper so UI and MCP never
  diverge.
- **Description = name-only (metadata-free)** — the dedicated tool's description
  is advertised **globally** to every non-connector MCP key (FastMCP filters the
  advertised list by `canAccess`; dedicated tools use only the connector-tier
  gate), so it must carry no per-agent metadata. The `trinity.template` Docker
  label is deliberately **excluded**: embedding it leaked the template/repo
  identifier cross-tenant to callers who cannot access the agent and opened a
  prompt-injection surface into the advertised description (#846 CSO). The agent
  name is already intrinsic to the `chat_with_<slug>` tool name, so a name-only
  description adds no disclosure beyond the name.
- **Visibility** mirrors operator tools: dedicated tools register with the
  `connectorDenied` `canAccess` gate (hidden from connector-scoped keys, ent#46
  isolation preserved). The shared `chat_with_agent` body is extracted into
  `tools/chat.ts::runAgentChat`, reused by both `chat_with_agent` and every
  dedicated tool — no logic fork (preserves #946 pull routing, parallel/self-task
  paths, idempotency tokens, #914 gateway-timeout recovery). The audit row binds
  the target agent via `withAudit(..., boundTargetId)` since dedicated tools carry
  no `agent_name` param.
- `mcp_exposed` is surfaced on `GET /api/agents` / MCP `list_agents`. Dual-track
  migration (SQLite `agent_ownership_mcp_exposed` + Alembic
  `0009_agent_ownership_mcp_exposed`).
- **Connect surface (#1575):** the Expose-via-MCP panel (`components/McpExposedPanel.vue`)
  gains a one-click **Copy connection config** action when exposed — it reuses the
  existing [MCP connector](feature-flows/mcp-connector.md) (scoped `scope='connector'`
  key + playbooks-as-tools + `build_snippets`) to hand an external client a
  ready-to-paste `.mcp.json` with a least-privilege, agent-scoped, revocable key
  already embedded. No new endpoint/key type — the connector endpoints are reused
  (owner-only, keys already list in Settings → MCP Keys, revoke severs the connection).

### Brain Orb — Self-Rendering Mind page (#58, trinity-enterprise)

Capability-gated per-agent 3D knowledge-graph page for Cornelius-class agents.
**Shipped: static render (Phase 1) + live scope control (Phase 2) + client-held
Gemini Live voice tile + read-only KB search (Phase 3, #60) + owner-gated KB
writes: capture/link + voice-transcript capture + the write→refresh loop (Phase
4a/4b, #61/#66/#67) + post-voice-session processing as a standard execution
(#102).** The orb renders the agent-produced graph, supports button-driven
**scope mount/unmount → agent re-export → live rebuild**, and a **client-held
voice tile** (browser connects directly to Gemini Live via a short-lived,
config-locked ephemeral token minted by Trinity — no audio proxying). Voice
transcripts save through the owner-gated `action` broker; the configured
post-session prompt (#73) is dispatched by `services/brain_orb_postprocess.py`
via `execute_task(triggered_by="voice")` — a real, observable execution row
(sweep-safe, cost-tracked, failures surface as FAILED), replacing the hook's
detached `claude -p` (#102). Still deferred: `run_skill` headless-skill
injection. Mirrors the workspace page (gated per-agent route) and the
agent-owned read-surface pattern (pipelines #919, reports #918): the agent owns
generation + scope state (Invariant #8), Trinity reads/renders + brokers
control. Default OFF — no impact on other agents. Full flow:
[brain-orb.md](feature-flows/brain-orb.md).

**Default Cornelius (trinity-enterprise#107):** a **fresh install** auto-seeds a
default "Cornelius" second-brain agent from the public `github:Abilityai/cornelius`
template — cloned anonymously (source-mode, no PAT) on the trinity-enterprise#123
tokenless path (`services/cornelius_agent_service.py`) — and existence-guarded-enables the
`brain_orb_enabled` flag, so the orb renders out-of-the-box. First-run-only (durable
`cornelius_seeded` system-setting flag — deleting Cornelius does not re-provision)
and skipped when any non-system agent already exists (established fleets are never
surprised); Redis SETNX lock (`cornelius:provision`) guards the `--workers 2` race (ownership-checked via `SingleFlightLock` #1920 — a verbatim twin of system_seed's constant-"1" + unconditional-delete bug, fixed with it).
Full flow: [cornelius-default-agent.md](feature-flows/cornelius-default-agent.md).

- **First-party assets** (`src/frontend/public/brain-orb/`): the orb's verbatim
  page is split into `index.html` + externalized `orb.js`, with `three`/`marked`/
  `DOMPurify`/JetBrains-Mono vendored locally — so it runs CSP-clean under prod
  `script-src 'self'` / `font-src 'self'` with **no nginx change** (the #979 trap
  was *agent-origin* + *inline* scripts; this is first-party + external). Only
  mechanical edits: externalize the module, vendor CDN deps, repoint the data +
  scope fetches at the per-agent proxy base (carrying the platform JWT), hide the
  still-deferred voice/action panels. Markdown note bodies are DOMPurify-sanitized
  (H-005).
- **Frontend host** (`views/AgentBrainOrb.vue`, route `/agents/:name/brain`,
  lazy + `beforeEnter` flag guard): a thin chrome + **same-origin iframe** of the
  static page. JWT reaches the iframe's data fetch via origin-pinned `postMessage`
  (orb posts `brain-orb:ready` → host replies `brain-orb:init {agentName, apiBase,
  authToken}`; never in a URL) — no new ticket primitive. A `brain-orb:error`
  message shows the "hasn't rendered its mind yet" empty state. Gating:
  `brainOrbAvailable` (platform flag, `stores/sessions.js`) **AND** the per-agent
  `brain-orb` token in `template.yaml capabilities` (read from `/info`). BOTH the
  route guard (`beforeEnter` fetches `/info`, #60) and the `visibleTabs` Brain tab
  enforce the capability, so the orb is never launchable on a non-Cornelius agent —
  even via a raw URL (redirect, not empty state). Selecting the tab route-pushes to
  the page. The voice tile also ships a **vendored p5** audio-reactive orb that
  pulses with the spoken audio (CDN load was removed then re-vendored, #60).
- **Backend proxy** (`routers/agent_brain_orb.py`, prefix `/api/agents/{name}/brain-orb/*`):
  one shared gate/proxy helper (flag → running → `agent_httpx_client` #1159, **byte
  pass-through**, 404/503/504/502 mapping). `GET /data` + `GET /scopes` + `POST /tool`
  (read-only KB search) are read (`AuthorizedAgentByName`); **`POST /scope` is the only
  mutating route and is `OwnedAgentByName`** (owner/admin) — body-capped 64 KB, 200s
  timeout above the agent hook's 180s. `POST /voice-token` (Phase 3, `AuthorizedAgentByName`,
  per-(user,agent) rate-limited) mints the ephemeral Gemini Live credential (does NOT
  contact the agent — a Google call, not an agent call).
- **Voice-token mint** (`services/brain_orb_voice_service.py`, Phase 3, #60): the client-held
  voice tile connects the browser DIRECTLY to Gemini Live. `mint_voice_token()` builds its
  **own v1alpha `genai.Client`** (NOT the cached `gemini_voice` singleton, which lacks
  v1alpha and would reject the ephemeral mint) and calls `auth_tokens.create` with
  `live_connect_constraints` locking the model + the whole `LiveConnectConfig` (system
  prompt + voice + the **read/visual/scope-only tool manifest** — no write tools), `uses=1`,
  a ~60s new-session window, and `expire_time = VOICE_MAX_DURATION`. The token's constraints
  ARE the security envelope (no Redis ticket needed — the browser talks to Google, not
  Trinity). Response field is `ephemeral_token` (never `token`, which would flip orb.js's
  deferred write surface on). The orb page (which holds the JWT) mints and relays only the
  Google token to the nested voice iframe over `postMessage`. Writes stay off by
  construction: locked manifest + no `/session` route.
- **Agent-server mirror** (`agent_server/routers/brain_orb.py`): `GET /api/brain-orb/data`
  streams the fixed-path `~/resources/agent-visualization/data.json` via `FileResponse`.
  Scope + search run **agent convention hooks** (mirrors `~/.trinity/pre-check`, #454):
  `GET /api/brain-orb/scopes` runs `~/.trinity/brain-orb/scopes`; `POST /api/brain-orb/scope`
  pipes the body to `~/.trinity/brain-orb/scope` (mutate active set, re-export → rewrite
  `data.json`, print new state); `POST /api/brain-orb/tool` pipes a query to the read-only
  `~/.trinity/brain-orb/search` hook (scope-aware, no writes). All via hardened async
  subprocess (timeout-kill, output cap, JSON-parse + non-zero-exit guards); **404 when the
  hook is absent**. Trinity never runs `export_data.py` itself — the agent owns generation +
  scope state (Invariant #8).
- Platform flags (**runtime-resolved, admin-configurable — trinity-enterprise#85**): the three
  flags resolve at request time via `settings_service.is_brain_orb_enabled()` /
  `..._voice_enabled()` / `..._write_enabled()` — `system_settings` row (wins in both
  directions) → `BRAIN_ORB_*` env var as opt-in fallback → default OFF (one shared
  `_resolve_bool_flag` helper; fail-open on a settings-read failure; deliberately uncached,
  #506 `--workers 2` rationale). **Precedence note:** once a stored row exists the env var is
  ignored until the override is cleared (`PUT /api/settings/brain-orb {clear: [...]}` or
  generic `DELETE /api/settings/{key}`). Compositions: `brain_orb_available = base`;
  `brain_orb_voice_available = base && voice && GEMINI_API_KEY` (key stays env-only — secret);
  `brain_orb_write_available = base && write`; the voice-token mint route gates on
  `base && voice` too. Admin surface: `GET/PUT /api/settings/brain-orb` (Settings → General
  panel with per-flag source display). Voice frontend assets are CSP-clean (hand-rolled Gemini
  client, externalized `voice/voice.js`, same-origin `voice/mic-worklet.js`; `connect-src`
  already allows `wss:`). No DB change, no migration, no new secret.

---

## API Endpoints

### Agents (33 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents` | List all agents |
| GET | `/api/agents/context-stats` | Context & activity state for all agents |
| GET | `/api/agents/autonomy-status` | Autonomy status for all accessible agents |
| GET | `/api/agents/sync-health` | Per-agent git sync health for dashboard dots (#389) |
| GET | `/api/agents/subscription-pressure` | Batch subscription-pressure rows for dashboard badges (#471): per accessible agent (pure-DB `visible_agent_names`, ent#384 helper) — `auth_mode` (`AgentAuthStatus` vocabulary), `subscription_name`, `failure_events_24h` + its auth-kind slice `auth_failures_24h` (#2352), one-gate `rate_limited_now`, the probe's own `token_status` (`ok`|`invalid_token`|`rate_limited`|`error`), fresh `utilization_5h_pct`. Explicit `response_model`; registered before `/{agent_name}` (Invariant #4) |
| POST | `/api/agents` | Create agent. Accepts an optional `display_label` (ent#1640) — the human-facing name set at creation (normalized + named-error validated like `PUT /label`); omit/blank → renders under the slug. Accepts `import_intent: fork\|copy\|clone` for `github:` templates (ent#15 — copy = backend-materialized snapshot, no sync/row/PAT; see [github-import-intents.md](feature-flows/github-import-intents.md)) and an `Idempotency-Key` header (Invariant #18, scope `agent_create:{user_id}`) |
| GET | `/api/agents/{name}` | Get agent details |
| GET/PUT | `/api/agents/{name}/label` | Get / set-or-clear the agent's human-facing **display label** (ent#181/#1640). Owner-only (`OwnedAgentByName`); `label` is **required-but-nullable** and unknown fields are rejected (`extra="forbid"`, #1821 — an ignored extra plus a `None` default made `{"display_label": …}` a silent 200 + wipe, so `{}` and any unrecognised body now 422); an explicit null or blank clears to the slug fallback; presentation-only (the slug never moves, unlike `PUT /rename`); trims + rejects control chars/line-breaks with a **named** error, **not** unique (the slug guarantees uniqueness), audit-logged, broadcasts `agent_label_changed` |
| DELETE | `/api/agents/{name}` | Soft-delete agent (see [Soft Delete](#soft-delete-retention--recovery-834-772)) |
| POST | `/api/agents/{name}/start` | Start agent |
| POST | `/api/agents/{name}/stop` | Stop agent |
| POST | `/api/agents/{name}/chat` | Send chat message |
| GET | `/api/agents/{name}/chat/history` | In-memory chat history (container) |
| GET | `/api/agents/{name}/chat/history/persistent` | Persistent chat history (database) |
| GET | `/api/agents/{name}/chat/sessions` | List chat sessions |
| GET | `/api/agents/{name}/chat/sessions/{id}` | Session details with messages |
| POST | `/api/agents/{name}/chat/sessions/{id}/close` | Close chat session |
| DELETE | `/api/agents/{name}/chat/history` | Reset session |
| GET | `/api/agents/{name}/logs` | Container logs |
| GET | `/api/agents/{name}/stats` | Live telemetry |
| GET | `/api/agents/{name}/activity` | Activity summary |
| GET | `/api/agents/{name}/info` | Template metadata |
| GET | `/api/agents/{name}/a2a/agent-card` | A2A Agent Card (protocol `0.3.0`) for external orchestrator discovery — authenticated (`AuthorizedAgentByName`) (#737) |
| POST | `/api/agents/{name}/a2a/call` | **Outbound** — task an EXTERNAL A2A agent (#736). `AuthorizedAgentByName` **+ an agent-scoped self-check** (an agent key may call only AS ITSELF; a *permitted sibling* may not place calls under a neighbour's name). Under the OSS provider endpoints are **platform-scope**, so what this protects is **attribution** — the rate-limit key, the audit row and the activity row all name the agent that actually spent the call — not a per-agent credential boundary that does not exist yet; it holds the line for a future per-agent provider. `reject_agent_principal` deliberately absent: a *use*, not a *grant* (Invariant #8). Target is a registry **name**, never a URL. Bounded per-agent + fleet; `effect_guard`-deduped on `{endpoint_id, resolved_url, context_id, task_id}` with a **required** `dedup_label`. 404 when `A2A_OUTBOUND_ENABLED` is off |
| POST | `/api/agents/{name}/a2a/task` | Poll a remote A2A task by id on the same registered endpoint (#736). Same gates; deliberately NOT `effect_guard`-wrapped — a poll is a read, and deduping it would answer "has it finished yet?" from a snapshot of the last time it had not |
| GET | `/api/agents/{name}/files` | List workspace files (tree) |
| GET | `/api/agents/{name}/files/download` | Download file |
| POST | `/api/agents/{name}/files/mkdir` | Create workspace directory (#37) |
| GET/PUT | `/api/agents/{name}/folders` | Get/update shared folder config |
| GET | `/api/agents/{name}/folders/available` | Mountable folders from permitted agents |
| GET | `/api/agents/{name}/folders/consumers` | Agents that will mount this folder |
| GET/PUT | `/api/agents/{name}/autonomy` | Get / enable-disable autonomy — the agent-level gate the scheduler checks on every cron fire. **Writes only `agent_ownership.autonomy_enabled`**; per-schedule `enabled` is owner intent and is never rewritten, so an off→on cycle restores the prior per-schedule state (#1945). Response: `total_schedules`/`enabled_schedules`/`message` |
| POST | `/api/agents/{name}/ssh-access` | Ephemeral **key-based** SSH credentials (admin-only; BYOK — the caller supplies `public_key`, the server never handles private keys #175). `auth_method` accepts only `"key"`; password auth returned 400 since #1615 (it never worked — agent sshd runs `PasswordAuthentication no`, and host-side hashing used the `crypt` module removed in Python 3.13) |
| GET/PUT | `/api/agents/{name}/read-only` | Read-only mode status / toggle (blocks source file writes) |
| GET/PUT | `/api/agents/{name}/timeout` | Execution timeout (60–7200s, default 3600s, #665). PUT 400 `agent_timeout_below_active_schedules` if the new cap drops below any non-deleted schedule's `timeout_seconds` (#929) |
| GET/PUT | `/api/agents/{name}/public-channel-model` | Per-agent model override for **public-facing** channels — public link, Slack/Telegram/WhatsApp, x402 (#894). GET returns raw override + resolved model + selectable list; PUT owner-only, whitelist-validated (422), NULL clears → platform default. The whitelist `settings_service.PUBLIC_CHANNEL_MODELS` now **derives from the single-source `services/model_catalog.py`** (#2086 — re-export, so `claude-opus-5` is selectable end-to-end and the list can't drift from the frontend picker). Resolved at `public.py`/`message_router.py`/`paid.py` (override → platform default → fallback); the owner's own chats/schedules are unaffected |
| GET/PUT | `/api/agents/{name}/voice-replies` | Per-agent outbound-voice (TTS) config (epic #24/#25; v2 ent#117). GET returns `{enabled, voice_id, channels:{telegram,slack,whatsapp}, effective_voice_id, default_voice_id, available}`; PUT owner-only partial update — agent-level `enabled`+`voice_id` (enabling needs a voice_id OR a platform default) and/or per-channel `channels` flags. Voice is a **per-message capability** now (agent opts in via `send_voice_reply`), not always-on |
| POST | `/api/agents/{name}/voice-reply` | Deliver one channel reply as a voice note (`send_voice_reply` MCP tool, ent#117). `AuthorizedAgentByName` + agent-scoped self-check; resolves the channel destination from the execution; fail-soft `{delivered, channel, reason}` (200) so the agent falls back to text; 409 on an in-flight duplicate for the same turn. A `source_channel="portal"` turn short-circuits with `reason="portal_client_narrated"` + `guidance` — or `portal_voice_not_configured` when no voice resolves, so the guidance never points at a speaker control that does not render (#2157). See [voice_reply_service](#cross-cutting-subsystems) |
| GET/PUT | `/api/agents/{name}/guardrails` | Per-agent guardrails config / overrides (GUARD-001) |
| GET/PUT | `/api/agents/{name}/file-sharing` | Outbound file-sharing status + quota / owner-only toggle (returns `restart_required`) (FILES-001) |
| POST | `/api/agents/{name}/shared-files` | Mint a download URL for a file in the publish dir (owner/admin or agent-scoped key) |
| GET | `/api/agents/{name}/shared-files` | List active shared files with download counts |
| DELETE | `/api/agents/{name}/shared-files/{file_id}` | Revoke a shared file (owner-only; idempotent) |
| POST | `/api/agents/{name}/user-memory` | Write per-user memory blob; email resolved from execution_id server-side (MEM-001, #888) |
| POST | `/api/agents/{name}/data/export` | Export agent `data/` as a tar (owner/admin; `?format=stream`\|`base64`; 413 over cap; per-agent op lock). See [Agent Runtime Data](#agent-runtime-data--data_paths--snapshotexport-1169) (#1169) |
| POST | `/api/agents/{name}/data/import` | Restore an uploaded tar into agent `data/` via the agent-server restore primitive (owner/admin; `data/**` allowlist + traversal guard; `Idempotency-Key`; op lock) (#1169) |
| POST | `/api/agents/{name}/heartbeat` | Agent liveness heartbeat — auth and semantics in [Heartbeat Liveness](#heartbeat-liveness-reliability-004-307) |
| POST | `/api/agents/{name}/executions/{execution_id}/result` | Fire-and-forget terminal callback — agent's own MCP key + ownership + durable async-marker gate; finalizes via `apply_result`. 503 + Retry-After when the #1085 re-delivery governor is paused / capped (retryable). See [Fire-and-Forget Dispatch](#fire-and-forget-dispatch-1083) (#1083) |
| GET | `/api/agents/{name}/circuit-breaker` | Unified breaker state: `{dispatch:{state,failure_count,retry_after_seconds}, transport:{...}, open:bool, config:{enabled,global_enabled}}` (#526) |
| PUT | `/api/agents/{name}/circuit-breaker` | Enable/disable per-agent dispatch breaker (owner-only); engages only with global `DISPATCH_BREAKER_ENABLED` (#526) |
| POST | `/api/agents/{name}/circuit-breaker/reset` | Admin-only; resets BOTH transport and dispatch breakers to closed (#921, #526) |
| GET/PUT | `/api/agents/{name}/operator-resume` | Per-agent respond→resume opt-in (ent#329). GET any accessor; PUT **owner-only** (enabling means answers may now spend, and the bill is the owner's). Default OFF — see [Respond → Resume](#respond--resume-dispatch-ent329) |
| GET | `/api/agents/{name}/brain-orb/data` | Read-only proxy of the agent's Brain Orb `data.json` (`AuthorizedAgentByName`; byte pass-through; 404 when flag off / no export, 503/504 unreachable, 502 agent error). See [Brain Orb](#brain-orb--self-rendering-mind-page-58-trinity-enterprise) (#58) |
| GET | `/api/agents/{name}/brain-orb/scopes` | List the agent's selectable + active vault scopes for the orb scope panel (`AuthorizedAgentByName`; 404 when unsupported). (#58 Phase 2) |
| POST | `/api/agents/{name}/brain-orb/scope` | Mutate the active scope set → agent re-export (**`OwnedAgentByName`** — owner/admin; body-capped; 404 when unsupported). (#58 Phase 2) |
| POST | `/api/agents/{name}/brain-orb/voice-token` | Mint a short-lived, config-locked Gemini Live **ephemeral token** for the client-held voice tile (`AuthorizedAgentByName`; per-(user,agent) rate-limited; 404 when the runtime-resolved base or voice flag is off (#85), 503 no key, 502 mint error). Response field `ephemeral_token`. (#60 Phase 3) |
| POST | `/api/agents/{name}/brain-orb/tool` | Read-only KB search — proxies to the agent's `~/.trinity/brain-orb/search` hook (`AuthorizedAgentByName`; 404 when unsupported). (#60 Phase 3) |
| GET | `/api/agents/{name}/compatibility` | Compatibility report (`?include_ai=` forces fresh AI; STATIC live + persisted AI). Non-blocking; `unavailable` when stopped. See [Agent Compatibility Validation](#agent-compatibility-validation-668) (#668) |
| POST | `/api/agents/{name}/compatibility/fix` | Owner/admin; apply a gitignore auto-fix (`{check_id}`). 400 non-fixable, 409 concurrent fix. Uncommitted until next git sync (#668) |
| GET | `/api/agents/{name}/mcp-exposed` | MCP-exposure flag + the deterministic `tool_name` the MCP server would register. See [MCP Exposure](#mcp-exposure--dedicated-dynamic-tools-846) (#846) |
| PUT | `/api/agents/{name}/mcp-exposed` | Owner-only; toggle exposing the agent as a dedicated `chat_with_<slug>` MCP tool (`{enabled}`). System agent → 403. No restart — MCP server picks it up on its next poll (#846) |
| GET | `/api/agents/{name}/mcp-key` | The agent's own `scope='agent'` Trinity MCP key: prefix / scope / created / `last_used_at` / usage + a health state (`missing`\|`env_absent`\|`env_mismatch`\|`never_used`\|`stale`\|`active`\|`exempt`). Never the secret. `OwnedAgentByName` + `reject_non_interactive_principal` (#1854) |
| POST | `/api/agents/{name}/mcp-key/verify` | Container config-truth probe — one `docker exec` returning ONLY `sha256(bearer)` per `.mcp.json` entry; verdicts `ok`\|`foreign_user_key`\|`foreign_agent_key`\|`unknown_key`\|`not_configured`\|`shadow_entry`\|`unavailable` (stopped container degrades, never 500). Rate-limited (#1854) |
| POST | `/api/agents/{name}/mcp-key/regenerate` | Rotate the agent key: 409 for `trinity-system`/ephemeral before any mutation → fail-**closed** Redis lock (503 down, 409 contention) → capture-before-mint → reconcile `spawned_by_key_id` → deliver (running: `clear_agent_breakers` + recreate + exact-key post-condition; stopped: DB-only, stays stopped) → DELETE the *captured* superseded ids. **Returns metadata only, no plaintext.** Rate-limited per agent and per actor (#1854) |

**Note**: Route ordering is critical — static routes (`/context-stats`, `/autonomy-status`) must be defined BEFORE the `/{name}` catch-all (Invariant #4).

### Voice (6 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agents/{name}/voice/start` | Start Gemini Live voice session; `workspace_mode` enables panel tools. Resolves voice as request override → persisted `voice_name` → `Kore` (#28) |
| POST | `/api/agents/{name}/voice/stop` | Stop active voice session |
| GET/PUT | `/api/agents/{name}/voice/prompt` | Get/set per-agent voice system prompt |
| GET/PUT | `/api/agents/{name}/voice/name` | Get (any accessor; returns `available_voices`/`default_voice`) / set (owner-only; 400 on a voice outside `GEMINI_VOICE_NAMES`) the persisted per-agent Gemini voice. Applies to both the voice overlay/workspace and outbound VoIP calls (#28) |
| GET | `/api/agents/{name}/voice/{session_id}/panel` | Canvas panel state for workspace mode (ownership-gated; empty state when session gone, #699) |

### VoIP Telephony (VOIP-001, #1056 — flag-gated, default OFF)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET/PUT/DELETE | `/api/agents/{name}/voip` | Owner | Twilio-voice binding status / configure / remove. 404 when `voip_available` off. Re-PUT preserves the `enabled` flag (#28) |
| PUT | `/api/agents/{name}/voip/enabled` | Owner | Enable/disable the binding without re-entering credentials (`{enabled: bool}`); 404 when no binding exists. Disabled ⇒ outbound calls refused (#28) |
| POST | `/api/agents/{name}/voip/call` | JWT/MCP (`AuthorizedAgent`) | Place outbound call; rate-limited + daily-capped; optional `Idempotency-Key`. Returns `{call_id, status:"ringing", twilio_call_sid}` |
| WS | `/api/voip/voice/{call_id}` | Call-bound ticket | Twilio Media Streams audio bridge — see [VoIP](#voip-telephony-voip-001-1056) |

The per-agent VoIP config + voice-picker UI lives in the agent Settings/Sharing tab (`components/VoipChannelPanel.vue`), shown only when the platform `voip_available` flag is true (frontend reads it via `stores/sessions.js`); the underlying CRUD/voice endpoints are OSS and ungated (#28).

### Activities (1 endpoint)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/activities/timeline` | Cross-agent activity timeline. Params: `start_time`/`end_time` (ISO 8601), `activity_types` (comma-separated), `limit` (default 100). Returns only agents the user can access (owner, shared, or admin) |

### Credentials (CRED-002)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents/{name}/credentials/status` | Check credential files in agent |
| POST | `/api/agents/{name}/credentials/inject` | Write credential files directly to agent (`files` text + `files_b64` binary) |
| POST | `/api/agents/{name}/credentials/export` | Export to `.credentials.enc` (AES-256-GCM) |
| POST | `/api/agents/{name}/credentials/import` | Import from encrypted file |
| POST | `/api/internal/decrypt-and-inject` | Auto-import on agent startup (internal, no auth) |
| GET | `/api/agents/{name}/credential-requirements` | Per-variable credential checklist + live set/missing status. **Owner-only AND human-only** (`get_owned_agent_by_name` + `reject_agent_principal`) — the inventory is an operator surface, so the read gate equals the write gate it drives; the coarse `/credentials/status` keeps the read gate because it returns a count and names nothing. Rate-limited + single-flight (each uncached call spawns a container process against the shared 4-slot Docker pool); 200 with a degraded body for a stopped agent (ent#127) |

**Credential-path policy (#11):** injection accepts a **curated set of credential file types**, not a fixed 3-path list — the policy lives in `services/credential_paths.py` (`is_allowed_credential_path`), vendored **byte-identically** into `docker/base-image/agent_server/credential_paths.py` for the agent-server second layer (Invariant #5; parity test). Allows `.env`/`.credentials.enc`/`.mcp.json` (the last still content-validated, #598) + `.config/gcloud/**`, `.kube/config`, `*.pem`/`*.key`/`*.crt`/`*.cert`/`*.p12`/`*.pfx`, `.ssh/id_*`; deny-list (precedence) blocks anything executed/sourced at startup (shell rc, `CLAUDE.md`/`AGENTS.md`/`.claude/**`, `.mcp.json.template`, `.ssh/authorized_keys`/`config`, `.git*`, `bin/**`) plus `..`/absolute traversal. Binary creds round-trip as base64 (`files_b64`); the `.credentials.enc` archive is a v2 `{files, files_b64}` envelope (legacy flat archives still decrypt) and export captures the **full** injected set via the agent `GET /api/credentials/list`.

**Per-variable credential status (ent#127):** which declared credentials are actually SET is read by a fixed base64-injected `docker exec` probe (`services/credential_requirements_service.py`), NOT by an agent-server endpoint — so it works on the whole existing fleet without a base-image rebuild. **Nothing is vendored and there is no agent-server mirror**, so no Invariant #5 parity obligation attaches; the one policy crossing the boundary is the empty-value predicate, which is *defined* as agreement with the agent's own exporter (`agent_server/routers/credentials.py`) and pinned by a parity test. It is deliberately a SEPARATE probe from the #668 compatibility collector — that snapshot treats `.env` as existence-only by security design and its payload feeds AI checks, so value-derived data there would be a widening; do not "unify" them without re-reading this. The exec is bounded three ways (container-side `timeout`, `asyncio.wait_for`, `S_ISREG` before `open()`) because `execute_command_in_container`'s `timeout` argument is never forwarded and the call runs on the shared 4-slot Docker pool. **There is deliberately no MCP tool** — the credential inventory is a human-only operator surface (the endpoint rejects agent principals), the issue does not ask for one, and `get_credential_status` already covers coarse file-level status, so Invariant #13's three-surface cost buys nothing.

### GitHub PAT & Git (#347, #389, #384)
| Method | Path | Description |
|--------|------|-------------|
| GET/PUT/DELETE | `/api/agents/{name}/github-pat` | PAT config status / set per-agent PAT (validated, encrypted) / clear (revert to global) |
| GET/PUT | `/api/agents/{name}/git/auto-sync` | Per-agent 15-min auto-sync heartbeat flag |
| GET/PUT | `/api/agents/{name}/git/freeze-schedules-if-failing` | Freeze-on-sync-failure flag |
| GET | `/api/agents/{name}/git/sync-state` | Persisted sync-state row |
| POST | `/api/agents/{name}/git/reset-to-main-preserve-state` | Recovery reset — see [Git Sync Health](#git-sync-health-389390) |
| POST | `/api/agents/{name}/git/bind-to-own-repo` | **Bind to a repo the caller owns** (ent#109) — create it if needed, push the agent's CURRENT workspace history, repoint `origin`, persist the per-agent PAT, re-bake the container env. `OwnedAgentByName` **+ `reject_agent_principal`** (human-only), `Idempotency-Key` verb-folded. See [Post-Creation Repo Binding](#post-creation-repo-binding-ent109) |
| GET | `/api/agents/{name}/git/bind-to-own-repo/status` | Resolve a binding whose HTTP response was lost — reports the DB row vs the live container's `origin` (`origin_in_sync`) rather than a remembered request (ent#109) |
| GET | `/api/fleet/sync-audit` | Aggregate per-agent sync state + `duplicate_binding` flag (admins all; others accessible agents) |

### Templates (2 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/templates` / `/api/templates/{id}` | List templates / template details |

### System Manifests (ent#126)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/systems/manifests` | `require_role("creator")` | List the manifests bundled in `config/manifests/` (id, name, description, agent/schedule counts, templates, `sets_prompt`, `permissions_preset`, `valid`+`reason`, `already_deployed`). Fail-soft per file — a bad manifest is listed `valid: false`, never a 500 for the catalog. `valid` = parse + validate + the dry-run's own template/resource preflight |
| GET | `/api/systems/manifests/{manifest_id}` | `require_role("creator")` | The same summary plus the raw YAML, for loading into the install editor. 400 malformed id, 404 unknown |

**Note**: both routes MUST stay above `GET /{system_name}` **and** `GET /{system_name}/manifest` (Invariant #4 — two separate collisions; declared after them, `/manifests` 404s as "system 'manifests' not found"). `/api/systems/manifests` (bundled catalog) and `/api/systems/{name}/manifest` (export a **deployed** system) read alike and are unrelated.

### Sharing & Access Control (#311, #951)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agents/{name}/share` | Share agent |
| DELETE | `/api/agents/{name}/share/{email}` | Remove share |
| GET | `/api/agents/{name}/shares` | List shares |
| GET | `/api/agents/{name}/access` | Operator (Trinity-user) access roster for the **Access tab** (trinity-enterprise#17). Resolves each `agent_sharing` allow-list email against `users`: resolved → **active** operator (`username`/`role`/`last_active`), unresolved → **pending** invite. Read-only typed view over `agent_sharing`; add/remove reuse `/share` + `/share/{email}`. Drawing the operator-vs-client line on the read path is this endpoint's job (strict client roster is the Sharing redesign #18/#20) |
| GET | `/api/agents/{name}/clients` | External-client roster: channel users who've messaged the agent, aggregated across Telegram + WhatsApp, sorted by `last_active` desc (never-active last). Owner-only, read-only, DB-sourced (renders when agent stopped). Slack/VoIP additive (#20) |
| GET/PUT | `/api/agents/{name}/public-prompt` | Owner-only per-agent custom instructions (`public_channel_system_prompt`, 4000-char cap) folded into the system prompt for **public-facing surfaces only** — public links, channel router (Slack/Telegram/WhatsApp), x402 paid chat — via `platform_prompt_service.build_public_channel_caller_prompt` (composes with the MEM-001 memory block). NOT applied to authenticated chat, schedules, loops, or a2a. Text counterpart of `voice_system_prompt` (#1205) |
| GET/PUT | `/api/agents/{name}/access-policy` | Cross-channel access policy: `require_email` / `open_access` flags |
| GET | `/api/agents/{name}/access-requests` | Pending access requests |
| POST | `/api/agents/{name}/access-requests/{id}/decide` | Approve (auto-shares + fire-and-forget approval notification on the requester's originating channel for telegram/slack/whatsapp, #951) or reject |

### Schedules (13 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET/POST | `/api/agents/{name}/schedules` | List / create. POST 400 `schedule_timeout_exceeds_agent_cap` if `timeout_seconds` > agent cap (#929) |
| GET/PUT/DELETE | `/api/agents/{name}/schedules/{id}` | Get / update (same 400 on timeout) / soft-delete |
| POST | `/api/agents/{name}/schedules/{id}/enable` · `/disable` · `/trigger` | Enable / disable / manual trigger |
| GET | `/api/agents/{name}/schedules/{id}/executions` | Execution history |
| GET | `/api/agents/{name}/schedules/analytics-summary` | **Per-schedule performance rollups for the whole agent** in one call (#1115). `?window=` ∈ {7d,14d,30d}→168/336/720h (422 else). One row per **non-deleted** schedule (zero-run included): terminal `success_rate` (`None`→`—`), `avg_duration_ms` (NULL-skip), `cost_total`, `context_avg`, `tool_call_total`, last-run outcome. Backs both the Overview "Schedules performance" section and the Schedules-tab inline stats from one fetch. **Declared before `/{id}`** so `analytics-summary` isn't captured as a `schedule_id` (Invariant #4). DB `get_agent_schedules_summary`; tool-call totals over the newest 5,000 rows (`tool_calls_sampled`) |
| GET | `/api/agents/{name}/schedules/{id}/analytics` | Per-schedule analytics: counts, success rate, duration p50/p95/p99, cost, tool-call top-5, daily timeline. `?window_hours=` ∈ {24,168,720}, default 168 (#868). Percentiles Python-side over the newest 5,000 success rows (`sampled:true` when capped); counts + timeline full-set; UTC buckets gap-filled. Tenant boundary in the DB layer (`agent_name` passed through) — `AuthorizedAgent` validates only the path agent name, not that `schedule_id` belongs to it. Soft-deleted schedules 404 |
| POST/GET/DELETE | `/api/agents/{name}/schedules/{id}/webhook` | Generate/rotate token · status + URL · revoke (WEBHOOK-001) |

### Webhook Triggers (WEBHOOK-001)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/webhooks/{webhook_token}` | Token (URL-embedded) + optional HMAC | Trigger schedule execution; rate-limited 10/60s per token via `rate_limiter.py` (#1023); returns 202. When the schedule has signature auth on, an `X-Trinity-Signature: sha256=HMAC-SHA256(secret, raw_body)` header is required (fail-closed 401 on missing/invalid) (ent#77) |
| POST/DELETE | `/api/agents/{name}/schedules/{id}/webhook/secret` | JWT (`AuthorizedAgent`) | Enable/rotate signature auth — mints the signing secret, returns it **exactly once** (`whsec_…`, then only the AES-256-GCM envelope is kept) / disable auth, URL stays live (ent#77) |

Token lifecycle: `secrets.token_urlsafe(32)` stored in `agent_schedules.webhook_token` (partial unique index, O(1) lookup); re-POST rotates (old URL instantly invalid) and clears any signing secret; DELETE nulls (subsequent triggers 404). Optional `{"context": "..."}` body (max 4000 chars) appended to the schedule message wrapped in a framing header to reduce prompt-injection surface. All triggers audit-logged with `triggered_by="webhook"`; auto-derives idempotency key `(token, body_hash)` (Invariant #18).

**Signature auth (ent#77):** optional per-schedule HMAC layer so a leaked URL alone can't trigger the schedule — off by default. `POST .../webhook/secret` mints a `whsec_` secret (returned once; stored only as an AES-256-GCM envelope, Invariant #12), sets `webhook_auth_enabled`. The public trigger verifies `X-Trinity-Signature = sha256=HMAC-SHA256(secret, raw_body)` (`services/webhook_signature.py`, constant-time) after the body is read + size-capped, **fail-closed** (401 on missing/invalid, 500 on an unreadable stored secret — never a silent bypass). Rotating the URL or revoking clears the secret. Mint/rotate/disable are `AuthorizedAgent` (aligns with schedule management). UI: the Schedules-tab per-schedule **Webhook** panel (enable/reveal/copy URL, example `curl`, rotate/revoke, enable/rotate/disable signing, secret shown once).

**Creation gate (#1445):** schedule *and* webhook creation require a **live owning agent** — `db.is_agent_live(name)` checks an `agent_ownership` row with `deleted_at IS NULL` (no `users` join, so it matches the token-lookup predicate exactly). A nonexistent / soft-deleted agent returns **404** (non-owners get a uniform **403** whether or not the agent exists — no enumeration oracle); enforced at both the router (`create_schedule`/`generate_webhook`) and the db chokepoint (`db/schedules.py:create_schedule` → `None`). This closes the orphan-schedule class (an admin's `can_user_access_agent` is unconditionally `True`, so admin callers could otherwise mint a schedule + real token on a never-created agent) so a webhook token always resolves to a schedule of a live agent — the invariant the #1423 token-lookup INNER JOIN assumes.

### Auth, Users & MCP (15 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/auth/mode` | Auth mode config (unauthenticated) |
| POST | `/api/token` | Admin login — `username` accepts `admin` OR the admin's registered email + password (#82); form-encoded |
| POST | `/api/auth/email/request` / `/verify` | Request email code / verify and login |
| POST | `/api/auth/logout` | Revoke the caller's JWT — blacklists its `jti` in Redis until the token's own expiry, so an exfiltrated 7-day token dies on logout (#187). Idempotent; MCP-key callers are a no-op (keys revoke via key management) |
| GET | `/api/auth/validate` | Validate JWT (for nginx auth_request) — also rejects a `jti` revoked via logout (#187) |
| GET | `/api/users/me` | Current user |
| PUT | `/api/users/me/email` | Bind a sign-in email to the caller's own account (#82 transition; 409 if taken). No verification email sent |
| GET | `/api/users` | List users with roles (admin-only; exposes `suspended_at` read-only) (ROLE-001) |
| PUT | `/api/users/{username}/role` | Update user role (admin-only) |
| GET | `/api/mcp/info` | MCP server info |
| POST/GET/DELETE | `/api/mcp/keys` (`/{id}`) | Create / list / delete API keys |
| GET | `/oauth/{provider}/authorize` / `/callback` | OAuth start / callback |
| GET | `/health` | Health check (unauthenticated, top-level — no `/api/` prefix) |
| GET | `/api/version` | Platform version + build-time git provenance (`git_commit`, `git_commit_short`, `git_commit_subject`, `git_commit_timestamp`, `git_branch`, `build_date`) from Dockerfile ARG/ENV wired through compose build args + `start.sh`; all default `"unknown"` when absent (#926). Also `edition: "oss"\|"enterprise"` + `enterprise_features` — effective entitlement state from `entitlement_service.list_entitled_features()`, same source as feature-flags (#1443) |

### Soft-Delete Admin Recovery (#834 Phase 1c)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/admin/soft-deleted/agents` | Admin | List soft-deleted agents (newest first) with computed `purge_eta` (null if retention `0`); `limit` ≤ 500 |
| POST | `/api/admin/soft-deleted/agents/{name}/recover` | Admin | Clear `deleted_at`; 404 if not soft-deleted; container NOT recreated. Audit `agent_lifecycle:recover` |
| GET | `/api/admin/soft-deleted/schedules` | Admin | List soft-deleted schedules (optional `?agent_name=`); `purge_eta`; `limit` ≤ 500 |
| POST | `/api/admin/soft-deleted/schedules/{id}/recover` | Admin | Clear `deleted_at`; rejoins scheduler next poll if enabled. Audit `agent_lifecycle:schedule_recover` |

### Executions (EXEC-022)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/executions/stats` | Fleet stat cards: `total`/`success_count`/`failed_count`/`total_cost` windowed by `hours` (0 = all-time); `running_count`/`queued_count` always live. Optional `agent` filter |
| GET | `/api/executions` | Paginated fleet execution list. Filters: `status`, `triggered_by`, `hours`, `agent`, `search`; `limit` (max 200, default 50), `offset`; ordered `started_at DESC` |
| GET | `/api/executions/timeline` | **Bucketed** fleet rollups for the Grid's data tiles (ent#326) — the time-series sibling of `/stats`. `group_by` ∈ {`hour`,`day`,`trigger`,`agent`}, `hours` ∈ the shared window set, optional `agent`. Per bucket: count, success, failed, cost, `context_used`. `hour`/`day` are **gap-filled** on a continuous UTC axis (#1107 convention) and refuse `hours=0` — an all-time axis would emit one bucket per interval since the fleet's first execution. Both params 422 on an unknown value rather than coercing to a default: that degrade is right for a *filter* and wrong for an *axis*, which would silently redraw a window the caller never asked for. **`split=trigger` (ent#96)** adds a SECOND dimension over a time axis: each bucket also carries `by_trigger: {bucket: {total, failed}}` and the response carries `trigger_order` (the `_BUCKET_ORDER` the fold uses), so a stacked tile is one request and cannot hold a stale copy of the stack order. Per-bucket totals are re-summed from the split rows, so a column and its segments cannot disagree; gap-filled intervals carry `{}`, never a missing key. 422 when combined with a categorical `group_by` |

Access: admins see all; non-admins only owned/shared agents (`accessible_agent_names()` helper). Stats use single-pass conditional aggregation (one SQL query). `/stats` registered before `""` so the literal `"stats"` never routes as an execution ID. `/timeline` likewise (Invariant #4). Buckets slice the stored ISO-Z `started_at` with `substr` rather than a date function — dialect-agnostic across SQLite and PostgreSQL, and the same UTC the row was written with (Invariant #16). `trigger` folding happens in **Python** through `_TRIGGER_BUCKETS`, not a SQL CASE, so a newly-added trigger lands in the explicit `Other` catch-all instead of vanishing from a chart. **No token measure exists here**: `schedule_executions` carries `cost`/`context_used`/`context_max` and no usage-token column (`output_tokens` lives only on `chat_messages`, i.e. chat turns, not fleet executions), so the endpoint reports context-window **occupancy** under that name — ent#94's #101 tile must be labelled to match rather than presented as tokens consumed. **OSS-core by decision (ent#326):** this endpoint is deliberately **ungated** — no `requires_entitlement(...)`, logic stays in the OSS tree. Recorded explicitly because CLAUDE.md's default for an enterprise-tracker feature is *gated unless ruled otherwise*, so the ruling must not be inferred later from the mere fact that it merged. Rationale: it is generic fleet telemetry over OSS tables, and its consumer — the Dashboard Grid (ent#47) — already shipped OSS-core. "Can build in OSS" ≠ "should"; this one was weighed and answered OSS-core.

### First-Run Front Desk (ent#319)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/onboarding/first-run` | Any authenticated user | Is the caller still looking at a seed-only install, and which seeded agent should the "Show me" door open. `first_run` stays true while every visible agent is one Trinity seeded — the seed deploys under the admin account, so the seeded set is derived from the seeder's naming contract (`system_seed_service.seeded_agent_names()` + Cornelius), not from `audit_log` actors. DB-only (renders with containers down); never raises — a failure reads as `first_run: false` so the card stays hidden |

### Agent Overview Analytics (#1107)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/agents/{name}/analytics` | `AuthorizedAgent` | Multi-day execution analytics for the Overview tab. `?window=` ∈ {`7d`,`14d`,`30d`} (422 otherwise). Returns per-day counts stacked by type bucket, per-day + headline terminal success rate, duration avg (full-set) + p95 (sampled), avg context use, per-bucket totals, gap-filled UTC-day timeline |

Generalises #868 to agent scope (`db/schedules.py:get_agent_analytics`); read-only, DB-sourced (renders when the agent is stopped). Data-source discipline (locked by /autoplan review): all per-day series and headline `avg`/`context_avg` are **full-set** aggregates — never the capped pool (a sampled avg would be silently wrong on high-traffic agents); only headline p95 uses the newest 5,000 success rows (`sampled=true` when capped). `triggered_by` grouped in Python via `_TRIGGER_BUCKETS` (Chat/Tasks, MCP, Channels, Public, Scheduled, Loops, Agent-to-agent, Voice) with an explicit `Other` catch-all so a new trigger never silently vanishes (`manual` → Chat/Tasks; `loop` → Loops, #1150). `success_rate` is terminal-based; zero-terminal days report `null` so charts render a gap, not a false 0%; `context_avg` uses NULL-skipping AVG.

### Operator Queue (OPS-001)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/operator-queue` | List queue items (filters: status, type, priority, agent_name, since) |
| GET | `/api/operator-queue/stats` | Counts by status/type/priority/agent |
| POST | `/api/operator-queue/bulk-cancel` | Cancel listed pending items (`{ids: [...]}`, 1–500, ids-scoped so a sync race can't cancel unseen items); returns `{cancelled, skipped}`; audit-logged (#1017) |
| POST | `/api/operator-queue/clear-resolved` | Hide terminal rows (acknowledged/cancelled/expired) by setting `cleared_at` — NOT a DELETE (the 5s sync loop would resurrect items whose agent-file entry still says `pending`); `responded` kept visible until delivered; actual row deletion is the automatic retention sweep's job (`operator_queue_retention_days`, #1142); returns `{cleared}`; audit-logged (#1017) |
| GET | `/api/operator-queue/{id}` | Single item |
| POST | `/api/operator-queue/{id}/respond` / `/cancel` | Submit operator response / cancel pending item. Respond returns **409** if the item left `pending` under the caller (race vs bulk-cancel, #1017) |
| GET | `/api/operator-queue/agents/{name}` | Items for one agent |

Bulk ops scope writes to the caller's accessible agents (tri-state: admin = no filter, empty set = no-op). The sync service write-back also propagates `cancelled`/`expired` status into agent queue files (in-place flips of still-`pending` entries only) so agents stop waiting on cleared items and stale file entries can't resurrect purged rows (#1017). The Operations UI exposes these as a per-tab **Clear All** button (`notifications` tab uses `POST /api/notifications/dismiss-all` — bulk pending+acknowledged → dismissed, same accessible-set scoping).

WebSocket events: `operator_queue_new`, `operator_queue_responded`, `operator_queue_acknowledged`, `operator_queue_cleared` (one per bulk op, #1017; `notifications_cleared` for the notifications variant). Backed by the 5s Operator Queue Sync background service.

**Addressed asks (ent#364).** `operator_queue.addressed_to_email` names the HUMAN an item is for; `agent_name` only ever said which agent. NULL — every pre-ent#364 row — means "the operator answers", so the column is additive by construction. It is a **column and not a key in `context`** because it is an authorization decision (who may answer, whose Workspace sidebar it appears in) and `context` is agent-authored JSON whose clamp bounds size and type only. Validated at the one agent-authored boundary (`operator_queue_service._validated_addressee`, inside the #1632 clamp) against `client_portal.service.agent_on_roster(agent, email, include_owned=False)` — an agent may address only someone already on its roster — and **fails closed**: malformed, off-roster, or an unreadable roster all drop to NULL. The read/answer surface is **OSS core** (ent#428): `client_portal/asks/`, mounted unconditionally in `main.py` at `/api/enterprise/client-portal/asks` — a prefix that is retained history, like the `enterprise_`-prefixed portal tables, not a licensing claim. It re-checks the roster at read time so a revoked share stops showing an already-raised ask, projects an explicit client-facing shape (never `context`), and answers through the OSS respond path so write-back, audit fields and the WS broadcast are unforked. `responded_by_id` stays NULL for a client answerer — there is no `users` row, and inventing one would be a lie in the audit trail.

**Ingestion caps (#1632).** There is **no HTTP create** — every operator-queue item is created through `db.create_operator_queue_item`, and the only untrusted producer is the sync service reading the agent-authored `~/.trinity/operator-queue.json` (`operator_queue_service._sync_agent`). #1402 makes this queue the approval channel for irreversible actions, so a compromised / prompt-injected agent that floods plausible "approve this" items causes operator fatigue → reflexive approval (XSS is already handled by DOMPurify; the exposure is volume + social engineering). The one agent-authored seam is bounded by two independent limits plus field hygiene, all env-tunable (see requirements §26.7): (1) a per-agent **pending-DEPTH cap** (`db.count_operator_queue_pending_for_agent`, default 25) — the **primary** bound, DB-measured ⇒ Redis-independent; at the cap the sync **stops** ingesting (never drips a growing file) and holds the surplus behind one aggregated alert; (2) a per-agent + fleet **create RATE limit** (`rate_limiter.check`, 60/60s + 300/60s, fail-open); (3) a total field-hygiene clamp (`_clamp_ingested_item`, inside the #1525 create try/except) — truncate-with-marker title/question, context/options over cap → marker (validated `execution_id` only), non-dict context → `{}`, `created_at` normalized to ingest time, priority validate-only; (4) a **reserved-id guard** rejecting agent ids that start with a platform-reserved prefix (`queue-flood-`/`poison-`/`cb-dormant-`/`sync-failing-`/`git-bloat-`/`skill-not-found-`/`val_`/`system-seed-`/`base-image-stale-`/`alert-budget-`) so an agent can't pre-create — and via `on_conflict_do_nothing` silence — its own flood alarm or the #1402 poison alert, plus a malformed/oversize-id reject; (5) a per-cycle scan bound (skip an oversized file wholesale; cap at 500 requests/cycle). The 5s sync loop is **leader-locked** (`opqueue:leader`, mirror monitoring #1464) so `--workers 2` doesn't double-charge / double-broadcast / double-scan. Platform creates bypass `_sync_agent`, and the exemption is **scoped by influence, not by caller location (#1677)**: a *platform-only* emitter (volume bound by platform cadence — edge-triggered, idempotent/bucketed id, leader-locked, or operator-driven: lease-reaper poison-park #1402, `validation_service._notify_operator_on_failure` — converted to a **direct DB create** in #1632 — and the internal breaker/skill/git alerts) stays direct and unthrottled, while an *agent-influenceable* one — `task_execution_service._alert_skill_not_found` (#1410), whose per-command dedup distinct unknown slash-commands defeat at $0/turn — routes through `operator_queue_service.create_bounded_alert`: a per-(agent, registered-type) pending-DEPTH budget (`OPERATOR_ALERT_MAX_PENDING_PER_TYPE`, default 5; type derived from `item["type"]` against the `_BUDGETED_ALERT_TYPES` frozenset; **fail-closed on every arm** — an unreadable count / unregistered type / failed create suppresses the ALERT only, the FAILED execution rows stay the primary surface, and the paired notification is gated on the same bool), with ONE cooldown-gated `alert-budget-{agent}-{type}-b{bucket}` episode alert per window (deterministic bucket ⇒ the `(agent_name, request_id)` on-conflict target dedups cross-worker; no `held` count, no agent-controlled text). The classification is CI-forced by the AST caller-parity guard `tests/unit/test_1677_operator_alert_emitters.py` (every `create_operator_queue_item` call site must be allowlisted platform-only or routed; OSS tree only — the private submodule owns its twin); a sink-level default bound was rejected because it fails QUIETLY at the load-bearing poison-park create where the parity test fails LOUDLY at CI (#1890's lesson deliberately inverted). A generous DB-sink belt in `create_item` (reject title>4 KiB / question>16 KiB / context>64 KiB / id>512; the derived `execution_id` COLUMN belted to None on non-str/>512, #1677) is a second layer so the exemption is never solely load-bearing (#1525 validate-at-boundary-AND-at-sink). A depth-held / rate-skipped / oversize-file episode emits **one** `queue-flood-{agent}-{utc_now_iso()}` alert (un-guessable id, priority `high`, in-memory cooldown, emit-failure-safe). The platform-emitter residual named on the pull-mode default-ON gate list (#1081 / `TARGET_ARCHITECTURE.md`) is closed by #1677; the remaining gate items are unchanged.

### Respond → Resume Dispatch (ent#329)

An operator's answer to a parked `operator_queue` item is written back to the
agent's `~/.trinity/operator-queue.json` within ~5s, but it is only *processed*
at the agent's next turn. An agent with a schedule picks it up on its next tick;
an agent started by a one-shot webhook or chat task has no next tick, so an
approved action silently never executes until somebody re-triggers it by hand
(the limitation #1402 documented and told agents to work around in prose). This
is the platform-side fix, and the prerequisite the Workspace ask surface is gated
on (ent#364 AC #5).

- **Opt-in, per agent** (`agent_ownership.operator_resume_enabled`, default 0,
  read via `db/agent_settings/operator_resume.py`). A dispatch spends money, so
  it is never unconditional — a respond-storm must not fan out executions. The
  switch is on the AGENT and not on the item because a per-request `resume: true`
  the agent sets itself would let the agent decide that answering costs the
  answerer money, which is unacceptable once the answerer is an external
  Workspace client. Every fail path (missing row, soft-deleted agent, NULL
  column, unreadable flag) reads as OFF.
- **One dispatch surface.** `services/operator_resume_service.py` calls
  `task_execution_service.execute_task(triggered_by="operator_response")` — so
  capacity admission, the dispatch breaker, cost accounting, activity rows and
  the terminal appliers are the platform's existing ones. There is deliberately
  no workspace- or queue-specific execution path.
- **Hung off the CAS win.** `routers/operator_queue.py`'s respond endpoint
  raises 409 when the item left `pending` under the caller; the dispatch call
  sits after that raise, so an answer that was never recorded never spends
  (the #1083 rule that side effects follow the CAS result).
- **Idempotent** (Invariant #18): the key is `operator_resume:{item_id}:{sha256
  of the answer}` in the agent scope, so a replayed or double respond dispatches
  once.
- **Never silent.** A dispatch failure is a FAILED execution row plus an
  `operator_resume_dispatch` audit entry; the audit `details` carry the item id,
  execution id and status but **never the answer text** (that row is broadly
  readable and the answer is whatever a client typed).
- **Trigger registration.** `operator_response` is in all three trigger sets —
  `_VALID_TRIGGERS` (Executions filter), `_TRIGGER_BUCKETS` → its own
  "Operator queue" analytics bucket (unmapped triggers silently become "Other"),
  and `_AUTONOMOUS_TRIGGERS` (nobody reads a resume turn's reply, so an
  unresolved slash command earns an alert). It is **stranded** for pull mode:
  dispatched by a direct backend call, never by `POST /task`, so
  `_derive_task_trigger` cannot emit it (#2048).

The owner-facing toggle is in `components/ReliabilityPanel.vue`.

### Platform Audit Log (SEC-001)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/audit-log` | Admin | List entries (filters: event_type, actor_type, actor_id, target_type, target_id, source, start/end_time, `request_id` (#905 — joins an MCP `mcp_operation` row to the backend row it triggered), limit, offset) |
| GET | `/api/audit-log/stats` | Admin | Counts by event_type and actor_type |
| GET | `/api/audit-log/heatmap` | Admin | Day-of-week × hour-of-day sparse 7×24 grid; honors time + event/actor filters (#941) |
| GET | `/api/audit-log/calendar` | Admin | Per-day calendar heatmap (sparse `[{date, count}]`); same filters — *when* in calendar time vs the weekly pattern from `/heatmap` (#941) |
| GET | `/api/audit-log/{event_id}` | Admin | Single entry by UUID |
| GET | `/api/audit-log/distinct/event-types` / `/actor-types` | Admin | Distinct values for dashboard filter dropdowns (#941) |
| GET | `/api/audit-log/export` | Admin | Export time range as `json` or `csv` |
| POST | `/api/audit-log/verify` | Admin | Verify SHA-256 hash chain over `start_id..end_id` |
| POST | `/api/audit-log/hash-chain/enable` | Admin | Toggle hash-chain computation for new entries. **Persisted** in `system_settings` (`audit_hash_chain_enabled`) and resolved live per write (#2015) — it was in-memory only, so every restart silently switched the integrity control off and no restore ran at boot. The chain head is likewise read from the DB inside the insert's transaction rather than held per-process, so two workers cannot build two interleaved chains that `verify_chain` then reports as `tampered`. Fail-**closed**: a settings-read failure reads as OFF, unlike the fail-open feature flags — it decides whether an integrity record exists |
| POST | `/api/internal/audit` | Internal secret | Fire-and-forget write path for MCP tool-call audit |

Coverage: agent lifecycle, auth, sharing, credentials, settings, rename; request-ID middleware; MCP tool-call audit via a transparent wrapper (all 66+ tools, zero per-tool code). The wrapper centrally resolves each `mcp_operation` row's `target_id` (from the tool's `agent_name`/`name` param) and `request_id` (a per-call id a tool may stamp on the shared context, e.g. the git tools) — both previously dropped (#905). Storage: append-only `audit_log` table (see schema). `/api/audit-log` is the only audit surface (the old `/api/audit` Process Engine router is gone).

### Canary (CANARY-001)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/canary/violations` | Admin | List violations (filters: invariant_id, severity, tier, start/end_time, limit, offset) |
| GET | `/api/canary/violations/stats` | Admin | Counts by invariant_id and severity |
| GET | `/api/canary/violations/{id}` | Admin | Single violation |
| GET | `/api/canary/status` | Admin | Run-state of the harness (#2217): `{enabled, status: disabled\|healthy\|stale\|unknown, last_cycle_at, seconds_since_last_cycle, interval_seconds, stale_after_seconds, alert_sink_configured, redis_available}`. Reads the **shared** `canary:last_cycle_at` cursor; fail-open (disabled/Redis-error → never `stale`), so a default-OFF install never alarms. Distinguishes "harness OFF / not cycling" from "cycling, zero violations" — the H-01 gap one level up. Thin pass-through to `CanaryService.get_run_status()` (Invariant #1) |
| POST | `/api/canary/run-cycle` | Admin | Run one cycle on demand (same `CanaryService.run_cycle()` as the 5-min loop; optional invariant filter in body). Returns snapshot + violations + transitions; 409 `"cycle in progress"` when another cycle is mid-run — empty payload never silently returned. **Also advances `canary:last_cycle_at`**, so `/status` reports last-cycle across scheduled AND on-demand cycles |

### Nevermined Payments (NVM-001)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/paid/{agent_name}/chat` | x402 | Paid chat (402/403/200). Accepts `Idempotency-Key` (Invariant #18, #1018) keyed on `(payment-signature ∥ message)`. A settle that fails after retries keeps 200 + `response` but returns honest `status:"success_unsettled"` (was lying `"success"`); a concurrent effect-guard settle → `settle_in_progress:true`. A completed-unsettled replay re-drives settle + converges the snapshot (#1018) |
| GET | `/api/paid/{agent_name}/info` | None | Payment requirements |
| POST/GET/DELETE | `/api/nevermined/agents/{name}/config` | JWT | Configure / get / remove payments |
| PUT | `/api/nevermined/agents/{name}/config/toggle` | JWT | Enable/disable |
| GET | `/api/nevermined/agents/{name}/payments` | JWT | Payment history |
| GET | `/api/nevermined/settlement-failures` | Admin | Failed settlements |
| POST | `/api/nevermined/retry-settlement/{log_id}` | Admin | Retry settlement |

### Outbound File Sharing (FILES-001)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/files/{file_id}` | Token (`?sig=`) | Public download: 401 bad/missing sig, 404 unknown id, 410 revoked/expired; `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`; per-IP rate limit; audit `file_share_download` |
| POST | `/api/internal/agent-files/share` | `X-Internal-Secret` | Agent-server path to mint a download URL |

### MCP Inline Email Auth (#848 — flag-gated, default OFF)

All four require `X-Internal-Secret` **AND** `MCP_INLINE_AUTH_ENABLED`; with the flag off the whole surface **404s** (a disabled deploy does not advertise it). The secret authenticates the *caller*, never the *action* — the last two re-gate on the asserted email's own standing via `assert_email_may_reach_agent` → `db.email_has_agent_access(agent, email)` + connector-enabled, so a compromised MCP server cannot reach an agent that email cannot. See [mcp-connector.md](feature-flows/mcp-connector.md) and requirements `mcp.md` §7.6.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/internal/mcp-auth/request` | `X-Internal-Secret` + flag | Mail a 6-digit login code. **Always** the same constant 202 — known, unknown, malformed, rate-limited or backend-threw — with **no audit row** (wording, status, latency or an audit entry would each be an oracle for "is this address registered", #186). Not an open relay: a code is only generated for an address Trinity already knows, and that lookup **fails closed**. All branch-dependent work (known-check, cap read, code INSERT) runs in a `BackgroundTasks` task after the response flushes, because a committing write on one branch only measured ~1.9× |
| POST | `/api/internal/mcp-auth/verify` | `X-Internal-Secret` + flag | Verify the code; returns the verified email + the agents it may reach (a **selector** list, not a grant — every later call is re-gated). Rate limit is **account-scoped, never per-IP**: `client_ip` is always the MCP server, so a per-IP bucket would collapse all users into one and let 30 wrong codes lock inline login out fleet-wide (#591). Creates the user account if the email is whitelisted |
| GET | `/api/internal/mcp-auth/playbooks` | `X-Internal-Secret` + flag | Exposed-playbook allow-list for one `(agent, verified email)` pair; re-gated per call |
| POST | `/api/internal/mcp-auth/chat` | `X-Internal-Secret` + flag | Dispatch a playbook turn as the verified email; re-gated per call. Idempotency is scoped by `make_inline_auth_scope(agent, email)` — folding the **email** in, because MCP clients derive deterministic keys from call args, so two verified users of one shared agent would otherwise share a `(scope, key)` and the second would receive the first's response snapshot and `execution_id` (cross-user disclosure reachable by accident, not just malice) |

### Sequential Agent Loops (#740)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/agents/{name}/loops` | JWT/MCP | Start loop; 202 with `{loop_id, status, agent_name, max_runs}`. Body: `message` (template), `max_runs` (1–100, required), `stop_signal`, `delay_seconds`, `timeout_per_run`, `max_duration_seconds`, `max_cost_usd`, `no_progress_threshold` (0 disables; default 3; `1` → 422), `on_failure` (`abort` default \| `continue`, #1167), `max_consecutive_failures` (continue-mode cutoff, default 3), `model`, `allowed_tools` |
| GET | `/api/agents/{name}/loops` | JWT/MCP | List loops (`?status=`, `?limit=` 1–200 default 50) |
| GET | `/api/loops/{loop_id}` | JWT/MCP | Status + per-run summaries + last full response; 404 unknown, 403 if caller neither initiator nor agent-accessor |
| POST | `/api/loops/{loop_id}/stop` | JWT/MCP | Graceful stop → `{status: "stopping" \| "already_done"}` |

### Agent Self-Reminders (#1296)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/agents/{name}/reminders` | JWT/MCP (`AuthorizedAgent` + self-gate) | Create a one-shot self-reminder; 201 → reminder. Body: `message` (≤4000), `fire_at` (ISO) **XOR** `delay_seconds` (60..2592000), optional `model`/`timeout_seconds`/`allowed_tools`. Accepts `Idempotency-Key` (else auto-derived over raw input). 400 min/max-window + timeout>cap; 429 pending/daily/rate cap; 403 sibling/connector |
| GET | `/api/agents/{name}/reminders` | JWT/MCP (self-gate) | List reminders (`?status=pending` default; `all` for every status), soonest fire first |
| POST | `/api/agents/{name}/reminders/{id}/cancel` | JWT/MCP (self-gate) | CAS `pending → cancelled`, tenant-scoped; already-cancelled → 200 no-op; firing/fired/failed → 409; foreign/unknown id → uniform 404 |

### Platform Settings
| Method | Path | Description |
|--------|------|-------------|
| GET/PUT/DELETE | `/api/settings/template-registry` | Remote template-registry URL + toggle (TMPL-002, ent#14). GET admin-only (adds a live `status` block — fail-open makes a broken registry invisible in the catalog, so this is the only place an operator can see it, plus `hard_disabled` and `suppressed_by_github_templates`); PUT/DELETE admin **+ `reject_agent_principal`** (an agent-scoped key resolves to its owner *carrying the owner's role*, so a bare admin gate would let any agent repoint the platform's registry), URL SSRF-validated, audit-logged. All four keys 422-blocked on the generic `PUT`/`DELETE /{key}`. Registered before `/{key}` (Invariant #4) |
| GET/PUT/DELETE | `/api/settings/mcp-url` | Get (any auth user) / set / reset-to-auto-detect (admin-only) MCP server URL |
| GET | `/api/settings/feature-flags` | Public-safe UI gating flags (any auth user): `session_tab_enabled`, `voice_available` (`VOICE_ENABLED && GEMINI_API_KEY`), `workspace_available` (voice AND `WORKSPACE_ENABLED`, #860), `voip_available` (#1056), `brain_orb_available` (runtime-resolved: `system_settings` override → `BRAIN_ORB_ENABLED` env opt-in → OFF; gates the Brain Orb page — #58/#85), `brain_orb_voice_available` (`base && voice && GEMINI_API_KEY`, all but the key runtime-resolved, default OFF; gates the client-held voice tile — #60/#85), `mcp_agent_chat_pull_enabled` (#946 observability-only; routing gate is the MCP server's own `MCP_AGENT_CHAT_PULL_ENABLED`, default OFF), `redelivery_governor_enabled` (#1085 observability-only; default OFF), `canary_enabled` (#2217 observability-only — whether the canary harness is enabled; last-cycle/stale/sink detail stays admin-only on `GET /api/canary/status`; default OFF), `tts_available` (ent#117 — an ElevenLabs key resolves via stored setting → `ELEVENLABS_API_KEY` env; gates the voice config UI + `send_voice_reply`), `a2a_outbound_available` (#736 — the outbound-A2A kill switch: `system_settings` → `A2A_OUTBOUND_ENABLED` env → **OFF**; both call routes 404 when off, so this is observability/UI gating, not the enforcement), `enterprise_features` (registered enterprise modules; empty in OSS-only or `TRINITY_OSS_ONLY=1`) (#847) |
| POST | `/api/settings/retention/acknowledge` | Approve ONE over-threshold retention prune (#1644). Admin-only **and human-only** (`reject_agent_principal` — `require_admin` alone is insufficient: an agent-scoped key resolves to its owner carrying the owner's role, and the default install is admin-owned; see trinity-ops-agent#232). Body `{key, window_days}`; **409** unless `window_days` matches the window in force, so an ack always names the deletion it authorizes. Single-use — consumed once the prune runs. **This endpoint is the gate**; the operator-queue alarm authorizes nothing. Audit-logged |
| GET | `/api/settings/retention` | Effective data-retention windows + active edition (admin-only, #1039). Also reports the fixed `guard.max_rows` read-only (#1644 — not settings-backed). Reports log-archival, execution log/row, health-check, agent/schedule soft-delete, and the audit-log window (365-day floor, exempt). `edition` is `enterprise` when an entitled override is registered (via the #847 entitlement seam), else `community`. Precedence is **`db-row → code-default`** for the five OPS windows (env drives log archival only — the previously advertised `enterprise → env → community-default` was never implemented, #1638) plus a per-key `sources` map (`db-row`\|`code-default`). OSS does not hard-clamp; the 5-day floor applies to fresh installs via the seed — see [Cleanup Service sweeps](#background-services). **#2216:** also carries a `backup` block (last status/success/age, artifact count+bytes, `enabled`, `retention_days`, `min_keep`, `stale`, `scope: "same-disk"`) and **excludes `backup_retention_days` from the generic `windows` map** — its coercion is inverted (garbage → 14, never → `_ops_int`'s 0 = keep-forever), so it renders only through the service's one shared reader |
| GET/PUT | `/api/settings/agent-defaults/resources` | Fleet-wide default CPU/memory for new containers (admin-only; CPU 1/2/4/8/16, memory 1g–32g) (RES-001) |
| GET/PUT | `/api/settings/agent-defaults/access-policy` | Fleet-wide default `require_email` for new agents (admin-only, #1129). Stored in `system_settings`, **secure-by-default ON** (code fallback when unset — no migration); seeds `agent_ownership.require_email` at creation (`register_agent_owner`) for **new** agents only, never rewrites existing rows; owners still override per agent via `PUT /api/agents/{name}/access-policy` |
| GET/PUT | `/api/settings/max-parallel-tasks-ceiling` | Fleet-wide ceiling on per-agent `max_parallel_tasks` (admin-only, #506). Returns `{value, default, min, max}`; PUT range-validated 1–32 (400 otherwise), audit-logged. Stored in `system_settings` (no migration). The generic catch-all `PUT /{key}` is blocked for this key (422 → dedicated route). Clamp is runtime/clamp-on-use — see [Capacity & Backlog](#capacity--backlog-428) |
| GET | `/api/skills/assignments` | **Which agents hold each skill**, batched (ent#384). `get_current_user` **+ `reject_agent_principal`** + a `response_model` (`name`/`display_label` only — the ent#334 rule from this same router). Admin unfiltered; everyone else owned ∪ shared, with the accessible set derived from `db.get_all_agent_metadata()` (pure DB) rather than `accessible_agent_names`, whose `list_all_agents_fast()` returns `[]` on any Docker fault and would report *no agent holds any skill* fleet-wide behind a throttled WARNING. Carries `scope: all|accessible` so an empty accessible set is worded honestly instead of asserting zero holders. Excludes soft-deleted agents (#834 preserves their rows up to 180 days) and ephemeral ghosts. Also carries `assignable_agents` (ent#386) — the agents this caller may assign TO, a strictly different set from the holders above (holders are owned ∪ shared; the skill write routes are owner-or-admin), computed server-side so the browser holds no second copy of an authorization predicate, and filtered identically to the holder list so the dropdown can never offer an agent the chips could never show. **OSS-core by decision** — see below |
| GET/POST | `/api/skills/sources` | List / register skill sources (ent#237). Admin-only **and human-only** — `reject_agent_principal` on the mutations *and on the LIST read*, since the rows carry private repo URLs and an agent-scoped key resolves to its owner carrying the owner's role (ent#293). URLs locked to github.com and rejected for embedded credentials on write. See [Database Schema → skill_sources](#sqlite-datatrinitydb) |
| PUT/DELETE | `/api/skills/sources/{source_id}` | Patch (name/url/ref/ref_type/enabled/priority) / remove a source. Admin + `reject_agent_principal`. A `url`/`ref`/`ref_type` edit **clears the sync bookkeeping** (`last_commit_sha` + status/timestamp/error) — the tag pin's baseline is only meaningful for the ref it was recorded against. DELETE reclaims the source's checkout (row first, disk second); assignments are not cascaded — the skill keeps resolving through whatever source still provides it (ent#237) |
| POST | `/api/skills/sources/{source_id}/sync` | Sync ONE source, leaving the others untouched. Admin **and human-only** (`reject_agent_principal`) — a sync clones executable material and can spawn the fleet re-inject, so it is not "use". Runs off the event loop; **409** on lock contention, mirroring the full-sweep route (ent#237) |
| GET/PUT | `/api/settings/skills-library` | Skills-library lifecycle automation (admin-only, ent#236). GET: `auto_sync_enabled` / `auto_sync_interval_seconds` / `auto_reinject_enabled` + interval bounds, **plus** the durable sync status (`last_sync`, `last_sync_status`, `last_sync_error`) and the last fleet-re-inject report — the panel must be able to show a *failing* auto-sync. PUT: partial update (an omitted field is untouched), interval range-validated 300–86400 with a descriptive 400 rather than a silent clamp; audit-logged. The three keys are blocked on the generic `PUT /{key}` (unvalidated `Dict[str,str]`; `"10"` would be accepted verbatim and fetch GitHub six times a minute — #1644 class). Registered before `/{key}` (Invariant #4) |
| GET/PUT | `/api/settings/brain-orb` | Brain Orb platform flags (admin-only, trinity-enterprise#85). GET: per-flag `{value, source: override\|env\|default}` + `gemini_key_configured` (boolean only — never the key). PUT: partial booleans (`enabled`/`voice_enabled`/`write_enabled`) and/or `clear: [flag,…]` reverting a flag to its env/default (400 on unknown name or set+clear conflict); audit-logged with per-flag old→new. Stored in `system_settings` (no migration); route gates resolve at request time — no restart. Registered before `/{key}` (Invariant #4) — see [Brain Orb](#brain-orb--self-rendering-mind-page-58-trinity-enterprise) |
| GET/PUT | `/api/settings/elevenlabs` | ElevenLabs / voice platform settings (admin-only, ent#117). GET: `{key_configured, key_source: override\|env\|none, default_voice_id}` — the key value is never echoed. PUT: partial `{api_key?, default_voice_id?, clear: ["api_key"\|"default_voice_id"]}`; key stored AES-256-GCM encrypted (Invariant #12) in `system_settings`; runtime-resolved (no restart); audit-logged masked. Registered before `/{key}` (Invariant #4) |
| GET/PUT/DELETE | `/api/settings/a2a-endpoints` | The OSS outbound-A2A endpoint registry (#736) — the target source `call_a2a_agent` resolves names against. Admin **+ `reject_agent_principal`**: registering an endpoint decides where a credentialed server-side request may go, so it is the GRANT half of the grant-vs-use line, and an agent-scoped key resolves to its owner carrying the owner's role. Credentials are **write-only** (reads report `has_credentials` only); URL is SSRF-validated on write for the operator's sake, and re-validated on every call regardless. Stored as ONE AES-256-GCM envelope in `system_settings` — no table, no migration. A `ref` (id **or** name) resolves and deletes **first-match-wins** through one shared predicate, so DELETE removes exactly the record the same ref resolves to — never two (#2174: id/name are separate namespaces with no cross-uniqueness, so a filter-out-every-match delete could destroy a second endpoint and its credential while reporting one success); a new endpoint may not be *named* after an existing id, which stops the collision at the source without stranding an already-stored one. Blocked on the generic `PUT /{key}`; declared before `/{key}` (Invariant #4) |
| PUT/DELETE | `/api/settings/api-keys/{anthropic,github}`, `/api/settings/slack`, `/api/settings/slack/connect` | The credential writers. Unchanged in shape and auth (admin-only, masked reads, env fallback), but since ent#435 they persist through `settings_service.set_secret_setting` — AES-256-GCM under `<key>_encrypted`, never a cleartext row (Invariant #12). DELETE clears **both** forms, so unconfiguring a not-yet-migrated install is complete. The `source: settings\|env` field on the status reads comes from `has_secret_setting` (presence in either form, never a decrypt — so a row written under a rotated key still honestly reports *settings*). `slack_client_id` stays a plain row: it is a public OAuth identifier |

### Session Endpoints
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agents/{name}/session` | Create session row (first turn cold; writes JSONL so turn 2 resumes) |
| GET | `/api/agents/{name}/sessions` | List caller's sessions (per-user scoped; `?status=active`) |
| GET | `/api/agents/{name}/sessions/{id}` | Session row + most-recent `?limit=N` (default 100, max 500) messages |
| POST | `/api/agents/{name}/sessions/{id}/message` | The turn endpoint (`{message, model?, timeout_seconds?}`) — semantics in [Resumable Turns](#resumable-turns) |
| POST | `/api/agents/{name}/sessions/{id}/reset` | Clear cached UUID (next turn cold); best-effort JSONL reap |
| DELETE | `/api/agents/{name}/sessions/{id}` | Delete session + messages; best-effort JSONL reap |

### Workspace / Client Portal (epic ent#78; OSS core since ent#356)

The client-facing surface, mounted in **every** build (`src/backend/client_portal/`,
prefix `/api/enterprise/client-portal`, Vue at `/workspace`). A caller is either an
**external client** — a verified email with no `users` row, signing in with a 6-digit
code — or a **signed-in platform user**, who reaches the same surface in one click
because their platform session *is* the workspace session (ent#357). Both resolve
through `client_portal/portal_auth.py::get_portal_principal`, which returns
`(email, is_platform)`; the roster is every agent shared with that email, plus — for a
platform session only — the agents they own.

**Sign-out ends whichever credential is live — never a derivation of it (#2258).** The
implicit entry above runs the other way too: `isPlatformSession = !portalToken &&
isAuthenticated`, so clearing only the portal token is what *activates* the platform
fallback, and the Workspace's "Sign out" used to re-enter as the operator on refresh (or
re-authenticate a client as the co-resident operator). `stores/clientPortal.js::
signOutEverywhere()` ends the platform session (`authStore.logout()`) **first**, then
clears portal state, and routes by principal — operator → `/login`, client → the OTP
form. A persisted suppression flag was rejected on evidence: the JWT is an axios
**default** header and per-request headers merge over defaults, so a flag hides the
disclosure while every portal request still carries the operator's credential.
`auth.logout()` clears local state **before** the network revoke, because the global 401
interceptors and the `/login → /` router guard both key on it. `endSession({expired})`
deliberately does not end a platform session (expiry is not a user act). Residuals stated
in [workspace-session-signout.md](feature-flows/workspace-session-signout.md): a client
session that *expires* on a browser which later gained a platform login, and the portal
token's server-side validity post-sign-out (no self-service revoke; ent#281's primitive
is per-email).

**Membership is a DB fact; container state is a projection onto the card (#2196).** The
roster is built from `agent_ownership` / `agent_sharing` and is **never** filtered by
whether a container exists. A live ownership row with no container is a routine state
(#1747: identity lives in the row, and #834 Phase 1c recovery, a `docker system prune`
or a crash mid-create all reach it), so hiding those rows would make "not shared with
me" indistinguishable from "shared but containerless" on the one surface a client has.
Filtering is also the dangerous direction at scale: every Docker read in the platform
collapses *no container* and *Docker could not be asked* into one falsy value
(`list_all_agents_fast` returns `[]` on any fault), so one daemon restart or one
`DOCKER_GID` change would tell every paying customer they have no agents. Instead each
card carries `availability` (`ready`/`stopped`/`unavailable`/`unknown`), resolved once
per roster load in `get_roster` — **not** in `_roster_rows`, which stays pure SQL so
#2198's batch-sessions gate does not inherit a Docker read, and **not** inside
`_agent_briefing`, so #2163 stays free to defer/bound/cache the briefing. Invariant #11
is untouched: every Docker read still happens inside `docker_service.py`.

**The sidebar's thread list is one viewer-scoped call (#2198).**
`GET /api/enterprise/client-portal/sessions` returns every thread the caller has across
every agent on their roster. It replaced a literal N+1: the sidebar renders a merged,
cross-agent, recency-sorted list, so it asked the per-agent route once per rostered
agent — from six `refreshThreads()` call sites including every thread open and every
completed turn — and each of those re-resolved the roster before reading the session
table. The batch's tenant scope IS the roster: `agent_name IN (…)` populated from
`service.roster_agent_names(email, include_owned)`, the same set `agent_on_roster`
enforces, extracted so the two cannot drift (filtering on `client_email` alone would
re-surface threads for an un-shared agent). No agent parameter, so it is strictly less
enumerable than the route it replaces (Invariant #8); no schema change and no new index
(the existing `(agent_name, client_email, last_message_at)` gives it the same plan each
per-agent query already got); rate-limited per viewer, since it is no longer even
incidentally throttled by a browser connection cap. The one real trade is failure
granularity — one unreadable agent used to degrade alone, and the read is now
all-or-nothing — which is why the store returns its **last good list** on failure rather
than blanking, and `refreshThreads` catches both halves: an uncaught rejection there
aborts `bootstrap()` before `resolveAgentQuery()` and breaks Workspace deep-link landing.
Shipping it also closed the hole it would have amplified: `get_portal_principal`'s
platform branch now runs `reject_agent_principal`, so an agent-scoped MCP key — which
resolves to its owner carrying the owner's role — can no longer traverse the Workspace
as `is_platform=True` (it could previously read the owner's threads with agents the
calling agent holds no `agent_permissions` edge to, and this route would have made that
one call). User-scoped keys, `scope='system'` and portal session tokens are unaffected;
there is no legitimate agent caller of this surface (no MCP tool targets it, no agent
image calls it).

It was an entitled module and returned 404 in community builds; ent#356 moved it into
OSS core (adoption: this is the main surface a non-operator uses to work with agents).
The `/api/enterprise/client-portal` prefix and the `enterprise_portal_sessions` /
`enterprise_portal_messages` / `enterprise_client_blocks` table names are **retained
history, not licensing** — ent#83 shipped that prefix as the documented integration
surface for API-only clients, and renaming the tables would force a data migration on
every existing install. Tables are versioned on the OSS two-track runner
(`client_portal_tables_to_oss` + Alembic `0036_client_portal_oss`), both
`CREATE TABLE IF NOT EXISTS` so adopting them is a no-op where they already exist, and
both `agent_name` columns are registered in `AGENT_REFS` (CASCADE) — which the enterprise
track never was, so an agent rename used to strand a client's portal history.

**New-chat briefing hints (ent#138 / ent#380):** each roster card ships a briefing —
description + capability hint cards `playbooks[]{title,description,starter_prompt}` —
resolved best-effort at sign-in (`service.py::_agent_briefing`, from the agent's
`/api/template/info` + `/api/skills`), so the empty-chat screen renders with zero extra
fetches. The hint set is a **ladder**: the operator's exposed playbooks (connector
allow-list ∩ `user_invocable` — the same policy the MCP connector advertises) win
outright; an agent exposing none falls back to its template-declared `use_cases`
("What You Can Ask"), sanitized and capped (6 × 200 chars). Clicking a hint **pre-fills
the composer, never auto-sends** (`PortalBriefing.vue` → `prefill`). The future curated
exposable-skills config (ent#178) slots into this same seam. A chat holds one agent —
or, where the capability below is present, several — and the picker starts a new chat
either way, so hints scope to the active agent by construction. ent#380 also fixed the
briefing's metadata read — #138 called a nonexistent agent `/info` route, so descriptions
were silently always `None`.

**Briefing hint grid is bounded (#2101):** with no connector allow-list configured every
`user_invocable` skill becomes a "Things you can ask" card, so the hint set is belted
server-side (`_agent_briefing` ships ≤24 — one final slice at the return so it binds
whichever tier populated the list) and folded client-side (`PortalBriefing.vue` renders 6
described-cards-first via `portalUtils.planHintDisplay`, the rest behind a counted
in-place "Show all N" toggle — deliberately **no nested scroll region**: the chat pane
stays the single scroll axis, and the toggle counts the shipped list, never claiming the
agent's full skill set). Hint *curation* stays the connector allow-list (ent#178 later).

**Composer typeahead — `/` playbooks, `@` agents (ent#392).** The same briefing payload
now also feeds a composer typeahead, which is what makes it reachable after turn 1 (the
hint cards render on the empty-chat screen only). `/` lists the active agent's
`playbooks[]` and splices the `starter_prompt` in **without sending** — the ent#138
prefill contract; `@` lists reachable agents and inserts a token `mentionedAgents()`
resolves (ent#361). All decidable logic is pure and exported from
`components/portal/portalUtils.js` (`vitest` runs `environment: 'node'` with no
component-mount harness, so a decision inside a component is one no test can reach); the
components are dispatchers over it and `PortalTypeahead.vue` is presentational. Three
properties are load-bearing rather than stylistic: the **trigger rule is stricter than
the parser** (`MENTION_RE` is unanchored, so `user@example.com` parses as `@example` —
the popup must never open on something the parser would not see); **un-mentionable slugs
are excluded**, the predicate *derived* by asking `mentionedAgents` rather than copying
the grammar, because `sanitize_agent_name` keeps `.` and caps nothing while the grammar
allows neither, so listing `data.scout` would manufacture the silent
degrade-to-plain-text this feature exists to close; and **a plain Enter never accepts
without an explicit selection**, since an accidental accept destroys typed work while an
accidental send is what the user was reaching for. `@` is hidden — popup *and*
placeholder — without the rooms capability, which it reads from the roster payload per
the rule below. The **room** composer gets `@` scoped to its **agent participants** —
established by *observing* the running server rather than reading the private rooms
engine (`POST /api/rooms/{id}/messages` answered `woke: ["<participant>"]` for a
participant and `woke: []` for a non-participant), so the list contains only names a
pick is known to wake; whether a non-participant mention still joins someone by the
ent#361 engine-side path (§5.12) is deliberately not claimed either way, and recruiting
stays with the explicit "+ Add agent" control. `/`-in-room is deferred — a room has no
active-agent subject. **No backend change, no new endpoint, no
migration. OSS-core by decision (ent#392): deliberately ungated — no
`requires_entitlement`, logic stays in the OSS tree. Recorded explicitly because
CLAUDE.md's default for an enterprise-tracker feature is gated unless ruled otherwise, so
the ruling must never be inferred later from the mere fact that it merged.** See
[workspace-composer-typeahead.md](feature-flows/workspace-composer-typeahead.md).

**Voice conversation — one conversation, two modalities (ent#440).** The Workspace's
two manual voice controls — hold-to-dictate (#2212) and the speaker toggle (#2157)
— become one hands-free loop: the mic listens, the utterance is submitted as an
**ordinary Workspace turn**, the reply is spoken, the mic reopens. The load-bearing
property is that a spoken turn takes the SAME path a typed one does
(`submitUserText` → `deliver` → `POST .../chat/stream`), so shared context, history
parity and permission parity are true by construction rather than by
synchronisation — same portal session, same resumed Claude session, same
`enterprise_portal_messages` row, no new route and therefore no new gate. Bridging
the platform's Gemini Live session (VOICE-001, `routers/voice.py`) was **rejected**:
it answers with a different model holding a *summarised copy* of the thread and
writes its transcript back afterwards (a parallel conversation wearing the same
page), it cannot reach the agent's own tools/canvas, and its JWT-only WebSocket
cannot authenticate a portal client, which holds no `users` row. All decidable
rules are pure in `components/portal/voiceConversation.js` — transition table
(`start` idempotent; every event inert in `off`, so a `/stt` answer arriving after
teardown cannot restart a loop the user ended; `transcript` sends from exactly one
state; every exit releases the microphone), an RMS level meter for utterance
boundaries (1.2 s silence hold, 30 s cap, 15 s no-speech → stop **with a
sentence**; room tone re-listens rather than spending a transcription), barge-in at
a **higher** threshold than listening plus `echoCancellation` on capture (without
both, the mic hears the agent's narration through the speakers and it interrupts
itself), and `spokenReply()`, which cleans a reply for the ear (code fences → one
spoken sentence, links read as text, sentence-boundary cap) while the rendered
message is untouched. The control renders only where the loop can run; an agent
with no configured voice still converses, in text, and says so. **OSS-core by
decision (ent#440): deliberately ungated** — recorded explicitly because the
default for an enterprise-tracker feature is gated-unless-ruled-otherwise (the
ent#326/ent#384/ent#392 discipline). **No backend change, no new endpoint, no
migration.** See [workspace-voice-conversation.md](feature-flows/workspace-voice-conversation.md).

**The roster payload is *the* portal capability channel (#2128).** A portal principal
cannot read `GET /api/settings/feature-flags` — that endpoint is `get_current_user`-gated
and the frontend store behind it returns `[]` for any caller without a platform JWT, i.e.
for **every** external client, including on an instance where the capability is present.
So any UI gate on this surface takes its signal from `PortalRoster`, which
`get_portal_principal` already serves to both principal kinds and `Portal.vue::bootstrap()`
already awaits first — one field, no new route, no new auth surface, no extra round-trip
(`voice_available` is the per-agent precedent). Reach for this before adding a second
channel: the platform entitlement store is structurally unavailable here.
`multi_agent_chat_available` is the first such field — resolved once per roster load from
the entitlement registry, **fail-closed** (an unreadable registry reports the capability
absent, because promising an affordance that cannot work is the bug it fixes), and named
for the *capability* rather than the module or the edition, since this payload is served
to an operator's customer. When it is false the picker is single-select, all five room
store actions refuse before issuing a request, and `/workspace/r/:roomId` renders an
honest refusal instead of mounting the room; a 404/403 from any of the five self-heals the
flag mid-session, so a capability that lapses between roster load and confirm — or while a
room is open, which nothing else converges, since the sidebar refresh is event-driven — is
observed by the next room call rather than dead-ending. **The status alone is not the
signal**: a serving module authors its own refusals as a structured `detail: {code, …}`
(*you cannot reach that agent* → 403, *you are not in that room* → uniform 404), while
absence is a plain string — the framework's own "Not Found" for an unmounted route, the
entitlement gate's sentence for mounted-but-unlicensed. Only the string form lowers the
flag; a coded refusal is passed through so the server's own words reach the user, because
reading one denied request as absence would turn it into a session-long false claim about
the operator's build. The frontend gate is **UX, not
containment** — a portal token legitimately reaches the room endpoints where they exist,
and the real boundary is the serving module's own roster-scoped access plus
membership-scoped uniform 404s. Room data is untouched by the flag and reappears intact
if the capability returns.

**`availability` is the second per-agent field on this channel, and it fails in the
OPPOSITE direction (#2196).** `voice_available` and `multi_agent_chat_available` default
**False** (fail-closed) because their bug is *promising an affordance that cannot work*.
`availability` defaults **`"unknown"`** (fail-open) because its bug is the mirror image:
*denying a working agent*, and — since a Docker fault would mark every card at once —
*emptying a paying customer's roster over an infrastructure fault*. Same payload channel,
opposite default, for a stated reason; the asymmetry is deliberate and is written into
`PortalAgentCard` itself so it is not later "tidied" into consistency. It is resolved from
the tri-state pair `docker_service.agent_container_states()` (batch, one **sparse**
`containers.list()` per roster load) / `agent_container_state(name)` (single, for the
agent page and each turn — routing one agent through the batch would make it pay a
fleet-scale read, against #2160). Those two exist because no pre-existing Docker helper
can distinguish *no container* from *Docker unreadable*: both return a falsy value, which
is the single fact this design turns on. Both are awaited through a `docker_utils`
executor wrapper (that module's mandatory async contract), and `list_all_agents_fast`'s
`[]`-on-fault contract is deliberately left unchanged — ~60 stub sites depend on it. The
portal seams `_availability_map` / `_agent_availability` are isinstance/enum-guarded (the
`a2a_outbound` precedent, here failing in the safe direction) so a `sys.modules` MagicMock
stub degrades to `unknown` rather than silently inverting the default inside the suite
meant to prove it; `_availability_map` also narrows its result to the requested names,
because the underlying call sees **every** agent container on the host.

### Multi-Agent Rooms (ent#169; OSS core since ent#443)

`src/backend/shared_sessions/` — the substrate behind a Workspace chat that holds
more than one agent. **One idea:** *a room is a shared persistent RECORD, never a
shared CONTEXT.* Each agent keeps its own isolated Claude session and, before it
speaks, is handed only the transcript it has not seen (`participants.last_read_seq`);
that is why a room does not cost N× tokens and why no LLM has to decide who talks
next — turn-taking is mechanical: **you are woken iff you were @mentioned**.

- **Two routers, mounted unconditionally in `main.py`**: `/api/rooms` (membership-scoped;
  any authenticated principal, and a Workspace client via the `get_room_principal`
  fallback, ent#362) and `/api/enterprise/room-budget-defaults` (admin-only operator
  defaults, ent#387). The second is deliberately NOT under `/api/rooms` — a
  `/budget-defaults` path there would sit beside `/{room_id}`, one ordering slip from
  being read as a room id (Invariant #4) on the one surface whose reader must never be
  a client.
- **Turn engine** (`service.py::post_message` → `_wake_agent`): mentions resolve against
  participants; each woken agent runs an **ordinary** `execute_task(triggered_by="room")`,
  so slots, the circuit breaker, cost and observability come for free, and its reply is
  auto-posted back. An agent never re-wakes itself; only a **human** mention recruits a
  non-participant (an agent that could pull agents in is a spend amplifier and a
  prompt-injection lever). Chain depth, a per-participant wake cap, and the ent#220
  cancellation shield bound the cascade; the ent#218 rule keeps an in-flight reply that
  was already billed from being discarded by a budget trip.
- **Three tables** — `enterprise_rooms`, `enterprise_room_participants`,
  `enterprise_room_messages`. The `enterprise_` prefix is **retained history, not a
  licensing claim** (the ent#356 portal precedent): every entitled install already holds
  live transcripts under those names, so renaming them would be exactly the data
  migration the move forbids. DDL in `db/schema.py`, versioned on the OSS two-track
  runner (`db/migrations.py::shared_sessions_tables_to_oss` + Alembic
  `0044_shared_sessions_oss`), both `CREATE TABLE IF NOT EXISTS` so adoption is a no-op
  on an install that already has them. The enterprise Alembic `0011_shared_sessions`
  stays on its own line — deleting it would break that chain — and is idempotent.
  The revision chains off **`0038_portal_chat_state`, `main`'s head** — this landed as a
  hotfix onto `main`, and 0039-0043 exist only on `dev`, so pointing at `dev`'s head
  would name an absent revision and fail boot on the line it ships to. That forks the
  two lines at 0038 by construction; the fork surfaces as two heads at the main→dev
  back-merge, where `check_alembic_heads` fails loudly until an `alembic merge` revision
  (`down_revision = ("0043_subscription_headroom_history", "0044_shared_sessions_oss")`)
  collapses it — the fix is that merge revision, never renumbering a revision already
  applied wherever the hotfix went. The file is numbered 0044 rather than 0039 so its
  prefix does not collide with `dev`'s `0039_operator_queue_addressed_to` after the
  back-merge (ids are strings, but the numeric prefix is the graph's only human ordering
  cue); `test_ent443_rooms_oss_core.py` pins both the parent and prefix-uniqueness.
- **Agent-identity columns are POLYMORPHIC and registered kind-scoped** (ent#443):
  `participants.identity` and `messages.sender_identity` hold an agent name, a platform
  user id, or a workspace client's verified email depending on the sibling `kind` /
  `sender_kind`. Both are in `AGENT_REFS` with an `extra_filter` (`kind = 'agent'`), so
  rename re-keys and purge cascades **only** the agent rows; an unscoped ref would
  rewrite — and on purge delete — a human participant whose id or email happened to
  equal the agent's name. The forward parity regex cannot see either column
  (`identity` is too generic to add to `_AGENT_ID_COLUMNS`), so
  `tests/unit/test_ent443_rooms_oss_core.py` pins them explicitly and
  `test_agent_cleanup_parity.py` carries a documented `_POLYMORPHIC_AGENT_COLUMNS` set
  for the backward direction.
- **Why OSS.** It was the entitled `shared_sessions` module, 404ing in community builds
  — while the frontend that drives it (`components/rooms/`, `stores/rooms.js`, the ent#392
  composer typeahead) and the MCP tools (`src/mcp-server/src/tools/rooms.ts`) shipped in
  **every** build and self-disabled. Three of four surfaces were already public, so gating
  only the backend left an OSS install rendering an affordance it then refused. Workspace
  itself moved for the same adoption reason (ent#356), and rooms are the half that makes
  it the place people work with agents rather than a second 1:1 chat.
- **`PortalRoster.multi_agent_chat_available` stays on the payload** and is now
  unconditionally true. It is the portal's ONLY capability channel (#2128) — a portal
  principal cannot read `/api/settings/feature-flags` — and the shipped bundle gates the
  picker, five room store actions and `/workspace/r/:roomId` on it, so deleting the field
  would make all of them read `undefined` and hide the feature this move exposes.
- **Transition ordering (load-bearing):** the OSS routers are included in `main.py`
  **before** `register_enterprise(app)`, so on an install whose submodule has not yet been
  bumped both routers mount and the **ungated OSS one wins** the match order. Pinned by
  `test_ent443_rooms_oss_core.py`.

### Enterprise Modules (#847)

Open-core seam (generic mechanism only). The public backend exposes an extension point: `main.py` conditionally `register_enterprise(app)` (no-op `ImportError` in OSS-only builds); each registered module calls `entitlement_service.register_module("<id>")`, and the registry drives `feature-flags → enterprise_features`, which the OSS Vue bundle reads to show/hide gated surfaces. `requires_entitlement("<id>")` in `dependencies.py` gates an entitled endpoint (403 unentitled; 404 when the submodule is absent). `TRINITY_OSS_ONLY=1` hard-empties the registry. Private enterprise tables migrate via the separate two-track runner (Invariant #3).

Install/verification surface (#1443): both private submodules carry `update = none` in `.gitmodules` — OSS clones init without credentials; mounting is a config-first per-clone opt-in (`git config submodule.<path>.update checkout`, then init) documented in `docs/ENTERPRISE.md` (mount, HTTPS-PAT override, rebuild, verify). `GET /api/version` reports `edition` + `enterprise_features` from the same registry.

> The catalog of specific enterprise modules, their private schema, and the commercial rationale are intentionally **not** documented in this public repo — they live in the private `trinity-enterprise` repository (see `docs/memory/ENTERPRISE_DOCS.md` there). Public docs describe the generic seam only.

## Architectural Invariants

These are structural patterns that must be preserved. Breaking them causes cascading issues.

1. **Three-Layer Backend: Router → Service → DB** — Every feature follows `routers/X.py` → `services/X_service.py` → `db/X.py`. Routers hold no business logic, services hold no SQL, db modules hold no HTTP concerns.

2. **DB Layer: Class-per-domain with Mixin Composition** — Each `db/` file defines an `XOperations` class. Agent-specific settings use mixins (`db/agent_settings/`) composed into `AgentOperations`. New agent settings → new mixin, not a bigger class. A larger domain follows the same shape when a single file grows unreviewable: `ScheduleOperations` is composed in `db/schedules/__init__.py` (#1481) from ten concern-scoped mixins — `ScheduleCommonMixin` (MRO-shared `_generate_id` + the `_norm_ts` leaf helper) / `ScheduleCrudMixin` / `ScheduleWebhooksMixin` / `ScheduleExecutionsMixin` / `ScheduleQueueMixin` / `ScheduleCleanupMixin` / `ScheduleAnalyticsMixin` / `ScheduleStatsMixin` / `ScheduleGitConfigMixin` / `ScheduleRetentionMixin`. The facade import path `from db.schedules import ScheduleOperations` is preserved by the package re-export, and cross-slice references resolve at runtime via the composed class's MRO (`self.<method>()`) with **no** import edges between the mixin files — the only intra-package import is the bare `_norm_ts` module-global.

3. **Schema in `db/schema.py`, Migrations in `db/migrations.py`** — All OSS table DDL lives in `schema.py`. Schema changes require a versioned migration in `migrations.py` (tracked in the `schema_migrations` table). Never create tables ad-hoc in service code. **Runner safety (#1160):** `init_database()` wraps both migration passes + `init_schema` in a cross-process `flock` (`db/migration_lock.py`) so workers + scheduler can't race; table-rebuild migrations use `_atomic_rebuild` (rename-swap inside `BEGIN`/`COMMIT`) so a crash mid-rebuild rolls back; a failed migration is named via `add_note` and surfaced as `first_pending` in the `/health` 503. **Backend split (#1183):** the `db/migrations.py` runner (PRAGMA + `INSERT OR IGNORE`) is **SQLite-only**; PostgreSQL is owned by **Alembic** — `init_database()`'s non-SQLite branch calls `db/alembic_runner.upgrade_to_head()` (`src/backend/migrations/` + `alembic.ini`; `env.py` targets `db/tables.py` MetaData). Fresh PG built by the `0001_baseline` revision (reuses `init_schema_postgres` DDL); pre-Alembic PG stamped at baseline. **Multi-worker serialisation (#1425):** the `flock` above only guards the SQLite runner — on PG, `upgrade_to_head()` wraps its stamp+upgrade in a **PostgreSQL session advisory lock** (`pg_advisory_lock`, fixed key) so concurrent worker/scheduler boots can't both enter `command.upgrade()` and deadlock inside a revision. **Revision-id width (#1420):** `alembic_version.version_num` is `VARCHAR(255)` (env.py `version_table_column_type` + `0001_baseline` DDL; the `0008a_widen_alembic_version` migration widens existing 32-wide DBs) because Trinity's descriptive `NNNN_<table>_<change>` ids exceed Alembic's 32-char default and PostgreSQL enforces the width (SQLite doesn't — so the truncation only breaks PG boot). Keep revision ids ≤255; the `pg-migrations` CI job runs a real `alembic upgrade head` and `tests/unit/test_alembic_revision_id_length.py` lints the ids. **One head per version-line (#2068):** `scripts/ci/check_alembic_heads.py` (pure stdlib, parses `revision`/`down_revision` without importing anything; wired **unconditionally** into `schema-parity`, so unlike path-filtered-and-advisory `pg-migrations` it is a required, always-evaluated gate) fails any version-line it is pointed at that does not resolve to exactly one head. Two revisions sharing a `down_revision` are two heads, and since `upgrade head` is **singular** and resolves its target *before* applying anything, such a graph applies **zero** revisions — every revision merged since the fork stops arriving, not only the one that forked — while git reports no conflict, because each file is individually valid and the defect exists only in the relationship. A merge revision's tuple `down_revision` counts all its members, so `alembic merge` correctly collapses a fork; the guard skips (loudly) a directory that is absent or holds no revisions, and fails **closed** when revision files are present but none parses. The private track's version-line is asserted the same way at its own branch point — that arm skips on public CI, which never checks the submodule out — and `deploy-dev` greps the registration boot log, so a version-line that merely *degrades* rather than crash-loops cannot ship green. Both coexist during transition, so a schema change lands in **both** `migrations.py` (SQLite) and a new Alembic revision (Postgres) — both tracks CI-guarded by the `schema-parity` job (SQLite parity pytest + the `scripts/ci/check_alembic_parity.py` cross-track guard that fails a DDL change missing its Alembic revision, #1342) — until SQLite **end-of-support September 1, 2026** (#1278; guide `docs/migrations/SQLITE_TO_POSTGRES.md`) — after which the goal (#746) is `tables.py` MetaData as single source with autogenerated revisions. **Two-track (open-core):** enterprise owns only `enterprise_*` tables via a **separate** runner (`enterprise/backend/_migrations.py`, tracked in `enterprise_schema_migrations`, never OSS `schema_migrations`); one file per migration (`NNNN_slug.py` with `NAME` + `upgrade(cursor, conn)`, filename order). Enterprise migrations may FK-into OSS tables but must **never ALTER** one — OSS enforcement goes through an OSS migration as an edition-agnostic primitive (e.g. `users.suspended_at`, #995). The enterprise runner runs from `register_enterprise` *after* OSS `init_database`.

4. **Router Registration Order Matters** — In `main.py`, static routes like `/api/agents/context-stats` must come before `/{name}` catch-all. New collection-level agent endpoints must be registered before parameterized routes.

5. **Agent Server Mirrors Backend (Subset)** — `docker/base-image/agent_server/routers/` has routers that mirror a subset of backend routers (chat, credentials, files, git, skills, dashboard). The backend proxies to the agent server. Changes to agent-internal APIs must update both sides. **Byte-identical vendored mirrors** (the agent server ships as its own image and structurally cannot import `src/backend`, so a shared *policy* is copied rather than imported, each with a parity test): `credential_paths.py` (#11), `model_context.py` (#1521), `safe_yaml.py` (#1965 — the ent#314 hardened YAML loader), and `mcp_validator.py` (#2007). **A guard that walks only one of the two trees is not a guard**: ent#314's AST scan had an empty allowlist over the whole backend and still missed six bare `yaml.safe_load` calls in the agent server on the same author-controlled documents (`template.yaml`, skill frontmatter, `dashboard.yaml`, `.trinity/persistent-state.yaml`), because it never looked there. It now walks `docker/base-image/agent_server/` too — extend the scan, not just the fix, whenever a policy gains a second home.

6. **Frontend: Store = Domain, View = Page** — Pinia stores (`stores/agents.js`) are domain-scoped, not view-scoped. Views compose from multiple stores. Composables (`composables/use*.js`) extract reusable logic. API calls go through stores, not views directly.

7. **Single API Client (`api.js`)** — One Axios instance with auth interceptor. Stores call `api.get()`/`api.post()`. No raw `fetch()` or duplicate Axios instances.

8. **Auth Pattern: `Depends(get_current_user)` + `AuthorizedAgent`** — Every authenticated endpoint uses FastAPI `Depends()` for auth. Agent-scoped endpoints use `AuthorizedAgent` or `OwnedAgentByName` for access control. Role-gated endpoints use `require_role("creator")` or `require_admin` (ROLE-001). `internal.py` is the only exception (no auth, for agent-to-backend calls). **Enumeration-safety (self-uniform, #186):** an agent-access handler must be *self-uniform* — it must **never** return an existence-`404` followed by an access-`403` in the same function (that differential lets any caller enumerate which agents exist). The four dependency helpers (`get_authorized_agent`/`get_owned_agent`/`…_by_name`, `dependencies.py`) now return a **uniform 404** for both a non-existent AND an inaccessible/unowned agent — they evaluate existence and access **before** branching (equal query-count → equal timing) and run `_enforce_connector_scope` first. Access-first inline handlers (`can_user_access/share` → 403 before any existence lookup) are already self-uniform and stay **403**. When adding an agent endpoint: route through a dependency, or if checking inline, check access first — do **not** reintroduce a 404-then-403 split. The MCP third surface mirrors this (Invariant #13): `chat.ts checkAgentAccess` returns one uniform reason and never discloses the owner username. Guarded by `tests/unit/test_186_enumeration_uniformity.py`. **Shared imperative-guard family (#1310):** the inline access/owner/admin checks are consolidated behind five leaf helpers in `dependencies.py` — `assert_admin` (403; `_reject_connector_principal` + `reject_agent_principal` + `role != "admin"`), `assert_agent_access` (403; `_enforce_connector_scope` + `can_user_access_agent`), `assert_agent_owner` (403; `_enforce_connector_scope(owner_op=True)` + `can_user_share_agent`), `assert_owns_or_admin` (403; `id != owner AND role != "admin"`), `assert_owns` (403; `id != owner`, **no admin bypass**). All raise **403** (access-first → self-uniform, above). **An admin gate is never agent-callable (ent#293/ent#297):** `get_current_user` resolves an agent-scoped MCP key to its owner **carrying the owner's role**, so on a default admin-owned install any non-ephemeral agent's injected `TRINITY_MCP_API_KEY` satisfied every admin gate. Five occurrences of that one class — trinity-ops-agent#232, #1644 (retention acknowledge), #1816 (system-agent restart), ent#236 and ent#293 (skills-library repointing) — were each closed by bolting `reject_agent_principal` onto one more endpoint, 18 of them against 114 admin-gated call sites. Five occurrences meant the **gate** was wrong, not the endpoints, so **`require_admin` and `assert_admin` now reject agent principals themselves** (#1890): a route added tomorrow inherits the protection without anyone remembering to ask, and the per-endpoint `reject_agent_principal` calls are belt-and-braces rather than the mechanism. Safe by construction and verified, not assumed — the agent-key flows that must keep working (heartbeat, structured reports, the #1083 result callback) authorize on `current_user.agent_name` self-checks and never touch an admin gate, and `User.agent_name` is populated only for `scope == "agent"`, so `trinity-system` still passes. This is the grant-vs-use line: the endpoint that **uses** a capability may be agent-callable; the endpoint that **grants** one is human-only. Two admin-gated reads that were *uses*, not grants, were re-opened rather than left half-working — `POST /api/monitoring/agents/{name}/check` drops to `get_current_user` (the admin gate was redundant with `AuthorizedAgentByName`) and `GET /api/subscriptions` is **owner-scoped, not un-gated** (un-gating would hand any `role=user` a fleet-wide `owner_email` + agent-name enumeration oracle — the Invariant #8 disclosure class, reintroduced by a fix for a different disclosure). **Never write `require_role("admin")`** — it is a third spelling that rejects connector but *not* agent principals, and `require_role` stays deliberately permissive because agent-spawned agent creation runs through `require_role("creator")` (ent#69 Part 2), so a blanket rejection there would break ghost spawning. Use `require_admin` (equivalent — `admin` is last in `ROLE_HIERARCHY` — and it rejects agent principals); an AST guard in `tests/unit/test_293_admin_gate_rejects_agent_keys.py` fails the build if the spelling reappears. The trigger to revisit an existing gate is a change in what the endpoint **does** — escalating a handler's destructiveness silently re-prices every principal that could already reach it. **Imperative vs path-dependency:** agent name *in the path* → prefer the path-dependency (uniform-404); agent name *derived from a resolved resource* (session/notification/subscription/execution row) or a *composite* gate → use the imperative helper; both run `_enforce_connector_scope` first so the connector boundary is enforced identically. **`assert_agent_owner` ≠ delete-authorization** — it wraps the owner-or-admin `can_user_share_agent`, NOT the `is_system`-guarded `can_user_delete_agent`; delete paths keep the delete predicate. Permanent exceptions (own intentional-404 helpers, enumeration-safe by construction): `nevermined._require_read_access`/`_require_write_access`, `reports.get_report`, `sessions._session_or_404` (compound 404). **The admin gate is an ALLOWLIST, not two named enemies (#2323).** ent#293/#297 closed it against `agent` and `connector` **by name**, which is a denylist over a free-text column: `mcp_api_keys.scope` carries no CHECK constraint, and any scope setting neither `agent_name` nor `connector_agent` walks both rejections and inherits the owner's role across ~163 admin-gated sites. `models.User.mcp_scope`'s own docstring predicted it — *"fail-closed against a sixth scope a future PR invents"* — and #2323's `ops` scope was that sixth. Both gates now additionally require `mcp_scope ∈ ADMIN_GATE_SCOPES = {None, "user", "system"}`: `None` is the JWT human the role check was written for, `user` legitimately drives admin endpoints today (ops dashboards, `trinity-ops-agent`) so removing it would be a behaviour change, and `system` is `trinity-system`. Zero behaviour change over the live domain except `portal_delegate`, which passed both named guards and was contained **only** by its route fence — it now has a second layer, unreachable in normal operation and firing only if that fence is holed. A principal object lacking `mcp_scope` entirely fails **closed** via a dedicated sentinel — never `getattr(..., None)`, which would make the absent attribute the *privileged* JWT value (the documented getattr-discriminator trap); `models.User` always declares the field, so the only objects this rejects are stand-ins that do not match the real principal. `assert_admin` takes `allow_scopes=` so an individual endpoint can opt a bounded scope in (`allow_scopes={"ops"}` on the ops reads) — the grant that makes a machine credential authorized by **what it is** rather than by who owns it, and which flips the failure direction so a new ops route is inaccessible until opted in rather than silently reachable. `require_role` is deliberately **not** consulted (agent-spawned creation through `require_role("creator")` is supported ent#69 behaviour). Static guard: `tests/unit/test_1310_auth_wiring.py`; behavioral proof: `tests/unit/test_1310_auth_consolidation.py`; allowlist + future-scope proof: `tests/unit/test_293_admin_gate_rejects_agent_keys.py`.

9. **Channel Adapter ABC** — External messaging (Slack, Telegram, WhatsApp/Twilio) follows `adapters/base.py` → `ChannelAdapter` ABC with `NormalizedMessage` and `ChannelResponse`. New channels must implement this interface.

10. **WebSocket Events for Real-Time** — All real-time updates go through WebSocket broadcast (`agent_activity`, `agent_collaboration`). Frontend subscribes via `utils/websocket.js`. Don't poll for state that should be pushed. Transport is the Redis Streams event bus in `services/event_bus.py` (RELIABILITY-003, #306) — `ConnectionManager` / `FilteredWebSocketManager` are thin shims that `XADD` to `trinity:events`; the `StreamDispatcher` runs one `XREAD BLOCK` per backend process and fans out to registered clients. New broadcast sites should continue calling the existing `manager.broadcast(...)` / `filtered_manager.broadcast_filtered(...)` API — do not bypass it to publish directly.

11. **Docker as Source of Truth** — Agent container state comes from Docker labels (`trinity.*`), not from an in-memory registry. `docker_service.py` is the single point of Docker interaction.

12. **Credentials: File Injection, Never Stored in DB as Plaintext** — Credentials use `.env` files injected into containers (CRED-002). Encrypted exports use AES-256-GCM (`.credentials.enc`). Redis holds transient secrets. **Exception with mandatory encryption**: channel bot/auth tokens (Slack, Telegram, WhatsApp) and subscription/Nevermined OAuth tokens are persisted in SQLite because they drive long-lived background processes (webhook receivers, scheduled bots) that can't depend on container env vars. These MUST be wrapped in AES-256-GCM JSON envelopes via `services/credential_encryption.py` — plaintext persistence is forbidden. Tables under this rule: `subscription_credentials.encrypted_credentials`, `nevermined_agent_config.encrypted_credentials`, `telegram_bindings.bot_token_encrypted`, `whatsapp_bindings.auth_token_encrypted`, `agent_git_config.github_pat_encrypted`, `users.github_pat_encrypted` (per-user GitHub PAT — ent#162; resolved live by owner at agent creation via `settings_service.resolve_github_pat`), `slack_workspaces.bot_token` (TEXT column, JSON-envelope content), `slack_link_connections.slack_bot_token` (TEXT column, JSON-envelope content — encrypted by #453, 2026-05-05), `system_settings['elevenlabs_api_key_encrypted']` (platform ElevenLabs key, JSON-envelope value — ent#117; runtime-resolved by `settings_service.get_elevenlabs_api_key()`), `system_settings['a2a_outbound_endpoints_encrypted']` (the outbound A2A endpoint list — one envelope over a JSON document whose entries each carry a peer credential; #736. Chosen over a new table precisely because this location is already blessed: it is why #736 ships with no migration and no Alembic revision), and — since ent#435 — the six **credential-bearing settings** `system_settings['{anthropic_api_key,github_pat,google_api_key,slack_app_token,slack_client_secret,slack_signing_secret}_encrypted']`. **ent#435 closes the invariant's own loophole:** those six rows held live third-party credentials in CLEARTEXT while this list read as though everything was covered, so the platform's encryption-at-rest posture was contradicted by a single `SELECT` (CWE-312) — every dump, backup, replica and snapshot carried usable tokens, readable without `CREDENTIAL_ENCRYPTION_KEY`. The fix is a policy leaf (`services/secret_settings.py`) plus a **sink guard**: `db.set_setting` raises `SecretSettingWriteError` (→ 422) for any key in `SECRET_SETTING_KEYS` **or** merely credential-*shaped* (`*_api_key`/`*_token`/`*_secret`/`*_pat`/`*_password`/`*_credentials`), because the generic `PUT /api/settings/{key}` catch-all can address ANY key — the same door #506, #1609, ent#12, #1644, ent#14 and ent#346 each found open. Three properties are load-bearing. (1) The key **NAME** moves (`<key>` → `<key>_encrypted`, cleartext row DELETED), not just the value: a same-named key that may hold either form leaves "is this install encrypted?" unanswerable by inspection, which IS the reported defect; with the rename the reporter's own query (`SELECT key … WHERE key IN (…)` returning nothing) is the proof and the guard keeps it true. (2) The read path **lazily migrates** — `settings_service._resolve_secret_setting` resolves encrypted → legacy-cleartext-encrypted-and-deleted-on-sight → env → `''`, so a restored pre-fix backup or a direct DB write makes cleartext *transient* rather than merely absent right now; steady state pays nothing (the legacy key is never read while the encrypted row exists). It is fail-OPEN on read (an unreadable envelope degrades to env, never a 500 on the agent-start path) but deliberately does **not** fall back to a stale legacy row, which would resurrect a replaced credential; and fail-CLOSED on write. (3) `slack_client_id` is a **documented exemption**, not an omission — an OAuth client_id is emitted verbatim in the browser-visible authorize URL (the `whatsapp_bindings.account_sid` "(public)" precedent) — recorded with its reason in `PUBLIC_CREDENTIAL_SHAPED_KEYS` so a later reader can tell *reviewed* from *overlooked*. Dual-track sweep (`secret_settings_encryption` + Alembic `0041`) sharing ONE decision function (`plan_migration`) since the two drivers cannot share SQL; hard-fails on a missing key **only when there is something to encrypt** (the #453 choice, so a fresh install still boots); `downgrade()` is a deliberate no-op, because the honest inverse is "write these credentials back in cleartext". `scripts/deploy/rotate-credential-key.py` gains a row-keyed `system_settings` pass, which also closes the **pre-existing** gap that left `elevenlabs_api_key_encrypted`/`a2a_outbound_endpoints_encrypted` out of every rotation (envelope-in-a-row is invisible to a column sweep). Encryption protects the DB going forward only — historical backups still hold the plaintext, so the affected tokens must be **rotated**: runbook [docs/migrations/SECRET_SETTINGS_ENCRYPTION_2026-08.md](../migrations/SECRET_SETTINGS_ENCRYPTION_2026-08.md). Guards: `tests/unit/test_ent435_settings_sink_guard.py` (AST — every `system_settings` writer is gated or listed with a reason; OSS tree only, the private submodule owns its twin per the #1677 convention).

13. **MCP Server = Third Surface in Sync** — The MCP server (`src/mcp-server/src/tools/*.ts`) is a TypeScript proxy over the backend API. When adding a backend endpoint for external access, the MCP tool module needs updating too. Three surfaces must stay in sync: backend router, agent server (if internal), MCP tool (if external).

14. **Pydantic Models Centralized in `models.py`** — Request/response models live in `models.py`, not scattered across routers (#654). Keeps the API contract in one place. **Scope:** this invariant governs **router** models — a `class X(BaseModel)` must not be defined under `routers/` (enforced by the static guard `tests/unit/test_models_centralized.py`). Two model homes are **intentionally separate** and out of scope: `db_models.py` (DB-row / persistence models — a distinct layer) and `adapters/base.py` (the ChannelAdapter ABC's `NormalizedMessage`/`ChannelResponse`). One documented exception, allowlisted in the guard: `routers/canary.py::RunCycleRequest` evaluates `INVARIANTS` (from the `canary` library) in a `Field(description=…)` at class-definition time, and the `canary` library imports `TaskExecutionStatus` back from `models` — relocating it would force `models.py` to `from canary import …`, inverting the dependency direction of a module meant to be a low-level leaf everything imports *from*.

15. **API URL Nesting Convention** — Agent-scoped resources nest under `/api/agents/{name}/...`. Platform-wide resources get top-level prefixes (`/api/executions`, `/api/operator-queue`).

16. **Time-Window SQL uses `iso_cutoff()`, not `datetime('now', ...)`** — Columns written via `utc_now_iso()` are ISO-Z strings (`T` separator, `Z` suffix); SQLite's `datetime('now', ...)` emits a different format (space separator, no suffix), making lexicographic comparison silently incorrect (#476). For rolling-window filters on ISO-Z TEXT columns, compute the cutoff in Python via `iso_cutoff(hours)` from `utils/helpers.py` and pass it as a bound parameter. **Write-side/read-boundary cousin (#1474):** the standalone **scheduler** is a separate package that can't import `utils/helpers.py`, so its execution/schedule timestamps (`started_at`/`completed_at`/`validated_at`/`last_run_at`/`retry_scheduled_at` + process-schedule variants) now serialize via a vendored **behavioural** mirror `src/scheduler/utils.py` (`utc_now_iso`/`to_utc_iso`, Z-suffixed) — functionally identical to the backend copy but **textually divergent** (`to_utc_iso` uses an early return vs the backend's `if/else`), so unlike `failure_classifier.py` a source diff CANNOT verify it; the contract is agreement on **output**, enforced by `tests/unit/test_1713_scheduler_utils_parity.py` (#1713) and the property/edge suites `tests/unit/test_1771b_timestamp_helpers_{properties,edges}.py` (#1771) — previously naive strings that JS `new Date(...)` mis-parsed as local time. `parse_scheduler_ts` reads tolerantly and returns **naive UTC** so the historical model type + `aware − naive` duration math are preserved (write + read are one atomic change). The leaking backend **read boundaries** (`db/schedules.py` `get_agent_executions_summary`/`get_fleet_executions`/`get_agent_schedules_summary` `last_run_at`; `db/activities.py` `_row_to_activity`/`_mapping_to_activity`) normalize naive stored strings via `parse_iso_timestamp`+`to_utc_iso`, fixing historical rows for all consumers; the 5 execution panels also parse via `parseUTC` as render-layer defense (covers WS-pushed timestamps). **Honest caveat:** `agent_schedules.next_run_at` stays mixed-format across the scheduler (`Z`) and the backend's own `next_run_at.isoformat()` writers (`db/schedules.py`, tz-aware `+00:00`/offset) — safe because `next_run_at` is only ever parse-compared in Python, never lexicographically in SQL (Invariant #16's trap doesn't apply). `main.py` `SchedulerStatus.last_check` (health endpoint, not a DB row / not a relative-time surface) is out of scope.

17. **Non-root containers** — every Trinity-built image MUST end with a `USER` directive switching to a non-root user; the backend additionally requires `group_add: ${DOCKER_GID:-999}` in compose for Docker socket access on Linux. New Dockerfiles failing this are rejected at review (#874). CI guards in `.github/workflows/container-security.yml` (path-filtered on `docker/**`, `docker-compose*.yml`, `scripts/deploy/start.sh`, `src/mcp-server/Dockerfile`, independent of the `ui`-gated e2e workflow): `verify-non-root` execs the backend/scheduler/mcp-server containers, asserts UID 1000, and proves `group_add` works by running `docker.from_env().ping()` from inside the backend (not a `/api/agents` probe — `list_all_agents_fast` swallows Docker errors, a false-positive trap); `verify-prod-frontend-uid` builds the prod frontend out-of-band and asserts UID 101 (`nginxinc/nginx-unprivileged`). Dev-only `docker/frontend/Dockerfile` is exempt. Upgrading deployments must re-own their data path and `agent-configs` volume per [docs/migrations/NON_ROOT_CONTAINERS_2026-05.md](../migrations/NON_ROOT_CONTAINERS_2026-05.md).

18. **Trigger boundaries accept `Idempotency-Key`** (RELIABILITY-006, #525) — every producer boundary that creates an execution accepts an optional `Idempotency-Key` header and routes it through `services/idempotency_service.py` (`begin`/`complete`/`fail`) backed by the `idempotency_keys` table. The same `(scope, key)` within 24h yields one execution; duplicates short-circuit with the original result + `X-Idempotent-Replay: true` (in-flight duplicate → 409). Enforcement lives at the **router** layer, not solely in `TaskExecutionService`, because sync `/chat` runs an inline path and `/api/webhooks/{token}` creates no execution. Wired boundaries: `/chat`, `/task`, `/api/internal/execute-task`, `/api/webhooks/{token}` (auto-derives `(token, body_hash)`), `/api/paid/{name}/chat` (#1018 — always keys on `(payment-signature ∥ message)` via `derive_payment_key`, a divergent client header never forks execution; a completed-unsettled snapshot re-drives settle then `upgrade_snapshot`s), `/api/agents/{name}/fan-out`, and the scheduler (`Idempotency-Key: sched:{execution_id}`) + MCP `chat_with_agent`/`fan_out` (deterministic key over call args). **Any new trigger type must accept an idempotency key before merge** — the dedup layer is fail-open (a key never blocks a real execution), so the cost of adding it is one `begin/complete/fail` triple.

---

## Database Schema

### SQLite (`/data/trinity.db`)

**users:**
```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT,
    role TEXT NOT NULL DEFAULT 'user',  -- ROLE-001: admin, creator, operator, user
    auth0_sub TEXT UNIQUE,
    name TEXT,
    picture TEXT,
    email TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login TEXT,
    suspended_at TEXT,                  -- #995: NULL = active; set = deactivated
    github_pat_encrypted TEXT           -- ent#162: per-user GitHub PAT (AES-256-GCM envelope; NULL = none)
);
```

`github_pat_encrypted` (ent#162) is a per-user GitHub credential: a non-admin stores one token in their own settings (`GET`/`PUT`/`DELETE /api/users/me/github-pat`, self-service, token never echoed on read) and agent creation resolves **per-agent → owner's per-user (live) → global** via `services/settings_service.resolve_github_pat(agent_name, owner_id)`, so a user is no longer confined to the admin PAT's repo scope. The resolved value is persisted as the agent's per-agent PAT (#347) **only** when it came from the per-user or fork tier — never the global fallback (a global-fallback agent keeps this NULL so `github_pat_propagation_service` still reaches it on admin rotation). The recreate/restart PAT ladder (`settings_service.get_github_pat_for_agent`) stays 2-tier (per-agent → global) and never re-derives the per-user tier, so adding a personal token in Settings cannot force-recreate a running agent. OSS-core.

`suspended_at` (#995) is an edition-agnostic primitive: OSS owns the column AND its enforcement — `dependencies.get_current_user` rejects suspended users on both JWT and MCP-key paths, so setting it blocks new logins and invalidates live tokens on the next request. Only the enterprise `user_management` module exposes a setter (core-primitive + enterprise-knob pattern); OSS builds ship column + enforcement but no setter.

**agent_ownership:**
```sql
CREATE TABLE agent_ownership (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT UNIQUE NOT NULL,
    owner_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    is_system INTEGER DEFAULT 0,
    use_platform_api_key INTEGER DEFAULT 1,
    autonomy_enabled INTEGER DEFAULT 0,
    memory_limit TEXT,
    cpu_limit TEXT,
    full_capabilities INTEGER DEFAULT 0,
    read_only_mode INTEGER DEFAULT 0,
    read_only_config TEXT,
    subscription_id TEXT,
    max_parallel_tasks INTEGER DEFAULT 3,          -- CAPACITY-001
    execution_timeout_seconds INTEGER DEFAULT 3600, -- TIMEOUT-001 (60 min, #665)
    avatar_identity_prompt TEXT,
    avatar_updated_at TEXT,
    is_default_avatar INTEGER DEFAULT 0,
    require_email INTEGER DEFAULT 0,               -- #311
    open_access INTEGER DEFAULT 0,                 -- #311
    max_backlog_depth INTEGER DEFAULT 50,          -- BACKLOG-001
    group_auth_mode TEXT DEFAULT 'none',
    voice_system_prompt TEXT,
    voice_name TEXT,                               -- #28: persisted Gemini voice (NULL → 'Kore')
    public_channel_model TEXT,                     -- #894: per-agent model for public channels (NULL → platform default)
    public_channel_system_prompt TEXT,             -- #1205: public/channel-only custom-instructions fragment
    guardrails_config TEXT,
    file_sharing_enabled INTEGER DEFAULT 0,        -- FILES-001
    circuit_breaker_enabled INTEGER DEFAULT 0,     -- RELIABILITY-007 (#526): dispatch-breaker opt-in
    mcp_exposed INTEGER DEFAULT 0,                 -- #846: dedicated chat_with_<slug> MCP tool opt-in
    operator_resume_enabled INTEGER DEFAULT 0,     -- ent#329: an operator answer re-triggers the agent (owner opt-in; each answer costs a turn)
    a2a_exposed INTEGER DEFAULT 0,                 -- ent#157: A2A inbound-server exposure opt-in (default OFF)
    tts_voice_replies_enabled INTEGER DEFAULT 0,   -- epic #24/#25: outbound voice replies (shared agent-level)
    tts_voice_id TEXT,                             -- epic #24/#25: ElevenLabs voice id for spoken replies
    tts_voice_telegram_enabled INTEGER DEFAULT 1,  -- ent#117: per-channel voice-allowed flag
    tts_voice_slack_enabled INTEGER DEFAULT 1,     -- ent#117: per-channel voice-allowed flag
    tts_voice_whatsapp_enabled INTEGER DEFAULT 1,  -- ent#117: per-channel voice-allowed flag
    deleted_at TEXT,                               -- #834: NULL = live; set = soft-deleted
    is_ephemeral INTEGER DEFAULT 0,                -- trinity-enterprise#69: 1 = ghost (budgeted, hard-discarded)
    ephemeral_max_executions INTEGER,              -- trinity-enterprise#69: NULL = no exec budget
    ephemeral_expires_at TEXT,                     -- trinity-enterprise#69: ALWAYS set for ghosts; doubles as discard-intent marker
    spawned_by_agent TEXT,                         -- trinity-enterprise#69 Part 2: parent agent name (provenance)
    spawned_by_key_id TEXT,                        -- trinity-enterprise#69 Part 2: parent MCP key id (stable identity)
    volume_base_name TEXT,                         -- #1664: base name of the agent's data volumes (NULL = agent_name);
                                                   -- pinned at rename (volumes keep the old name), frozen across re-renames
    FOREIGN KEY (owner_id) REFERENCES users(id),
    FOREIGN KEY (subscription_id) REFERENCES subscription_credentials(id)
);

-- #834: partial index keeps the retention sweep cheap as the live agent count grows
CREATE INDEX idx_agent_ownership_deleted_at
    ON agent_ownership(deleted_at) WHERE deleted_at IS NOT NULL;
```

Soft-delete semantics: see [Soft Delete & Retention](#soft-delete-retention--recovery-834-772).

**agent_sharing** (cross-channel allow-list — same email admits the user on web, Telegram, and Slack):
```sql
CREATE TABLE agent_sharing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    shared_with_email TEXT NOT NULL,
    shared_by_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    allow_proactive INTEGER DEFAULT 0,
    UNIQUE(agent_name, shared_with_email),
    FOREIGN KEY (shared_by_id) REFERENCES users(id)
);
```

**access_requests** (#311 — unified channel access control):
```sql
CREATE TABLE access_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    email TEXT NOT NULL,                  -- verified email of requester
    channel TEXT NOT NULL,                -- 'web' | 'telegram' | 'slack' | 'whatsapp'
    status TEXT NOT NULL DEFAULT 'pending', -- pending, approved, rejected
    decided_by TEXT,                      -- user_id of approver
    decided_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(agent_name, email)
);

-- access_control migration also adds to telegram_chat_links:
--   verified_email TEXT, verified_at TEXT
```

Access-control flow (#311): `ChannelAdapter.resolve_verified_email()` maps native channel identity → verified email; `message_router` runs a single gate — owner/admin/`agent_sharing` → `open_access` → upsert pending `access_requests` row. Approval inserts into `agent_sharing`, whitelists the email (if email auth on), and fires a fire-and-forget notification on the requester's originating channel (telegram/slack/whatsapp only; bypasses `allow_proactive` and per-recipient rate limit — the user initiated the request; outcome audit-logged; delivery failure never rolls back the approval) (#951). Group chats bypass the gate; agents with both policy flags off retain legacy permissive behavior.

**mcp_api_keys:**
```sql
CREATE TABLE mcp_api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    key_prefix TEXT NOT NULL,
    key_hash TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    usage_count INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    user_id INTEGER NOT NULL,
    agent_name TEXT,                 -- non-null for agent-scoped keys
    scope TEXT DEFAULT 'user',       -- user | agent | system | connector | portal_delegate
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

**agent_schedules:**
```sql
CREATE TABLE agent_schedules (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    name TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    message TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    timezone TEXT DEFAULT 'UTC',
    description TEXT,
    owner_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_at TEXT,
    next_run_at TEXT,
    model TEXT,                                  -- MODEL-001: override (NULL = agent default)
    timeout_seconds INTEGER,                     -- #913: NULL = inherit agent cap
    webhook_token TEXT,                          -- WEBHOOK-001: 43-char urlsafe token, nullable
    webhook_enabled INTEGER DEFAULT 0,           -- WEBHOOK-001
    webhook_secret_encrypted TEXT,               -- ent#77: AES-256-GCM HMAC signing secret (Invariant #12), nullable
    webhook_auth_enabled INTEGER DEFAULT 0,      -- ent#77: gate signature verification in the public trigger
    deleted_at TEXT,                             -- #834: NULL = live; set = soft-deleted
    FOREIGN KEY (owner_id) REFERENCES users(id)
);

CREATE INDEX idx_agent_schedules_deleted_at
    ON agent_schedules(deleted_at) WHERE deleted_at IS NOT NULL;
```

**schedule_executions:**
```sql
CREATE TABLE schedule_executions (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,
    message TEXT NOT NULL,
    response TEXT,
    error TEXT,
    triggered_by TEXT NOT NULL,
    model_used TEXT,                             -- MODEL-001
    queued_at TEXT,                              -- BACKLOG-001: when task entered backlog
    backlog_metadata TEXT,                       -- BACKLOG-001: JSON identity/request for drain replay
    retry_count INTEGER DEFAULT 0,               -- #678: in-line auto-retry count (reader-race recovery)
    fan_out_id TEXT,                             -- FANOUT-001: parent fan-out operation ID
    loop_id TEXT,                                -- #740: parent agent_loops.id
    claim_token TEXT,                            -- #1081 Phase 0 (dark): pull-worker lease CAS token (NULL on push/#1083 rows)
    lease_expires_at TEXT,                       -- #1081 Phase 0 (dark): ISO-Z lease deadline; non-NULL ⇒ owned by the lease-reaper
    claimed_by_worker TEXT,                      -- #1081 Phase 0 (dark): opaque pull-worker identity that holds the lease
    redelivery_count INTEGER DEFAULT 0,          -- #1081 Phase 3 (#429/#1402): lease-reaper re-delivery count (distinct from retry_count)
    source_channel TEXT,                         -- ent#117: originating channel (telegram|slack|whatsapp) for voice-reply delivery;
                                                 -- also 'portal' (#2157) — a surface stamp, NOT a delivery leg (no chat id)
    source_channel_chat_id TEXT,                 -- ent#117: channel destination (chat/channel id)
    source_channel_thread TEXT,                  -- ent#117: channel thread id (nullable)
    source_channel_agent TEXT,                   -- ent#265: binding-agent for channel report-back (NULL = executing agent)
    FOREIGN KEY (schedule_id) REFERENCES agent_schedules(id)
);

-- BACKLOG-001: partial index for cheap atomic FIFO claim
CREATE INDEX idx_executions_queued ON schedule_executions(agent_name, queued_at)
    WHERE status = 'queued';
-- #740: partial index for joining executions back to their parent loop
CREATE INDEX idx_executions_loop ON schedule_executions(loop_id)
    WHERE loop_id IS NOT NULL;
```

**agent_loops + agent_loop_runs** (#740 — see [Sequential Agent Loops](#sequential-agent-loops-740-ui-1106)):
```sql
CREATE TABLE agent_loops (
    id TEXT PRIMARY KEY,                         -- 'loop_<urlsafe>'
    agent_name TEXT NOT NULL,
    message_template TEXT NOT NULL,              -- supports {{run}} and {{previous_response}}
    max_runs INTEGER NOT NULL,                   -- 1–100 hard cap
    stop_signal TEXT,                            -- NULL = fixed mode; set = until mode
    delay_seconds INTEGER NOT NULL DEFAULT 0,
    timeout_per_run INTEGER,                     -- NULL = agent's execution_timeout_seconds
    max_duration_seconds INTEGER,                -- #1156: NULL = no wall-clock deadline (≤7d when set)
    max_cost_usd REAL,                           -- #1155: NULL = no cost budget (gt=0 when set)
    no_progress_threshold INTEGER,               -- #1157: NULL = disabled (legacy); 0 = off; ≥2 = stop after K identical responses
    on_failure TEXT NOT NULL DEFAULT 'abort',    -- #1167: abort (fail-fast) | continue (tolerate failed iterations)
    max_consecutive_failures INTEGER NOT NULL DEFAULT 3,  -- #1167: continue-mode cutoff (1–100)
    model TEXT,
    allowed_tools TEXT,                          -- JSON array
    status TEXT NOT NULL,                        -- queued | running | completed | completed_with_errors | stopped | failed | interrupted
    runs_completed INTEGER NOT NULL DEFAULT 0,
    failed_runs INTEGER NOT NULL DEFAULT 0,      -- #1167: tolerated-failure count (continue mode)
    stop_reason TEXT,                            -- max_runs_reached | stop_signal_matched | user_stopped | deadline_exceeded | budget_exhausted | no_progress | max_consecutive_failures | error | interrupted
    last_response TEXT,
    error TEXT,
    started_by_user_id INTEGER,
    started_by_user_email TEXT,
    source_agent_name TEXT,
    source_mcp_key_id TEXT,
    source_mcp_key_name TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX idx_loops_agent ON agent_loops(agent_name);
CREATE INDEX idx_loops_status ON agent_loops(status);
CREATE INDEX idx_loops_user ON agent_loops(started_by_user_id);

CREATE TABLE agent_loop_runs (
    id TEXT PRIMARY KEY,                         -- 'lr_<urlsafe>'
    loop_id TEXT NOT NULL,
    run_number INTEGER NOT NULL,                 -- 1-indexed
    execution_id TEXT,                           -- joins back to schedule_executions
    status TEXT NOT NULL,                        -- running | completed | failed
    response TEXT,                               -- full response for this iteration
    error TEXT,
    cost REAL,
    duration_ms INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (loop_id) REFERENCES agent_loops(id)
);
CREATE INDEX idx_loop_runs_loop ON agent_loop_runs(loop_id, run_number);
```

**agent_reminders** (#1296 — see [Agent Self-Reminders](#agent-self-reminders-1296)). Durable
one-shot deferred self-trigger; the standalone scheduler arms a `DateTrigger` per pending row.
Dual-track migration (SQLite `agent_reminders_table` + Alembic `0028_agent_reminders`); `AGENT_REFS`
CASCADE (rename cascade + purge). Status: `pending → firing → fired` / `firing → pending|failed` /
`pending → cancelled`:
```sql
CREATE TABLE agent_reminders (
    id TEXT PRIMARY KEY,                        -- 'rem_<hex>'
    agent_name TEXT NOT NULL,                   -- target == source (self-reminder)
    message TEXT NOT NULL,
    fire_at TEXT NOT NULL,                      -- ISO-Z absolute (relative delay resolved at create)
    status TEXT NOT NULL DEFAULT 'pending',     -- pending | firing | fired | cancelled | failed
    model TEXT,                                 -- optional override
    timeout_seconds INTEGER,                    -- optional; clamped to agent cap at create
    allowed_tools TEXT,                         -- optional JSON array
    owner_id INTEGER,                           -- resolved owner (provenance)
    created_by_email TEXT,                      -- denormalized owner email (provenance)
    source_agent_name TEXT,                     -- the agent that set it (provenance)
    source_mcp_key_id TEXT,                     -- MCP key id that set it (provenance)
    execution_id TEXT,                          -- latest fire attempt's execution row
    fire_attempts INTEGER NOT NULL DEFAULT 0,   -- at-least-once retry counter (≤ MAX_REMINDER_FIRE_ATTEMPTS)
    firing_at TEXT,                             -- in-flight fire start (stale-firing reclaim threshold)
    error TEXT,                                 -- last-attempt failure detail
    created_at TEXT NOT NULL,
    fired_at TEXT,
    cancelled_at TEXT
);
CREATE INDEX idx_agent_reminders_agent ON agent_reminders(agent_name);
-- Partial index covers BOTH the pending-scan and the stale-firing reclaim.
CREATE INDEX idx_agent_reminders_active ON agent_reminders(fire_at)
    WHERE status IN ('pending', 'firing');
```

**agent_activities** (unified activity stream):
```sql
CREATE TABLE agent_activities (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    activity_type TEXT NOT NULL,            -- chat_start, chat_end, tool_call, schedule_start, schedule_end, agent_collaboration
    activity_state TEXT NOT NULL,           -- started, completed, failed, cancelled (#1332)
    parent_activity_id TEXT,                -- link to parent activity (tool → chat)
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,
    user_id INTEGER,
    triggered_by TEXT NOT NULL,             -- user, schedule, agent, system
    related_chat_message_id TEXT,           -- FK to chat_messages (observability link)
    related_execution_id TEXT,              -- FK to schedule_executions (observability link)
    details TEXT,                           -- JSON: tool_name, target_agent, etc.
    error TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (parent_activity_id) REFERENCES agent_activities(id),
    FOREIGN KEY (related_chat_message_id) REFERENCES chat_messages(id),
    FOREIGN KEY (related_execution_id) REFERENCES schedule_executions(id)
);

CREATE INDEX idx_activities_agent ON agent_activities(agent_name, created_at DESC);
CREATE INDEX idx_activities_type ON agent_activities(activity_type);
CREATE INDEX idx_activities_state ON agent_activities(activity_state);
CREATE INDEX idx_activities_user ON agent_activities(user_id);
CREATE INDEX idx_activities_parent ON agent_activities(parent_activity_id);
CREATE INDEX idx_activities_chat_msg ON agent_activities(related_chat_message_id);
CREATE INDEX idx_activities_execution ON agent_activities(related_execution_id);
```

Data strategy: `chat_messages.tool_calls` holds the aggregated JSON summary; `agent_activities` holds granular per-tool rows; observability fields (cost, context) live in `chat_messages`/`schedule_executions` only — activity queries JOIN for them.

**chat_sessions / chat_messages** (persistent chat — survives container restarts/deletions; auto-created per user+agent; access control: own messages only, admins all). The authenticated Chat tab's `/task` writer (`chat_persistence_service.py::persist_chat_session`, extracted from `routers/chat.py` by #1483; shared by the sync + async branches; guarded on a SUCCESS terminal) is **fail-loud** (#1444): a persistence error logs at ERROR with a stack trace (message carries agent + execution_id + exc-type only, and the SQLAlchemy engine sets `hide_parameters=True` in `db/engine.py` so a DB-error traceback can't leak bound values either — no user content in message or trace) and never re-raises past a completed, billed turn — the sync branch surfaces a `chat_persist_failed` marker on the response. A caller-supplied `chat_session_id` is **owner-checked** (`session.user_id == caller`) before appending (closes an IDOR); on mismatch the write falls through to the caller's own session. The in-process path is the only persister — the #1083 fire-and-forget callback path is structurally unreachable by a manual `/task` (`ASYNC_DISPATCH_ELIGIBLE_TRIGGERS` = `{schedule, webhook}`), so callback-path persistence is deferred to the pull-mode epic. Schema:
```sql
CREATE TABLE chat_sessions (
    id TEXT PRIMARY KEY,                  -- urlsafe token
    agent_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_email TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,      -- user + assistant
    total_cost REAL DEFAULT 0.0,
    total_context_used INTEGER DEFAULT 0,
    total_context_max INTEGER DEFAULT 200000,
    status TEXT DEFAULT 'active',         -- 'active' or 'closed'
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_chat_sessions_agent ON chat_sessions(agent_name);
CREATE INDEX idx_chat_sessions_user ON chat_sessions(user_id);
CREATE INDEX idx_chat_sessions_status ON chat_sessions(status);

CREATE TABLE chat_messages (
    id TEXT PRIMARY KEY,                  -- urlsafe token
    session_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,             -- denormalized for queries
    user_id INTEGER NOT NULL,
    user_email TEXT NOT NULL,
    role TEXT NOT NULL,                   -- 'user' or 'assistant'
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    cost REAL,                            -- assistant only (NULL for user)
    context_used INTEGER,
    context_max INTEGER,
    tool_calls TEXT,                      -- JSON array (assistant only)
    execution_time_ms INTEGER,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_chat_messages_session ON chat_messages(session_id);
CREATE INDEX idx_chat_messages_agent ON chat_messages(agent_name);
CREATE INDEX idx_chat_messages_user ON chat_messages(user_id);
CREATE INDEX idx_chat_messages_timestamp ON chat_messages(timestamp);
```

**agent_sessions / agent_session_messages** (per-platform-user resumable sessions — see [Resumable Turns](#resumable-turns)):
```sql
CREATE TABLE agent_sessions (
    id TEXT PRIMARY KEY,                           -- urlsafe token
    agent_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_email TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    total_context_used INTEGER DEFAULT 0,
    total_context_max INTEGER DEFAULT 200000,
    status TEXT DEFAULT 'active',                  -- active | archived | reset
    subscription_id TEXT,
    cached_claude_session_id TEXT,                 -- THE primitive — Claude Code UUID for --resume
    last_resume_at TEXT,
    consecutive_resume_failures INTEGER DEFAULT 0, -- drives the resume-fallback path
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX idx_agent_sessions_agent_user ON agent_sessions(agent_name, user_id);
CREATE INDEX idx_agent_sessions_status ON agent_sessions(status);

CREATE TABLE agent_session_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_email TEXT NOT NULL,
    role TEXT NOT NULL,                            -- user | assistant
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    cost REAL,
    context_used INTEGER,
    context_max INTEGER,
    cache_read_tokens INTEGER,                     -- prompt-cache hit observability across resume turns
    tool_calls TEXT,                               -- JSON
    execution_time_ms INTEGER,
    claude_session_id TEXT,                        -- per-message UUID Claude actually ran under (audit; changes on fallback/reset)
    FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX idx_agent_session_messages_session ON agent_session_messages(session_id);
CREATE INDEX idx_agent_session_messages_user ON agent_session_messages(user_id);
```

ON DELETE CASCADE is aspirational (`PRAGMA foreign_keys` is off platform-wide); `delete_session()` deletes child rows explicitly.

**agent_permissions** (agent-to-agent access — enforced at the MCP layer, see Auth section):
```sql
CREATE TABLE agent_permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_agent TEXT NOT NULL,           -- agent making calls
    target_agent TEXT NOT NULL,           -- agent being called
    granted_by TEXT NOT NULL,             -- user ID who granted permission
    created_at TEXT NOT NULL,
    UNIQUE(source_agent, target_agent),
    FOREIGN KEY (granted_by) REFERENCES users(id)
);
CREATE INDEX idx_agent_permissions_source ON agent_permissions(source_agent);
CREATE INDEX idx_agent_permissions_target ON agent_permissions(target_agent);
```

**agent_shared_folder_config** (shared folders — exposing agents publish a Docker volume at `/home/developer/shared-out`; consumers with `agent_permissions` mount it at `/home/developer/shared-in/{agent}`; container recreated on restart when mount config changes; volume ownership fixed to UID 1000):
```sql
CREATE TABLE agent_shared_folder_config (
    agent_name TEXT PRIMARY KEY,
    expose_enabled INTEGER DEFAULT 0,     -- 1 = expose /home/developer/shared-out
    consume_enabled INTEGER DEFAULT 0,    -- 1 = mount permitted agents' folders
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_shared_folders_expose ON agent_shared_folder_config(expose_enabled);
CREATE INDEX idx_shared_folders_consume ON agent_shared_folder_config(consume_enabled);
```

**agent_shared_files** (FILES-001 — see [Outbound File Sharing](#outbound-file-sharing-files-001)):
```sql
CREATE TABLE agent_shared_files (
    id TEXT PRIMARY KEY,                  -- UUID
    agent_name TEXT NOT NULL,
    filename TEXT NOT NULL,               -- display name in download
    stored_filename TEXT NOT NULL,        -- UUID filename under /data/agent-files/
    size_bytes INTEGER NOT NULL,
    mime_type TEXT,                       -- python-magic detected
    download_token TEXT UNIQUE NOT NULL,  -- secrets.token_urlsafe(32), 192-bit
    created_by TEXT NOT NULL,             -- agent name (or user for admin-created)
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,             -- default 7d
    revoked_at TEXT,
    one_time INTEGER DEFAULT 0,           -- deferred: one-time link mode (column reserved)
    consumed_at TEXT,                     -- deferred
    download_count INTEGER DEFAULT 0,
    last_downloaded_at TEXT,
    FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
        ON DELETE CASCADE ON UPDATE CASCADE   -- aspirational; manual cascade per platform convention
);
CREATE INDEX idx_agent_files_agent ON agent_shared_files(agent_name);
CREATE INDEX idx_agent_files_token ON agent_shared_files(download_token);
CREATE INDEX idx_agent_files_expires ON agent_shared_files(expires_at) WHERE revoked_at IS NULL;
```

**agent_event_subscriptions / agent_events** (EVT-001 — agent event pub/sub):
```sql
CREATE TABLE agent_event_subscriptions (
    id TEXT PRIMARY KEY,
    subscriber_agent TEXT NOT NULL,       -- agent receiving events
    source_agent TEXT NOT NULL,           -- agent emitting events
    event_type TEXT NOT NULL,             -- namespaced event type
    target_message TEXT NOT NULL,         -- message template with {{payload.field}}
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(subscriber_agent, source_agent, event_type)
);
CREATE TABLE agent_events (
    id TEXT PRIMARY KEY,
    source_agent TEXT NOT NULL,
    event_type TEXT NOT NULL,             -- agent-emitted, OR backend-emitted 'agent.task.completed'/'agent.task.failed' (#1578)
    payload TEXT,                         -- JSON
    subscriptions_triggered INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
```

`agent_events` rows are written by **agent-emitted** `emit_event` (EVT-001) AND by the **system-emitted** task-completion emitter (#1578), which persists a row ONLY when a matching subscription exists — see [Task Completion Events](#task-completion-events-1578).

**slack_workspaces** (SLACK-002):
```sql
CREATE TABLE slack_workspaces (
    id TEXT PRIMARY KEY,
    team_id TEXT UNIQUE NOT NULL,          -- Slack workspace team ID
    team_name TEXT,
    bot_token TEXT NOT NULL,               -- AES-256-GCM JSON envelope of OAuth token
    connected_by TEXT,
    connected_at TEXT NOT NULL,
    enabled INTEGER DEFAULT 1
);
```

`bot_token` is a TEXT column whose contents are an AES-256-GCM JSON envelope (not renamed for backward compatibility); the read path in `db/slack_channels.py:_decrypt_token` handles both encrypted and legacy plaintext (`xoxb-*`) values, and plaintext rows are re-encrypted on next backend restart by the `slack_bot_token_encryption` migration (#453).

**slack_link_connections** (SLACK-001 — one Slack workspace = one public link = one agent; coexists with `slack_workspaces` (SLACK-002 multi-agent routing) — different products, different OAuth installations possible):
```sql
CREATE TABLE slack_link_connections (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL UNIQUE,          -- FK to agent_public_links
    slack_team_id TEXT NOT NULL UNIQUE,
    slack_team_name TEXT,
    slack_bot_token TEXT NOT NULL,         -- AES-256-GCM JSON envelope (same pattern as slack_workspaces.bot_token)
    connected_by TEXT NOT NULL,
    connected_at TEXT NOT NULL,
    enabled INTEGER DEFAULT 1
);
```

**slack_channel_agents / slack_active_threads** (SLACK-002):
```sql
CREATE TABLE slack_channel_agents (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,                 -- FK to slack_workspaces.team_id
    slack_channel_id TEXT NOT NULL,
    slack_channel_name TEXT,
    agent_name TEXT NOT NULL,
    is_dm_default INTEGER DEFAULT 0,       -- 1 = default agent for DMs
    created_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(team_id, slack_channel_id)
);

CREATE TABLE slack_active_threads (
    team_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,               -- Slack thread timestamp
    agent_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(team_id, channel_id, thread_ts)
);
```

**whatsapp_bindings / whatsapp_chat_links** (WHATSAPP-001 — one Twilio sender per agent, owner brings their own Twilio account; webhook verification dual-factor: URL `webhook_secret` + HMAC-SHA1; Sandbox auto-detected from well-known sender `whatsapp:+14155238886`; DMs only — Twilio's WhatsApp API has no groups; `verified_email`/`verified_at` shipped up-front so #311 Phase 2 is additive):
```sql
CREATE TABLE whatsapp_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL UNIQUE,
    account_sid TEXT NOT NULL,                 -- Twilio AccountSid (public)
    auth_token_encrypted TEXT NOT NULL,        -- AES-256-GCM
    from_number TEXT NOT NULL,                 -- 'whatsapp:+E164'
    messaging_service_sid TEXT,                -- optional; preferred over from_number
    display_name TEXT,                         -- friendly_name from Twilio Account fetch
    is_sandbox INTEGER DEFAULT 0,              -- auto-detected from from_number
    webhook_secret TEXT NOT NULL UNIQUE,       -- 32-byte token_urlsafe
    webhook_url TEXT,                          -- computed from public_chat_url
    enabled INTEGER DEFAULT 1,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX idx_whatsapp_bindings_agent ON whatsapp_bindings(agent_name);
CREATE INDEX idx_whatsapp_bindings_webhook ON whatsapp_bindings(webhook_secret);

CREATE TABLE whatsapp_chat_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_id INTEGER NOT NULL REFERENCES whatsapp_bindings(id),
    wa_user_phone TEXT NOT NULL,               -- 'whatsapp:+E164'
    wa_user_name TEXT,                         -- Twilio ProfileName
    session_id TEXT,
    verified_email TEXT,                       -- #311 Phase 2
    verified_at TEXT,
    message_count INTEGER DEFAULT 0,
    last_active TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(binding_id, wa_user_phone)
);
CREATE INDEX idx_whatsapp_chat_links_binding ON whatsapp_chat_links(binding_id);
```

**operator_queue** (OPS-001):
```sql
CREATE TABLE operator_queue (
    id TEXT PRIMARY KEY,               -- #1631: platform-minted uuid4().hex (global row handle)
    agent_name TEXT NOT NULL,
    request_id TEXT,                   -- #1631: agent-authored correlation string; UNIQUE(agent_name, request_id)
    type TEXT NOT NULL,                -- approval, question, alert
    status TEXT NOT NULL DEFAULT 'pending', -- pending, responded, acknowledged, expired, cancelled
    priority TEXT NOT NULL DEFAULT 'medium', -- critical, high, medium, low
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    options TEXT,                       -- JSON array (approval choices)
    context TEXT,                       -- JSON metadata from agent
    execution_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    response TEXT,
    response_text TEXT,
    responded_by_id TEXT,
    responded_by_email TEXT,
    responded_at TEXT,
    acknowledged_at TEXT,
    cleared_at TEXT,                    -- #1017: NULL = visible; set = hidden by Clear All (rows deleted by the #1142 retention sweep past operator_queue_retention_days)
    addressed_to_email TEXT,            -- ent#364: the human this ask is for; NULL = operator ask. Validated at ingestion against the agent's roster, never trusted from the payload
    FOREIGN KEY (responded_by_id) REFERENCES users(id)
);
CREATE INDEX idx_operator_queue_agent ON operator_queue(agent_name);
CREATE INDEX idx_operator_queue_status ON operator_queue(status);
CREATE INDEX idx_operator_queue_priority ON operator_queue(priority);
CREATE INDEX idx_operator_queue_type ON operator_queue(type);
CREATE INDEX idx_operator_queue_created ON operator_queue(created_at DESC);
-- #1631: per-agent uniqueness on the agent-authored correlation string
CREATE UNIQUE INDEX idx_operator_queue_agent_request ON operator_queue(agent_name, request_id);
```

**agent_sync_state** (#389 — see [Git Sync Health](#git-sync-health-389390)):
```sql
CREATE TABLE agent_sync_state (
    agent_name TEXT PRIMARY KEY,
    last_sync_at TEXT,
    last_sync_status TEXT,                 -- 'success' | 'failed' | 'never'
    consecutive_failures INTEGER DEFAULT 0,
    last_error_summary TEXT,
    last_remote_sha_main TEXT,
    last_remote_sha_working TEXT,
    ahead_main INTEGER DEFAULT 0,
    behind_main INTEGER DEFAULT 0,
    ahead_working INTEGER DEFAULT 0,       -- #389 P6: working-branch divergence
    behind_working INTEGER DEFAULT 0,
    git_dir_bytes INTEGER,                 -- #1596: agent .git on-disk size (bloat curve)
    pack_count INTEGER,                    -- #1595: packs from `git count-objects -v`
    loose_objects INTEGER,                 -- #1595: loose objects (gc-health signal)
    maintenance_failures INTEGER DEFAULT 0, -- #1595: consecutive failed maintenance attempts
    last_check_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
);
CREATE INDEX idx_sync_state_status
    ON agent_sync_state(last_sync_status, consecutive_failures);

-- Also adds to agent_git_config:
--   auto_sync_enabled INTEGER DEFAULT 0
--   freeze_schedules_if_sync_failing INTEGER DEFAULT 0
```

**audit_log** (SEC-001 — append-only at the database layer):
```sql
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,         -- UUID, generated by service layer
    event_type TEXT NOT NULL,              -- AuditEventType (agent_lifecycle, authentication, ...)
    event_action TEXT NOT NULL,            -- specific action ("create", "login_success", etc.)
    actor_type TEXT NOT NULL,              -- user | agent | mcp_client | system
    actor_id TEXT,                         -- user.id, agent_name, or mcp key id
    actor_email TEXT,
    actor_ip TEXT,
    mcp_key_id TEXT,                       -- #2323: populated for EVERY key-authenticated
                                           -- call, not just MCP tool calls (see below)
    mcp_key_name TEXT,
    mcp_scope TEXT,                        -- user | agent | system | connector | portal_delegate | ops
    target_type TEXT,
    target_id TEXT,
    timestamp TEXT NOT NULL,               -- ISO 8601 UTC
    details TEXT,                          -- JSON payload, event-specific
    request_id TEXT,                       -- request correlation id
    source TEXT NOT NULL,                  -- api | mcp | scheduler | system
    endpoint TEXT,                         -- request path
    previous_hash TEXT,                    -- hash chain
    entry_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_audit_log_timestamp ON audit_log(timestamp DESC);
CREATE INDEX idx_audit_log_event_type ON audit_log(event_type, timestamp DESC);
CREATE INDEX idx_audit_log_actor ON audit_log(actor_type, actor_id, timestamp DESC);
CREATE INDEX idx_audit_log_target ON audit_log(target_type, target_id, timestamp DESC);
CREATE INDEX idx_audit_log_mcp_key ON audit_log(mcp_key_id, timestamp DESC);
CREATE INDEX idx_audit_log_request ON audit_log(request_id);

-- Append-only enforcement
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'Audit log entries cannot be modified'); END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
WHEN OLD.timestamp > datetime('now', '-365 days')
BEGIN SELECT RAISE(ABORT, 'Audit log entries cannot be deleted within retention period'); END;
```

**canary_violations** (CANARY-001 — one row per fired check per cycle; `observed_state` carries invariant-specific JSON; append-only in practice — no UPDATE/DELETE in the read API):
```sql
CREATE TABLE canary_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invariant_id TEXT NOT NULL,            -- 'S-01', 'E-02', 'L-03', ...
    tier TEXT NOT NULL,                    -- 'A' | 'B'
    severity TEXT NOT NULL,                -- 'critical' | 'major' | 'minor'
    snapshot_time TEXT NOT NULL,           -- ISO 8601 UTC
    observed_state TEXT NOT NULL,          -- JSON, invariant-specific
    signal_query TEXT,                     -- the check that fired (debugging aid)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_canary_violations_invariant
    ON canary_violations(invariant_id, snapshot_time DESC);
CREATE INDEX idx_canary_violations_severity
    ON canary_violations(severity, snapshot_time DESC);
CREATE INDEX idx_canary_violations_snapshot
    ON canary_violations(snapshot_time DESC);
```

**idempotency_keys** (RELIABILITY-006 — see [Idempotency](#idempotency-reliability-006-525) and Invariant #18):
```sql
CREATE TABLE idempotency_keys (
    scope TEXT NOT NULL,              -- tenant isolation: "agent:{name}" | "webhook:{token}"
    idempotency_key TEXT NOT NULL,    -- caller-supplied or derived
    execution_id TEXT,               -- nullable (webhook short-circuit has none)
    status TEXT NOT NULL,            -- 'in_flight' | 'completed'
    response_snapshot TEXT,          -- JSON of the original response, for replay
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, idempotency_key)
);
CREATE INDEX idx_idempotency_created ON idempotency_keys(created_at);
```

**agent_compatibility_results** (#668 — see [Agent Compatibility Validation](#agent-compatibility-validation-668)). Latest-snapshot-per-agent (one row, upserted by `agent_name`); STATIC recomputes live, persisted AI verdicts merge in. Dual-track migration (SQLite `db/migrations.py` + Alembic `migrations/versions/0003_*`); cascade/rename via `AGENT_REFS`:
```sql
CREATE TABLE agent_compatibility_results (
    agent_name TEXT PRIMARY KEY,
    overall_status TEXT NOT NULL,        -- compatible | issues | unavailable
    checks_json TEXT NOT NULL,           -- full last report's check list (JSON)
    hard_count INTEGER NOT NULL DEFAULT 0,
    soft_count INTEGER NOT NULL DEFAULT 0,
    info_count INTEGER NOT NULL DEFAULT 0,
    container_running INTEGER NOT NULL DEFAULT 0,
    ai_ran_at TEXT,                      -- last AI evaluation (NULL = never)
    static_ran_at TEXT,
    updated_at TEXT NOT NULL
);
```

**agent_reports** (#918 — see [Agent Reports](#agent-reports-918)). Dual-track migration
(SQLite `agent_reports_table` + Alembic `0006_agent_reports`). `user_id` = the MCP-key/JWT
owner who authored the report (not necessarily the agent owner):
```sql
CREATE TABLE agent_reports (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    user_id INTEGER,                     -- author = MCP-key owner (current_user.id)
    report_type TEXT NOT NULL,           -- namespaced, e.g. 'recon.weekly_summary'
    title TEXT NOT NULL,
    payload TEXT NOT NULL,               -- arbitrary JSON, ≤5 MiB (413 over cap, #1537)
    display_hint TEXT,                   -- table|kpi|markdown|timeline|json|NULL
    schema_version INTEGER DEFAULT 1,
    period_start TEXT,
    period_end TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX idx_agent_reports_agent   ON agent_reports(agent_name, created_at DESC);
CREATE INDEX idx_agent_reports_type    ON agent_reports(report_type, created_at DESC);
CREATE INDEX idx_agent_reports_created ON agent_reports(created_at);  -- retention sweep
```

**subscription_headroom_history** (ent#433 — the durable half of #471's live headroom).
One row per probe, so utilization trends survive a Redis snapshot that overwrites itself on
every probe. Written only by `_probe_and_store` (off-loop, after the Redis write, fail-open);
read as a bounded `last`-per-bucket series. Dual-track migration (SQLite
`subscription_headroom_history_table` + Alembic `0043_subscription_headroom_history`).
Subscription-keyed, so the `AGENT_REFS` agent cascade does not apply; the subscription cascade
is performed **explicitly** inside `delete_subscription`'s transaction, because the DDL's
`ON DELETE CASCADE` is decorative (`PRAGMA foreign_keys` is off platform-wide and
`_PG_TABLE_SUBS` strips every FK clause before the DDL reaches PostgreSQL — hence the Alembic
revision deliberately declares **no** constraint, which would otherwise be the platform's first
enforced FK and would diverge the backends on the in-flight-probe delete race):
```sql
CREATE TABLE subscription_headroom_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,   -- SERIAL on PostgreSQL
    subscription_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,               -- ISO-Z; stamped at probe START (pre-HTTP)
    status TEXT NOT NULL,                   -- ok | rate_limited | invalid_token | error | no_windows
    five_hour_utilization_pct REAL,         -- NULL-able INDEPENDENTLY of status: a 429 reports
    five_hour_resets_at TEXT,               --   a window status with no figure, so a reader that
    five_hour_status TEXT,                  --   coerces NULL to 0 inverts its most important sample
    seven_day_utilization_pct REAL,
    seven_day_resets_at TEXT,
    seven_day_status TEXT,
    representative_claim TEXT,
    overage_status TEXT,
    unified_status TEXT,
    FOREIGN KEY (subscription_id) REFERENCES subscription_credentials(id) ON DELETE CASCADE
);
CREATE INDEX idx_headroom_history_sub_fetched
    ON subscription_headroom_history(subscription_id, fetched_at DESC);  -- the windowed read
CREATE INDEX idx_headroom_history_fetched
    ON subscription_headroom_history(fetched_at);                        -- the retention sweep
```
The series is **`last`-per-bucket, never `max`** — probes are demand-driven so a max is biased by
how often anyone looked (`E[max of n]` rises with `n`, and the unattended overnight burn gets the
fewest samples); the 5h and 7d windows peak at different instants so a two-column max has no
single owning row; and a max over `utilization_pct` drops rate-limited samples outright. Selection
is `ROW_NUMBER() OVER (PARTITION BY bucket ORDER BY fetched_at DESC)`, never a bare
non-aggregated column beside an aggregate (a SQLite-only extension that raises `GroupingError` on
PostgreSQL). Each bucket carries **both** its logical `bucket_start` and the real `fetched_at`:
emitting only non-empty buckets with real timestamps alone cannot support gap detection, because
sample jitter and a true gap are indistinguishable from timestamp deltas.

**agent_evaluations** (ent#206 — see [Agent Evaluations](#agent-evaluations--the-referee-surface-ent206)).
The referee surface: written by the platform/evaluator, **never** by the graded agent
(`require_admin` + `reject_agent_principal` on the single write route). `quality` is
nullable — null means "not graded yet", not zero. Dual-track migration (SQLite
`agent_evaluations_table` + Alembic `0033_agent_evaluations`); `AGENT_REFS` CASCADE:
```sql
CREATE TABLE agent_evaluations (
    id TEXT PRIMARY KEY,                 -- 'eval_<hex>'
    agent_name TEXT NOT NULL,
    execution_id TEXT,                   -- nullable: may grade the agent, not one run
    archetype TEXT,                      -- what "good" means here (per-archetype rubric)
    completion INTEGER,                  -- mirror of the clean-exit axis
    quality REAL,                        -- the graded axis (nullable, independent)
    checks_json TEXT,                    -- Tier-0 deterministic check results
    judge_json TEXT,                     -- Tier-1 judge output (enterprise layer)
    evaluator TEXT NOT NULL,             -- 'tier0' | judge id | admin username
    created_at TEXT NOT NULL,
    FOREIGN KEY (execution_id) REFERENCES schedule_executions(id)
);
CREATE INDEX idx_agent_evaluations_agent ON agent_evaluations(agent_name, created_at DESC);
CREATE INDEX idx_agent_evaluations_execution ON agent_evaluations(execution_id);
```

**skill_sources** (ent#237 — the multi-source skills library). **Replaces** the single
`skills_library_url` system setting: the platform syncs from a bundled public community
catalog plus any number of admin-added custom repos, each with its own checkout at
`/data/skills-library/<source_id>/`. Resolution across sources is `priority` ASC then
`created_at` ASC — custom sources default to 100 and the community source to 1000, so
**custom wins** a name clash and names stay bare. Dual-track migration (SQLite
`skill_sources_table` + Alembic `0034_skill_sources`):
```sql
CREATE TABLE skill_sources (
    id TEXT PRIMARY KEY,                 -- 'src_<hex8>' (server-minted; also the checkout dir name)
    name TEXT NOT NULL,
    url TEXT NOT NULL,                   -- github.com only (SSRF allowlist, SEC-179), validated on write AND at sync
    ref TEXT NOT NULL,                   -- branch name or tag name
    ref_type TEXT NOT NULL,              -- 'branch' | 'tag'
    is_default INTEGER DEFAULT 0,        -- the bundled community source
    enabled INTEGER DEFAULT 1,
    priority INTEGER NOT NULL,           -- resolution order; lower wins
    last_sync_at TEXT,
    last_sync_status TEXT,               -- 'never' | 'success' | 'failed'
    last_commit_sha TEXT,                -- the pin comparison's durable side (tag sources)
    last_error TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT
);
CREATE UNIQUE INDEX idx_skill_sources_url_ref ON skill_sources(url, ref);
```

`agent_skills.source_id` records which source an assignment resolved from — recorded, not
keyed (the UNIQUE stays `(agent_name, skill_name)`, since two sources' copies cannot
coexist on disk). Deleting a source does **not** cascade to assignments.

**Tag pinning is the supply-chain control (AC#5).** Skills carry executable `scripts/`
that the ent#139 runner executes and ent#236 re-injects fleet-wide unattended, so the
community source — which takes public PRs — pins to a **tag we bump**, never a branch
head; custom sources, whose write access the operator controls, track a branch. A pinned
tag that resolves to a different commit than the last sync is **refused**
(`moved_tag`), never adopted. The refusal covers **both** materialization paths, which is
the whole of it: the update path via a `fetch` without `--force` plus an explicit SHA
comparison, and the **clone** path via `_refuse_moved_pin_after_clone`, which also deletes
the checkout (a failed sync that leaves the tree on disk would still serve the moved tag's
content to `list_skills` and to injection). Enforcing only the update path leaves the pin
bypassed exactly when the checkout was lost — a quarantine rename, a restored `/data`
backup, a recreated volume — and a moved tag would then reach every running agent behind a
successful-looking sync. Revocation = cut a new tag without the offending skill.

### Redis

- **Credential storage (DEPRECATED — CRED-002)**: credentials moved to encrypted files in agent workspaces (`.env` + `.credentials.enc`); legacy keys (`credentials:{id}:*`, `user:{id}:credentials`, `agent:{name}:credentials`) kept for backward compatibility only.
- **OAuth state**: `oauth_state:{state}` → `{provider, redirect_uri, user_id}`.
- **Heartbeat keys**: see [Heartbeat Liveness](#heartbeat-liveness-reliability-004-307). All heartbeat ops are within the backend Redis ACL (`-@dangerous`) and follow the `agent:*` naming convention.
- **Capacity/breaker keys**: `agent:slots:{name}` (ZSET) + `agent:slot:{name}:{eid}` (HASH — whose `timeout_seconds` field is the floor canary S-03 reads, ent#336), `agent:circuit:{name}`, `agent:dispatch:{name}`, `agent:canary_zombie:{name}` (HASH, canary R-01's per-pid zombie-dwell marker with its own 24h TTL — ent#337), `canary:drain_tick_at`, `canary:leader` (the single-cycling-worker lease, SET NX + TTL floor 900s, mirror `monitoring:leader` — #1881; global rather than `agent:`-keyed, so like `canary:e02:*` it is legitimately unregistered: clearing it on an agent lifecycle event would drop leadership for the whole fleet), `canary:alert_pending` (HASH, field = invariant id — the #1897 undelivered-alert retry store, **no TTL**: it is bounded by the invariant registry (≤16 fields) and reaped by the success/give-up `HDEL`, whereas a TTL would silently discard every pending alert after one quiet hour, which is the silent loss the key exists to prevent. Keyed by invariant id, a fixed code registry that no user can recycle, so it is global rather than `agent:`-keyed and — like `canary:leader` and `canary:e02:*` — legitimately absent from `CLEARED_KEYSPACES`: clearing it on an agent lifecycle event would drop a pending fleet-level alert) — see the respective subsystem blocks. Every **name-keyed** per-agent keyspace is enumerated once in `services/agent_runtime_state.py` (`CLEARED_KEYSPACES` / `EXEMPT_KEYSPACES`) and cleared across the agent lifecycle, so a recycled name never inherits its predecessor's state (#1560); a parity test fails CI on an unregistered `agent:*` key.
- **Resumable-turn keys**: `session_lock:*`, `session_inflight:*` — shared by both conversation surfaces; see [Resumable Turns](#resumable-turns).
- **Skills library sync lock**: `skills:library:sync` (SET NX + TTL, fail-open, check-and-delete release) serialises clone/pull across workers (ent#236). ent#237 keeps it covering the **whole sweep** rather than one lock per source: per-source locking would let two workers interleave and each publish a merged listing built from a half-updated set of checkouts, and the listing, the cache invalidation and the durable status are all library-wide. Contention returns `busy` (409), never a failure — a contended manual click must not overwrite the panel with "Last sync failed". Deliberately outside `agent:*` (the `compat_fix` precedent), so the #1560 name-keyed registry doesn't apply.
- **JWT / portal-session revocation**: `jwt:revoked:{jti}` (per-token blacklist, TTL = the token's remaining life, #187) and `portal:revoked_before:{email}` (a single per-email *cutoff* timestamp; `decode_portal_session` rejects a portal token whose `iat` is at or before it, TTL = the max portal-session lifetime). The cutoff form exists because `jti` is random per token and nothing indexes email → issued jtis, so "revoke every session this address holds" is O(1) instead of an index maintained at every mint — and it therefore cannot be forgotten on a new mint path. Both fail open on Redis; an edition-agnostic primitive (`dependencies.revoke_portal_sessions_for_email`), with the policy for *when* to call it owned by the entitled module that mints portal sessions.
- **Repo-binding locks** (ent#109): `agent:bind_op:{name}` (SET NX + TTL, double-submit guard) and `agent:bind_dest:{sha256(lower(destination_repo))}` — the latter keyed by **destination, not agent name**, because the collision is between two *different* agents targeting one repo. Both **fail closed** (503 + `Retry-After`), unlike `agent:data_op:`. Registered in `agent_runtime_state.EXEMPT_KEYSPACES` with reasons (clearing either mid-operation would unserialize the very operation that asked for the recreate).
- **Compatibility fix lock**: `compat_fix:{name}` (SET NX, 30s TTL) serialises the per-agent gitignore auto-fix read-modify-write (#668); ownership-checked via the shared `SingleFlightLock` (#1920 — was a constant-"1" + unconditional-delete twin of system_seed's bug).
- **Skill injection / removal lock**: `skill_inject:{name}` (SET NX + TTL, fail-open) serialises injection against removal — both mutate `~/.claude/skills/` and read-modify-write CLAUDE.md (ent#183 / ent#236). Deliberately outside `agent:*` (the `compat_fix` precedent), so the #1560 name-keyed registry doesn't apply. **Skills auto-sync leader**: `skills:sync:leader` (SET NX, TTL 3× interval, own-lease refresh, fail-open) — one worker per cycle (ent#236).
- **Operator-queue sync keys** (#1632): `opqueue:leader` (SET NX, TTL `max(3×poll-interval, 30s)` floor — the single-syncing-worker lease, mirror `monitoring:leader`) and the create rate-limit windows `ratelimit:operator_queue_create:{agent}` + `ratelimit:operator_queue_create:_fleet` (ZSET, via `rate_limiter.check`, fail-open). Not `agent:*`-named, so the #1560 name-keyed registry doesn't apply; both fail open.
- **Subscription-headroom keys** (#471): `subscription:headroom:{id}` (JSON provider snapshot, 7d TTL, best-effort DEL on subscription delete) + `subscription:headroom_probe:{sid}` (probe `SingleFlightLock` #1920). Subscription-id-keyed, deliberately not `agent:*` (the #1560 name-keyed registry doesn't apply); ambient probing is fail-CLOSED on an unanswered Redis read — see `subscription_headroom_service`.
- **Ephemeral-agent keys** (trinity-enterprise#69): `ephemeral:quota:{owner_id}` (owner-keyed atomic ghost-quota counter — deliberately not `agent:*`, so the #1560 name-keyed registry doesn't apply) and `ephemeral:discard:{name}` (SETNX+TTL discard lock, acquired/released through `SingleFlightLock` #1920). Both fail-open.
- **System-seed provisioning lock** (trinity-enterprise#124, hardened #1920): `system_seed:provision` (SETNX + 600s TTL) — one first-run seeding pass at a time; acquired + released through `SingleFlightLock` so the release is ownership-checked (unique per-acquire token + compare-and-delete), no longer a tokenless `delete` that could remove a successor's lock. Not `agent:*`-named; fail-open; the reserved-name existence backstop is the real duplicate guard.
- **Single-flight lock consolidation (#1920).** The SETNX single-flight sites — `ops:fleet_restart`, `ephemeral:discard:{name}`, `skill_inject:{name}`, `skills:library:sync`, `system_seed:provision`, `cornelius:provision`, `compat_fix:{name}` — now share ONE ownership-checked primitive, `redis_breaker_util.SingleFlightLock` (mint-per-acquire token, GET-then-DELETE compare-and-delete). This unifies the **single-flight family only**: **four lock idioms remain** after this change — the shared `SingleFlightLock`, the async Lua-CAD `ResumeLock` (`session_turn_service.py`), the two verbatim leader leases (`monitoring:leader` / `opqueue:leader`, stable cross-cycle worker id), and canary's Lua-CAD leader lease (`canary:leader`, #1881) — kept separate by design. A static guard (`tests/unit/test_1920_no_hand_rolled_single_flight.py`) fails CI when a new hand-rolled `set(..., nx=True, ex=...)` single-flight lock appears outside `SingleFlightLock` with no allowlist entry.
- **Fleet-restart lock** (#1860, hardened #1919): `ops:fleet_restart` — SETNX + 2100s TTL single-flight guard for `POST /api/ops/fleet/restart` (409 on contention); TTL is sized above the slowest single agent (skill injection alone is bounded by `skill_service._INJECT_LOCK_TTL_SECONDS`=1800 — constants comment-linked). The live loop's per-agent refresh is an **ownership-checked pre-action gate** (GET-compare → EXPIRE, never a bare EXPIRE): a **foreign** token (concurrent caller took the lock after a lapse) stops the run — partial results stand, `summary`/audit carry `stopped_early="lease_lost_foreign"` + `processed` vs `total`; an **absent** token (lapsed, unclaimed) is re-acquired via SETNX and the run continues (`lease_reacquired` audited); acquire sits inside the `try/finally` so nothing can leak the lock. Release is compare-and-delete (shared `redis_breaker_util.lock_token_matches`), attempted even after detected loss (foreign-safe by construction). Not `agent:*`-named (fleet-scoped, #1560 registry doesn't apply); fail-open when Redis is down (a refresh Redis error is throttle-logged, never treated as loss).
- **SSH-port reservations + first-run seed pass lock** (#2215): `port_alloc:{port}` — transient per-port reservation bridging the allocator's check and `containers.run` (SETNX + TTL 600s at allocation; SET no-NX by `reserve_port_for_recreate` across the recreate gap). **No release path by design**: once the container exists its `trinity.ssh-port` label is the durable truth (Invariant #11), so the key self-expires; deliberately not `agent:*` (port-keyed, and no lifecycle event should clear it — clearing on an agent event would un-reserve a port mid-create; the `ephemeral:quota:` precedent). `first_run_seed:provision` — pass-level lock serialising BOTH first-run seeders across workers (SETNX, uuid token, TTL 900s, `lock_token_matches` compare-and-delete release, fail-open; loser skips the whole pass). Both fail open; neither is in the #1560 registry.

---

## Authentication & Authorization Architecture

### 1. User Authentication (Human → Platform)

| Mode | Flow | Token |
|------|------|-------|
| **Email** (primary) | Email → 6-digit code → `POST /api/auth/email/verify` | JWT with `mode: "email"` |
| **Admin** (secondary) | Password → `POST /api/token` | JWT with `mode: "admin"` |

- Email whitelist controls who can login via email; admin login always available for 'admin'.
- **JWT revocation on logout** (#187): every access token carries a random `jti`; `POST /api/auth/logout` writes `jwt:revoked:{jti}` to Redis with a TTL equal to the token's remaining life, and `get_current_user` / `decode_token` (WS) / `/api/auth/validate` (nginx) reject a revoked `jti`. Closes the "exfiltrated 7-day token survives logout" gap (pentest 3.3.4). Fail-open (Redis down or a legacy no-`jti` token → not revoked), so the check can never lock out a valid session; backend restart still rotates `SECRET_KEY` and invalidates everything. Token-lifetime reduction + refresh tokens deferred (separate issue).
- **4-tier role hierarchy** (ROLE-001): `user` < `operator` < `creator` < `admin`. Agent creation requires `creator`+. Enforced via `require_role()` in `dependencies.py`.
- **Whitelist-driven role on first login** (#314): new email users inherit the `default_role` on their `email_whitelist` row (fallback `user`). Callsites pass explicit intent — `/share` and access-request approvals → `user` (chat-only grant); public `/api/access/request` self-signup → `user`; admin whitelist UI → caller-specified. Owners promote collaborators explicitly via `PUT /api/users/{username}/role`. Closes a privilege escalation where any access grant silently promoted the recipient to `creator`.
- **Public self-signup is default-OFF** (trinity-enterprise#10): the unauthenticated `POST /api/access/request` returns **403** unless an operator opts in via `PUBLIC_ACCESS_REQUESTS_ENABLED` (env) or the `public_access_requests_enabled` system setting. When off it never auto-whitelists, so the email whitelist stays authoritative against self-enrollment. Login-code requests for already-whitelisted emails are unaffected.

### 2. MCP API Keys (User → MCP Server)

Created via UI `/settings?tab=mcp-keys`; format `trinity_mcp_{random}` (44 chars); SHA-256 hash stored in SQLite; sent as `Authorization: Bearer trinity_mcp_...`; MCP server validates via `POST /api/mcp/validate`.

Client config (`.mcp.json`):
```json
{
  "mcpServers": {
    "trinity": {
      "type": "http",
      "url": "http://localhost:8080/mcp",
      "headers": { "Authorization": "Bearer trinity_mcp_..." }
    }
  }
}
```

### 3. MCP Server → Backend (Key Passthrough)

The FastMCP `authenticate` callback validates the user's key via the backend and returns the `McpAuthContext`; MCP tools then call the backend API with the user's own key — the backend's `get_current_user()` accepts JWT OR MCP API key. In production (`MCP_REQUIRE_API_KEY=true`) the MCP server holds NO admin credentials.

### 4. Agent MCP Keys (Agent → Trinity MCP)

Each agent gets an auto-generated agent-scoped key (`scope='agent'`, `agent_name` stored for permission checks), injected as `TRINITY_MCP_API_KEY` env var and auto-added to the agent's `.mcp.json` pointing at the internal URL `http://mcp-server:8080/mcp`.

**Regenerable + self-healing (#1854).** The injection the agent server runs on every start early-returns unless **both** `TRINITY_MCP_URL` and `TRINITY_MCP_API_KEY` are set, and the creation-time mint is `try/except`-swallowed and sets them together — so a failed mint leaves an agent that never self-heals, and a Trinity-pointing `.mcp.json` entry under any name other than the literal `trinity` is never touched at all. Three surfaces close that: a **container config-truth probe** (`POST /api/agents/{name}/mcp-key/verify` — one `docker exec` returning ONLY `sha256(bearer token)` per entry, matched against `mcp_api_keys.key_hash`), a **start-time drift predicate** (`check_agent_mcp_key_matches`, the ninth `check_*_matches` in `start_agent_internal`; exempts `trinity-system` and ephemeral ghosts; fail-safe on error), and **owner-driven rotation** (`POST .../mcp-key/regenerate` — mint → reconcile `spawned_by_key_id` → deliver → DELETE the *captured* superseded ids; fail-closed lock; DB-only for a stopped agent; **returns no plaintext**). The self-heal takes the **same** per-agent lock as rotation and does nothing at all without it — the two run the identical capture→mint→DELETE sequence and there is no per-agent start lock, so an unserialised heal can delete the key a concurrent heal is about to bake in; a skipped pass mutates nothing and the next start retries. Delivery rides a new `env_overrides` kwarg on `recreate_container_with_updated_config`, applied last. No `docker/base-image/**` change — it works on every deployed agent with no rebuild. See [agent-mcp-key.md](feature-flows/agent-mcp-key.md).

### 5. Agent-to-Agent Permissions

Enforced at the **MCP server layer** (`src/mcp-server/src/tools/`), not the backend REST API: `list_agents` returns only permitted agents + self; `chat_with_agent` blocks non-permitted targets. The backend resolves agent-scoped keys to the owner user and applies standard ownership/sharing checks (`current_user.agent_name` is used only by notifications and event subscriptions). **Restrictive default**: new agents start with zero permissions; grants are explicit via the Permissions tab (`agent_permissions` table).

**Residual, stated plainly (#1854).** The enforcement is `scope === "agent"`-conditional, so it holds only while the container's `.mcp.json` actually carries the agent's own key. A **user-scoped** key pasted into that file authenticates as the owner and bypasses the matrix silently — the platform accepts it. #1854 **detects** this (the config-truth probe above, verdicts `foreign_user_key` / `foreign_agent_key` / `shadow_entry`) and **repairs** it (drift predicate + rotation); it does not **prevent** it. Prevention needs a trustworthy request-origin signal, which does not exist on the MCP path today — the MCP server forwards no origin marker, and client IP cannot substitute (port 8080 is published on all interfaces and the frontend also sits on `trinity-agent-network`). Deferred as Part 2b/3; if built it must be an explicit allowlist over **all five** scopes, since "reject non-agent" breaks the system agent, the connector and portal_delegate.

### 6. System Agent

`trinity-system` has `scope='system'`: bypasses all permission checks, can call any agent/tool, cannot be deleted via API. Purpose: platform operations (health, costs, fleet management).

| Scope | MCP Enforcement | Backend Enforcement |
|-------|-----------------|---------------------|
| `user` | Owner/admin/shared checks | Owner/admin/shared checks |
| `agent` | Explicit permission list (`agent_permissions`) | Resolves to owner user; ownership/sharing checks only |
| `system` | **Bypasses all checks** | Resolves to owner user (system agent owner) |
| `connector` | Consumption-only, bound to ONE agent (ent#46): connector-only tool set (`list_playbooks` / `run_playbook` / `ask`), operator tools hidden via `connectorOnly`; the playbook allow-list is authoritative | `_enforce_connector_scope` at the single auth entry point **fences it to two routes** — its bound agent's `/chat` + `/connector/playbooks`; every other path 403s (ent#46 → #118) |
| `anonymous` | #848 keyless pre-login tier, **only when `MCP_INLINE_AUTH_ENABLED`**: sees `request_login`/`verify_login` + the connector tools, which refuse to act until an email is verified. Never satisfies `operatorOnly` — `scope` stays `anonymous` after login | Holds no *key*, so it never authenticates to the backend directly; the MCP server relays over `/api/internal/mcp-auth/*` and the backend re-gates every call on `db.email_has_agent_access(agent, email)`. It is **not credential-free**, though: with nothing on the wire to re-present, the verified identity is resolved per request from a store keyed on `Mcp-Session-Id`, which makes that header **bearer-equivalent for this tier** — serve the MCP port over TLS only, keep it out of logs, and note the 30 min idle / 4 h absolute expiry is the only thing that ends a session (#2035) |
| `portal_delegate` | n/a (not an MCP tool principal) | **Fenced to a single route** — may only exchange an asserted end-user email for a portal session; every other path 403s (ent#163) |
| `ops` | n/a (not an MCP tool principal — `OPERATOR_SCOPES` is an allowlist, so it is excluded by construction) | **Fenced to a read-only route allowlist** — fleet health, telemetry, roster/capacity, execution reads and the live log relay, subscription reads. Every write 403s. Admin-minted and human-only to mint. Additionally kept OUT of `ADMIN_GATE_SCOPES`, so an admin-gated ops route must opt in with `assert_admin(..., allow_scopes={"ops"})` — or `Depends(require_admin_allowing("ops"))`, the `Depends` spelling added in #2389 because `require_admin` took no `allow_scopes` and an allowlisted route gated that way was dead to ops keys **with no opt-in available**. The opt-in makes the grant **per route** (a new ops route is inaccessible until added), and it is an **ADDITIONAL gate, never a substitute one**: the scope is admitted and `role == "admin"` still runs afterwards, so an ops key is a *narrowing* of its owner, not a *decoupling* from them. The tier therefore does **not** survive its owner being offboarded — demotion 403s it, and `get_current_user` rejects a suspended owner (#995) one layer up regardless; an earlier revision of this row claimed otherwise, which an operator would have acted on. Dropping the role check for the opted-in scope was refused: it would make the bounded tier harder to revoke than the unbounded `user`-scoped key it exists to displace, while still not delivering the claim. Mint ops keys under a service admin account that is not offboarded with people (#2323/#2389). **The fence and the gate must agree**: `GET /api/subscriptions/{id}/usage` shipped admitted by the fence and refused by its own bare `assert_admin`, so the subscription-pressure read the fence was measured for could not work — a guard asserting only the fence half gave false assurance. A live-handler scan now reds when any allowlisted route calls `assert_admin` without the opt-in, and a sibling scan reds on bare `require_admin` — closing the *shape* rather than the instance (#2389) |

Only `user`/`agent`/`system` are the credentialed **operator** tier — the allow-list `OPERATOR_SCOPES` in `server.ts`. Every other scope is deliberately outside it, and widening that set is a deliberate edit pinned by `tool-visibility.test.ts`: the gate was previously a `!== "connector"` deny-check, which admitted every scope it had not heard of — including a null auth context, where FastMCP skips `canAccess` filtering entirely (#848). `portal_delegate` (ent#163) is the clearest illustration of why the deny-check shape was untenable: it is not an MCP tool principal at all, yet a `!== "connector"` gate would have advertised it every operator tool the day it was introduced.

**`/ws/events` is a second auth entry point, and it is allowlisted too (#2389).** That handler
calls `db.validate_mcp_api_key` itself and never runs `get_current_user`, so **none** of the
fences above reach it — the ops fence's own "enforced at the single auth entry point" claim was
false for exactly one surface, and it is the broad one: the stream carries fleet-wide
`agent_activity` and execution events scoped by the **owner's** accessible agents, which for an
admin owner is everything. `dependencies.WS_EVENT_STREAM_SCOPES = {None, "user", "agent",
"system"}` gates it (close code 4003), so `ops` cannot read outside its allowlist, and
`connector`/`portal_delegate` — fenced to one or two routes everywhere else — lose a hole that
predates #2323 rather than keeping it out of politeness. An unknown future scope is refused by
construction, matching the allowlist rule below. `agent` is admitted but **not wholesale**: the
same absence of `get_current_user` also skips `_enforce_ephemeral_key_fence`, whose entire point
is that a ghost's key on an untrusted workspace must not be a fleet skeleton key — so the gate
takes the key's `agent_name` and refuses an `is_ephemeral` row, using the fence's own predicate.
That sub-check fails **CLOSED**, deliberately inverting the ephemeral fence's fail-open: that
fence guards heartbeats and result callbacks where a DB blip must not take the fleet down, while
losing this stream costs an observability client a reconnect. Presence of the `is_ephemeral` key
is what makes the answer real — `get_agent_ephemeral_info` coalesces the column for every live
row, so a dict without it is no row at all (or a stand-in), and a bare `.get()` would map that
onto the same falsy value a genuine durable agent gives.

The same reasoning governs the backend side. `scope` is a free-text column with **no CHECK constraint**, so this table is a snapshot of live values, not a closed set. Guards over it must be **allowlists**: `reject_agent_principal` + `_reject_connector_principal` between them cover only `agent` and `connector`, and a `scope='system'` key sets neither field, so both are no-ops for it while it still resolves to the key owner carrying the owner's role. `reject_non_interactive_principal` (#1854) inverts this — it passes only when `User.mcp_scope is None`, i.e. the caller came through the JWT branch — and gates the credential-lifecycle routes (agent MCP-key read/verify/rotate). Key revoke/delete (`/api/mcp/keys/*`) and the connector key mint additionally run the agent+connector pair, because `db.revoke_mcp_api_key`/`delete_mcp_api_key` skip the ownership check entirely for admins and an agent key inherits its owner's role (#1854).

### 7. External Credentials (Agent → External Services)

CRED-002 file-injection model (Invariant #12): `.env` (KEY=VALUE source of truth) + `.mcp.json` edited directly; encrypted backup `.credentials.enc` (AES-256-GCM, safe for git); auto-import on startup if `.credentials.enc` exists without `.env`. Flow: Quick Inject writes `.env` → Export encrypts to `.credentials.enc` → agent start decrypts and writes files. OAuth providers for agent credentials: Google, Slack, GitHub (PAT), Notion. Common MCP servers inside agents: google-workspace, slack, notion, github, n8n-mcp.

---

## Network Topology (#589)

Two Docker bridge networks, by design — agents physically cannot route to Redis.

| Network | Subnet | Members |
|---------|--------|---------|
| `trinity-platform-network` | 172.29.0.0/16 | redis, scheduler, vector |
| `trinity-agent-network` | 172.28.0.0/16 | agents, frontend |

Bridges (members of **both** networks): `backend` (primary HTTP API — Redis on platform side, agents on agent side), `mcp-server` (agents reach `http://mcp-server:8080/mcp` via Docker DNS), `otel-collector` (agents push metrics), `cloudflared` (prod only — proxies to backend and public agents).

**Rule:** agents are *never* on `trinity-platform-network`. Any new service that mounts the agent network must NOT connect to Redis — full stop. The three agent-container-create sites hard-code the network name `trinity-agent-network` (cited by function so the reference survives line drift): `crud.py::_create_agent_container` (#1484), `lifecycle.py::_provision_folders_and_run_agent_container`, and `system_agent_service.py::SystemAgentService._create_system_agent`.

**Redis ACL users:**

| User | Auth | Purpose |
|------|------|---------|
| `default` | `REDIS_PASSWORD` | Admin / recovery / ad-hoc ops; `+@all` |
| `backend` | `REDIS_BACKEND_PASSWORD` | Backend runtime; data ops only, `-@dangerous` |
| `scheduler` | `REDIS_BACKEND_PASSWORD` | Scheduler runtime; same access pattern |

`backend`/`scheduler` cannot run `FLUSHALL`, `CONFIG`, `SHUTDOWN`, `DEBUG`, `MIGRATE`, `REPLICAOF`, `MONITOR`, or other `@dangerous` categories. Both passwords are mandatory in `.env`; compose refuses to render without them, and `src/backend/config.py` / `src/scheduler/config.py` raise on import if `REDIS_URL` lacks credentials. Upgrade path: `docs/migrations/REDIS_AUTH.md`.

---

## Container Security

- **Non-root execution** (Invariant #17, #874): backend and scheduler as `trinity` (UID 1000), MCP server as `node` (UID 1000), frontend as `nginx` (UID 101), agents as `developer` (UID 1000). Backend needs `group_add: ${DOCKER_GID:-999}` for Docker socket access on Linux.
- `CAP_DROP: ALL` + `CAP_ADD: NET_BIND_SERVICE`; `security_opt: no-new-privileges:true`; tmpfs `/tmp` with `noexec,nosuid` (RAM-backed, default 512 MB — operator-tunable via `AGENT_TMP_SIZE` on the backend service, validated `^\d+[mg]$` with invalid→default; `noexec,nosuid` stay fixed; counts against the agent memory cgroup; creation-time, so existing agents pick up a change on recreate not restart, #1231. Heavy scratch like pip/npm/ML wheels is redirected via a default `TMPDIR=/home/developer/.tmp` on the disk-backed home volume, created at start by `startup.sh`; mount spec + TMPDIR default live in `services/agent_service/capabilities.py` so create/recreate/system-agent can't drift, #1098); no external UI port exposure; network isolation per Network Topology above.
- **Bounded container logs (#1871).** Docker's `json-file` driver has no default `max-size`/`max-file`, so every container log grew forever under `/var/lib/docker/containers/` — silently, until the Docker data root hit 100%, dockerd could no longer parse its own logs, and the whole fleet wedged at once (2026-07-27). Two halves, because compose cannot reach agents: the platform services share an `x-logging` anchor in **all three** compose files (`CONTAINER_LOG_MAX_SIZE`/`_MAX_FILE`, default `10m`×3; the #2280 hosted file's parity with prod is CI-guarded), and SDK-created agent containers use `AGENT_LOG_CONFIG` in `services/agent_service/capabilities.py` (`AGENT_LOG_MAX_SIZE`/`_MAX_FILE`, same default), threaded into the three agent-container create sites beside `AGENT_TMPFS_MOUNT`. Validation is fail-safe in **both** directions — malformed *and* out-of-range (>1g, >10 files) fall back to the bounded default, because a well-formed absurd value like `1000g` passes a format-only check while effectively removing the cap, which is the exact failure the constant prevents; an explicitly-set rejection logs a WARNING (a silently-ignored knob is the #1039 inert-by-obscurity class). Creation-time like the tmpfs spec: platform services adopt on the next `docker compose up`, existing agents on **recreate**, not restart. The raw Docker log is a *secondary* copy — Vector's aggregate at `/data/logs` is the primary queryable one and keeps its own `LOG_RETENTION_DAYS`; live streaming is lossless across rotation (only post-hoc `docker logs` history shortens, and the UI's log endpoint defaults to `tail=100`). `tests/unit/test_1871_log_config_parity.py` is the CI guard that fails when a **new** durable-container create site ships without `log_config` (the `learnings.md` 2026-07-10 "the create path is never one call site" class).
- **Internal API security (C-003)**: `/api/internal/` endpoints (scheduler, agent containers) require the `X-Internal-Secret` header; falls back to `SECRET_KEY` if `INTERNAL_API_SECRET` unset.
- **Agent-server inbound auth (#1159)** (details in [agent-server-authentication.md](feature-flows/agent-server-authentication.md)): every backend→agent call carries a per-agent `X-Trinity-Agent-Token` = `HMAC-SHA256(AGENT_AUTH_SECRET, "trinity-agent-auth:v1:"+name)` — *derived*, not stored; the master lives only in backend env, so a compromised agent can't compute a sibling's token. A **pure-ASGI** middleware (`docker/base-image/agent_server/middleware/auth.py`) enforces it on **all** HTTP **and** WS routes via constant-time compare, exempting only exact `/health` (+ `OPTIONS`) — pure-ASGI (not `BaseHTTPMiddleware`) so it gates WS scopes too and never buffers SSE. The dead unauthenticated `/ws/chat` route (ran arbitrary Claude) was removed; CORS dropped (internal-only). Grace path: empty `TRINITY_AGENT_AUTH_TOKEN` → allow (old-image); `check_agent_auth_token_env_matches` forces a one-pass recreate so a missing/stale token re-injects. Backend fail-closed (`derive_agent_token` raises on empty secret; `start.sh` auto-generates the hex32 master, all three compose files forward it (dev, prod, and the #2280 hosted file)). Callers route through `services/agent_auth.py`; a static guard (`tests/unit/test_agent_auth_header_guard.py`) fails any raw `agent-{name}:8000` caller that skips them.
- **WebSocket security (C-002, #550)**: single-use ticket auth — see [Real-time Delivery](#real-time-delivery-reliability-003-306).
- **Guardrail registration is root-owned (ent#345)**: the GUARD-002 hooks are registered in `/etc/claude-code/managed-settings.json` — Claude Code's admin-controlled managed-settings path, which takes precedence over user/project settings and sits **outside** the agent's HOME and the git-synced tree. It ships root:root `0444` inside a root-owned `0755` dir, so the file cannot be rewritten and the dir cannot be used to replace it or shadow it via `managed-settings.d`. It used to be `/home/developer/.claude/settings.json`, owned by `developer` at 0644 *inside the synced repo*: the guarded agent could edit its own guardrail registration (one `Edit` disables the bash deny-list, file guard, read-only guard and output-scanner for every later tool call — observed in the wild during #2036), and a push to the agent's repo could set it from outside. No platform-owned `settings.json` is shipped into `~/.claude` any more. The failure mode of the new path is **silent** (no registration ⇒ no hooks, nothing logged), so `startup.sh` asserts existence + non-writability each boot and logs `GUARDRAILS: ERROR …`; it reports and continues, since a registration fault must not become a fleet outage (a `/health` signal is the tracked follow-up). Read-only mode does not conflict: since #887 it writes a config file the baked hook reads, not a second registration.
- **Frontend XSS (H-005)**: all markdown rendering uses DOMPurify via `utils/markdown.js`; no direct `v-html` with unsanitized content.
- **Rate limiting (#1023)**: shared sliding-window limiter `services/rate_limiter.py` — Redis sorted-set rolling window (no fixed-window boundary burst), fail-open with bounded per-worker in-process fallback; `enforce(key, limit, window)` raises 429 + `Retry-After`, `check(key, limit, window)` is the non-raising variant for background loops. New request-rate limits reuse this primitive — don't hand-roll Redis counters. Current consumers: webhook trigger (#1023), agent reports (#918), operator-queue create ingestion (per-agent + fleet, `check`, #1632). Intentionally NOT unified under it: the auth login/OTP limiters in `routers/auth.py` are failure-counters (increment on failure, reset on success) — a different pattern. A global ASGI middleware with a route→policy table is a tracked follow-up.
- **Secret scanning (#1164)**: `.github/workflows/secret-scan.yml` runs the gitleaks MIT CLI on every PR (commit-range scope, `contents: read`, `--redact=100`) to block a re-landed credential (the `re_`-prefixed Resend key removed in #1158); config + custom `re_` rule + allowlists in `.gitleaks.toml`. Commit-time source scanning — complementary to GUARD-002's runtime output scanning. Non-blocking until a repo admin makes it a required check (follow-up).

---

## Development Environment

Local and production use the same ports. Local URLs, auth, and admin credentials: see `CLAUDE.md` / `CLAUDE.local.md`.

| Port | Service |
|------|---------|
| 80 | Frontend (nginx/Vite) — prod: `https://your-domain.com` |
| 8000 | Backend (FastAPI) — `/docs` for OpenAPI |
| 8080 | MCP Server (`/mcp`) |
| 8686 | Vector health |
| 2222–2262 | Agent SSH |

---

## Data Persistence

- **Bind mount** (survives `docker-compose down -v`): `~/trinity-data/` → `/data` — contains `trinity.db` (SQLite), `agent-files/` (FILES-001), `agent-data-tmp/` (transient export staging, #1169), `agent-import-tmp/` (transient copy-intent snapshot staging, ent#15 — free-space preflighted, `AGENT_IMPORT_MAX_BYTES`-capped, swept after 24h), and **`backups/`** (#2216 — the automatic database recovery points: `trinity-backup-YYYYMMDD.{db,dump}` nightly + `pre-migration-YYYYMMDD-HHMMSS.db` at boot; dir 0700; **same-disk scope** — protects against corruption/slips, not disk loss; retention `backup_retention_days` + `MIN_KEEP=3`).
- **Docker volumes**: `redis-data` (Redis AOF), `agent-configs`, `audit-data`, `audit-logs`, per-agent `agent-{name}-workspace` (the durable home volume — declared `data_paths` runtime data lives under `/home/developer/data` here, #1169), `agent-{name}-public` (FILES-001), and shared-folder volumes.
