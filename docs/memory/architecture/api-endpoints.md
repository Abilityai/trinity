# Trinity Architecture — API endpoint tables

> Part of the Trinity architecture set. Core map, invariants and topology: [architecture.md](../architecture.md). This file is **not** auto-loaded.
>
> **Owns**: `src/backend/routers/**`, `src/backend/main.py`
>
> **Read this before changing the paths above**: Static routes must be registered before the `/{name}` catch-all (Invariant #4). Declared after it, `GET /api/systems/manifests` answers "system 'manifests' not found" instead of listing manifests.
>
> **Write path**: changes to this area land here, not in the core (core editorial rule 4). Keep the core's map row in step if the owned paths change.

---

### Agents (37 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents` | List all agents |
| GET | `/api/agents/context-stats` | Context & activity state for all agents |
| GET | `/api/agents/autonomy-status` | Autonomy status for all accessible agents |
| GET | `/api/agents/sync-health` | Per-agent git sync health for dashboard dots (#389) |
| GET | `/api/agents/subscription-pressure` | Batch subscription-pressure rows for dashboard badges (#471): per accessible agent (pure-DB `visible_agent_names`, ent#384 helper) — `auth_mode` (`AgentAuthStatus` vocabulary), `subscription_name`, `failure_events_24h` + its auth-kind slice `auth_failures_24h` (#2352), one-gate `rate_limited_now`, the probe's own `token_status` (`ok`|`invalid_token`|`rate_limited`|`error`), fresh `utilization_5h_pct`. Explicit `response_model`; registered before `/{agent_name}` (Invariant #4) |
| POST | `/api/agents` | Create agent. Accepts an optional `display_label` (ent#1640) — the human-facing name set at creation (normalized + named-error validated like `PUT /label`); omit/blank → renders under the slug. Accepts `import_intent: fork\|copy\|clone` for `github:` templates (ent#15 — copy = backend-materialized snapshot, no sync/row/PAT; see [github-import-intents.md](../feature-flows/github-import-intents.md)) and an `Idempotency-Key` header (Invariant #18, scope `agent_create:{user_id}`) |
| POST | `/api/agents/deploy-local` | Deploy a packaged local agent (base64 tar.gz; `require_role("creator")`). #2060 integrity contract: embedded `.trinity-manifest.json` verified post-extract + post-copy (fail-closed `MANIFEST_DRIFT`/`MANIFEST_REQUIRED`), in-root symlinks preserved, caps carry observed+limit, evidence-bearing response (`verified`/counts/`compatibility_hard_count`), `Idempotency-Key` (scope `agent_deploy:{user_id}`) + per-base-name `agent:deploy_op:` lock, failed deploys compensate (residue removed, workspace volume reclaimed, stopped previous version restarted). See [local-agent-deploy.md](../feature-flows/local-agent-deploy.md) |
| GET | `/api/agents/{name}` | Get agent details |
| GET/PUT | `/api/agents/{name}/label` | Get / set-or-clear the agent's human-facing **display label** (ent#181/#1640). Owner-only (`OwnedAgentByName`); `label` is **required-but-nullable** and unknown fields are rejected (`extra="forbid"`, #1821 — an ignored extra plus a `None` default made `{"display_label": …}` a silent 200 + wipe, so `{}` and any unrecognised body now 422); an explicit null or blank clears to the slug fallback; presentation-only (the slug never moves, unlike `PUT /rename`); trims + rejects control chars/line-breaks with a **named** error, **not** unique (the slug guarantees uniqueness), audit-logged, broadcasts `agent_label_changed` |
| DELETE | `/api/agents/{name}` | Soft-delete agent (see [Soft Delete](reliability.md#soft-delete-retention--recovery-834-772)) |
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
| POST | `/api/agents/{name}/voice-reply` | Deliver one channel reply as a voice note (`send_voice_reply` MCP tool, ent#117). `AuthorizedAgentByName` + agent-scoped self-check; resolves the channel destination from the execution; fail-soft `{delivered, channel, reason}` (200) so the agent falls back to text; 409 on an in-flight duplicate for the same turn. A `source_channel="portal"` turn short-circuits with `reason="portal_client_narrated"` + `guidance` — or `portal_voice_not_configured` when no voice resolves, so the guidance never points at a speaker control that does not render (#2157). See [voice_reply_service](backend.md) |
| GET/PUT | `/api/agents/{name}/guardrails` | Per-agent guardrails config / overrides (GUARD-001) |
| GET/PUT | `/api/agents/{name}/file-sharing` | Outbound file-sharing status + quota / owner-only toggle (returns `restart_required`) (FILES-001) |
| POST | `/api/agents/{name}/shared-files` | Mint a download URL for a file in the publish dir (owner/admin or agent-scoped key) |
| GET | `/api/agents/{name}/shared-files` | List active shared files with download counts |
| DELETE | `/api/agents/{name}/shared-files/{file_id}` | Revoke a shared file (owner-only; idempotent) |
| POST | `/api/agents/{name}/user-memory` | Write per-user memory blob; email resolved from execution_id server-side (MEM-001, #888) |
| POST | `/api/agents/{name}/data/export` | Export agent `data/` as a tar (owner/admin; `?format=stream`\|`base64`; 413 over cap; per-agent op lock). See [Agent Runtime Data](agent-lifecycle.md#agent-runtime-data--data_paths--snapshotexport-1169) (#1169) |
| POST | `/api/agents/{name}/data/import` | Restore an uploaded tar into agent `data/` via the agent-server restore primitive (owner/admin; `data/**` allowlist + traversal guard; `Idempotency-Key`; op lock) (#1169) |
| POST | `/api/agents/{name}/executions/{execution_id}/terminate` | Stop an in-flight turn (existing route; ent#155 makes it the operator arm of a three-surface cancel). Service is now principal-agnostic — `current_user` optional, `actor_kind` on the activity — so the public-link and Workspace arms delegate to it instead of re-implementing cancellation. See [chat-turn-cancellation.md](../feature-flows/chat-turn-cancellation.md) |
| POST | `/api/public/executions/{token}/{execution_id}/terminate` | **Public-link arm** of the same cancel (ent#155). Token-scoped like the sibling `status`/`stream` routes, and additionally gated on `triggered_by == "public"` so a link-holder cannot stop a scheduled run, an operator's chat, or a Workspace turn on the same agent — reading a stream is passive, cancelling destroys work, and they are not the same authority. Uniform 404 on a miss. Two links on one agent can still cross-cancel (named residual) |
| POST | `/api/enterprise/client-portal/agents/{name}/executions/{execution_id}/terminate` | **Workspace arm** (ent#155), scoped per CALLER behind the same three gates as its stream: roster scope, execution-belongs-to-agent, and started-by-this-caller. A cancelled turn is classified `409 / category="cancelled"` rather than falling through the failure ladder, so it does not come back as "Something went wrong" on the next reload |
| POST | `/api/agents/{name}/heartbeat` | Agent liveness heartbeat — auth and semantics in [Heartbeat Liveness](reliability.md#heartbeat-liveness-reliability-004-307) |
| POST | `/api/agents/{name}/executions/{execution_id}/result` | Fire-and-forget terminal callback — agent's own MCP key + ownership + durable async-marker gate; finalizes via `apply_result`. 503 + Retry-After when the #1085 re-delivery governor is paused / capped (retryable). See [Fire-and-Forget Dispatch](execution.md#fire-and-forget-dispatch-1083) (#1083) |
| GET | `/api/agents/{name}/circuit-breaker` | Unified breaker state: `{dispatch:{state,failure_count,retry_after_seconds}, transport:{...}, open:bool, config:{enabled,global_enabled}}` (#526) |
| PUT | `/api/agents/{name}/circuit-breaker` | Enable/disable per-agent dispatch breaker (owner-only); engages only with global `DISPATCH_BREAKER_ENABLED` (#526) |
| POST | `/api/agents/{name}/circuit-breaker/reset` | Admin-only; resets BOTH transport and dispatch breakers to closed (#921, #526) |
| GET/PUT | `/api/agents/{name}/operator-resume` | Per-agent respond→resume opt-in (ent#329). GET any accessor; PUT **owner-only** (enabling means answers may now spend, and the bill is the owner's). Default OFF — see [Respond → Resume](#respond--resume-dispatch-ent329) |
| GET | `/api/agents/{name}/brain-orb/data` | Read-only proxy of the agent's Brain Orb `data.json` (`AuthorizedAgentByName`; byte pass-through; 404 when flag off / no export, 503/504 unreachable, 502 agent error). See [Brain Orb](integrations.md#brain-orb--self-rendering-mind-page-58-trinity-enterprise) (#58) |
| GET | `/api/agents/{name}/brain-orb/scopes` | List the agent's selectable + active vault scopes for the orb scope panel (`AuthorizedAgentByName`; 404 when unsupported). (#58 Phase 2) |
| POST | `/api/agents/{name}/brain-orb/scope` | Mutate the active scope set → agent re-export (**`OwnedAgentByName`** — owner/admin; body-capped; 404 when unsupported). (#58 Phase 2) |
| POST | `/api/agents/{name}/brain-orb/voice-token` | Mint a short-lived, config-locked Gemini Live **ephemeral token** for the client-held voice tile (`AuthorizedAgentByName`; per-(user,agent) rate-limited; 404 when the runtime-resolved base or voice flag is off (#85), 503 no key, 502 mint error). Response field `ephemeral_token`. (#60 Phase 3) |
| POST | `/api/agents/{name}/brain-orb/tool` | Read-only KB search — proxies to the agent's `~/.trinity/brain-orb/search` hook (`AuthorizedAgentByName`; 404 when unsupported). (#60 Phase 3) |
| GET | `/api/agents/{name}/compatibility` | Compatibility report (`?include_ai=` forces fresh AI; STATIC live + persisted AI). Non-blocking; `unavailable` when stopped. See [Agent Compatibility Validation](agent-lifecycle.md#agent-compatibility-validation-668) (#668) |
| POST | `/api/agents/{name}/compatibility/fix` | Owner/admin; apply a gitignore auto-fix (`{check_id}`). 400 non-fixable, 409 concurrent fix. Uncommitted until next git sync (#668) |
| GET | `/api/agents/{name}/mcp-exposed` | MCP-exposure flag + the deterministic `tool_name` the MCP server would register. See [MCP Exposure](integrations.md#mcp-exposure--dedicated-dynamic-tools-846) (#846) |
| PUT | `/api/agents/{name}/mcp-exposed` | Owner-only; toggle exposing the agent as a dedicated `chat_with_<slug>` MCP tool (`{enabled}`). System agent → 403. No restart — MCP server picks it up on its next poll (#846) |
| GET | `/api/agents/{name}/mcp-key` | The agent's own `scope='agent'` Trinity MCP key: prefix / scope / created / `last_used_at` / usage + a health state (`missing`\|`env_absent`\|`env_mismatch`\|`never_used`\|`stale`\|`active`\|`exempt`). Never the secret. `OwnedAgentByName` + `reject_non_interactive_principal` (#1854) |
| POST | `/api/agents/{name}/mcp-key/verify` | Container config-truth probe — one `docker exec` returning ONLY `sha256(bearer)` per `.mcp.json` entry; verdicts `ok`\|`foreign_user_key`\|`foreign_agent_key`\|`unknown_key`\|`not_configured`\|`shadow_entry`\|`unavailable` (stopped container degrades, never 500). Rate-limited (#1854) |
| POST | `/api/agents/{name}/mcp-key/regenerate` | Rotate the agent key: 409 for `trinity-system`/ephemeral before any mutation → fail-**closed** Redis lock (503 down, 409 contention) → capture-before-mint → reconcile `spawned_by_key_id` → deliver (running: `clear_agent_breakers` + recreate + exact-key post-condition; stopped: DB-only, stays stopped) → DELETE the *captured* superseded ids. **Returns metadata only, no plaintext.** Rate-limited per agent and per actor (#1854) |

**Note**: Route ordering is critical — static routes (`/context-stats`, `/autonomy-status`) must be defined BEFORE the `/{name}` catch-all (Invariant #4).

### Voice (9 endpoints)
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
| WS | `/api/voip/voice/{call_id}` | Call-bound ticket | Twilio Media Streams audio bridge — see [VoIP](integrations.md#voip-telephony-voip-001-1056) |

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
| POST | `/api/agents/{name}/git/reset-to-main-preserve-state` | Recovery reset — see [Git Sync Health](agent-lifecycle.md#git-sync-health-389390) |
| POST | `/api/agents/{name}/git/bind-to-own-repo` | **Bind to a repo the caller owns** (ent#109) — create it if needed, push the agent's CURRENT workspace history, repoint `origin`, persist the per-agent PAT, re-bake the container env. `OwnedAgentByName` **+ `reject_agent_principal`** (human-only), `Idempotency-Key` verb-folded. See [Post-Creation Repo Binding](agent-lifecycle.md#post-creation-repo-binding-ent109) |
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

### Schedules (20 endpoints)
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

### Auth, Users & MCP (21 endpoints)
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
- **Two callers, one rule (ent#430).** The Workspace ask answer
  (`client_portal/asks/service.py::answer_ask`) is the second, and it had to
  learn the CAS rule the hard way: `respond_to_operator_queue_item` signals a
  lost race by returning a **truthy** dict carrying `_status_conflict`, so a
  plain `if not updated` let the loser dispatch — spending on an answer that is
  not in the database, and, because the idempotency key digests the answer TEXT,
  spending TWICE for one queue item. Guarded now by enumerating every caller
  rather than the one route ent#329 knew about, so a third site inherits the
  rule instead of re-losing it.
- **The dispatch must survive a sync caller.** `spawn_resume_dispatch` was
  `asyncio.create_task`, which needs a RUNNING loop and gets one only from an
  `async def` endpoint. The operator route is one; the Workspace ask route is a
  plain `def`, which FastAPI runs through `run_in_threadpool` — a worker thread
  with no loop — so it raised `RuntimeError: no running event loop`, the
  caller's `except` swallowed it, and every client answer recorded the answer
  and dispatched nothing. It now hops back to the host loop via
  `anyio.from_thread.run_sync` when there is no running loop. The tests could
  not see it because every one of them monkeypatched the spawn; the guard is a
  test that drives the real function from a real worker thread.
- **`resume_requested` reports what was SCHEDULED**, not what the opt-in
  permits — set only after the spawn returns, so a spawn that raised answers
  `false`. It is a report of intent, not a promise: the opt-in is read once here
  and again inside the dispatch, and an owner disabling it in between gets an
  over-report that the FAILED row and the audit entry above are the remedy for.
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
  unresolved slash command earns an alert). It is **stranded** for pull mode.
  Until #2391 that was structural — dispatched by a direct backend call, never by
  `POST /task`, so `_derive_task_trigger` could not emit it (#2048). Since #2391
  gave `task_execution_service` a pilot-gated `queue_persistent` policy it is a
  **choice**: the respond endpoint records `result.status` as the dispatch
  receipt (the `operator_resume_dispatch` audit row above and the #525
  idempotency completion), and `queued` is not the outcome that contract
  reports. It is absent from `pull_pilot.PULL_REACHABLE_TRIGGERS` deliberately.

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

All four require `X-Internal-Secret` **AND** `MCP_INLINE_AUTH_ENABLED`; with the flag off the whole surface **404s** (a disabled deploy does not advertise it). The secret authenticates the *caller*, never the *action* — the last two re-gate on the asserted email's own standing via `assert_email_may_reach_agent` → `db.email_has_agent_access(agent, email)` + connector-enabled, so a compromised MCP server cannot reach an agent that email cannot. See [mcp-connector.md](../feature-flows/mcp-connector.md) and requirements `mcp.md` §7.6.

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
| GET | `/api/settings/feature-flags` | Public-safe UI gating flags (any auth user): `session_tab_enabled`, `voice_available` (`VOICE_ENABLED && GEMINI_API_KEY`), `workspace_available` (voice AND `WORKSPACE_ENABLED`, #860), `voip_available` (#1056), `brain_orb_available` (runtime-resolved: `system_settings` override → `BRAIN_ORB_ENABLED` env opt-in → OFF; gates the Brain Orb page — #58/#85), `brain_orb_voice_available` (`base && voice && GEMINI_API_KEY`, all but the key runtime-resolved, default OFF; gates the client-held voice tile — #60/#85), `mcp_agent_chat_pull_enabled` (#946 observability-only; routing gate is the MCP server's own `MCP_AGENT_CHAT_PULL_ENABLED`, default OFF), `redelivery_governor_enabled` (#1085 observability-only; default OFF), `canary_enabled` (#2217 observability-only — whether the canary harness is enabled; last-cycle/stale/sink detail stays admin-only on `GET /api/canary/status`; default OFF), `tts_available` (ent#117 — an ElevenLabs key resolves via stored setting → `ELEVENLABS_API_KEY` env; gates the voice config UI + `send_voice_reply`), `a2a_outbound_available` (#736 — the outbound-A2A kill switch: `system_settings` → `A2A_OUTBOUND_ENABLED` env → **OFF**; both call routes 404 when off, so this is observability/UI gating, not the enforcement), `install_source` / `marketplace_install` / `install_tls_posture` (#2380 — how this instance was installed, the resolved marketplace gate, and what URL posture it *advertises*. A string on a mostly-boolean surface, the `platform_default_model` precedent; `marketplace_install` is resolved server-side so the browser holds no copy of which channels count), `telemetry_sharing_enabled` / `_hard_disabled` / `_dismissed` / `_first_value` (ent#12/ent#437 — the four booleans the Finish-setup consent card gates on, served here so the card decides from a document the page already awaits and never calls the admin status route on a load it will not act on; fail-safe in the hidden direction), `enterprise_features` (registered enterprise modules; empty in OSS-only or `TRINITY_OSS_ONLY=1`) (#847) |
| POST | `/api/settings/retention/acknowledge` | Approve ONE over-threshold retention prune (#1644). Admin-only **and human-only** (`reject_agent_principal` — `require_admin` alone is insufficient: an agent-scoped key resolves to its owner carrying the owner's role, and the default install is admin-owned; see trinity-ops-agent#232). Body `{key, window_days}`; **409** unless `window_days` matches the window in force, so an ack always names the deletion it authorizes. Single-use — consumed once the prune runs. **This endpoint is the gate**; the operator-queue alarm authorizes nothing. Audit-logged |
| GET | `/api/settings/retention` | Effective data-retention windows + active edition (admin-only, #1039). Also reports the fixed `guard.max_rows` read-only (#1644 — not settings-backed). Reports log-archival, execution log/row, health-check, agent/schedule soft-delete, and the audit-log window (365-day floor, exempt). `edition` is `enterprise` when an entitled override is registered (via the #847 entitlement seam), else `community`. Precedence is **`db-row → code-default`** for the five OPS windows (env drives log archival only — the previously advertised `enterprise → env → community-default` was never implemented, #1638) plus a per-key `sources` map (`db-row`\|`code-default`). OSS does not hard-clamp; the 5-day floor applies to fresh installs via the seed — see [Cleanup Service sweeps](background-services.md#background-services). **#2216:** also carries a `backup` block (last status/success/age, artifact count+bytes, `enabled`, `retention_days`, `min_keep`, `stale`, `scope: "same-disk"`) and **excludes `backup_retention_days` from the generic `windows` map** — its coercion is inverted (garbage → 14, never → `_ops_int`'s 0 = keep-forever), so it renders only through the service's one shared reader |
| GET/PUT | `/api/settings/agent-defaults/resources` | Fleet-wide default CPU/memory for new containers (admin-only; CPU 1/2/4/8/16, memory 1g–32g) (RES-001) |
| GET/PUT | `/api/settings/agent-defaults/access-policy` | Fleet-wide default `require_email` for new agents (admin-only, #1129). Stored in `system_settings`, **secure-by-default ON** (code fallback when unset — no migration); seeds `agent_ownership.require_email` at creation (`register_agent_owner`) for **new** agents only, never rewrites existing rows; owners still override per agent via `PUT /api/agents/{name}/access-policy` |
| GET/PUT | `/api/settings/max-parallel-tasks-ceiling` | Fleet-wide ceiling on per-agent `max_parallel_tasks` (admin-only, #506). Returns `{value, default, min, max}`; PUT range-validated 1–32 (400 otherwise), audit-logged. Stored in `system_settings` (no migration). The generic catch-all `PUT /{key}` is blocked for this key (422 → dedicated route). Clamp is runtime/clamp-on-use — see [Capacity & Backlog](execution.md#capacity--backlog-428) |
| GET | `/api/skills/assignments` | **Which agents hold each skill**, batched (ent#384). `get_current_user` **+ `reject_agent_principal`** + a `response_model` (`name`/`display_label` only — the ent#334 rule from this same router). Admin unfiltered; everyone else owned ∪ shared, with the accessible set derived from `db.get_all_agent_metadata()` (pure DB) rather than `accessible_agent_names`, whose `list_all_agents_fast()` returns `[]` on any Docker fault and would report *no agent holds any skill* fleet-wide behind a throttled WARNING. Carries `scope: all|accessible` so an empty accessible set is worded honestly instead of asserting zero holders. Excludes soft-deleted agents (#834 preserves their rows up to 180 days) and ephemeral ghosts. Also carries `assignable_agents` (ent#386) — the agents this caller may assign TO, a strictly different set from the holders above (holders are owned ∪ shared; the skill write routes are owner-or-admin), computed server-side so the browser holds no second copy of an authorization predicate, and filtered identically to the holder list so the dropdown can never offer an agent the chips could never show. **OSS-core by decision** — see below |
| GET/POST | `/api/skills/sources` | List / register skill sources (ent#237). Admin-only **and human-only** — `reject_agent_principal` on the mutations *and on the LIST read*, since the rows carry private repo URLs and an agent-scoped key resolves to its owner carrying the owner's role (ent#293). URLs locked to github.com and rejected for embedded credentials on write. See [Database Schema → skill_sources](database.md#sqlite-datatrinitydb) |
| PUT/DELETE | `/api/skills/sources/{source_id}` | Patch (name/url/ref/ref_type/enabled/priority) / remove a source. Admin + `reject_agent_principal`. A `url`/`ref`/`ref_type` edit **clears the sync bookkeeping** (`last_commit_sha` + status/timestamp/error) — the tag pin's baseline is only meaningful for the ref it was recorded against. DELETE reclaims the source's checkout (row first, disk second); assignments are not cascaded — the skill keeps resolving through whatever source still provides it (ent#237) |
| POST | `/api/skills/sources/{source_id}/sync` | Sync ONE source, leaving the others untouched. Admin **and human-only** (`reject_agent_principal`) — a sync clones executable material and can spawn the fleet re-inject, so it is not "use". Runs off the event loop; **409** on lock contention, mirroring the full-sweep route (ent#237) |
| GET/PUT | `/api/settings/skills-library` | Skills-library lifecycle automation (admin-only, ent#236). GET: `auto_sync_enabled` / `auto_sync_interval_seconds` / `auto_reinject_enabled` + interval bounds, **plus** the durable sync status (`last_sync`, `last_sync_status`, `last_sync_error`) and the last fleet-re-inject report — the panel must be able to show a *failing* auto-sync. PUT: partial update (an omitted field is untouched), interval range-validated 300–86400 with a descriptive 400 rather than a silent clamp; audit-logged. The three keys are blocked on the generic `PUT /{key}` (unvalidated `Dict[str,str]`; `"10"` would be accepted verbatim and fetch GitHub six times a minute — #1644 class). Registered before `/{key}` (Invariant #4) |
| GET/PUT | `/api/settings/brain-orb` | Brain Orb platform flags (admin-only, trinity-enterprise#85). GET: per-flag `{value, source: override\|env\|default}` + `gemini_key_configured` (boolean only — never the key). PUT: partial booleans (`enabled`/`voice_enabled`/`write_enabled`) and/or `clear: [flag,…]` reverting a flag to its env/default (400 on unknown name or set+clear conflict); audit-logged with per-flag old→new. Stored in `system_settings` (no migration); route gates resolve at request time — no restart. Registered before `/{key}` (Invariant #4) — see [Brain Orb](integrations.md#brain-orb--self-rendering-mind-page-58-trinity-enterprise) |
| GET/PUT | `/api/settings/elevenlabs` | ElevenLabs / voice platform settings (admin-only, ent#117). GET: `{key_configured, key_source: override\|env\|none, default_voice_id}` — the key value is never echoed. PUT: partial `{api_key?, default_voice_id?, clear: ["api_key"\|"default_voice_id"]}`; key stored AES-256-GCM encrypted (Invariant #12) in `system_settings`; runtime-resolved (no restart); audit-logged masked. Registered before `/{key}` (Invariant #4) |
| GET/PUT/DELETE | `/api/settings/a2a-endpoints` | The OSS outbound-A2A endpoint registry (#736) — the target source `call_a2a_agent` resolves names against. Admin **+ `reject_agent_principal`**: registering an endpoint decides where a credentialed server-side request may go, so it is the GRANT half of the grant-vs-use line, and an agent-scoped key resolves to its owner carrying the owner's role. Credentials are **write-only** (reads report `has_credentials` only); URL is SSRF-validated on write for the operator's sake, and re-validated on every call regardless. Stored as ONE AES-256-GCM envelope in `system_settings` — no table, no migration. A `ref` (id **or** name) resolves and deletes **first-match-wins** through one shared predicate, so DELETE removes exactly the record the same ref resolves to — never two (#2174: id/name are separate namespaces with no cross-uniqueness, so a filter-out-every-match delete could destroy a second endpoint and its credential while reporting one success); a new endpoint may not be *named* after an existing id, which stops the collision at the source without stranding an already-stored one. Blocked on the generic `PUT /{key}`; declared before `/{key}` (Invariant #4) |
| PUT/DELETE | `/api/settings/api-keys/{anthropic,github}`, `/api/settings/slack`, `/api/settings/slack/connect` | The credential writers. Unchanged in shape and auth (admin-only, masked reads, env fallback), but since ent#435 they persist through `settings_service.set_secret_setting` — AES-256-GCM under `<key>_encrypted`, never a cleartext row (Invariant #12). DELETE clears **both** forms, so unconfiguring a not-yet-migrated install is complete. The `source: settings\|env` field on the status reads comes from `has_secret_setting` (presence in either form, never a decrypt — so a row written under a rotated key still honestly reports *settings*). `slack_client_id` stays a plain row: it is a public OAuth identifier |

### Session Endpoints
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agents/{name}/session` | Create session row (first turn cold; writes JSONL so turn 2 resumes) |
| GET | `/api/agents/{name}/sessions` | List caller's sessions (per-user scoped; `?status=active`) |
| GET | `/api/agents/{name}/sessions/{id}` | Session row + most-recent `?limit=N` (default 100, max 500) messages |
| POST | `/api/agents/{name}/sessions/{id}/message` | The turn endpoint (`{message, model?, timeout_seconds?}`) — semantics in [Resumable Turns](execution.md#resumable-turns) |
| POST | `/api/agents/{name}/sessions/{id}/reset` | Clear cached UUID (next turn cold); best-effort JSONL reap |
| DELETE | `/api/agents/{name}/sessions/{id}` | Delete session + messages; best-effort JSONL reap |

### Enterprise Modules (#847)

Open-core seam (generic mechanism only). The public backend exposes an extension point: `main.py` conditionally `register_enterprise(app)` (no-op `ImportError` in OSS-only builds); each registered module calls `entitlement_service.register_module("<id>")`, and the registry drives `feature-flags → enterprise_features`, which the OSS Vue bundle reads to show/hide gated surfaces. `requires_entitlement("<id>")` in `dependencies.py` gates an entitled endpoint (403 unentitled; 404 when the submodule is absent). `TRINITY_OSS_ONLY=1` hard-empties the registry. Private enterprise tables migrate via the separate two-track runner (Invariant #3).

Install/verification surface (#1443): both private submodules carry `update = none` in `.gitmodules` — OSS clones init without credentials; mounting is a config-first per-clone opt-in (`git config submodule.<path>.update checkout`, then init) documented in `docs/ENTERPRISE.md` (mount, HTTPS-PAT override, rebuild, verify). `GET /api/version` reports `edition` + `enterprise_features` from the same registry.

> The catalog of specific enterprise modules, their private schema, and the commercial rationale are intentionally **not** documented in this public repo — they live in the private `trinity-enterprise` repository (see `docs/memory/ENTERPRISE_DOCS.md` there). Public docs describe the generic seam only.

