# UI Sweep — explore + edges, all areas — 2026-09-13

**Branch:** `dev` @ `2c5cfe0e3` · **Target:** http://localhost (fresh `docker compose build` + base-image rebuild + `start.sh`, backend `0.9.5-rc2`) · **Edition:** enterprise · **Budget:** unbounded by request (≈ 5 h wall clock, 16 subagent sheets) · **Status:** COMPLETE · **Issues filed:** none — every finding is held for the user's confirmation (first full run of the playbook; the private enterprise submodule checkout was ahead of the pinned commit on its `main` lineage and was deployed as checked out)

## 1. Executive summary

| | |
|---|---|
| Sheets run | 11 explore (every area in the Area Table plus the 404 catch-all and every legacy redirect), 4 edges (agent-detail, workspace, library, settings), 1 consolidated fresh-context repro pass (21 items) |
| Routes covered | all 31 router paths; 26 agent-detail tab ids; 11 Settings tab ids; 6 Operations tab ids; 3 Library tabs; `/m` at 390 and 320; the public chat link logged out |
| Findings | **1 critical, 29 medium, 35 low** (65 total, deduplicated by fingerprint) |
| Second reproduction | 20 of 21 Medium candidates reproduced in a fresh context; 1 not reproduced and downgraded |
| Source validation | every Critical/Medium was traced to a file and line or to an API response where the claim allowed it; two subagent claims were overturned by source (`<b>` in replies is DOMPurify-by-design; `Bad Name!` → `bad-name` is deliberate sanitization) |
| Fixtures | test-echo ✅ · test-counter ✅ · test-delegator ✅ (created this run, running throughout) |
| Evidence | `evidence/2026-09-13-explore-all-areas/` — 149 screenshots from every sheet and the repro pass (`sheet-<lane>-<area>-<step>.png`, `repro-<n>.png`); local-only (gitignored), the repo is public |
| Design-system spot check | `gray500meta` = 0 on workspace, library, portal, mobile, the edition-gated area; 16–32 on dashboard list view and several agent-detail tabs; `spinners` (outside a button) = 0 everywhere except the public chat page; control radii cluster at 4px (library), 8px (`/m`, dashboard) instead of the 6px standard |

**The headline:** an anonymous public-chat session is stored with a mixed-case id but looked up lowercased, so **every public link conversation is empty after a reload** even though the messages are in the database — a one-line backend fix (`db/public_chat.py`). Below that, the run found one class defect repeated across the app: the modal shell (`ConfirmDialog`, `CreateAgentModal`, the execution-log and MCP-key dialogs) has no Escape handling, no focus trap and no initial focus, and the delete-agent confirm does not restate its consequence; and one layout class: five surfaces (Operations, MCP keys, the whitelist, the audit table, the public chat thread) grow the page instead of scrolling inside a bounded viewport.

## 2. Critical

### 🔴 C-1 — Anonymous public-chat history never restores after reload   `fp=d4a6e8df`

**Repro** (fresh logged-out context, 1440×900, either theme): create a public chat link on `test-echo` with the agent's access policy set to not require email → open `/chat/<token>` → send two messages (echo replies arrive) → reload. **Expected:** "the conversation is either restored or an honest fresh start"; the page's own `onMounted` tries to load history first. **Observed:** empty thread; `GET /api/public/history/<token>?session_id=<id>` answers `{"messages":[],"message_count":0}` while the same session id is kept in `localStorage` and the DB holds the rows (`public_chat_sessions` shows `('JUSAKF4-kFW7_obLcQmplg', 2)`). **Cause:** `src/backend/db/public_chat.py` line 84 stores anonymous session ids verbatim (minted by `secrets.token_urlsafe(16)`, mixed case) but line 146 in `get_session_by_identifier` compares against `session_identifier.lower()` unconditionally. Any id with an uppercase letter (almost all of them) can never be found again. Reproduced twice in-run with two different ids, then confirmed by API and DB from the orchestrator. Screenshot `evidence/2026-09-13-explore-all-areas/sheet-explore-portal-public-2-R4.png`.

