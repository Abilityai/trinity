# Feature Flows Index

> **Purpose**: Maps features to detailed vertical slice documentation.
> Each flow documents the complete path from UI → API → Database → Side Effects.
>
> For detailed change history, see `git log`.

---

## Recent Updates

> Newest ~20 changes only — older entries are discoverable via the **Documented Flows**
> category tables below, the linked flow doc, and `git log`. (Capped per #1360 to keep
> this index minimal and navigable.)

| Date | ID | Change | Flow |
|------|-----|--------|------|
| 2026-07-07 | #1500 | fix(ui): Tasks tab joins `FULLSCREEN_TABS` — task list flex-fills the viewport (old `max-h-96` cap becomes the `min-h-96` floor), panel root is the short-viewport fallback scroller; e2e fill/parity spec + `.first()` repair of the #954 spec broken by #1114's measuring mirror | [tasks-tab.md](feature-flows/tasks-tab.md) |
| 2026-07-07 | trinity-enterprise#107 | feat: auto-seed a default **Cornelius** second-brain agent on fresh install with the Brain Orb enabled — bundled `local:cornelius` template (pre-generated seed graph so the orb renders immediately); first-run-only (durable `cornelius_seeded` flag) + fresh-install-scoped (`count_non_system_agents()`) `ensure_seeded()`; setup-completion BackgroundTask + `main.py` lifespan safety-net; local bundle ⇒ no upstream `origin` (durable ownership deferred to fork-to-own trinity-enterprise#109) | [cornelius-default-agent.md](feature-flows/cornelius-default-agent.md) |
| 2026-07-06 | #1450 | fix(canary): B-01 queue-status coherence — Side B (queued id-count) reads via the SAME `get_engine()`/`DATABASE_URL` seam as the accessor (backend-consistent on Postgres, not raw-sqlite vs engine; #300/#1093 gap), dedicated `queued_ids_via_engine` field leaves `queued_exec_ids` for B-02/E-02; collector does one confirm-re-read so a transient enqueue/drain race self-heals while a persistent drift still fires; engine-read failure → B-01 skip. Production residue of #1446 | [architecture.md](architecture.md) |
| 2026-07-06 | #1489 | fix(frontend): plumb `VITE_BUG_REPORTING_ENABLED` / `VITE_BUG_INTAKE_URL` as prod build args (`ARG`+`ENV` in `Dockerfile.prod` before `npm run build`, `${VAR:-default}` compose `build.args`, `.env.example` docs) so the #1116 disable/repoint knobs actually reach the shipped image — Vite inlines `import.meta.env` at build time. compose==ARG==code default; disable works end-to-end, repoint still CSP-blocked (deferred #1485). Umbrella #1485 | [in-app-bug-reporting.md](feature-flows/in-app-bug-reporting.md) |
| 2026-07-04 | #903 | fix(slack): thread-scoped session + per-speaker attribution + sender-filtered memory | [slack-channel-routing.md](feature-flows/slack-channel-routing.md) |
| 2026-07-04 | #1445 | fix(webhooks): gate schedule/webhook **creation** on a live owning agent (`is_agent_live` → 404; access-check-first so non-owners see a uniform 403) so a webhook token always resolves to a schedule of a live agent — closes the orphan-schedule class that made valid tokens 404 after #1423's soft-delete-aware token-lookup INNER JOIN | [webhook-triggers.md](feature-flows/webhook-triggers.md) |
| 2026-07-06 | trinity-enterprise#47 | feat(ui): Dashboard **Grid view** — third mode (Grid/Graph/Timeline, not default): magnetic tile canvas on an unbounded pan/zoom lattice; five-zone 384×216 tiles (adaptive chips, Activity·14d + Context·7d charts, Run/Auto toggles); drag with socket preview + swap, tidy/reset, keyboard reorder; skeleton-first render, viewport-gated cached analytics hydration, visibility-aware batch polling. No new backend endpoints | [dashboard-grid-view.md](feature-flows/dashboard-grid-view.md) |
| 2026-07-04 | #1018 | fix(nevermined): settlement-ordering — honest `success_unsettled` on settle-fail (was lying `"success"`); `Idempotency-Key` on the paid boundary keyed on `(payment-signature ∥ message)` with in-flight-409 / settled-replay / unsettled-re-drive-converge; `/retry-settlement` stub → honest 501. Tier 2 durable retry split to a follow-up | [nevermined-payments.md](feature-flows/nevermined-payments.md), [effect-idempotency.md](feature-flows/effect-idempotency.md) |
| 2026-07-04 | #186 | fix(security): close user & agent enumeration oracles — uniform 404 across the agent-access dependency family + router sweep + Tier-4 GETs; identical email-request body/status/timing; MCP `checkAgentAccess` uniform reason + owner-username removal (pentest 3.3.3) | [email-authentication.md](feature-flows/email-authentication.md), [agent-permissions.md](feature-flows/agent-permissions.md) |
| 2026-07-04 | #1444 | fix(chat): fail-loud + owner-gated `/task` chat-session persistence (no silent swallow, IDOR fix, SUCCESS-guarded) + fast unit regression guard | [authenticated-chat-tab.md](feature-flows/authenticated-chat-tab.md) |
| 2026-07-04 | #1446 | fix(canary): stop B-01 queue-status coherence false-firing under full-suite load — canary tests own the `database` dep (temp-DB stub via `get_engine`) + first end-to-end B-01 coverage (test-only) | [architecture.md](architecture.md) |
| 2026-06-30 | trinity-enterprise#58 | feat(ui): Brain Orb — capability-gated per-agent 3D knowledge-graph page. Phase 1 (static render: first-party CSP-clean assets + same-origin iframe host + read-only `data.json` proxy) + Phase 2 (live scope mount/unmount → owner-gated agent re-export via `~/.trinity/brain-orb/` convention hooks → in-place rebuild). Voice/KB-actions/transcript deferred | [brain-orb.md](feature-flows/brain-orb.md) |
| 2026-06-29 | #1376 | fix(session): reconcile against server state on a severed turn so long Session turns never show a false "Failed to send" | [session-tab.md](feature-flows/session-tab.md) |
| 2026-06-29 | #506 | feat(capacity): admin fleet-wide `max_parallel_tasks` ceiling + runtime clamp + owner/admin UI | [capacity-management.md](feature-flows/capacity-management.md) |
| 2026-06-29 | #894 | feat(channels): per-agent **public-channel model override** — owners pick the Claude model for public-facing chats (public link, Slack/Telegram/WhatsApp, x402) via the Sharing tab; additive NULL `agent_ownership.public_channel_model` (NULL→platform default), owner-only `GET`/`PUT /api/agents/{name}/public-channel-model`. Epic #1079. | [public-channel-model.md](feature-flows/public-channel-model.md) |
| 2026-06-29 | #846 | feat(mcp): per-agent opt-in exposure as a dedicated `chat_with_<slug>` tool (dynamic register, poll-reconciled) | [mcp-agent-exposure.md](feature-flows/mcp-agent-exposure.md) |
| 2026-06-28 | #1155 | feat(loops): per-loop cost budget (`max_cost_usd`) iteration-boundary hard stop | [run-agent-loop.md](feature-flows/run-agent-loop.md) |
| 2026-06-28 | #1157 | feat(reliability): no-progress / doom-loop detection for sequential loops | [run-agent-loop.md](feature-flows/run-agent-loop.md) |
| 2026-06-28 | #1085 | feat(reliability): correlated-failure / thundering-herd controls for re-delivery | [redelivery-governor.md](feature-flows/redelivery-governor.md) |
| 2026-06-27 | #918 | feat: agent-reported structured reports via MCP + dashboard | [agent-reports.md](feature-flows/agent-reports.md) |
| 2026-06-27 | #905 | feat(mcp): deterministic git tools over MCP + request-id audit correlation | [mcp-git-tools.md](feature-flows/mcp-git-tools.md), [audit-trail.md](feature-flows/audit-trail.md) |
| 2026-06-27 | #792 | feat(reliability): retry the triggering execution after a SUB-003 auto-switch | [task-execution-service.md](feature-flows/task-execution-service.md) |
| 2026-06-26 | #1332 | fix(reliability): record a user-cancelled execution's activity as CANCELLED | [execution-termination.md](feature-flows/execution-termination.md), [dashboard-timeline-view.md](feature-flows/dashboard-timeline-view.md) |
| 2026-06-26 | #1131 | fix(deploy): probe in-container docker.sock GID for non-root backend (macOS/Colima/rootless) | [docker-socket-gid-detection.md](feature-flows/docker-socket-gid-detection.md) |
| 2026-06-23 | #654 | refactor(models): centralize router Pydantic models in `models.py` (Invariant #14) | [architecture.md](architecture.md) |
| 2026-06-22 | trinity-enterprise#38, #82 | feat(setup): first-run operator intake + admin email login | [first-time-setup.md](feature-flows/first-time-setup.md) |
| 2026-06-22 | #1084 | feat(reliability): effect-scoped idempotency for outbound side effects | [effect-idempotency.md](feature-flows/effect-idempotency.md) |
| 2026-06-21 | #1159 | fix(security): authenticate the in-container agent server on the shared network | [agent-server-authentication.md](feature-flows/agent-server-authentication.md) |
| 2026-06-21 | #946 | feat(orchestration): pull-pilot routing for agent→agent MCP chat | [agent-to-agent-collaboration.md](feature-flows/agent-to-agent-collaboration.md), [mcp-orchestration.md](feature-flows/mcp-orchestration.md) |
| 2026-06-21 | #1088 | refactor(reliability): unify the SUB-003 auth-class failure classifier | [subscription-auto-switch.md](feature-flows/subscription-auto-switch.md) |
| 2026-06-20 | #1169 | feat(infra): agent runtime data — declared `data_paths` + export/import | [agent-data-volumes.md](feature-flows/agent-data-volumes.md) |
| 2026-06-20 | #668 | feat: agent deployment compatibility validation | [agent-compatibility-validation.md](feature-flows/agent-compatibility-validation.md) |
| 2026-06-19 | #1231 | fix(agent): agent `/tmp` tmpfs size configurable via `AGENT_TMP_SIZE` | [container-capabilities.md](feature-flows/container-capabilities.md) |
| 2026-06-19 | #1115 | feat(ui): per-schedule performance scorecards on Agent Detail | [agent-overview-dashboard.md](feature-flows/agent-overview-dashboard.md), [scheduling.md](feature-flows/scheduling.md) |
| 2026-06-19 | #1083 | feat(exec): fire-and-forget dispatch (202 ACK + result callback) | [task-execution-service.md](feature-flows/task-execution-service.md), [capacity-management.md](feature-flows/capacity-management.md), [cleanup-service.md](feature-flows/cleanup-service.md) |

