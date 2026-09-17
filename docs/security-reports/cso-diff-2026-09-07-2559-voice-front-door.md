# CSO diff audit — trinity#2559 (`AndriiPasternak31/issue-2559`)

**Date**: 2026-09-07 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `c04750e73` on `dev` · **Diff**: 37 files, +1069 / −301 — Vue/JS frontend, docs, tests. **Zero backend source changes** (`git diff --name-only | grep '^src/backend/'` → only `tests/unit/test_ent438_agent_canvas.py`).

## Verdict
**No findings at the gate.** The diff adds no endpoint, tool, schema, credential, container, network, dependency or workflow. It *removes* one authenticated caller of `POST /api/agents/{name}/voice/start` and one transcript destination. The single new security-relevant surface — a URL query parameter that starts a billed, microphone-opening realtime session — is guarded by an in-app armed one-shot that was reviewed end to end and holds.

## Attack surface introduced by the diff
- **`?voice=1` on `/workspace` — a URL that would open a microphone and mint a billed Gemini Live session.** This is the one surface worth the audit. It is the LLM-cost-amplification exception to the DoS exclusion, and the asset is worse than cost: an auto-started call captures ambient audio and writes a durable transcript into the victim's own Workspace thread.
- **`PortalConversation` widens `defineExpose`** from `{ focusComposer }` to `{ focusComposer, startVoiceCall }`. Reachable only by the parent component (`Portal.vue`, first-party) through a template ref — not a cross-origin or user-reachable surface.
- **Removed** surface: `ChatPanel.vue` no longer calls `GET /api/agents/{name}/voice/status` or `POST …/voice/start`, and `useVoiceSession.start()` is gone. `chat_messages.source='voice'` gains no new writer, so one transcript home fewer.
- No new outbound request, no new sink (`v-html` / `innerHTML` / `eval` / `window.open` / `target="_blank"` — grep over every added line in `src/frontend/src/`: 0 hits, two comment mentions of `sessionStorage` only).

## Findings
| # | Sev | Conf | Status | Category | Finding | Phase | File:Line |
|---|-----|------|--------|----------|---------|-------|-----------|
| — | — | — | — | — | none at the gate | — | — |

## Verification performed
- **Secrets (P2)**: known-prefix scan (`AKIA`, `sk-`, `ghp_`/`gho_`/`github_pat_`, `xox*`, PEM private keys) plus `password|secret|token|api_key` assignments over every added line of `c04750e73..HEAD` — 0 matches.
- **Supply chain / CI / infra (P3–P5)**: no `package.json`, lockfile, `requirements*.txt`, `.github/`, `docker/` or `docker-compose*` file in the diff. Out of scope by construction.
- **The armed one-shot (P7/P9-A04) — traced, not assumed.** `voiceAutoStart({query, landed, isPlatform, armed})` fails **closed** on every parameter (all default `false`; `voiceQueryRequested` is a strict `=== '1'`). The arming flag is a module-scoped `let` in `portalVoiceMode.js` set only by `AgentHeader::goToTalk()`, so:
  - a pasted / mailed / bookmarked / `window.open`ed / iframed link always arrives on a **fresh document**, where `armed` is `false` — including the signed-out variant that defeats a `navigator.userActivation` check (`Portal.vue` runs `bootstrap()` only while signed in, so such a link survives unconsumed until exactly the sign-in click that would satisfy the browser heuristic);
  - the rejected `navigator.userActivation` design is correctly characterised in the code: it is an audio-playback heuristic, sticky per document, and fail-open on `null`.
- **The module-instance assumption behind that flag was verified against a real build**, not reasoned about: `AgentDetail` and `Portal` are separate lazy route chunks, and a duplicated module would give each its own `armed` and silently break the door in production while working in dev. `npm run build` → both chunks carry `import{…}from"./portalVoiceMode-CobLka0j.js"`, and the `unarmed`/`unreachable`/`principal` verdict function is defined **once**, in that shared chunk. Single instance confirmed.
- **Server-side is the real gate, and it is untouched.** `client_portal/voice.py::start_workspace_voice` evaluates roster membership and thread ownership **before** branching → one uniform 404 (Invariant #8), refuses a non-platform principal, and `router.py:1041` enforces `rate_limiter.enforce(f"portal_voice_start:{email}:{agent_name}", 10, 60)`. `PortalConversation::startVoiceCall` additionally re-checks `voiceEntry.render/enabled` and the secure-context/mic pre-flight at call time. Three layers; the query param is the weakest and it is not load-bearing.
- **Access control (P9-A01)**: no auth-relevant code changed. The removed `voice_available` prop/binding gated nothing server-side. Enumeration uniformity, agent-key self-boundaries and backend→agent auth are untouched surfaces.
- **Independent verification**: no finding survived the gate, so no verifier subagent was spawned.

## Observations (below the gate)
| # | Sev | Conf | Observation |
|---|-----|------|-------------|
| O1 | INFO | 4/10 | `armed` is set before `router.push` and cleared only when a later `bootstrap()` observes the `voice` key. If that navigation is aborted, the flag stays set for the document's lifetime, and a *subsequent in-app* navigation to a `?voice=1` URL in the same document would then be honoured. No first-party link produces such a URL (only `goToTalk` does), and a plain `<a href>` in rendered content is a full page load — a fresh document. Hardening, not a vulnerability: `router.push(...).catch(disarmVoiceAutoStart)`, or disarm on the next route change. |
| O2 | INFO | 5/10 | `voiceAutoStart`'s `landed` guard is structurally always `true` at its only call site — `resolveAgentQuery()` has already returned when `resolveAgentLanding` finds nothing. Defence in depth costs nothing here and the roster decision upstream is the real check; recorded so a future reader does not mistake it for the enforcing line. |
| O3 | INFO | 6/10 | An **admin** viewing an agent they neither own nor were shared reaches Talk (the door is ungated) and lands on copy that is false: *"That link points at an agent that isn't shared with you… ask whoever sent the link."* `db/agents.py::can_user_access_agent` grants any admin every agent; `client_portal/service.py::_roster_rows` is shared ∪ owned with no admin arm. Not a disclosure — the Workspace correctly refuses — but a dead end with misleading copy. Pre-existing (the ungated Workspace button has the same reach gap today) and already registered as follow-up #3 in `.plan/issue-2559.md`. |

## Trend
Prior report `cso-diff-2026-09-07-ent541-retention-parity-guard`: different surface (a test helper + CI trigger). Nothing resolved, persistent, or new carries across.