## 3. Medium

| fp | area | finding | validation | reproduction |
|---|---|---|---|---|
| `763ada69` | auth | Login has no return path: expired session and deep links always land on the dashboard | source | reproduced ×3 (auth, agent-detail edges, settings edges, repro) |
| `b00c8834` | dashboard | Create Agent modal ignores Esc and backdrop click and never moves focus into the modal | source | reproduced ×3 (dashboard, library edges, repro) — focus never enters the modal |
| `3a51bfac` | agent-detail | ConfirmDialog primitive has no Esc, focus trap or initial focus; delete-agent confirm does not restate the consequence | source | reproduced ×3 |
| `381355a4` | agent-detail | Execution-log modal and Create MCP Key dialog do not close on Esc | source | reproduced ×2 |
| `f457dc14` | agent-detail | Agent Detail header row causes horizontal page scroll at 768 and 390 | observed x2 | reproduced ×3 (also trinity-system) |
| `e1007ae8` | agent-detail | Agent Detail tab clicks never update ?tab= so refresh and back lose the tab | source | reproduced ×2 |
| `89d516c8` | agent-detail | Schedule form opens blank and accepts an empty submit without any named error | source+edges | reproduced ×2 (explore + edges) |
| `11226c3d` | agent-detail | Password fields without autocomplete opt-out get the admin login autofilled into third-party credential fields | source | observed ×3 surfaces; source-confirmed |
| `930abed1` | agent-detail | Sharing tab renders ChannelConfigDialog that is never imported (6 Vue warnings, dead dialog path) | source | source-confirmed; observed once |
| `4d727d26` | workspace | Workspace deep link to an unknown session shows the previous agent instead of a not-found state | source | reproduced ×2 (+3 in-run) |
| `0abec0bc` | operations | Operations lists grow the page instead of scrolling in a bounded viewport | observed | reproduced ×2 |
| `1c9b4cca` | operations | Operations shows the page size as the total (Notifications Total 50 vs Pending 427; Active Alerts 50) | API | reproduced ×2 + API |
| `05a55425` | operations | Health tab shows no circuit-breaker state for agents whose breaker is DORMANT | source | reproduced ×2 |
| `6b84de2f` | operations | Operations Needs-Response cards and Health rows overflow horizontally at 390 | observed | reproduced ×2 |
| `a092f108` | library | Skills assign dropdown offers agents that no longer exist | API | reproduced ×2 + API |
| `94c5401b` | library | Header navigation disappears below 640px with no menu | source | reproduced ×3 (dashboard, library, repro) |
| `be77459c` | settings | Settings tab strip is hand-rolled and overflows the viewport at 768 and 390 | source | reproduced ×3 |
| `ad284d77` | settings | Edition-gated Settings tabs fall back to General on a cold load or refresh | source | reproduced ×2 (4/4 tabs first run) |
| `8c213903` | settings | Retention tab never renders the backup status the API already returns | source | reproduced ×2 |
| `9d9030cd` | settings | Email whitelist accepts any string: no client-side or server-side validation (invalid entries already persisted) | observed+API | reproduced ×3 (explore, edges w/ server 200s, repro) |
| `a284ab64` | settings | MCP Keys list renders 456 cards unbounded (52k px page) | observed | reproduced ×2 |
| `318f03eb` | mobile | Help chat FAB overlaps the System tab on /m at 390 | source | reproduced ×2 (+ landing card at 390) |
| `1bfe4103` | enterprise | Audit table on the edition-gated audit surface has no bounded viewport, no sticky header and clips columns at 390 | observed | observed once (computed styles) |
| `a2d79bab` | portal | Public chat thread grows the page instead of scrolling inside its container | observed+source | observed once + source |
| `b0b78d8e` | workspace | Message over the 8000-character server limit fails with a bare error 422 and no client-side limit | source | observed once + source |
| `0f801498` | workspace | Closed room Send button re-enables as soon as text is typed | source | observed once + source |
| `59ba7dd2` | workspace | One rail click pushes two history entries so Back needs several presses | source-plausible | observed once; source plausible |
| `15a9833a` | workspace | Composer draft is silently lost on a thread switch (confirmed twice) | observed x2 | reproduced ×3 (explore, edges, repro) |
| `1cbc0f01` | library | Agent name has no length ceiling or format allowlist; hostile input is sanitized into a real agent with no preview | source+observed | observed once (5 creations) + source |

