# Bug Escape Analysis — 2026-06-27

> Defect-escape (leakage) analysis of bugs closed in the last **30 days** (the skill's default
> window). Every reported bug *escaped* one or more quality gates between an engineer's keyboard
> and `main`. The escape is the signal; aggregated, the escapes name which gate is leaky and
> whether the fix is **enforcement** (a gate exists but wasn't applied) or **coverage** (no gate
> watches the class). **Report-only** — no skills, CI, or ledger were edited.

> **Relationship to the 2026-06-26 report.** A 90-day analysis was written yesterday
> (`bug-escape-analysis-2026-06-26.md`). This 30-day window (`closed:>=2026-05-28`) is a strict
> *subset* of that population — the 2026-06-01 release batch-close pulls almost the entire
> backlog into both windows — so a blind re-run would near-duplicate it. Instead this report
> **composes** with yesterday's: it deep-analyzes **18 high-severity 30-day defects that yesterday
> only aggregated or deferred** (the reliability/feature/voice P0–P1s, deliberately *not* the
> subprocess-teardown saga or the security cluster, which yesterday covered in depth). Treat the
> two reports as one corpus: yesterday = teardown saga + security debt; today = the feature/lifecycle/
> voice escapes. Where today reinforces a yesterday recommendation it says so; where it surfaces a
> genuinely new class it flags it **[NEW]**.

## Executive summary

- **Scope**: closed `type-bug` in `abilityai/trinity` (public tracker), `closed:>=2026-05-28`.
  **131 defects** in scope (P0 ×6, P1 ×70, P2 ×47, P3 ×6, untagged ×2 — a P1-dominated window
  because the 2026-06-01 release closed the whole reliability saga at once). Deep blame-chain RCA
  on **18** fresh defects yesterday did not individually analyze. **~29 excluded** as
  non-product-escape (8 `/validate-*` drift meta-issues, ~15 test-infra fixes, ~6 dependency/
  credential hygiene). The rest are aggregate-only or already deep-analyzed in the 2026-06-26 report.
- **Leakiest gate**: **Tests / Implementation** again — ~10 of the 18 are "a path with no test that
  was wrong from birth or after a partial fix" (param mismatch, stale signature, wrong-API kwargs,
  missing dispatch marker, unvalidated input). Same verdict as yesterday, independent sample.
