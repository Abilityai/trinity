# CSO diff audit — abilityai/trinity-enterprise#534 (`feature/534-workspace-voice-mode`)

**Date**: 2026-09-07 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `9a1dfa77` on `dev` · **Diff**: 36 tracked files +1628 / −1740 plus 7 new files (working tree, pre-commit). OSS code: backend (`client_portal`, `routers/voice.py`, `services/gemini_voice.py`, schema/migrations/config), frontend (Workspace conversation, shell, composable, two new components), docs, compose env passthrough.

## Verdict
**No findings at the gate.** The diff adds one authenticated route under the Workspace's portal principal, two nullable columns, one env knob, and moves a voice-call surface into the Workspace. Two pre-existing oracles were closed on the way; one pre-existing exposure (JWT in the voice WebSocket query string) is unchanged and already registered as debt. Three observations recorded below the gate.

## Attack surface introduced by the diff
- **New route** `POST /api/enterprise/client-portal/agents/{agent_name}/voice/start` (`client_portal/router.py::portal_voice_start`). Gates in order: `Depends(get_portal_principal)` (portal-token or platform JWT; agent-scoped MCP keys rejected by the dependency, blocked clients refused) → `is_platform` required, else uniform 404 → `_require_roster(include_owned=True)` (uniform 404) → `rate_limiter.enforce("portal_voice_start:{email}:{agent}", 10, 60)` → `get_current_user(request, token)` for the user id the WebSocket ownership gate needs → `start_workspace_voice`, which re-evaluates roster **and** thread ownership (`get_portal_session(id, agent, email)`) before branching and raises one detail string for both. No new WebSocket channel, tool, Docker permission or network edge.
- **Schema**: `enterprise_portal_messages.source`, `.voice_call_id` — nullable, platform-written only (no request model carries them), both tracks (`db/migrations.py` + Alembic `0056`, one head).
- **Config**: `WORKSPACE_VOICE_MAX_DURATION` (int, default 1800) through both compose files and `.env.example`. Not a credential.
- **Provider config** (`gemini_voice._build_live_config`): adds `context_window_compression` and `session_resumption`; the tool manifest is unchanged (`run_task` + the six canvas verbs in workspace mode). The resumption handle is server-issued and kept in-process, never sent to the browser.
- **Persistence**: spoken turns are written by `client_portal/voice.py::persist_voice_turn` through `runtime_secret_scrub.scrub_text` (the ent#279 chokepoint rule), bound parameters only.

## Findings
| # | Sev | Conf | Status | Category | Finding | Phase | File:Line |
|---|-----|------|--------|----------|---------|-------|-----------|
| — | — | — | — | — | none at the gate | — | — |

## Verification performed
- **Secrets (P2)**: known-prefix scan (AWS, OpenAI/Anthropic `sk-`, GitHub `ghp_`, Google `AIza`, `trinity_mcp_`), internal domains/emails and private-IP patterns over every added line of the working diff — 0 matches. No `.env` tracked; compose adds an int knob only. No stray media in the tree (Playwright screenshots removed).
- **Enterprise-disclosure guard**: the workflow's PCRE pattern run locally over every changed doc and seam file — 0 hits (`enterprise_portal_*` is allowlisted by the guard itself; no paid-module token appears).
- **Auth (P9 A01)**: `tests/unit/test_186_enumeration_uniformity.py`, `test_2094_dependency_path_param_pairing.py`, `test_1310_auth_wiring.py`, `test_293_admin_gate_rejects_agent_keys.py`, `test_agent_auth_header_guard.py`, `test_ent435_settings_sink_guard.py` → 79 passed. The new route's uniform-404 behaviour is pinned by `test_ent534_workspace_voice.py::test_off_roster_foreign_thread_and_portal_token_are_one_uniform_404` (three causes, one detail string) and the live smoke (bogus thread → `404 Conversation not found`).
- **WebSocket (P6)**: `/ws/voice/{id}` now decodes the JWT **before** the session lookup, closing the 4004-vs-4001 existence oracle for unauthenticated callers; the #600 ownership gate (user id or admin) is unchanged. `/panel` keeps its 403 for a foreign session (pinned by `test_1310`).
- **Injection (P9 A03)**: no new raw SQL string interpolation (`add_portal_message` binds `:source`/`:call`); no `v-html`, `innerHTML`, `subprocess`, `eval` or `verify=False` in added lines. Spoken turns render through `PortalAgentBubble` (existing DOMPurify path) and a plain text bubble.
- **LLM (P7)**: the thread's recent rows enter the voice system prompt as "Conversation so far" — the same shape the Agent Detail path has carried since VOICE-001, now scoped to the Workspace thread. The author of those rows is the calling internal user (or the agent) and the prompt drives that user's own call against their own agent; no cross-tenant path (portal-token clients cannot reach the route). The cap-warning text is platform-authored. Tool manifest not widened.
- **Cross-worker (P10 Tampering/Repudiation)**: Redis metadata now carries every field a reconstructed session decides on (`canvas_audience`, `portal_session_id`, `client_email`); the Agent Detail save-at-end takes a `SETNX voice_session:{id}:saved` claim (fail-open on Redis error, documented); the Workspace path writes only from the worker holding the live socket, so an off-worker `/stop` cannot write. Pinned by `test_a_reconstructed_cross_worker_session_never_writes` and `test_the_transcript_save_claim_is_a_setnx_and_fails_open`.
- **Data classification (P11)**: spoken transcripts are CONFIDENTIAL (same class as typed Workspace turns), stored in the same table with the same roster-scoped readers; the canvas the call draws stays at `operator` audience (never widened), and the platform-principal read widening discloses nothing a platform user could not already read on Agent Detail (`routers/canvas.py` GET is `AuthorizedAgent`, unfiltered by audience).
- **Live**: the route, the socket, a spoken round-trip and the `saved` handshake exercised against the local stack with a real provider key; the frontend modal verified in Playwright (controls inert, Escape ends, canvas column fetches by real id after the found-live fix).
- **Independent verification**: no finding survived the gate, so no verifier subagent was needed.

## Observations (below the gate)
| # | Sev | Conf | Observation |
|---|-----|------|-------------|
| O1 | MED | 7/10 | The platform JWT rides `?token=` on the voice WebSocket (pre-existing, unchanged; a second front door now uses it). Proxy/access logs can capture a 7-day bearer. Registered as `debt:2026-09-07-voice-ws-jwt-in-query-string`; the fix is a one-shot ticket minted by the start route (the platform `/ws` pattern). Not introduced here. |
| O2 | INFO | 6/10 | `GET /api/agents/{name}/voice/{sid}/panel` answers `empty_canvas` for an unknown session id before any ownership check — an authenticated liveness probe on 128-bit ids (pre-existing; kept because `test_1310` pins the 403 for a *foreign* session and a uniform-empty would retire that guard). |
| O3 | INFO | 5/10 | `_persist_user_turn`'s resend dedup now ignores spoken rows by design; a client that types the same words it just said produces a second row. Intended (a new message, not a retry); noted so it is not later read as a dedup bug. |

## Trend
Prior report `cso-diff-2026-09-07-ent541-retention-parity-guard`: different surface; nothing resolved, persistent, or new on this diff. O1 persists from the VOICE-001 era and is now tracked in the debt inbox.