### Repro notes for the Medium class findings

- **Modal contract (four dialogs, one primitive).** `components/ConfirmDialog.vue` has a backdrop-click cancel and nothing else: no Escape handler, no focus trap, no initial focus (7 call sites). `CreateAgentModal.vue` has neither Escape nor backdrop handler; focus never leaves the trigger button behind it. `TasksPanel.vue:408` (execution log) and the Create MCP Key dialog close only via their X/Cancel. The delete-agent confirm's whole message is "Are you sure you want to delete this agent?" (`composables/useAgentLifecycle.js:50`). Contract: "Esc + click-outside close; focus trapped, initial focus on the safe action; destructive confirms restate the consequence."
- **Unbounded surfaces.** Operations Needs-Response (23 655 px for 200 items), Notifications (7 424 px), Executions (2 211 px); Settings MCP Keys (457 cards, 53 209 px) and whitelist (63 rows); the edition-gated audit table (50 rows, header `position: static`, wrapper `overflow-x: hidden` clipping three columns at 390); the public chat thread (`min-h-screen` chain, the inner `overflow-y-auto` never engages). Contract: "bounded viewport, sticky header, stated total; the page never grows without limit."
- **Dishonest totals.** `GET /api/notifications?status=pending&limit=50` and `GET /api/monitoring/alerts?…&limit=200` return `count` equal to the page length; the Notifications stats card shows "Total: 50" beside "Pending: 427"; Health shows "Active Alerts (50)" above 5 rows; `/m` badges read "Queue 100 / Alerts 100" against 296 / 427 real.
- **Circuit breaker invisible on Health.** `components/MonitoringPanel.vue` contains no breaker rendering; two agents whose breaker is DORMANT (visible only in the operator queue) read "healthy" on the Health tab.
- **Horizontal overflow at supported widths.** Agent Detail header CPU/MEM/uptime row: `scrollWidth` 882 at 768 and 874 at 390 (also on `trinity-system`). Settings tab strip: 946 at 768, 938 at 390, hand-rolled `v-for` (`Settings.vue:25`), `OverflowTabs` not imported. Operations: the Needs-Response card list (841–911 px wide) and the Health unknown-status rows (~500 px) at 390, isolated by hiding children — the tab strip is not the cause.
- **Navigation.** `NavBar.vue:25` hides the whole link strip below 640 px (`hidden sm:flex`) with no menu; the only navigation at 390 is the logo. `AgentDetail.vue` reads `?tab=` but never writes it, so refresh and back lose the tab on the busiest page. The router guard returns a bare `/login` and `Login.vue` pushes `/`, so an expired session or a logged-out deep link never returns to its destination (confirmed on four routes). Four edition-gated Settings tabs resolve once at setup (`Settings.vue:2226`) before entitlements load and fall back to General on a cold load or reload.
- **Workspace.** `Portal.vue:1766`: the session-id watcher acts only when the id is a known thread, so `/workspace/c/<unknown>` keeps the previous agent on screen and 404s its history (the room path has a proper "Could not load this conversation" branch). A composer draft is lost on a thread switch (three reproductions). One rail click pushes two history entries (`Portal.vue:1324` then `:1474`). A closed room's Send re-enables on typing (`PortalRoom.vue:279` ignores `isClosed`). A message over the backend's 8000-character cap (`client_portal/models.py:306`) fails as "error 422" with no client-side limit or named message.
- **Validation gaps.** `routers/settings.py:1644` lowercases and inserts any string into the login whitelist (`not-an-email-2`, `a@b`, `<script>@example.com`, a 306-char local part all → 200; an old `not-an-email` row is already persisted). `utils/helpers.py:275` sanitizes agent names with no length cap and no preview: `a`×65, `1starts-with-digit`, `../../etc/passwd` → `etc-passwd`, and `<script>alert(1)</script>` → `script-alert-1-script` all became real agents (no XSS; all deleted). The schedule form opens blank (`SchedulesPanel.vue:1215`) and an empty submit shows no error.
- **Stale and missing data.** `GET /api/skills/assignments` lists four deleted agents as assignable. The Retention tab never renders the `backup` object the same request already returns (0 occurrences of "backup" in `Settings.vue`). `ChannelConfigRow.vue:41` renders `<ChannelConfigDialog>` without importing it (6 Vue warnings, dead dialog path, since v0.8.0).
- **Autofill.** 17 `type="password"` inputs in 12 files carry no `autocomplete` attribute; the browser password manager filled the platform login into the Payments API-key field, the Slack bot-token field and the Slack client-id text field (`Settings.vue:935`). Environment-dependent, but the reveal-eye and Save would expose or persist the login secret.
- **`/m`.** `App.vue:36` mounts the help-chat FAB on every authenticated route lacking `hideHelpWidget`; the `/m` route has no such flag, so the FAB covers the right third of the System tab (rects 310–366 × 764–820 vs 260–390 × 787–844).

