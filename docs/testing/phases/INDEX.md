# Trinity UI Test Phases — Index

The catalog of click-through scenarios in this folder. [`README.md`](README.md) explains how
they are run and the rules every phase follows.

**State of the set (2026-10-09):** every active phase was rewritten or written against the
current source for the v1 UI. None has been run in a browser since — a phase moves from 🟡 / 🆕
to 🟢 on its first passing run. Expect the first run of each to surface a few steps that need
tightening; fix the phase, not the report.

| Mark | Meaning |
|---|---|
| 🟢 | Run in a browser and passed since its last rewrite |
| 🟡 | Rewritten against source; not yet run in a browser |
| 🆕 | New phase; not yet run in a browser |
| ⛔ | Retired — the file is a stub saying why and what covers it now |

## Active phases

Phases are independent: any one can be run on its own, in any order, against an instance where
the three fixture agents are running. Phase 0 is the preflight that checks exactly that.
"Manual-only part" means the file also describes steps that are never run unattended (they
need a second user, a mailbox, an external repository, a fresh database, or are fleet-wide).

| # | Phase | Time | A pass proves | Manual-only part | Status |
|---|---|---|---|---|---|
| 0 | [Preflight (non-destructive)](PHASE_00_SETUP.md) | 5 min | Backend healthy, admin token mintable, setup completed, fixture trio running, fixture templates resolvable, login page renders cleanly | — | 🟡 |
| 1 | [Authentication](PHASE_01_AUTHENTICATION.md) | 10 min | An unauthenticated visitor is held at `/login`, `admin` can sign in and reach every top-nav destination, a lost token ends the session, and "Sign out" revokes it server-side | yes | 🟡 |
| 2 | [Agent Creation](PHASE_02_AGENT_CREATION.md) | 12 min | An agent can be created and deleted entirely through the UI, that bad or duplicate names are refused with a named error, and that nothing is left behind | yes | 🟡 |
| 3 | [Tasks & Chat on One Agent](PHASE_03_CONTEXT_VALIDATION.md) | 10 min | The Tasks composer and the Chat tab both reach the agent, each produces an execution row, the execution detail page renders, and context usage is reported where the UI shows it | yes | 🟡 |
| 4 | [State Persistence (test-counter)](PHASE_04_STATE_PERSISTENCE.md) | 12 min | Counter value written by one task is read by the next, is visible in the Files tab, and is still there after a reload and a stop/start | yes | 🟡 |
| 5 | [Agent Collaboration & Permissions (test-delegator)](PHASE_05_AGENT_COLLABORATION.md) | 15 min | A permitted delegation from `test-delegator` to `test-echo` runs and is recorded as an `agent`-triggered execution; a denied one is refused; the original permission set is restored | yes | 🟡 |
| 9 | [File Browser (Files tab)](PHASE_09_FILE_BROWSER.md) | 12 min | The Files tab lists and previews a running agent's workspace honestly in every state, and leaves the workspace exactly as it found it | yes | 🟡 |
| 11 | [Dashboard: Timeline, Grid, List](PHASE_11_MULTI_AGENT_DASHBOARD.md) | 15 min | Timeline, Grid and List each render the fleet (trio + system agent) with working, non-destructive controls, and the user's saved view preference is respected | yes | 🟡 |
| 12 | [Agent Lifecycle — stop, start, delete (throwaway only)](PHASE_12_CLEANUP.md) | 10 min | Header run-state switch and delete confirmation work end to end; a deleted agent leaves the list and the API; the trio and `trinity-system` are still running | — | 🟡 |
| 13 | [Settings — General & Access](PHASE_13_SETTINGS.md) | 12 min | Settings tabs deep-link and switch, the Trinity Prompt saves and persists, and the whitelist accepts and removes an address — with both restored to their original state | yes | 🟡 |
| 15 | [System Agent (observe-only)](PHASE_15_SYSTEM_AGENT.md) | 10 min | The system agent is reachable, clearly marked, offers no delete or owner-management surfaces, and the read-only ops API answers for an admin | yes | 🟡 |
| 17 | [Email Authentication](PHASE_17_EMAIL_AUTHENTICATION.md) | 8 min | The email login form renders and validates, the request endpoint does not reveal whether an address is registered, and a wrong code is refused with the named error | yes | 🟡 |
| 18 | [GitHub Initialization (observe-only)](PHASE_18_GITHUB_INITIALIZATION.md) | 8 min | The Git tab renders the correct state for a fixture agent, the initialise dialog opens with the right fields and cancels cleanly, and the git status / config / PAT-status endpoints return their documented shapes | yes | 🟡 |
| 19 | [First-Time Setup](PHASE_19_FIRST_TIME_SETUP.md) | 8 min (Part A only) | `/setup` is unreachable once setup is completed; the status endpoint reports completed; the provisioning endpoint refuses a second setup without side effects | yes | 🟡 |
| 20 | [Live Execution Streaming](PHASE_20_LIVE_EXECUTION_STREAMING.md) | 12 min | A task can be run inline, its execution opens in Execution Details, a finished transcript renders and survives a refresh, and an unknown execution id fails honestly | yes | 🟡 |
| 21 | [Chat Session Management](PHASE_21_SESSION_MANAGEMENT.md) | 12 min | A new chat session is created by the first message after `New Chat`, earlier sessions stay selectable with their history intact, and the API agrees with what the selector shows | yes | 🟡 |
| 22 | [Telemetry & Logs](PHASE_22_LOGS_TELEMETRY.md) | 15 min | Live CPU/memory figures, the Overview panel and the stats/logs endpoints all answer for a running fixture agent | yes | 🟡 |
| 23 | [Agent Configuration](PHASE_23_AGENT_CONFIGURATION.md) | 20 min | Each control reads its real value, one autonomy flip and one resource change round-trip through the API, and everything is back to its starting value | yes | 🟡 |
| 24 | [Credential Management](PHASE_24_CREDENTIAL_MANAGEMENT.md) | 15 min | A key injected through the UI lands in the agent's `.env`, is counted by the status API without its value being returned, and is removed again | yes | 🟡 |
| 25 | [Agent Access & Sharing](PHASE_25_AGENT_SHARING.md) | 15 min | An operator row can be added, is rejected as a duplicate, and is removed; the whitelist and access policy end exactly as they started | yes | 🟡 |
| 26 | [Shared Folders](PHASE_26_SHARED_FOLDERS.md) | 12 min | Expose on one agent and mount on another are saved, surfaced as pending in both panels and in the API, and restored to their original values | yes | 🟡 |
| 27 | [Public Chat Links](PHASE_27_PUBLIC_ACCESS.md) | 20 min | A new link opens a working public chat for a logged-out visitor, stops working when disabled, and is deleted; the seeded link is untouched | yes | 🟡 |
| 28 | [Agent Dashboard](PHASE_28_AGENT_DASHBOARD.md) | 15 min | Tab gating matches the API flags; a written `dashboard.yaml` renders its widgets; removing the file falls back to the cached dashboard with a visible banner | yes | 🟡 |
| 29 | [Workspace — chats, tabs, drafts](PHASE_29_WORKSPACE_CHAT.md) | 20 min | A chat with `test-echo` can be started, switched, starred, archived and reopened by URL without losing typed text or position | — | 🆕 |
| 30 | [Workspace — Inbox, asks, rooms, projects](PHASE_30_WORKSPACE_INBOX_ROOMS.md) | 15 min | The Inbox, a room and Projects each render an honest populated or empty state, and deep links to them survive a reload | — | 🆕 |
| 31 | [Operations](PHASE_31_OPERATIONS.md) | 15 min | Each Operations tab loads an honest populated or empty state, `?tab=` survives reload, old URLs land on the right tab, and the seeded executions are listed and open | — | 🆕 |
| 32 | [Library — templates, systems, skills](PHASE_32_LIBRARY.md) | 15 min | The Library renders every asset kind with honest empty states, its tabs are addressable, and nothing was created, installed, assigned or synced | yes | 🆕 |
| 33 | [Agent detail — Overview, Reports, Loops, Playbooks, Schedules, Skills, Payments, Info](PHASE_33_AGENT_DETAIL_V1_TABS.md) | 25 min | Every tab on `test-echo` loads its real content or its honest empty state, tab deep links work, and the one schedule created here is gone again | yes | 🆕 |
| 34 | [Canvas and shared canvas links](PHASE_34_CANVAS.md) | 20 min | A canvas renders its blocks, the share link opens read-only without an account, a revoked or unknown link says so honestly, and `test-echo` ends with the canvases it started with | yes | 🆕 |
| 35 | [Settings: MCP Keys, Agents, Retention, Integrations](PHASE_35_SETTINGS_KEYS_RETENTION.md) | 20 min | Tab deep links, the key create → one-time reveal → revoke → delete lifecycle, and the read-only state of the other three tabs are proven against the API | yes | 🆕 |
| 36 | [Onboarding checklist and app chrome](PHASE_36_ONBOARDING_AND_CHROME.md) | 20 min | The chrome every page shares behaves at wide and narrow widths, theme choice survives a reload, and onboarding state is read, exercised and put back | yes | 🆕 |
| 37 | [Mobile admin (/m)](PHASE_37_MOBILE_ADMIN.md) | 15 min | `/m` signs in inline, shows the fleet, the operator queue and fleet health consistently with the API, never overflows sideways on a small phone, and signs out in place | yes | 🆕 |