---

## Documented Flows

### Core Agent Features

| Flow | Document | Description |
|------|----------|-------------|
| Agent Lifecycle | [agent-lifecycle.md](feature-flows/agent-lifecycle.md) | Create, start, stop, delete Docker containers |
| Default Cornelius Agent | [cornelius-default-agent.md](feature-flows/cornelius-default-agent.md) | Auto-seed a default Cornelius second-brain agent + enable the Brain Orb on fresh install; first-run-only, fresh-install-scoped `ensure_seeded()` (trinity-enterprise#107) |
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
| Effect Idempotency | [effect-idempotency.md](feature-flows/effect-idempotency.md) | Per-sink exactly-once-style guard for outbound side effects (messages/voip/share_file/Nevermined settle) — `effect_guard` keyed on resolved identity, scoped by execution_id (#1084, Direction A — the pull-mode side-effect approach was reframed to retry-with-trace (#1401) + tool-side gates + async operator human-gate (#1402) in `TARGET_ARCHITECTURE.md` v2; gates per-effect, not *the* per-agent gate) |
| Redelivery Governor | [redelivery-governor.md](feature-flows/redelivery-governor.md) | Correlated-failure / thundering-herd controls for the #1083 re-delivery callback path — agent-side jitter (unflagged) + backend rate caps + distinct-agent shared-cause pause; fail-open, Redis-only, default-OFF behind `REDELIVERY_GOVERNOR_ENABLED` (#1085) |
| Business Validation | [business-validation.md](feature-flows/business-validation.md) | Post-execution auditor verifies task completion (VALIDATE-001) |
| Fan-Out | [fan-out.md](feature-flows/fan-out.md) | Parallel task dispatch and result collection via semaphore (FANOUT-001) |
| Sequential Agent Loops | [run-agent-loop.md](feature-flows/run-agent-loop.md) | `run_agent_loop` server-side sequential bounded task execution with stop-signal + graceful stop (#740) |
| Dispatch Circuit Breaker | [dispatch-circuit-breaker.md](feature-flows/dispatch-circuit-breaker.md) | Per-agent producer-side dispatch breaker (RELIABILITY-007, #526) |
| Execution Context Injection | [execution-context-injection.md](feature-flows/execution-context-injection.md) | Inject prior-execution context into a turn (#171) |
| Schedule Pre-Check | [scheduler-pre-check.md](feature-flows/scheduler-pre-check.md) | Conditional template-supplied pre-check hook before a cron run (SCHED-COND-001, #454) |

### Dashboard & Monitoring

| Flow | Document | Description |
|------|----------|-------------|
| Agent Network (Dashboard) | [agent-network.md](feature-flows/agent-network.md) | Real-time visual graph at `/` |
| Dashboard Timeline View | [dashboard-timeline-view.md](feature-flows/dashboard-timeline-view.md) | Graph/Timeline mode toggle with execution boxes |
| Dashboard Grid View | [dashboard-grid-view.md](feature-flows/dashboard-grid-view.md) | Magnetic tile canvas — third dashboard mode (trinity-enterprise#47) |
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
| Executions Dashboard | [executions-dashboard.md](feature-flows/executions-dashboard.md) | Unified fleet executions list + stats (EXEC-022) |

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
| 4-Tier Role Model | [role-model.md](feature-flows/role-model.md) | user < operator < creator < admin role hierarchy (ROLE-001) |

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
| WhatsApp Integration | [whatsapp-integration.md](feature-flows/whatsapp-integration.md) | Per-agent WhatsApp via Twilio (WHATSAPP-001) |

### Mobile & PWA

| Flow | Document | Description |
|------|----------|-------------|
| Mobile Admin PWA | [mobile-admin-pwa.md](feature-flows/mobile-admin-pwa.md) | Standalone mobile admin at `/m` with agent chat, autonomy toggle, Ops/System tabs (MOB-001) |

### Platform Services

| Flow | Document | Description |
|------|----------|-------------|
| Image Generation | [image-generation.md](feature-flows/image-generation.md) | Gemini-powered two-step image generation pipeline (IMG-001) |
| In-App Bug Reporting | [in-app-bug-reporting.md](feature-flows/in-app-bug-reporting.md) | Bug / feature / feedback reporting from the Help widget (#1116) |
| Docs Q&A | [trinity-docs-qa.md](feature-flows/trinity-docs-qa.md) | Documentation Q&A bot (Vertex AI Search) |

### MCP & Integration

| Flow | Document | Description |
|------|----------|-------------|
| MCP Orchestration | [mcp-orchestration.md](feature-flows/mcp-orchestration.md) | 62 MCP tools for agent orchestration |
| MCP Git Tools | [mcp-git-tools.md](feature-flows/mcp-git-tools.md) | Deterministic git tools over MCP + request-id audit correlation (#905) |
| MCP Agent Exposure | [mcp-agent-exposure.md](feature-flows/mcp-agent-exposure.md) | Per-agent opt-in dedicated `chat_with_<slug>` MCP tool, dynamically poll-reconciled (#846) |
| Trinity CLI | [cli-tool.md](feature-flows/cli-tool.md) | Python Click CLI with multi-instance profiles, mirroring core MCP tools as shell commands |
| Trinity Connect | [trinity-connect.md](feature-flows/trinity-connect.md) | Local-remote agent sync via WebSocket |
| Write User Memory | [write-user-memory.md](feature-flows/write-user-memory.md) | Per-user memory write MCP tool (MEM-001, #888) |

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
| Proactive Messaging | [proactive-messaging.md](feature-flows/proactive-messaging.md) | Proactive agent-to-user messaging (#321) |

### Configuration & Settings

| Flow | Document | Description |
|------|----------|-------------|
| Public-Channel Model | [public-channel-model.md](feature-flows/public-channel-model.md) | Per-agent model override for public-facing channels (#894) |
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
| Docker Socket GID Detection | [docker-socket-gid-detection.md](feature-flows/docker-socket-gid-detection.md) | `start.sh` probes the in-container `docker.sock` GID so the non-root backend reaches Docker on Docker Desktop/Colima/rootless + throttled WARN on denial + hermetic CI guard — closes the #874 "No agents" regression (#1131) |
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
| Self-Execute | [self-execute.md](feature-flows/self-execute.md) | Agent background task during chat (SELF-EXEC-001) |

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