## 4. Low

| fp | area | finding | validation | reproduction |
|---|---|---|---|---|
| `4f4330c2` | auth | Header connection indicator read Disconnected after a client-side login once (flaky; source shows connect() only runs at app mount with a token) | source | not reproduced in the fresh-context pass |
| `0c76cf52` | auth | Failed admin login Try Again drops back to the email-first screen | source |  |
| `b6912b03` | auth | Unknown routes silently redirect to the dashboard (no not-found state) | source |  |
| `66f3df0e` | dashboard | Dark list-view column headers and many meta labels use gray-500 | source |  |
| `0f0bc129` | dashboard | View-switcher active state uses raw bg-blue-600 (in baseline) | source |  |
| `baa0e1f7` | dashboard | ?view= deep link is a one-shot initializer and does not survive reload | observed |  |
| `129f0113` | agent-detail | Duplicate requests on load (stats, activity, git/status, playbooks, workspace history) | observed |  |
| `53bc4815` | agent-detail | Relative timestamps have no absolute time on hover (Tasks list, Reports tab raw ISO) | observed x2 |  |
| `369e21fb` | agent-detail | Payments tab logs a console 404 on every visit for an unconfigured agent | source |  |
| `a7fe65e4` | agent-detail | Cross-model validation setting is a square checkbox beside toggle switches | observed |  |
| `d829f034` | agent-detail | Playbooks empty state lists the same scan path twice | source |  |
| `8ea3ef5c` | operations | Executions filter does not sync to the URL and double-fires the fetch | observed |  |
| `a2fd84b0` | operations | Resolved empty state has no next action; Reports tab shows raw ISO microsecond timestamps | observed |  |
| `e0cbdbc2` | library | Create form does not preview the sanitized slug (Bad Name! became bad-name) | source |  |
| `4576475a` | library | Use Template opens with an empty slug and an unlocked template picker | observed |  |
| `63f7bfd7` | library | Library CTAs and skills assign controls are hand-rolled (4px radius, no ring) | observed |  |
| `e2fb364c` | library | /templates redirect drops the URL hash | source |  |
| `02430864` | settings | Console 404 for unset public_chat_url on every Settings load | source |  |
| `64a60089` | settings | Save buttons enabled with no change on several Settings forms | observed |  |
| `6f1fbbcc` | settings | Email whitelist table unbounded (63 rows, no total) | observed |  |
| `93a7eec3` | settings | MCP keys list has no copy control | observed |  |
| `b9cee2db` | settings | General tab How-it-works panel renders on every Settings tab | observed x6 |  |
| `47746a35` | mobile | /m resets to the Agents tab on refresh; tabs are not history entries | observed |  |
| `ba99dd4f` | mobile | /m Ops badges show 100 (the fetch limit) with no plus or total | API |  |
| `08012ddc` | mobile | /m is hardcoded dark with no theme control | source |  |
| `c23fa124` | mobile | /m controls use 8px and 0px radii, not the 6px control standard | observed |  |
| `ef191385` | enterprise | Edition-gated landing counter says entitled where cards say Available or Coming soon | source |  |
| `54cb4184` | enterprise | Edition-gated audit filters do not sync to the URL; timestamps absolute-only; no-match state lacks next action | observed |  |
| `ea4e80fc` | portal | Public chat uses two bespoke spinners and a hand-rolled Send button; New conversation uses window.confirm | source |  |
| `bd19c28c` | agent-detail | Schedule form has no next-run preview for a valid cron; Back discards a draft silently | observed |  |
| `007ad8d5` | agent-detail | Files tab overflows horizontally at 320px | observed |  |
| `11eee28e` | operations | Operator queue holds 270 identical stale-base-image items and 383 duplicate heartbeat-lost notifications | API |  |
| `4fbebc97` | workspace | Fenced code blocks wrap instead of scrolling; chat messages carry no timestamp at all; ghost model select stays open on Esc | observed |  |
| `28e69f66` | library | Empty agent name submit is a silent no-op; rejection copy lacks an example | observed |  |
| `74dce3aa` | settings | Retention Save stays enabled on out-of-range values; MCP key name has no validation or length cap; Default Model row does not stack at 320px | observed |  |