32 active phases, about 448 minutes (7.5 hours) if run back to back. There is no phase 6.

## Retired phases

Numbers are never reused. Each file is a short stub: why it was retired and what covers it now.

| # | Phase | Status |
|---|---|---|
| 7 | [Scheduling & Autonomy](PHASE_07_SCHEDULING.md) | ⛔ |
| 8 | [Execution Queue](PHASE_08_EXECUTION_QUEUE.md) | ⛔ |
| 10 | [Error Handling](PHASE_10_ERROR_HANDLING.md) | ⛔ |
| 14 | [OpenTelemetry](PHASE_14_OPENTELEMETRY.md) | ⛔ |
| 16 | [Web Terminal](PHASE_16_WEB_TERMINAL.md) | ⛔ |

## Where each part of the UI is covered

| Surface | Phases |
|---|---|
| Login, sign-out, setup, email codes | 1, 17, 19 |
| Dashboard (timeline, grid, list), nav and app chrome, onboarding checklist | 11, 36 |
| Agent creation and lifecycle | 2, 12 |
| Agent detail — tasks, chat, sessions, execution detail | 3, 20, 21 |
| Agent detail — files, state, shared folders | 4, 9, 26 |
| Agent detail — permissions, collaboration | 5 |
| Agent detail — configuration, credentials, access and sharing, git | 23, 24, 25, 18 |
| Agent detail — overview, reports, schedules, loops, playbooks, skills, payments, info | 33 |
| Agent detail — dashboard tab, canvas, telemetry | 28, 34, 22 |
| System agent | 15 |
| Public chat links, shared canvas links | 27, 34 |
| Workspace — chats, inbox, rooms, projects | 29, 30 |
| Operations | 31 |
| Library | 32 |
| Settings | 13, 35 |
| Mobile admin | 37 |

## Recording a run

A run reports each step as PASS, FAIL, or SKIPPED with a reason (`not present`, `manual-only`,
`fixture`, `gate`, `run finished`), then the phase's Success Criteria, then every state change
it made and restored. A FAIL carries a screenshot. Reports from the automated sweep are written
to [`docs/testing/ui-sweep/`](../ui-sweep/).

## Version history

| Date | Change |
|---|---|
| 2026-10-09 | **v1 rewrite.** All 23 surviving January phases rewritten against current source (independent, fixture-only, self-restoring, no hardcoded credentials); phases 7, 8, 10, 14 and 16 retired; nine new phases (29–37) for Workspace, Operations, Library, the newer agent tabs, Canvas, Settings keys and retention, onboarding and chrome, and mobile admin. References to a `run_test_phases.py` runner removed — it never existed. |
| 2026-01-14 | Phases 19–28 added (gap analysis). |
| 2025-12-26 | Phases 16–18 added. |
| 2025-12-21 | Phases 14–15 added. |
| 2025-12-14 | Phase 13 added. |
| 2025-12-09 | Modular phase structure created (phases 0–5), split from a monolithic test document. |