- **Enforcement vs coverage**: this batch is **~16 coverage / ~2 enforcement** — even more
  coverage-skewed than yesterday's 70/30, *by selection*: I sampled the reliability/feature
  population, whose escapes are overwhelmingly "no gate watches this." The enforcement-heavy
  gaps are in **security** (yesterday's `#888/#821/#600/#470/#720/#1159`). Combined read across both
  reports stands: **security needs teeth, reliability/features need reach.**
- **Recurrence**: `docs/memory/learnings.md` still holds **exactly 1 entry** (2026-06-23,
  schema.py+tables.py). None of these 18 match it. Yet the classes recur *across both reports* —
  **incomplete-fix chains** (4 fresh: `#686 #1264 #1153 #1197`), **worker-local state** (`#704`),
  **new-surface-not-wired** generalized to allowlists/CSP (`#1224 #1153 #1264`). **A comprehensive
  report recommended repopulating the ledger yesterday; an independent cut re-finds the same classes
  today.** The review→ledger→autoplan loop is demonstrably broken on the *write* side.

**Top 3 systemic gaps (this window, by frequency × severity):**
1. **Incomplete-fix chains — the single most recurrent class across both reports.** A prior fix
   establishes a defense/guard but applies it to only one of two codepaths, or *the fix itself*
   ships the next bug: `#280` added `mark_execution_dispatched` to the async branch only →
   **`#686`** (sync path); `#212→#739` kept a restrictive PAT guard → **`#1264`**; `#817`'s `#857`
   cgroup sweep lacked a docker-exec allowlist → **`#1153`**; **`#1126`'s own fix `#1128`
   introduced `#1197`** (unguarded `int(cpu)`). Sharpens yesterday's Rec #7.
2. **One feature/refactor commit ships a *cluster* of latent defects, each masked by a different
   non-happy-path. [NEW framing]** MON-001 commit `7f443f7c` → **`#1121`** (restart-masked) *and*
   **`#682`** (non-admin-masked); VoIP commit `4840f367d` → **`#1069`** *and* **`#1073`**; the voice
   feature → `#704`/`#705`. (Yesterday: PR `#1093` → `#1199`+`#1200`.) The happy path was tested;
   restart, non-admin, multi-worker, and the real external protocol were not.
3. **Silent-degrade / swallowed-exception inflates latency and misdirects diagnosis. [NEW]** A broad
   `except` that logs a generic message and continues hides the root cause: **`#1267`** (NameError →
   "Error starting transport", masked ~2.5 mo), **`#950`** (OSError → silent fallback to a
   container-only path, ~5 mo), **`#1153`** (kills misread as OOM — a host was upsized for nothing).

**Single highest-leverage fix** (unchanged from yesterday, now *urgent*): **make report-writing
append to `docs/memory/learnings.md`, and backfill the classes both reports identified.** The
reports are being produced; nothing flows into the ledger; so the next `/autoplan` can't see any of
it. This is the one fix whose absence both reports independently prove.

---

## Defect inventory (18 fresh deep-analyzed)

Escape stage per the DEVELOPMENT_WORKFLOW gate taxonomy. **Enf** = enforcement gap (gate existed,
missed). **Cov** = coverage gap (no gate). Conf = attribution confidence. Latency = introducing-merge
→ issue `createdAt`.

| Issue | Pri | One-line defect (from the fixing diff) | Introduced-by (its issue) | Fixed-by | Escape stage | Class | Enf/Cov | Conf | Latency |
|-------|-----|----------------------------------------|---------------------------|----------|--------------|-------|---------|------|---------|
| #1121 | P1 | fleet-health loop only started by `/monitoring/enable`, never lifespan-wired; `enabled` non-persisted, default True → dies every restart | commit `7f443f7c` MON-001 (direct) | PR #1123 | Architecture | service-not-lifespan-wired | Cov | med | ~15wk |
| #682 | P2 | monitoring `get_fleet_status` calls stale 2-arg `get_accessible_agents` → 500 for non-admins | commit `7f443f7c` MON-001 (direct) | PR #694 | Tests/Impl | stale-signature, role-branch-untested | Cov | high | ~10wk |
| #1267 | P2 | lifespan uses bare `db` (only `_db` in scope) → NameError, swallowed into misleading "Error starting transport"; skips webhook reconcile | PR #234 (#216) + PR #463 (#299) | PR #1270 | Tests/Impl | swallowed-exception, dual copy-paste | Cov | high | 8–11wk |
| #1153 | P1 | cgroup orphan sweep SIGKILLs operator `docker exec` + platform exec paths — no docker-exec allowlist (SSH had one) | PR #857 (#817 follow-up) | PR #1154 | Tests/Impl | incomplete-fix, kills-legit-process | Cov | high | ~4wk |
| #1069 | P1 | VoIP `/voip/call` uses `AuthorizedAgent` (Path param `name`) vs route `{agent_name}` → 422 on every call | PR #1066 (#1056) | PR #1071 | Tests/Impl | new-endpoint param/dep mismatch | Cov | high | ~2h |
| #1073 | P1 | VoIP WS ticket via `<Stream url>` query string (Twilio strips it) + close-before-accept → 403 on answer | PR #1066 (#1056) | PR #1075 | Implementation | external-protocol assumption (Twilio) | Cov | high | ~3.5h |
| #704 | P1 | voice WS session in a per-process dict → lost across `--workers 2` (~50% 403) | commit `4cbf6cac` voice (#159, direct) | PR #706 | Architecture | worker-local state | Cov | high | ~6.5wk |
| #705 | P1 | voice `on_tool_call` audit `log()` called with non-existent `actor_type/_id/_email` kwargs → TypeError every tool call | PR #594 (#581) | PR #706 | Tests/Impl | wrong-API-signature, no-test | Cov | high | ~8d |
| #1037 | P1 | SUB-003 auto-switch `_restart_agent` recreates container → kills in-flight executions in other slots | PR #154 (#153) | PR #1196 (#1089) | Impl/design | destructive-op conflates rotate+kill | Cov | high‡ | ~74d |
| #686 | P2 | sync `/api/chat` never calls `mark_execution_dispatched` → no-session sweep false-FAILs cold-start rows after 60s | PR #280 (#279), async-only | PR #795 | Tests/Impl | incomplete-fix, one-of-two-paths | Cov | med | ~29d |
| #741 | P1 | terse MCP `enabled` tool-schema description → AI callers echo `enabled:true`, silently re-enable disabled schedules | commit `24763b54` MCP-SCHED-001 (direct) | PR #742 | Impl/design | MCP-tool-schema-as-AI-contract | Cov | high | ~99d |
| #1264 | P1 | per-agent PAT never propagates to a tokenless existing container — guard `if env_vars.get('GITHUB_PAT')` | PR #212 (#209) → PR #739 (#735) | PR #1271 | Tests/Impl | incomplete-fix, graft-onto-guard | Cov | med | 41–83d |
| #1197 | P2 | unguarded `int(cpu)` on fractional/k8s CPU → opaque ValueError; failed create leaks an `mcp_api_keys` row | PR #1128 (#1126) | PR #1227 | Implementation | input-validation, partial-create-no-rollback | Cov | high | ~2d |
| #950 | P1 | `deploy_local_agent` probes `:ro` mount, silently falls back to a container-only path → empty agents | commit `dd6ad60f` (direct) + refactor | PR #971/#982 | Implementation | swallowed-exception, wrong-fallback | Cov | med | ~5mo |
| #1265 | P1 | `_fetch_single_agent_context` declared `async` but runs blocking Docker+DB on the loop → `gather` serializes (20s); + N+1 + missing index | commit `adb743a2a` (direct, *billed as perf*) | PR #1269 | Impl/perf | async-blocking-call, no-scale-test | Cov | med | ~5.5mo |
| #1224 | P0 | CSP `connect-src` omits the ask-trinity Cloud Function origin → Help chat `fetch()` blocked | PR #199 (#190) + PR #641 (#549) | PR #1225/#1226 | CI/Config | new-fetch-target not in CSP allowlist | Cov | high | 41–79d |
| #1201 | P2 | agent 504 timeout body omits `ctx.metadata` → bare FAILED row, cost/context/tool-calls lost | PR #1094 (#1147) inherited older gap | PR #1229 | Implementation | error-path telemetry-loss | Cov | med | ~2d† |
| #1126 | P1 | `cpu_count=` (Windows-only docker-py) instead of `nano_cpus` → CPU cgroup limit never enforced on Linux; + UI defects | commit `1e6eeda8` (direct) + re-copied by PR #613 (#611) | PR #1128 | Implementation | wrong-API-param silent-noop, copy-paste ×3 | Cov | high | ~5.5mo / 41d |

‡ `#1037` is a **mass-close trap**: the issue was administratively closed by Release v0.6.1 (PR #1171, no code change); the real fix is the `#1089` hot-reload (PR #1196), a week later. High confidence on the *introducing* side (born with auto-switch `#154`), med overall.
† `#1201` line-level blame is clean to `#1094` (2d), but `#1094` *inherited* the omission — the timeout path never carried telemetry since it was first written; the true class is older.

#689 (P1, "duplicate mount point on restart with custom_mounts") was **closed-no-fix** (NOT_PLANNED) — triple-verified as a *phantom*: there is no `custom_mounts` column/field/migration anywhere in the repo. Excluded from escape stats; noted as a likely cross-codebase/auto-generated false report.

---

## Patterns

### Per escape-stage (18 analyzed)

| Escape stage | Count | What leaked |
|--------------|-------|-------------|
| **Tests / Implementation** | 8 | Wrong-from-birth or wrong-after-partial-fix paths with no test: `#682 #1267 #1153 #1069 #705 #686 #1264 #1197` |
| **Implementation / design / perf** | 5 | Behavioral/portability/perf defects a test *could* catch but none existed: `#1037 #741 #950 #1265 #1126 #1201` (overlaps) |
| **Architecture** | 3 | Lifecycle/worker-local: `#1121` (lifespan), `#704` (worker-local), `#1224` (cross-surface CSP) |
| **External protocol** | 1 | `#1073` — Twilio Media Streams contract, unknowable from local unit tests |

Same shape as the 2026-06-26 report from an *independent* sample: Tests/Implementation is the leak.

### Enforcement vs coverage

**~16 coverage / ~2 enforcement.** The two arguable enforcement misses (`#682` stale signature,
`#1267` bare-`db` scope error) are things `/review` could in principle flag but realistically only a
test catches. This batch is coverage-dominated *by construction* — I sampled reliability/feature
defects. The enforcement story lives in security (yesterday). **Net across both reports: the process
needs reach for reliability and teeth for security.**

### Recurrence — the headline, now with a second data point

The ledger has **1 entry**; these classes recur **across both reports**, unledgered:

- **Incomplete-fix chains** — `#686` (`#280` async-only), `#1264` (`#212→#739` guard), `#1153`
  (`#817→#857` allowlist), `#1197` (`#1126`'s fix `#1128` planted it). Joins yesterday's
  `#531→#657`, `#687→#921`. **The most recurrent class in the corpus.**
- **One feature commit → a defect cluster** — `7f443f7c` (MON-001) → `#1121`+`#682`; `4840f367d`
  (VoIP) → `#1069`+`#1073`; voice → `#704`+`#705`. Joins yesterday's `#1093` → `#1199`+`#1200`.
- **Worker-local state** — `#704` (voice WS). Joins yesterday's `#1165 #858 #456`.
- **Swallowed-exception / silent-degrade** — `#1267 #950 #1153`. **Not named in either report's
  recommendations yet** — new.
- **New-surface-not-wired, generalized to allowlists/CSP** — `#1224` (CSP connect-src), `#1153`
  (orphan allowlist), `#1264` (PAT propagation guard). Broadens yesterday's Dockerfile-COPY/facade
  Rec #3 from "COPY set" to "any consumer-side allowlist/guard/registry."

That a 90-day report recommended repopulating the ledger **yesterday**, and a 30-day cut re-derives
the same classes **today**, is itself the proof that the loop is broken on the write side.

### Hot introducing areas

`services/monitoring_service.py` + `routers/monitoring.py` (MON-001 commit `7f443f7c` → 2 defects),
`adapters/transports/twilio_media_stream.py` + `services/voip_service.py` (VoIP `4840f367d` → 2),
`routers/voice.py` + `services/gemini_voice.py` (voice cluster), `services/agent_service/{crud,lifecycle}.py`
(resource-config churn: `#1126`→`#1128`→`#1197`), `services/subscription_auto_switch.py` (`#1037`→`#1089`).
**The resource-config area is a live churn hotspot** — a fix bred a regression inside one window.

### Escape latency — bimodal, and the swallowed-exception tax

- **Hours–days** when a fast signal exists: VoIP `#1069`/`#1073` caught same-day in `dev` (2–3.5h);
  `#1197` 2d (opaque-but-loud ValueError); `#1201` 2d.
- **Weeks–months** on prod-only, scale-only, role-gated, or *swallowed* paths: `#1265` ~5.5mo
  (scale-only, 10+ agents), `#950` ~5mo (silent fallback), `#1126` ~5.5mo (Linux-only silent no-op),
  `#741` ~99d, `#1037` ~74d, `#682` ~10wk (non-admin-only), `#704` ~6.5wk (multi-worker-only),
  `#1267` ~2.5mo (NameError swallowed). **The longest latencies cluster on defects that produce a
  *misleading* symptom rather than a loud failure** — the silent-degrade tax.

---

## Recommendations (prioritized)

Each names a specific lever, cites real defects, and is tagged enforcement/coverage. Recommendations
that **reinforce** a 2026-06-26 item add new evidence rather than restating it.

### P0 — Make report-writing append to the learnings ledger, and backfill it *(reinforces 2026-06-26 Rec #2 — now urgent)*
- **Evidence**: ledger = 1 entry; both reports independently re-derive incomplete-fix, worker-local,
  new-surface-not-wired, swallowed-exception. The 24-hour gap between the two reports re-finding the
  same classes *is* the evidence the write side never fires.
- **Target lever**: `/review` (Self-Improvement / ledger-append step), `/bug-escape-analysis`
  (Phase 5 already *recommends* ledger entries — make it the canonical producer), `docs/memory/learnings.md`.
- **Proposed change**: (a) backfill ledger entries for the corpus classes — *incomplete-fix recurrence*
  (require a regression test naming the prior issue before closing a follow-up), *one-feature-commit-
  defect-cluster* (feature PRs need non-happy-path tests), *worker-local-state*, *swallowed-exception/
  silent-degrade*, *new-producer-not-in-consumer-allowlist*, *MCP-tool-schema-as-AI-contract*,
  *async-blocking-call*. (b) Add a hard checklist line to `/review`: fail the review if it surfaced a
  class but wrote no ledger entry. (c) Have `/autoplan` print which ledger entries it checked.
- **Expected prevention**: turns each escape into a pipeline input. **Type**: enforcement (of a
  designed-but-dormant mechanism).

### P0 — `/review` plan-completion: enforce incomplete-fix discipline *(reinforces 2026-06-26 Rec #7, sharper)*
- **Evidence**: `#686` (`#280` patched async-only), `#1264` (`#212→#739` left the guard),
  `#1153` (`#817→#857` missed an allowlist source), **`#1197` (the fix `#1128` *introduced* it)** —
  four fresh instances; plus yesterday's `#531→#657`, `#687→#921`.
- **Target lever**: `/review` Plan-Completion audit.
- **Proposed change**: when a PR references a prior issue ("follow-up/regression of #N") **or is itself
  a fix**, `/review` must (a) require a regression test that names the prior issue; (b) enumerate
  *every* codepath/call-site of the defended behavior and assert the fix covers all of them, not just
  the reported one (the `#280`/`#212`/`#857` failure mode); (c) check the fix introduces no adjacent
  unguarded sibling (the `#1128→#1197` failure mode — a new `int()`/parse/branch shipped *by the fix*).
- **Expected prevention**: breaks the fix-breeds-the-next-fix chains that dominate the corpus.
  **Type**: enforcement.

### P1 — Require non-happy-path tests on feature/refactor PRs *(NEW — addresses the defect-cluster pattern)*
- **Evidence**: `7f443f7c` → `#1121`+`#682`; `4840f367d` → `#1069`+`#1073`; voice → `#704`+`#705`;
  (yesterday `#1093` → `#1199`+`#1200`). The happy path worked; **restart-survival, non-admin,
  `--workers 2`, and the real external protocol** were each an untested escape.
- **Target lever**: `/implement` (test-authoring step) + `/review` checklist + `/update-tests` catalog.
- **Proposed change**: a feature/refactor PR that adds a background service, an endpoint, a WS handler,
  or an external integration must ship the matching non-happy-path test: **service → survives a lifespan
  restart with persisted config** (`#1121`); **endpoint → a non-admin/owner-scoped call** (`#682`) +
  **a smoke call that actually exercises the dependency/path params** (`#1069`); **WS/session state →
  a `--workers 2` cross-worker assertion** (`#704`); **external integration → a contract test or an
  explicit "untestable locally, verified against <vendor>" note** (`#1073`). Pair with the
  `--workers 2` CI lane below.
- **Expected prevention**: the whole "one commit, N latent bugs" class. **Type**: coverage.

### P1 — `--workers 2` CI smoke lane *(reinforces 2026-06-26 Rec #4)*
- **Evidence**: `#704` (voice WS session) confirmed deep this window; joins `#1165 #858 #456 #1160`.
- **Target lever**: `/test-runner` / `backend-unit-test.yml`.
- **Proposed change**: boot the backend `--workers 2` and assert cross-worker-shared state
  (setup token, **voice/VoIP WS sessions**, chat sessions) lives in Redis, not a module global; plus
  the migration suite under concurrent workers. **Type**: coverage.

### P1 — `/review`: ban silent-degrade / swallowed-exception *(NEW — addresses the latency tax)*
- **Evidence**: `#1267` (NameError → misleading log, ~2.5mo), `#950` (OSError → wrong fallback path,
  ~5mo), `#1153` (mis-diagnosed as OOM for weeks). These three account for the longest mis-directed
  latencies in the window.
- **Target lever**: `/review` structural checklist + a ledger entry.
- **Proposed change**: flag any new `try/except` that (a) catches broadly and logs a *generic* message
  while continuing, or (b) silently falls back to an alternate path on error. Require either re-raise,
  a specific exception type, or a log that names the actual exception. A lightweight grep guard
  (`except Exception` followed by a `logger.*` with no `exc_info=`/`{e}`) can seed it.
- **Expected prevention**: collapses the misleading-symptom latency tax. **Type**: coverage→enforcement.

### P1 — Cross-surface "producer not in consumer allowlist" guards *(reinforces/broadens 2026-06-26 Rec #3)*
- **Evidence**: yesterday's Dockerfile-COPY/facade (`#1033 #768 #1200`); this window broadens it —
  `#1224` (new `fetch()` target ↔ CSP `connect-src`), `#1153` (new exec path ↔ orphan allowlist),
  `#1264` (new PAT source ↔ propagation guard).
- **Target lever**: `/validate-config` (CSP allowlist vs frontend external fetch origins) and a
  `/review` checklist line for allowlist/guard registration.
- **Proposed change**: (a) extend `/validate-config` to diff every frontend external fetch origin
  against `security-headers.conf` + `vite.config.js` `connect-src` (would block `#1224` at config-validate
  time). (b) `/review` line: "if the diff adds a new producer of a guarded resource (exec session, kill
  target, credential source, fetch origin), confirm it's registered in the consumer's allowlist/guard."
- **Expected prevention**: `#1224`-class CSP omissions; `#1153`/`#1264`-class allowlist gaps. **Type**: coverage.

### P2 — Review MCP tool-schema descriptions as AI-caller contracts *(NEW)*
- **Evidence**: `#741` — a terse `enabled: "Enable/disable schedule"` description led AI callers to
  echo `enabled:true` and silently re-enable disabled schedules (~99d latency).
- **Target lever**: `/review` (MCP surface) — Architecture Invariant #13 already names the MCP server
  as a synced surface; extend it to *semantics*.
- **Proposed change**: when a PR touches `src/mcp-server/src/tools/*.ts`, `/review` checks that any
  field whose value mutates state (enable/disable, delete, overwrite) carries a description warning the
  AI caller to omit it unless intentionally changing it, and that `exclude_unset`/partial-update
  semantics are documented. **Type**: coverage.

### P2 — Lint blocking calls in async + a scale perf smoke *(NEW)*
- **Evidence**: `#1265` — `async def` doing blocking Docker+DB on the event loop defeated its own
  `gather` (20s dashboard, scale-only, ~5.5mo); the introducing commit was itself *billed as a perf
  improvement*.
- **Target lever**: `/review` checklist + `/test-runner` (a fixed-fleet timing smoke).
- **Proposed change**: (a) `/review` line: "an `async def` that calls a blocking client (docker-py,
  a sync DB cursor, `requests`) on the loop thread is a concurrency bug — it must `await`, use a thread
  executor, or be sync." (b) A smoke test that fans `context-stats` over ~10 stub agents and asserts a
  wall-clock ceiling (would have caught the serialization). **Type**: coverage.

---

## Appendix

### Methodology & exact scope
- **Tracker**: `abilityai/trinity` (public; bugs route here per § Repository Routing).
- **Population**: `gh issue list --repo abilityai/trinity --label type-bug --state closed
  --search "closed:>=2026-05-28" --limit 300` → **131** issues. Default window (30d), default
  `--limit 20` (18 analyzed; the rest deferred or already covered 2026-06-26).
- **Blame chain** (per defect): fixing PR via `closedByPullRequestsReferences`, discarding the
  release PR that mass-closed issues (2026-06-01 / -06-12 / -06-23) and resolving the real
  `fix/N-*`/`feature/N-*` code PR via `gh pr list --search "<N> in:title,body"`; then
  `git blame <fix_merge>~1 -L start,end -- file` → introducing squash → `(#NNNN)` parsed from the
  subject. Latency = introducing-merge → issue `createdAt`.

### Attribution-confidence notes
- **Med/low confidence**: `#1121` (omission/missing-wiring, not a regressed line — clean blame to the
  MON-001 commit but no "removed buggy line"); `#686` (additive fix, causal chain over 3 commits);
  `#1264` (root cause multi-site: creation gate + lifecycle guard + propagation carve-outs); `#950`
  (a refactor `1e6eeda8` relocated the block, so raw blame credits the move, not the Dec-2025 semantic
  origin `dd6ad60f`); `#1265` (dominant cost is one clean line, but "the bug" is distributed across
  several N+1 origins); `#1201` (line-level clean to `#1094`, but `#1094` inherited an older omission);
  `#1037` (mass-close trap — issue-close PR ≠ real fix).
- Everything else is single clean blame.
- **Mass-close trap explicitly hit**: `#1037` (closed by Release v0.6.1 #1171 with no code change;
  real fix is `#1089`/PR #1196). Flagged in-table with ‡.
- **Title-vs-diff correction**: `#1126` filed as a UI bug ("header display, dialog pre-selection,
  apply flow") — the confirmed root cause is a backend Docker-API portability defect (`cpu_count` vs
  `nano_cpus`: CPU limits were silently never enforced on Linux). Reclassified to Implementation.

### Excluded from per-defect escape stats (counted, not blamed) — ~29
- **8 `/validate-*` drift meta-issues** (`#655 #656 #690 #691 #721 #722 #770 #771`) — detector output,
  not escaped product defects.
- **~15 test-infra fixes** (`#659 #660 #715 #725 #752 #755 #762 #763 #764 #765 #766 #802 #804 #1260`
  + `#664` security-test backfill) — full-suite-only failures; engineering drag, not product escapes.
- **~6 dependency/credential hygiene** (`#793 #823 #1140 #1158 #1163 #1300`).

### Deferred (in-scope, aggregate-only — not re-blamed here)
- **Already deep-analyzed in the 2026-06-26 report** (this window overlaps it): the subprocess-teardown
  saga `#531 #548 #586 #618 #630 #631 #640 #649 #657 #687 #970 #808 #817`, security `#600 #720 #1159`,
  status/error `#671 #1022`, `#476`, `#1130 #1076 #1231`, `#1199 #1200`, `#1033 #768`, `#1165 #858`.
  See that report rather than duplicating.
- **In-scope, not yet deep-analyzed by either report**: `#244` (Slack Socket Mode 4h death),
  `#333` (futex spin loop), `#408 #474` (worker-recycle / broken-pipe-CB), `#456` (migration race),
  `#539` (public-chat dup message), `#557` (stale images), `#562` (Telegram image uploads),
  `#568` (file-sharing require_email), `#606 #612` (auto-switch injection), `#675` (fleet-health
  refresh), `#683` (Slack watchdog stall), `#707` (canvas flicker), `#728` (90% CPU OAuth),
  `#735` (PAT fallback), `#759 #789` (session-tab), `#816` (orphan rows), `#843` (local templates),
  `#859 #677` (clipboard), `#951 #895` (channel approval/memory), `#989` (push button), `#1094`
  (watchdog mislabel), `#1096` (telemetry 6s), `#1143` (badge limit=1), `#1160` (migration runner),
  `#1230` (healthcheck flap). These corroborate the same clusters; none changes a recommendation.

### Caveats & limits
- **Selection bias (two layers)**: only *reported* bugs are visible (detection-of-detected, not the
  true escape rate); and within that, this report deliberately sampled the reliability/feature
  population, which makes the enforcement/coverage split coverage-heavy. Read the split together with
  the 2026-06-26 security findings, not alone.
- **Window overlap**: the 30-day window is a strict subset of the 2026-06-26 90-day window. The two
  reports are one corpus; cross-report recurrence (not ledger-match) is the real recurrence signal.
- **Report-only**: no skill, CI workflow, or ledger file was modified. Applying any recommendation
  (and filing the P0/P1 process-debt items via `/create-issue`) is a separate, human-approved step.