Observations kept out of the findings (by design or taste): inline `<b>` and stripped `<script>` in agent replies (DOMPurify with `FORBID_TAGS: ['style']`); `Bad Name!` → `bad-name` (deliberate `sanitize_agent_name`); `/templates` dropping the hash (`Library.vue:562` on purpose); `/m` forced dark (`MobileAdmin.vue:1477`); the "4 of 6 features entitled" copy (counts entitled including "coming soon"); a 10 000-character echo reply that summarised instead of echoing (model behaviour). **Backend hygiene surfaced through the UI:** the operator queue holds 270 identical "System agent is running a stale base image" items for `trinity-system` and the notifications table 383 duplicate "heartbeat lost" rows across six agents — no dedup on repeated platform alarms.

## 5. Verified working

- Login: mode-detect skeleton per contract, empty submit disabled, wrong password named, Enter submits, `/login` while authenticated and `/setup` redirect cleanly, expired session never white-screens; `/agents/<unknown>` has a proper not-found state.
- Dashboard: timeline default persists; list statuses honest; sort; filter hotkey `/` with live count and Esc; filter consistent across timeline/grid/list; live update from a second tab without reload or request storm; no console errors.
- Agent Detail: honest header, execution detail and honest execution not-found, double-Enter guard (one POST), files preview with a named reason on protected files, schedules empty state, `OverflowTabs` collapse with active-tab dot, rapid tab switching without storms, every direct `?tab=` renders its panel, reports/canvas/loops/playbooks/git/folders empty states name purpose and next action, credentials "needs none", skills framed as assignment with stated total, settings defaults pre-filled, brain disabled → clean redirect, `/system-agent` → `trinity-system` with honest header and no destructive control exposed.
- Workspace: new thread pre-allocates `/c/<id>`; closed room shows reason and disabled send; room not-found honest; all legacy redirects keep query and hash; thin hover-revealed rail scrollbar; ghost model select with chevron flip and no send on change; `@mention` creates a room with an honest banner; 390 drawer and composer; refresh re-sticks to bottom; 10 000-char composer bounded; markdown, code, RTL render.
- Operations: Health honest for the trio and "unknown" for never-checked agents; check-now 200; four legacy redirects; `?tab=bogus` fallback; reports render with export controls; failed (red, reason) and cancelled (gray, reason) honest.
- Library: hidden test templates excluded; duplicate/reserved names → named 409; GitHub template field validates client-side with an example; cancel discards and resets; systems tab honest "already installed"; skills status line honest; 320 and 720×450 clean.
- Settings: secrets masked everywhere; `?tab=nope` and `/api-keys` redirect; gated tabs render fine once clicked; MCP create default scope; agents quotas pre-filled; Custom Instructions textarea `resize: vertical`; back mid-edit keeps the draft; rapid tab switching tracks `?tab=`; 46/46 focusable controls show a focus ring.
- `/m`: password-only login with named error; statuses and Fleet Health match the API; lists bounded inside `main`; 320 and 390 clean; polling 2 s, no duplicates.
- Public link: no platform chrome, only public endpoints, invalid token → designed state; single POST per send; `<b>`/`<script>` inert; copy-message feedback; attach opens a chooser.
- Edition-gated area: landing clean in both themes; audit totals and pagination consistent; row detail panel closes on Esc; empty state only after a 200.

