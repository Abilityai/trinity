# CSO diff audit — abilityai/trinity-enterprise#475 (`feature/475-rail-rehome-tabs`)

**Date**: 2026-09-06 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `83c0f51e` on `dev` · **Diff**: 23 files changed, 1915 insertions(+), 362 deletions(-) (staged, pre-commit)

## Verdict
**No findings at the daily gate.** Three INFO observations recorded below. Frontend-only change: no backend route, model, migration, MCP tool, Docker, compose, CI or dependency change.

## Attack surface introduced by the diff
- **No new server surface.** The rail's Loops / Canvas / Files tabs read the routes the loops strip, the Workspace agent page and the Files drawer already called: `GET /api/agents/{name}/loops` (platform JWT, `AuthorizedAgent`), and the client-portal `…/canvas[/{id}]`, `…/documents`, `…/uploads`, `POST …/documents` (portal principal, roster-gated; canvas narrowed to `audience='roster'` in the query; uploads inbox-scoped).
- **Fetch follows the door, not only render** (`portalRail.feedsFor` over `visibleTabs`): an external client (Canvas · Files) never causes a loops request; nothing is fetched while the rail is off screen (agent page, loading/failed stage, unreachable deep link — the fetch-before-verdict gap the plan review named is closed by gating the owner on `railVisible`).
- **New `/ws` consumer** (`stores/portalRailFeeds.handleWebSocketEvent`): reacts to `loop_*` and terminal `agent_activity` events **by `agent_name` membership only**, refetches through the access-controlled REST reads (the #918 thin-trigger rule), debounced 2s, ignores an event with no agent. No payload content is rendered or stored.
- **New localStorage key** `trinity-workspace-rail-seen`: agent names the viewer already sees on the roster + server ISO timestamps. No content, no credential. Normalised on read (unknown tabs/blank agents/unparseable stamps dropped); per browser, like the existing rail key.
- **Composer prefill** ("Ask for a canvas"): fixed text into the composer, focused, never auto-sent — user content in the user-message position.
- Download links keep `target="_blank" rel="noopener"` and the existing signed `?sig=` URLs; upload forwards a `File` to the existing route, whose size/type/rate checks return the named `detail` now shown inline.

## Verification performed
- Secrets: known-prefix scan (AWS, OpenAI/Anthropic, GitHub token and PAT, Slack) over the staged diff — 0 matches. No `.env`, workflow or config file touched.
- Enterprise-docs guard: the workflow's pattern run over the changed public docs — 0 hits.
- Rendering: no `v-html` / `innerHTML` / `eval` in any changed frontend file; canvas blocks still render through `CanvasPanel` → `CanvasBlock` (DOMPurify for `html`, unchanged).
- Auth boundaries: `tests/unit/portalRail.spec.js` (client door = Canvas · Files; `feedsFor`), `tests/unit/portalRailFeeds.spec.js` (a client session never feeds the loops store; hidden rail fetches nothing; partial failure keeps rows; stale response dropped) — green. Full frontend unit suite 87 files / 1931 tests green.
- Supply chain / CI / Docker: no changes.

## Observations (below the gate)
| # | Sev | Conf | Observation |
|---|-----|------|-------------|
| O1 | INFO | 3/10 | Seen markers are per browser, not per principal: two Workspace accounts on one machine share "seen". Names + timestamps only; same precedent as the rail state key. Documented in the flow. |
| O2 | INFO | 2/10 | Below `sm` the CSS-hidden column and the open sheet both mount the active tab body. Bodies never fetch (the shell owns the stores), so the cost is a second form instance, never a second request. |
| O3 | INFO | 3/10 | The `agent_activity` consumer sees the fleet-wide stream a platform session already receives (scoped by ent#467); it acts only on names in the current chat and only by refetching. No new disclosure. |

## STRIDE (diff scope)
- **Frontend**: spoofing — none (existing session credentials, no new token); tampering — localStorage markers can be hand-edited: worst case a dot lights or stays dark; repudiation — n/a (no new mutation beyond the existing upload route); disclosure — O1/O3; DoS — excluded (push refresh debounced; no idle timer); elevation — none (door gate extended to fetch).

## Data classification (diff)
- `trinity-workspace-rail-seen` — INTERNAL (agent names visible on the roster, server timestamps).
- Feed store rows (canvas metadata, document metadata, inbox filenames) — CONFIDENTIAL in memory only, exactly the rows the existing drawer/agent page held; never persisted client-side.

_Trend_: second diff audit on this branch line (prior: ent#437, 2026-09-03); not comparable (different surface). No persistent findings.
