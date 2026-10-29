# Release-test matrix → automation map (trinity-enterprise#905)

Source: trinity-enterprise#847 (1.0 matrix, 31 rows, rc1→rc4) — its "Coverage by layer" table, per-row "Not covered" notes, and the rc3 recheck logs (B9: dolho 2026-10-13, pash PG recheck; B14: pash 2026-10-11).
Status: DRAFT v0.1, 2026-10-29. Effort and target are proposals until reviewed. Work branch: `feature/ent905-release-matrix-automation`.

## Lanes (where a suite runs)

| Lane | Trigger | Budget | Credentials | Public workflow today |
|---|---|---|---|---|
| **per-PR** | every PR to `dev` | unit 45 min; journey-smoke 30 min (8–12 boot + 3–4 journeys) | none, by policy (#1896: `pull_request` exposes secrets to forks) | `backend-unit-test.yml`, `journey-smoke.yml`, `schema-parity.yml`, `frontend-build.yml`, `journey-impact.yml` |
| **nightly** | cron on `dev` / open PRs | 45 min | none (`ANTHROPIC_API_KEY: placeholder`) | `dev-nightly.yml`, `integration-nightly.yml`, `backend-unit-nightly.yml`, `frontend-e2e.yml` (advisory) |
| **release-cut dispatch** | `workflow_dispatch` on an RC tag | 60 min | repo secrets allowed (dispatch is not fork-reachable) | `system-chains.yml` (J15–J23). **NEW:** `upgrade-in-place.yml` (J02), recovery journey (B9) |
| **manual** | a person, logged on the matrix | — | — | legs with a *named* reason: multi-instance, real third-party side, real e-mail delivery, "does it read right" |

## Fixes from the 1.0 fix round — AC line 1

| Fix | Regression today | Kind | Where it lives | Gap |
|---|---|---|---|---|
| t#3588 cancelled/claimed activities left `started` | `tests/unit/test_3588_close_cancelled_claimed_activities.py` | unit | public | live leg (restart mid-turn, watchdog claim) → B9 recovery journey |
| t#3589 claim time recorded as end | `tests/unit/test_3589_claim_completed_at.py` | unit | public | same |
| t#3590 rebuild drops pull env | `tests/unit/test_3590_rebuild_pull_env.py` | unit | public | live leg (rm -f + /start, de-pilot drift recreate) → B9 recovery journey |
| t#3533 chat titled with generator failure text | `tests/unit/test_3533_title_failure_text.py` | unit | public | none |
| t#3520 gated run end never reaches requester | `tests/unit/test_3520_gate_completion_notice.py` + `tests/system_chains/test_j21_approval_chain.py` | unit + chain | public | J21 not yet `built` |
| t#3526 held Chat-tab request lost on reload / duplicate approvals | `tests/unit/test_3526_held_chat_trace.py` + vitest `skillGateSenders`, `mobileAdminSkillGate` | unit | public | none |
| t#3521 thumbs-down comment never reaches alert | `tests/unit/test_3521_problem_report_comment.py` | unit | public | none |
| t#3545 own repo treated as catalog template | `tests/unit/test_3545_own_repo_not_catalog.py` | unit | public | none |
| ent#844 sweep cancels approvals by age | `tests/unit/test_ent844_expired_approval_outcome.py` + vitest `queueItemDetailExpired`, `operatorQueueOutcomeUnknown` | unit | public | none |
| **t#3534 role card empty without `x-role`** | **NONE** | — | fix is in the private submodule (ent#818, bump PR t#3607) | write the regression in the private submodule's `tests/` — not in this repo |

The three unit files for t#3588/3589/3590 satisfy AC line 1 on their own (a unit test is a named automated regression). Whether they are *enough* is a different question, answered by AC line 2 — see the decision record below.

## Table A — release story

| Row | Manual legs (what a person did) | Automated today | Target suite | Lane | Effort | Stays manual (named reason) |
|---|---|---|---|---|---|---|
| A1 role card: who it is for, what it is measured on | assignments read on role card; both themes | none on OSS CI | J15 rollout chain + J19 objective→person (`tests/system_chains/`) exist, not `built` | release-cut dispatch | S (make green) | "does it read right" |
| A2 approval chain: approve / refuse / expire, requester told | D1–D8, D3 expired, D5 double-approve, Chat tab quotes reply, same request twice → one card | J21 (`test_j21_approval_chain.py`), units t#3654, t#3520, t#3526 | J21 — add D3 expiry + D5 double-approve assertions | release-cut dispatch | M | — |
| A3 self-change without grant refused | platform half; chat half blocked by Refco template | 1 unit file (thin, B5) | API integration test in `tests/` root: four grant flags × agent-key attempt, with/without grant (credential-free: agent MCP key, no model) | nightly | M | chat half until template allows |
| A4 project open→close with steward | C3 new project page, task to Done, F4 approval in project log, F6 stuck-task digest | J22 + J23 (`tests/system_chains/`) | J22/J23 green; F6 7-day stall via `tests/clockshift.py` | release-cut dispatch (+ private-submodule tests for module internals) | M | three-identity UI pass |
| A5 morning brief reaches the seat holder | brief fires, lands in Inbox, 3 vs 0 fresh, refusal named | J11 (`test_j11_brief_delivery_journey.py`) | J11 green; assert on the held row (ent#689) | release-cut dispatch | S | ent#689 held row not reproducible on Refco |
| A6 number reads stale when stale | stale badge + timestamp before write, "updated just now" after; REST + join agree | J12 declared metrics, J19 | J19 green with stale assertion | release-cut dispatch | S | — |
| A7 rating reaches a reviewer | thumbs-down → operator alert with comment | unit t#3521 | API integration test: rate via API → alert row carries comment + owner-only read (ent#866) | nightly | S | "a person rates" UI click |

## Table B — area pass

| Row | Manual legs | Automated today | Target suite | Lane | Effort | Stays manual (named reason) |
|---|---|---|---|---|---|---|
| B1 Workspace chats | compose, attach, reload, star, close tab, key map, shared thread; themes; 320 px | e2e `workspace-chat-tabs`, `-drafts`, `-stick-to-bottom`, `-code-blocks`, `-model-choice`, `-absorbs-session` | Playwright from PHASE_29: attachments persist (t#3265), star first click (t#3356), 320 px | nightly (frontend-e2e) | M | — |
| B2 Inbox, asks, rail | ask lands → Discuss/Dismiss/Something else → agent woken; Asks tab | e2e `workspace-inbox`, `workspace-rail-reserved` | Playwright from PHASE_30 (ask outcomes) | nightly | M | agent actually woken needs a model turn |
| B3 Operator queue & alerts | same alert twice → one row; ack → gone; malformed entry → siblings delivered; Operations view | 21 unit; e2e `mobile-admin-queue-respond`; `tests/test_operator_queue.py` | extend `tests/test_operator_queue.py` (API legs); Playwright PHASE_31 | nightly | S + M | — |
| B4 Skills, gates, library | gate set → call from chat → ask opens; refusal body without approval; Library + Skills tab | 45 unit; J21 (partial) | J21 covers gate path; Playwright PHASE_32 for Library | release-cut + nightly | M | — |
| B5 Self-change grants | four flags via agent-settings API; attempt each self-change ± grant | 1 unit (thin) | same test as A3 | nightly | M | — |
| B6 Git sync | fork-to-own with personal PAT, divergence >24 h, fetch-and-rebase refuses, sync health, bind button (t#3546) | `tests/git_sync/` nightly | extend `tests/git_sync/` with a token-bearing dispatch lane (pattern: `CHAIN_SKILLS_GITHUB_TOKEN`); divergence via `clockshift.py` | release-cut dispatch | M | t#2931 leg; a real external GitHub side |
| B7 Projects | create, staff, task, verify, close as three identities; non-member cannot read | none on OSS CI | J22/J23 for the API path; private-submodule `tests/` for module internals | release-cut dispatch + private CI | M | three-identity UI (private-module UI not bootable on public e2e) |
| B8 Metrics, widgets | push points, stop pushing, read stale (API); role card + widget with `dims:` (UI) | 16 unit; e2e `settings-retention-metric-rows`, `dashboard-*`; J12 | J12/J19 green; Playwright PHASE_28 `dims:` render | release-cut + nightly | S + M | — |
| **B9 Pull dispatch, scheduler, loops** | legs 1–6 of the rc3 log: cancel push, cancel pull, watchdog claim across restart, restart mid sync turn → real duration, rm -f + /start rebuild keeps pull env, de-pilot drift recreate once; primary proofs: ids, cron, webhook idempotency, capacity, chain depth, loops, no replay, breakers | 40 unit; `tests/scheduler_tests/`; canary E-01/E-05; units t#3588/3589/3590 | **NEW recovery journey** (`tests/journeys/test_j24_*` or name TBD) — needs a real ≥60 s turn + `docker restart trinity-backend` + pull pilot with 3 workers ⇒ model credential ⇒ dispatch-only lane | release-cut dispatch | L | t#3306 cross-worker (needs >1 uvicorn worker); chain depth via schedule/session; t#3086 effect guard |
| B10 MCP server & A2A-in | caller turn carried; description cap 2,048; seat decisions own-agent only; agent key cannot reach sibling | 3 MCP test files; J10 agent-calls-agent; unit `test_ent629_agent_reach` | J10 green; `mcp-server-test.yml` unit for description cap | per-PR + release-cut | S | — |
| B11 A2A & trusted networks | second instance over trusted network keyless; untrusted address refused; allow-list 403; exposure-off 404 | 10 unit | unit/integration for refusal matrix | per-PR | S | **multi-instance** (two Trinity hosts) — named manual; AAuth flag on; paid x402 |
| B12 Auth, security, files | agent key: admin routes 403, credential files 403, leading slash denied; file browser UI | 11 unit; `tests/security/` nightly; `test_293_*` admin-gate guard | already covered server-side; Playwright PHASE_09 for browser | per-PR + nightly | S | — |
| B13 First-run, setup, system agent | fresh DB → /setup → admin → system agent; `ADMIN_USERNAME` ≠ admin | e2e `system-install`; journey smoke (lifecycle) | J01 (not built) — local lane first, `ADMIN_USERNAME` variant | per-PR (local lane) | M | — |
| **B14 Upgrade in place** | restore v0.9.5 → boot RC: `/health` no `first_pending`, `pre-migration-*.db` present, table count, every list endpoint returns old rows, `.env` unchanged, idempotent second boot; SQLite AND PG | `schema-parity`, `pg-migrations` (fresh DB only) | **J02** `tests/journeys/test_j02_upgrade_in_place.py` + `upgrade-in-place.yml` (`from_ref`/`to_ref`, matrix sqlite×pg); synthetic data seeded by the test; OSS table set only (private-table count → private twin) | release-cut dispatch | L | a real customer data copy (never public); hosted/GHCR image path until J01 cloud lane |
| B15 Published images (DO, AMI, Vultr) | boot from RC images, first login, one agent, one chat | `build-without-submodule`, `backend-image-smoke` (build + boot, no login) | J01 digitalocean lane | release-cut dispatch | L | **AMI, Vultr** (third-party sides) |
| B16 Client portal | logged-out public link, email verify, return after 24 h (t#3406), background ask in Inbox | e2e `portal-agent-page-overview` | Playwright PHASE_27 + API for return visit (`clockshift.py`) | nightly | M | **real e-mail OTP delivery** |
| B17 Canvas, charts, mermaid 12 | new canvas follows, bar tooltip at viewport edge, mermaid renders | e2e `canvas-gallery*`, `canvas-table-markdown`; `frontend-build` (`test:unit` 5450 green) | vitest for mermaid 12 types; Playwright tooltip edge | per-PR + nightly | S | — |
| B18 Channels | one Slack + one Telegram round trip; resend same idempotency key → one delivery | 4 unit | unit for #665 idempotency leg (not covered) | per-PR | S | **real Slack/Telegram side** |
| B19 Subscriptions & paid | list owner-scoped; operator-resume PUT refused for agent key; one paid chat | 4 unit | unit/integration for the two gates | per-PR | S | **Nevermined paid chat** (third party) |
| B20 Logs, telemetry, audit | Audit Log + host/agent stats render | e2e `audit-dashboard` | Playwright PHASE_22; 375 px clipping (t#3610) | nightly | S | — |
| B21 Templates, webhooks, reminders, public links | one webhook fires; one template create; one public link; seat-write schedule (t#2924) | — | API tests in `tests/` root (webhook fire, template create, public link) | nightly | M | t#2924 needs a model turn; t#2728 reply quality |
| B22 Agent create, share, folders | create from GitHub, share to second user, shared folder read from both; second stack's volumes survive sweep (t#3231) | journey smoke (lifecycle); e2e `agent-detail-*`; `tests/test_access_control.py`, `test_agent_files.py` | extend access/files tests; unit for t#3231 sweep scope (data-loss class) | per-PR + nightly | S | — |
| B23 Login, MCP keys | email OTP, admin login, mint key, revoke | e2e `smoke`, `api-keys-copy` | e2e already; OTP via test-mode code if one exists | nightly | S | **real e-mail delivery** |
| B24 OSS build without submodule | 0 × 5xx over 126 GET routes + 26 UI routes; 17 private-module prefixes 404 | `build-without-submodule` (build only) | add a boot + GET-sweep step to `build-without-submodule.yml` (route list from OpenAPI, assert no 5xx, private prefixes 404) | per-PR (path-filtered) | S | — |

## AC line 2 — the B9 recovery journey (decision record, 2026-10-29)

ent#905 acceptance has two independent lines. Line 1: every 1.0-round fix has a named automated regression or a named manual reason — satisfied by the unit files above. Line 2: "the B9 and B14 (J02) legs are implemented and green in CI" — unconditional. So the live B9 journey is required regardless of how line 1 is read; its L effort is a fixed cost, not a choice.

**Why a unit test is not enough here.** All three B9 fixes sit in recovery paths, the #1804 class that has now shipped three times with green units each time — the functions were right, the wiring that reaches them in the real failure path was not.

| Fix | The unit proves | The unit cannot prove |
|---|---|---|
| t#3588 activities left `started` | closing logic closes rows when called | a real terminate / a real watchdog claim actually reaches that logic |
| t#3589 claim time as `completed_at` | the clamp helper + the ops-level stamp | restart mid-turn → agent retains result → watchdog claims → `completed_at` is the agent's finish, not the claim |
| t#3590 rebuild drops pull env | the env dict carries `TRINITY_PULL_MODE` + `TRINITY_MAX_PARALLEL_TASKS` | `docker rm -f` + `/start` → recreated container runs a pull worker → the queue drains |

**Why it is L.**
- A real agent turn of ≥60 s with a backend restart mid-turn needs a real model credential. No stub runtime exists in the base image; the journey harness gate (#2812, `tests/journeys/conftest.py`) skips keyed journeys on a keyless stack.
- Watchdog grace 180 s + a cleanup cycle of up to 300 s per claim leg; six legs ≈ 15–25 min wall clock (estimate).
- A pull pilot with 3 workers plus the de-pilot drift path.
- Credentials are forbidden on `pull_request` (#1896) and on nightly (placeholder key). The only lane is `workflow_dispatch` with a repo secret — precedent `system-chains.yml` + `secrets.CHAIN_SKILLS_GITHUB_TOKEN`. So: a new workflow file, a new repo secret, and real tokens spent per run.

**Open decisions** (owner: pavshulin; none taken yet).

| # | Question | Options | Recommendation |
|---|---|---|---|
| D1 | Scope of the B9 journey | all six rc3 legs, or the three that carry the fixes (watchdog claim across restart; restart mid sync turn → real duration; rm -f + /start keeps pull env) | three first; cancel push / cancel pull / de-pilot recreate as a follow-up PR. Whether the issue author means all six is unconfirmed — ask on #905 |
| D2 | Credential lane | dispatch-only with a repo secret now, or build a stub runtime (fake model, credential-free) first and unlock nightly | dispatch + secret now; stub runtime is its own M–L project that pays off across every keyed journey — file it separately |
| D3 | Who provisions the secret | needs admin on the public repo to set the dispatch-lane model secret | ask early, in parallel with J02 work; it is the one blocker outside this branch |
| D4 | Order of the two L items | B9 first or J02 (B14) first | J02 first — credential-free, no external blocker; B9 once the secret exists |

Note on "green in CI" for a dispatch lane: green when a person runs it on an RC tag, not per-PR. That matches the issue's "pre-release" wording.

## Proposed order of implementation

1. This doc + `docs/testing/STRATEGY.md` lanes table; the t#3534 regression in the private submodule.
2. J02 upgrade harness + `upgrade-in-place.yml` — closes t#2341 scope as agreed there.
3. B9 recovery journey + dispatch-only lane with a model secret — D1–D3 above first.
4. B24 GET sweep (cheap, per-PR, retires a whole SMOKE row).
5. API-level rows A3/B5, A7, B3, B21 in `tests/` root (nightly) — each a small PR.
6. Playwright phases (B1, B2, B8, B16, B20) — one issue per row, after the above.

## Rows retired for the next RC checklist (fill as suites go green)

| Row | Retired by | Lane | Since |
|---|---|---|---|
| — | — | — | — |