## 6. Not covered this run

- `scenarios` lane (the 28 phase files) and the `refresh` lane — out of scope for an explore/edges run; the phase files were not driven.
- Two-tab WebSocket update on agent-detail (message cap), 8+ workspace chat tabs (unsent tabs are singletons; needs seeded threads), slow/aborted network via route interception (no tool), the public email-verification code flow (live mail provider), toggles that persist on Space (guard), arrow keys on native selects (automation limitation), non-admin visibility of `/settings` (no user created), quick-reply buttons on the public page (message cap).
- Loading skeleton frames on most routes (local backend too fast to catch).

## 7. State changes made by this run

- Stack rebuilt and restarted: `docker compose build`, `build-base-image.sh` (base image `0.9.5-rc2`), `TRINITY_UNATTENDED=1 start.sh`. Provenance in `/api/version` reads `unknown` because the images were built outside `start.sh`'s exported git vars.
- Fixtures created and left running: `test-echo`, `test-counter`, `test-delegator` (local templates). Seeded: ~15 chat/task messages on test-echo, 1 task on test-counter, one workspace thread on test-echo, one auto-created room with test-echo and test-counter (left in place), 3 public-chat messages.
- `test-echo` access policy: `require_email` flipped false for the public send-flow sheet and **restored to true**. Public link `46jff8mOu_gxgvETDcrE_Q` created and **revoked** (0 links left).
- Throwaway agents created by the create-form probes and deleted immediately by the sheet guard: `bad-name`, `1starts-with-digit`, `a`×64, `a`×65, `etc-passwd`, `script-alert-1-script`; their six retained workspace volumes (65 MB each, kept by design until the retention sweep) were removed with `docker volume rm`. Roster verified back at 10 agents.
- Whitelist entries added by the validation probes and deleted via API (count back to 63). Workspace model preference set and restored. Theme toggled and restored on every sheet. A health check was triggered on test-echo. `trinity-system` was only observed.
- Nothing was committed by the sweep; no issues were filed (held for confirmation).

## 8. Next actions

1. Confirm which findings to file; suggested first: C-1 (one-line fix + regression test), the modal-shell class (one ticket on `ConfirmDialog` + the two hand-rolled modals), the unbounded-surface class, the whitelist server-side validation, the agent-name cap, the gated-Settings-tab cold-load fallback, the mobile header navigation.
2. Playbook fixes before the next run (see `playbook-notes` in the run summary): Area Table is stale (nine redirect routes, no `network`/`sessions`/`system-agent` routes, `/:pathMatch` unassigned); the tester profile is not fresh; `browser_handle_dialog` is missing from the tester's tool list; workspace fixture ids must come from the client-portal sessions endpoint; the public-link probe needs `require_email:false`; the hostile-name taxonomy contradicts the deliberate sanitize-then-accept design; deleting a guard-created agent leaves its volume.
3. `needs_repro` for the next run on these areas: the "Disconnected after login" indicator (source says real, repro said no), the two-history-entries rail click, the edition-gated audit table styles, the closed-room Send.

