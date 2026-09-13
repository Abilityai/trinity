# Feature Flows Index

> **Purpose**: Maps features to detailed vertical slice documentation.
> Each flow documents the complete path from UI → API → Database → Side Effects.
>
> For detailed change history, see `git log`.

> **Scope — the doc follows the code.** This index covers flows whose
> implementation is OSS code in this repository, regardless of which tracker the
> issue was filed in. Flows for code that lives in the optional private
> submodule (`src/backend/enterprise/`) are documented in that repository, not
> here; see [docs/ENTERPRISE.md](../ENTERPRISE.md) for how the seam works.
> Public docs never carry private-module design or schema
> (trinity-enterprise#45, enforced by `.github/workflows/enterprise-docs-guard.yml`).

---

## Recent Updates

> Newest ~20 changes only — older entries are discoverable via the **Documented Flows**
> category tables below, the linked flow doc, and `git log`. (Capped per #1360 to keep
> this index minimal and navigable.)

| Date | ID | Change | Flow |
|------|-----|--------|------|
| 2026-09-14 | #2744 | fix(skills): **a refused legacy skills-library adoption files one row per refused URL, not one per sync**. `_adopt_legacy_clone` is the first statement of every `sync_library()`, so an install past migration whose `skills_library_url` matches no configured source raised a fresh `priority:"high"`, `expires_at:None` operator-queue item on **every** sync — unbounded (288/day at the ent#236 auto-sync floor), never reaped, and un-dismissable, because the timestamped `request_id` defeats `create_item`'s `(agent_name, request_id)` ON CONFLICT DO NOTHING by construction; 17 of them, ~17% of all pending items, on the reporting install. The terminal *"this install already has sources"* branch is the designed **resting state** of a migrated install whose legacy key lingers, not a failure, so it drops to `priority:"low"` + `logger.info` with a **stable** `skills-legacy-adoption-refused-{sha256(url.strip())[:12]}` id ⇒ ≤1 row per refused URL, and — the conflict target ignoring `status` — a dismissal that finally sticks. A *different* refused URL still raises its own item. One keyword-only `steady_state` flag on the existing emitter rather than a second method (one #1677 `_ALLOWED_CALLERS` key), defaulting `False` so the two actionable call sites are **literally unchanged lines** — the strongest proof that the validation-reject and adoption-exception branches keep `high` and their repeat-visible ids. `skills-legacy-adoption-` joins `_RESERVED_ID_PREFIXES` (a URL-derived id is guessable, and `is_platform_minted` reads the same tuple to gate the ent#499 write-back and the ent#329 respond→resume dispatch this change makes an expected operator action). The URL echo is `strip_url_credentials`-scrubbed — `EmbeddedCredentialError` subclasses `ValueError`, so the validation-reject branch is exactly the one a PAT-bearing URL reaches, and the raw value was landing at ERROR in the Vector-captured log **and** durably in `operator_queue.context` (Invariant #12). Complementary to #2763, which stops the branch being entered spuriously; no schema change ⇒ Invariant #9 N/A | [operating-room.md](feature-flows/operating-room.md) |
| 2026-09-14 | #2700 | fix(workspace): **an orphaned live-call marker no longer 409s the thread for ~32 minutes when the audio socket never opens**. `/voice/start` armed the thread's live-call marker for the whole call cap *before* the WebSocket that is the only thing able to clear it existed — both clear paths and the cap watchdog live downstream of that socket — so a start whose socket never opened refused every typed turn in that thread, from any tab and from the headless `/chat`, naming a call the person could not end. The audio bridge is the only writer now: it arms the marker as the first statement inside the same `try` whose `finally` releases it, and that release is unconditional, LAST, keyed on the pre-`try` local (on `ended.portal_session_id` an `ended is None` return re-strands it — the same defect moved) and synchronous, so it runs under cancellation too. Two moves make that more than a relocation: the marker becomes a **lease** (60 s TTL renewed every 15 s by a bridge-owned task, bounded at the call's own cap + 120 s so a half-open peer cannot hold the thread forever, the 4x ratio copied from `agent_call_limiter`) — without it every crash, OOM and routine backend deploy mid-call keeps this issue's own symptom — and the lease gains an **owner** (the call's `voice_session_id`, compare-and-delete on the `clear_turn_inflight` precedent), without which a closing bridge frees the thread of a newer call on a reload or a second tab. In blast radius: the close-out is wrapped so a raising `end_session`/persist no longer skips the gemini cancel, the `saved` frame and the socket close. The read stays fail-OPEN, the 409 copy and the ent#551 `voice_call_id` exemption are byte-unchanged, and no frontend file is touched. Accepted and documented: the `/start`→connect gap is unmarked, so the claim is now scoped to *no reply lands mid-call **once the audio bridge is up***. | [workspace-voice-conversation.md](feature-flows/workspace-voice-conversation.md) |
| 2026-09-13 | #2742 | fix(git-sync): **the sync-health poll no longer takes — or orphans — the agent's `index.lock`**. `GET /api/git/status` ran ~8 bare `subprocess.run` git children **on the agent's event loop**, and `git status --porcelain` takes `.git/index.lock` on EVERY invocation to refresh the index (0.6 ms at one file, 12.8-15.4 ms at 20 000, 334-473 ms measured in a real container) — so the 60 s poller took that lock ~2×/min in every git-enabled workspace, outside `_REPO_LOCK`, racing the agent's own `git add`; a sweep-killed child then orphaned a **0-byte** lock and every later git write failed **silently** (`git status` itself returns rc=0 against a stale lock — that is the "silent" in the title). Fix: `git --no-optional-locks status --porcelain` on **exactly that one call site** (the auto-sync commit path and the `sync`/`pull` bodies want the writeback and keep the plain form); the seven status children plus `_compute_ahead_behind`/`_get_pull_branch`/`_persist_last_remote_sha` route through `run_registered`, so a sweep tick during the 30 s fetch cannot orphan ref litter (`capture_output`/`text` deleted at all ten sites — `run_registered` accepts neither); and the handler becomes a thin **loop-level single-flight** over one `to_thread` worker, so poller + UI panel + MCP coalesce onto one computation and one `git fetch origin`. Coalescing on the **loop** and not in `to_thread` is load-bearing: followers blocking in the default executor is the #2433 starvation class (6 threads on a 2-vCPU agent, shared with `ctx.terminate`), pinned by a test asserting exactly ONE `to_thread` call for five callers. `computed_at` admits the bounded staleness rather than denying it. **The runtime reaper was cut on measurement.** A 0-byte lock is the signature of a *live* `git add` for ~100 % of its life (29 s at 60 000 files, 155 s under a `clean` filter) and `st_mtime` is stamped at create and never advances, so neither size nor age separates abandoned from busy — and a wrong unlink is **permanent and worse than the wedge**: git renames by path, so a second git's in-flight file is promoted onto `.git/index` and the corrupting process exits rc=0, after which nothing in Trinity (not the boot reap, not `_reap_stale_git_litter`, not `git reset`) clears it. So the lock is **reported, never deleted** — two-point inode stability (same inode across ≥3 ticks / ≥15 min, clock-step-immune), gitdir-resolved to cover worktrees and submodules, `lstat`-only with **no** repo lock (holding one would make a status poll a new source of 409 `agent_busy`) and its own `except OSError` (it sits in the `try` whose tail is a 500, and a 500 makes the poller write nothing). Recovery stays where "nobody holds this lock" is provable for free — container boot — and the #1595 reap there now **announces** what it cleared, surfacing as `sync_state.last_lock_recovery` → `lock_recovery` → one backend WARNING. The rollout that delivers the fix therefore also clears the installed base. Backend: a `synchealth:leader` lease (SET NX, own-lease refresh, compare-and-delete release, **fail-open**) so `--workers 2` polls once, with the honest side effect named, tested and in the PR body — `upsert_sync_state` *increments* `consecutive_failures`, so one poller instead of two moves `sync_failing` from ~90 s to ~180 s. Plus `SYNC_HEALTH_POLL_INTERVAL_SECONDS` at its **unchanged** 60 s default, agent-supplied `lock_recovery`/`index_lock_stuck` rebuilt from coerced values behind a `[now − 2×interval, now]` clamp (the agent authors `sync-state.json` wholesale and `git_service` proxies it to UI and MCP unmodified), a 64 KiB gate on that read, and `remote_url` unconditionally `redact_url_userinfo`-d — the non-`github.com` branch was returning a fully tokenized URL in the response body (ent#615 owns the class; this is one line in a block this diff already moved). `test_1920`'s walk now covers the agent-server tree (Invariant #5: a guard that walks one of two trees is not a guard). AC4 is proven **deterministically and concurrently** by SIGSTOP-freezing a real `git status` child the instant the lock appears (30/30 on the base image's own git 2.39.5); the threaded witness beside it must reproduce a failure in its control arm or skip — it can never pass without having shown it can fail. Known residual, reported not fixed: `FETCH_HEAD.lock`/`packed-refs.lock` are covered by no reaper at all; a confirmed wedge is now visible but still needs a restart; and the flag costs ~29× steady-state on a stat-dirty tree (the writeback it suppresses is also the stat cache) with the obvious `update-index --refresh` mitigation measured **not** to work. | [git-sync-health.md](feature-flows/git-sync-health.md) |
| 2026-09-12 | #2726 | fix(models): **Claude Fable 5.1 is selectable, and the `(latest)` marker sits on it**. The catalog had not moved since Anthropic shipped `claude-fable-5-1`, so the model was absent from the operator picker and the admin fleet default and **422'd** by the server-validated public-channel override — not drift (#2086 made that impossible) but source-vs-reality staleness, the one control #2086 left human. One `ModelEntry` at index 1 + one codegen run; no consumer edited. `claude-fable-5` keeps its id, slot and both flags and loses only the marker (no `Legacy`, no reposition). The **Workspace composer stays excluded** on ent#403's written tier rule — reversing it is a `type-feature`, not a P1 bug. Agent-server `GET /api/model` now states the durable alias rule before the perishable list (`opus` was documented as Opus 4.8), and `PUT /api/model` accepts the `fable` alias the same router advertised. The derived parity guard ratifies whatever the catalog says, so the AC is pinned by explicit assertions: a per-id end-to-end test, a family-generic `at most one (latest) per tier` rule (meta-tested by planting violations), and a coverage assertion closing the hole where `test_per_model_flags` checked 9 of 10 entries. Deferred: `_format_model_name` renders the new id as "Claude Fable 5 1" (#2086 FR-7). | [model-selection.md](feature-flows/model-selection.md) |
| 2026-09-12 | #2572 | fix(subscriptions): **a credential-less fleet adopts an available subscription instead of sitting on API-Key auth with nothing behind it**. On an instance with no `ANTHROPIC_API_KEY`, registering a subscription — the one action the product tells the operator to take — assigned nobody: SUB-003 only moves agents that ALREADY have one (its precondition 2 is literally where the gap was written down) and `POST /api/subscriptions` assigned none. Three triggers now cover it, with no periodic sweep: registration, **instance-Anthropic-key deletion** (the canonical migration is register-then-delete, in which order the first trigger correctly adopts nobody; BOTH clear routes are hooked, because `db.delete_setting` has no delete-side twin of ent#435's write sink guard), and agent creation, which #74 already did. The predicate is *no usable credential*: no key resolvable at instance level — through `get_anthropic_api_key()`, **env fallback included**, never the DB-only `has_secret_setting()` — no subscription, `use_platform_api_key` true, a runtime label that is present AND Claude (absence is not evidence; `trinity-system` carries none), not ephemeral. An unreadable Docker adopts nobody. The decide phase is awaited so `GET /api/subscriptions` is right on the panel's immediate refetch; the container apply is backgrounded, re-verified under the #799 lock, and skips `trinity-system` (#1816) and ghosts (whose workspace a recreate destroys). Adoption is audited, and so — finally — is manual assign/clear. No schema change, no frontend change. | [subscription-management.md](feature-flows/subscription-management.md) |
| 2026-09-11 | ent#581 | feat(onboarding): **one blocking first-run overlay replaces the inline card ladder**. The hardening guide, finish-setup card, front desk and ent#52 wizard become steps (`secure` · `email` · `claude` · `keys` · `agent` · `sharing`) of one teleported two-pane overlay that never reflows the Dashboard; `ActivationChecklist` stays inline. Steps are a registry with `eligible` × `pending` predicates in the pure `firstRunSteps.js`: completion is derived (only close + per-step skips persist, per browser), `claude` is the one blocking step, ride-along `keys` never opens it on its own, an established fleet never sees it, and a failed flags read never reads as "Claude is not configured". `?onboarding=1` and Settings → General → Re-run setup reopen it over every eligible step. `TrinityMark.vue` is the one copy of the logo, shared with `/setup` | [onboarding-wizard.md](feature-flows/onboarding-wizard.md) |
| 2026-09-11 | #2638 | fix(subscriptions): **a rate-limited turn completes on another subscription** — the 2h skip-list is overridable per candidate on positive headroom evidence (`recovery_verdict`), the switch can happen before the first dispatch, the platform API key is a last resort behind the new `subscription_api_key_fallback` setting (GET/PUT `/settings/api-key-fallback`, toggle in Settings → Subscriptions), a 429 now classifies as `BILLING`, and every terminal `TaskExecutionResult` carries `subscription_switch` so the Workspace answers `auth_switched`/retryable instead of "not retryable" | [subscription-auto-switch.md](feature-flows/subscription-auto-switch.md) |
| 2026-09-11 | #2703 | fix(skills): **assigning a library skill delivers it, and every playbook list follows** — the `POST`/`PUT` assign paths call `skill_service.deliver_assigned` (start-path injection, bounded at 20 s, busy retried once, under-lock re-read) and return an honest `delivery` block; every listing writer fires the identifiers-only `agent_skills_changed` WS trigger and the Skills tab, Playbooks tab, `/` popup and Workspace briefing refetch; MCP passes `delivery` through | [skill-injection.md](feature-flows/skill-injection.md), [skill-assignment.md](feature-flows/skill-assignment.md) |
| 2026-09-11 | #2670 | fix(mcp): **`fan_out` answers with a `fan_out_id`, and a batch can be polled** — route three of the gateway-timeout receipt: `client.ts::fanOut()` is bounded and on abort recovers a receipt carrying the server-minted `fan_out_id`; new `GET /api/agents/{name}/fan-out/{fan_out_id}` (`db.get_fan_out_executions` + `build_fan_out_batch_status`) and MCP `get_fan_out_result` rebuild the batch from the stamped `schedule_executions` rows | [fan-out.md](feature-flows/fan-out.md), [mcp-orchestration.md](feature-flows/mcp-orchestration.md) |
| 2026-09-11 | #2571 | fix(telemetry): **the send log and the last-shared stamp record where a share went, and the receiver sentence is decided from that record**. Each attempt carried time, outcome, status and payload but not its destination, and Settings → Usage sharing decided "404 at the default address" vs "your receiver answered 404" by comparing the CURRENT `TELEMETRY_SHARING_URL` to the default at read time, beside the current address — so an operator who tested against a local sink, got a 200 and restored the default read "the receiving service acknowledged the last send" next to the production address. Now every entry carries the origin it was posted to (scheme + host + port after credential stripping; never path, query or userinfo; total over every URL shape), the 2xx stamp records the origin that acknowledged it (`telemetry_sharing_last_shared_host`, written before the date), the hint is decided from the entry, and the status carries `receiver_host` / `configured_host` / `receiver_mismatch` so the panel names the host that answered, says plainly when the newest send went elsewhere than the configured address and what happens next, and shows "to <host>" per row and "Last delivered <date> to <host>". Entries and stamps written earlier read as "an unknown receiver" and never as a mismatch; `share_url` is scrubbed. No migration, no new setting. Deferred: a destination change starting a new delivery episode, and the benchmark read using the recorded origin. | [telemetry-sharing.md](feature-flows/telemetry-sharing.md) |
| 2026-09-11 | #2705 | fix(frontend-e2e): **the nightly's red had two causes, neither the one first blamed**. `workspace-model-choice.spec.js` read Send's box, then each button's, then clicked at remembered coordinates; since #2676 the rail column enters through a 300 ms width transition once the roster arrives, so the reads straddled it and the Send-skip fired on Send itself (3 of 3 attempts in some stacks). Now every box comes from ONE `page.evaluate` under `reducedMotion: 'reduce'`, and the shell click is an element action. Separately, Vite's scanner tree-shakes before it records imports, so `CanvasDiagram.vue`'s lazy `import('mermaid')` in an unexported helper was never pre-bundled and the first served page that included it full-reloaded every open tab (the dedupe spec's flake). `optimizeDeps.include` now lists every dynamic bare import, enforced by `scripts/scan-dynamic-imports.mjs` + `tests/unit/optimizeDepsIncludeGuard.spec.js` against the resolved config. Follow-ups: #2710 (compatibility double fetch), #2711 (rail load-time shift vs contract rule 4), #2712 (e2e on the prod image) | [workspace-model-choice.md](feature-flows/workspace-model-choice.md) |
| 2026-09-10 | #2694 | fix(workspace): **a voice call sits where it happened, and the agent's next typed turn knows what was said**. The thread read counted ROWS (newest 100) and a 30-minute call is ~180, so one call pushed every typed turn before it off the screen and the call block rendered as the head of the thread; the window is now counted in typed turns with the calls among them riding along under a row ceiling that reports `truncated`, the reply poll reads `?limit=N` and finds the reply by identity, a resumed turn is prefixed with the spoken rows since the agent's last typed reply (one 24k-char budget, every cut named, the call's label as a platform marker), and a call and a typed turn are mutually exclusive (409 both ways, after the uniform 404). | [workspace-voice-conversation.md](feature-flows/workspace-voice-conversation.md) |
| 2026-09-09 | #2662 | fix(workspace): **one composer shell — the field on top, the controls inside it, the model picker beside Send**. ent#403 parked the picker on its own row above the composer because the single-row layout had 34px left to give it; the cause was that every 44px button came out of the growing field's width (143px of a 351px form at 375px). Stacking the field over a control row inside ONE bordered box gives the field the full width at every size and makes the **picker** the element that yields. The border, fill and ring move onto the shell, scoped `has-[textarea:focus]` and never `focus-within` (which lit the whole box on a button tab and double-ringed the picker) — so the shell must also put the caret in on a click of its own padding band, and must shed its chrome for a voice call, being the parent of both inert regions and the holder of the one control that stays live. The picker becomes `BaseSelect variant="ghost"` — a second recipe on the primitive (borderless, content-width, 44px, ground-only hover), entered in the design system rather than hand-rolled in chat chrome. Cascade trap recorded: two Tailwind utilities of the same shape disagree on which of an equal-specificity pair wins, and a `dark:` variant hides the fallout in light mode. The picker's chevron also now points up while it is open, joining the idiom the app's nine other dropdowns already follow — selects were excluded from it only because a native picker reports no open state, and `:open` (Baseline 2026-05) is the first hook that does; scoped to the `ghost` recipe so the Settings-form `field` variant, which this issue does not own, is unchanged. | [workspace-model-choice.md](feature-flows/workspace-model-choice.md) |
| 2026-09-09 | ent#545 | fix(telemetry): **the activation-funnel read no longer mints the install id on a GET**. `GET /api/enterprise/telemetry/funnel` reached the OSS `get_or_create_installation_id`, so on an install whose id was never minted the first admin open of Settings → Activation created the durable identity row from a read path — the `get_or_create_*`-on-a-read class (ledger 2026-08-05), third occurrence after #1987 and the ent#190 benchmark read. OSS gains the non-minting twin `get_installation_id` beside the writer; the enterprise read uses it and `installation_id` goes nullable on the wire; the panel footer renders three honest states through the pure `funnelFormat.js` ("No install id yet — one is minted when this instance opts in to security & product updates" for the backend's explicit `null`; "unavailable" for a value that is merely absent or malformed). In the same module the writers' mint becomes a write-once claim (`insert_setting_if_absent`), closing the SELECT-then-upsert race #1987 had recorded as pre-existing. Enterprise code and its never-mint suite land via the submodule pointer. | [telemetry-sharing.md](feature-flows/telemetry-sharing.md) |
| 2026-09-09 | #2661 | fix(mcp): **the gateway-timeout receipt now covers the sync `/task` route** — `chat_with_agent(parallel=true, async=false)` held the fetch for `timeout_seconds + 60` (up to 7260s) while the MCP gateway killed the call at 30-60s, so the caller saw a bare `fetch failed`, got no `execution_id`, and re-sent (fleet incident 2026-09-08: every duplicate in a three-hop cascade). The #914 matcher could **not** be mirrored: `/chat` is queue-serialised so newest-wins was near-unambiguous, but `/task` runs N concurrently and every filter (`triggered_by`, `source_mcp_key_id`, window) is identical across one caller's rows — it would have handed caller A caller B's `execution_id`, i.e. silent wrong data replacing a loud error. Attribution is now **provable or absent**: match the call's own `message`, read the `execution_id` out of an idempotency 409 instead of throwing it away, and return **nothing when >1 candidate survives**. Three latent defects in the shipped #914 route fixed alongside: the recency window was a fixed 30s while the abort is `MCP_CHAT_TIMEOUT_MS`, so raising that documented knob to ≥30s was a **silent kill-switch** for every receipt on both routes (now derived, `timeout + 10s`); the recovery lookup ran through the unbounded `_fetch` with a 401-reauth retry, spending the very gateway budget the abort was protecting (now `MCP_RECOVERY_TIMEOUT_MS`, no retry); and the trigger allowlist had to become **per call site** — widening the shared constant with `self_task` would have let a `/chat` abort attribute a concurrently-running parallel self-task. Backend counterpart: a failed sync `/task` left its idempotency claim `in_flight` for the full 24h TTL, so a legitimate retry answered 409 for a day against a task dead for minutes, and **rewording was the only way through** — which derives a new key and dispatches a real duplicate, selecting for the exact behaviour being fixed. Also ships `mcp-server-test.yml`: 341 tests across 28 files gated nothing, and `npm run build` (this package's only typecheck) first ran when `deploy-dev` built the image *on dev*. `fan_out` is route three and stays open — it needs a `fan_out_id` receipt and no polling surface resolves one | [mcp-orchestration.md](feature-flows/mcp-orchestration.md) |
| 2026-09-09 | #2618 | fix(telemetry): **the sharing heartbeat's cadence is anchored on the persisted last-share stamp, not on process age**. The loop used to sleep the whole 24h interval from process start and then send unconditionally, so any install that restarted inside every window (a nightly reboot, a daily update, a dev box under `--reload`) shared its consent-time backfill once and never again while Settings reported sharing as on. Now it wakes every 10 min (+ ≤10 min jitter, sleep-first so boot is never a burst) and sends when `telemetry_sharing_last_shared_at` is empty, unparseable, in the future, or older than the interval. The Redis tick marker keeps its cadence-keyed TTL, is a fresh lock per claim, and is released only when the receiver did not acknowledge (also on a cancellation mid-send; the heartbeat is now stopped in lifespan shutdown); `share_now` reports an acknowledged send as True even if the local stamp write fails; after five consecutive failures attempts fall to one per half-interval, measured from the persisted send log. No new setting. The consent card's "retried daily" became "retried automatically". | [telemetry-sharing.md](feature-flows/telemetry-sharing.md) |
| 2026-09-08 | #2578 | fix(ci): **Deploy to Dev survives an enterprise pointer bump, and a stale enterprise tree fails the deploy instead of warning**. The VM's superproject fetch ran with git's default on-demand submodule recursion, so every commit that moved the `src/backend/enterprise` gitlink fetched the submodule over its stored SSH URL before the PAT transport block existed, died at host-key verification, and stopped the script under `set -e` — self-healing on the next push because the refs advance before the recursion runs, which is why it never had a ticket. `--no-recurse-submodules` on the block's fetch, checkout and pull (the flag beats every config source, `.claude`'s `.gitmodules` on-demand included); the submodule block is the only sync path, and its failure now fails the run after the health check with a classified cause and no escape hatch. Guarded by a whole-script tokenised scan of every fetch/pull/checkout plus an 11-way mutation meta-test, and by a hermetic real-git test that runs the workflow's own Pull block against a bump under both `submodule.recurse` settings with a fake `ssh` that logs every dial (asserted empty). | [ENTERPRISE.md](../ENTERPRISE.md) |
| 2026-09-08 | ent#547 + #2580 | feat+fix(workspace): **the compact header — Info as a rail tab, one paperclip, voice at the composer**. The band loses its chart legend and its 7d/14d/30d selector (window fixed at 7 days; series identity is the hover tooltip, which already names every bucket with its swatch and count): **99px → 56px measured in Chromium at 1440px**, against the ≤60% target. The load-bearing fact is that once the legend is gone **the chart stops driving the band's height and the stats strip sets it** — a stat block is 39px, so the chart column is budgeted at exactly one, and the legend was the real cost all along (`flex-col`, ~13px per bucket → ~133px at nine). Within that budget the chart keeps its title OR its x-axis, not both; the title stays (board A3) and the axis goes. **Agent details becomes the rail's Info tab**, reversing the 2026-09-05 "never a rail tab" ruling — whose two objections are ANSWERED, not dropped: `RAIL_DOORS.SOLO_AGENT` (`=== 1`, never `> 0`) is the participant scoping, and collapsing the rail is the dismissal. Not `PLATFORM`: the header button carried no gate, so a platform door would silently remove a panel external clients have today (#2128). Info **does not group by agent in a room** — a recorded deviation from the AC, because the reports store is a singleton keyed to one agent (`loadAgentReports` → `resetAgentReports` bumps a generation and invalidates siblings), so N panels leave N−1 in a **permanent loading skeleton**. It is the registry's first **static** tab (`RAIL_SIGNAL_NONE`, `empty: null` — declared absences, since a borrowed `updated` would light no dot while documenting a rule that does not exist). One paperclip (the header's opened Files with the *attach* glyph); the composer row is voice-call · attach · dictate · send at 44px, and **the call toggle sits OUTSIDE the composer's `pointer-events-none` region** — it is a TOGGLE, so inside it would render pressed and refuse the click that ENDS the call, the dead affordance the issue's own AC forbids, manufactured by its own move (the wrapper is a real flex row, not `display: contents`, which generates no box and would drop the dimming while `pointer-events` still inherited). **#2580**: the band's remount is fixed at its SOURCE rather than by moving it — the operator ruled a band "under the header" and the header lives inside the keyed conversation, so hoisting renders an agent's numbers above its name (built, measured, reverted); instead `usePortalAgentPage` seeds from cache **during setup** (not `onMounted`, which runs after the first paint — the reason a warm remount still flashed) and skips the refetch inside `PAGE_FRESH_MS`, the pure `shouldRefetchPage` failing **stale** on an unusable timestamp. Verified live: 6 same-agent tab switches → 0 `/page` requests, 0 placeholder frames, with a control proving the probe could see one. Sidebar dates get a reserved right-aligned column (an ordering and width problem, not an alignment one — three siblings sat to their right). `PortalRating` becomes a SINGLE root owning no margin, which is what stops its comment box opening as a flex sibling *beside* the thumbs, with the margin moved to the `PortalDeliverables` call site that mounts it outside any row. And **a reply is rateable as it lands**: `awaitPersistedReply` already read the persisted row and was discarding its `id` — one shared mapper now feeds all three sites, and the synchronous fallback returns `message_id`, DECLARED on `PortalChatResponse` because the response model strips undeclared keys in silence. | [workspace-agents-at-the-centre.md](feature-flows/workspace-agents-at-the-centre.md), [workspace-rail.md](feature-flows/workspace-rail.md), [workspace-ratings.md](feature-flows/workspace-ratings.md), [workspace-voice-conversation.md](feature-flows/workspace-voice-conversation.md) |
| 2026-09-07 | #2582 + ent#548 | fix+feat(workspace): **the Files tab — uploads appear at once, Download saves, and a file can be previewed or removed**. Three defects and two asks from an operator's test of `dev`. (1) A composer upload never told the rail's feed store, so "Files you sent" was stale until a turn ended — fixed at the ONE store funnel `uploadDocument` (three callers: the rail, the conversation, the room), so the conversation file the whole delivery sequence is serialized to protect is never touched. The signal is a **pending-agent SET** drained with leading+trailing coalescing and ordered by a **per-agent inbox epoch** that `refresh()` snapshots before its awaits, because the obvious scalar re-breaks the same defect twice: a sequential multi-file batch joins a listing snapshotted before the later files landed, and a room's fan-out mutates one scalar per agent inside a single Vue flush window. (2) Own uploads had no control at all — a client upload has no DB row, so reading one back is `extract_from_agent`, deleting one is `rm -f --`, and the MIME must be guessed (which also fixed a live bug: the row's `FileIcon` read a field the response model had always stripped). (3) `GET/HEAD /api/files/{id}` gain a **ONE-WAY `?download=1`** that may only force `attachment` — never `inline`, which is ent#461's XSS allowlist — parsed tolerantly rather than as a `bool`, because a `bool` 422s `?download=` on the public link opened from Telegram/iOS. ent#548 adds a preview modal (images via `<img>` only, never inline `<svg>`; markdown through the one sanitiser; text capped at 256 KB, fetched whole and sliced because CORS `allow_headers` omits `Range`; never a blank modal, a failed fetch included) with next/previous over the *previewable* subset and capture-phase Escape/arrows so Escape cannot cancel an in-flight turn, and a delete whose affordance is session-type dependent — a non-owner admin is a viewer, and so is an owner on a portal token. "Unshare" needed new per-viewer storage (`portal_file_dismissals`, both tracks, Alembic 0058, `AgentRef`-registered), which deliberately does NOT validate the `file_id` — an existence oracle — and caps rows instead, the fork `set_chat_star` already resolved. A preview no longer inflates the owner's `download_count`: a ranged prefix read is audited `ranged_prefix: true` without bumping it. OSS-core by the standing Workspace ruling, ungated. | [workspace-rail.md](feature-flows/workspace-rail.md), [file-sharing-outbound.md](feature-flows/file-sharing-outbound.md), [chat-turn-cancellation.md](feature-flows/chat-turn-cancellation.md) |
| 2026-09-07 | #2579 | fix(workspace): **the chat tab strip — a New chat you can see, a Main that is there, titles that are titles, and tabs that hold their width**. Four defects from one operator test, and none of them was the sort rule. (1) An unsaved chat now draws a **provisional** tab (`New chat`, `thread: null`, inserted after Main — the slot the real row takes, so adoption causes no jump), a recorded **reversal** of the 2026-09-06 ruling for the STRIP only; keyed off explicit intent (`startingNewChat` ORed with the conversation's `bornHere`, raised in the ONE `adoptSession()` seam all three adoption sites share), never off "the active id is not in the list" — which would label a cold deep link. (2) New chat **focuses the composer** from `onMounted`, because the press bumps `convGen` and remounts the conversation. (3) **Main is ensured** by `ensureMainListed` through the per-agent read that already mints it — a GET that INSERTS, deduped in flight, capped at two attempts (`fetchAllSessions` never rejects, so a resolved entry over a miss is permanent), both maps cleared at sign-out (that handler resets in place, so client B would inherit client A's promises); `landOnAgent`'s repair branch had never once run, destructuring `{ sessions }` off an **array**. (4) **Fixed-width tabs** via an opt-in `OverflowTabs.fixedWidth` (`FIXED_TAB_WIDTH = 'w-40'`, Main included, label clamped, full text on hover and in the menu) — an **amendment to the design contract's "never truncate"**, which governs the SET, not a label in a strip of unbounded model text; `shrink-0` + `overflow-hidden` are load-bearing against the pre-measure squeeze, and the width class goes in both rows or the mirror overflows one tab too late. Root cause of (4)'s sibling: the **title spawn moves to run concurrently with the turn** (after `_persist_user_turn`, `reply=""`, a separate `_TITLE_PROMPT_OPENER` rather than an empty `<assistant_reply>` block) — so a title is generated from the opening message alone and a FAILED turn still titles its thread, both deliberate and both pinned; the client keeps a bounded settle re-read as the belt, aborting on `sessionsFailed` and asking the health record rather than deciding. The existing `titleGenerationNotice` rides `PortalConversation`'s new `#notice` slot for platform admins only, over `portalHttp` (never `@/api`, which hard-navigates to `/login` on a 401 under `/workspace`). | [workspace-chat-tabs-and-titles.md](feature-flows/workspace-chat-tabs-and-titles.md) |
| 2026-09-07 | #2541 | fix(agents): **agent containers are born `unless-stopped`**. Docker's default is `no`, so every agent Trinity created stayed `Exited` after a host reboot until somebody started it by hand — `trinity-system` was the only exception, and a 2026-09-04 power-off left 8 of 19 agents (the fleet conductor among them) dead ~42h while the 11 a manual `docker update` had patched came back. `AGENT_RESTART_POLICY` joins `AGENT_TMPFS_MOUNT`/`AGENT_LOG_CONFIG` in `capabilities.py` and is imported by all three create sites; **not** operator-tunable, unlike both siblings, because it is a safety floor and `always` must stay unreachable. The shared recreate tail bakes it **unconditionally**, retiring #1816's carry-forward: faithful carry-forward carries `no` forward forever, and the tail's other caller (the #1559 recovery rebuild) passed nothing at all — a third broken path nothing had noticed. `unless-stopped`, never `always`, is a security property: Trinity stops via `container.stop()`, which sets Docker's manual-stop flag, so a quarantined agent stays down. Guard `test_2541_restart_policy_parity.py` pins the VALUE by name (presence alone would admit `{"Name": "always"}`), adds docker-py's third creation form `<x>.containers.run`, and asserts the REVERSE direction — a non-detached `remove=True` helper WITH a policy is accepted by the daemon and leaks a forever-restarting orphan. Consequences shipped with the cause: `restarting` normalizes to `stopped` on both `docker_service` normalizers (verbatim it matched neither frontend filter), two dormant `RestartCount` alert paths go live fleet-wide, and `docker compose down` now leaves every agent in a dockerd backoff loop. Plus the compose half — `backend`/`frontend`/`redis` had no `restart:` in the **base** file, the one the README quickstart uses. Existing containers adopt on recreate: [AGENT_RESTART_POLICY_2026-09.md](../migrations/AGENT_RESTART_POLICY_2026-09.md). | [container-capabilities.md](feature-flows/container-capabilities.md), [agent-lifecycle.md](feature-flows/agent-lifecycle.md) |
| 2026-09-07 | #2529 | fix(git-sync): **the canonical `.gitignore` stops reversing the agent's own negations, and a Push says what it untracked**. Both writers of an agent's `.gitignore` APPENDED and git is last-match-wins, so the canonical block landed BELOW every `!negation` the agent had written and silently reversed it — after which the per-Push `git rm --cached` sweep untracked exactly the files those negations were protecting, inside an unrelated sync commit that named none of them (`47efd80`; corbin repeated it and left the comment *"Negation must stay LAST in this file."*). `.env.example` was a casualty, and compat check **F-004** requires it, so an agent shipping one lost it on its first Push and then failed its own compatibility contract. The merge is now a **normalize-and-rebuild into TWO managed regions** — `[defaults][the agent's rules, original order][protected floor]` — because one block provably cannot carry both defaults the agent may override and guarantees it may not: hoisting a single block would turn every currently-INERT `!.env` in the fleet live in one unattended 15-minute cycle (measured), and would let a user `*.sh` beat `!.trinity/setup.sh` (ent#76/#1704, failing quietly since the rm-cached pathspec still exempts it). AC-1 is enforced by **git itself** — with the block above the agent's rules, `git ls-files -ci` no longer lists a path an *effective* negation covers — so #462's and #1596's purpose (untracking long-committed `node_modules/`) survives untouched. `_GITIGNORE_PROTECTED` is a filter over `_GITIGNORE_PATTERNS`, never a second list. Idempotent by CONTENT (`cmp` gate), so the auto-sync loop has nothing to re-commit, and the 14 bundled templates are regenerated as the merge's own **fixed point** to keep #1908's byte-identity that #953 depends on. `_GITIGNORE_PROTECTED` membership is decided against `services/credential_paths.py` — the platform's own answer to *"is this secret material"* — not against the constant's comment headings, which is how `.ssh/` was left in the OVERRIDABLE region on the first pass: an agent carrying `!.ssh` with no `.ssh/` line had `.ssh/id_rsa` ignored under the old append-only merge and NOT ignored under the hoist, and the unattended `git add -A` then committed a private key (found in review, reproduced, pinned). The Push now returns `removed_paths` / `unignored_paths` / `shadowed_negations` on **all four** returns — the index mutation precedes the HTTP call, so a 409 is as obliged to report it as a 200 — across the API response, the `git_sync` MCP result, the toast, the commit message (best-effort: `git rm --cached` only *stages*) and a durable `gitignore_untracked` **operator-queue** entry, which is the surface that outlives an unattended cycle — every one of them gated on `changed_tracking` (removed OR unignored), because the addition half is already in the remote's history and may need a credential rotated rather than a file restored. Shell hardening, each reproduced first: the strip `grep` is a real command with an `rc<=1` check (a failure inside a process substitution is invisible and the `mv` then destroys the user's rules), `LC_ALL=C grep -a` (a NUL byte otherwise drops the whole user region while exiting 0), and a bare+CR strip list. Known residual, reported not fixed: 23 canonical patterns are dir-form and git never descends into an excluded directory, so a negation beneath one is inert at any position — contents-form conversion was costed and rejected. CI direction fixed: `test_doc_and_constant_in_sync` is now set EQUALITY (asserting `constant ⊆ doc` is exactly how the guide carried `!.env.example` for months while the constant did not) and `ALLOWED_NON_CANONICAL` shrinks to empty. | [github-sync.md](feature-flows/github-sync.md), [git-sync-health.md](feature-flows/git-sync-health.md) |
| 2026-09-07 | #2533 | fix(ci): **the pre-merge Alembic head check stops being stale-by-construction**. `schema-parity`'s single-head guard was never wrong — it runs unconditionally and `actions/checkout` on `pull_request` already resolves `refs/pull/N/merge` — it was **stale**: GitHub recomputes that ref when the base advances but does NOT re-trigger workflows, so #2526 read *mergeable, all checks green* against a base that had gained a revision with the same parent, and `alembic upgrade head` (singular, target resolved BEFORE anything applies) would have applied **zero** revisions on PostgreSQL. `alembic-head-watch.yml` re-runs `check_alembic_heads.py` **unmodified** over an in-memory `git merge-tree` of every open migration PR against the live `dev` tip on each push to `dev` (cron is a dropped-run backstop only, and fires solely from `main`), publishing an **advisory** commit status + one sticky comment; `conflict` and `unknown` publish nothing rather than a false all-clear, and a forked `dev` suppresses every PR verdict rather than smearing blame (#1941's class). No new flow doc — the mechanism is one stdlib script plus a workflow, owned by Invariant #3 and `requirements/infrastructure.md` §8.11; this row records where the guard's freshness now comes from. | [database-migration-runner.md](feature-flows/database-migration-runner.md) |
| 2026-09-07 | ent#500 | feat(assignments): **who fills which role, and which agent serves which human** — the edition-agnostic half. Four optional `ExecutionContext` fields (`primary_user_display` / `role_id` / `stakeholders` / `proactive_consent`) auto-filled beside `collaborators` from the new OSS seam `services/assignment_provider.py`, with **zero call-site changes** and byte-identical output on a build that registers no provider. The seam is deliberately SYNC (`compose_system_prompt` is a plain `def` on the async request path) and **owns its own failure handling**, because that function has no exception handler: an escape costs all three callers the context block and costs one of them the platform prompt. It also validates the answer SHAPE, which `try`/`except` cannot do — a `str` where a list was promised iterates into single characters and renders the wrong prompt without raising. Two disclosure gates, both required: the rendered identity is a display NAME (the answer contract has no email-shaped key) and `triggered_by` is forwarded so the provider suppresses on an outside audience as an ALLOW-list, fail-closed on a label nobody has thought of yet — a Workspace/portal turn is labelled `public`, asserted over the source so a new outside surface cannot inherit disclosure. Consent is **reported, not granted**: the line states it explicitly, because a bare name reads as permission and permission lives on `agent_sharing.allow_proactive`. MCP `get_agent_assignments` is read-only by construction (creating an assignment is a GRANT the write path refuses to every non-interactive principal). Found and fixed on the way: `cascade_rename` never swept `EXTRA_AGENT_REFS` although `register_agent_owned_table`'s docstring said it did. **Not a live disclosure** — the one production caller (`rename_agent`) carried its own copy, which is what made it survivable and what made it dangerous: the behaviour lived in the caller, so the shared contract was a lie a cross-repo module author would believe and a second caller would silently drop. Consolidated (loop moved in, duplicate deleted), with the caller level tested too. | [role-assignments.md](feature-flows/role-assignments.md), [execution-context-injection.md](feature-flows/execution-context-injection.md) |

## Documented Flows

### Core Agent Features

| Flow | Document | Description |
|------|----------|-------------|
| Agent Lifecycle | [agent-lifecycle.md](feature-flows/agent-lifecycle.md) | Create, start, stop, delete Docker containers |
| Ephemeral Agents | [ephemeral-agents.md](feature-flows/ephemeral-agents.md) | Budgeted "ghost" agents: hard-discard lifecycle, budget gates, GC, ghost-key fence, spawn provenance + parent control (trinity-enterprise#69) |
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
| Agent Self-Reminders | [agent-self-reminders.md](feature-flows/agent-self-reminders.md) | Durable one-shot deferred self-trigger — the scheduler arms a `DateTrigger` per pending reminder + fires a normal execution of the same agent (`triggered_by="reminder"`); self-scoped `set/list/cancel_reminder` (#1296) |
| Dispatch Circuit Breaker | [dispatch-circuit-breaker.md](feature-flows/dispatch-circuit-breaker.md) | Per-agent producer-side dispatch breaker (RELIABILITY-007, #526) |
| Execution Context Injection | [execution-context-injection.md](feature-flows/execution-context-injection.md) | Inject prior-execution context into a turn (#171) |
| Schedule Pre-Check | [scheduler-pre-check.md](feature-flows/scheduler-pre-check.md) | Conditional template-supplied pre-check hook before a cron run (SCHED-COND-001, #454) |
| Agent Compatibility Validation | [agent-compatibility-validation.md](feature-flows/agent-compatibility-validation.md) | Deterministic + AI-assisted check that a deployed agent follows Trinity conventions, with fixes (#668) |
| Agent Runtime Data Volumes | [agent-data-volumes.md](feature-flows/agent-data-volumes.md) | Declared `data_paths` on the durable home volume + snapshot/export (#1169) |
| Agent Plugin Manifest | [agent-plugin-manifest.md](feature-flows/agent-plugin-manifest.md) | Declared, committed, self-healing Claude Code marketplace plugin list (#1704) |
| Gemini Runtime | [gemini-runtime.md](feature-flows/gemini-runtime.md) | Gemini CLI as an alternative agent runtime |
| Schedule → Workspace Delivery | [schedule-workspace-delivery.md](feature-flows/schedule-workspace-delivery.md) | A schedule names one Workspace user; its output lands as a brief in their Main chat (ent#498) |

### Dashboard & Monitoring

| Flow | Document | Description |
|------|----------|-------------|
| Agent Network (Dashboard) | [agent-network.md](feature-flows/agent-network.md) | Real-time visual graph at `/` |
| Dashboard Timeline View | [dashboard-timeline-view.md](feature-flows/dashboard-timeline-view.md) | Graph/Timeline mode toggle with execution boxes |
| Dashboard Grid View | [dashboard-grid-view.md](feature-flows/dashboard-grid-view.md) | Magnetic tile canvas dashboard mode (trinity-enterprise#47) |
| Dashboard List View | [dashboard-list-view.md](feature-flows/dashboard-list-view.md) | Agents-page row list as the third dashboard mode (trinity-enterprise#260) |
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
| Activity Stream Collaboration Tracking | [activity-stream-collaboration-tracking.md](feature-flows/activity-stream-collaboration-tracking.md) | Agent-to-agent collaborations from MCP call → DB → WebSocket → Dashboard replay |
| Agent Custom Metrics | [agent-custom-metrics.md](feature-flows/agent-custom-metrics.md) | Agent-defined KPIs in `template.yaml` rendered in the UI |
| Agent Reports | [agent-reports.md](feature-flows/agent-reports.md) | Agent-published structured reports; thin WS trigger, refetch via access-controlled REST (#918) |
| Execution List Page | [execution-list-page.md](feature-flows/execution-list-page.md) | `/executions` — cross-process execution list with status/process filters |
| Status-as-Projection | [status-as-projection.md](feature-flows/status-as-projection.md) | "Is execution X running?" is a projection over three stores, resolved in one place (#1082) |

### Agent Detail UI

| Flow | Document | Description |
|------|----------|-------------|
| Overview Tab | [agent-overview-dashboard.md](feature-flows/agent-overview-dashboard.md) | Default landing tab — multi-day trend charts + analytics endpoint (#1107) |
| Tab Overflow (More ▾) | [agent-detail-tab-overflow.md](feature-flows/agent-detail-tab-overflow.md) | Reusable `OverflowTabs.vue` — tabs collapse into a "More" dropdown instead of horizontal scroll (#1114) |
| Tasks Tab | [tasks-tab.md](feature-flows/tasks-tab.md) | Task execution UI with history |
| Playbooks Tab | [playbooks-tab.md](feature-flows/playbooks-tab.md) | Invoke agent skills from UI (PLAYBOOK-001) |
| Authenticated Chat Tab | [authenticated-chat-tab.md](feature-flows/authenticated-chat-tab.md) | Simple chat UI with dynamic status labels (CHAT-001, THINK-001) |
| Playbook Autocomplete | [playbook-autocomplete.md](feature-flows/playbook-autocomplete.md) | Slash-command autocomplete for playbooks in chat input |
| Voice Chat | [voice-chat.md](feature-flows/voice-chat.md) | The real-time voice session (Gemini Live today): orb, tools, canvas verbs, session lifetime; **the Workspace is the only front door (#2559)** — Agent Detail offers a Talk door into it |
| Execution Log Viewer | [execution-log-viewer.md](feature-flows/execution-log-viewer.md) | Modal for viewing execution transcripts |
| Execution Detail Page | [execution-detail-page.md](feature-flows/execution-detail-page.md) | Dedicated page for execution details |
| Continue Execution as Chat | [continue-execution-as-chat.md](feature-flows/continue-execution-as-chat.md) | Resume executions as interactive chat (EXEC-023) |
| Agent Avatars | [agent-avatars.md](feature-flows/agent-avatars.md) | AI-generated avatars with reference images, emotion variants, and default generation (AVATAR-001/002/003) |
| Agent Info Display | [agent-info-display.md](feature-flows/agent-info-display.md) | Info tab: About leads; `template.yaml` metadata behind a collapsible "Technical details" (#1107) |
| Per-Agent File Manager | [file-browser.md](feature-flows/file-browser.md) | Two-panel file manager in Agent Detail Files tab |
| File Manager (Deprecated) | [file-manager.md](feature-flows/file-manager.md) | Former standalone `/files` page — replaced by per-agent Files tab |
| Brain Orb | [brain-orb.md](feature-flows/brain-orb.md) | Per-agent page rendering a Cornelius-class agent's live 3D knowledge-graph orb from data the agent produces in its container (ent#58) |

### Collaboration & Permissions

| Flow | Document | Description |
|------|----------|-------------|
| Agent-to-Agent Collaboration | [agent-to-agent-collaboration.md](feature-flows/agent-to-agent-collaboration.md) | Inter-agent communication via MCP |
| Agent Event Subscriptions | [agent-event-subscriptions.md](feature-flows/agent-event-subscriptions.md) | Lightweight pub/sub for inter-agent event pipelines |
| Agent Evaluations | [agent-evaluations.md](feature-flows/agent-evaluations.md) | Referee surface for the `quality` axis + the completion relabel; the graded agent cannot write its own grade (ent#206) |
| Task Completion Events | [task-completion-events.md](feature-flows/task-completion-events.md) | Backend-emitted `agent.task.completed`/`failed` at every CAS-won terminal (#1578) |
| Channel Completion Report-Back | [channel-completion-report.md](feature-flows/channel-completion-report.md) | Delegated/background terminals report into the originating Slack/Telegram chat — binding-agent consent + delivery, effect-guarded (ent#224/ent#265) |
| Role Assignments | [role-assignments.md](feature-flows/role-assignments.md) | Which human fills which business role for an agent, and which one it primarily serves — the OSS seam, the execution-context lines, and the audience gates that keep staff identities off outside-facing turns (ent#500) |
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
| First-Run Overlay | [onboarding-wizard.md](feature-flows/onboarding-wizard.md) | The blocking post-login setup sequence — step registry, derived completion, re-run (ent#581; formerly the ent#52 wizard) |
| MCP API Keys | [mcp-api-keys.md](feature-flows/mcp-api-keys.md) | API key management |
| Execution Origin Tracking | [AUDIT-001-execution-origin-tracking.md](feature-flows/AUDIT-001-execution-origin-tracking.md) | Track who triggered executions |
| Agent-Server Authentication | [agent-server-authentication.md](feature-flows/agent-server-authentication.md) | Per-agent inbound auth for the in-container agent server (`:8000`) — derived `X-Trinity-Agent-Token` (HMAC over `AGENT_AUTH_SECRET`) enforced by a pure-ASGI middleware on every HTTP/WS route (#1159) |
| 4-Tier Role Model | [role-model.md](feature-flows/role-model.md) | user < operator < creator < admin role hierarchy (ROLE-001) |
| Guided Credential Setup | [guided-credential-setup.md](feature-flows/guided-credential-setup.md) | Per-agent credential checklist: what an agent needs, what is set (bounded `docker exec` probe, names never values), and where to get each — owner-only + human-only (ent#127) |
| Runtime Secret-Scrub Seam | [runtime-secret-scrub.md](feature-flows/runtime-secret-scrub.md) | Generic OSS identity-scrub: a producer stages a secret value (fail-closed), the terminal persistence chokepoints scrub every staged value out of durable output (fail-open) before write — catches prefix-less values the pattern sanitizer misses; global 24h-TTL Redis store, behaviour-neutral when nothing staged (ent#279) |
| Audit Trail | [audit-trail.md](feature-flows/audit-trail.md) | Append-only platform audit log — who did what, when (SEC-001, #20) |

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
| MCP Connector | [mcp-connector.md](feature-flows/mcp-connector.md) | Per-agent MCP connector — expose playbooks as tools to an external AI client via a scoped key; OSS-core (ent#46 → #118) |
| Agent MCP Key | [agent-mcp-key.md](feature-flows/agent-mcp-key.md) | The agent's own `scope='agent'` key — container config-truth probe, start-time drift self-heal, owner-driven rotation (#1854) |
| A2A Inbound Server | [a2a-inbound-server.md](feature-flows/a2a-inbound-server.md) | Opt-in public Agent Card + JSON-RPC/SSE task endpoint so external orchestrators discover and task an agent; per-caller `messageId` dedup, rate-limited public route, allow-list seam (ent#157/#160) |
| A2A Outbound Calls | [a2a-outbound-call.md](feature-flows/a2a-outbound-call.md) | The calling half: a Trinity agent tasks an external A2A agent through an operator-registered endpoint (never a caller-supplied URL); call-time SSRF re-validation, connect-time IP pinning, same-origin card pin, `effect_guard` keyed on the conversation, default-OFF kill switch (#736) |
| Trinity CLI | [cli-tool.md](feature-flows/cli-tool.md) | Python Click CLI with multi-instance profiles, mirroring core MCP tools as shell commands |
| Trinity Connect | [trinity-connect.md](feature-flows/trinity-connect.md) | Local-remote agent sync via WebSocket |
| Write User Memory | [write-user-memory.md](feature-flows/write-user-memory.md) | Per-user memory write MCP tool (MEM-001, #888) |

### GitHub Integration

| Flow | Document | Description |
|------|----------|-------------|
| GitHub Sync | [github-sync.md](feature-flows/github-sync.md) | Source mode (pull-only) or Working Branch mode |
| GitHub Repo Initialization | [github-repo-initialization.md](feature-flows/github-repo-initialization.md) | Initialize GitHub sync for existing agents |
| Agent Repo Binding | [agent-repo-binding.md](feature-flows/agent-repo-binding.md) | Bind a live agent to a GitHub repo the user owns (ent#109) |
| GitHub Import Intents | [github-import-intents.md](feature-flows/github-import-intents.md) | fork / copy / clone create intents + inline compat check (ent#15) |
| Persistent-State Allowlist | [persistent-state-allowlist.md](feature-flows/persistent-state-allowlist.md) | `.trinity/persistent-state.yaml` primitive for reset-preserve-state (S4, #383) |
| Git Sync Health | [git-sync-health.md](feature-flows/git-sync-health.md) | Auto-sync heartbeat, dual ahead/behind, dashboard dot, `/api/fleet/sync-audit`, creation-time canonical `.gitignore` seed (#2069, readiness-gated) |

### Skills Management

| Flow | Document | Description |
|------|----------|-------------|
| Skills CRUD | [skills-crud.md](feature-flows/skills-crud.md) | Admin CRUD for platform skills |
| Skill Assignment | [skill-assignment.md](feature-flows/skill-assignment.md) | Owner assigns skills to agents |
| Skill Injection | [skill-injection.md](feature-flows/skill-injection.md) | Full-directory skill packages injected on agent start, on assign (#2703), by the fleet sweep and manual Sync; `agent_skills_changed` WS trigger |
| Skills on Agent Start | [skills-on-agent-start.md](feature-flows/skills-on-agent-start.md) | Detailed startup injection flow |
| MCP Skill Tools | [mcp-skill-tools.md](feature-flows/mcp-skill-tools.md) | 8 MCP tools for skill management |
| Skills Library Sync | [skills-library-sync.md](feature-flows/skills-library-sync.md) | GitHub repository sync |

### Notifications & Events

| Flow | Document | Description |
|------|----------|-------------|
| Agent Notifications | [agent-notifications.md](feature-flows/agent-notifications.md) | Agent-to-platform notifications (NOTIF-001) |
| Events Page UI | [events-page.md](feature-flows/events-page.md) | Consolidated into Operating Room Notifications tab |
| Operating Room | [operating-room.md](feature-flows/operating-room.md) | Unified operator command center: queue, notifications, resolved (OPS-001); async fire-and-park contract + derived request ids + lease-reaper poison-park items (#1402); agent-authored ingestion depth/rate/size caps + reserved-id guard + leader lock (#1632); per-(agent, type) budget for agent-influenceable platform emitters + caller-parity guard (#1677); per-branch bounds on the skills legacy-adoption alarm (#2744) |
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
| Template Registry | [template-registry.md](feature-flows/template-registry.md) | Remote `registry.yaml` as the runtime source for the GitHub half of the template catalog, with the bundled list as its fail-open floor (TMPL-002, trinity-enterprise#14) |
| SSH Access | [ssh-access.md](feature-flows/ssh-access.md) | Ephemeral SSH credentials |
| Subscription Management | [subscription-management.md](feature-flows/subscription-management.md) | Claude Max/Pro subscription tokens via env var (SUB-002) |
| Subscription Usage Tracking | [subscription-usage-tracking.md](feature-flows/subscription-usage-tracking.md) | Rolling 5h/7d token and cost usage per subscription (SUB-004) |
| Subscription Auto-Switch | [subscription-auto-switch.md](feature-flows/subscription-auto-switch.md) | SUB-003: switch an agent to another subscription on a rate-limit/auth refusal — hot-reload (#1089), headroom-ranked selection (#2409), the turn completes on the new one with the platform API key as last resort (#2638) |
| Subscription Headroom History | [subscription-headroom-history.md](feature-flows/subscription-headroom-history.md) | One durable row per headroom probe — "how close did we run to the 5h wall this week" (ent#433) |
| Usage Sharing (Tier-2 telemetry) | [telemetry-sharing.md](feature-flows/telemetry-sharing.md) | Opt-in anonymized aggregates: the Finish-setup consent ask, share id, enforced schema v2, outcome mix, send log, backfill-until-delivered (ent#12, ent#437); the gated benchmark card reads the live hosted service (ent#190); a restart-proof cadence anchored on the last-share stamp (#2618); the funnel read is pure and the footer honest about an unminted install id (ent#545); every send and the last-shared stamp record the origin they went to, and the receiver sentence is decided from that record (#2571) |
| First-Run Onboarding Wizard | [onboarding-wizard.md](feature-flows/onboarding-wizard.md) | Guided first-agent deploy for a fresh self-hosted instance (ent#52) |

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
| Automatic Database Backups | [database-backup.md](feature-flows/database-backup.md) | Daily 03:30 UTC in-process backup for SQLite (`Connection.backup()`) and PostgreSQL (`pg_dump -Fc`) under `/data/backups/`, boot pre-migration copy, inverted-fail-safe retention (`backup_retention_days` + fixed `MIN_KEEP=3` floor), durable status + edge/staleness operator alarms, exercised restore (#2216) |
| Prebuilt Images & Hosted Install | [hosted-install.md](feature-flows/hosted-install.md) | Five images published to GHCR on every `v*` tag (mutable/version tags gated on `push`, so a `workflow_dispatch` smoke build publishes `sha-<short>` only) + `start.sh --hosted` pull-and-retag install: no on-box builds, `TRINITY_IMAGE_TAG` pin read from `.env`, wholesale prod↔hosted compose parity guard, both-direction data-switch refusal, hosted-aware `stop.sh` (#2280). `start.sh --provision` is the one bare-VM provisioner (Packer bakery, 1-Click first boot, and the operator-side `trinity-do-create.sh` installer): pinned Caddy, a port-list-free `DOCKER-USER` firewall that also blocks the cloud metadata service, and on-demand TLS for a saved domain (#2380) |
| Install Provenance & Hardening Guide | [install-provenance.md](feature-flows/install-provenance.md) | Write-once `TRINITY_INSTALL_SOURCE` → `system_settings.install_source` gating the first-run overlay's two-stage `secure` step (domain, then Cloudflare Tunnel; ent#581) on marketplace and `do-script` installs only (`hardening_guide_eligible`); per-browser skip; Caddy's `/api/public/tls-allowed` exact-host `ask` gate makes the domain step a Settings field (#2380) |
| Database Migration Runner | [database-migration-runner.md](feature-flows/database-migration-runner.md) | Cross-process `flock` + atomic rename-swap rebuilds make the SQLite migration suite crash-safe and concurrency-safe; failed migration named in the `/health` 503 (#1160 / #1183) |
| WebSocket Event Bus | [websocket-event-bus.md](feature-flows/websocket-event-bus.md) | Redis Streams transport for `/ws` + `/ws/events` with reconnect replay, per-client eviction, `MAXLEN` trim (RELIABILITY-003 / #306) |

### Templates & Pages

| Flow | Document | Description |
|------|----------|-------------|
| Template Processing | [template-processing.md](feature-flows/template-processing.md) | GitHub and local template handling |
| Library Page | [library-page.md](feature-flows/library-page.md) | `/library` — agent templates + fleet skills browse; legacy `/templates` redirects (trinity-enterprise#263) |
| API Keys Page | [api-keys-page.md](feature-flows/api-keys-page.md) | `/api-keys` page UI flow |
| Agents Page UI | [agents-page-ui-improvements.md](feature-flows/agents-page-ui-improvements.md) | **Superseded** (trinity-enterprise#260) — page retired into the Dashboard List mode; see [dashboard-list-view.md](feature-flows/dashboard-list-view.md) |
| Alerts Page | [alerts-page.md](feature-flows/alerts-page.md) | Removed in #430 (process engine deletion; cost alerts were PE-only) |

### File Management

| Flow | Document | Description |
|------|----------|-------------|
| Web Chat File Upload | [web-chat-file-upload.md](feature-flows/web-chat-file-upload.md) | Drag-drop/picker for authenticated and public chat; shared upload_service (#364) |

### Chat & Sessions

| Flow | Document | Description |
|------|----------|-------------|
| Persistent Chat Tracking | [persistent-chat-tracking.md](feature-flows/persistent-chat-tracking.md) | Database-backed chat persistence |
| Session Tab | [session-tab.md](feature-flows/session-tab.md) | The `--resume` engine — each turn reattaches to the same Claude memory (SESSION_TAB_2026-04). Its Agent Detail surface retired in ent#358; the engine and endpoints are live |
| Workspace absorbs Session | [workspace-absorbs-session.md](feature-flows/workspace-absorbs-session.md) | ent#358 — one continuous-conversation surface, with the continuity parity that had to land before the other could be removed |
| Workspace sidebar IA | [workspace-sidebar-ia.md](feature-flows/workspace-sidebar-ia.md) | ent#359 — agents block on top, starred chats, per-agent unread badges; why the per-viewer state could not be a column on the chat row |
| Workspace agent page | [workspace-agent-page.md](feature-flows/workspace-agent-page.md) | ent#360 — an agent becomes a destination; what the page deliberately does NOT carry, and why that is enforced by projection rather than by template |
| Workspace ratings | [workspace-ratings.md](feature-flows/workspace-ratings.md) | ent#366 — thumbs on a message, Useful on a deliverable; why a user rating is a platform primitive and what the rated agent may read |
| Workspace deliverables | [workspace-deliverables.md](feature-flows/workspace-deliverables.md) | ent#365 — reports gain an audience and a place to appear; why the Workspace read had to stop asking the operator's question |
| Agent canvas | [agent-canvas.md](feature-flows/agent-canvas.md) | ent#438 — the durable surface an agent keeps current (vs a report, published once); why the per-agent workspace page could be deleted, and why audience defaults closed |
| Workspace voice mode | [workspace-voice-conversation.md](feature-flows/workspace-voice-conversation.md) | ent#534 — the modal real-time call with the orb inside the Workspace chat: thread binding, write-as-you-go transcript, the canvas column, the cap, and why the ent#440 loop went |
| Workspace thread code blocks | [workspace-thread-code-blocks.md](feature-flows/workspace-thread-code-blocks.md) | #2515 — code blocks read as code and the thread can be copied; why decoration is opt-in and post-render, why the markers are stripped before it, and why a `<` in a block body means the block is not decorated |
| Workspace stick-to-bottom | [workspace-stick-to-bottom.md](feature-flows/workspace-stick-to-bottom.md) | #2624 — an arrival follows only a reader who was already following; why the threshold is 64px rather than exact, why a thread switch re-arms without scrolling, and why late growth needs a ResizeObserver rather than a frame loop |
| Workspace composer typeahead | [workspace-composer-typeahead.md](feature-flows/workspace-composer-typeahead.md) | ent#392 — `/` playbooks and `@` agents in the composer; why the trigger rule is stricter than the parser, why un-mentionable slugs are excluded, and why Enter never accepts implicitly |
| Workspace session identity | [workspace-session-signout.md](feature-flows/workspace-session-signout.md) | ent#357 + #2258 — two ways of being signed in, one way out; why sign-out destroys the credential rather than suppressing a derivation of it |
| Workspace roster briefing | [workspace-roster-briefing.md](feature-flows/workspace-roster-briefing.md) | #2163 — the roster stops waiting for the slowest agent in the fleet; the deferred-briefing route, the two-number bound, the server-owned `briefing_state`, and the three loading zones (skeletons since #2540) |
| Workspace agents at the centre | [workspace-agents-at-the-centre.md](feature-flows/workspace-agents-at-the-centre.md) | ent#523 + ent#524 — the pinned Main chat and why its uniqueness is an index rather than a check, why Reset needs no second reset primitive, where each piece of the dismantled agent page went, and why the file drop is one implementation with three destinations |
| Workspace conversation rail | [workspace-rail.md](feature-flows/workspace-rail.md) | ent#474 + #2540 + #2582/ent#548 — the collapsible third column and the tab contract every capability docks into; why the door gate is one function, why the signal is derived rather than latched, why the stage loads as a skeleton, and (Slice 3) why the upload signal is a pending SET, why `?download=1` is one-way, and why a per-viewer dismissal does not validate the id it dismisses |
| Workspace work | [workspace-work.md](feature-flows/workspace-work.md) | ent#525 — the live execution card and the Work tab; why the read is under the portal roster and not the fleet ACL, why a delegated child is found by the chat, why steps have three states, and why the signal is one set merged by id |
| Workspace model choice | [workspace-model-choice.md](feature-flows/workspace-model-choice.md) | ent#403 — the composer's curated model dropdown; why curation is a flag on the one catalog, why the options ride the roster and the default rides the card, why the model is resolved right after the availability gate rather than where it is used, and why the usage-limit copy must never mention it |
| Workspace loops | [workspace-loops.md](feature-flows/workspace-loops.md) | ent#458 + ent#338 — running and watching a bounded loop from the chat that started it; why it needed no new endpoint, and why the per-run timeout refuses rather than clamps |
| Chat turn cancellation | [chat-turn-cancellation.md](feature-flows/chat-turn-cancellation.md) | ent#155 — Escape and Stop on all three conversation surfaces; why the authorization differs per surface, and why a refused cancel must not restore the text |
| Web Terminal | [web-terminal.md](feature-flows/web-terminal.md) | Browser-based terminal for System Agent |
| Self-Execute | [self-execute.md](feature-flows/self-execute.md) | Agent background task during chat (SELF-EXEC-001) |
| Workspace chat tabs & titles | [workspace-chat-tabs-and-titles.md](feature-flows/workspace-chat-tabs-and-titles.md) | Workspace chats as tabs, New-chat hotkey, renameable titles |

### Testing & Development

| Flow | Document | Description |
|------|----------|-------------|
| Testing Agents Suite | [testing-agents.md](feature-flows/testing-agents.md) | Automated pytest suite (1460+ tests) |
| Local Agent Deployment | [local-agent-deploy.md](feature-flows/local-agent-deploy.md) | Deploy local agents via MCP/CLI — embedded-manifest integrity verification, symlink preservation, evidence-bearing response (#2060) |
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
| Skills Management | SPLIT | — (document not preserved) | Split into the dedicated skills flows above (skill-assignment, skill-injection, skills-library-sync) |

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

The Testing-section template is the add-testing skill (`/add-testing`, dev-methodology plugin); the method behind it is `docs/testing/STRATEGY.md`.