## 9. Playbook observations (first full run of /ui-sweep v1.0)

- Step 2.4: `GET /api/version` is auth-gated (401 without a token); the skill text implies a bare GET. Add "with the minted token".
- Area Table is stale vs router: `/agents`, `/monitoring`, `/templates`, `/operating-room`, `/executions`, `/events`, `/api-keys`, `/system-agent`, `/network`, `/sessions*`, `/portal`, `/portal/c/:id` are all redirects. Areas `network` (10), `sessions` (11), `system-agent` (9) have no routes of their own; `portal` (12) is only `/chat/:token`. `/:pathMatch(.*)*` is unassigned.
- ui-integration-tester says "Playwright starts with a fresh browser" — false here: the profile restored a JWT and the browser autofilled admin credentials. Sheets that test logged-out state must clear storage first; the agent def should say so.
- `mcp__trinity__tag_agent` unavailable this session (trinity MCP server auth failed) — fixture tagging silently skipped; fine as best-effort but the skill should say the tag is optional.
- "Redeploy" is outside the skill: start.sh does not rebuild images (only builds the base image if missing). `docker compose build` outside start.sh stamps provenance as `unknown` in /api/version (git_commit_short=unknown) — the documented "rebuild with: docker compose build && docker compose up -d" hint has the same effect. Deploy-doc nit, not UI.
- The skill and the updated agent definition are uncommitted in the `.claude` submodule (`?? skills/ui-sweep/`, `M agents/ui-integration-tester.md`).
- Sheet sizing: the auth sheet (9 routes, checklist on 3) used 38 of 40 min, 171 tool calls, ~295k subagent tokens. The reference's "explore ≈ 6–8 routes" cap is about right for a 30-min budget; the checklist's per-route cost dominates.
- Fixture create via POST returns `status: stopped` and the container then auto-starts within ~5 s; the skill's "if present but stopped → start" branch must not fire immediately after create.
- Subagent report includes absolute local paths (screenshots, source files) — the orchestrator must strip them before anything public; the skill already says so for issues, but not for the committed report's screenshot lines (say "filename only").
- The subagent invented a `?delete_data=true` parameter on DELETE /api/agents — harmless (ignored) but the fixture-cleanup instruction should name the real contract: delete keeps the workspace volume until the retention sweep, so a guard-created agent leaves a 65 MB volume behind; the sheet GUARD should say "delete, then `docker volume rm agent-<name>-workspace`".
- The reference's hostile-name list ("uppercase, spaces, 64 chars, leading digit… → Expected: a named validation error") contradicts the backend's deliberate sanitize-then-accept design; the edges taxonomy should say "either a named error OR a previewed sanitized slug", or the sweep will keep creating throwaway agents.
- Unsent workspace "New chat" tabs are singletons — the 8+-tab overflow probe needs pre-seeded threads, not "open new threads without sending".
- The workspace fixture id must come from the client-portal sessions endpoint, not POST /api/agents/{name}/session (different store).
- Public-link probe needs the fixture's access policy `require_email:false` (PUT /api/agents/{name}/access-policy with the full body) or the run blocks at the email gate on installs with a live mail provider.
- `ui-integration-tester` cannot dismiss native dialogs: `mcp__playwright__browser_handle_dialog` is not in its `tools:` list, so `confirm()`-guarded verbs (whitelist Remove, public chat "New", …) dead-end and the runner falls back to the API. Add the tool to the agent definition.
- The 16 subagent sheets used ≈4.6 M subagent tokens and 2 700 tool calls; sheets ran 9–38 min each, none hit the 40-min budget.
- `browser_press_key F5` does not reload the page in the tester's Chromium; sheets must use `browser_navigate` to the same URL for a real reload.
- `mcp__trinity__report` was unavailable (the Trinity MCP server rejected its key this session), so no Trinity report was published.
- A subagent regenerated `src/frontend/raw-color-baseline.json` while checking a finding (the refreeze annotations were lost); the orchestrator reverted it. The tester should be told never to write outside the artifact directory.
