# Meta-Analysis: Trinity's Execution / Orchestration Bug History

**Date:** 2026-06-06
**Scope:** ~80 issues spanning #56 → #1085 — orchestration, executions, timeouts, circuit breakers, watchdogs, cleanup, loops, scheduler dispatch, idempotency, subscription auto-switch, the canary harness, and agent-side reader races.
**Method:** 11 per-family forensic analyzers (each reading the actual issue bodies, merge diffs, and owning code), each pressure-tested by an adversarial verifier, feeding 4 cross-cutting synthesis lenses. Raw outputs preserved in Part II (synthesis lenses) and the Appendix (per-family forensic reports).
**Companion:** `TARGET_ARCHITECTURE.md` §Coordination Model. This analysis drove the 2026-06-06 result-contract tightenings in that document.

> **Update 2026-07-01 — #1084 approach reframed (v2).** References below to "#1084 as *the gate*" and the `{execution_id}:{effect_ordinal}` universal-sink key describe the **Direction-A** thinking as of this 2026-06 analysis. `TARGET_ARCHITECTURE.md` v2 replaces universal sink-keying with **retry-with-prior-trace** (#1401) + **deterministic tool-side gates** on capability-confined irreversible rails + an **async operator human-gate** (#1402), gating **per-effect not per-agent**; #1084 is no longer *the* gate on pull default-on. Kept as a point-in-time snapshot — the analysis body is unchanged.

---

## The one-sentence finding

> **Every one of the ~50 execution bugs traces to the same two co-original-sins: the backend holds an HTTP connection open for the entire multi-minute agent turn (`PUSH_DISPATCH_BLOCKING`), so it can never *know* an execution finished — only time out — and because there is no completion signal, "is-this-running" got physically split across a Redis ZSET + a SQL row + agent RAM (`SPLIT_STATE_AUTHORITY`), which nothing reconciles transactionally.**

Push-blocking creates the *need* to split; split-state produces the actual *bugs*. Everything else — TTLs, watchdogs, circuit breakers, the canary harness — is compensation for those two. Removing push-dispatch (#1081 pull) removes the *need* to split; removing split-state (#1082 / #429) removes the *bugs* even before push is gone.

---

## 1. Root-cause taxonomy (frequency-ranked)

| Rank | Class | Primary bugs | Families | Verdict |
|---|---|:---:|:---:|---|
| 1 | **SPLIT_STATE_AUTHORITY** | ~18 | 10/11 | **The disease.** One fact owned by 3+ stores, reconciled by nothing. |
| 2 | **MISCLASSIFIED_FAILURE** | ~12 | 8/11 | A killed/OOM/timed-out turn reported as auth/empty/null — the platform guesses from exit codes & substrings. |
| 3 | **TTL_HEURISTIC** | ~10 | 9/11 | A clock standing in for a completion signal; the value drifted 3+ times. |
| 4 | **PUSH_DISPATCH_BLOCKING** | ~8 | 9/11 | **The environmental original sin** — forces TTLs / watchdogs / breakers into existence. |
| 5 | **PARALLEL_CODE_PATHS** | ~9 | 8/11 | N dispatchers each re-implement timeout / classify / dedup; one copy always drifts. |
| 6 | MISSING_IDEMPOTENCY | ~7 | 5/11 | Trigger-dedup shipped (#525); effect-dedup (#1084) is the open frontier. |
| 7 | ORPHAN_PROCESS | ~7 | 7/11 | Leaked claude procs / slots / reader threads — the tail of split state. |
| 8 | RACE_TOCTOU | ~6 | 8/11 | A *symptom* of split state (a CAS race needs two writable copies), rarely the deepest cause. |
| 9 | POINT_FIX_ACCRETION | ~6 | 9/11 | The meta-class: each fix added a guard/sweep instead of removing a cause. |
| 10 | READER_RACE | ~4 | 4/11 | stdout is a lossy, child-inherited completion channel (rooted in #548). |

**Master cause: SPLIT_STATE_AUTHORITY** (primary on ~18 bugs, secondary on ~24 more, present in 10/11 families). Every other top class is provably downstream: RACE_TOCTOU needs two writable copies; TTL_HEURISTIC is the coping mechanism for having no authoritative signal; ORPHAN_PROCESS is the leaked-resource consequence; the entire canary harness exists only to *measure* the split. **Co-original-sin: PUSH_DISPATCH_BLOCKING** is the environmental pressure that *makes* the split necessary.

---

## 2. The "kept-breaking" list — recurrence hotspots

The strongest meta-signal is not any single bug; it is the areas where **each fix became the next bug**:

| Hotspot | Breaks | The chain |
|---|:---:|---|
| **`cleanup_service.py` watchdog/sweep stack** | **9+** | #129 → #219 → #226 → #378/#403 → #497 → #748 → #749 → #869 (→ #1035 decomp) |
| **Transport circuit breaker** (`agent_client.py`) | **6** | #304 → #474 → #873 → #631 → #687 → #921/#934 |
| **The auth-fallback classifier** | **5** (×3 copies) | #285 → #361 → #516/#517 → #906 → #904 |
| **`_classify_empty_result` reader-race** | **5** | #520 → #531 → #630 → #640 → #678 |
| **Slot-TTL math** | **4** | #226 (fixed 20m) → #323 (per-agent) → #869 (per-slot) → #913 (decay-reconstruct) |
| **The two timeout columns** | **4** | #99 → #665 → #913 → #929 |
| **Startup orphan recovery** | **3** | #128 → #748 → #749 |

The two worst offenders: `cleanup_service.py` (9 reconciliation paths + a cyclomatic-53 method that #1035 itself calls *"unsafe for an AI agent to edit"*), and the auth classifier (**physically copied across 3 containers, hand-synced — and the backend docstring claiming "the scheduler imports this list" is already false**, so the drift the family warns about is visibly underway).

---

## 3. The accretion pattern (how point-fixing failed)

Four representative chains, each showing a fix *creating* the next bug:

- **Slot TTL** — a fixed 20-min guess (#226) → per-agent (#323) → per-slot (#869) → and when per-slot TTL equalled the floor exactly, the canary built to watch it (S-03) began **false-firing on natural decay** (#913), so the *detector* had to be taught to reconstruct the original TTL. Five revisions of one number, ending not in a fix but in a watcher that **accepts the guess**. The 1200s fallback is *still in the code* (`slot_service.py:33`).
- **Circuit breaker** — #304 shipped four bad choices at once (count-everything, per-worker state, fixed cooldown, clock-reset-on-failure); the next *eight* tickets un-make them one at a time — including #688, which was **deleted 4 days after merge** by the Redis migration (#698) that superseded it. Endpoint (#526) is what #304 should have been on day one.
- **Cleanup pyramid** — watchdog (#129) needed a SQL-fail path (#219), which *raced* the agent's success (#378), whose re-verify ping *timed out under load* (#869), whose unreachable-branch *deferred forever* (#497) … 1 watchdog → 9 paths + 3 independent grace constants nothing keeps consistent.
- **Failure classifier** — one substring auth-heuristic (#285) that *every new exit shape falls into*: max-turns (#361) → SIGKILL (#517) → chat path (#909) → OOM (#904), each needing a re-added precedence guard, now triplicated across containers.

**Tipping point.** **#428 (CapacityManager consolidation)** is where the team stopped *adding* primitives and started *deleting* them — but it **deliberately preserved the wire format** (same Redis keys, same ZSET-vs-row split). The proof it was *containment, not cure*: **#748, #749, #869, #913 all post-date #428 and all hit the same split-state machinery, now behind a cleaner door.** #428's own issue body says it depends on push-completion because *"much of the current TTL/drain machinery exists to compensate for blocking HTTP dispatch."* That sentence is the bridge from patching → the **#1081 pull / work-stealing** redesign.

---

## 4. What pull fixes — vs. what it does not

| Pathology | Pull mechanism | Verdict |
|---|---|---|
| Slot/capacity overbooking | Physical worker pool — N workers literally can't run N+1 | **Eliminates** (gated on #300) |
| Transport + dispatch breakers | Dead agent just stops pulling; breaker → operator *alert* | **Transport eliminated; dispatch relocated** |
| 9-path cleanup pyramid | Single lease-reaper flips expired leases back to `queued` | **Eliminates 9→1** (gated on #307) |
| Timeout / TTL math | One envelope `deadline` + lease renewal | **Eliminates the heuristic** |
| 12-writer status column | Backend-owned row + CAS result-write | **Eliminates split** (#1082) |
| Scheduler held connection | Scheduler becomes "INSERT a queued row" | **Eliminates** (#1083) |
| Canary invariants | Single owner → divergence structurally impossible | **Retires S-01/S-02/S-03/E-02** |

**What pull does NOT solve (and where it makes things worse):**

1. **Effect idempotency (#1084) — and pull makes it *worse*.** Lease-expiry re-delivery **re-runs the whole turn**, re-sending any email / Slack / Nevermined-charge / git-push the first attempt made. **Zero sinks carry an effect key today.** This is *the gate*: read-only agents migrate first, side-effect agents last.
2. **Thundering herd (#1085).** A backend restart makes ~200 agents re-poll one DB simultaneously; a shared cause turns benign re-delivery into a fleet-wide storm. A hazard pull *introduces*; soak must be validated against an induced mid-flight backend restart.
3. **In-agent reader-races (#548, #333).** The result is corrupted *before* it's reported; pull gives a cleaner ack channel but re-runs produce the same null. Needs an out-of-band, agent-owned result record.
4. **Credential hot-reload.** Auto-switch still requires a container recreate (#1037); pull only softens the *consequence*.

**Hard sequencing gates:** `#300 (Postgres) → before the queue carries the fleet` · `#1084 (effect-keys) → before any side-effect agent migrates` · `#307 (push-completion, shipped-but-UNWIRED) → before #429 retires the watchdog` · `#1085 (herd controls) → before default-on`.

---

## 5. Recommended changes to the Target Architecture (applied 2026-06-06)

The target architecture already predicted the two co-original-sins, the lease-reaper, #1084-as-gate, the #1085 herd risk, and the #300 sequencing. The analysis surfaced **three residual seams pull alone does not close**, now folded into `TARGET_ARCHITECTURE.md`:

1. **Typed terminal-reason on the `reply` envelope.** The agent emits `{status, error_code, cost, tokens, session_id}`; the backend never infers the failure reason from exit codes or stderr substrings. Structural cure for the MISCLASSIFIED_FAILURE class (the auth-substring classifier re-patched 5+ times across 3 hand-synced copies). Pins part of #945.
2. **Agent-owned out-of-band result record.** The worker's result POST must read from a durable agent-written record, not parsed `stdout` (which Claude's grandchildren inherit on fd 1 and can corrupt before the POST — #548/#333). State stays the backend's; the *result payload* must not ride a lossy inherited pipe.
3. **Credential rotation via hot-reload, not recreate.** Rotating a token uses `/api/credentials/update`; recreate is reserved for image/template changes. "Rotate a credential" stops being "kill every in-flight turn" (#1037).

The **coordination model itself is unchanged** — these tighten the *result contract* (#945) and the *agent runtime*.

---

## 6. Open risks (highest-severity, as of 2026-06-06)

| Sev | Issue | What's exposed |
|---|---|---|
| **HIGH** | **#1022** | **Largest production failure class.** Scheduler dispatch POST has no try/except; `str(httpx.ReadTimeout()) == ''` → empty-error `failed` rows with zero triage signal, in synchronized batches. Cheap, isolated fix — *not* on the pull path. |
| **HIGH** | **#1037** | Auto-switch `_restart_agent` gates on Docker `container.status`, never CapacityManager — one 429 recreates the container and kills *every* parallel in-flight execution. |
| **HIGH** | **#1084** | Effect idempotency unbuilt — the gate on pull-default for side-effect agents. |
| **HIGH** | **#548** | Root of the whole reader-race family; the `FD_CLOEXEC` fix is necessary-but-insufficient (claude re-inherits fd 1 to its own grandchildren). |
| **HIGH (latent)** | **#408** | **Closed prematurely** ("shipped v0.6.0") but the synchronous ~2h await is *still live* at `task_execution_service.py:746` — a recurrence trap for anyone searching closed issues. |
| MED | #333, #429, #307, #792, #799 | Futex/zombie spin; pyramid intact; heartbeat seam unwired; one-shot switch has no replay; no per-agent switch lock. |

**Cross-cutting fragilities (no single issue #):** 3-container classifier duplication with a false docstring; non-atomic slot acquire (`slot_service.py:132/141`, S-02 is a *detector* not a guard); the live 1200s slot-TTL fallback; the drain-sentinel swap window (`backlog_service.py:184-187`); the `chat↔backlog` string-literal lazy import (`backlog_service.py:255`); sync-`sqlite3`-in-async-loop (#904 RC-1 is an accepted band-aid); fail-open dedup that weakens precisely under a duplicate storm.

**The canary harness is currently the load-bearing safety net** — #748/#749 were *found by canary S-01, not in production*. But that is containment, not cure: the harness needed its *own* point-fixes (S-03 decay, B-02 boot-window both shipped buggy), and its deepest risk is that *its existence becomes an excuse to defer the structural fix*. Its eventual *shrinkage* is the redesign's success metric (S-01 retired by #1082, etc.) — and it must **not** be retired ahead of the redesign that makes its invariants structurally impossible to violate.

---

## 7. Ordered forward recommendations

1. **[HIGH · cheap] Fix #1022 now** — wrap the dispatch POST, emit `str(e) or type(e).__name__` + a structured `error_code`, don't finalize on timeout. Kills the biggest production failure class without touching the dispatch model.
2. **[HIGH · P1] Fix #1037** — gate `_restart_agent` on CapacityManager, not Docker status; add the #799 per-agent switch lock in the same PR.
3. **[MED] Unify the failure classifier** into one shared package — ends the #361 → #517 → #904 treadmill and the false "scheduler imports this" docstring.
4. **[MED] Wire #307's heartbeat into the dispatch breaker** — the unmet prerequisite for #429 (whose body mislabels its dep as #306).
5. **[HIGH · gating] Ship #1084 effect-scoped idempotency** — non-negotiable gate for pull-default on side-effect agents.
6. **[HIGH · gating] Migrate the queue to Postgres (#300)** — `SKIP LOCKED` is also the natural #1085 herd mitigation and collapses the three concurrency authorities (slot ZSET + fan-out semaphore + loop handle).
7. **[STRUCTURAL] Land #1082 (status-as-projection)** — bankable win, retires S-01/S-02/E-02, ships *independently* of the full migration.
8. **[STRUCTURAL] #1083 fire-and-forget → then #429 cleanup-collapse** — **do not ship #429 early** (ripping out the watchdog before the lease model soaks trades known bugs for unknown ones).
9. **[STRUCTURAL · terminal] #1081 pull / work-stealing** — the cure; gated on #1084 + #300 + #307 + #1082 landing and soaking. Complete #1068/#1074 (drop per-task `timeout_seconds`) as in-flight pre-work so the timeout becomes one envelope-level `deadline`.

Items 1–4 are containment, 5–6 are the gates, 7–9 are the cure in dependency order.

---

# Part II — Synthesis Lenses (raw)

*The four cross-cutting synthesis outputs, verbatim. Each grounds the executive summary above.*

===== SYNTHESIS LENS: Root-cause taxonomy & frequency =====
# LENS 1 — Root-Cause Taxonomy & Frequency

## Methodology note
Counted across all 11 family reports. Each scored bug entry contributes its **primary** class (weight 1) and any **notable secondaries** (tracked separately). Where the same issue appears in multiple families (e.g. #226, #378/#403, #516/#517, #913, #904/#907, #1022, #748/#749, #1082), I count the bug **once per distinct appearance** for the "appearances" tally but flag cross-family duplicates so frequency isn't double-inflated. The ranked table below uses **primary-class assignments** as the spine, with secondary tallies in a separate column.

---

## Ranked root-cause taxonomy

| Rank | Root-cause class | Primary count | + Secondary count | Families touched | One-line verdict |
|------|------------------|:---:|:---:|:---:|------------------|
| 1 | **SPLIT_STATE_AUTHORITY** | ~18 | ~24 | 10 / 11 | The disease. One fact ("is-X-running / capacity / deadline / done") owned by Redis ZSET + SQL row + agent RAM + a 2nd SQL column, reconciled by nothing — every other class is downstream of it. |
| 2 | **MISCLASSIFIED_FAILURE** | ~12 | ~13 | 8 / 11 | A killed/timed-out/OOM/transport-dropped turn reported as the wrong thing (auth/empty/null), because the platform never sees a typed completion signal — it guesses from exit codes and substrings. |
| 3 | **TTL_HEURISTIC** | ~10 | ~17 | 9 / 11 | A clock standing in for a completion signal. Every grace window, slot TTL, and stale-cutoff is a guess that races the thing it measures; the value drifted 3+ times (20min→per-agent→per-slot→decay-reconstructed). |
| 4 | **PUSH_DISPATCH_BLOCKING** | ~8 | ~14 | 9 / 11 | The environmental original sin. Backend holds an HTTP connection for the whole multi-minute turn → no liveness signal exists → forces TTLs, watchdogs, and breakers into being. |
| 5 | **PARALLEL_CODE_PATHS** | ~9 | ~12 | 8 / 11 | N independent dispatchers (chat/task/scheduler/fan-out/loop/MCP) each re-implement timeout, classification, and dedup — so every fix must be applied N times and one copy always drifts. |
| 6 | **MISSING_IDEMPOTENCY** | ~7 | ~8 | 5 / 11 | No request/effect identity at producer boundaries → re-deliveries and auto-retries manufacture duplicates; trigger-dedup (#525) shipped, effect-dedup (#1084) is the open frontier. |
| 7 | **ORPHAN_PROCESS** | ~7 | ~13 | 7 / 11 | A process/slot/row/thread outlives the thing that should reap it (claude subprocess, Redis slot, daemon reader thread, loop handle) — the leaked-resource tail of split state. |
| 8 | **RACE_TOCTOU** | ~6 | ~14 | 8 / 11 | Check-then-act with no CAS across the split (cleanup-vs-completion, startup-recovery-vs-dispatch, ZADD-vs-ZREM, concurrent auto-switch). Always a *symptom* of split state, rarely the deepest cause. |
| 9 | **POINT_FIX_ACCRETION** | ~6 | ~15 | 9 / 11 | The meta-class. Each fix added a guard/sweep/marker instead of removing a cause — 9 cleanup paths, 3 classifier copies, the canary needing its own point-fixes. The shape of the whole corpus. |
| 10 | **READER_RACE** | ~4 | ~6 | 4 / 11 | stdout is a lossy, child-inherited terminal-signal channel; the trailing `{"type":"result"}` line is lost on force-close → null telemetry. Concentrated in Family 6, rooted in #548. |

---

## THE MASTER CAUSE

### **SPLIT_STATE_AUTHORITY** — removal would have prevented the most distinct bugs.

**The count.** SPLIT_STATE_AUTHORITY is the **primary** class for ~18 bugs and a **secondary** on ~24 more. It is the *only* class that appears in **10 of 11 families** (absent only as a primary in pure-classification Family — and even there it's the design-pathology backdrop). The bugs it directly causes, by issue number:

- **#90, #913** — one timeout/status fact in *two SQL stores* (scheduler DB vs backend DB; `agent_schedules.timeout_seconds` vs `agent_ownership.execution_timeout_seconds`).
- **#219, #378/#403, #748/#749, #129, #128/#165, #524, #1082** — "is-running" split across Redis ZSET + SQL row + agent RAM.
- **#631** — circuit-breaker state split across two per-worker RAMs.
- **#1037** — destructive recreate reads Docker container status while live-work truth lives in CapacityManager.
- **#260/#316, B-01, B-02, #411, #653, #882** — the backlog and the *entire canary harness* exist solely to reconcile or measure this split.

**The argument.** Every other top-ranked class is provably **downstream** of split state:

- **RACE_TOCTOU (#8)** only exists *because* two stores can disagree — a CAS race requires two writable copies. The verifier notes repeatedly demote RACE to secondary "per the prefer-the-deeper-cause rule" (#378, #129, #1082): the race is real but the split is deeper.
- **TTL_HEURISTIC (#3)** is the *coping mechanism* for split state with no authoritative signal — "a clock standing in for a completion signal." Remove the split (single owner of "done") and the clock has nothing to stand in for.
- **ORPHAN_PROCESS (#7)** is the leaked-resource *consequence* of split state (slot outlives row, #749; row outlives reaper, #767/#106).
- The **canary harness** (Family 10) is the strongest evidence: it is described verbatim as *"the instrument that measures split-brain"* — an entire subsystem built only because the splits are structural and uncurable by point-fix.

**The clincher.** The corpus names the cure explicitly and consistently: **#1082 status-as-projection** ("once a single structure owns the fact, 'Redis disagrees with SQL' becomes structurally impossible") retires canary S-01; **#429 lease-reaper** collapses 9 reconciliation paths into 1; **#1081 pull/work-stealing** makes "is-X-running" a single lease the worker holds. All three target split state. No other class has a single structural cure that dissolves this many families at once.

**Honest caveat — the co-original-sin.** PUSH_DISPATCH_BLOCKING (#4) is the *environmental pressure* that **makes** split state necessary: because the backend blocks on the turn and gets no push-completion, "done" has no single owner, so it gets split and guessed-at. Multiple families (2, 3, 4, 5, 6, 8) name push-dispatch as the "true original sin." The honest synthesis: **PUSH_DISPATCH_BLOCKING creates the conditions; SPLIT_STATE_AUTHORITY is the mechanism that produces the actual bugs.** Removing push-dispatch (#1081) removes the *need* to split; removing split state (#1082/#429) removes the *bugs* even before push is gone. Since the question is "whose removal prevents the most **distinct bugs**," SPLIT_STATE_AUTHORITY wins on bug count (~18 vs ~8 primary), but #307 push-completion is the prerequisite that makes its removal *achievable* — and the corpus repeatedly notes #307's heartbeat seam is **shipped-but-unwired**, which is why the cure remains gated.

---

## RECURRENCE HOTSPOTS — the "kept-breaking" list (3+ breaks)

Files/areas that broke **three or more times** across the corpus, with the breaking sequence:

| Hotspot (file / area) | Breaks | Sequence | Dominant class |
|---|:---:|---|---|
| **`cleanup_service.py` watchdog/sweep stack** | **9+ paths** | #129 → #166 → #219/#227 → #226 → #378/#403 → #497/#783 → #748/#812 → #749/#814 → #869/#871 (+#1035 decomposition) | SPLIT_STATE_AUTHORITY / TTL_HEURISTIC |
| **`slot_service.py` / slot TTL math** | **4** | #226 (fixed 20-min) → #323 (per-agent) → #869/#871 (per-slot-metadata) → #913 S-03 (decay-reconstruction) | TTL_HEURISTIC |
| **The auth-fallback classifier heuristic** (`error_classifier.py` / `_classify_signal_exit` / `_diagnose_exit_failure`) | **5** | #285/#322 → #361 (max-turns) → #516/#517 (SIGKILL) → #906/#909 (chat path) → #904/#907 (OOM + backend matcher) | MISCLASSIFIED_FAILURE / PARALLEL_CODE_PATHS |
| **`_classify_empty_result` / reader-race telemetry classifier** | **5** | #520 → #531 → #630 → #640 → #678 (all cited in its own docstring) | READER_RACE / MISCLASSIFIED_FAILURE |
| **Transport circuit breaker** (`agent_client.py` `CircuitState`) | **6** | #304/#308 → #474/#798 → #873 → #631/#698 → #687/#688 → #921/#924/#934 | SPLIT_STATE_AUTHORITY / MISCLASSIFIED_FAILURE / TTL_HEURISTIC |
| **The two timeout columns** (`agent_ownership` vs `agent_schedules`) | **4** | #99 → #665 → #913 → #929 (+#1068/#1074 third override removal) | SPLIT_STATE_AUTHORITY |
| **Startup orphan recovery** (`recover_orphaned_executions` + `_reconcile_orphaned_slots`) | **3** | #128/#165 → #748/#812 → #749/#814 | RACE_TOCTOU / ORPHAN_PROCESS |
| **No-session / skipped cleanup query** | **3** | #106 → #137/#201 → (E-05 guard added) | ORPHAN_PROCESS / POINT_FIX_ACCRETION |
| **`execute_task` push-dispatch boundary** (status write) | **3+** | #101 → #132 → #1022 (→#1083 structural) | PUSH_DISPATCH_BLOCKING / MISCLASSIFIED_FAILURE |
| **The canary harness itself** (self-regressions) | **3** | S-03 decay (d2148677) → B-02 boot-window (659df68f) → R-01 regex doc-lag | POINT_FIX_ACCRETION |
| **`backlog_service.py` drain spawn / lazy import** | **3** | #260/#316 (seam born) → #496/#500 (import dead since #95) → #428 (absorbed, not cured) | PARALLEL_CODE_PATHS |

### The two worst offenders
1. **`cleanup_service.py`** — broke at least **9 distinct times** and grew to 9 liveness-reconciliation paths + 13 total sweeps. It is the physical embodiment of SPLIT_STATE_AUTHORITY: every break is a new gap *between two reconcilers*. It cannot be fixed by another sweep — only #429/#1082 retire it.
2. **The auth-fallback classifier** — broke **5 times** across **3 physical copies** in 3 containers (`error_classifier.py`, `subscription_auto_switch.py`, `src/scheduler/service.py`), kept in sync by hand. Every new non-auth exit shape (max-turns → SIGKILL → OOM → call-budget) that contains an auth substring re-triggers it. The "scheduler imports this list" docstring is *already stale/false* — the drift the family warns about is visibly underway.

**Meta-signal:** every hotspot above is a place where a fix became the next bug. That is POINT_FIX_ACCRETION (Rank 9) operating on a SPLIT_STATE_AUTHORITY (Rank 1) substrate under PUSH_DISPATCH_BLOCKING (Rank 4) pressure — the three-class signature of the entire corpus.

===== SYNTHESIS LENS: The accretion narrative =====
# LENS 2 — The Accretion Narrative

## The Four Deepest Point-Fix Chains

### Chain A — Slot TTL: a time-guess that needed five revisions and a watcher

| Step | Issue/PR | Fix | What it CREATED for the next step |
|---|---|---|---|
| A0 | #226 (TTL_HEURISTIC) | Replace the *acquire-path* per-agent `timeout+buffer` mismatch... but the **sweep** still used fixed `DEFAULT_SLOT_TTL_SECONDS=1200` | A single 20-min constant now governs reclaim regardless of the agent's real timeout |
| A1 | #323 (point-guard) | Thread a per-**agent** `agent_timeouts` dict into the sweep | Per-agent is still wrong for per-**schedule** timeouts; and the #665 default bump (900→3600) made non-default agents common, so the gap became reproducible |
| A2 | #665 (TTL_HEURISTIC) | Bump default chat timeout 900→3600 | Every downstream TTL heuristic that assumed "timeout ≈ 900" (slot floor, cleanup window) now silently wrong; directly arms #869 and #913 |
| A3 | #869/#871 (TTL_HEURISTIC + PUSH_DISPATCH_BLOCKING) | Read per-**slot** `timeout_seconds` from the slot HASH at ZADD time; raise `WATCHDOG_HTTP_TIMEOUT` 5s→15s | Per-slot TTL now equals the floor *exactly*, so the canary's `ttl < floor` check false-fires by ~1s on every healthy slot |
| A4 | #913 (SPLIT_STATE_AUTHORITY) | Scheduler column `agent_schedules.timeout_seconds` shadowed the per-agent value; made it NULL-inherit (#922) | Producer TTL is now correct, but the canary S-03 detector now trips on decay |
| A5 | #913 S-03 decay-invariance (d2148677, TTL_HEURISTIC) | Reconstruct initial TTL as `ttl + age` from the ZSET score; compare `floor − 1` | The *detector* is now decay-tolerant — i.e. the system **accepts** a decaying-guess TTL rather than carrying a true deadline. Containment, not cure. |

**Net:** five revisions of one number (`20-min → per-agent → per-slot → NULL-inherit → decay-reconstructed`) plus a canary invariant (S-03) built *to watch the guess*. Residual: `DEFAULT_SLOT_TTL_SECONDS=1200` still lives at `slot_service.py:33` as a fallback when the metadata HASH expires before the ZSET member (#226 class can still bite).

### Chain B — Circuit breaker: every property of the original #304 design re-litigated once

| Step | Issue/PR | Fix | What it CREATED for the next step |
|---|---|---|---|
| B0 | #304/#308 (origin) | In-process `CircuitState`, threshold 3, **fixed 30s cooldown**, `last_failure_time` reset every call, **count-everything** | Four latent defects shipped at once: count-everything, per-worker state, time-cooldown, clock-reset-on-every-failure |
| B1 | #474→#798 (MISCLASSIFIED_FAILURE) | Count only `(ConnectError, ConnectTimeout)`; EPIPE/ReadTimeout skip `record_failure()` | Narrowed "what counts" — but left the *sibling-collapse* race when one caller evicts the pooled client |
| B2 | #873 (MISCLASSIFIED_FAILURE + RACE_TOCTOU) | Process-local `_recent_drops` ~2s grace + `AgentConnectionDroppedError` + identity-checked pool eviction | Grace map is **per-worker** — deliberately accepted SPLIT_STATE remnant in an otherwise Redis-unified breaker |
| B3 | #687→#688 (TTL_HEURISTIC) | One-line `if self.state != "open":` so probe failures stop pinning the cooldown clock | Point-fix on a field about to be deleted — **superseded 4 days later** (#688 May 6 → #698 May 10) |
| B4 | #631→#698 (SPLIT_STATE_AUTHORITY) | State → Redis Lua hash, `SET NX EX 10` probe-lock, exp backoff 30→300s, **dormant after 10 probes** | The new **dormant** state has *no exit path* → becomes #921 |
| B5 | #767→#773 (ORPHAN_PROCESS) | `allow_request()` before dispatch → close probe row immediately as `CIRCUIT_OPEN` | `CIRCUIT_OPEN` enum lineage carried forward; #773 body names #526 as the upstream move |
| B6 | #921→#924 (TTL_HEURISTIC) | 1h self-heal cooldown probe, operator-queue alert, admin reset, two-cycle watchdog confirm | The backend two-cycle/Redis-sentinel fix is itself complexity → deleted by #934 |
| B7 | #934 (POINT_FIX_ACCRETION→deletion) | Close the false-orphan race **agent-side** (`_recently_completed` buffer); **182 added, 518 deleted** (net −336 LOC) | First commit missed the startup-recovery path; a `/review` caught it, second in-PR commit hoisted `_extract_agent_known_ids` |
| B8 | #526/#986 (MISCLASSIFIED_FAILURE) | **Second** breaker (dispatch, AUTH-only) in a separate `agent:dispatch:{name}` namespace + separate Lua, at the top of `CapacityManager.acquire()` | Converges on what #304 should have been: **two breakers, two namespaces, each fed one failure class** |

**Net:** the count-everything / per-worker / time-cooldown / clock-reset triple of #304 was each fixed in turn; the endpoint is two Redis-backed breakers each answering exactly one question ("can I reach it" vs "will dispatch succeed").

### Chain C — Cleanup pyramid: each layer's fix is the next layer's bug

| Step | Issue/PR | Fix | What it CREATED for the next step |
|---|---|---|---|
| C0 | #129 (SPLIT_STATE_AUTHORITY) | Add the **active watchdog** Phase 0 reconciliation (PR #166) — first brick | Watchdog needs a way to fail the SQL row when it reclaims a slot |
| C1 | #219/#227 (SPLIT_STATE_AUTHORITY) | `fail_stale_slot_execution()` with `WHERE status='running'` guard | The new FAILED-write path now **races** the agent's in-flight SUCCESS; review of #227 also spawns #226 (Chain A) |
| C2 | #378/#403 (RACE_TOCTOU) | JIT per-agent **re-verify** HTTP ping before writing FAILED | The re-verify is an HTTP ping that **times out under load** (busy agent at 3/3 capacity) |
| C3 | #497/#783 (POINT_FIX_ACCRETION) | "Agent unreachable" branch now **force-fails** instead of deferring forever | Adds a 4th heuristic branch; the cure for one race opened a liveness gap that needed its own guard |
| C4 | #128/#165 → #748/#812 + #749/#814 (RACE_TOCTOU / ORPHAN_PROCESS) | Startup recovery (PR #165) → then **two symmetric** sweeps: SQL→Redis (#812) and Redis→SQL `_reconcile_orphaned_slots` (#814), each with its own 15s grace constant | Now **three independent grace constants** (`WATCHDOG_MIN_AGE=60`, `STARTUP_RECOVERY_GRACE=15`, `SLOT_RECOVERY_GRACE=15`) must stay mutually consistent — nothing enforces it |
| C5 | #429 (POINT_FIX_ACCRETION) | **9 reconciliation paths / 12 writers** recognized as the bug; lease-reaper planned | The pyramid is the bug; collapse gated on agent push-completion (#307, OPEN) |
| C6 | #1035/#1026 (POINT_FIX_ACCRETION) | Extract `_run_cleanup_inner` (cyclomatic-53, ~290 LOC) into 13 named `_sweep_*` strategies | Consolidated the **code**, not the **paths** — runtime risk unchanged; canary is the safety net |

**Net:** 1 watchdog → 9 reconciliation paths, 3 grace constants, a 53-complexity mega-method, and a canary harness (S-01/S-02/S-03/E-01/E-02/E-05/B-02/R-01) that *detects* the divergence the pyramid was built to prevent. #748 and #749 were found **by canary S-01, not in production** — the harness is now the active line of defense.

### Chain D — Failure classification: one auth heuristic, every exit shape lands in it

| Step | Issue/PR | Fix | What it CREATED for the next step |
|---|---|---|---|
| D0 | #285/#322 (MISCLASSIFIED_FAILURE) | Agent self-kills on stderr `_is_auth_failure_message()` match → 503 → backend tags `AUTH` | A substring/zero-token heuristic is now the auth signal — every non-auth exit shape will land in it |
| D1 | #361 (MISCLASSIFIED_FAILURE) | Precedence guard for **max-turns** exit ahead of auth heuristic | First instance of "new exit shape needs a new guard"; same file, same heuristic block |
| D2 | #516/#517 (MISCLASSIFIED_FAILURE) | `_classify_signal_exit()` consulted **before** auth heuristics → 504; require `return_code > 0` | Added **headless path only** — the chat path classifier is now missing |
| D3 | #906/#909 (PARALLEL_CODE_PATHS) | Wire the **chat path** (`claude_code.py:458`) to call `_classify_signal_exit` | The literal "add the same function to the other path" — the classifier now exists in 2 of 3 surfaces |
| D4 | #904/#907 (MISCLASSIFIED_FAILURE + PARALLEL_CODE_PATHS) | `NON_AUTH_KILL_MARKERS` blocklist in `subscription_auto_switch.py` + scheduler twin; `BackendAgentCallBudgetExhausted` bypasses SUB-003 | Classifier now physically duplicated across **3 containers** (agent-server, backend, scheduler), hand-synced; the backend docstring claiming "scheduler imports this list" is already **stale/wrong** |

**Net:** one substring heuristic became a precedence guard re-added at least 5 times (#361, #517, #909) across 3 hand-synced copies. The misclassification also burned the SUB-003 2h skip-list slot (MISSING_IDEMPOTENCY) on every false auth call.

---

## The Tipping Point: #428, then #1081

The accretion has a precise inflection. **#428/#527 (CapacityManager consolidation, PARALLEL_CODE_PATHS → structural-consolidation, CLOSED)** is where the team stopped adding primitives and started deleting them: `ExecutionQueue` was deleted, `SlotService` + `BacklogService` became private internals behind one `acquire()` facade — but **the wire format was explicitly preserved** (same Redis keys, same SQL columns, same ZSET-vs-row split). That is the signature of a *containment* decision, not a cure: #428 collapsed the PARALLEL_CODE_PATHS half of the family while leaving SPLIT_STATE_AUTHORITY untouched. The proof is in the corpus's own timestamps — **#748, #749, #869, #913 all post-date #428 and all hit the same split-state/TTL machinery, now behind one facade.**

What made consolidation *inevitable* rather than optional: the chains crossed a complexity threshold where each fix demonstrably *caused* the next bug in the same area, and the team began documenting this in the fixes themselves. #871's PR names its own predecessors (#226, #378, #497, #749) in the same code. #429 enumerates "9 reconciliation paths / 12 writers." #1035 shows the code-shape symptom: a single cyclomatic-53, ~290-line method "unsafe for an AI agent to edit." When a point-fix can no longer be reasoned about in one pass and reliably spawns its successor, point-fixing stops scaling — and the team switched to consolidation.

But #428 also *named its own ceiling*. Its issue body states verbatim that it **depends on #306** because "much of the current TTL/drain machinery exists to compensate for blocking HTTP dispatch; the consolidation gets much smaller once #306 moves state transitions to the event consumer." This is the hinge from consolidation to redesign: the corpus's single deepest finding is that **PUSH_DISPATCH_BLOCKING is the true original sin** behind every chain. The backend holds an `httpx` connection open for the entire agent turn (up to `execution_timeout_seconds`, default 3600s), so it can never *know* an execution is done — only time out and reconcile afterward. That forces the slot TTL (Chain A), forces the breaker to guess liveness from a clock (Chain B), forces the watchdog pyramid (Chain C), and makes "agent busy" indistinguishable from "agent dead" so every kill is mis-tagged (Chain D). **#1081 (pull / work-stealing redesign, Epic #1045, OPEN)** is the structural cure: once an agent *pulls* work and reports completion through the event bus, the slot ZSET, per-slot TTL, watchdog, re-verify ping, two orphan sweeps, and canary S-01/S-02/S-03 collapse into a single authoritative completion signal. The gate, per the corpus, is **#1084 effect-idempotency** (because pull/redelivery trades reconciliation bugs for duplicate-execution bugs) and **Postgres #300** before the queue carries the fleet — and the prerequisite #307 agent-push-completion **is still OPEN**, which is exactly why #429's pyramid-collapse cannot ship yet.

---

## Narrative

Trinity's execution layer was built on one assumption that was never true: that the backend could know when an agent finished. It couldn't. Because dispatch is a blocking HTTP push — the backend holds a connection open for the entire multi-minute agent turn — the only completion signal it ever had was a *clock running out*. Every chain in this archaeology is a consequence of substituting a timeout for a completion event, and then patching the places where that substitution leaks.

The slot-TTL chain (A) is the purest illustration. A single fixed 20-minute constant (#226) stood in for "how long can this legitimately run." When that was obviously wrong it became per-agent (#323); when per-agent was wrong for per-schedule timeouts it became per-slot (#869/#871); when the per-slot TTL was made to equal the floor exactly, the very canary built to watch it (#913 S-03) began false-firing on *natural decay*, and had to be taught to reconstruct the initial TTL from the ZSET score. Five revisions of one number, terminating not in a fix but in a detector that *accepts* the guess. The 1200-second fallback is still in the code. The circuit-breaker chain (B) is the same story in a different organ: the original #304 breaker shipped four bad choices at once — count every exception, store state per-worker, use a fixed cooldown, reset the clock on every failure — and the next eight tickets are those four choices being un-made one at a time, including a point-fix (#688) that was deleted four days after merge by the Redis migration (#698) that superseded it. The endpoint, #526, is what #304 should have been on day one: two breakers in two namespaces, each fed exactly one failure class.

The deepest signature of accretion is that **the fix for each layer became the bug of the next**, and the team eventually started writing this down inside the fixes. In the cleanup pyramid (C), the watchdog (#129) needed a SQL-write path (#219), which raced the agent's success (#378), whose re-verify ping timed out under load (#869), whose unreachable-handling deferred forever (#497) — and in parallel the startup-recovery surface split into two symmetric sweeps (#748 SQL→Redis, #749 Redis→SQL) each with its own 15-second grace constant, none of which is enforced to stay consistent with the other two. One watchdog became nine reconciliation paths and twelve writers (#429), wrapped in a single cyclomatic-53 method that #1035 itself describes as "unsafe for an AI agent to edit." That sentence is the tipping point made explicit: when a point-fix can no longer be reasoned about in one read and reliably spawns its successor, point-fixing has stopped scaling. The classification chain (D) shows the cost compounding sideways — one substring auth-heuristic (#285/#322) that every new exit shape fell into, requiring a precedence guard re-added for max-turns (#361), then signal-kills (#517), then the chat path (#909), then OOM at the auto-switch layer (#904) — now physically copied across three containers and hand-synced, with a backend docstring already lying about the scheduler importing the shared list.

So the team consolidated. **#428** is the visible inflection: it deleted `ExecutionQueue`, hid `SlotService` and `BacklogService` behind one `acquire()` facade, and gave the codebase a single drain path. But it deliberately preserved the wire format — the Redis keys, the SQL columns, the ZSET-versus-row split — which is why #748, #749, #869, and #913 all happened *after* it, in the same split state now behind a cleaner door. #428 contained the parallel-code-paths disease; it did not cure the split-state-authority one. And it said so: its own issue body declares it depends on #306 because "much of the current TTL/drain machinery exists to compensate for blocking HTTP dispatch."

That sentence is the bridge from patching to redesign. The corpus's eleven families converge on a single root — **PUSH_DISPATCH_BLOCKING** — and **#1081's pull / work-stealing model** is the only fix that removes it rather than reconciling around it. When an agent pulls its own work and reports completion through the event bus, "is this running" stops being a fact split across three stores that a watchdog must continuously reconcile; it becomes a lease the worker holds. The slot ZSET, the per-slot TTL, the watchdog, the re-verify ping, both orphan sweeps, and half the canary invariants lose their reason to exist. But the redesign cannot ship cheaply: pull-with-redelivery trades this whole reconciliation family for a *duplicate-execution* family unless every outbound effect is idempotent first (#1084), and the queue cannot carry the fleet until it sits on Postgres (#300) — and the immediate prerequisite, agent push-completion (#307), is still open, which is precisely why #429's pyramid-collapse remains gated and the canary harness, not the redesign, is currently the thing standing between Trinity's split state and a production incident.

===== SYNTHESIS LENS: Pathology -> pull-coordination mapping =====
I have everything I need from both docs. Now I'll synthesize the Lens 3 mapping grounded in the corpus.

# LENS 3 — Pathology → Pull-Coordination Mapping

The corpus contains 11 family reports, each with a `design_pathology` section. Every one of them resolves to the **same two co-original-sins**, named verbatim in the corpus and confirmed against `TARGET_ARCHITECTURE.md` §Coordination Model and the #428 dependency note ("much of the current TTL/drain machinery exists to compensate for blocking HTTP dispatch"):

1. **No single authority for "is-X-running / capacity / queued / what's-its-deadline"** — the fact is physically split across the Redis slot ZSET, the SQLite `schedule_executions` row, the agent's in-RAM process registry, and (control-plane) two SQL timeout columns / two SQL files (`SPLIT_STATE_AUTHORITY`).
2. **Push-in-process blocking dispatch** — the backend holds an `httpx` connection open for the entire multi-minute agent turn, so it can only *infer* liveness from a clock, never *know* completion (`PUSH_DISPATCH_BLOCKING`).

Every other token (`TTL_HEURISTIC`, `RACE_TOCTOU`, `ORPHAN_PROCESS`, `MISCLASSIFIED_FAILURE`, `MISSING_IDEMPOTENCY`, `READER_RACE`, `PARALLEL_CODE_PATHS`, `POINT_FIX_ACCRETION`) is downstream of those two. The pull model's design (Principle #5, #2; §Coordination Model; §Recovery; §Failure Isolation) attacks exactly these two, in this precise way: **PostgreSQL = one store kills the split; physical worker pool = capacity is structural, no counter to overbook; agent stops pulling = no push to block; one lease-reaper = no reconciliation pyramid; CAS result-write = no FAILED↔SUCCESS race.**

---

## Master table: each `design_pathology` → pull mechanism

| # | Family / design_pathology (root token) | Pull mechanism (from §Coordination Model / §Recovery / §Failure Isolation) | Verdict | Caveat |
|---|---|---|---|---|
| 1 | **Slot / capacity overbooking** — capacity = TTL'd Redis ZSET decoupled from SQL row; acquire is non-atomic `zcard`→`zadd` (`slot_service.py:132/141`, no Lua/WATCH); S-02 is a *detector* not a guard (`SPLIT_STATE_AUTHORITY` + `TTL_HEURISTIC`) | **Physical worker-pool capacity** (§Coord "Capacity is therefore physical… the agent literally cannot run more than N workers — overbooking is structurally impossible"). The slot ZSET is *deleted*; the atomic `UPDATE…RETURNING` claim replaces `zcard→zadd`. | **ELIMINATES** | The atomic claim's correctness now rides on PostgreSQL row-locking — so this elimination is **gated on #300**. On SQLite the single-writer claim is the new ceiling *before* agent count is (§Open Q #2). Until #300, pull on SQLite re-imports the contention. |
| 2 | **Circuit breakers** — count-everything / per-worker-RAM / time-based-cooldown triple baked into #304; blocking dispatch is the *environmental pressure* that makes a breaker necessary (`MISCLASSIFIED_FAILURE` + `SPLIT_STATE_AUTHORITY` + `TTL_HEURISTIC`) | **Agent stops pulling** (§Failure Isolation: "A dead or wedged agent simply stops calling `next-task`… zero compute is wasted… the per-agent dispatch breaker (#526) is repurposed from a gate into an operator **alert**"). | **TRANSPORT BREAKER ELIMINATED; DISPATCH BREAKER RELOCATED (gate→alert)** | Two survivors. (a) The **sync edge adapter** (human chat / `?wait=true`) still holds a connection → still needs a transport breaker (§Async-First: "The held connection must time out"). (b) #526's AUTH-death detection (`agent:dispatch:{name}`) survives as an *alert*, not a gate — depth-climbing-with-no-results. The #307 `missed_heartbeat` seam stays **unwired** (corpus Family 2; §Failure Isolation note) — pull is "the one model that does not depend on it." |
| 3 | **Cleanup / watchdog pyramid** — 9 reconciliation paths / ~12 status writers, each existing because the layer below can't be trusted; 3 hand-tuned grace constants (`WATCHDOG_MIN_AGE=60`, `STARTUP_RECOVERY_GRACE=15`, `SLOT_RECOVERY_GRACE=15`) (`POINT_FIX_ACCRETION` + `SPLIT_STATE_AUTHORITY`) | **Single lease-reaper** (§Recovery: "A single lease-reaper sweep flips any expired claimed/running row back to `queued`… This one sweep replaces the ~5 reconciliation sweeps and the slot-ZSET watchdog"). One store → nothing to reconcile. | **ELIMINATES (collapses 9→1)** | This is **#429**, and #429's gate is **agent push-completion = #307 (OPEN)**, NOT #306 (corpus Family 3's central correction — #306 shipped only the WS transport). The #429 body itself mislabels the dep as #306. So the pyramid stays load-bearing until #307 lands *and soaks* ("DO NOT SHIP EARLY"). Canary (S-01/E-01/E-05/R-01) remains the active line of defense in the interim. |
| 4 | **Timeout / TTL math** — no completion signal → one timeout number copied into 4 downstream budgets (HTTP wait, slot TTL, cleanup window, canary floor) and split across 2 SQL columns; #326 orphan-cure made SIGKILL the normal end-of-life (`TTL_HEURISTIC` + `SPLIT_STATE_AUTHORITY` + `PARALLEL_CODE_PATHS`) | **Single envelope `deadline` + lease renewal** (§Envelope: one `deadline` field; §Recovery: "A heartbeat from the worker *renews* the lease, so a legitimately long turn is never reaped"). The lease *is* the liveness signal the timeout was faking. | **ELIMINATES the heuristic; RELOCATES the deadline** | The "4 copies → 1 envelope field" is explicit pre-work: **#1068/#1074** demote the per-task `timeout_seconds` override (PR-1-of-6, both **OPEN issues**; `ParallelTaskRequest.timeout_seconds` still live at `models.py:90`). The two SQL columns coexist even post-#913 (NULL-inherit + #929 point-validation, not a single source). Slot-TTL math survives until #429 retires TTL-based tracking. |
| 5 | **Execution state machine** — agent not authoritative for its own lifecycle; `status` is a multi-writer column (~12 writers) the backend guesses from outside; agent has no Redis (#589) so it *can't* be authoritative today (`SPLIT_STATE_AUTHORITY`) | **Backend-owned single queue row + CAS result-write** (§Coord #1 "one writer of 'queued'… one place it lives (PostgreSQL)"; §Recovery "applied under a compare-and-set guard"; #1082 status-as-projection). | **ELIMINATES the split; RELOCATES the writer (N→1)** | #524 **shipped the CAS-guard scope** (v0.6.0, corpus Family 5 correction — it is CLOSED, not "open-unfixed"); the full single-writer `ExecutionStateProjector` was **deferred → re-filed as #1082 (OPEN, P1)**. Under pull the agent computes but does not own state (§Runtime "Result reporting, not journal-as-truth") — which is what finally makes #524's deferred contract realizable. **Gated on #1082 + #300.** |
| 6 | **Agent-side reader races / OOM** — authoritative result (cost/tokens/turns/session-id) rides the same stdout pipe inherited by every claude grandchild; **no out-of-band, agent-owned completion record acked back** (`READER_RACE` + `MISCLASSIFIED_FAILURE` + `ORPHAN_PROCESS`) | **Worker POSTs result to `/api/internal/tasks/{id}/result`** (§Coord #3) — an explicit out-of-band completion ack, exactly the channel the corpus says is missing. JSONL salvage (#797) becomes the primary record. | **REDUCES, does NOT eliminate** | **Pull does not fix #548/#333.** The stdout-pipe-FD inheritance (#548, **OPEN**) and futex/zombie spin (#333, **OPEN**) are *inside the agent container* — they corrupt the result *before* it's POSTed. Pull gives a cleaner ack channel and (via lease re-delivery) a retry, but a reader-race that null-everythings turn N still produces a bad/empty result POST; the agent must still own a durable result the worker reads from. `#945` actor-mailbox (the reason `mailbox_depth` is deliberately unemitted) is the real fix surface, and §Runtime explicitly keeps the journal *non-authoritative*. |
| 7 | **Scheduler fire-and-forget** — dispatch by a blocking HTTP request held open for the full turn; response-as-system-of-record; each fix pushed the held connection one hop further (`PUSH_DISPATCH_BLOCKING`) | **Scheduler becomes "INSERT a queued row on a cron tick"** (§Scheduling: "the scheduler is just another producer… never dispatches to or blocks on an agent"). The held connection ceases to exist. | **ELIMINATES** | This is the canonical pull win (#1083, bankable-win-2, OPEN). **But #1022 — the largest production failure class** (empty-error `failed` rows, `str(httpx.ReadTimeout)==''`) — is *primarily* `MISCLASSIFIED_FAILURE` (corpus Family 5/7 correction), independently fixable and **not waited on by pull**; pull removes its *trigger* (event-loop stall) but the empty-error recording bug should be fixed now, not deferred to the migration. APScheduler+PG job store needs **#300**. |
| 8 | **Idempotency / duplicate executions** — the "single funnel" trusts the producer; push-dispatch manufactures ambiguous "did it land?" signals that turn well-behaved retries into duplicates (`MISSING_IDEMPOTENCY`; deeper: `PUSH_DISPATCH_BLOCKING`) | **Async-first enqueue + lease re-delivery reuses the same `execution_id`+`idempotency_key`** (§Recovery: "a duplicate result POST is absorbed by the compare-and-set guard"; §Async-First: `chat_with_agent` enqueues and returns `{status:"queued"}`). | **RELOCATES — and pull *creates* a new instance of this class** | **Critical inversion (§Re-Delivery):** pull's lease-expiry *re-runs whole turns*, so it **trades reconciliation bugs for duplicate-side-effect bugs** unless every effect is idempotent first. Trigger-dedup (#525) is shipped; **effect-dedup (#1084) is OPEN and is THE GATE.** Pull makes the duplicate-execution risk *worse* before #1084, which is why read/analysis-only agents migrate first. |
| 9 | **Subscription auto-switch** — token injected as **create-time container env var** (`CLAUDE_CODE_OAUTH_TOKEN`) with no hot-reload, so "rotate credential" and "kill every running turn" are the *same operation*; `_restart_agent` reads Docker container status, not CapacityManager slots (`SPLIT_STATE_AUTHORITY`) | **Queue lives in backend → container recreate loses at most the active turn, recovered by lease expiry** (§Recovery crash taxonomy: "container recreate (e.g. subscription auto-switch) → the queue lives in the backend, so at most the active turn is lost… upgraded to a clean drain by a pre-recreate 'stop pulling, finish in-flight' handshake"). | **REDUCES (data-loss → transparent re-delivery); does NOT remove the recreate** | The env-var-injection original sin (no hot-reload) is **unaddressed by pull** — recreate is still required to rotate the token. Pull converts the *consequence* (#1037 collateral kills, **OPEN P1**) from data-loss into re-delivery, **but only if #1084 is done** (a recreate mid-turn re-runs side effects). #799 (no per-agent switch lock, **OPEN**) and #792 (one-shot replay, **OPEN**) both need #1084's idempotency primitive. The "stop pulling, finish in-flight" handshake is new design surface, not free. |
| 10 | **Canary harness** — no single structure owns "is-running"; the harness is the *instrument that measures split-brain*, built because point-fixes kept failing (`SPLIT_STATE_AUTHORITY` + `POINT_FIX_ACCRETION`) | **Single-owner status → invariants become structurally impossible** (#1082 AC: "Canary S-01 documented as redundant once single-owner holds"; §Data Layer kills the slot-ZSET/SQL/RAM split). | **ELIMINATES, invariant-by-invariant (the success metric)** | The canary's *shrinkage* is the redesign's success metric, not a side effect. B-01 already went trivially-green after #428. **But the canary is the only safety net during the transition** and several invariants (S-03 decay, B-02 boot-window) needed their *own* point-fixes — the harness itself accretes fragility. Do not retire S-01/E-02 before #1082 *ships and soaks*. |
| 11 | **Loops / backlog / fan-out** — no single admission/queue authority; each new trigger type (`chat→fan-out→backlog→loop`) funnels into one budget split across Redis+SQL+RAM and re-implements enqueue/drain/restart-recovery (`SPLIT_STATE_AUTHORITY` + `PARALLEL_CODE_PATHS`) | **One durable queue + N pulling workers; fan-out with explicit non-blocking join** (§Coord #1; §Fan-Out: "the coordinator does not hold a worker while waiting"). All four dispatchers collapse to "enqueue an envelope." | **ELIMINATES the multi-dispatcher class; RELOCATES fan-out join to explicit backend state** | The naive self-fan-out deadlock (parent waits on children its own remaining workers must pull) is **a hazard pull *introduces*** — §Fan-Out solves it with an explicit join + a canary for stuck joins, *not* a blocking wait. The `chat↔backlog` import-cycle string-literal lazy import (#496 root, **still live** at `backlog_service.py:255`) is **unaffected by pull** — it's a code-structure seam #428 didn't touch and pull doesn't either. |

---

## What pull does NOT solve — brutal honesty

Pull is a coordination-layer cure. It is silent on, or actively worsens, five things. The corpus is explicit on each:

**1. Effect idempotency (#1084) — the hardest open problem, and pull makes it worse.**
This is stated three times across the corpus (Families 5, 8, 9) and verbatim in §Re-Delivery: lease-expiry re-delivery **re-runs the entire turn**, re-emitting any irreversible side effect (email, Slack/Telegram/WhatsApp/VoIP send, Nevermined charge, git push, `share_file`) the first attempt already performed. Today **zero sinks carry an effect key** — corpus Family 8 confirms `grep` for `effect_ordinal` = 0 across `adapters/`, `proactive_message_service`, MCP outbound tools. Under Invariant #589 the agent's "I finished" write and the backend's idempotency-complete write are on different machines and **can never be one transaction** — so exactly-once external effects are *unattainable at the platform layer*, by construction. The platform contract is honestly "at-least-once delivery with an idempotent coordination boundary." **Pull is strictly more dangerous than push here:** push held the connection and mostly ran a turn once; pull's recovery primitive is "re-run the whole turn." The `{execution_id}:{effect_ordinal}` key threaded through every sink is a cross-cutting workstream (#1084, OPEN) and **it is the gate**.

**2. Thundering herd / correlated failure (#1085).**
Pull's per-agent-benign re-delivery primitive becomes a **self-amplifying retry storm** under a shared cause: a backend restart (which §Coord notes "already happens routinely") makes ~200 agents simultaneously re-poll/reconnect/re-deliver against **one** DB; a fleet-wide bad skill, expired platform key, or Claude-API outage turns each benign re-delivery into a fleet-wide storm. This is a hazard pull *introduces* and that push (with its per-worker breakers and blocking back-pressure) partially masked. Needs jittered re-poll, per-agent + fleet-wide re-delivery rate caps, and a shared-cause pause (#1085, OPEN). The soak gate must be validated against an **induced backend restart with the fleet mid-flight**, not steady state.

**3. Reader-races *inside* the agent (#548 OPEN, #333 OPEN).**
Pull changes *how the result is reported* (out-of-band POST instead of inferred-from-blocking-HTTP), but the corruption in Family 6 happens **before** the result exists: claude's grandchildren inherit stdout fd 1 (#548), wedging the reader thread and discarding the `{"type":"result"}` line; leaked daemon reader threads + zombie claude procs spin into a futex storm over days (#333). A worker that POSTs a null/empty result is still wrong, and lease re-delivery just re-runs a turn that will null-everything again the same way. The corpus is blunt: the real cure is an **out-of-band, agent-owned completion record** (the #945 mailbox/actor surface) — and §Runtime deliberately keeps the journal **non-authoritative**, so even the target arch does not promote JSONL to source-of-truth. These are agent-container bugs pull routes around but does not fix.

**4. The side-effect re-run on re-delivery (the agent's, not the platform's).**
Distinct from #1084's *sink-level* dedup: even with effect keys, a turn with **non-deterministic** internal logic (an LLM that picks a different tool call on re-run) can produce a *different* second side effect that no `{execution_id}:{effect_ordinal}` key matches. §Re-Delivery concedes "exactly-once external effects are the agent's responsibility" — the platform "cannot make a third party's email/payment API exactly-once." Pull's contract is at-least-once, full stop.

**5. Pre-pull demotion debt and code-structure seams pull never touches.**
The envelope "cannot fit honestly until [`ParallelTaskRequest`'s 15 fields] are demoted" (§Open Q #1, `ACTOR_MODEL_TASK_DEMOTION_MAP.md`); #1068/#1074 (OPEN) are PR-1-of-6 of that demotion. And two confirmed in-the-wild incident roots survive every consolidation including pull: the `chat↔backlog` import-cycle lazy import (#496, live at `backlog_service.py:255`, guarded only by an AST name-check) and the per-dispatcher timeout-default convention (#418, "drop the default / pass None" at 5 sites with no enforced ceiling — a sixth dispatcher re-opens it). These are `PARALLEL_CODE_PATHS`/`POINT_FIX_ACCRETION` at the code-organization layer, orthogonal to coordination.

---

## Sequencing gates (hard ordering, from §Open Questions + corpus)

```
                          ┌─────────────────────────────────────────────┐
                          │  #300  PostgreSQL                            │
                          │  ── THE physical gate ──                     │
   The atomic UPDATE…RETURNING claim, the lease-renewal, and the result-│
   write all converge on ONE DB. At 200 agents SQLite's single-writer   │
   lock is the ceiling BEFORE agent count is. PG must land BEFORE the   │
   pull queue carries the full fleet (§Open Q #2). Without it, pull     │
   re-imports the contention class it was meant to kill.                │
                          └───────────────────┬─────────────────────────┘
                                              │
                  ┌───────────────────────────┴──────────────────────────┐
                  │                                                       │
   ┌──────────────▼───────────────┐                    ┌─────────────────▼──────────────┐
   │ READ/ANALYSIS-ONLY agents     │                    │ SIDE-EFFECT-BEARING agents      │
   │ migrate FIRST                 │                    │ migrate LAST                    │
   │ (no irreversible effects →    │   ── #1084 GATE ──▶│ blocked until effect-scoped     │
   │  lease re-delivery is safe)   │                    │ keys {exec_id}:{ordinal} thread │
   └───────────────────────────────┘                    │ through every sink (#1084 OPEN) │
                                                         └─────────────────────────────────┘
```

**The four hard edges:**

1. **#300 (PostgreSQL) BEFORE the queue carries the fleet at scale.** Stated as a §Open-Q-#2 "sequencing constraint added by the pull model" and confirmed across Families 1/5/7: the claim/lease/result-write all hit one DB, and that is *exactly where SQLite's single-writer lock becomes the ceiling, before agent count does*. Pull-on-SQLite is a pilot-only configuration.

2. **#1084 (effect-keys) BEFORE any side-effect-bearing agent migrates.** The explicit rollout rule (§Re-Delivery, §Open-Q-#2a, #1081 tracking table: "#1084 — **the gate**"). Read/analysis-only agents are unblocked now; channel- and payment-bound agents are last.

3. **#307 (agent push-completion / heartbeat) BEFORE #429 retires the cleanup pyramid.** Corpus Family 3's central correction: #429's gate is push-completion = **#307 (OPEN)**, not the #306 that shipped (WS transport). The pyramid is load-bearing — "DO NOT SHIP EARLY." Note pull is "the one model that does not *depend* on #307" as a *breaker* seam (§Failure Isolation), but #429's *cleanup collapse* still needs the push signal to trust before deleting the watchdog.

4. **#1085 (herd controls) BEFORE default-on at fleet scale.** The soak gate must be validated against an induced mid-flight backend restart, not steady state.

**Bankable wins that ship *independently* of the full migration** (de-risking, per #1081 table): **#1082** (status-as-projection / CAS single-owner, retires canary S-01) and **#1083** (fire-and-forget dispatch, kills the Cornelius runaway). Both are corpus-confirmed as shippable without the queue. **#1022** (largest production failure class) is *not* on the pull critical path and should be fixed standalone now — it is a `MISCLASSIFIED_FAILURE` recording bug, not a coordination bug.

**Net:** pull *eliminates* the SPLIT_STATE_AUTHORITY and PUSH_DISPATCH_BLOCKING root pair that generates ~8 of the 11 families — but only behind #300, and it *relocates* the MISSING_IDEMPOTENCY family from trigger-dedup (solved) to effect-dedup (#1084, the gate), while *introducing* two new hazard classes (herd #1085, self-fan-out deadlock) that the target arch handles explicitly rather than papering over. It is silent on the in-agent reader-races (#548/#333) and the credential-hot-reload original sin (#9), which are container-internal and orthogonal to coordination.

===== SYNTHESIS LENS: Residual risk & forward map =====
# LENS 4 — Residual Risk & Forward Map

## Open Risks (severity · issue · root-cause class)

| Sev | Issue | Class token | What's exposed |
|-----|-------|-------------|----------------|
| **HIGH** | **#1022** | MISCLASSIFIED_FAILURE (2°: PUSH_DISPATCH_BLOCKING, TTL_HEURISTIC) | **Largest production failure class.** Scheduler dispatch POST (`service.py:1090-1096`) has no try/except; `str(httpx.ReadTimeout()) == ''` (verified on in-repo httpx 0.28.1) → empty-error `failed` rows, `duration_ms≈30000`, zero triage signal. Fires in synchronized cross-agent batches when the single backend worker's event loop stalls. The #1026 refactor passed over it. |
| **HIGH** | **#1037** | SPLIT_STATE_AUTHORITY (2°: PUSH_DISPATCH_BLOCKING, ORPHAN_PROCESS) | P1, `status-in-progress`. Auto-switch's `_restart_agent` gates on Docker `container.status=="running"` (line 278) and **never consults CapacityManager slots or running rows** — one 429 recreates the container and kills *every* in-flight execution in parallel slots. One afternoon: 8 collateral failures off one shared sub. Also reachable from manual assign (`subscriptions.py:252`) + rebuild/deploy. |
| **HIGH** | **#1084** | MISSING_IDEMPOTENCY (2°: SPLIT_STATE_AUTHORITY) | Effect-scoped idempotency unbuilt — **zero** `effect_ordinal`/effect-key refs in `src/`. Trigger dedup (#525) covers the *entry*, not the agent's *tool calls*. Any at-least-once re-run (lease re-delivery) re-sends email / re-posts Slack / re-charges Nevermined / re-pushes git. **This is the explicit gate on defaulting pull mode ON for side-effect agents.** |
| **HIGH** | **#548** | READER_RACE (2°: ORPHAN_PROCESS) | OPEN, `status-in-progress`. Root cause of the entire agent-side reader-race family. Child procs (hooks, MCP servers) inherit claude's stdout fd 1; kernel never EOFs; reader thread leaks; the trailing `{"type":"result"}` line (sole source of cost/duration/turns/session-id) is lost. The proposed `FD_CLOEXEC` fix is **necessary-but-insufficient** (claude re-inherits fd 1 to its own grandchildren, outside Trinity's control) — needs an out-of-band completion channel. Salvage (#797 JSONL) only fires for timeout>600s tasks (`_JSONL_PERSIST_THRESHOLD_S=600`); short fan-out reader-races still null-everything. |
| **MEDIUM** | **#333** | ORPHAN_PROCESS (2°: READER_RACE) | OPEN, `status-ready`, P2. After 2-6 days uptime, 7 containers at 50%+ CPU, 100% futex spin, zombie claude procs. Only the unbounded-history leak was capped (`_DEFAULT_HISTORY_LIMIT=1000`); futex/zombie root cause unidentified. Substrate is the leaked daemon threads from #548 accumulating over uptime → containers still need periodic restart. Detected by canary R-01, not cured. |
| **MEDIUM** | **#429** | POINT_FIX_ACCRETION (2°: SPLIT_STATE_AUTHORITY, PARALLEL_CODE_PATHS) | OPEN. The 9-reconciliation-path / ~12-status-writer pyramid is intact (#1035 reorganized the *code* into 13 named sweeps, preserving every path). **Gate is NOT met:** #429's body mislabels its dependency as #306, but #306 shipped only the WebSocket transport — the real prerequisite is **#307 (OPEN)**. Ripping out the watchdog early trades known bugs for unknown ones (the "DO NOT SHIP EARLY" warning is in force). |
| **MEDIUM** | **#307** | (enabling: PUSH_DISPATCH_BLOCKING) | Liveness layer **shipped 2026-05-30**, BUT the dispatch breaker's `record_failure("missed_heartbeat")` seam is **unwired** — the string appears only inside `dispatch_breaker.py`, no consumer calls it. So the breaker is **still polling**; dormant recovery is still a 1h TTL guess, not a liveness signal. #429 cannot proceed until this integration lands and soaks. |
| **MEDIUM** | **#792** | MISSING_IDEMPOTENCY (2°: PARALLEL_CODE_PATHS) | OPEN, P2. A switch on a one-shot trigger (manual/webhook/MCP) marks the execution FAILED and never replays; chat retries client-side and cron recovers next tick, but one-shots have no recovery path. The `idempotency_keys` primitive (#525) now exists to unblock it but no replay path is wired. |
| **MEDIUM** | **#799** | RACE_TOCTOU (2°: MISSING_IDEMPOTENCY, SPLIT_STATE_AUTHORITY) | OPEN, `status-ready`. No per-agent switch lock anywhere in `subscription_auto_switch.py`. A burst of N concurrent 429s on one agent fires N unsynchronized switches → wedged container / duplicate notifications / spurious `was_already_running`. |
| **LOW→MED** | **#767** | ORPHAN_PROCESS (2°: TTL_HEURISTIC) | Restructured upstream (#773 closes row immediately as `FAILED/CIRCUIT_OPEN`; #526/#986 moved the breaker into CapacityManager). The `CIRCUIT_OPEN` enum lineage carried forward intact. **Not regressed** — but the leaked-probe-row *class* depends on the breaker fast-fail discipline holding; listed for completeness. |
| **MEDIUM** | **#1085** | (herd / TTL_HEURISTIC-adjacent) | The thundering-herd risk: when many queued rows + freed slots coincide (drain wake-up, restart recovery), uncoordinated drain admits can stampede. Not in the verified corpus bodies but named in the lens; treat as a pull-migration design constraint (Postgres `SKIP LOCKED` claim semantics, #300, are the natural mitigation). |
| **HIGH (latent)** | **#408** | PUSH_DISPATCH_BLOCKING | **Closed prematurely** (2026-06-01, "shipped v0.6.0 via #998") but the synchronous long-await dispatch is **still live** at `task_execution_service.py:746` (timeout up to ~2h). A maintainer searching *open* issues will believe this is solved — recurrence trap. #914 is a relabel, not a cure. |

**Cross-cutting fragilities (no single issue #):**
- **Classifier drift** — `AUTH_INDICATORS`/`NON_AUTH_KILL_MARKERS` physically duplicated across **3 containers** (`subscription_auto_switch.py`, `scheduler/service.py`, agent-server `error_classifier.py`/`headless_executor.py`), hand-synced. The backend docstring *falsely* claims the scheduler imports the list — the drift is already visible in the comment. Next novel kill/OOM shape carrying an auth substring re-triggers the #361→#517→#904 false-switch pattern. (MISCLASSIFIED_FAILURE × PARALLEL_CODE_PATHS)
- **Non-atomic slot acquire** — `slot_service.py:132` ZCARD then `:141` ZADD, no Lua/WATCH; canary S-02 is a *detector* not a *guard*; concurrent admits can still overbook. (SPLIT_STATE_AUTHORITY)
- **`DEFAULT_SLOT_TTL_SECONDS=1200` fallback still live** (`slot_service.py:33`) — the #226 class can still bite if the slot metadata HASH expires before the ZSET member. (TTL_HEURISTIC)
- **Drain-sentinel swap** (`backlog_service.py:184-187`) — non-atomic `release_slot(sentinel)`→`acquire_slot(real_id)` is a fresh leaked-slot/overbook window inside the consolidation meant to reduce them. (SPLIT_STATE_AUTHORITY)
- **Backlog drain lazy import** still a string literal (`backlog_service.py:255`); AST test guards only the *name*, not the `chat↔backlog` cycle that forced it (#496 class). (PARALLEL_CODE_PATHS)
- **Sync-`sqlite3`-in-async-loop** — #904 RC-1 semaphore is an explicit accepted band-aid ("does NOT fix the underlying sync-DB problem"); a second long agent call still serializes on the SQLite writer lock. (POINT_FIX_ACCRETION)
- **Fail-open dedup degrades under load** (#525) — the dedup *guarantee* silently weakens during a duplicate-storm (DB stress), exactly when needed.

---

## What the Canary Harness Is COMPENSATING For

The canary (#411 epic, 10 invariants shipped via #653/#882, +3 planned in #1077) is **not a feature — it is the instrument that measures split-brain.** It exists because the orchestration bug class was **intractable by point-fix**: the same FAILED↔SUCCESS / leaked-slot / stuck-row pathologies kept recurring under each patch (#94→#219→#226→#378/#403; #106→#137/#201→#129), so the team built a permanent watcher instead of curing the cause.

Each invariant is a standing reconciliation check between **two stores that hold the same fact** with no transaction spanning them (Redis ZSET × SQLite row × Docker process registry × agent RAM):

| Invariant | Watches | Compensates for (class · bug) |
|-----------|---------|-------------------------------|
| **S-01** | Redis slot ZSET == SQL `running` rows (bijection) | SPLIT_STATE_AUTHORITY · #378/#403 phantom failure, #748/#749 leaked slots |
| **S-02** | `ZCARD ≤ max_parallel_tasks` (no overbooking) | SPLIT_STATE_AUTHORITY · the non-atomic acquire (`slot_service.py:132/141`) |
| **S-03** | slot TTL ≥ `timeout+300s` | TTL_HEURISTIC · #226 fixed-20-min, #913 scheduler shadow |
| **E-01** | no `running` past `timeout+300s` (closure) | SPLIT_STATE_AUTHORITY+TTL · #129 stuck executions |
| **E-02** | no terminal→non-terminal reversal | RACE_TOCTOU · #378/#403 |
| **E-05** | no `running >60s` with NULL session id | READER_RACE/ORPHAN · #106 silent launch failure |
| **B-01** | `get_queued_count` == independent id-count | SPLIT_STATE_AUTHORITY · queue re-split guard (trivially-green post-#428) |
| **B-02** | no queued + free-slots + stale drain-tick | SPLIT_STATE_AUTHORITY · #496 invisibly-dead drain |
| **L-03** | no live row referencing missing agent | cascade/MISSING_IDEMPOTENCY · #129 orphan refs |
| **R-01** | no zombie `claude` in agent containers | ORPHAN_PROCESS · #407, #333 |

**Meta-signal:** the harness needs its *own* point-fixes — S-03 decay-invariance (commit `d2148677`) and B-02 boot-window (`659df68f`) both shipped buggy and were patched post-merge. POINT_FIX_ACCRETION reproduced one level up. **The deepest risk: the harness's existence becomes an excuse to defer the structural fix** — as long as the canary catches divergence, the split-state authority underneath survives. (#748/#749 were *found by canary S-01*, not in production — the harness is the active line of defense, which is containment, not cure.)

### What happens to each invariant under pull (#1081)

The canary's eventual **shrinkage is the success metric for the redesign** — each invariant the consolidation makes structurally impossible is one the harness can delete:

| Invariant | Fate under pull | Mechanism |
|-----------|----------------|-----------|
| **S-01** | **RETIRED by #1082** (explicit AC: "documented as redundant once single-owner holds") | status becomes a CAS-guarded projection of one terminal event → Redis can't disagree with SQL because there's one owner |
| **S-02** | Retired/weakened | slot held by the worker, not a separate ZSET to overbook |
| **S-03** | **Retired** | no slot TTL — worker holds a lease, released by the worker; the decaying-TTL guess disappears |
| **E-01** | **Becomes lease-expiry**, not a timeout-closure check | a crashed worker's lease expires (real liveness signal); E-01's `timeout+300s` heuristic is replaced by lease semantics |
| **E-02** | Retired with #1082 | single-writer projection makes terminal reversal structurally impossible |
| **E-05** | Survives (narrowed) | still want to catch a silent-launch worker; but reframed as "claimed lease, no session" |
| **B-01** | Already trivially-green (post-#428); fully retired under one durable queue | no secondary queue representation |
| **B-02** | **CHANGES MEANING** | "drain alive" 60s-heartbeat heuristic disappears (no push-drain); becomes "queue has claimable rows but no worker is pulling" — a worker-liveness/herd check (#1085), not a drain-tick check |
| **L-03** | Survives | cascade integrity is independent of push/pull |
| **R-01** | Survives | zombie-process reaping is an agent-container concern regardless of dispatch model |

---

## Still Fragile (after all shipped fixes)

1. **The push-dispatch blocking model is intact** — `task_execution_service.py:746` still `await`s the agent for the whole turn (up to ~2h). This is the **TRUE original sin** across 8 of 11 families (slot/capacity, breakers, cleanup pyramid, timeout, state-machine, scheduler, idempotency, subscription). Every TTL, every sweep, every breaker, every canary invariant is *compensation* for it. #408 is closed but the code is live.
2. **No single owner of "is-running"** — split across Redis ZSET + SQL row + agent RAM + OS process table; ~12 status writers across ~35 SQL comparison sites; #524 shipped only per-write CAS guards, the `ExecutionStateProjector` is **deferred to #1082 (OPEN)**.
3. **No completion signal** — the backend infers liveness from a timeout clock, not a push. #307 shipped the liveness layer but the breaker integration is **unwired**.
4. **Two timeout columns coexist** (`agent_ownership.execution_timeout_seconds` vs `agent_schedules.timeout_seconds`) — #913 made the schedule column NULL-inherit, #929 added a write-time guard, but the runtime `min()` at `task_execution_service.py:127` is still live and a new writer bypassing the validator re-opens the silent-truncation trap.
5. **Effect-scoped idempotency absent** (#1084) — the gate on safe pull re-delivery.
6. **Three concurrency authorities** still live — slot ZSET cap, fan-out `asyncio.Semaphore` (`fan_out_service.py:107`), loop sequential-by-construction — uncoordinated; fan-out can admit past its semaphore into backlog/reject.
7. **Both breakers fail-open** — a Redis blip blinds transport + dispatch breakers; a genuinely sick agent gets full dispatch during the outage.

---

## Ordered Forward Recommendations

**Sequencing rationale:** cheap-and-isolated diagnostic fixes first (stop the bleeding on the largest production failure class), then the gating prerequisites for the structural redesign, then the redesign itself. The corpus is unanimous that the redesign (#1081 pull) is the only *cure*; everything before it is containment that buys safety margin.

1. **[HIGH, cheap, isolated] Fix #1022 now.** Wrap the dispatch POST in `try/except httpx.TimeoutException`, emit `error_msg = str(e) or type(e).__name__` + a structured `error_code`, and **don't finalize** — leave the row RUNNING for the background poll to resolve. Lift the hardcoded `30.0` into `SchedulerConfig`. This kills the **largest production failure class** without touching the dispatch model. (MISCLASSIFIED_FAILURE; the PUSH trigger is separate.)

2. **[HIGH, P1] Fix #1037 — gate `_restart_agent` on CapacityManager, not Docker status.** Before any recreate, ask CapacityManager whether the agent has running slots; defer-when-busy / bounded-drain, or inject the token without a full recreate. This stops auto-switch from destroying in-flight work across all 3 reachable paths (auto-switch, manual assign, rebuild/deploy). Add the #799 per-agent switch lock in the same PR (RACE_TOCTOU). Now-possible because #428 unified the slot-count surface.

3. **[MEDIUM, low-effort] Unify the failure classifier into one shared package.** Eliminate the 3-container `AUTH_INDICATORS`/`NON_AUTH_KILL_MARKERS` duplication and the stale "scheduler imports this" docstring. This is the recurring seed of #361→#517→#904; a shared module ends the treadmill. (PARALLEL_CODE_PATHS × MISCLASSIFIED_FAILURE.)

4. **[MEDIUM] Wire #307's heartbeat into the dispatch breaker.** The liveness layer shipped 2026-05-30; connect `record_failure("missed_heartbeat")` to a real heartbeat consumer so the breaker stops polling and dormant recovery becomes liveness-driven, not a 1h TTL guess. **This is the unmet prerequisite for #429** — and the corpus shows #429's gate has been mislabeled (#306≠#307) by the issue body itself, so flag and correct that dependency first.

5. **[HIGH, gating] Ship #1084 effect-scoped idempotency.** Thread `{execution_id}:{effect_ordinal}` keys at every outbound sink (email/Slack/Nevermined/git/MCP outbound). This is the **non-negotiable gate** on defaulting pull mode ON for any side-effect-bearing agent (per the 2026-06-05 decision). Until it lands, pull is safe only for read/analysis-only agents.

6. **[HIGH, gating] Migrate the queue to Postgres (#300).** Per the 2026-06-05 decision, Postgres must precede the queue carrying the fleet. `SKIP LOCKED` claim semantics are the natural mitigation for the #1085 herd risk and give the single durable queue that collapses the three concurrency authorities (slot ZSET + fan-out semaphore + loop handle).

7. **[STRUCTURAL] Land #1082 (status-as-projection) as bankable-win #1.** CAS-guarded single-owner `status` projection + audit every reader that treats `status` as authoritative liveness. This **retires S-01/S-02/E-02** and is explicitly scoped to ship *independently* of the push→pull migration — the highest-leverage structural fix available before the full redesign.

8. **[STRUCTURAL] #1083 fire-and-forget dispatch, then #429 cleanup-collapse.** #1083 stops the backend pinning a coroutine for the whole turn (only safe after #307 + #1082); #429 collapses the 9-path pyramid to one lease-reaper (only safe after push-completion soaks ≥2 weeks with zero orphan recoveries). **DO NOT ship #429 early** — the corpus is explicit that ripping out the watchdog before the lease model soaks trades known bugs for unknown ones.

9. **[STRUCTURAL, terminal] #1081 pull / work-stealing redesign.** The cure. Agents pull from the durable queue and own their lifecycle; "is-running" becomes one lease; the slot ZSET, per-slot TTL, watchdog, re-verify ping, two orphan sweeps, both breakers' transport half, and canary S-01/S-02/S-03/B-02 collapse into a single authoritative completion signal. Gated on #1084 + #300 + #307 + #1082 all landing and soaking. Complete #1068/#1074 (drop per-task `timeout_seconds`) as in-flight pre-work so the timeout becomes one envelope-level `deadline`.

**The through-line:** items 1-4 are containment (stop bleeding, end the classifier treadmill, make the breaker liveness-driven); items 5-6 are the gates; items 7-9 are the structural cure in dependency order. The corpus's strongest finding is that **every family traces to the same two co-original-sins — push-dispatch blocking + no single owner of "is-running"** — and only the pull redesign addresses both; until then, the canary harness is the load-bearing safety net and must not be retired ahead of the redesign that makes its invariants structurally impossible to violate.



---

# Appendix — Per-Family Forensic Reports (raw)

*The 11 family analyses verbatim (analyzer + adversarial verifier). Each catalogs the load-bearing bugs with symptom / root_cause / primary_class / fix_shape / recurrence. These are the source material the synthesis lenses draw on.*

#378's root cause is confirmed as a TOCTOU race between cleanup-watchdog and completion — and the fail-path it races against is precisely the one #219 introduced (`fail_stale_slot_execution`). The analysis's "#219's correlation caused #378" is a reasonable causal read, though #378's body frames it as a hypothesis rather than definitively citing #219. The analysis's classification of #378 as primary SPLIT_STATE_AUTHORITY (not RACE_TOCTOU) is correct per the "prefer deeper cause" rule — the race only exists because is-running lives in 3 stores.

I now have enough to deliver the corrected analysis. Summary of what I verified and what needs correction:

**Confirmed solid:** #90, #219, #226, #378, #749, #428/#527 classifications; all residual-risk code claims (non-atomic acquire line 132/141, TTL fallback line 33, drain-sentinel swap lines 184-187); the #428→#306 dependency quote; the design pathology.

**Needs correction:**
1. **#913 primary class** — should be SPLIT_STATE_AUTHORITY (two timeout columns with no precedence, scheduler's shadows agent's; fix = NULL-inherit consolidation), not PARALLEL_CODE_PATHS. The fix_shape is also closer to structural-consolidation than point-guard.
2. **#748 is a missing standalone bug** — fixed separately by PR #812, not folded into #814. The analysis only mentions it as a sibling inside #749's "recurred" field. It deserves its own entry.
3. **#904 mention is imprecise** — its real tie to this family is PUSH_DISPATCH_BLOCKING (worker saturation), not "SIGKILL misclassification → same seam as #90." The misclassification (exit -9 → auth) is MISCLASSIFIED_FAILURE and belongs to a different family seam.
4. Minor: #226's "DEFAULT 1200 still bites when HASH outlives less than the ZSET" residual-risk wording is garbled.

Here is the corrected analysis.

---

## FAMILY: Slot / capacity accounting & overbooking

### Bugs

- **#90 — Executions stuck "running" (scheduler/backend DB split)**
  - symptom: ~14 of 15 orphaned executions silently stuck `status="running"`, `claude_session_id=NULL`, `duration_ms=NULL` forever; no error logged.
  - root_cause: The scheduler created the execution row in its *own* SQLite DB; the backend skipped row creation when handed an `execution_id`, then issued terminal `update_execution_status` against its own (empty) DB — a silent no-op. The same fact (this execution exists / its status) lived in two SQLite files.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: PARALLEL_CODE_PATHS
  - fix_shape: structural-consolidation (both processes repointed to one `/data/trinity.db`; `src/scheduler/config.py:32` `DATABASE_PATH` default `/data/trinity.db` confirmed still in place)
  - recurred: yes — the Redis-ZSET-vs-SQL split (#219/#226/#378/#748/#749/#869) is the same disease on the slot side; #913 (timeout authority split across the scheduler→backend boundary) hits the same seam. (Corrected: #904 is **not** a clean recurrence of this seam — see Verifier notes.)

- **#219 — Cleanup reclaims stale slots but never fails the SQL row**
  - symptom: logs show `stale_slots: 1` but `stale_executions: 0`; slot freed in Redis, execution row stays "running"/`session_id="dispatched"` forever (scheduler polled one 234 times).
  - root_cause: Slot liveness (Redis ZSET) and execution liveness (SQL row) are two stores; the cleanup sweep touched only one. The "is-this-running" fact was split and the two halves were reconciled by *nothing*.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: ORPHAN_PROCESS
  - fix_shape: new-sweep-or-path (`fail_stale_slot_execution()` correlating Redis reclaim → guarded SQL `WHERE status='running'` update)
  - recurred: yes — the correlation it added then *caused* the #378 phantom-failure race; the PR itself spun off #226.

- **#226 — Stale-slot sweep uses fixed 20-min TTL regardless of agent timeout**
  - symptom: agents with 60–120 min timeouts have live slots reclaimed mid-run; the *acquire* path computed `timeout+buffer` correctly but the *sweep* used `DEFAULT_SLOT_TTL_SECONDS=1200`.
  - root_cause: A time-based guess (fixed 20-min) standing in for a real liveness signal — and two code paths (acquire vs sweep) computed that guess differently.
  - primary_class: TTL_HEURISTIC
  - secondary: PARALLEL_CODE_PATHS
  - fix_shape: point-guard (#323 threaded a per-agent `agent_timeouts` dict into the sweep)
  - recurred: yes — #869 re-broke the same way at a finer granularity (per-agent default still wrong for per-*schedule* timeouts), and #913 re-broke it again at the scheduler boundary (per-schedule `900` shadowing the per-agent value).

- **#378 — Execution shows Failed then Success ~14s later (cleanup race)**
  - symptom: scheduled run renders "Failed: Stale Execution Slot TTL Expired", then flips to "Success" with full output after refresh; false-positive failure.
  - root_cause: TOCTOU between cleanup Phase 0 (batch watchdog snapshot) and Phase 3 (slot reclaim) — the agent dropped a just-completed execution from its registry between phases, so `confirmed_running_ids` missed it and Phase 3 wrote FAILED (via the #219 `fail_stale_slot_execution` path) seconds before the agent's in-flight SUCCESS landed. The race only exists *because* "is-running" lives in agent RAM + Redis ZSET + SQL and they're reconciled by polling.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: RACE_TOCTOU, TTL_HEURISTIC
  - fix_shape: new-sweep-or-path (#403 added a just-in-time per-agent re-verify immediately before writing FAILED)
  - recurred: yes — re-verify is itself an HTTP ping that #869 then showed times out under load, *causing* false kills.

- **#748 — Startup recovery races concurrent in-flight `/internal/execute-task` → leaked Redis slot (SQL-ahead-of-Redis)**
  - symptom: `recover_orphaned_executions` (backend startup) marks a SQL row FAILED ("orphaned — recovered on backend restart") because its `agent:slots:{name}` ZSET membership is missing; ms later the still-late `/internal/execute_task` handler `acquire()`s the slot for that already-failed execution. A second uvicorn reload then kills the in-flight handler before its `finally`-block ZREM runs, leaking a Redis slot with no live SQL counterpart. Canary S-01 fired three consecutive cycles.
  - root_cause: Recovery and dispatch are *unordered* across the split — startup recovery scanned/patched SQL with no coordination against in-flight ZADDs from the scheduler, so a row could be failed and then have a slot born for it. The authoritative "is-running" fact was split across SQL + Redis with no atomic handshake at the startup boundary.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: RACE_TOCTOU (startup-recovery vs live-dispatch), ORPHAN_PROCESS
  - fix_shape: new-sweep-or-path / point-guard (#812 — `7b63018b fix(startup): close startup-recovery vs /internal/execute-task race`)
  - recurred: no direct re-break — but it is the **symmetric twin** of #749 (Redis-ahead-of-SQL): same incident (canary S-01, 2026-05-09), two opposite leak directions, two separate PRs (#812 for #748, #814 for #749). One split produced both.

- **#749 — Backend kill between ZADD and finally-block ZREM leaks Redis slots (Redis-ahead-of-SQL)**
  - symptom: uvicorn reload/SIGKILL between `acquire()` (ZADD) and `release()` (ZREM) leaves a Redis slot with no SQL counterpart; startup recovery scanned SQL→patched SQL but never scanned Redis. Slot survives until 1200s TTL. Canary S-01 fired 3 cycles straight.
  - root_cause: Recovery was *asymmetric* across the split — it reconciled SQL but had no Redis→SQL pass, so a Redis-ahead-of-SQL divergence had no reaper but the TTL clock.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: ORPHAN_PROCESS, RACE_TOCTOU (startup-recovery vs in-flight)
  - fix_shape: new-sweep-or-path (#814 added `_reconcile_orphaned_slots()` SCANning `agent:slots:*` with a 15s grace window)
  - recurred: no direct re-break — inverse of the same-incident sibling #748 (SQL-ahead-of-Redis); two symmetric leaks from one split.

- **#869 — Watchdog false-kills long-running executions before timeout**
  - symptom: runs at 50–66 min of a 7200s timeout (slot TTL 7500s) killed as "stale"; re-verify HTTP ping times out because the agent is at 3/3 capacity, so the watchdog force-fails a healthy run; retry then SIGKILLs from accumulated memory.
  - root_cause: Two compounding errors. (1) the sweep cutoff used the agent-ownership default (3600s) not the per-schedule `timeout_seconds=7200` — the #226 TTL-heuristic recurrence at finer granularity. (2) the JIT re-verify's 5s HTTP timeout is a liveness guess that a *busy* agent fails — "agent at full capacity" is indistinguishable from "agent dead" because dispatch is blocking push. Both substitute clocks/pings for a real completion signal.
  - primary_class: TTL_HEURISTIC
  - secondary: PUSH_DISPATCH_BLOCKING, SPLIT_STATE_AUTHORITY
  - fix_shape: point-guard (#871: per-slot metadata TTL + raise `WATCHDOG_HTTP_TIMEOUT` 5s→15s)
  - recurred: yes — the per-schedule-vs-per-agent timeout mismatch resurfaced as #913 (canary S-03/E-01) immediately after.

- **#913 — Scheduled runs ignore per-agent `execution_timeout_seconds` (canary S-03 + E-01)**
  - symptom: every agent with `execution_timeout_seconds != 900` continuously trips canary S-03 (slot TTL below floor) and E-01 (terminal-state closure); `PUT /api/agents/{name}/timeout` is silently ineffective for the scheduler path only.
  - root_cause: The execution-timeout fact lived in **two columns** — `agent_schedules.timeout_seconds` (scheduler) and `agent_ownership.execution_timeout_seconds` (the per-agent control) — **with no defined precedence**. The scheduler shipped a hardcoded `900` in its column, which silently *shadowed* the agent's value, and the backend trusted whatever the scheduler passed (the scheduler "does not have the per-agent value" — `scheduler/service.py:85`). The slot HASH + SQL row therefore carried 900 while cleanup *and* canary computed the floor from the per-agent value. One logical setting owned by two stores, reconciled by nothing.
  - primary_class: SPLIT_STATE_AUTHORITY *(corrected from PARALLEL_CODE_PATHS)*
  - secondary: PARALLEL_CODE_PATHS (chat/task read the per-agent value directly; the scheduler path didn't), TTL_HEURISTIC
  - fix_shape: structural-consolidation *(corrected from point-guard)* — #922 made `agent_schedules.timeout_seconds` **nullable with `NULL = inherit agent_ownership.execution_timeout_seconds`**, migrated legacy `900`/`3600` defaults to NULL (`_migrate_null_legacy_schedule_timeouts`), and moved resolution to the backend so the per-agent column is the single authority. The *separate* canary S-03 decay-invariance change (`ttl + age` initial-TTL reconstruction) is a true point-guard on the detector, not the producer.
  - recurred: open at the structural level — the canary fix concedes the producer TTL is a decaying guess and works around it rather than removing it. The slot HASH still carries a resolved integer, not a live deadline.

- **#428 / #527 — Consolidate ExecutionQueue + SlotService + BacklogService into CapacityManager**
  - symptom: not a bug — three primitives ("one primitive with two knobs expressed as three classes") with overlapping TTL/drain logic; every new trigger type had to pick among three APIs that drifted.
  - root_cause: accreted parallel implementations of one capacity gate.
  - primary_class: PARALLEL_CODE_PATHS
  - secondary: POINT_FIX_ACCRETION
  - fix_shape: structural-consolidation (one `CapacityManager` facade; `ExecutionQueue` deleted; Slot/Backlog kept as private internals — wire format, Redis keys, SQL columns unchanged)
  - recurred: contained, not cured — #748, #749, #869, #913 all post-date #428 and hit the *same* split-state/TTL machinery, now behind one facade.

### Family pattern
Every bug is a divergence between two (or three) stores that each claim to own a fact — "is this execution running / does it have capacity / what is its deadline": the Redis slot ZSET, the SQL `schedule_executions` row, the agent's in-RAM process registry, and (the case #90/#913 add) **two separate SQL columns/files for the same setting**. There is no single authority and no atomic transition across them, so the system runs *continuous reconciliation* — a cleanup watchdog, a JIT re-verify, two startup recovery sweeps (SQL→Redis and Redis→SQL, #748/#749), a 60s maintenance tick, and a canary harness — all clocks and polls standing in for a real completion signal. Each fix added or tuned another reconciler, and the next bug appeared in the gap between two of them.

### Design pathology
Capacity was modeled as a **TTL'd Redis ZSET decoupled from the durable SQL execution row**, with acquisition done by **non-atomic check-then-act** (`zcard` → conditional `zadd`, no Lua/WATCH — confirmed `slot_service.py:132` ZCARD then `:141` ZADD, no CAS between them; S-02 overbooking exists solely to *catch* the bypass). Because the slot has a TTL but the work has no liveness signal, a *time guess* became the de facto completion detector — and because dispatch is blocking push (backend holds the HTTP turn over multi-minute timeouts), "agent is busy" and "agent is dead" are indistinguishable to the watchdog. The same split also fractured the *control plane*: the execution-timeout setting itself lived in two stores (scheduler column vs agent column, #913; scheduler DB vs backend DB, #90) with no precedence. Splitting an authoritative fact across stores that can only be reconciled after the fact is what made this entire family possible; the fixed-20-min TTL was just the first guess in a chain (20-min → per-agent → per-slot → decay-invariant reconstruction).

### Residual risk
- **Acquire is still not atomic** — `slot_service.py:132/141` `zcard`-then-`zadd` has no CAS/Lua; S-02 overbooking is a *detector*, not a *guard*. Concurrent admits can still overbook under load.
- **The 1200s `DEFAULT_SLOT_TTL_SECONDS` fallback still lives** at `slot_service.py:33`, used when the per-slot metadata HASH expires *before* the ZSET member (the #226 class can still bite: a slot whose metadata HASH has been reaped falls back to the 20-min guess regardless of the agent's real timeout).
- **The drain-sentinel slot swap** (`backlog_service.py:184-187`: `release_slot(sentinel)` → `acquire_slot(real_id)`, non-atomic) is a fresh leaked-slot/overbook window inside the very consolidation meant to reduce them — between the ZREM and the re-ZADD another admit can claim the freed slot, and on `acquire` failure the row is re-queued (`backlog_service.py:196-198`).
- **#913 was patched at the producer but the canary was made decay-tolerant** — the harness now *accepts* a decaying TTL guess rather than the system carrying a true deadline; the heuristic is contained, not eliminated.
- Reconciliation correctness still depends on the agent's HTTP re-verify answering in time (15s) under full-capacity load — the #869 root condition is mitigated, not removed.
- **Two timeout columns coexist** even after #913 — `agent_schedules.timeout_seconds` is now NULL-by-default-inherits, but a non-NULL per-schedule value can still legitimately exceed/diverge from the per-agent cap; the #929 `schedule_timeout_exceeds_agent_cap` guard is a point-validation, not a single source of truth.

### Link to consolidation / pull redesign
#428/#527 collapsed the *three primitives* into one `CapacityManager` and gave the codebase a single drain path and one TTL reasoner — but explicitly preserved the wire format (same Redis keys, same SQL columns, same ZSET-vs-row split). It **contained** the parallel-code-paths half of the family and made future fixes land in one place; it did **not** cure the split-state-authority root, which is why #748, #749, #869, and #913 all post-date it. The #428 issue itself names the real fix: it *depends on #306* (push→event-driven completion) because — verbatim — "much of the current TTL/drain machinery exists to compensate for blocking HTTP dispatch; the consolidation gets much smaller once #306 moves state transitions to the event consumer." That is the bridge to the #1081 pull / work-stealing redesign: once an agent *pulls* work and reports completion through the event bus (#306) rather than the backend holding a blocking turn and inferring liveness from a slot clock, the slot ZSET, the per-slot TTL, the watchdog, the re-verify ping, the two orphan sweeps, and canary S-01/S-02/S-03 collapse into a single authoritative completion signal — removing the split that this entire family is built on. Until then, CapacityManager + the canary are the containment layer, not the cure.

### Verifier notes

Changed (against the repo):

1. **#913 primary_class: PARALLEL_CODE_PATHS → SPLIT_STATE_AUTHORITY; fix_shape: point-guard → structural-consolidation.** Verified via `git show 33023f51` (#922): the fix did not "thread the per-agent value into the scheduler path" — it made `agent_schedules.timeout_seconds` nullable where `NULL = inherit agent_ownership.execution_timeout_seconds`, migrated legacy `900`/`3600` rows to NULL (`_migrate_null_legacy_schedule_timeouts`), and moved resolution to the backend (the scheduler comment at `service.py:85` says it "does not have the per-agent value"). The true root is one setting owned by two columns with no precedence — the control-plane twin of #90's two-DB split — so SPLIT_STATE_AUTHORITY is the deeper cause per the "prefer the deeper cause" rule. PARALLEL_CODE_PATHS retained as secondary. The decay-invariant canary `ttl + age` change is a real point-guard, but on the *detector*, not the producer — separated out in the corrected entry.

2. **Added #748 as its own bug entry.** The original folded it into #749's "recurred: no" field as "the inverse sibling." `git log --grep=748` shows it was fixed by a **separate PR (#812 / commit `7b63018b`)**, not #814 (which fixed #749). #748's body documents a distinct startup-recovery-vs-in-flight TOCTOU producing a SQL-ahead-of-Redis leaked slot — a standalone bug that earns its own row (primary SPLIT_STATE_AUTHORITY, secondary RACE_TOCTOU). It and #749 are symmetric leaks from the same canary-S-01 incident (2026-05-09).

3. **Corrected the #904 reference inside #90's "recurred" field.** The original claimed #904 ("SIGKILL misclassification") "hit the same seam" as #90's split-DB. Verified #904's body: its tie to *this* family is **PUSH_DISPATCH_BLOCKING** (backend's 2 uvicorn workers held for 610s/1210s/2710s agent turns → worker saturation → frozen UI), and its misclassification facet (exit `-9` OOM reported as "subscription token expired", wrongly triggering SUB-003 auto-switch) is **MISCLASSIFIED_FAILURE** — a *different* family seam, not the scheduler/backend DB split. Removed #904 from #90's recurrence list and noted the correction; #904 is better placed in the dispatch-blocking / failure-classification family, not here.

Confirmed (held up under code inspection, no change):

- All residual-risk code claims: non-atomic acquire (`slot_service.py:132` ZCARD / `:141` ZADD, no Lua/WATCH); `DEFAULT_SLOT_TTL_SECONDS=1200` at `slot_service.py:33`; drain-sentinel swap at `backlog_service.py:184-187`.
- #90 structural consolidation persists (`scheduler/config.py:32` → `/data/trinity.db`).
- The #428→#306 dependency and "blocking HTTP dispatch" quote are verbatim in the #428 issue body.
- #90, #219, #226, #378, #428/#527 classifications and fix_shapes.
- #219→#378 causation (the #378 race is against the FAILED-write path #219 introduced) and #226→#869 TTL-granularity recurrence.


---

So `missed_heartbeat` is only referenced in `dispatch_breaker.py` itself — the seam **exists but is not yet wired to a heartbeat consumer that calls it**. So #307 shipped the *liveness layer* but the breaker's heartbeat-fed `record_failure("missed_heartbeat")` seam is **not yet connected**. The analyst's claim "the real cure (#307 heartbeat push) is unshipped; the breaker is still polling" is **half-right**: #307 the liveness layer shipped, but its integration into the breaker (replacing polling) did not. I'll correct this precisely rather than leave it as a flat "unshipped."

I now have full forensic grounding. Let me write the corrected analysis.

---

## FAMILY: Circuit breakers (transport + dispatch)

### Bugs

- **#304/#308 — Original circuit breaker (RELIABILITY-001), the design seed**
  - symptom: (feature, not bug) agent-to-agent HTTP calls had no failure isolation; a dead agent was retried indefinitely.
  - root_cause: introduced an **in-process** `_circuit_registry` of `CircuitState` with `failure_threshold=3`, constant `cooldown_seconds=30.0`, `last_failure_time` reset on *every* `record_failure()`, and **no failure classification** — any exception counted. Every later bug in this family is a direct consequence of one of those four choices. (Verified: `git show 4d849d85` — `+159` LOC into `agent_client.py`, "3 failures → 30s cooldown".)
  - primary_class: n/a (origin)
  - secondary: SPLIT_STATE_AUTHORITY, MISCLASSIFIED_FAILURE, TTL_HEURISTIC
  - fix_shape: n/a
  - recurred: yes — re-opened by #474, #631, #687 (the three independent latent defects it shipped with)

- **#474 — Dropped MCP sync connection trips the breaker (`[Errno 32] Broken pipe`)**
  - symptom: a healthy agent's breaker opens after a client-side disconnect; concurrent pollers on still-running executions then re-trip it, blocking all new dispatch while the container is fine.
  - root_cause: the breaker counted *application/transport noise* (EPIPE from a half-written response, `ReadTimeout` from concurrent pollers) as agent-health failures. The "what counts as a failure" question was never answered — every exception was a vote to open.
  - primary_class: MISCLASSIFIED_FAILURE
  - secondary: none
  - fix_shape: reclassification (#798: **verified** `CIRCUIT_FAILURE_EXCEPTIONS = (httpx.ConnectError, httpx.ConnectTimeout)` only — TCP unreachability; `TRANSIENT_TRANSPORT_EXCEPTIONS` [TimeoutException/ReadError/WriteError/RemoteProtocolError/PoolTimeout] surface as typed errors but skip `record_failure()`)
  - recurred: **yes — twice.** #798 narrowed it; #873 immediately reopened the same wound (sibling-collapse + subprocess pipe-drop), and #526's dispatch breaker exists partly because the transport classifier *can't* see auth-class failure.

- **#873 — Sibling-collapse + subprocess pipe-drop reclassification (#474 follow-up to #798)**
  - symptom: after #798 tightened the classifier, 9-of-10 concurrent transport drops on a healthy agent still tripped the breaker; subprocess early-exit returned 500/`[Errno 32]` that read adjacent to the 503 auth-switch path.
  - root_cause: #798 was correct but incomplete — when one caller evicts the pooled httpx client on a drop, siblings race to rebuild against a half-closed peer and see real `ConnectError`/`Timeout`, which #798's classifier *does* count. The classification rule was right; the concurrency around it wasn't. (Verified in `agent_client.py:805-855`: `_is_within_drop_grace()` + identity-checked `_client_pool.pop` eviction + `AgentConnectionDroppedError`.)
  - primary_class: MISCLASSIFIED_FAILURE
  - secondary: RACE_TOCTOU
  - fix_shape: new-sweep-or-path (process-local `_recent_drops` ~2s grace window + `AgentConnectionDroppedError` + identity-checked pool eviction)
  - recurred: contained, not cured — the grace map is explicitly **process-local** (per-worker), a deliberately accepted residual SPLIT_STATE_AUTHORITY.

- **#631 — Breaker flood → SQLite locks → UI dead for everyone**
  - symptom: an agent `Up` but with a dead :8080 server got probed every 30s by *both* uvicorn workers; 400+ failures over 3h, `database is locked` cascading into WebSocket auth, UI unresponsive for all users.
  - root_cause: three compounding defects — (1) per-process circuit state (each worker tripped independently, N× the rate); (2) fixed 30s cooldown, no backoff; (3) `monitoring_service` probed with **raw httpx, never consulting the circuit at all**, writing 4 `agent_health_checks` rows per cycle for the same dead agent. The same fact "is this circuit open" lived in two worker RAMs that never reconciled. (Verified via #698 commit body.)
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: TTL_HEURISTIC (fixed-cooldown probing). **[CORRECTED]** The third compounding defect is a **side-channel probe-write flood** (monitoring_service writing health rows off-circuit), which is *not* PUSH_DISPATCH_BLOCKING — nothing here holds a long agent-turn connection that ties up backend capacity. It's a SQLite write-contention consequence of the N×-probing + an off-circuit writer; it folds under SPLIT_STATE_AUTHORITY (two workers + an uncoordinated prober), not a distinct PUSH class.
  - fix_shape: structural-consolidation (#698: state → Redis hash `agent:circuit:{name}`, atomic Lua, `SET NX EX 10` probe-lock so one worker probes, exponential backoff 30→300s, dormant after 10 probes ~40min, dormant short-circuits health writes). (Verified: `git show f8114931`.)
  - recurred: **yes** — the dormant state #698 introduced *became* #921; and the in-process→Redis migration silently superseded #688 (below).

- **#687 — No half-open recovery: breaker stays open permanently under load**
  - symptom: a long CPU-heavy execution makes the 5s health probe time out → breaker opens; counter keeps climbing *even after the execution completes*; only `docker restart trinity-backend` resets it.
  - root_cause: `record_failure()` reset `last_failure_time` on *every* call (the original #304 line), so continuous probe failures (the #687 commit cites "cleanup re-verify, scheduler dispatches" as the failure sources) kept the cooldown clock pinned near zero — half-open was mathematically unreachable. A time-based cooldown standing in for a real recovery signal, defeated by its own clock. (Verified: `git show e5bc3b32` — the one-line `if self.state != "open":` guard.)
  - primary_class: TTL_HEURISTIC
  - secondary: MISCLASSIFIED_FAILURE (load-induced probe timeout counted as unhealth), PUSH_DISPATCH_BLOCKING (the long blocking turn *is* what starves the 5s probe and the timeout is then counted)
  - fix_shape: point-guard (#688: one-line `if self.state != "open": self.last_failure_time = ...`)
  - recurred: **yes — the fix itself was superseded 4 days later.** **[VERIFIED]** #688 patched the in-process field May 6 21:53; #698 moved all state to Redis Lua May 10 09:20, replacing the guarded field entirely. Point-fix on a structure that was about to be deleted.
  - **[CORRECTED — dropped a false secondary symptom]** The original analysis attached "Secondary: open breaker blocks cleanup re-verify → zombie `running` executions" to #687. **This is wrong on current code and wrong as a mechanism.** The *active watchdog* reconciler (`cleanup_service._reconcile_orphaned_executions` → `_get_agent_running_ids`, line 897) has used a **raw `httpx.AsyncClient`** against `http://agent-{name}:8000/api/executions/running` since its inception (#129) — it has **never** gone through the breaker-wrapped `get_agent_client`, so an open breaker cannot poison it. The one reconciler that *does* use `get_agent_client` (the startup `recover_orphaned_executions`, line 1247) catches `AgentClientError` (which `AgentCircuitOpenError` subclasses) and leaves `registry_ids` *empty* → executions fall through to `_recover_execution`, which marks them **FAILED**. An open breaker there causes **spurious over-recovery** (a still-running task wrongly failed during startup), the *opposite* of a zombie. The genuine zombie-`running` rows in this family come from a *different* mechanism — #767's leaked probe rows and #921's false-FAILED watchdog writes — not from the breaker blocking re-verify. The #687 commit's mention of "cleanup re-verify" is as a *failure source feeding* the breaker (pinning the clock), not as something the open breaker blocks.

- **#767 — CB probe executions left open until backend restart inflate the timeline**
  - symptom: a multi-minute red failure block on the execution timeline; probe `schedule_executions` rows sat in non-terminal state, then got stamped `completed_at = restart_time` by startup orphan cleanup.
  - root_cause: probes called `mark_execution_dispatched` (sets `claude_session_id='dispatched'`), opting out of the 60s no-session sweep; the 120-min stale sweep was the only reaper and it back-dated `completed_at` to wall-clock-now. A leaked execution row outliving the thing that should reap it. (Verified: #773 commit body — "left schedule_executions rows stuck in 'running'", two bugs: CB fast-fail + Python 3.11 `CancelledError`-as-`BaseException` on shutdown.)
  - primary_class: ORPHAN_PROCESS
  - secondary: TTL_HEURISTIC (relies on the 120-min stale sweep)
  - fix_shape: new-sweep-or-path (#773: `allow_request()` check before dispatch → close row immediately as `FAILED/CIRCUIT_OPEN`; explicit `CancelledError` handlers to close on shutdown)
  - recurred: **[CONFIRMED]** restructured upstream, not re-broken — #773's body names #526 as moving "the breaker upstream to CapacityManager," and #526/#986 delivered exactly that. The `TaskExecutionErrorCode.CIRCUIT_OPEN` enum member in `task_execution_service.py` is still annotated `#767`, confirming the lineage carried forward intact.

- **#921 — Dormant breaker silently fast-fails scheduled tasks for 14.5h**
  - symptom: slot saturation (3/3) → capacity rejections marked FAILED → breaker opens at 3 failures → dormant ~30–40min later → every scheduled execution fast-fails for ~14.5h with no notification, no admin reset, no auto-recovery, until incidental chat traffic reset it. Container healthy throughout.
  - root_cause: the dormant state #698 added to fix #631 had **no exit path** — "manual recovery required" with no manual mechanism. The cascade's *trigger* is a watchdog false-positive: the agent's in-`finally` `registry.unregister()` (in `claude_code.py`) runs **before** the backend writes `success`, so a single watchdog snapshot sees a completing execution as a false orphan, marks it FAILED, releases its slot, and those spurious failures saturate slots / open the breaker. (Verified: #924 commit body.)
  - primary_class: TTL_HEURISTIC (dormant is a time-based give-up with no liveness signal to return)
  - secondary: RACE_TOCTOU (the agent-`finally`-unregister vs backend-`success`-write watchdog race — note this race is in the **raw-httpx watchdog path**, NOT in the breaker's HTTP path; the breaker is the *downstream victim* of the spurious FAILEDs, not the cause), SPLIT_STATE_AUTHORITY (the agent process-registry vs the backend DB row are the two divergent stores), PUSH_DISPATCH_BLOCKING (long blocking turns saturate the 3 slots that make the whole cascade possible)
  - fix_shape: new-sweep-or-path + feature-flag (#924: 1h self-heal cooldown probe under the open-state probe-lock, operator-queue alert on dormant transition, admin reset endpoint, two-cycle watchdog confirmation via Redis sentinel)
  - recurred: **yes, structurally** — #924's backend two-cycle/Redis-sentinel watchdog fix was itself deleted by #934 (`182 added, 518 deleted`, net −336 LOC), which moved the race-close agent-side. The dormant→self-heal change persists. (Verified: `git show 14eb9de9`.)

- **#934 — Close the watchdog race agent-side (#921 simplification)**
  - symptom: (refactor) #924's backend Redis sentinel + two-cycle confirmation was complexity working around a race that lived on the agent side.
  - root_cause: the false-orphan race is the gap between agent `unregister()` (in `claude_code.py`'s `finally`) and the backend `success`-write; #924 compensated on the reader (backend) with a defer-a-cycle heuristic instead of closing it at the source. An agent-side `_recently_completed: dict[id, float]` buffer (5-min TTL) + a `recently_completed_ids` field on `GET /api/executions/running` + a union accessor (`_extract_agent_known_ids`, shared by both the periodic watchdog and startup recovery) removed the need entirely.
  - primary_class: POINT_FIX_ACCRETION (it *removes* accreted state)
  - secondary: SPLIT_STATE_AUTHORITY (the agent registry vs backend success-write were the two stores)
  - fix_shape: deletion / structural-consolidation (182 added, 518 deleted)
  - recurred: **[CORRECTED — a follow-up gap they'd miss]** the *first* #934 commit fixed only the periodic watchdog and **missed the startup-recovery path** (`recover_orphaned_executions` still read only the `executions` field). A `/review` on PR #934 caught it; a second commit in the same PR hoisted `_extract_agent_known_ids` and wired both call sites. Self-corrected within the PR, but worth noting the simplification shipped one revision before it was actually complete. No post-merge recurrence (current).

- **#526/#986 — Per-agent dispatch circuit breaker (RELIABILITY-007)**
  - symptom: (new breaker) an *auth-dead* agent (reachable, answers HTTP 503 / `error_code == AUTH`) poisoned the persistent backlog with doomed tasks the transport breaker couldn't see.
  - root_cause: the transport breaker keys on TCP reachability (post-#474) and therefore *cannot* see application-level auth death — the producer side needed its own machine fed by execution outcomes. The two breakers answer different questions ("can I reach it" vs "will dispatch succeed") and **must not share a counter** — a 503 mustn't move the transport counter and a TCP drop mustn't move the dispatch counter. (Verified: `dispatch_breaker.py` — `AUTH_ERROR_CODE = "auth"`, `record_outcome` counts AUTH only, separate `agent:dispatch:{name}` namespace + separate Lua; shared fail-open plumbing in `redis_breaker_util.py`.)
  - primary_class: MISCLASSIFIED_FAILURE (the underlying need: separate auth-death from transport-death cleanly)
  - secondary: SPLIT_STATE_AUTHORITY (deliberately separate namespace + separate Lua), MISSING_IDEMPOTENCY (no-enqueue invariant — never persist a doomed backlog row; the gate sits at the top of `CapacityManager.acquire()`, raising `CircuitOpen` before any slot/overflow write)
  - fix_shape: feature-flag + new structural path (AUTH-only counting, separate Redis namespace, shared plumbing extracted to `redis_breaker_util.py`, default OFF, both per-agent + global flags required)
  - recurred: too recent (merged Jun 2); the careful namespace/Lua isolation is an explicit hedge against the "two breakers contaminate one counter" recurrence class.

### Family pattern

**Every bug in this family is a re-litigation of one question: "what is a failure, and where does the truth about the breaker's state live?"** The original #304 breaker answered both wrong — it counted *every* exception (MISCLASSIFIED_FAILURE) and stored state in *per-worker RAM* (SPLIT_STATE_AUTHORITY), with a *time-based cooldown* (TTL_HEURISTIC) standing in for a real recovery signal. The next eight tickets are that triple replaying: #474/#873 re-narrow "what counts" (drop EPIPE, drop timeouts, drop sibling-collapse, then add a *second* breaker for AUTH); #631/#688/#698 fix "where state lives and how the clock behaves"; #767/#921 deal with the *consequences* of a breaker being open (leaked probe rows, no exit from dormant). The recurrence signature is unusually strong: #474→#798→#873 is one wound reopened twice, and #688 was *superseded by #698* four days after merge (verified May 6→May 10).

**One correction to the family's causal map:** the zombie/leaked-execution consequences in this family (#767, #921) are *adjacent to* the breaker but are not caused by the breaker poisoning the reconciliation path. The watchdog reconciler runs on **raw httpx, off-circuit, by design** — so it is immune to the breaker. #767's zombies are leaked *probe* rows (the breaker's own fast-fail executions, not closed); #921's zombies/false-FAILEDs come from the **agent-finally-unregister vs backend-success-write** race in that raw-httpx watchdog. The breaker is the *downstream victim* (fed spurious FAILEDs that open it), not the upstream poisoner.

### Design pathology

The original breaker was built as a **generic, in-process, count-everything** breaker bolted onto the agent HTTP client — modeled on a stateless-service breaker where "request failed" is unambiguous. But a Trinity agent call is a **long-lived blocking dispatch** (the backend holds an `httpx.AsyncClient(timeout=execution_timeout_seconds)` open for the entire Claude turn, up to 3600s), and "failure" is *richly typed* — TCP-unreachable, read-timeout-under-load, client-dropped EPIPE, 503-auth-dead, capacity-rejection — each needing a *different* verdict. One counter, fed by one undifferentiated exception funnel, stored per-worker, could never be right. The fixes converged on what should have been the original design: **two breakers in two Redis namespaces with two Lua state machines, each fed by exactly one failure class.** This *is* the true original sin — not a proximate one. The blocking dispatch model (PUSH_DISPATCH_BLOCKING) is the *environmental* pressure that makes the breaker necessary at all, but the *defect* the family keeps re-litigating is the count-everything/per-worker/time-based-cooldown triple baked into #304.

### Residual risk

- **The drop-grace map (`_recent_drops`) and the httpx client pool are still process-local** (#873) — the per-worker sibling-collapse neutralization is accepted as "good enough" because the pool is also per-worker, but it's an explicit, documented SPLIT_STATE remnant in an otherwise Redis-unified system.
- **Both breakers fail-open** — verified via `redis_breaker_util.fail_open()`; a Redis blip makes `allow_request()`/`allow_dispatch()` return True and `record_failure()`/`record_outcome()` no-op. Correct for availability, but a Redis outage *blinds* both breakers; a genuinely sick agent gets full dispatch during the blip.
- **Dormant is still a 1-hour TTL guess** (#924), not a liveness signal — the worst-case false-fail window dropped from 14.5h to ~1h but is still bounded by a clock, not by "the agent proved healthy." **[CORRECTED]** The intended cure — the #307 agent push-heartbeat liveness layer (RELIABILITY-004) — **did ship (2026-05-30)**, and the dispatch breaker exposes the `record_failure("missed_heartbeat")` seam for it. But that seam is **not yet wired to any heartbeat consumer** (the string `missed_heartbeat` appears only inside `dispatch_breaker.py`). So the breaker is still polling in practice; the heartbeat-fed transition is built but unconnected. The original analysis's flat "#307 is unshipped" is wrong — the correct statement is "#307 shipped the liveness layer but its integration into the breaker's recovery signal is incomplete."
- **The whole dispatch machine is opt-in / OFF by default** (#526) — the producer-side protection exists but isn't engaged fleet-wide, so backlog-poisoning is *containable* not *prevented* in the default config.

### Link to consolidation / pull redesign

This family is the producer-side mirror of the #428 CapacityManager consolidation. #773's own body names #526 as moving "the breaker upstream to CapacityManager," and #986 delivers exactly that (verified: `git show 0c671158 --stat` — dispatch gate added to `capacity_manager.py`, outcome-recording to `task_execution_service.py`): the dispatch gate now lives at the top of `CapacityManager.acquire()`, with the **no-enqueue invariant** (a doomed task is never written to the backlog — `acquire()` raises `CircuitOpen` before any slot/overflow work) as the whole point — the breaker is now part of the admit/slot path #428 unified, not a side-channel on the HTTP client. **The breaker is, structurally, compensation for PUSH_DISPATCH_BLOCKING.** Because the backend holds a connection for the entire agent turn, a slow/auth-dead agent ties up slots (the #921 saturation cascade) and the breaker is the bolt-on that detects and short-circuits that — #687's symptom ("probe times out *because* a long blocking turn pins the CPU") is the blocking model leaking through the breaker. Under the **#1081 pull / work-stealing redesign**, agents pull from a queue instead of being pushed long-held connections, so "is this agent reachable/auth-alive right now" stops being a precondition the producer must guess via a polled breaker — the agent simply doesn't pull while sick, the task waits, and the entire transport-breaker apparatus (probe-locks, dormant, backoff, drop-grace) loses most of its reason to exist. The dispatch breaker's job (don't admit doomed work) survives as a backlog-admission policy; the transport breaker is largely an artifact of the push model it was built to protect.

### Verifier notes

Changed the following against the repo; everything else held up and is preserved:

1. **Rejected the top-line forensic claim** ("cleanup re-verify shares the breaker-wrapped `get_agent_client` path, so an open breaker poisons stale-execution reconciliation — the zombie-execution secondary"). **The active watchdog reconciler uses raw `httpx.AsyncClient`, off-circuit, since #129** (`cleanup_service._get_agent_running_ids`, line 897, hits `http://agent-{name}:8000/...` directly). The one reconciler that *does* use the breaker-wrapped client (startup `recover_orphaned_executions`, line 1247) responds to an open breaker by *over-recovering* (marking running rows FAILED via `_recover_execution`), the **opposite** of leaving zombies. The analyst's stated "full forensic confidence" was misplaced.

2. **#687 — removed the false secondary symptom** "open breaker blocks cleanup re-verify → zombie running executions." Wrong mechanism (see #1). Replaced with a precise note that #687's "cleanup re-verify / scheduler dispatches" are *failure sources feeding* the breaker (pinning the cooldown clock), not paths the breaker blocks. The real zombie sources in this family are #767 (leaked probe rows) and #921 (false-FAILED watchdog writes).

3. **#631 — corrected the PUSH_DISPATCH_BLOCKING secondary.** The "probe-write flood" is `monitoring_service` writing health rows on raw httpx, off-circuit (per the #698 commit body) — a SQLite write-contention consequence of SPLIT_STATE (N× per-worker probing + an uncoordinated prober), **not** PUSH_DISPATCH_BLOCKING (which requires a long agent-turn connection tying up backend capacity). Folded it under SPLIT_STATE_AUTHORITY.

4. **#921 — sharpened the RACE_TOCTOU/SPLIT_STATE secondary** to name the race explicitly (agent `finally:unregister()` vs backend `success`-write) and to flag that this race lives in the **raw-httpx watchdog path**, making the breaker the *downstream victim* of the spurious FAILEDs, not their cause. The TTL_HEURISTIC primary stands.

5. **#934 — added the missed follow-up gap:** the first #934 commit fixed only the periodic watchdog and **missed the startup-recovery path**; a `/review` caught it and a second in-PR commit hoisted `_extract_agent_known_ids` to wire both call sites. Self-corrected within the PR, but the simplification was incomplete on its first revision.

6. **Residual risk — corrected "#307 heartbeat is unshipped."** #307 (agent push-heartbeat liveness layer, RELIABILITY-004) **shipped 2026-05-30**, and the dispatch breaker exposes a `record_failure("missed_heartbeat")` seam. But that seam is **not yet wired to any consumer** (`missed_heartbeat` appears only inside `dispatch_breaker.py`), so the breaker is still polling in practice. Corrected to "liveness layer shipped, breaker integration incomplete."

7. **Verified and left intact:** the #688→#698 four-day supersession (May 6 21:53 → May 10 09:20); `CIRCUIT_FAILURE_EXCEPTIONS = (ConnectError, ConnectTimeout)` exactly; #698 introduced dormant + 30→300s backoff + `SET NX EX 10` probe-lock + 10-probe→dormant; #526 AUTH-only counting in a separate `agent:dispatch:{name}` namespace with separate Lua; both breakers fail-open via `redis_breaker_util.fail_open`; #934 net −336 LOC; #767's `CIRCUIT_OPEN` enum lineage carried into #526/#986; the design-pathology and pull-redesign narrative. The primary_class assignments for #474, #873, #767, and #526 are correct as the deeper cause in each case.


---

The final piece. The #429 body literally lists **#306** as its dependency with the description "agent must push completion via Redis Streams so the event consumer owns the terminal status transition." But:
1. What actually shipped as #306 is the WebSocket delivery transport, NOT agent push-completion.
2. The agent push-completion / heartbeat work is **#307** (OPEN), which `dispatch_breaker.py` confirms is still a future "seam" ("#307 heartbeat — owns *wedged* — calls record_failure() seam").

So the #429 issue body itself has a **mislabeled dependency** — it says #306 but means the push-completion capability, which is #307. The analyst inherited this mislabeling and compounded it by declaring #306 "now CLOSED, dependency satisfied." The dependency is **not** satisfied: the push-completion root (#307) is open. The pyramid's gating condition is NOT met, contradicting the analysis's headline claim.

I now have everything needed. Writing the corrected analysis.

---

## FAMILY: Cleanup / watchdog / recovery pyramid

### Bugs

- **#129 — Stuck executions detected but never remediated**
  - symptom: Operators watched the monitoring service flag an execution "degraded" at 30 min, but nothing acted on it; the slot stayed wasted until passive 120-min cleanup, and a human had to hit Stop.
  - root_cause: "Is-this-execution-actually-running" lived in three stores (SQL `running` row, Redis slot, agent process registry) and only the agent registry was authoritative — but nothing reconciled them, so the SQL row and Redis slot drifted into a stuck state with no remediation loop.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: TTL_HEURISTIC, ORPHAN_PROCESS
  - fix_shape: new-sweep-or-path (added the active watchdog Phase 0 reconciliation — the first brick of the pyramid; shipped in PR #166)
  - recurred: yes — directly spawned #166, #219/#227, #378/#403, #497/#783 as the watchdog's own follow-on bugs

- **#106 — `skipped` and no-session `running` rows invisible to cleanup**
  - symptom: Executions sat in `skipped` for 5+ hours with no `completed_at`, and `running` rows with `claude_session_id=NULL` (silent launch failures) held capacity slots for the full 120 min.
  - root_cause: The stale-cleanup query only matched `WHERE status='running'` and used one 120-min timeout for every failure mode; a launch that never produced a session is dead-on-arrival but the heuristic couldn't tell it apart from a long-running task.
  - primary_class: TTL_HEURISTIC
  - secondary: SPLIT_STATE_AUTHORITY, ORPHAN_PROCESS
  - fix_shape: new-sweep-or-path (added `_sweep_no_session_executions` 60s + `_sweep_orphaned_skipped` — two more bricks)
  - recurred: **yes — CORRECTED.** The original analysis claimed "no direct re-open." The exact same no-session/skipped sweep area broke again: **#137 / #201** ("Cleanup service misses no-session running executions and doesn't terminal-state skipped executions"), commit `4b2a339c fix: Cleanup service misses no-session and skipped executions (#137) (#201)`. The two-sweep accretion did not hold.

- **#226 — Stale-slot cleanup uses a fixed TTL regardless of agent timeout** *(NEW — was missing as a standalone bug)*
  - symptom: A long-running execution under a non-default agent timeout was killed early because the slot-reclaim TTL was a hardcoded 20-min default, unrelated to the agent's configured `execution_timeout_seconds`.
  - root_cause: A single fixed time constant stood in for the real "how long can this legitimately run" signal — the canonical TTL_HEURISTIC failure. Surfaced during the #227 review of the SQL-write wiring.
  - primary_class: TTL_HEURISTIC
  - secondary: SPLIT_STATE_AUTHORITY
  - fix_shape: point-guard (`#323`: replace fixed 20-min default with per-agent slot TTL derived from the agent's timeout)
  - recurred: **yes** — the per-agent TTL fix was itself insufficient under load: **#869 / #871** ("Cleanup watchdog falsely kills long-running executions as stale before timeout expires") forced a *third* iteration — per-slot TTL read from slot metadata at ZADD time (commit `98574f37`), plus bumping `WATCHDOG_HTTP_TIMEOUT` 5s→15s "to handle agents under load." Three TTL revisions (20-min → per-agent → per-slot-metadata) over the same false-kill class make this the strongest TTL_HEURISTIC recurrence in the family — and the original analysis folded it into a one-line aside under #219/#227 rather than scoring it.

- **#128 / #165 — No startup recovery for crashed-mid-run executions**
  - symptom: After a backend crash/restart, `running` rows from before the crash stayed "running" in the UI and held Redis slots for 2 hours until passive cleanup caught them.
  - root_cause: The passive 120-min TTL was the only backstop for orphans, and it's far too slow for the crash case where the agent registry is the ground truth and is immediately checkable at boot.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: TTL_HEURISTIC, RACE_TOCTOU
  - fix_shape: new-sweep-or-path (added `recover_orphaned_executions` at startup — the third independent recovery surface, PR #165)
  - recurred: yes — this exact startup-recovery path is what #748 and #749 later broke

- **#219 / #227 — Slot reclaim doesn't fail the execution record**
  - symptom: Cleanup logs showed `stale_slots: 1` but `stale_executions: 0` — the Redis slot was freed but the SQL row stayed `running` forever, requiring manual cleanup.
  - root_cause: `SlotService` (Redis) and `schedule_executions` (SQL) are two stores for the same liveness fact, and slot reclamation updated only Redis; the SQL write was a separate, un-wired code path.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: PARALLEL_CODE_PATHS
  - fix_shape: point-guard (added `fail_stale_slot_execution` with a `WHERE status='running'` race guard, wiring Step 3 to also write SQL)
  - recurred: yes — wiring the SQL write directly created the FAILED→SUCCESS race fixed by #378, and the review of #227 immediately spawned **#226** (TTL mismatch, now scored separately above)

- **#286 / #324 — Error-context overwrite**
  - symptom: Every recovered execution's `error` field read "Stale execution — slot TTL expired" / "recovered by watchdog," erasing the real cause (expired token, OOM). Operators had to SSH in and grep backend logs to diagnose fleet-wide failures.
  - root_cause: The cleanup paths classify *every* unreported terminal as a generic "stale/orphan" failure because, from SQL+Redis alone, they cannot see *why* the agent stopped reporting — the real failure type lives only in the agent's log buffer.
  - primary_class: MISCLASSIFIED_FAILURE
  - secondary: SPLIT_STATE_AUTHORITY
  - fix_shape: new-sweep-or-path (added agent error-context fetch + `_get_execution_error` to fetch and prepend the real cause before overwriting; `ERROR_FETCH_TIMEOUT = 2.0s`, `MAX_ERROR_MESSAGE_LENGTH = 2000`)
  - recurred: no evidence (mitigated; but the generic message still wins whenever the agent is unreachable, i.e. exactly the cases that matter most)

- **#378 / #403 — Phantom stale-slot failure (FAILED then SUCCESS 14s later)**
  - symptom: A scheduled task flashed "Failed — slot TTL expired," then flipped to "Success" with full output ~14s later on refresh. Would have produced false failure alerts.
  - root_cause: Race between Phase 0's batch registry query and Phase 3's slot reclaim — the agent dropped a just-completed exec from its registry between the two phases, so `confirmed_running_ids` missed it and Phase 3 wrote FAILED milliseconds before the agent's in-flight SUCCESS landed. The race only exists because a TTL clock, not a completion signal, triggers the reclaim.
  - primary_class: RACE_TOCTOU
  - secondary: TTL_HEURISTIC, SPLIT_STATE_AUTHORITY
  - fix_shape: point-guard (added a just-in-time per-agent re-verify HTTP call immediately before writing FAILED + an observability WARNING for residual races; code at `_sweep_stale_slots` lines 610–716)
  - recurred: yes — the JIT re-verify's "unreachable" handling then caused #497 and #783
  - *Classification note (held up):* RACE_TOCTOU is correct as primary even under the "prefer the deeper cause" rule. The deeper cause (split state under a TTL trigger) is captured as secondaries; the *bug as filed* is a genuine TOCTOU between two phases of one sweep, and the fix is a CAS-shaped re-verify — the race is the load-bearing fact, not just a symptom of divergence.

- **#497 / #783 — JIT re-verify defers forever under load (zombie running rows)**
  - symptom: Under sustained 3/3-capacity fan-out load, the same exec IDs logged "agent unreachable during re-verify; Phase 1 is fallback" every cycle for up to 2 hours; new work bounced off "Agent at capacity" against slots that were already dead.
  - root_cause: The #378 patch treated "agent unreachable" identically to "still running" (`continue`), so the cleanup deferred indefinitely while the Redis slot was already TTL-reclaimed — a guard added to fix one race opened a liveness gap.
  - primary_class: POINT_FIX_ACCRETION
  - secondary: TTL_HEURISTIC, MISCLASSIFIED_FAILURE
  - fix_shape: point-guard (#783: the unreachable branch now force-fails via the race-guarded writer instead of deferring — confirmed at lines 624, 665–716)
  - recurred: yes — this *is* the recurrence of #378; the area broke again in the very next layer

- **#748 / #812 — Startup-recovery-vs-execute-task race (leaked slot)**
  - symptom: Canary S-01 fired three consecutive cycles: a Redis slot ZADDed for an execution that startup recovery had marked FAILED 25ms earlier. Slot leaked until TTL.
  - root_cause: Startup recovery (TOCTOU: check SQL, then write FAILED) ran concurrently with a late `/internal/execute-task` handler that ZADDs a slot *after* the row was failed — two writers to split state (SQL row + Redis slot) with no atomic coordination across the boot boundary.
  - primary_class: RACE_TOCTOU
  - secondary: SPLIT_STATE_AUTHORITY, MISSING_IDEMPOTENCY
  - fix_shape: point-guard (added `STARTUP_RECOVERY_GRACE_SECONDS=15` skip window + a "warming-up" gate on `/internal/execute-task`; confirmed at lines 38–41, 1294–1307)
  - recurred: yes — its mirror-image inverse is #749/#814, filed from the same incident

- **#749 / #814 — Backend killed between ZADD and ZREM (orphan slot, no SQL row)**
  - symptom: Canary S-01 detected Redis-only slot members with no matching/terminal SQL row after a uvicorn reload killed the in-flight handler before its `finally: release()` ran.
  - root_cause: A slot (Redis) outlives the SQL row and the handler that should reap it; startup recovery was asymmetric — it scanned SQL→Redis but never Redis→SQL, so a leaked slot was invisible.
  - primary_class: ORPHAN_PROCESS
  - secondary: SPLIT_STATE_AUTHORITY, RACE_TOCTOU
  - fix_shape: new-sweep-or-path (added `_reconcile_orphaned_slots` SCAN of `agent:slots:*` with its own `SLOT_RECOVERY_GRACE_SECONDS=15` window + `_DRAIN_SENTINEL_PREFIX` filter — yet another recovery surface; confirmed at lines 42–55, 1326–1369)
  - recurred: no evidence yet (but it added the 9th path and a third independent grace constant)

- **#429 — Collapse the 9-path pyramid to one lease-reaper (open)**
  - symptom: `schedule_executions.status` has 12 writers and 9 reconciliation paths, each existing because the layer below can't be trusted; the FAILED→SUCCESS race is mitigated, not eliminated.
  - root_cause: The pyramid itself is the bug — N point-fixes accreted into N independent sweeps over the same split state instead of one authoritative writer.
  - primary_class: POINT_FIX_ACCRETION
  - secondary: SPLIT_STATE_AUTHORITY, PARALLEL_CODE_PATHS
  - fix_shape: open-unfixed (structural-consolidation planned, explicitly gated behind agent push-completion soaking)
  - recurred: n/a
  - **CORRECTED — gating dependency is NOT satisfied.** The original analysis's headline claim ("#306 is now CLOSED/shipped… the dependency is satisfied, the pyramid's gate is met") is **wrong on the facts**:
    - **#306 is NOT agent push-completion.** #306 shipped is *"Redis Streams event bus for reliable **WebSocket delivery** (RELIABILITY-003)"* — the browser-facing real-time transport. `event_bus.py`'s own "Scope discipline (#306)" docstring states explicitly: *"This module is the WebSocket delivery layer. **Agent-push completion, heartbeat push**, and capacity consolidation (#307 / #428 / #429) **will reuse the same stream primitive in later sprints**."*
    - **The real push-completion gate is #307** — *"feat: Agent heartbeat push for fast failure detection (RELIABILITY-004)"* — which is **OPEN**. `dispatch_breaker.py` confirms #307 is still an unfilled future seam: *"#307 heartbeat — owns *wedged* — calls `record_failure()` seam."*
    - The **#429 issue body itself is mislabeled**: it lists "#306 — agent must push completion via Redis Streams so the event consumer owns the terminal status transition" as the dependency, but that *capability* is #307, not the #306 that actually shipped. The analyst inherited that mislabel and amplified it into "dependency satisfied." It is not. The watchdog pyramid's gating condition ("push-based completion has soaked, zero orphan recoveries for ≥2 weeks") **cannot have been met** because the push mechanism does not exist yet. The pyramid is load-bearing not just because #429 is open, but because its *prerequisite* (#307) is open.

- **#1035 / #1026 — Cleanup hotspot complexity debt**
  - symptom: `_run_cleanup_inner` was a ~290-line / cyclomatic-53 method running many sequential sweeps — unreviewable in one pass, unsafe for an AI agent to edit.
  - root_cause: Not a runtime bug — it's the *code-shape symptom* of the same accretion: every issue above bolted one more sweep into a single mega-method.
  - primary_class: POINT_FIX_ACCRETION
  - secondary: none
  - fix_shape: structural-consolidation (extracted each sweep into a `_sweep_*` strategy; orchestrator now ~25 lines dispatching 13 named sweeps — explicitly *behavior-preserving*, so the reconciliation paths still all exist; commit `fd6a9771`, #1026 parent OPEN)
  - recurred: no — but note it consolidated the *code*, not the *paths*; the pyramid is intact, just better-organized. (Minor: the live `_run_cleanup_inner` now lists 13 sweeps, most of which are retention/idempotency housekeeping, not liveness reconciliation — the "9 reconciliation paths" count is specifically the *liveness* subset, which remains accurate.)

### Family pattern
Every bug in this family is a reconciliation between the same liveness fact stored in three places — the SQL `schedule_executions.status` row, the Redis `agent:slots:{name}` ZSET, and the agent's in-RAM process registry. None of the three is authoritative on its own, so the platform compensates with a growing stack of time-based guesses (120-min stale, 60s no-session, 60s watchdog-min-age, 15s startup-grace, 15s slot-recovery-grace, per-slot TTL) that stand in for a real completion signal. Each guess opened a new race (TTL fires before completion, recovery races a late dispatch, kill between ZADD and ZREM), and each race was closed by adding another guard or another sweep — never by removing the divergence. The strongest meta-signal is the recurrence chain, which is **longer than the original analysis drew it**:
- **TTL line:** #106 → **#137/#201** → (slot TTL) #226 → **#869/#871** — the no-session sweep *and* the slot-TTL heuristic each broke a second time.
- **Race line:** #129 → #166 → #219/#227 → #378/#403 → #497/#783.
- **Startup line:** #128/#165 → #748/#812 → #749/#814.
The fix for each layer became the bug of the next.

### Design pathology
The original choice to make capacity/liveness a **Redis TTL'd slot decoupled from the SQL execution row, with the agent's RAM registry as a third silent authority, and no single owner of the terminal status transition** (`status` has 12 writers). This holds up under inspection — `task_execution_service.execute_task` does `response = await client.post(agent_url, …)` with `timeout` up to the agent's full `execution_timeout_seconds` (default 3600s), i.e. the backend **holds an HTTP connection for the entire agent turn and learns the outcome only when the POST returns or times out**. The agent push-completes nothing; the backend can never *know* an execution is done, only time out and reconcile after the fact. That forces a watchdog. The watchdog then needs grace windows to avoid racing live dispatches, which need re-verify, which needs unreachable-handling, which needs a startup variant, which needs a Redis-side variant. The pyramid is the inevitable shape of "reconcile three stores of the same fact under a TTL heuristic with no authoritative writer." PUSH_DISPATCH_BLOCKING is the **true** original sin here, not a proximate one — and crucially, the mechanism that would remove it (#307 heartbeat push) **has not shipped**, which is why the pyramid is still required.

### Residual risk
- The generic-error and TTL-expiry messages still win whenever the agent is unreachable during re-verify (#497/#783 force-fails with a synthetic message; #286/#324's real-error fetch fails exactly then) — so the worst incidents still produce uninformative records.
- Three separate, hand-tuned grace constants (`WATCHDOG_MIN_AGE_SECONDS=60`, `STARTUP_RECOVERY_GRACE_SECONDS=15`, `SLOT_RECOVERY_GRACE_SECONDS=15`) must stay mutually consistent with the slot TTL and the dispatch window; nothing enforces their relationship — drift re-opens the phantom-failure or leaked-slot class. The slot TTL itself has been revised **three times** (#226 → #323 → #869/#871) and `WATCHDOG_HTTP_TIMEOUT` once (5s→15s under load), so the constants are demonstrably unstable.
- The FAILED→SUCCESS race is *mitigated by JIT re-verify, not eliminated* — #403 itself ships an observability WARNING for "residual races that slip past the primary fix," i.e. the authors know it can still fire.
- The canary harness (S-01/S-02/S-03/B-02/R-01) is the real safety net now — it *detects* the divergence the pyramid is supposed to prevent. That's containment, not cure: the canary exists *because* the state is split. Notably, #748 and #749 were *found by canary S-01*, not in production — evidence the harness is the active line of defense.
- #1035 reduced code complexity but preserved all reconciliation paths verbatim, so the runtime risk surface is unchanged.

### Link to consolidation / pull redesign
This family is the single strongest argument for both consolidations. **#428 (CapacityManager, CLOSED/shipped)** already collapsed the slot/backlog/queue three-class pyramid on the *producer* side, making slot+backlog one facade — but the *reconciliation* pyramid on the consumer/cleanup side (#429) is still open: capacity is still TTL'd in Redis rather than recomputed from authoritative DB rows. #429's stated target — "**one** periodic reconciliation, agent wins, capacity recomputed from DB not TTL, single writer per status transition" — is exactly the cure for SPLIT_STATE_AUTHORITY + TTL_HEURISTIC.

**Corrected dependency story:** #429 is gated on **agent push-completion**, which the #429 body mislabels as "#306" but which is actually **#307 (RELIABILITY-004, OPEN)** — #306 (shipped) was only the WebSocket *transport* primitive that #307 will *reuse*. So #429's gate is **not** met: the push mechanism that removes the PUSH_DISPATCH_BLOCKING root does not exist yet. Until #307 lands and soaks, ripping out the watchdog would trade known bugs for unknown ones — exactly the "DO NOT SHIP EARLY" warning in the #429 body, which remains in force.

Under the **#1081 pull / work-stealing redesign** (Epic #1045, both OPEN; effect-idempotency #1084 is the gate), the agent pulls work and owns its own lifecycle, so "is-X-running" stops being a fact the backend has to reconcile across three stores — it becomes a single lease the worker holds and renews. A lease-reaper (one path) replaces all the reconciliation paths because there is one authoritative store and one writer. Until #429 ships — and it cannot ship before #307 — every constant and every sweep in `cleanup_service.py` remains load-bearing, and the canary harness is the only thing standing between the split state and a production incident.

### Verifier notes
What I changed, with repo evidence:

1. **#429 gating dependency — the analysis's central headline claim was factually wrong.** The original asserted "#306 (the gating dependency for #429) is CLOSED/shipped… the pyramid's gate is now met." Verified via `gh`: **#306** shipped is *"Redis Streams event bus for reliable WebSocket delivery (RELIABILITY-003)"* — the browser real-time transport, NOT agent push-completion. `src/backend/services/event_bus.py` lines 23–28 ("Scope discipline (#306)") explicitly carve out *"Agent-push completion, heartbeat push… (#307 / #428 / #429) will reuse the same stream primitive in later sprints."* The real push-completion gate is **#307 (RELIABILITY-004), which is OPEN** (`dispatch_breaker.py:13` confirms #307 is an unfilled "heartbeat seam"). The #429 issue body itself mislabels its dependency as #306; the analyst inherited and amplified the error into "dependency satisfied." Rewrote the #429 entry, the design-pathology section, and the consolidation-link section to reflect that the gate is **not** met.

2. **#106 "recurred: no" → "recurred: yes."** Missed recurrence: **#137/#201** ("Cleanup service misses no-session running executions and doesn't terminal-state skipped executions") re-broke the exact same sweep — commit `4b2a339c`. Verified via `gh issue view` (both CLOSED) and git log.

3. **Added #226 as a standalone scored bug (TTL_HEURISTIC).** The original buried it in a one-line aside under #219/#227. It is the canonical TTL_HEURISTIC bug in the family and it **recurred** (#869/#871): the slot TTL went fixed-20-min (#226) → per-agent (#323) → per-slot-metadata (#869/#871, commit `98574f37`), plus `WATCHDOG_HTTP_TIMEOUT` 5s→15s. Verified via git log + `cleanup_service.py:36`.

4. **Extended the family-pattern recurrence chains** to include the two missed TTL recurrences (#106→#137/#201; #226→#869/#871), strengthening the "fix-of-each-layer-is-bug-of-next" meta-signal.

5. **Confirmed (no change) the design pathology = PUSH_DISPATCH_BLOCKING as true original sin.** Verified `task_execution_service.py` does `await client.post(agent_url, timeout=…)` with timeout up to the full agent `execution_timeout_seconds` — the backend genuinely blocks on the turn and has no push signal.

6. **Confirmed (no change):** all primary_class assignments for #129, #128/#165, #219/#227, #286/#324, #378/#403, #497/#783, #748/#812, #749/#814, #1035 — including #378/#403 staying RACE_TOCTOU (the deeper split-state cause is correctly a secondary; the bug-as-filed is a genuine inter-phase TOCTOU). All grace constants, the `_reconcile_orphaned_slots` 9th path, the JIT re-verify force-fail, and the #1035 behavior-preserving decomposition were verified directly in `cleanup_service.py`. The "9 reconciliation paths / 12 writers" framing is the #429 body's own and is accurate for the liveness subset (the live orchestrator runs 13 total sweeps, most being retention housekeeping). PR numbers (#165, #166, #227, #324, #403, #783, #812, #814, #871, #323, #201) all confirmed MERGED.


---

Both residual-risk claims are verified in the live code: `_AUTO_RETRY_MAX_TIMEOUT_S = 300.0` with `min(effective_timeout, ...)` clamping, and the `2 * sync_effective_timeout` "Policy B" connection hold cap in `chat.py:1659`. These are indeed additional timeout-math copies derived from the same number.

I have completed my verification. The analysis is overwhelmingly well-supported by the repo. The classifications hold; causal chains (#326→#517, #665→#869/#913) are confirmed by timestamps and issue text; the cited commits exist; the residual-risk claims are real. I found only minor inaccuracies: (1) the #1068 deprecation mechanism gloss in the intro is wrong (it's "ignore + warn," not "honor-but-clamp with Redis counter"), and (2) #921 is mis-grouped into the agent-side orphan-sweep lineage when it's actually a backend-side CB-cascade bug from the same trigger. Here is the corrected analysis.

---

## FAMILY: Timeout handling & TTL math

### Bugs

- **#61 — Orphaned Claude processes accumulate after backend timeout**
  - symptom: After a backend timeout the DB row flips to `failed`, but the `claude` OS process keeps running indefinitely; operators found 6 concurrent claude procs (5 orphaned, ~800MB wasted), one "running" stuck 3+ hours; `cost: null`/`context_used: null`; `/terminate` returned "Execution not found in agent."
  - root_cause: The backend's only notion of "done" was its own `httpx` client returning; the long agent turn ran in a separate `docker exec` process tree the backend held no PID handle to, so timing out the *HTTP wait* abandoned (didn't kill) the work.
  - primary_class: ORPHAN_PROCESS
  - secondary: PUSH_DISPATCH_BLOCKING, TTL_HEURISTIC
  - fix_shape: new-sweep-or-path (added `terminate_execution_on_agent()` + cleanup-service reclaim; later a whole agent-side process-group/orphan-sweep subsystem)
  - recurred: yes, repeatedly — the terminate primitive was never reliable: #407 (zombie reaping/CPU spin), #517/#516 (misclassified kills), #618/#728/#808/#827 (pgid escapes via setsid/FD-detach/env-strip), #817/#857 (cgroup-walk sweep), #912 (false-kill on drain). [verified: #61 body, #326 PR; lineage titles all confirmed]

- **#326 — terminate orphaned processes on backend timeout (the #61 fix)**
  - symptom: (the fix) timeout handler now POSTs `/api/executions/{id}/terminate` to the agent before writing `failed`.
  - root_cause: Cures the symptom (leaked process) not the cause (no completion signal; backend guesses liveness from an HTTP clock). Best-effort terminate with a 5s timeout; "watchdog is safety net."
  - primary_class: ORPHAN_PROCESS
  - secondary: PUSH_DISPATCH_BLOCKING
  - fix_shape: new-sweep-or-path
  - recurred: yes — directly *spawned* #517 (merged 2026-04-26, twelve days after #326 merged 2026-04-14; #516 explicitly notes the signal-kill exit "still falls through" the same heuristic block #361 touched) and the entire orphan-sweep accretion chain. [verified: merge timestamps confirm #326 precedes #517]

- **#517 / #516 — SIGKILLed claude misreported as expired auth token**
  - symptom: Every timeout/OOM/operator-cancel tick surfaced a misleading "Subscription token may be expired — generate a new one with `claude setup-token`" 503, masking the real cause.
  - root_cause: A signal-killed subprocess (exit `<0`, or shell-encoded 130/137/143) emits zero tokens and no `result` message; the zero-token path fell through to the auth-fallback heuristic and was reported as auth failure instead of a kill. Neither heuristic checked the **signed return code first**.
  - primary_class: MISCLASSIFIED_FAILURE
  - secondary: ORPHAN_PROCESS
  - fix_shape: reclassification (`_classify_signal_exit()` consulted *before* auth heuristics → HTTP 504)
  - recurred: this *was* the recurrence of #61/#326 (the backend-driven terminate made SIGKILL the routine end-of-life); the SIGKILL message itself was then re-touched by #929. Same file/heuristic block as the prior #361 max-turns misclassification — the auth-fallback heuristic has now been patched at least twice (#361, #517). [verified: #516 body confirms "same shape as #361, same file, same heuristic block, different exit path"]

- **#99 — Per-agent configurable execution timeout (consistency across triggers)**
  - symptom: Same agent timed out at wildly different values by trigger path: Task API 120s, chat 120s, scheduler 900s, MCP 120s, paid 120s, plus four desynced infra TTLs (queue 600s, queue-wait 120s never-enforced, slot 1800s, cleanup 120min).
  - root_cause: Timeout was hardcoded independently in each of N parallel execution paths and in four separate infra layers; no single owned value, so "the agent's timeout" didn't exist as one fact.
  - primary_class: PARALLEL_CODE_PATHS
  - secondary: SPLIT_STATE_AUTHORITY, TTL_HEURISTIC
  - fix_shape: structural-consolidation (`agent_ownership.execution_timeout_seconds`; all paths read it; "slot TTL = agent timeout + 5min", "cleanup = max + 30min")
  - recurred: yes — the "all paths read it" claim was false for the scheduler (#913) and the derived infra TTLs kept drifting (#869, #913 S-03). [verified: #99 "Current State (Inconsistent)" table confirms five hardcoded path timeouts + four infra TTLs; PARALLEL_CODE_PATHS primary is correct — the deeper cause is N independent implementations, not a single fact split across two stores]

- **#665 — Bump default chat timeout 15m → 60m**
  - symptom: Long real-world tasks silently truncated at 900s and looked successful until users noticed cut-off output.
  - root_cause: Not a logic bug — a too-tight default. But raising it from 900→3600 broke the assumption baked into every downstream TTL heuristic (slot floor, cleanup window) that "timeout ≈ 900," directly setting up #869 and #913.
  - primary_class: TTL_HEURISTIC
  - secondary: PARALLEL_CODE_PATHS
  - fix_shape: point-guard (schema default + migration of rows still at 900)
  - recurred: yes — the default bump is explicitly cited as the trigger that made #869 (false-kill at ~65min) and #913 (S-03/E-01) reproducible in the field. [verified: #913 repro step 1 references "any agent created after the #665 default bump to 3600"]

- **#869 / #871 — Cleanup watchdog false-kills long-running executions before timeout**
  - symptom: Legitimate runs at 50–66min of a 7200s budget (slot TTL 7500s) killed as "stale, agent unreachable during re-verify"; recurring on the same agents, never completing.
  - root_cause: `_cleanup_stale_slots_for_agent` used one per-agent cutoff (`agent_ownership.execution_timeout_seconds + 300`, default 3600+300) via `ZREMRANGEBYSCORE`, reclaiming slots acquired with a *longer* per-schedule timeout (7200) at ~65min; the per-slot `timeout_seconds` was *already stored in the slot HASH at acquire time* but the cleanup code ignored it. The re-verify ping then timed out (busy agent at full slots, 5s HTTP timeout) and force-failed the live run.
  - primary_class: TTL_HEURISTIC
  - secondary: SPLIT_STATE_AUTHORITY (the per-slot deadline lived in the slot HASH but the watchdog read the per-agent default instead — divergence between two stores of the same "deadline" fact), PUSH_DISPATCH_BLOCKING (busy agent can't answer the 5s re-verify ping, so the time-cutoff guess can't be corrected by a real liveness check)
  - fix_shape: point-guard (per-slot metadata TTL instead of one per-agent cutoff; `WATCHDOG_HTTP_TIMEOUT` 5s→15s)
  - recurred: yes — PR #871 names its own predecessors #226 (per-agent TTL), #378 (JIT re-verify), #497 (force-fail unreachable), #749 (startup orphan sweep) in the same code, and the per-slot TTL it introduced immediately surfaced the S-03 decay false-positive. [verified: #871 PR + slot_service.py:150 confirms `timeout_seconds` stored in HASH at acquire; the bug was the cleanup path not reading it. TTL_HEURISTIC primary holds — the time-cutoff-as-liveness mechanism is the deeper cause; the agent-cap-vs-per-slot split is the *value* the heuristic used wrong, and PUSH_DISPATCH_BLOCKING is what made the false-positive unrecoverable. The analyst slightly underweighted PUSH_DISPATCH_BLOCKING; promoted from implicit to explicit secondary.]

- **#913 — Scheduled runs ignore per-agent execution_timeout_seconds (canary S-03 + E-01)**
  - symptom: `PUT /api/agents/{name}/timeout` silently ineffective for cron runs; canary fired S-03 (slot TTL below floor) and E-01 (terminal-state closure) every cycle on any agent whose timeout ≠ 900.
  - root_cause: The same fact ("this agent's execution timeout") lived in two columns — `agent_ownership.execution_timeout_seconds` (which `/timeout` writes) and `agent_schedules.timeout_seconds` (which the scheduler reads). `src/scheduler/database.py:76` coerced any falsy value back to 900, so the scheduler always passed a concrete int, making the per-agent fallback (`task_execution_service.py:281`) dead code on that path; cleanup used `ex.timeout_seconds or 900` for the same reason.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: PARALLEL_CODE_PATHS, TTL_HEURISTIC
  - fix_shape: structural-consolidation (round-trip `None` through ScheduleCreate/Schedule/scheduler boundary; drop `DEFAULT 3600`/`900` on the column; poll-deadline default 7200 on inherit)
  - recurred: yes — fix landed in three commits, needed a follow-up review guard (854758d2 "guard timeout-cap check on None"), and surfaced the S-03 decay false-positive that needed its own fix (d2148677). [verified: both commits exist with matching messages; #913 body confirms the two-column split + the `database.py:76` falsy-coercion]

- **#913 S-03 decay-invariance (d2148677) — canary false-positive on healthy slots**
  - symptom: After #913 made slot TTL match the floor exactly, S-03's `ttl < floor` tripped on every fresh slot ~1s after creation (Redis `TTL` decays linearly from `EXPIRE`).
  - root_cause: The invariant compared a *decaying* clock (current remaining TTL) against a *static* floor — a time-based proxy with no fixed reference point. Masked earlier only because #913's pre-fix 1200-vs-3900 gap was huge.
  - primary_class: TTL_HEURISTIC
  - secondary: none
  - fix_shape: point-guard (reconstruct initial TTL as `ttl + age` from the ZSET score; `floor - 1` rounding tolerance)
  - recurred: contained for now; it's a canary check on a TTL heuristic, so it lives only as long as TTL-based slot tracking does (slated for retirement by #429/#1081). [verified: commit d2148677 message matches the described decay-reconstruction fix]

- **#929 — schedule-vs-agent timeout precedence silent + ambiguous SIGKILL message**
  - symptom: Operator sets schedule `timeout_seconds=7200`, UI shows 7200, run is silently truncated to the lower agent cap (3600) and SIGKILLed; error message lists "schedule/agent timeout exceeded, OOM, or operator cancel" with no way to know which fired; `cleanup_service` shows `auto_terminated: 0` (kill happened in the exec path, not the watchdog).
  - root_cause: Two independently-configurable caps for one effective deadline reconciled silently as `min()` in `task_execution_service`, with no write-time validation and no surface exposing the effective value; the kill's cause was then reported as an unresolved 3-way disjunction.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: MISCLASSIFIED_FAILURE
  - fix_shape: point-guard / reclassification (Approach A: reject `schedule > agent` at config time with `schedule_timeout_exceeds_agent_cap`; reject agent-cap drop below active schedules; trim the SIGKILL message's disjunction since the agent cap can no longer silently truncate)
  - recurred: no NEW field recurrence yet (recent) — but note this is *itself* a partial recurrence of #913's two-column split (same `agent_ownership` vs `agent_schedules` timeout pair, attacked from the precedence angle rather than the dead-fallback angle), and it adds *another* guard (write-time validation) on top of the runtime `min()` rather than collapsing the two caps into one. The SIGKILL-message half is the third touch of the same error string (#517 → #929). [verified: `min()` confirmed at task_execution_service.py:127 comment + #929 body; architecture.md confirms the `schedule_timeout_exceeds_agent_cap` 400 shipped]

- **#1068 / #1074 — Drop per-task `timeout_seconds` override (pull-redesign pre-work)**
  - symptom: Not a field bug — `ParallelTaskRequest.timeout_seconds` (`models.py:90`, still live) is a *third* per-call override on top of the agent cap and schedule cap, breaking the actor-model invariant that the platform routes an envelope without reading inside the payload.
  - root_cause: Per-call timeout variance was never actually needed beyond agent/schedule caps; the override exists only because each parallel execution path grew its own knob.
  - primary_class: PARALLEL_CODE_PATHS
  - secondary: none
  - fix_shape: deprecate-then-delete (#1068 PR-1 of 6: **deprecate** — ignore the field for one release with a header/log warning, `extra="ignore"` fail-safe so in-flight `backlog_metadata` rows don't break on drain; #1074 hard-deletes after the soak window). [CORRECTED: the original said "#1068 deprecates: honor-but-clamp to agent cap with a Redis usage counter + soak gate" — that mechanism is not in the issue. #1068's acceptance criteria specify "ignore it / header warning" for one release, then delete. The Redis-usage-counter/soak-gate framing is the analyst's invention.]
  - recurred: open/in-progress — both #1068 and #1074 are OPEN issues (not PRs); the field still exists in `models.py` today. The deletion is gated on a soak window precisely because the additive-first migration must not break queued rows mid-flight. [verified: #1068/#1074 both OPEN issues; `ParallelTaskRequest.timeout_seconds` still present at models.py:90]

### Family pattern

Trinity has **no completion signal** for an agent turn — it has a *timeout clock standing in for one*. Because a long turn runs in a detached `docker exec` process tree the backend holds open via a blocking HTTP wait, the platform can only infer "this is done/dead" from "a clock expired." That one timeout number is then **copied into four downstream time-budgets** (HTTP wait, slot TTL, cleanup staleness window, canary floor) and **stored in two SQL columns** (agent cap, schedule cap) that must be kept equal by hand. Every bug in the family is one of: the clock fires but the process survives (#61/#326), the clock-kill is reported as the wrong failure (#517, #929), the four copies disagree (#99, #869, #913, S-03 decay), or the third redundant copy is being removed (#1068/#1074).

### Design pathology

The backend **pushes** a task and **blocks on a synchronous HTTP connection for the entire agent turn**, with no out-of-band liveness/completion channel back from the agent. Given that, a timeout *had* to become the universal liveness proxy — and a single proxy value then had to be replicated into every layer that needs to bound resources (connection, slot, watchdog, canary), across every trigger path that grew independently (chat/task/scheduler/MCP/fan-out/loop). Split state (two timeout columns) + parallel paths + a TTL standing in for a completion signal is the same root choice viewed from three angles. The orphan-process cure (#326) made it worse: it made SIGKILL the normal end-of-life for a timed-out turn, which is why a whole agent-side process-group/zombie-sweep subsystem (#407→#912) had to be grown to make "kill the process" reliable — pure POINT_FIX_ACCRETION.

### Residual risk

- **Slot/cleanup is still TTL-based.** #871 fixed *where* the TTL is read (per-slot metadata HASH) but kept the heuristic; #913's decay-invariance fix is a canary patch on that heuristic. The SPLIT_STATE between Redis ZSET score, slot HASH `timeout_seconds`, and the SQL row remains — it's only reconciled, not eliminated (#871 explicitly says #429 will "retire TTL-based slot tracking entirely"). [verified: #871 PR body]
- **Two timeout columns still exist** (#929 added write-time validation, not consolidation) — a future code path that writes a schedule timeout bypassing the validator re-opens the silent-`min()` trap. The `min()` reconciliation at `task_execution_service.py:127` is still live; #929 only guards the *write* boundary, not the runtime reconciliation.
- **`min(effective_timeout, ...)` on the inline auto-retry** (#678, `_AUTO_RETRY_MAX_TIMEOUT_S=300.0` at task_execution_service.py:130, clamped at lines 777/779) and the sync `/chat` "Policy B" connection hold (`sync_wait_cap = 2 * sync_effective_timeout` at chat.py:1659) are both new timeout-math derived from the same number — more copies to keep coherent. [verified: both present in live code]
- **#1074 deletion is gated on a soak window** — until #1068's deprecation has soaked one release, a third override path is still live; the fail-safe is `extra="ignore"` (silently drops the field), which is degrade-not-error but also invisible. (Both #1068 and #1074 are still open.)
- The orphan-sweep subsystem (cgroup-walk, allowlist) is **contained, not cured**: kills still race against setsid/FD/env-strip escapes, hence the steady #618/#728/#808/#827/#817/#857/#912 stream. Note #921 (CB dormant-state cascade) is a *related but distinct* failure — same trigger (long turns saturate slots), but the breakage is in the backend transport circuit breaker (`agent_client.py`), not the agent-side process sweep.

### Link to consolidation / pull redesign

- **#428 (CapacityManager consolidation)** folded SlotService + BacklogService behind one facade, but the slot TTL math the timeout family lives in is *internal* to that facade — #428 unified the *capacity* surface, not the *timeout/TTL* one. #871's note that #429 (CLEANUP-COLLAPSE) "will retire TTL-based slot tracking entirely" is the still-pending other half: this whole family persists until the TTL heuristic is removed, not just consolidated.
- **#1081 pull/work-stealing redesign** is the real cure. In a pull model the agent *claims* work and the platform learns completion from a returned result/ack on the queue, not from a blocking HTTP wait expiring — which removes the PUSH_DISPATCH_BLOCKING that forced timeout-as-liveness, removes the need to replicate the timeout into a slot TTL (the slot is held by the worker, released by the worker), and collapses the orphan problem (a crashed worker's claim is reclaimed by lease expiry, a real liveness signal, not a guessed completion deadline).
- **#1068/#1074 are explicit pre-work**: dropping the per-task override is step 1 of the 6-PR task-shape demotion (#945 actor-model postcard; map in `docs/planning/ACTOR_MODEL_TASK_DEMOTION_MAP.md`) so the timeout becomes a single envelope-level `deadline`. That collapses three timeout knobs toward one and is the migration bridge from "TTL heuristic per path" to "one deadline on the envelope" — the family's terminal state. [verified: #1068 body confirms PR-1-of-6 sequence and envelope-deadline target]

### Verifier notes

Pressure-tested every bug against the repo (issue/PR bodies, merge timestamps, live source, cited commits). The analysis held up strongly. Changes made:

1. **#1068/#1074 fix_shape & intro gloss — corrected a fabricated mechanism.** The original intro and bug entry claimed #1068 deprecates via "honor-but-clamp to agent cap with a Redis usage counter + soak gate." The actual issue (#1068 body, acceptance criteria) specifies **deprecate = ignore the field for one release with a header/log warning**, `extra="ignore"` fail-safe, then hard-delete in #1074 after soak. No Redis usage counter exists in the issue. Relabeled fix_shape to "deprecate-then-delete" and removed the invented counter. Also corrected that **both #1068 and #1074 are OPEN issues, not PRs** (the original "feature-flag → deletion" framing implied a flag mechanism that isn't there).

2. **#921 mis-grouped — split out of the orphan-sweep lineage.** The original listed the orphan-sweep chain as `#407→…→#921`. Verified #921 is a **transport circuit-breaker dormant-state cascade** in `agent_client.py` triggered by slot saturation — *not* an agent-side process-sweep escape like #618/#808/#817/#827/#857/#912. Same root trigger (long turns saturate slots), distinct failure surface. Removed #921 from the sweep lineage in the family pattern / design pathology / residual risk and added a one-line note distinguishing it.

3. **#869 — promoted PUSH_DISPATCH_BLOCKING to an explicit secondary and sharpened the SPLIT_STATE note.** Confirmed via `slot_service.py:150` that the per-slot `timeout_seconds` was *already stored in the HASH at acquire time*; the watchdog simply read the per-agent default instead — so the split is real (two stores of the "deadline" fact) and the busy-agent unreachable-ping (PUSH_DISPATCH_BLOCKING) is what made the false-positive unrecoverable. Kept TTL_HEURISTIC as primary (the time-cutoff-as-liveness mechanism is the deeper cause; the wrong value and the unanswerable ping are contributing). The original had PUSH_DISPATCH_BLOCKING only implicit.

4. **#929 — flagged it as a partial recurrence of #913's two-column split** (same column pair, different angle) and noted the SIGKILL message is the third touch of the same error string. The original said "no evidence of recurrence"; technically true for #929-as-field-bug but it *is* the same split-state authority re-surfacing, which I made explicit.

Confirmed without change: #61, #326, #517/#516 (causal "spawned by #326" verified by merge timestamps: #326 2026-04-14 → #517 2026-04-26), #99 (PARALLEL_CODE_PATHS primary correct — the inconsistency table shows N hardcoded paths, not a single split fact), #665, #913 (both cited commits 854758d2 and d2148677 exist and match), the S-03 decay entry, and all residual-risk timeout-math claims (`_AUTO_RETRY_MAX_TIMEOUT_S=300.0`, `2 * sync_effective_timeout` Policy B — both verified in live code). The core thesis (no completion signal → timeout-as-liveness proxy, replicated across paths and split across columns, made worse by the #326 orphan-cure) is correct and well-evidenced.


---

This confirms the "~12 writers" count (9 cleanup paths + TES + scheduler + retry/etc.) and that #429 is gated on agent-authoritative state (i.e., on #1082/#524's deferred half). Everything checks out.

I have everything needed. Here is the corrected analysis.

---

## FAMILY: Execution state machine / status writes

### Bugs

- **#524 — Agent-authoritative state machine w/ single-writer projection (RELIABILITY-005)**
  - symptom: `schedule_executions.status` has ~12 distinct writers (TES, cleanup phases 0/1/1b/1c/3, slot reclaim, backlog drain, scheduler, retry, terminate handler); FAILED→SUCCESS races and double-terminal writes were only *mitigated* by re-verify patches, not prevented.
  - root_cause: The agent isn't authoritative for its own lifecycle — the backend *guesses* state from outside the container and N independent code paths each write the same column with no single-writer guarantee. "is-running" lives in Redis ZSET + SQL row + agent RAM simultaneously.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: PARALLEL_CODE_PATHS, RACE_TOCTOU
  - fix_shape: **point-guard (scoped-down).** ⚠️ **CORRECTION:** #524 is **CLOSED/COMPLETED** (2026-06-01, shipped v0.6.0 via #998 — issue closed by PR #541). The maintainer **explicitly scoped it down**: the full `ExecutionStateProjector` (agent-push events, separate stream key, `projected_status` shadow column, restart reconciliation) was deferred as "complex and risky," and the CAS guards + stale/no-session TOCTOU fixes shipped *under #524* as the delivered scope. So #524's shipped fix_shape is point-guard, **not** "open-unfixed." The deferred projector half was **re-filed as #1082** (OPEN).
  - recurred: **the deferred half was re-filed, not regressed.** #1082 is the planned continuation of #524's deferred projector ("Bankable reliability win #1," explicitly independent of push→pull) and remains the gating contract for #429 cleanup-collapse. Not a recurrence-after-fix — a re-scoped remainder.

- **#541 — CAS guards on execution status writes (the PR that closed #524)**
  - symptom: A late agent "I'm done!" (SUCCESS) could overwrite a CANCELLED row, or a cleanup stale-timeout FAILED could clobber a real SUCCESS that arrived mid-sweep.
  - root_cause: `update_execution_status` and the stale/no-session sweeps did check-then-act with an unguarded `UPDATE` — no compare-and-set on the terminal transition, so a slower writer won.
  - primary_class: RACE_TOCTOU
  - secondary: SPLIT_STATE_AUTHORITY
  - fix_shape: point-guard — ⚠️ **CORRECTION (line numbers):** confirmed but the cited lines have shifted. SUCCESS write guarded `WHERE id = ? AND status != CANCELLED` (`db/schedules.py:1316`, blocks SUCCESS-over-CANCELLED per #671; SUCCESS still wins over RUNNING/QUEUED and even a phantom-stale FAILED per #378); non-success terminal write `WHERE status NOT IN (SUCCESS,FAILED,CANCELLED,SKIPPED)` (`:1331`); stale/no-session sweeps carry `WHERE … status = RUNNING` (`:1851`, `:1902`); `_recover_execution` routed through the already-guarded `mark_execution_failed_by_watchdog` (`cleanup_service.py:1020`, `:1315`).
  - relationship: ⚠️ **CORRECTION:** #541 is the **PR** (`pull_request: true`), not an independent bug. It is the implementation that **closed issue #524**. #524 (issue) and #541 (PR) are *one delivery*, not two separate family members. They are listed separately here only because the source analysis split them; the honest framing is "#524 was delivered as the CAS-guard scope via PR #541, with the projector deferred to #1082."
  - recurred: the guards are the floor #1082 explicitly builds on ("RELIABILITY-005 pattern"); the multi-writer topology they patch is still live. CAS is containment, not cure.

- **#1022 — Scheduler writes status='failed' with empty error on 30s dispatch timeout**
  - symptom: Synchronized cross-agent batches of silent `failed` rows — `error=''`, `response` NULL, `duration_ms≈30000` — giving operators zero diagnostics. Currently the largest failure class on a production instance.
  - root_cause: **Verified end-to-end.** The execution row is created with `status=RUNNING` *before* dispatch (`service.py:768`, `database.py:287`). The dispatch POST at `service.py:1090-1096` has **no try/except**; the `/api/internal/execute-task` handler is `async def` and in `async_mode` spawns a background task and returns `{"status":"accepted"}` in milliseconds (`internal.py:335-348`), so the 30s ceiling only fires when the **single backend worker's event loop is stalled** under concurrent cron fan-out and can't even reach the `return`. The `httpx.ReadTimeout` then propagates to the generic `except Exception as e: error_msg = str(e)` (`:928-929`). **Verified `str(httpx.ReadTimeout('')) == ''`** — httpx maps the transport exception via `message = str(exc); raise mapped_exc(message)`, and a bare read timeout carries no message. The outer handler's anti-overwrite guard (`current.status != RUNNING`, `:937`) does **not** save the row: the backend never *accepted* the dispatch, so the row is still `RUNNING`, the guard falls through to the `else`, and `update_execution_status(FAILED, error='')` writes the empty-error terminal row (`:946-950`).
  - primary_class: ⚠️ **CORRECTION — MISCLASSIFIED_FAILURE** (was PUSH_DISPATCH_BLOCKING). The *bug filed in #1022* is the **undiagnosable recording** — a transport timeout written as an application FAILED with no error string. That defect is self-contained and fixable without touching the dispatch model (wrap the POST, classify the timeout, emit a structured `error_code`, and **don't** finalize — let the background poll resolve the still-RUNNING row). PUSH_DISPATCH_BLOCKING is the *triggering condition* (event-loop contention is why the timeout fires at all) but it is a property the pull redesign removes separately; it is not the cause of the empty-error column. Demoting it to secondary follows "prefer the deeper cause of *this* bug."
  - secondary: PUSH_DISPATCH_BLOCKING (single-worker event-loop stall under cron fan-out is the trigger), TTL_HEURISTIC (the arbitrary hardcoded 30s ceiling)
  - fix_shape: open-unfixed (P2, `status-ready`, still OPEN; the dispatch POST at `service.py:1090-1096` still has no exception capture)
  - recurred: n/a — fix not landed; same `str(e)`-drops-diagnostic shape as the empty-error class #524's projector was meant to replace with a structured terminal event.

- **#1082 — status-as-projection (never read status back as "is running")**
  - symptom: cleanup + canary reconciliation machinery exists *only* because "is-running" is split across Redis slot ZSET, SQL row, and agent RAM; readers treat `status` as authoritative liveness and get split-brain.
  - root_cause: same as #524 — one fact (is-this-running) has three owners that must be reconciled; divergence is the bug. The fix is to make `status` a CAS-guarded projection of the terminal event and audit out every reader that uses it as the authoritative running signal.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: RACE_TOCTOU
  - fix_shape: open-unfixed (P1, OPEN — explicitly the re-statement of #524's **deferred** half, scoped to ship independently of the push→pull migration). ⚠️ **CORRECTION:** acceptance criteria are CAS-guard-only ("a stale writer can never clobber a terminal row" + "audit every reader" + "canary S-01 documented as redundant once single-owner holds") — it does **not** require the full agent-push projector either; it's the reader-discipline + CAS half, not the agent-authoritative-transport half.
  - recurred: this is the **re-filed deferred remainder of #524**, not a regression of a shipped fix. Canary S-01 is documented (in #1082's own ACs) as retirable once single-owner status lands.

- **#79 / #440 — Naive ISO timestamps (no Z) parsed as local time**
  - symptom: timestamps across the UI (Payments, chat, activity, schedule history) rendered in the server's timezone; `new Date(str)` parsed naive `datetime.utcnow().isoformat()` as browser-local.
  - root_cause: 41 backend modules emitted timezone-unmarked ISO strings; JS treats marker-less ISO as local.
  - primary_class: MISCLASSIFIED_FAILURE (a value's type/zone misinterpreted) — adjacent to the family via the timestamp-format defect
  - secondary: PARALLEL_CODE_PATHS (38+ duplicate `utcnow().isoformat()` callsites)
  - fix_shape: structural-consolidation — mass-replace with `utc_now_iso()` helper (appends `Z`); `parse_iso_timestamp` tolerates legacy naive labels. (#440 is the MERGED PR closing issue #79 — confirmed.)
  - recurred: partial — PR explicitly leaves pre-existing naive DB rows and `db/monitoring.py` patterns untouched (follow-up debt); same format-mismatch root then bit #476.

- **#476 — ISO-Z lexicographic comparison: rate-limit events never age out**
  - symptom: a single 429 marked a subscription rate-limited for the *entire UTC day*; `"no viable alternative subscription found"` persisted for hours instead of the designed 2h window. (Issue title confirms: "rate-limit events never age out due to SQLite string-compare bug; retries amplify outages.")
  - root_cause: stored timestamps are `…THH:MM:SS.ffffffZ` (T-separator, Z); SQLite's `datetime('now','-2 hours')` returns `… HH:MM:SS` (space, no Z). String-compared, `T`(0x54) > space(0x20) at position 10, so any event with *today's date prefix* passes the "last 2 hours" filter regardless of clock time.
  - primary_class: TTL_HEURISTIC (a broken time-window stands in for real liveness) — manifested as a format/comparison defect
  - secondary: MISSING_IDEMPOTENCY (the retry-amplification half — `max_retries`+overdue-retry burst on restart amplifies outages, per the issue title), PARALLEL_CODE_PATHS
  - fix_shape: point-guard hardened into a shared primitive — `iso_cutoff()` computes the cutoff in Python in stored format, passed as a bound param. **Verified adopted** in `db/subscriptions.py` (`record_rate_limit_event` → `iso_cutoff(2)`, `:474+`), and also present in `db/schedules.py`, `db/monitoring.py`, `db/audit.py`, `canary/snapshot.py`; codified as Invariant #16.
  - recurred: contained but not eradicated — `mark_stale_executions_failed`/`mark_no_session_executions_failed` still hand-roll `strftime('%Y-%m-%dT%H:%M:%S')` thresholds instead of `iso_cutoff` (**verified** at `db/schedules.py:1823` and `:1875`; correct today only because they bind a Python value, not `datetime('now')`). ⚠️ **CORRECTION:** the residual-risk claim that `db/agent_shared_files.py` "still uses raw `datetime('now')` comparisons on TEXT columns" as part of *this bug class* is **wrong** — that file wraps **both** sides in `datetime()` (`datetime(expires_at) < datetime('now')`, `:145-147`, `:206`, `:226`), which normalizes the format on both operands and is therefore **not** the #476 lexicographic bug. (Invariant #16 only governs the *unwrapped* raw-TEXT-vs-`datetime('now')` case.) The audit-log triggers still use `datetime('now')` but compare a `created_at` written by `datetime('now')` (same format), so they too are not #476-class.

- **Canary E-01 / E-02 / E-05 — compensating detectors for this family**
  - symptom: no operator-visible symptom — these are the *watchers* that exist because the writes can't be trusted. E-01: a `running` row past `timeout+300s` (cleanup never closed it); E-02: a terminal row re-appearing non-terminal (phantom reversal — the #378/#403 bug class); E-05: a `>60s running` row with `claude_session_id IS NULL` (silent launch failure).
  - root_cause: the multi-writer/split-state design (#524) makes terminal-state monotonicity un-guaranteed by construction, so a periodic reconciliation harness is required to *detect after the fact* what a single-writer projection would prevent.
  - primary_class: SPLIT_STATE_AUTHORITY (the invariants exist to reconcile split state)
  - secondary: ORPHAN_PROCESS (E-01 leaked running row), READER_RACE (E-05 no-session launch), RACE_TOCTOU (E-02 reversal)
  - fix_shape: new-sweep-or-path — deterministic 5-min invariant harness writing `canary_violations`. **Verified:** E-01 threshold `execution_timeout_seconds + SLOT_TTL_BUFFER_SECONDS (300)` (`canary/invariants/e01_terminal_state_closure.py:55,75`); E-05 60s tied to `mark_no_session_executions_failed`'s SLA (`e05_dispatched_rows_have_session.py:5-23`); E-02 keeps a Redis `canary:e02:terminal_seen` ZSET side-table to catch even silent direct-DB reversals (`e02_no_phantom_reversal.py:61`).
  - recurred: by design these are *evidence of* recurrence — S-01/E-02 are documented (in #1082's ACs) as retirable only once single-owner status lands.

### Family pattern

Every bug here is a symptom of the same disease: **`schedule_executions.status` is a multi-writer column the backend guesses at from outside the agent, while the single fact "is this execution running" is physically stored in three places at once** (Redis slot ZSET + SQL row + agent RAM). The backend never reads the agent's own status back as authoritative — it infers liveness from timeouts and slot bookkeeping, then writes the column from ~12 paths (enumerated in #429: 9 cleanup phases + TES + scheduler + retry). Divergence between the three stores *is* the bug, and every fix to date has been a guard on one writer (CAS — shipped under #524/#541), a sweep to detect drift after the fact (canary E-*), or a format fix on the time-heuristics those sweeps depend on (#476/#440) — never the removal of the multiple writers.

### Design pathology

The original choice that made the whole family possible: **the agent container is not authoritative for its own lifecycle, and "is-running" was never given a single owner.** Status was modeled as a value any subsystem may write whenever it forms an opinion (dispatch path, cleanup phase, slot reclaim, backlog drain, scheduler poll, retry, terminate), rather than as a write-once projection of an event emitted by the one actor that actually knows (the agent). Because the agent has no transport to the backend's source of truth (no Redis access — agents physically can't route to the platform network per Issue #589; confirmed as the reason #524's projector was deferred), the backend was forced into *external inference*: timeouts, TTLs, and lexicographic time-window queries standing in for a real completion signal. That inference layer is where #1022 (30s dispatch ceiling + empty-error recording), #476/#440 (ISO-Z time math), and the canary E-* heuristics all live. This is the TRUE original sin — endorsed verbatim in #524's body and the maintainer's own close comment ("the backend is guessing state from outside the agent and patching the gaps with timeouts").

### Residual risk

- **The cure is unbuilt, but #524 is closed.** ⚠️ **CORRECTION:** the CAS-guard + TOCTOU-fix *scope* of #524 **shipped** (v0.6.0); only the `ExecutionStateProjector` was deferred and re-filed as #1082 (OPEN, P1). Today's protection is CAS guards (containment) plus canary detection (after-the-fact) — both leave the ~12-writer topology intact. Do not characterize #524 as "open-unfixed"; characterize the **projector** as deferred-into-#1082.
- **#1022 is live and is the largest production failure class** — the 30s dispatch POST still has no exception capture; synchronized empty-error `failed` batches continue. Its primary defect is MISCLASSIFIED_FAILURE (transport timeout → empty-error application FAILED), independently fixable; the underlying single-worker event-loop stall that triggers it is a separate PUSH_DISPATCH_BLOCKING property the pull redesign must remove.
- **#476's bug class is contained, not eradicated** — `mark_stale_executions_failed`/`mark_no_session_executions_failed` still hand-roll ISO thresholds instead of `iso_cutoff` (correct today only because they bind a Python value). ⚠️ **CORRECTION:** `db/agent_shared_files.py` and the audit triggers are **not** part of this risk — they compare same-format operands (both `datetime()`-wrapped, or `created_at`-vs-`datetime('now')` both written by `datetime('now')`). Invariant #16 documents the rule but isn't mechanically enforced.
- **Canary readers themselves read `status` as authoritative** — until #1082 audits every reader, the detectors share the split-state assumption they're meant to police; their TTL/grace-window tuning (`timeout+300s`, 60s, 2h) are the same heuristic class that #476 broke.

### Link to consolidation / pull redesign

This family is the *state* dual of what #428 did for *capacity*. #428 (CapacityManager) consolidated three slot/queue primitives into one facade so "how many are running / queued" has a single owner; this family is the unfinished equivalent for "*which* executions are running and what is their terminal status" — still split across Redis + SQL + RAM with ~12 writers. #1082 is explicitly the bankable, ship-independently first step toward #1081's pull/work-stealing redesign: collapsing `status` to a CAS-guarded single-owner projection structurally removes split-brain and lets canary S-01 (slot↔row bijection) retire. Under pull coordination the agent pulls its own work and emits its own terminal event, which finally makes #524's *deferred* "agent-authoritative" contract realizable — and would dissolve the inference layer (#1022's dispatch ceiling, the time-heuristics, the E-* detectors) rather than continuing to guard it. The gate on that, per the 2026-06-05 decision, is #1084 effect-idempotency and Postgres #300 before the queue carries the fleet.

### Verifier notes

Changed (everything else confirmed against the repo):

1. **#524 status — the biggest error.** The analysis repeatedly called #524 "open-unfixed" with a deferred projector. **#524 is CLOSED/COMPLETED** (2026-06-01, shipped v0.6.0 via #998), closed by **PR #541** (`pull_request: true`). The maintainer scoped it down: the projector was deferred, but the CAS-guard scope **shipped under #524**. Corrected #524's fix_shape from "open-unfixed" to "point-guard (scoped-down)," and corrected the design-pathology/residual-risk lines that asserted "#524's cure is unbuilt / open-unfixed."

2. **#524 and #541 are one delivery, not two bugs.** #541 is the PR that implemented and closed issue #524 (its title literally references #524). Re-labeled #541's entry as "the PR that closed #524" and added an explicit relationship note; kept it listed for continuity but flagged the duplication.

3. **#524/#1082 "recurred" framing.** The analysis called #1082 "the recurrence of #524." It is the **re-filed deferred remainder** (planned continuation, never-shipped half), not a regression of a shipped fix. Corrected the `recurred` fields on both. Also noted #1082's own ACs are CAS-guard + reader-audit only — it does not require the full agent-push projector either.

4. **#1022 primary_class swapped: PUSH_DISPATCH_BLOCKING → MISCLASSIFIED_FAILURE** (PUSH_DISPATCH_BLOCKING demoted to secondary). Verified the dispatch handler is `async def` returning `accepted` in ms (`internal.py:335-348`), so blocking is only the *trigger* (event-loop stall); the *reported bug* is the empty-error application-FAILED recording of a transport timeout — independently fixable without touching the dispatch model. "Prefer the deeper cause of *this* bug" points at the misclassification, not the stall.

5. **#476 residual-risk over-reach corrected.** `db/agent_shared_files.py` wraps **both** sides in `datetime()` (verified `:145-147`, `:206`, `:226`), so it is **not** the #476 lexicographic bug class; same for the audit triggers (same-format operands). Removed it from the #476 residual risk. The genuine #476 residual (`mark_stale_executions_failed`/`mark_no_session_executions_failed` hand-rolling `strftime` thresholds) is **confirmed** at `db/schedules.py:1823,1875` and kept.

6. **Line-number drift fixed** for #541's CAS guards: `db/schedules.py:1305-1339` → actual `:1316`/`:1331`; sweeps `:1851`/`:1902`; cleanup routing `cleanup_service.py:1020,1315`. Substance unchanged.

Verified-and-confirmed (no change): the #1022 mechanism end-to-end (RUNNING row created pre-dispatch at `service.py:768`/`database.py:287`; no try/except at `:1090-1096`; `str(httpx.ReadTimeout(''))==''` via httpx's `message=str(exc)` mapping; `current.status==RUNNING` guard does not save the row); `iso_cutoff` adoption list; canary E-01 `300s`/E-05 `60s`/E-02 `canary:e02:terminal_seen`; issue numbers/titles for #79/#440/#476/#1081/#1084/#1045/#429/#378/#403; the "~12 writers" count (#429 body enumerates 9 cleanup paths + TES/scheduler/retry); the design pathology and the agent-has-no-Redis (#589 network split) deferral reason.


---

Confirmed: `_JSONL_PERSIST_THRESHOLD_S = 600` (line 67), and `_recover_metadata_from_jsonl` exists in `jsonl_recovery.py` (line 307). The analyst's residual-risk claim — "Salvage depends on JSONL existing — only enabled for timeout>600s tasks; short fan-out reader-races still null-everything with no recovery" — is verified.

I now have comprehensive verification. Let me consolidate. The analysis is overwhelmingly correct. The key corrections are:

1. **Missing bug #906/#909** — the clearest PARALLEL_CODE_PATHS recurrence of #516 (chat-path classifier added separately). The analyst attributed this to #907.
2. **#907 misattribution** — #907 did NOT add the chat-path signal classifier (that was #906/#909, which landed earlier). #907's primary should be MISCLASSIFIED_FAILURE (OOM-as-auth wording + blocklist) + the semaphore, not PARALLEL_CODE_PATHS.
3. **#904 RC-1 deeper cause** — the `agent_call_limiter.py` docstring reveals the deeper cause is sync-`sqlite3`-in-async-event-loop, with the semaphore explicitly an accepted band-aid (POINT_FIX_ACCRETION), not just PUSH_DISPATCH_BLOCKING.
4. **Minor**: `read_stderr` (line 475) logs, it doesn't silently swallow — the `except: pass` at line 590 is the `_run_stdout` terminate-call swallow. And `_finalize_headless_result` already has partial snapshotting (line 88).

Here is the corrected analysis.

---

## FAMILY: Agent-side reader races / empty results / OOM

### Bugs

- **#548 — Child processes inherit agent-server stdout pipe → reader thread stuck**
  - symptom: Persistent false `"0 tool calls"` failures across agents; reader-thread warnings (`Reader thread(s) still stuck after 30s`, `I/O operation on closed file`), final result block lost.
  - root_cause: When the agent spawns `claude --print --output-format stream-json`, async Stop hooks and process-based MCP servers inherit the Trinity stdout pipe fd (fd 1). They keep the write end open after claude exits, so the kernel never EOFs the read end and the reader thread blocks forever; force-closing the pipe discards the buffered `{"type":"result"}` line. **Verified nuance:** because Trinity uses `subprocess.PIPE` (Python dups the pipe onto claude's fd 1) and `close_fds=True` is already the POSIX default for fds ≥ 3, the leak is specifically claude's *own* re-inheritance of fd 1 to *its* grandchildren — which `FD_CLOEXEC` on Trinity's parent pipe does **not** fully prevent (claude controls that exec, Trinity doesn't). This makes the issue's own proposed one-line fix necessary-but-insufficient, and explains why no source-level FD fix ever landed.
  - primary_class: READER_RACE
  - secondary: ORPHAN_PROCESS
  - fix_shape: open-unfixed — the issue's proposed `FD_CLOEXEC`-on-the-parent-pipe fix was NEVER implemented (verified: no `FD_CLOEXEC` / `set_inheritable` / `os.pipe` anywhere in `docker/base-image/`). The area was instead patched obliquely with `start_new_session=True` (#407) + an unconditional **cgroup** orphan sweep (#817, which subsumed the earlier #618 pipe-writer-by-fd-inode scan, #728, #808, #827 env-tag) + a 90s daemon drain budget (#728/#657). Pipe inheritance itself is never prevented at the source.
  - recurred: This IS the persistent recurrence. Still OPEN / `status-in-progress` (confirmed). Root cause that every other bug in this family is a downstream symptom or mitigation of.

- **#678 — Async chat silently fails with null response/telemetry (reader race mid-turn)**
  - symptom: 24-min / 56-turn / 135-raw-message async `chat_with_agent` completed `status=failed`, `response=null`, and ALL telemetry (cost/context/model/log) null. Error text said "0 tool calls" and "transient — retry."
  - root_cause: The #548 inherited-pipe race wedged the reader thread before the trailing `result` line was captured; the success path populates cost/duration only from that line, so its loss null-everythinged the row.
  - primary_class: READER_RACE
  - secondary: MISCLASSIFIED_FAILURE (a real lost-result reported as null/"transient")
  - fix_shape: new-sweep-or-path — #797 added JSONL side-channel salvage (`_recover_metadata_from_jsonl`, confirmed in `jsonl_recovery.py:307`), a structured 502 dict body, and a backend in-line auto-retry on the `num_turns<5, raw_message_count==0, parse_failure_count==0` signature. Did not touch the pipe inheritance.
  - recurred: The owning function `_classify_empty_result` carries fix citations for #520, #531, #630, #640, #678 in its own docstring (verified, lines 368-418) — five prior point-fixes to the same classifier. Strongest recurrence signal in the family.

- **#797 — Salvage telemetry + auto-retry reader-race empty results**
  - symptom: (the fix PR for #678) reader-race rows now carry salvaged cost + `recovered_from_jsonl=True` and auto-retry once.
  - root_cause: N/A — this is the mitigation layer. It treats the JSONL on disk as the authoritative side channel because stdout cannot be trusted.
  - primary_class: READER_RACE (mitigation)
  - secondary: MISSING_IDEMPOTENCY (auto-retry reuses the same `execution_id` — verified `target_id=execution_id` at `task_execution_service.py:815` — and caps timeout at 300s; idempotency was a deliberate design concern, with the failed attempt's cost carried forward at lines 793-798)
  - fix_shape: new-sweep-or-path + reclassification (502 dict body instead of null)
  - recurred: The auto-retry signature (`num_turns<5`) is a heuristic; a genuinely-short-but-real turn could trip it. No direct follow-up, but it added a second recovery path on top of an unfixed cause.

- **#516 — SIGKILL/timeout misclassified as authentication failure**
  - symptom: claude subprocess killed by schedule timeout or OOM surfaced "Generate a new one with `claude setup-token`."
  - root_cause: The SDK's exit-failure diagnosis defaults to an OAuth/auth explanation when it can't determine why the child died; a signal kill looks the same as a clean exit-1 token failure.
  - primary_class: MISCLASSIFIED_FAILURE
  - secondary: none
  - fix_shape: reclassification — introduced `_classify_signal_exit` (PR #517, commit `42f75be5`). **Covered the headless path only.**
  - recurred: YES — but the direct recurrence is **#906/#909** (see below), NOT #904/#907. The headless-only `_classify_signal_exit` left the **chat path** (`claude_code.py`) unprotected; the identical misclassification re-surfaced there and had to be fixed separately. Direct PARALLEL_CODE_PATHS recurrence.

- **#906 → #909 — SIGKILL-at-0-turns misclassified as auth, CHAT PATH (the direct #516 recurrence)** *(ADDED by verifier — was missing from the original analysis, mis-folded into #907)*
  - symptom: Same OOM/SIGKILL, same agent, recorded three different ways — "Subscription token may be expired or revoked. Generate a new one with `claude setup-token`" (a guess; no 401/403/429 ever on the wire), the honest SIGKILL string, and "[Errno 32] Broken pipe." The auth guess also leaked into the SUB-003 auto-switch matcher, burning the 2h skip-list slot.
  - root_cause: `_classify_signal_exit` (#516/#517) was added only to `headless_executor.py`; the interactive/chat execution path in `claude_code.py` never invoked it, so a signal kill there still fell through to the auth-fallback heuristic.
  - primary_class: PARALLEL_CODE_PATHS (the chat path lacked the classifier the headless path already had — the literal "add the same function to the other path")
  - secondary: MISCLASSIFIED_FAILURE (OOM/SIGKILL reported as auth), MISSING_IDEMPOTENCY (futile SUB-003 auto-switch burns the skip-list slot)
  - fix_shape: reclassification — PR #909 (commit `f78c5054`, merged **2026-05-21**) wired the chat path to call `_classify_signal_exit` before the auth fallback (`claude_code.py:458`).
  - recurred: This IS the #516 recurrence the original analysis attributed to #907. By the time #907 landed (near v0.6.0 / #998, **after** 05-21), the chat path already had the classifier.

- **#904 — Agent OOM cascades into backend worker saturation + false "token expired"**
  - symptom: Operator sees "UI frozen"; root cause is a single agent's `git` op OOM-killing the claude subprocess (`exit -9`), which the Claude Code SDK reported as "Subscription token may be expired or revoked." Backend's 2 uvicorn workers saturated on multi-minute hung agent HTTP calls; SUB-003 auto-switch fired futilely.
  - root_cause: SIGKILL/OOM exit (`-9`) is indistinguishable to the SDK from token expiry, so an OOM was reported as auth failure (RC-2/RC-3); separately the backend held the event loop for the whole long agent turn (RC-1). **Verified deeper RC-1 mechanism:** the `agent_call_limiter.py` docstring states the true cause is **synchronous `sqlite3` calls intermixed with `await httpx.post` inside `execute_task`** — sync DB stalls the async event loop, and N parallel long agent calls serialise on the SQLite writer-lock + GIL until the 10s Docker healthcheck flips the container `unhealthy`. The semaphore is explicitly "NOT a fix for the underlying sync-DB problem … The proper sync→async-DB migration is a separate follow-up."
  - primary_class: MISCLASSIFIED_FAILURE (OOM reported as auth)
  - secondary: PUSH_DISPATCH_BLOCKING (RC-1; whose *deeper* substrate is sync-DB-in-async-loop, an accepted-band-aid POINT_FIX_ACCRETION), MISSING_IDEMPOTENCY (futile auto-switch consumed the 2h skip-list slot)
  - fix_shape: reclassification + point-guard — #907 reworded `_diagnose_exit_failure`'s no-stderr fallback so it no longer matches `_is_auth_failure_message` (verified `error_classifier.py:156-170`), added `NON_AUTH_KILL_MARKERS` short-circuits in `subscription_auto_switch.py:54` + `scheduler/service.py`, and added the per-agent + global `asyncio.Semaphore` budget (`agent_call_limiter.py`).
  - recurred: RC-4 (cgroup OOM observability) explicitly deferred. The auth-misclassification fix is string-blocklist-based (`NON_AUTH_KILL_MARKERS`) — fragile to future SDK message wording. The signal classifier is the sibling of #516/#906.

- **#907 — No false 'token expired' on SIGKILL/OOM + backend call budget** *(reclassified by verifier)*
  - symptom: (the fix PR for #904) OOM now reports a no-evidence-of-auth diagnostic instead of "token expired"; SUB-003 no longer fires on signal kills (`NON_AUTH_KILL_MARKERS`); backend call budget caps concurrency.
  - root_cause: N/A — mitigation. **Correction:** #907 did NOT add `_classify_signal_exit` to the chat path — that was #906/#909, which merged earlier. #907's `error_classifier.py` change was the `_diagnose_exit_failure` *wording* fix (RC-2), and its `chat.py` change wired the call-budget translation. The "same classifier added a second time" framing belongs to #906/#909, not here.
  - primary_class: MISCLASSIFIED_FAILURE (the OOM-as-auth wording + blocklist mitigation — *not* PARALLEL_CODE_PATHS as originally stated)
  - secondary: PUSH_DISPATCH_BLOCKING (the semaphore band-aid; deeper sync-DB cause), POINT_FIX_ACCRETION (string blocklist + semaphore both bound symptoms, neither removes a cause)
  - fix_shape: reclassification (wording) + new-sweep-or-path (semaphore budget)
  - recurred: RC-4 deferred; blocklist approach is point-fix accretion brittle to SDK message wording.

- **#333 — agent-server.py futex spin loop after days of uptime**
  - symptom: After 2-6 days uptime, 7 containers each at 50%+ CPU; strace shows 100% futex with ~50% errors; zombie claude processes present; no active executions.
  - root_cause: Not definitively isolated. Strong candidates tie to this family: leaked daemon reader threads (from #548) and zombie claude processes accumulating over days, plus unbounded growth (in-memory `conversation_history` was uncapped — the #333 hardening added `_DEFAULT_HISTORY_LIMIT=1000` FIFO trim in `state.py`, verified lines 19/155-157).
  - primary_class: ORPHAN_PROCESS
  - secondary: READER_RACE (leaked reader threads are daemon threads that "die with the container" — verified comment `subprocess_lifecycle.py:44` — i.e. accumulate over uptime), POINT_FIX_ACCRETION
  - fix_shape: point-guard / partial — only the unbounded-history leak was capped (#333 hardening in `state.py`). The futex spin / zombie-claude root cause is still OPEN (`status-ready`, P2, confirmed). Canary invariant R-01 (no zombie claude) was added as a *detector*, not a cure.
  - recurred: OPEN — the leaked-daemon-thread and zombie-process accumulation it depends on is #548, still unfixed.

- **#970 → #973 / #980 / #1025 — Headless executor reader-thread leak causes 2h hangs**
  - symptom: Headless executions hung for ~2h before finalizing.
  - root_cause: Misdiagnosed at first as the reader drain; the real bottleneck was `process.wait(timeout=effective_timeout)` (`headless_executor.py:588` at the time; the bounded polling loop now lives at lines 634-652). The competing PR #980 was built on the wrong diagnosis and closed as superseded — confirmed verbatim by the #1025 issue body.
  - primary_class: TTL_HEURISTIC (a 2h wait budget standing in for a real completion signal) with READER_RACE as the suspected-but-wrong cause
  - secondary: READER_RACE, POINT_FIX_ACCRETION
  - fix_shape: point-guard (#973 bounded the wait with `_WAIT_POLL_S` polling + `result_seen` early-exit + stall watchdog) + deferred hardening (#1025 salvages #980's genuine improvements: capture daemon-thread exceptions in `_drain_bounded`, snapshot-isolate `_finalize_headless_result` from the still-alive reader).
  - recurred: YES — #1025 is the open follow-up (confirmed OPEN, `type-refactor`). The drain daemon still does `except Exception: pass` (`subprocess_lifecycle.py:74-75`, verified). **Correction:** the original claim that `read_stderr` "still swallows exceptions (`headless_executor.py:590`)" is inaccurate — `read_stderr`'s own `except` *logs* (`headless_executor.py:475-476`); line 590's `except Exception: pass` is the secondary terminate-on-crash swallow inside `_run_stdout`, not the stderr reader. Also `_finalize_headless_result` already snapshots `execution_log` via `list(...)` (line 88); #1025's residual ask is the *deep metadata copy* + drain-exception capture, not all snapshotting. The 2h hang area broke, was point-fixed, and spawned an open hardening ticket.

- **#106 — Cleanup service misses 'skipped' executions + slow no-session detection**
  - symptom: 2 executions stuck `skipped` 5+ hours (never reaped), and `running`-with-`claude_session_id=NULL` launch-failures held capacity slots for the full 120-min stale timeout.
  - root_cause: Twofold (verified from issue body): (1) `mark_stale_executions_failed()` filtered `WHERE status='running'` only — the `SKIPPED` enum value was added to `TaskExecutionStatus` but the cleanup query was never updated (enum-completeness gap), so skipped rows leaked; (2) the 120-min fixed timeout (`EXECUTION_STALE_TIMEOUT_MINUTES`) is a liveness stand-in, so dead-on-arrival no-session rows held slots for 2h. The no-session rows themselves are a sibling of the reader race (agent-side launch silently fails — no session id, no terminal signal). Related #90 (scheduler/backend DB split) is the SPLIT_STATE_AUTHORITY backdrop.
  - primary_class: ORPHAN_PROCESS (leaked execution rows holding capacity slots)
  - secondary: TTL_HEURISTIC (120-min fixed stale timeout as a liveness stand-in), POINT_FIX_ACCRETION (enum grew, query didn't — the exact gap #137 reopened), SPLIT_STATE_AUTHORITY (#90)
  - fix_shape: new-sweep-or-path — fast-fail no-session rows after 60s + handle `skipped` as terminal.
  - recurred: YES — re-fixed by #137/#201 ("misses no-session and skipped executions") AFTER the #106 fix landed. Verified: #137's body cites the *identical two categories* (no-session running + skipped never-terminal). The same cleanup gap reopened.

### Family pattern
**stdout is a lossy, untrusted terminal-signal channel, and every fix added a parallel recovery/reclassification path instead of making the channel reliable.** The single shared root is #548: child processes (hooks, MCP servers) inherit the agent-server's stdout pipe fd, so the kernel never delivers EOF, the reader thread leaks, and the trailing `{"type":"result"}` line — the ONLY source of cost/duration/turns/session-id — is discarded on force-close. Every other bug is a consequence: null telemetry (#678), "0 tool calls" (#548/#678), OOM-looks-like-the-same-empty-exit-as-auth (#516 → recurring in #906/#909 chat path → #904/#907 OOM cascade), leaked daemon threads + zombies accumulating into a futex spin over days (#333), 2h waits on a result that will never arrive on stdout (#970), and orphan rows when the agent emits no terminal signal at all (#106). The agent reports the wrong terminal signal — or none — and the platform keeps building detectors and salvage paths around an unreliable wire.

### Design pathology
The original choice to **carry the authoritative execution result (cost, tokens, turns, session id, completion) on the same stdout pipe that is inherited by every child process claude spawns** — with no fd isolation Trinity can enforce (claude, not Trinity, execs the grandchildren that re-inherit fd 1) and no out-of-band completion ack. stdout is simultaneously the streaming-text channel AND the structured-terminal-signal channel, and it is shared with untrusted grandchildren. That coupling means any long-lived child (an async git-push hook, an `npx` MCP server) can corrupt the completion signal of a run it has nothing to do with. The JSONL "side channel" salvage (#678) is a tacit admission that stdout was the wrong place for the authoritative record — but the salvage was bolted on rather than promoting the JSONL (or an explicit completion endpoint) to be the primary signal. The deeper structural sin is that there is **no out-of-band, agent-owned completion record acked back to the backend** — every signal rides the one pipe, and the backend can only guess (via TTL, blocklist string match, or empty-result heuristic) when the pipe lies.

### Residual risk
- **#548 is still OPEN** — the root cause is uncured; `start_new_session` + cgroup sweep + 90s drain budget *contain* the leak (reader threads become leaked daemons that "die with the container") but do not prevent pipe inheritance. Note the proposed `FD_CLOEXEC`-on-parent-pipe fix is itself insufficient (claude re-inherits fd 1 to grandchildren outside Trinity's control), so even "implementing the fix" wouldn't fully close it — the real cure is an out-of-band completion channel. Leaked daemon threads accumulate over uptime, which is exactly the substrate of the OPEN #333 futex/zombie problem.
- **#333 OPEN** — futex spin / zombie-claude root cause unidentified; only the unbounded-history leak was capped. Containers still need periodic restarts.
- **#1025 OPEN** — drain daemon (`subprocess_lifecycle.py:74-75`) still `except Exception: pass`; finalize's deep-metadata snapshot isolation (the leaked-reader race on `ctx` fields beyond `execution_log`) never landed. Defense-in-depth incomplete.
- **Heuristic fragility** — the #678 auto-retry fires on `num_turns<5 && raw_message_count==0 && parse_failure_count==0`; the #904/#907 auth-vs-OOM separation is a string blocklist (`NON_AUTH_KILL_MARKERS`) brittle to SDK message wording. Both are guesses standing in for a real signal.
- **Sync-DB-in-async-loop uncured** — the #904 RC-1 semaphore explicitly does not fix the underlying sync-`sqlite3`-inside-`async` event-loop stall; the proper sync→async-DB migration is an unscheduled follow-up. A second long agent call still serialises on the SQLite writer lock; the semaphore only bounds how many.
- **Salvage depends on JSONL existing** — only enabled for timeout>600s tasks (`_JSONL_PERSIST_THRESHOLD_S=600`, verified); short fan-out reader-races still null-everything with no recovery.

### Link to consolidation / pull redesign
- **#428 (CapacityManager) connection:** the agent-side failures here directly feed the producer-side machinery #428 consolidated. A reader-race that null-terminates or never-terminates a row leaks a capacity slot (#106) and an open execution; misclassifying OOM as auth (#904/#906) wrongly burns the SUB-003 skip-list and trips the dispatch breaker (#526). #904 RC-1's per-agent semaphore is a *second* capacity gate living outside CapacityManager (`agent_call_limiter.py`) — a parallel-code-path that should fold into the unified facade. The canary invariants (S-01 slot↔row bijection, E-05 dispatched-rows-have-session, R-01 no-zombie-claude) are detectors for exactly these agent-side leaks.
- **#1081 pull redesign connection:** this family is the strongest argument for it. Under push dispatch the backend holds the event loop on a long synchronous agent HTTP call (#904 RC-1 PUSH_DISPATCH_BLOCKING, deepened by sync-DB stalls) and trusts a single stdout terminal signal it can't verify. A pull/work-stealing model with explicit effect-idempotency (#1084) and an agent-side mailbox/actor model (#945, referenced in `state.py` as the reason `mailbox_depth` is deliberately not emitted) would let the agent own its own completion record (the JSONL becomes primary, acked out-of-band) and the backend stop blocking on a worker per turn — collapsing READER_RACE + MISCLASSIFIED_FAILURE + PARALLEL_CODE_PATHS + PUSH_DISPATCH_BLOCKING + ORPHAN_PROCESS into "agent commits a durable result, backend reconciles from it." Until then the family is contained, not cured.

### Verifier notes

What I changed (everything else held up under repo verification):

1. **Added missing bug #906/#909** — the direct, literal PARALLEL_CODE_PATHS recurrence of #516 (chat-path `_classify_signal_exit` added separately). Git history (`git log -S`) proves PR #909 (commit `f78c5054`, merged 2026-05-21) added the chat-path classifier, and #906's body is a textbook OOM-as-auth misclassification. The original analysis omitted this issue entirely and folded its substance into #907.

2. **Corrected #907 misattribution + reclassified its primary** — the original said "#907 RC-2 mirrors `_classify_signal_exit` … into the chat path — the same classifier had to be added a second time," classing #907 PARALLEL_CODE_PATHS. Verified false: #909 added the chat-path classifier and merged *before* #907 (which landed near v0.6.0/#998). #907 touched `error_classifier.py` only for the `_diagnose_exit_failure` *wording* fix. #907's primary is now **MISCLASSIFIED_FAILURE** (OOM-as-auth wording + `NON_AUTH_KILL_MARKERS` blocklist) + semaphore; PARALLEL_CODE_PATHS moves to #906/#909 where it actually belongs. The recurrence claim itself was right in substance — only the issue number was wrong.

3. **Deepened #904 RC-1** — the `agent_call_limiter.py` docstring shows the *deeper* cause of the worker saturation is **synchronous `sqlite3` inside the async event loop**, not generic thread-holding. The semaphore is explicitly an accepted band-aid ("does NOT fix the underlying sync-DB problem"). Added POINT_FIX_ACCRETION as a secondary and a dedicated residual-risk bullet. PUSH_DISPATCH_BLOCKING remains the right symptom-level label.

4. **Corrected #970/#1025 residual-risk detail** — `read_stderr` does *not* silently swallow (it logs at `headless_executor.py:475-476`); the `except Exception: pass` the original cited at line 590 is the secondary terminate call inside `_run_stdout`. Also `_finalize_headless_result` already snapshots `execution_log` via `list(...)` (line 88) — #1025's residual ask is narrowed to the deep-metadata copy + drain-exception capture.

5. **Added a verified nuance to #548's root cause + design pathology** — `close_fds=True` is already the POSIX default for fds ≥ 3, and Trinity uses `subprocess.PIPE` not a manual `os.pipe()`; the leak is claude *re-inheriting fd 1 to its own grandchildren*, which the issue's proposed `FD_CLOEXEC`-on-the-parent-pipe fix does NOT fully prevent. This sharpens *why* no source fix landed and strengthens the "needs an out-of-band completion channel" pathology. Confirmed: no `FD_CLOEXEC`/`set_inheritable`/`os.pipe` anywhere in `docker/base-image/`.

6. **Enriched #106 root cause** — confirmed from the issue body it's a two-part bug: an enum-completeness/POINT_FIX_ACCRETION gap (`SKIPPED` added to the enum, cleanup query not updated — the exact gap #137 reopened) plus the 120-min TTL_HEURISTIC. Added POINT_FIX_ACCRETION as a secondary; primary ORPHAN_PROCESS retained.

All core claims confirmed against the repo: #548/#333/#1025 are OPEN as stated; `_classify_empty_result` carries the five cited fix-citations in its own docstring; #678 auto-retry signature, 300s cap, and same-`execution_id` reuse are exact; `_JSONL_PERSIST_THRESHOLD_S=600` gating is exact; the cgroup-sweep mitigation accretion (#618→#728→#808→#827→#817) is exact; #137 is a verbatim recurrence of #106. The family thesis, the single-root attribution to #548, the design pathology, and the pull-redesign linkage all hold — the corrections are attribution/depth refinements, not a change to the family's shape.


---

All claims are verified. Here is the corrected analysis.

---

## FAMILY: Scheduler dispatch (fire-and-forget)

### Bugs

- **#101 — Scheduler TCP connection drops on long agent turns**
  - symptom: `scheduler.service: Schedule Heartbeat execution failed: Server disconnected without sending a response` (httpx `RemoteProtocolError`); agent work completes on the container, but `schedule_executions` has **zero rows** for the agent and every cycle warns about stale-slot cleanup.
  - root_cause: The scheduler issued a *blocking* `POST /api/internal/execute-task` that the backend held open for the entire multi-minute agent turn (backend → `agent_post_with_retry` → agent `/api/task`). Pre-#101 the scheduler's own httpx timeout was `request_timeout = float(timeout_seconds) + 30` (verified at the 747ec4a5 parent) — i.e. it deliberately stayed open for the *whole* turn. Any TCP instability over that 10–60-min window (uvicorn worker recycle, Docker bridge idle-close, per-request `AsyncClient`) reset the connection, so the scheduler never received the result it was waiting for. The #101 issue body names this exactly: *"the architecture: blocking HTTP request that must stay open for the entire duration."*
  - primary_class: PUSH_DISPATCH_BLOCKING
  - secondary: SPLIT_STATE_AUTHORITY (the *consequence* — with the result encoded only in the in-flight HTTP response, a lost connection = lost record. Note: pre-fix this was *single* authority in a volatile place, not yet a true split; the Redis-slot/SQL-row/transit split only fully crystallizes as the system grows, see #1083.)
  - fix_shape: new-sweep-or-path (added `async_mode=True` dispatch + a `dispatch_timeout = 30.0` cap + a DB-polling path `_poll_execution_completion`; **kept the synchronous backward-compat path**). **Correction (verified by git):** #101 (747ec4a5, Mar 11) called `_poll_execution_completion` *synchronously* (`return await …`), so the APScheduler job function **still blocked** for the whole turn after this fix — which is precisely why #132 was still needed. #101 did **not** add the background `_poll_and_finalize` task.
  - recurred: yes — the same dispatch area produced #1022 (the 30s literal this fix introduced) and the job-function-blocking it left in place produced #132; the structural follow-up is #1083.

- **#132 — APScheduler skips triggers when max_instances=1 reached**
  - symptom: `apscheduler.scheduler: Execution of job ... skipped: maximum number of running instances reached (1)` even though the agent had free Redis slots; a stuck execution blocked the job function for up to an hour, dropping every subsequent cron fire and leaving a non-terminal "skipped" record.
  - root_cause: Two independent concurrency systems — APScheduler's in-process `max_instances=1` per cron job vs. Trinity's per-agent Redis SlotService (default 3). **But the divergence is only *expressed* because the job function blocks**: after #101 the scheduler still did `return await self._poll_execution_completion(...)` synchronously, pinning the Python job function for the full execution timeout, so APScheduler counted the job as "still running" and refused to re-fire, regardless of available agent capacity. Remove the blocking poll (the actual #132 fix) and `max_instances=1` is never reached even though the two counters still "disagree by construction."
  - primary_class: PUSH_DISPATCH_BLOCKING — **CORRECTED from SPLIT_STATE_AUTHORITY.** The two-counter mismatch is a standing *condition*, not the trigger; the bug fires only when the blocking poll pins the job function long enough for the next cron tick. The fix (Option A in the issue, shipped as fb1d22cf) is to *stop blocking*, not to reconcile the counters — `max_instances=1` was deliberately left in place. A SPLIT_STATE_AUTHORITY bug would be cured by reconciling the stores; this one is cured by removing the blocking call, which is the PUSH_DISPATCH_BLOCKING signature.
  - secondary: SPLIT_STATE_AUTHORITY (APScheduler's instance counter and the Redis slot ZSET encode "is-running" independently and disagree by construction — the enabling condition), POINT_FIX_ACCRETION (`max_instances=1` kept; fast-return + a `_on_job_max_instances` skip-audit listener added *around* it rather than removing the mismatch).
  - fix_shape: new-sweep-or-path (fb1d22cf / #328, Apr 14: `_call_backend_execute_task` now wraps the poll in `asyncio.create_task(self._poll_and_finalize())` and returns `"dispatched"` in ~30s — **this commit, not #101, introduced the true fire-and-forget background task**; also added the `_on_job_max_instances` listener writing a `skipped` audit row at service.py:611).
  - recurred: yes — `max_instances=1` and the skip-handler still live in `service.py` (lines 428, 611, 1718); the fix made the blocking window short rather than removing it, and #1083 now removes the underlying backend pinning.

- **#1022 — Scheduler writes status='failed' with empty error on 30s dispatch timeout**
  - symptom: synchronized cross-agent batches of `schedule_executions` with `status='failed'`, `error=''` (empty), `response` NULL, `duration_ms` ≈ 30000; log line `Schedule {name} execution failed:` with nothing after the colon. Largest failure class on the production instance, with zero triage signal.
  - root_cause: Two defects compound. (1) A hardcoded `dispatch_timeout = 30.0` (service.py:1088) on the dispatch POST, with **no try/except around the `async with httpx.AsyncClient()` block** (verified — lines 1090-1096), so an httpx timeout propagates out of `_call_backend_execute_task`. (2) The handler at `_run_scheduled_task` does `error_msg = str(e)` (line 929) and **`str(<httpx timeout exception>) == ''`** — confirmed empirically against the in-repo `httpx==0.28.1`: a *real* triggered `ConnectTimeout` and a *real* `ReadTimeout` (accept-but-no-reply) both yield `str(e) == ''` and `args == ('',)`, not just hand-constructed ones. That empty string is persisted as `error`. The 30s ceiling is only hit when the single backend worker's event loop briefly stalls and can't even *accept* the POST (the async endpoint normally returns `{"status":"accepted"}` in well under a second) — a shared stall, so every concurrent dispatch (and the pre-check) times out at the same instant.
  - primary_class: MISCLASSIFIED_FAILURE (a dispatch-acceptance timeout recorded as a generic empty-error `failed`, indistinguishable from a real task failure and stripped of its type)
  - secondary: PUSH_DISPATCH_BLOCKING (the shared single-worker event-loop stall is the trigger), TTL_HEURISTIC (the 30s is a fixed guess at "dispatch should be fast," not lifted into `SchedulerConfig` — verified absent from `scheduler/config.py`), POINT_FIX_ACCRETION (the 30s literal was introduced by the #101 fix and *survived the recent #1026 refactor of the very function it lives in* — 006dea84 split `_execute_schedule_with_lock` without touching the empty-error path).
  - fix_shape: open-unfixed (issue is OPEN — verified; no commit references #1022 and no working-tree change; proposed cheap fix is `error_msg = str(e) or f"{type(e).__name__}"` + an explicit `except httpx.TimeoutException` + lifting `30.0` into config; the root event-loop stall is explicitly deferred to a follow-up).
  - recurred: this *is* a recurrence within the #101 dispatch area. **Precision correction:** #101 did not *create* the empty-error path — `error_msg = str(e)` predates #101 (it's in the original standalone scheduler, dd457094, and reached its current form in e1998fd3 on Mar 9, two days before #101). And a blocking call that could time out into that same handler also predates #101. What #101 actually did was *shrink the timeout window* from `agent_turn + 30s` (~60 min) to a fixed `30s`, converting a rare latent trap into a frequent one that then surfaced en masse under concurrent cron fan-out. So #1022 is "an old latent empty-string trap, newly made easy to hit by #101's short dispatch deadline" — not "a path #101 created."

- **#1083 — Fire-and-forget dispatch so a hung turn holds zero backend resource**
  - symptom (design target): a wedged agent turn (e.g. a hung MCP call — the "Cornelius" class) holds a backend slot until timeout, trips the dispatch breaker, and cascades to other agents' executions.
  - root_cause: #101/#132 only moved the blocking from *scheduler→backend* to *backend→agent*. Verified: the backend's `_execute_task_internal_background` (internal.py:383) is spawned via `asyncio.create_task` and then `await task_service.execute_task(...)` (line 392), which acquires a capacity slot and holds the agent HTTP connection (`agent_post_with_retry`) for the entire turn, recording the dispatch-breaker outcome at the terminal (`task_execution_service` imports `DispatchBreaker` + `capacity_manager.acquire`; records outcome at service.py:431). So a hung turn still pins a backend coroutine + slot and can trip the breaker — the "fire-and-forget" label was true only for the *scheduler* hop, not the backend hop.
  - primary_class: PUSH_DISPATCH_BLOCKING
  - secondary: SPLIT_STATE_AUTHORITY (slot in Redis + execution row in SQL + the live agent connection all encode "is-running" — *this* is where the true three-store split named in #101's note actually lives), PARALLEL_CODE_PATHS (sync `/chat`, async `/task`, scheduler async, fan-out, and the backward-compat blocking execute-task path — verified still present at internal.py:349 — all push-dispatch and would each need the same change).
  - fix_shape: structural-consolidation (planned — backend stops pinning a coroutine for the turn; terminal state applied from the agent's own result report under the existing CAS guard; the natural shape under the #1081 pull migration).
  - recurred: n/a — this issue is the recognition that #101/#132 did not cure the family; it's the structural follow-up, not yet shipped.

### Family pattern
Every bug in this family is a consequence of **synchronous push-dispatch**: a long agent turn is modeled as a long-lived blocking call that some component must hold open end-to-end. Each fix pushed the held connection one hop further down the chain (scheduler-job-function-via-sync-poll *after #101* → backgrounded out of the scheduler job function *by #132* → still held by the backend coroutine *targeted by #1083*) without ever eliminating it. Because "is this execution running" is encoded *in a live blocking call* rather than in a single authoritative store, any disruption of that call (TCP reset #101, APScheduler instance-counter pinned by a blocking poll #132, event-loop stall #1022, hung MCP turn #1083) corrupts or loses state — false failures, skipped triggers, empty-error rows, and breaker cascades are all the same lost-blocking-call wearing different masks.

### Design pathology
The original scheduler dispatched by making a **blocking HTTP request that stays open for the full duration of the agent's work** (originally `timeout = agent_turn + 30s`, potentially 60 min), treating the HTTP response as the system of record for the execution result. That single choice means: liveness is tied to connection health, two independent concurrency limiters (APScheduler `max_instances` vs Redis slots) must agree about "running," and the only place a result exists is in transit. There was no design where the dispatcher just *records intent* (an INSERT of a queued row) and lets a worker report the terminal state asynchronously. Each subsequent fix shortened or relocated the blocking call but preserved this core "response-as-system-of-record" assumption — which is why #1022's empty-error trap (a latent defect older than #101) only became the largest production failure class once #101 shrank the window enough to hit it routinely.

### Residual risk
- **#1022 is still OPEN** — the production fleet's largest failure class (empty-error `failed` rows) is uncured; operators still get blank diagnostics on every backend-stall batch. The recent #1026 refactor touched the same function and did *not* fix it.
- **The backend coroutine still pins a slot for the whole turn** (#1083 unshipped) — a hung MCP/Cornelius turn still ties up backend capacity and can trip the dispatch breaker, cascading to sibling agents.
- **`max_instances=1` plus the skip-recording handler remain** (service.py:428, 611, 1718) as point-fix scaffolding — defense-in-depth that exists only because the blocking was shortened, not removed; if the background poll ever blocks the loop, the skip path re-activates.
- **`error_msg = str(e)`** at line 929 (and a twin at 1883) is still a latent empty-string trap for *any* future exception type whose `str()` is empty.
- The 30s dispatch timeout is still a hardcoded literal (a TTL_HEURISTIC), not tied to any real "backend accepted" signal and not in `SchedulerConfig`.

### Link to consolidation / pull redesign
The breaker-outcome recording and slot acquisition that a hung turn trips are owned by the **#428 CapacityManager consolidation** (`capacity_manager.acquire` / `record_outcome` — verified imported and called from `task_execution_service`); #1083's "a single slow turn must not trip the dispatch breaker" requires changing what a backend-held turn counts as, which only became tractable after #428 unified the slot/backlog/breaker surface into one facade. More fundamentally, #1083 is explicitly framed as bankable win #2 of the **#1081 pull / work-stealing redesign**: the entire family dissolves when the dispatcher stops pushing and instead just `INSERT`s a queued row that a worker *pulls* — the backend never holds a coroutine for the turn, "running" lives in exactly one store, the APScheduler/Redis split disappears, and there is no blocking call left to time out or lose. The scheduler family is the canonical motivating case for moving from push to pull (and is gated, per project memory, on #1084 effect-idempotency and Postgres #300 before the queue carries the fleet).

### Verifier notes
Changed:
1. **#101 fix_shape — corrected a factual error.** The original said #101 "added a background DB-polling path `_poll_and_finalize`." Git (`git show 747ec4a5`) proves #101 added a **synchronous** `_poll_execution_completion` called via `return await …`, so the job function still blocked. The background `_poll_and_finalize` task was added by **#132** (fb1d22cf / #328, Apr 14), not #101. Updated both #101 and #132 fix_shape/root_cause to reflect the real two-step history.
2. **#132 primary_class — changed SPLIT_STATE_AUTHORITY → PUSH_DISPATCH_BLOCKING.** The deeper cause is the blocking poll that pins the APScheduler job function; the two-counter mismatch is the standing *condition*, not the trigger, and the actual fix removes the blocking (not the mismatch). Demoted SPLIT_STATE_AUTHORITY to secondary. (The original analyst's own "secondary: PUSH_DISPATCH_BLOCKING — the blocking poll is what pinned the job function" concedes the mechanism; I promoted it to primary per the "prefer the cause the fix actually addresses" rule.)
3. **#1022 recurrence narrative — corrected the overstated claim.** The original (and the analyst's confident closing line) asserted "the empty-error path was created when #101 added the 30s timeout." Git shows `error_msg = str(e)` predates #101 (original scheduler dd457094; current form e1998fd3, Mar 9). #101 *shrank the timeout window* (≈60 min → fixed 30 s), making an older latent trap fire routinely — it did not create it. Reworded root_cause/recurred and the design-pathology paragraph accordingly. Also reclassified the `str(e)` empty-string risk note from POINT_FIX_ACCRETION-only by adding the verified detail that the #1026 refactor passed over it.
4. **#101 secondary SPLIT_STATE_AUTHORITY — softened.** Pre-fix the result lived in a *single* (volatile) place, not 2+ stores, so it's not yet a true split; relocated the "three stores encode is-running" framing to #1083 where it's literally true.

Empirically/independently verified and **confirmed unchanged**: all line numbers (1088, 929, 428, 611, 1718, internal.py:383/392/349); issue states (#101/#132 CLOSED, #1022/#1083/#1084/#1081/#300 OPEN, #428 CLOSED) and titles; the `str(httpx-timeout) == ''` mechanism (tested against real `ConnectTimeout` *and* `ReadTimeout` on the in-repo httpx 0.28.1); the no-try/except dispatch block; `dispatch_timeout` absent from config; #1022 unfixed; the backend background coroutine still holding slot + connection; the #428 / breaker-recording ownership; the backward-compat sync paths (PARALLEL_CODE_PATHS). The primary_class for #101 (PUSH_DISPATCH_BLOCKING), #1022 (MISCLASSIFIED_FAILURE), and #1083 (PUSH_DISPATCH_BLOCKING), the family pattern, design pathology, and residual-risk list all hold up.


---

All confirmed. I have everything I need. Here is the corrected analysis.

---

## FAMILY: Idempotency / duplicate executions

### Bugs

- **#525 / #1019 — No idempotency at any producer boundary (RELIABILITY-006)**
  - symptom: Webhook re-deliveries, MCP client retries, and scheduler→backend network blips all created phantom duplicate executions; the unified `TaskExecutionService` funnel "just trusts the producer," so funneling actually concentrated the gap.
  - root_cause: Every execution-creating trigger boundary dispatched on arrival with no dedup record — a re-delivered or retried trigger was indistinguishable from a fresh one. No `(scope, key)` claim existed.
  - primary_class: MISSING_IDEMPOTENCY
  - secondary: PARALLEL_CODE_PATHS, RACE_TOCTOU
  - fix_shape: new-sweep-or-path (new `idempotency_keys` table + `idempotency_service` + per-router `begin/complete/fail` wiring + Invariant #18)
  - recurred: no evidence — `idempotency_service.py`/`db/idempotency.py` untouched since the #1019 merge (verified: only later touches to `chat.py` are the #1026/#1051 decomposition and #526 breaker, which *wrap* the `begin/complete/fail` calls without changing the primitive).

- **#525 atomic-claim primitive — concurrent duplicate triggers must serialize to one execution**
  - symptom: (latent) two uvicorn workers or worker+scheduler racing the same key could both INSERT and both dispatch.
  - root_cause: A naive check-then-insert would be a TOCTOU window; the fix makes `PRIMARY KEY (scope, idempotency_key)` itself the claim — `claim()` INSERTs an `in_flight` row and the loser catches `sqlite3.IntegrityError` and reads the surviving row, which is cross-process-safe over the one shared SQLite file (verified `db/idempotency.py:55-85`).
  - primary_class: RACE_TOCTOU
  - secondary: MISSING_IDEMPOTENCY
  - fix_shape: structural-consolidation (DB unique constraint IS the lock, no app-level CAS)
  - recurred: no evidence

- **#525 webhook boundary — naive external senders retry without an Idempotency-Key**
  - symptom: A webhook sender retrying on a perceived timeout/5xx fired the schedule twice because it sent no idempotency header.
  - root_cause: Idempotency can't depend on the producer being well-behaved; external webhook callers are not. Fix auto-derives `auto:{sha256(token‖\x00‖body)}` (verified `derive_webhook_key`, `webhooks.py:135`) so a literal re-POST resolves to the same key even with no header.
  - primary_class: MISSING_IDEMPOTENCY
  - secondary: none
  - fix_shape: point-guard (body-hash key derivation at the one boundary that owns untrusted producers)
  - recurred: no evidence

- **#525 scheduler boundary — APScheduler retry vs. transient backend 5xx**
  - symptom: A network blip between the scheduler container and backend, plus APScheduler resend, double-fired a cron schedule.
  - root_cause: Scheduler dispatch had no per-fire token. Fix keys on `sched:{execution_id}` (verified `scheduler/service.py:1071`) — the per-fire execution_id is reused across an HTTP resend of the *same* dispatch (dedupes) but an intentional #271 retry mints a fresh execution_id (correctly not suppressed).
  - primary_class: MISSING_IDEMPOTENCY
  - secondary: PARALLEL_CODE_PATHS (scheduler is a separate process / separate code path from the API)
  - fix_shape: point-guard (deterministic per-fire key)
  - recurred: no evidence

- **#914 — MCP `chat_with_agent` sync timeout returned `fetch failed`, callers retried into duplicate-queue**
  - symptom: Sync `chat_with_agent` held the MCP→backend→agent chain open for the whole turn; past the gateway timeout the tool returned generic `fetch failed` while the agent kept running. A good-faith caller retried, queuing duplicates that Trinity's concurrent-duplicate guard then killed mid-execution (~$2–4 + 12 min wasted across 4 rows, bdr-agent 2026-05-22).
  - root_cause: PRIMARY: the backend holds the HTTP connection for the duration of a long agent turn (push-dispatch blocking) — verified still-live in `task_execution_service.py:736-752`, `await agent_post_with_retry("/api/task", timeout=effective_timeout)` with `effective_timeout` up to ~2h — so a slow turn always trips the gateway timeout. The duplicate-queue is the *downstream* symptom of an ambiguous failure: a successful queue reported to the caller as a transport failure.
  - primary_class: PUSH_DISPATCH_BLOCKING
  - secondary: MISCLASSIFIED_FAILURE, MISSING_IDEMPOTENCY
  - fix_shape: new-sweep-or-path (client aborts at `MCP_CHAT_TIMEOUT_MS`, looks up the in-flight execution via `findRecentMcpExecution`/`/executions`, returns `{status:"queued_timeout", execution_id}` so the caller polls instead of retries)
  - recurred: no evidence in this area, but the fix is a workaround — it does not stop holding the connection; it just relabels the timeout. The underlying long-dispatch problem is #408. **#408 is CLOSED (marked "Shipped in v0.6.0 via #998", 2026-06-01) — but the close is premature/aspirational: #998 is a release rollup, and the synchronous long-await dispatch it was meant to dissolve is *still present in the code* (`task_execution_service.py:746`). The root cause persists despite the issue being closed.**

- **#914 ↔ #525 interaction — literal MCP retry is suppressed by the in-flight 409 guard, not by snapshot replay**
  - symptom: (design coupling) the #914 receipt only *advises* the caller to poll; a non-cooperating caller can still retry.
  - root_cause: The two fixes compose, but **not the way the original analysis stated.** `deriveMcpIdempotencyKey([source, agent, mode, model, async, message])` (verified `chat.ts:20`, `:301`) means a *literal* retry of the same message hits the same `(scope, key)`. **Crucially, in the #914 timeout scenario the original execution is still running** (that is *why* the MCP client timed out), so the duplicate claim resolves to `STATE_IN_FLIGHT` → the chat/task router raises **409 `request_in_progress` with the original execution_id to poll** (verified `chat.py:169-177`, `:1325-1333`), NOT a `STATE_COMPLETED` snapshot replay. Snapshot replay only applies once the first execution has finished and `complete()` has stored the snapshot. Either way the retry produces no duplicate execution — but the load-bearing mechanism is the in-flight 409, and that depends on the byte-identical retry carrying the same MCP key. The gap remains if the caller retries with a reworded message.
  - primary_class: MISSING_IDEMPOTENCY
  - secondary: PUSH_DISPATCH_BLOCKING
  - fix_shape: point-guard (deterministic key over call args; relies on byte-identical retry; suppression is 409-in-flight while the first turn runs, snapshot-replay only after it completes)
  - recurred: no evidence

- **#1084 — Effect-scoped idempotency for outbound side effects (OPEN, unfixed)**
  - symptom: Lease-expiry re-delivery (or any at-least-once re-run) of a turn that already sent an email / posted to Slack / charged a Nevermined payment / pushed git re-emits that effect. Trigger idempotency dedups the *entry*, not the agent's *tool calls*.
  - root_cause: Under network invariant #589 the agent's local "done" write and the backend's idempotency-complete write are on different machines and can never be one transaction; exactly-once external effects must therefore be enforced at the sink, per action, on an effect-scoped key `{execution_id}:{effect_ordinal}` — which no sink threads today. Confirmed: **zero** `effect_ordinal`/effect-key references anywhere in `src/` (verified grep count = 0 across `adapters/`, `proactive_message_service`, and MCP outbound tools). Issue #1084 is OPEN (verified) and its body names exactly this sink list + the pull-mode gate.
  - primary_class: MISSING_IDEMPOTENCY
  - secondary: SPLIT_STATE_AUTHORITY (two "done" facts on two machines)
  - fix_shape: open-unfixed
  - recurred: n/a — never shipped; it is the *gate* on defaulting pull mode ON for side-effect-bearing agents.

- **#525 fail-open posture — dedup must never block a real execution (but post-dispatch failures fail *closed*)**
  - symptom: (design constraint) a Redis/SQLite hiccup in the dedup layer must not wedge legitimate traffic. **But the posture is not uniform** — there is a deliberate fail-*closed* window the original analysis missed.
  - root_cause: Every `idempotency_service` *infrastructure* call swallows exceptions and proceeds without dedup; an unknown claim state is also treated as no-dedup (verified `idempotency_service.py:90-92`, `:107-109`). This is deliberate, but it means under DB stress the dedup *guarantee* silently degrades to "best effort" — a duplicate can slip through exactly when load (the duplicate-storm condition) is highest. **Separately and intentionally, the chat/task boundaries do NOT call `fail()` on a post-dispatch failure: the in-flight claim is left in place, so a same-key retry within the 24h TTL gets a 409 rather than a fresh dispatch** (verified comment + code `chat.py:1298-1304`; `fail()` is called *only* on upfront capacity/breaker rejection where nothing was dispatched, `chat.py:213-216`, `:262-264`). So the layer is fail-open on its *own* errors but fail-closed-for-24h once a real claim is taken and dispatch fails downstream — two opposite postures co-resident by design.
  - primary_class: MISSING_IDEMPOTENCY
  - secondary: TTL_HEURISTIC (the 24h window is what bounds the fail-closed retry-block)
  - fix_shape: feature-flag (implicit — infra fail-open is a permanent "off switch on error"; the post-dispatch fail-closed is a deliberate TTL-bounded block)
  - recurred: no evidence, but it is a contained risk rather than a cure.

### Family pattern
Every bug here is the same shape: **a trigger or action arrives more than once and the receiver has no record that it already handled this exact thing.** The cure is always a claim keyed by "the identity of the thing" — `(scope, key)` at the boundary (#525), `(token, body_hash)` for naive senders, `sched:{execution_id}` per fire, `deriveMcpIdempotencyKey(...)` per MCP call, and the still-unbuilt `{execution_id}:{effect_ordinal}` per outbound effect (#1084). The grain just keeps getting finer as you push the dedup point further from the entry and closer to the irreversible side effect. The *enforcement* shape also varies by claim state: a still-running duplicate gets a 409 (in-flight); a finished one gets a snapshot replay — the same key, two different short-circuits.

### Design pathology
The "single funnel" (`TaskExecutionService`) was sold as the place to dedup, but it was never actually single — sync `/chat` runs an inline path and `/api/webhooks/{token}` creates no execution at all (verified: enforcement lives at each *router* boundary, not in the service) — and, more deeply, **funneling concentrated the duplicate problem without solving it: the funnel trusts the producer.** Combined with push-dispatch (the backend holds the connection for the whole turn — verified still live at `task_execution_service.py:746` despite #408 being marked closed), *every* slow turn manufactures an ambiguous "did it land?" signal that turns well-behaved retries into duplicates. The original system equated "I received a request" with "this is a new request" — there was no notion of request identity anywhere in the dispatch path. **The true original sin is the push-dispatch blocking model, not the missing dedup**: dedup (#525) is the *corrective patch* that makes the blocking model survivable, and #914 is a second patch on top of it. Both are downstream of the architectural choice to hold the connection for the whole turn — which is exactly why the pull/work-stealing redesign (#1081) targets that choice rather than adding a third dedup layer.

### Residual risk
1. **#1084 is wide open** — exactly-once external effects are unsolved; today's only mitigation is the policy "pull mode defaults ON only for read/analysis-only agents." Any irreversible-effect agent under at-least-once re-delivery can double-send.
2. **Fail-open dedup degrades under load** — the dedup guarantee silently weakens precisely during a duplicate-storm (DB stress), exactly when it's needed.
3. **#914 is a relabel, not a cure** — the connection is still held for the whole turn. **#408 (the canonical push-dispatch-blocking bug) was *closed* 2026-06-01 as "shipped in v0.6.0," but the synchronous long-await dispatch is still in the code (`task_execution_service.py:746`) — the close is premature/aspirational, so the root cause is in the codebase under a closed issue (a latent recurrence trap: a future maintainer searching open issues will believe this is solved).** The receipt depends on a *cooperative* caller polling, and the in-flight-409/snapshot-replay dedup misses a reworded retry.
4. **24h TTL is a heuristic** — a re-delivery at 24h+1s re-claims as new and re-dispatches (verified: `claim()` DELETEs and re-INSERTs any row older than `iso_cutoff(hours=24)`; the cleanup service purges on the same 24h window, `cleanup_service.py:577`). The window is a guess, not a liveness signal (TTL_HEURISTIC). It also bounds the fail-closed retry-block from residual-risk note in the fail-open bug above.

### Link to consolidation / pull redesign
- **#428 CapacityManager consolidation** collapsed the capacity/slot/backlog stores into one facade but left the *producer* boundary untouched — #525 is the producer-side counterpart that consolidation made *more* necessary (one funnel that doesn't dedup is worse than scattered paths). The two are complementary: #428 unified where executions *land*, #525 unified how duplicate triggers are *rejected before they land*.
- **#1081 pull redesign is gated directly on #1084.** Trigger-idempotency (#525, solved) makes the producer boundary safe; pull/work-stealing introduces lease-expiry re-delivery, which re-runs whole turns — and re-running a turn re-fires its side effects. The issue body states it plainly: #1084 is "the gate on defaulting pull mode ON for side-effect-bearing agents." Until effect-scoped idempotency ships, pull mode is safe only for read/analysis-only agents. The family's unfinished frontier (effect-grain dedup at the sink) is precisely the precondition for the pull rollout.
- **The pull redesign also targets the *real* root cause (#408 push-blocking), not just dedup.** Because #408's synchronous long-await is still in the code despite the closed issue, the dedup family is permanently a patch layer over an unresolved architectural choice — pull/work-stealing is what actually removes the connection-holding that manufactures the ambiguous-failure signals this whole family exists to mop up.

### Verifier notes
Changed (everything else confirmed against the repo):

1. **#914↔#525 interaction — corrected the mechanism.** The original said a literal retry "hits the same key → backend **replays** instead of duplicating." In the #914 timeout scenario the first execution is *still running*, so the duplicate hits `STATE_IN_FLIGHT` → **409 `request_in_progress`** (`chat.py:169-177`, `:1325-1333`), not a `STATE_COMPLETED` snapshot replay. Snapshot replay only applies after the first turn completes. The retry is still suppressed either way, but the load-bearing guard is the in-flight 409. Rewrote the bug + fix_shape and added a sentence to the family pattern.

2. **#408 issue state — corrected a factual error stated three times.** The original asserted "#408, still open" / "#408 open." **#408 is CLOSED** (2026-06-01, "Shipped in v0.6.0 via #998"). However, #998 is a *release rollup* and the synchronous long-await dispatch #408 describes is **still in the live code** (`task_execution_service.py:736-752`, verified). So the analysis's *substance* (connection still held; #914 is a relabel) is correct, but the close is premature/aspirational. Reframed this as a sharper finding: a closed issue whose root cause is still in the codebase — a recurrence trap. Updated the #914 bug, residual-risk #3, and the pathology/link sections.

3. **Design pathology — promoted the deeper root cause.** Per the "prefer the DEEPER cause" instruction: push-dispatch blocking is the original sin; missing-idempotency (#525) and the #914 receipt are both corrective patches downstream of the connection-holding model. Added this explicitly and tied it to why #1081 targets dispatch, not dedup.

4. **Fail-open bug — added the fail-*closed* nuance the original missed.** The posture is not uniform: infra-error fail-open coexists with a deliberate fail-closed-for-24h on post-dispatch failure (the chat/task boundaries leave the in-flight claim in place so a same-key retry gets a 409, `chat.py:1298-1304`; `fail()` runs only on upfront capacity/breaker rejection, `chat.py:213-216`). Added TTL_HEURISTIC as a secondary class on that bug.

5. **Added verification anchors** (file:line) to most claims and confirmed: the atomic-claim primitive, all four boundary wirings (chat/task/webhook/scheduler), the MCP key arg list, the #914 client-side receipt being a non-claim-consuming lookup, zero effect-key references anywhere, #1084 OPEN, and the 24h purge in cleanup_service. All primary_class assignments other than the two above held up.


---

Confirmed: `_restart_agent` consults ONLY Docker container status — never CapacityManager slots or running execution rows. This validates the SPLIT_STATE_AUTHORITY reclassification for #1037. The destructive action's gate reads the wrong authority for "is this agent busy."

I now have all evidence needed. Let me compile the corrected analysis.

## FAMILY: Subscription auto-switch interacting with executions

### Bugs

- **#285 — Expired subscription token causes hour-long zombie executions**
  - symptom: With an expired `CLAUDE_CODE_OAUTH_TOKEN`, some executions failed fast (~3 min) but others hung for the full execution timeout (up to 3600s), holding the slot and blocking the fleet — worse when one stale subscription was shared across many agents.
  - root_cause: Claude Code's behavior on an expired token is nondeterministic (silent internal token-refresh retries → indefinite hang). The backend had **no liveness/completion signal** — the only detector was the wall-clock execution timeout, so a hang was indistinguishable from a slow turn and held the slot for the full budget. The "auth" failure was invisible until the timeout fired.
  - primary_class: PUSH_DISPATCH_BLOCKING
  - secondary: TTL_HEURISTIC, MISCLASSIFIED_FAILURE *(the slot is held because the backend blocks on the whole turn (PUSH); the reason the hold runs an hour is that wall-clock timeout — a time-based guess — stands in for a real liveness probe (TTL_HEURISTIC), which is exactly what the watchdog replaced)*
  - fix_shape: new-sweep-or-path (added a 60s startup watchdog `_spawn_startup_watchdog()` in `docker/base-image/.../claude_code.py` that kills the subprocess on **no stdout/stderr within `CLAUDE_STARTUP_TIMEOUT`=60s**, converting a silent hang into a fast 503; backend `task_execution_service` classifies the resulting 503 → `AUTH`)
  - recurred: yes — the same 2-hour-hang *symptom* reappeared with a **different root cause** via leaked stdout-reader threads holding grandchild pipe FDs (#970 → fix #973, "false timed-out after 7200s"). Distinct mechanism (READER_RACE/ORPHAN_PROCESS), same operator-visible failure; the "hang → full-timeout slot hold" failure mode was not eliminated by the watchdog, only narrowed to the startup window.

- **#322 — Detect auth failures in stderr and abort early** *(the in-execution detector half of the #285 fix)*
  - symptom: Same as #285 from the operator's side; this PR added the mid-turn stderr detector.
  - root_cause: No mid-turn signal classified an expired token; the agent had to scan its own stderr for `_is_auth_failure_message()` patterns and self-kill, returning 503 so the backend could tag `error_code=AUTH`.
  - primary_class: MISCLASSIFIED_FAILURE
  - secondary: READER_RACE, TTL_HEURISTIC
  - fix_shape: reclassification (stderr pattern-match → kill → 503 → AUTH error code)
  - recurred: yes — the stderr-pattern + zero-token heuristic immediately produced false positives on **max-turns termination (#361)** and on **SIGKILL/OOM (#516→#517, then #904→#907)**. The substring heuristic it introduced is the seed of the entire classifier-drift sub-family below.

- **#361 — Max-turns termination misclassified as authentication failure** *(MISSING FROM THE ORIGINAL ANALYSIS)*
  - symptom: An execution that hit Claude Code's max-turns limit surfaced to the operator as a misleading "Subscription token may be expired" 503 on every cron tick.
  - root_cause: The #285/#322 auth-fallback heuristic had no precedence rule for a clean max-turns exit — it fell into the auth bucket. #517's own PR calls this "the same shape, different exit path" and labels its own fix a *partial* predecessor.
  - primary_class: MISCLASSIFIED_FAILURE
  - secondary: POINT_FIX_ACCRETION
  - fix_shape: reclassification (precedence guard for the max-turns exit ahead of the auth heuristic)
  - recurred: yes — the *identical class* re-appeared one exit-path over (signal kills) as #516/#517, then again at the backend/auto-switch layer as #904/#907. #361 is the first instance of the "every new non-auth exit shape that lands in the auth bucket needs a new guard" treadmill, so omitting it understates the accretion.

- **#516 / #517 — Signal-killed Claude exits misreported as "token expired"**
  - symptom: Timeouts, OOM-kills, and operator cancels surfaced to users as a misleading "Subscription token may be expired" 503 on every cron tick — masking the real cause.
  - root_cause: The #285/#322/#361 auth-fallback heuristic had no precedence rule for **external signal terminations**, so a SIGKILL (`return_code < 0` or shell-encoded 130/137/143) fell through into the auth bucket. Became routinely reproducible after **#61/PR #326** added backend-driven `terminate_execution_on_agent()` — every timeout now produces a signal-killed subprocess on the agent side, hitting the misclassification on each tick.
  - primary_class: MISCLASSIFIED_FAILURE
  - secondary: POINT_FIX_ACCRETION
  - fix_shape: reclassification (added `_classify_signal_exit()` → HTTP 504, consulted **before** auth heuristics; tightened the zero-token heuristic to require `return_code > 0`)
  - recurred: yes — same class re-fixed at the *other* layer in #904/#907 (the chat path `claude_code.py:450` still didn't call the signal classifier, and the backend's `is_auth_failure` substring matcher had no kill/OOM guard).
  - timeline correction: #517 was a **preventive** fix that merged **2026-04-26, one day BEFORE** auth-class auto-switch went live (#508, 2026-04-27). Its PR explicitly states it "de-risks PR #508 — without this fix, every timeout would trigger an unnecessary subscription rotation." So the "post-#441 it *wrongly triggered an auto-switch*, burning a healthy alternate's skip-list slot" framing is chronologically inverted for #517: that was a **prospective** risk #517 closed before #508 shipped, not an observed regression #517 cleaned up. (The skip-list-burn *did* later become an observed in-prod behavior — but at the #904 layer, after the chat path and the backend substring matcher were found to still be unguarded.)

- **#904 / #907 — False "token expired" on SIGKILL/OOM at the auto-switch + chat layers** *(driven by an agent-container OOM cascade — see note)*
  - symptom: Auth false-positives still reached `is_auth_failure()` and fired SUB-003 auto-switches on what were really kills/OOMs, polluting subscription selection and burning the 2h skip-list slot. The umbrella issue #904 is broader — its headline symptom is an agent OOM cascading into **backend uvicorn-worker saturation that froze the admin UI**; the false-auth-switch is RC-2/RC-3 within it.
  - root_cause: Two unguarded copies of the classification: (a) the chat path `claude_code.py:450` never called the `_classify_signal_exit` helper #516/#517 added (only the headless path did); (b) the backend's `is_auth_failure()` substring matcher in `subscription_auto_switch.py` (and its scheduler twin) had no kill/OOM guard — the same misclassification #517 fixed agent-side leaked into the backend's switch decision because **each surface re-implements the classification independently**.
  - primary_class: MISCLASSIFIED_FAILURE
  - secondary: PARALLEL_CODE_PATHS, POINT_FIX_ACCRETION
  - fix_shape: point-guard (added `NON_AUTH_KILL_MARKERS` list short-circuiting `is_auth_failure` → False; wired `_classify_signal_exit` into the chat path). **Correction:** the backend-call-budget exhaustion path is NOT "a synthetic 503 *carrying* those markers" — #907 RC-1 added a per-agent + global semaphore and a **dedicated `BackendAgentCallBudgetExhausted` exception** whose handler in `task_execution_service` *explicitly bypasses SUB-003* (no auto-switch, no 503 synthesis) and marks the row FAILED directly.
  - recurred: contained for now; the classifier is duplicated across **agent-server (`error_classifier.py` + `headless_executor.py`), backend (`subscription_auto_switch.py`), and scheduler (`src/scheduler/service.py`)** — at least three drift surfaces, all kept in sync by hand.

- **#441 / #508 — Auto-switch on first failure + auth-class triggers, default-on** *(#442 is the duplicate feature request — see correction)*
  - symptom: Agents stayed stuck on a broken subscription until a human noticed (old rule needed 2 consecutive 429s and never fired on auth-class errors); single failures wasted user-visible runs.
  - root_cause: This is the *amplifier*, not a fix — it lowered the switch threshold from 2→1, broadened the trigger from 429-only to the whole `AUTH_INDICATORS` surface, and flipped the `auto_switch_subscriptions` default to `"true"`. That made a credential/lifecycle action (container recreate) fire far more often against in-flight executions — the precondition for #799 and #1037.
  - primary_class: POINT_FIX_ACCRETION
  - secondary: MISCLASSIFIED_FAILURE, RACE_TOCTOU
  - fix_shape: feature-flag (default flip) + new-sweep-or-path (broadened trigger)
  - recurred: yes — directly enabled #799 (concurrent switches), #792 (one-shot exec lost), #1037 (recreate kills in-flight work), and forced the #904 kill-marker guard.
  - correction: **#442 is a near-duplicate of #441** — both acceptance lists ask for the identical change (threshold 2→1, broaden to auth-indicators, flip default off→on), and **both were satisfied by the same PR #508**. #442 simply remains open as an un-closed issue. It is *not* a "no-retry-of-one-shot" bug and does **not** belong with #792 (the original analysis mis-paired them). It belongs here, with #441, as the amplifier.

- **#799 — No per-agent lock; concurrent 429s race the switch (OPEN)**
  - symptom: Two concurrent failures on one agent both enter `handle_subscription_failure`, both pick the same alternative, both call `assign_subscription_to_agent` + `_restart_agent` → wedged container, duplicate switch notifications/activity rows, or a spurious `was_already_running` skip.
  - root_cause: The read-decide-write window from `get_agent_subscription_id` through `_restart_agent` return is unsynchronized check-then-act — no `asyncio.Lock`/CAS around it. Each failing execution fires the switch independently from its own failure handler (`task_execution_service.py:1042` / `chat.py:862,891`), so a burst of failures on one agent produces N concurrent unsynchronized switch attempts. (Confirmed: no lock primitive exists anywhere in `subscription_auto_switch.py`.)
  - primary_class: RACE_TOCTOU
  - secondary: MISSING_IDEMPOTENCY, SPLIT_STATE_AUTHORITY
  - fix_shape: open-unfixed (issue proposes a module-level per-agent lock; not yet in code)
  - recurred: n/a — never fixed; OPEN (P2, `status-ready`).

- **#1037 — Auto-switch recreates the container, killing in-flight executions (OPEN)**
  - symptom: A 429 on *one* task auto-switches the agent → `_restart_agent` does `container_stop` immediately → every *other* execution in the agent's parallel slots dies with `RemoteProtocolError`/`ReadError`, terminal-FAILED, spend wasted. One afternoon: 8 collateral failures off one shared subscription (9 switches total, 8 on one shared sub).
  - root_cause: A credential/lifecycle action decides "is this agent busy?" from the **wrong authority**. `_restart_agent` is gated only on Docker `container.status == "running"` (line 278) — i.e. "the container is up" — and **never consults `CapacityManager` slot occupancy or the `schedule_executions` running rows** that actually own the live turns (confirmed: no `capacity`/`slot`/`running` reference in the file). The "is-this-agent-running-work" fact lives in CapacityManager/SQL; the destructive action reads only Docker; the divergence between those authorities *is* the bug. The token requiring a recreate (build-time env var) is what makes the destructive action necessary, but the data-loss specifically comes from acting on container status instead of slot state.
  - primary_class: **SPLIT_STATE_AUTHORITY** *(changed from PUSH_DISPATCH_BLOCKING)*
  - secondary: PUSH_DISPATCH_BLOCKING, ORPHAN_PROCESS *(the `RemoteProtocolError` transport-drop is the PUSH symptom — the backend was blocked on `/api/task` when the peer was stopped — but the deeper cause is the action consulting the wrong state authority; the fix is literally "ask CapacityManager whether the agent has running slots before recreating," a reconciliation between the two authorities)*
  - fix_shape: open-unfixed (issue sketches defer-when-busy / bounded drain / token-inject-without-recreate)
  - recurred: n/a — OPEN, P1, `status-in-progress`; same no-drain `container_stop`→recreate path is also reachable from manual assign (`routers/subscriptions.py:252`) and rebuild/deploy (`agent_service/lifecycle.py recreate_container_with_updated_config`).

- **#792 — No retry of the triggering one-shot execution after a successful switch (OPEN)**
  - symptom: When the switch fires on a **one-shot trigger** (manual `trigger`, webhook on a non-recurring schedule, MCP `trigger_agent_schedule`), the triggering execution is marked FAILED and never replayed; the user must manually re-trigger after the (usually successful) switch.
  - root_cause: The switch repairs *future* dispatch but drops the execution that surfaced the 429. Interactive chat retries client-side (`routers/chat.py` returns `retry_after:15` + `auto_switch` payload) and recurring schedules recover at next cron tick — but one-shots have **no recovery path**, and a platform-driven replay is blocked because there is no execution-level idempotency to make it safe (external side effects could double-fire).
  - primary_class: MISSING_IDEMPOTENCY
  - secondary: PARALLEL_CODE_PATHS, POINT_FIX_ACCRETION *(the three recovery behaviors — chat client-retry, cron recurrence, the missing one-shot path — are three divergent implementations of "recover from a mid-turn switch")*
  - fix_shape: open-unfixed (gated on an idempotency model — see #525/Invariant #18 below)
  - recurred: n/a — OPEN (P2). (Note: the platform now *has* an idempotency primitive — `idempotency_keys` table + `services/idempotency_service.py`, RELIABILITY-006/#525 — that #792 could build on; it was not available when #792 was filed.)

### Family pattern
A credential-health signal (subscription failure) is wired to a *destructive lifecycle action* (full container recreate) that runs **synchronously against in-flight executions it doesn't own**. Every bug in this family is a collision between "the subscription looks broken" and "executions are still running." It splits into two intertwined sub-families:

1. **Detection drift** (#285 → #322 → **#361** → #516/#517 → #904/#907): a substring/zero-token heuristic stood in for a real auth signal, so every *new* non-auth exit shape (max-turns, then SIGKILL, then OOM, then call-budget) landed in the auth bucket and needed another precedence guard. Each guard was added to a *different copy* of the classifier.
2. **Destructive trigger** (#441/#442/#508 amplifier → #799 no-lock → #1037 no-drain → #792 no-replay): once the trigger was made hair-trigger and default-on, the action's flaws surfaced — it fires N times concurrently with no lock (#799), recreates the container off the wrong state authority with no drain (#1037), and loses the triggering one-shot with no idempotent replay (#792).

### Design pathology
The subscription token is injected as a **build-time/create-time container env var** (`CLAUDE_CODE_OAUTH_TOKEN`, written into `container.attrs["Config"]["Env"]`; `_env_matches_config` returns `False` on a token mismatch → forces a recreate). There is **no hot-reload path for the one credential auto-switch exists to rotate** — the `/api/credentials/update` agent endpoint reloads `.env`/`.mcp.json` files, not the Claude-Code process's own `CLAUDE_CODE_OAUTH_TOKEN` env, which is read at subprocess spawn. That single choice means "rotate a credential" and "kill every running turn on this agent" are the same operation — this is the **true original sin**; every destructive-trigger bug (#799/#1037/#792) is downstream of it.

Layered on top, a **second, independent** pathology: failure classification is a substring heuristic (`AUTH_INDICATORS` / `NON_AUTH_KILL_MARKERS`) standing in for a real auth signal, **physically duplicated across three containers** — `subscription_auto_switch.py` (backend), `src/scheduler/service.py` (scheduler), and `error_classifier.py`/`headless_executor.py` (agent-server) — kept in sync only by hand and a "keep in sync" comment. (Note: the backend file's own docstring claims the scheduler "now imports this same list" — that comment is **stale/wrong**; the scheduler explicitly says it *cannot* import from `backend.services` and maintains its own copy.) So the auth/timeout/kill boundary can never be authoritative and each new kill shape needs a new guard in N places.

A **third** pathology compounds the action side: the switch acts on split state (DB subscription row + Redis/CapacityManager slots + the live Docker container) with **no lock and no execution lease**, and the destructive step reads container status while the live-work truth lives in CapacityManager — so any concurrent or in-flight execution is collateral (#799, #1037).

### Residual risk
- **#1037 (P1, OPEN, in-progress):** auto-switch still destroys in-flight executions on every recreate; reachable from auto-switch, manual assign (`routers/subscriptions.py:252`), and rebuild/deploy (`lifecycle.py`). No drain, no token hot-reload, and the recreate decision reads Docker container status instead of CapacityManager slot occupancy.
- **#799 (OPEN, ready):** still no per-agent switch lock — burst-loaded agents can fire N concurrent unsynchronized switches, wedge a container, and trip the `was_already_running` ambiguity.
- **#792 (OPEN):** one-shot executions that trigger a switch are lost with no safe replay. The `idempotency_keys` primitive (#525) now exists to unblock it, but no execution-replay path is wired.
- **#442 (OPEN issue, functionally shipped):** duplicate of #441; its behavior already lives in #508. Open-but-done — a backlog-hygiene artifact, not a live risk.
- **Classifier drift:** `AUTH_INDICATORS` / `NON_AUTH_KILL_MARKERS` are maintained in **three containers** by convention (a cross-reference comment), not by a shared package — the next novel kill/OOM/timeout shape that contains an auth substring will re-trigger a false switch (the #361 → #517 → #904 pattern will recur). The stale "scheduler imports this list" docstring is itself evidence that the hand-sync convention is already drifting.

### Link to consolidation / pull redesign
- **#428 CapacityManager:** the switch has no notion of slot occupancy because `_restart_agent` queries Docker container status, not `CapacityManager` (confirmed). A drain guard (#1037) and the switch lock (#799) both need the unified admit/release/slot-count surface #428 created — the fix is "ask CapacityManager whether the agent is idle / has running slots" before recreating, which only became possible after the three-class pyramid was consolidated. This is the **same authority-reconciliation** the #1037 SPLIT_STATE_AUTHORITY reclassification names.
- **#525 idempotency (RELIABILITY-006 / Invariant #18):** #792's blocker — "no execution-level idempotency to make a replay safe" — is exactly what the `idempotency_keys` table + `idempotency_service` now provide. A one-shot replay after a switch becomes safe once the triggering execution carries an idempotency key, so a re-dispatch can't double-fire external side effects.
- **#1081 pull / work-stealing redesign:** this family is the canonical crash-taxonomy case the pull model addresses. Today dispatch is *push + blocking*: the backend holds the HTTP connection for the whole turn, so a recreate severs it and the turn is terminal-FAILED with no recovery (#285 hang, #1037 transport-drop, #792 lost one-shot). Under pull/work-stealing with **lease re-delivery** (gated on #1084 effect-idempotency, the productized #525), a container recreate would simply drop the lease and the next worker re-claims and re-runs the turn — converting "credential rotation kills the active turn" from a data-loss bug into transparent re-delivery. The token-hot-reload-vs-recreate pathology and the missing-idempotency gap (#792) are exactly what #1084 must close before pull can safely re-deliver these executions.

### Verifier notes

Changes made after checking every claim against the repo (`gh`, `git show`, `grep`, file reads):

1. **Added missing bug #361** (max-turns misclassified as auth). #517's own PR text names it "the same shape, different exit path" and labels itself a partial successor. It is the *first* instance of the detection-drift treadmill and was omitted from the chain, understating the accretion. Confirmed CLOSED via `gh`.

2. **Reclassified #1037 primary: PUSH_DISPATCH_BLOCKING → SPLIT_STATE_AUTHORITY** (PUSH demoted to secondary). Verified `_restart_agent` gates only on Docker `container.status == "running"` (line 278) and never references `CapacityManager`, slots, or running-execution rows. The `RemoteProtocolError` is the PUSH *symptom*, but the data-loss cause is the action reading container status while the live-work authority lives in CapacityManager/SQL — the divergence between authorities is the bug, and the proposed fix is literally to reconcile them. Deeper cause wins per the rubric.

3. **Corrected the #442 placement.** The original paired "#792 / #442" as one "no-retry-of-one-shot" bug. Read both issue bodies: **#442 is a near-duplicate of #441** (identical acceptance criteria — threshold 2→1, broaden to auth-indicators, flip default on) and **both were satisfied by PR #508**. #442 is an open-but-functionally-shipped backlog artifact and belongs with the #441/#508 amplifier, not with the open one-shot-retry bug. Split #792 out as its own standalone MISSING_IDEMPOTENCY entry.

4. **Fixed the #517 timeline.** `git show` dates: #517 merged 2026-04-26, #508 merged 2026-04-27 — #517 landed **one day before** auth-class auto-switch went live. #517's PR explicitly says it "de-risks PR #508." So the "post-#441 it wrongly *triggered* an auto-switch burning a skip-list slot" was a **prospective risk #517 closed**, not an observed regression #517 fixed. The skip-list-burn *did* become real later, at the #904 layer — re-attributed accordingly.

5. **Corrected the #907 call-budget fix_shape detail.** The original said exhaustion was "translated to a synthetic 503 *carrying* those markers." Read `task_execution_service.py:974` — #907 RC-1 added a dedicated `BackendAgentCallBudgetExhausted` exception whose handler **explicitly bypasses SUB-003** and marks the row FAILED; there is no synthetic-503-with-markers. The marker list is a *separate* mechanism (the `is_auth_failure` guard). Corrected.

6. **Strengthened the classifier-drift evidence and flagged a stale in-repo comment.** Confirmed THREE physical copies (`subscription_auto_switch.py`, `src/scheduler/service.py`, `error_classifier.py`/`headless_executor.py`). The original's "three drift surfaces" claim holds. Added the finding that `subscription_auto_switch.py`'s docstring claims the scheduler "now imports this same list" — which is **false**; the scheduler comment says it *cannot* import and keeps a duplicate. The drift the analysis warns about is already visible in the doc comment.

7. **Confirmed (unchanged):** the design pathology (token = create-time `Config.Env` var → recreate required; `/api/credentials/update` does not hot-reload it), #799 has no lock primitive in code, #1037 reachable from manual-assign + lifecycle, #285 watchdog fix shape, default-on flip, and the #285 "recurred via #970/#973" claim (kept, but annotated that it is a *same-symptom / different-root-cause* recurrence, not the same bug).

Everything else in the original held up and was retained.


---

This validates the design-pathology core. The state-machine doc explicitly lists **multiple authorized writers** (TaskExecutionService, BacklogService, CleanupService, terminate handler, scheduler retry handler) for `schedule_executions.status`, and there are ~35 status-comparison SQL sites in `db/schedules.py`. The analysis's claim that "no single structure owns is-running" and there are ~12 writers of status is well-founded (the "~12 writers" figure the analysis cites from #524 is in the ballpark; the model doc lists 5+ distinct writer roles across more call sites).

I now have full forensic coverage. Let me do a final confirmation on the one remaining loose end — whether the S-01/E-02 RACE_TOCTOU secondary is correctly the secondary (not primary). The #378/#403 root cause is a two-phase cleanup race, but the analysis correctly classes it SPLIT_STATE_AUTHORITY primary (deeper cause) per the vocabulary instruction. That holds.

Summary of my verification — only **one** substantive error found (the E-02 "recurred-in-itself" fabrication), one minor nuance (R-01 regex doc lag, already correctly noted). Everything else held up under direct git/gh inspection. Here is the corrected analysis.

---

## FAMILY: Canary invariant harness (the meta-watcher)

### Bugs

- **#411 — Orchestration invariant catalog + continuous canary harness (the epic)**
  - symptom: A run of orchestration bugs (#378/#403 phantom slot failures, #407 zombie spin, #129 stuck executions, #226 premature slot expiry) all lived in cross-component state that unit tests structurally could not see; each was found in production by a user or operator, not by a test.
  - root_cause: The orchestration layer's truth about "is-X-running / capacity / queued" is split across Redis ZSET × SQLite row × Docker process registry × agent RAM, with no transaction spanning them. Divergence between stores *is* the bug class, and divergence only manifests in live cross-store state.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: POINT_FIX_ACCRETION
  - fix_shape: new-sweep-or-path (a whole new read-only reconciliation harness — does not remove any cause; it continuously *observes* the divergence the splits produce)
  - recurred: this is the meta-response to recurrence itself; the harness exists *because* the underlying bugs recurred under point-fixes (#94→#219→#226→#378/#403; #106→#129). [Verified: #411 OPEN, type-epic, P1; PR #653 body explicitly names "#378, #403, #129, #226" as the recurring class.]

- **#653 / PR — Phase 1: S-01 (slot↔row bijection), E-02 (no phantom reversal), L-03 (delete cascades)**
  - symptom: No new user symptom; encodes three already-shipped fixes as standing invariants. S-01 directly watches the #378/#403 pathology (Redis slot set ≠ SQL running rows), E-02 watches terminal-status backsliding, L-03 watches agent-delete orphans.
  - root_cause: S-01 and E-02 both exist *only because* "is running" is dual-sourced (Redis ZSET + SQL `status`); L-03 exists because agent delete had no enforced cascade across the cross-cutting tables + Redis slot keys.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: RACE_TOCTOU (S-01/E-02 fire on cleanup-vs-completion races), none for L-03 which is MISSING_IDEMPOTENCY-adjacent cascade
  - fix_shape: new-sweep-or-path
  - recurred: **no.** [CORRECTED — see Verifier notes.] The original claim ("E-02's early version DEL'd the whole `canary:e02:terminal_seen` key at a 5000-row cap and lost in-window terminal ids; replaced with score-based `ZREMRANGEBYSCORE`; the watcher caught a regression in itself") is **not supported by git history**. The E-02 file (`canary/invariants/e02_no_phantom_reversal.py`) has only two commits: the Phase 1 ship (a4eec13d, #653) and the v0.6.0 release sync (521c7935). The Phase 1 commit **already shipped the score-based `ZREMRANGEBYSCORE` approach**, and the "earlier hard count cap (5000) that DEL'd the entire key" appears *only as a design-rationale docstring* describing an alternative that was rejected *during development* — never a committed, shipped, then-fixed version. There is no follow-up fix PR, no recurrence. (The genuine canary self-regressions are S-03 and B-02 — see #882 below — not E-02.)

- **#882 / #884 — Phase 2+3: S-02, E-01, E-05, B-01, S-03, B-02, R-01 (seven invariants)**
  - symptom: Catalog widening — each new check pins a distinct prior pathology: S-02 (overbooking past `max_parallel_tasks`), E-01 (row stuck `running` past timeout+buffer = cleanup failed to act, the #129 gap), E-05 (`running` >60s with NULL `claude_session_id`, the #106 silent-launch class), S-03 (slot TTL below `timeout+300s`, the #226 fixed-20-min class), B-02 (queued rows with free slots + stale drain = stalled drain), R-01 (zombie `claude` in agent containers, the #407 class).
  - root_cause: The same split-state authority, now watched from more angles — S-02/S-03 watch Redis-internal capacity/TTL truth; E-01/E-05 watch SQL row truth diverging from real liveness; R-01 watches OS process truth diverging from the agent's process tracking; B-02 watches the drain heartbeat.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: TTL_HEURISTIC (S-03, E-01, B-02 all encode time-based floors as proxies for real completion signals), ORPHAN_PROCESS (R-01), TTL_HEURISTIC again for S-03's #226 root
  - fix_shape: new-sweep-or-path
  - recurred: yes — **two of the canary's own invariants shipped buggy and needed their own point-fixes.** S-03's raw `ttl < floor` check false-fired by ~1s on every healthy slot (masked initially by #913 storing timeout=900); fixed by reconstructing initial-TTL as `ttl + age` (commit **d2148677**, "fix(canary): S-03 decay-invariance — initial-TTL reconstruction", 2026-05-23). B-02 false-fired in the boot window; fixed in **659df68f** ("fix(canary): /review fixes — alert quality + B-02 boot-window false-positive", 2026-05-18). The harness keeps needing its *own* point-fixes — POINT_FIX_ACCRETION reproduced one level up. [Both commits verified verbatim. NOTE: this recurrence signal is genuine and load-bearing — it survives the removal of the false E-02 example.]

- **#378/#403 — Phantom stale-slot failure (the bug S-01/E-02 guard)**
  - symptom: Execution shows "Failed — Stale Execution Slot TTL Expired" then flips to "Success" ~14s later; false-positive failure on first render.
  - root_cause: Cleanup service Phase 0 (batch watchdog read) and Phase 3 (slot reclaim) raced against the agent completing — an agent dropped a just-completed execution from its registry between the two phases, so cleanup wrote FAILED seconds before the agent's in-flight SUCCESS landed. The two stores disagreed; whichever wrote last "won."
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: RACE_TOCTOU, TTL_HEURISTIC
  - fix_shape: point-guard (JIT per-agent re-verify before writing FAILED) — did not collapse the dual source; added a re-check
  - recurred: yes — the JIT re-verify is itself a patch (`cleanup_service.py` even documents a residual race: lines 681–703, "agent unreachable during re-verify... slot reclaimed"). The canary S-01/E-02 were created precisely because this patch "holds until it doesn't." Followed by #524 (per-write CAS guards) and #1082 (status-as-projection) — neither fully collapses the dual source. [Verified: PR #403 = "fix(cleanup): prevent phantom stale-slot failures via JIT re-verify (#378)", merged 2026-04-19; #378 is the issue, #403 the PR; residual-risk code path confirmed in cleanup_service.py.]

- **#226 — Stale slot cleanup uses fixed 20-min TTL (the bug S-03 guards)**
  - symptom: Long-timeout agents (60–120 min) had slots reclaimed mid-execution while still legitimately running.
  - root_cause: `acquire_slot` computed per-agent `timeout + buffer`, but the periodic `cleanup_stale_slots` sweep defaulted to `DEFAULT_SLOT_TTL_SECONDS = 1200`. A time-based guess stood in for the real per-agent completion window.
  - primary_class: TTL_HEURISTIC
  - secondary: SPLIT_STATE_AUTHORITY (TTL lived in two places — acquire metadata vs sweep default)
  - fix_shape: point-guard (read `timeout_seconds` from slot metadata in the sweep)
  - recurred: yes — re-surfaced via #913 ("bug: scheduled runs ignore per-agent execution_timeout_seconds (canary S-03 + E-01 surfaced)"), which in turn unmasked the S-03 canary's own decay bug. The TTL-as-liveness-proxy keeps producing new variants. [Verified: #226 CLOSED; #913 CLOSED and its title literally credits "canary S-03 + E-01 surfaced" — the harness caught the recurrence it was built to catch.]

- **#106 — Cleanup misses skipped + slow no-session detection (the bug E-05 guards)**
  - symptom: Executions `running` with `claude_session_id = NULL` (silent launch failure) sat orphaned 2 hours holding capacity; `skipped` rows accumulated for 5+ hours with no `completed_at`.
  - root_cause: The cleanup query only matched `status='running'` and used the generic 120-min stale window — a dispatched-but-dead execution had no fast liveness signal, so it leaked a slot until the coarse TTL expired.
  - primary_class: ORPHAN_PROCESS (leaked slot outliving the execution)
  - secondary: TTL_HEURISTIC, SPLIT_STATE_AUTHORITY
  - fix_shape: point-guard (add `mark_no_session_executions_failed` at 60s + match `skipped`)
  - recurred: E-05 + E-01 exist as standing regression guards because this is "cleanup missed a status again" — the same shape recurs whenever a new terminal/non-terminal status is introduced. [Verified: #106 CLOSED; `mark_no_session_executions_failed` exists in db/schedules.py with a CAS guard (`WHERE id = ? AND status = ? AND claude_session_id IS NULL`).]

- **#407 — agent-server spins at 83% CPU on defunct claude child (the bug R-01 guards)**
  - symptom: A `claude` subprocess exits `<defunct>`, the parent never reaps it, agent-server pins a core and stops answering *all* HTTP; backend's transport circuit breaker re-opens every ~35s; tasks watchdog-terminated and then **misclassified** downstream as "Auth failure — subscription token may be expired."
  - root_cause: A child process outlived the parent's reaping logic (no `wait()` on every exit path); the OS process table diverged from the agent's process tracking.
  - primary_class: ORPHAN_PROCESS
  - secondary: MISCLASSIFIED_FAILURE (SIGINT-from-watchdog reported as auth/token failure), PUSH_DISPATCH_BLOCKING (hung agent ties up dispatch)
  - fix_shape: point-guard (restore reaping)
  - recurred: R-01 is the standing guard; the misclassification tail is a separate dispatch-breaker concern (#526), evidence the symptom fanned out across subsystems. [Verified: #407 CLOSED; R-01 implemented in r01_zombie_claude.py with anchored `^Z.*claude` regex in the check message (a stale docstring line still shows the catalog's ` Z` leading-space form — cosmetic doc lag, the check itself uses `^Z`, consistent with architecture.md Invariant note).]

- **#129 — Active watchdog: remediate stuck executions (the bug E-01/L-03 guard)**
  - symptom: Monitoring detected `running > 30 min` and flagged "degraded" but took no action; a 90-min dead-zone between detection (30 min) and passive cleanup (120 min) where a slot was wasted, scheduler blocked, no notification.
  - root_cause: Detection and remediation were disconnected — the monitoring store knew the truth, the cleanup store acted on a coarser timer, and neither reconciled the SQL `running` row against the agent's actual registry within one cycle.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: TTL_HEURISTIC, ORPHAN_PROCESS
  - fix_shape: new-sweep-or-path (active watchdog reconciliation in cleanup_service)
  - recurred: L-03 (orphan refs) + E-01 (closure) both encode this; the broader fix is CLEANUP-001's continuing accretion of reconciliation phases (Phase 0/1/1b/1c/3). [Verified: #129 CLOSED.]

- **#1082 — status-as-projection (RETIRES S-01)**
  - symptom: No symptom — a refactor proposal. Acceptance criteria explicitly: "Canary S-01 documented as redundant once the single-owner invariant holds (retire or downgrade)."
  - root_cause: S-01 only has meaning *because* "is running" is split across Redis ZSET + SQL row + agent RAM and must be reconciled. #1082 makes `schedule_executions.status` a CAS-guarded projection of one authoritative terminal event, with no reader treating `status` as the authoritative "is running" answer. Once a single structure owns the fact, "Redis disagrees with SQL" becomes structurally impossible — there is nothing left to reconcile, so the bijection check has no bug to catch.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: RACE_TOCTOU
  - fix_shape: structural-consolidation (then deletion of the S-01 invariant)
  - recurred: open/unfixed; #524 (RELIABILITY-005) shipped only a *per-write* CAS guard (`WHERE id=? AND status=?` in `db/schedules.py`), **not** the single-owner collapse — the dual source still exists, which is why S-01 cannot be retired yet. [Verified — strongly. #1082 OPEN; its body and AC match verbatim. PR #524/#541 ("fix(reliability): CAS guards on execution status writes + state machine doc", commit 0e600ada, merged 2026-04-27) **explicitly defers** the structural fix: "Full `ExecutionStateProjector` architecture... single-writer guarantee by construction) is **deferred** — agents have no Redis access and the restart-recovery design needs more thought." The `TaskExecutionStatus` state-machine doc in models.py lists 5+ distinct authorized writers (TaskExecutionService, BacklogService, CleanupService, terminate handler, scheduler retry), and `db/schedules.py` has ~35 status-comparison SQL sites — the dual-source / multi-writer design is intact. The analyst's forensic claim in their preamble is correct.]

### Family pattern
Every invariant in the harness is a *standing reconciliation check between two stores that hold the same fact*: Redis ZSET vs SQL `running` rows (S-01), Redis vs cap (S-02), SQL row vs real liveness/timeout (E-01/E-05), slot metadata TTL vs declared timeout (S-03), drain heartbeat vs queue depth (B-02), OS process table vs agent tracking (R-01), live row vs `agent_ownership` membership (L-03). The canary is not a feature — it is the *instrument that measures split-brain*, built because the same FAILED↔SUCCESS / leaked-slot / stuck-row pathologies kept recurring under point-fixes. Its own growth (3 → 10 → +3 planned in #1077, the E-04/E-03/G-03 Phase 4 batch) and its own internal point-fixes (**S-03 decay false-positive [d2148677], B-02 boot-window [659df68f]**) are the strongest meta-signal: the bug class was intractable by point-fix, so the team built a permanent watcher instead of curing the cause. [CORRECTED: removed the "E-02 cap-DEL blind spot" from the list of internal point-fixes — it was never a shipped regression. The two genuine canary self-regressions, S-03 and B-02, fully carry the meta-signal.]

### Design pathology
The original sin is that **no single structure owns "is this execution running."** Slot capacity lives in a Redis ZSET (for atomic N-ary acquisition), execution status lives in a SQLite row (for durability/history), liveness lives in the agent's process registry + RAM, and the OS owns the actual process. These four were never made transactional or hierarchical, so the backend perpetually *guesses* state from outside the agent and patches the gaps with time-based TTLs and reconciliation sweeps (cleanup phases 0/1/1b/1c/3; the `TaskExecutionStatus` doc names 5+ authorized writers of `status` across ~35 SQL sites). Every guess can be wrong, every sweep can race the thing it sweeps, and every TTL is a heuristic standing in for a real completion signal — so the bugs are structurally infinite and the only scalable response was to *continuously measure the divergence* rather than prevent it. [Confirmed as the TRUE original sin, not a proximate one — the multi-writer / multi-store split is verified in the model docstring and is what every invariant probes.]

### Residual risk
The canary cures nothing — it is pure observation that fires after the fact (5-min cadence; real bugs "persist across a cycle by definition"), so a user can still see a phantom failure before the operator sees the alert. It accretes its own fragility: each invariant carries hand-tuned grace windows (S-01 `GRACE_SECONDS=3`, E-05 60s, B-02 60s, E-01 `timeout+300`) and hard-coded constants deliberately *not* imported from runtime (so config drift silently invalidates a check — E-01 explicitly notes this). It needs its own regression-fixes (**S-03 and B-02 both shipped buggy and were patched post-merge**). Severity tuning is a judgment call (R-01 "slow-fuse," E-05 "major not critical"). And the deepest risk: the harness's *existence* can become an excuse to defer the structural fix — as long as the canary catches divergence, the split-state authority underneath survives. #1082 (retire S-01), #524's full single-writer collapse (explicitly deferred in the shipped PR), and #1077's Phase 4 are all still open. [CORRECTED: dropped E-02 from the list of self-regressions; it never had one. S-03 + B-02 are sufficient and verified.]

### Link to consolidation / pull redesign
The canary is the diagnostic counterpart to the #428 CapacityManager consolidation: #428 collapsed three parallel capacity abstractions (ExecutionQueue + SlotService + BacklogService) into one, which is exactly what made B-01 "trivially green" (the backlog *became* `status='queued'` rows — no secondary representation to disagree). Consolidation removes the divergence the canary was built to watch; B-01 now exists only as a guard against a *future* re-split (a cache on `get_queued_count`). [Verified verbatim in b01_queue_status_coherence.py: "After the #428 CapacityManager consolidation the backlog has no [secondary representation]... Today this check is trivially-green. That's fine — it's a regression [guard against] a cache to `db.get_queued_count`."] The strongest link is to the #1081 pull/work-stealing redesign via its bankable-win-#1 sub-issue #1082: making `status` a CAS-guarded single-owner projection structurally eliminates the slot↔row split, which is the *only* reason S-01 (the canary's flagship invariant) exists — so #1082 explicitly retires S-01. This is the clean meta-arc: the push-dispatch + split-state design *spawned* the canary; the pull redesign + status-as-projection *retires it, invariant by invariant*. Each invariant the consolidation makes structurally impossible is one the harness can delete — the canary's eventual shrinkage is the success metric for the redesign. [#428 CLOSED, #1082/#1081 OPEN — all verified.]

### Verifier notes

**One substantive correction; everything else confirmed.**

1. **#653 / E-02 "recurred: caught a regression in itself" — FABRICATED, corrected to `recurred: no`.** The original analysis claimed the early E-02 implementation "DEL'd the whole `canary:e02:terminal_seen` key at a 5000-row cap and lost in-window terminal ids; replaced with score-based `ZREMRANGEBYSCORE`... The watcher caught a regression in *itself*." Git history refutes this: `canary/invariants/e02_no_phantom_reversal.py` has exactly two commits (Phase 1 ship a4eec13d / #653, and release sync 521c7935 / v0.6.0). The Phase 1 commit **already contained the `ZREMRANGEBYSCORE` score-based approach**, and the "5000-row hard cap that DEL'd the whole key" exists only as a *docstring describing a rejected design alternative* — never a shipped, then-fixed version, no follow-up PR. I removed this false example from the bug entry, the Family pattern, and the Residual risk sections. The meta-signal it was supporting (the harness needing its own point-fixes) is **independently and genuinely true** via S-03 (d2148677) and B-02 (659df68f), both of which I verified verbatim — so no conclusion changes.

2. **Minor doc-lag note added to #407/R-01**: the R-01 docstring line still shows the catalog's ` Z` (leading-space) regex while the actual check message uses anchored `^Z.*claude`. Cosmetic only; matches the architecture.md note. Not an analysis error — added for precision.

**Confirmed against repo (no change needed):**
- #524/#541 CAS guard is a *per-write* guard, NOT single-owner collapse — verified in PR body ("Full ExecutionStateProjector... deferred"), commit 0e600ada, and the multi-writer state-machine doc in models.py. The analyst's forensic preamble claim is correct.
- S-03 decay-invariance fix (d2148677) and B-02 boot-window fix (659df68f) — both commits exist verbatim with matching messages.
- #913 title literally credits "canary S-03 + E-01 surfaced" — the recurrence-detection chain (#226→#913→S-03 self-bug) is real.
- #403 = JIT re-verify PR for #378 with documented residual race (cleanup_service.py:681–703).
- B-01 "#428 consolidation → trivially-green → future re-split guard" — verbatim in b01_queue_status_coherence.py.
- All issue/PR states (#411 OPEN epic, #428 CLOSED, #1082 OPEN, #1077 OPEN E-04/E-03/G-03, #106/#129/#226/#407 CLOSED) confirmed via `gh`.
- All primary_class assignments hold; the SPLIT_STATE_AUTHORITY-over-RACE_TOCTOU "deeper cause" rule is correctly applied throughout (S-01/E-02/#378/#129). The design_pathology is the true original sin, not a proximate one.


---

All claims verified. The design pathology holds: #428's own dependency note ("#306 push-based completion must land first... much of the current TTL/drain machinery exists to compensate for blocking HTTP dispatch") confirms the analyst's framing that push-in-process dispatch is *why* durability has to be reconciled via sweeps — but the analyst already captures this in the design_pathology section ("Because dispatch is push and in-process..."). So "no single admission/queue authority" + the push-in-process model together are correctly identified as co-original-sins.

I now have everything needed. Here is the corrected analysis.

---

## FAMILY: Loops, persistent backlog, fan-out

### Bugs

- **#260 / #316 — Persistent task backlog (BACKLOG-001) lands on a split-state foundation**
  - symptom: async `/task` requests at full capacity returned HTTP 429 instead of queueing; the feature was the marquee P1 reliability ask.
  - root_cause: the design persists the queue as `status='queued'` rows in SQLite, but liveness/capacity to admit-or-drain lives in the Redis slot ZSET, and the drain logic must hold both consistent (acquire slot *before* claim, release-claim-back on any post-claim failure — confirmed in PR #316: "Drain acquires the slot before claiming the row... On any failure after claim, row is put back via `release_claim_to_queued`"). The whole feature is a reconciliation harness between two stores for one fact ("is there room"). Plus the cross-module dependency forced a lazy import (the seed of #496 — PR #316 explicitly notes "`BacklogService` late-imports `_execute_task_background` to avoid `routers.chat ↔ services.backlog_service` cycle").
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: RACE_TOCTOU, PARALLEL_CODE_PATHS
  - fix_shape: new-sweep-or-path (new service + 60s maintenance sweep + release callback)
  - recurred: yes — broke immediately and silently (#496), then was structurally absorbed by #428; canary B-01/B-02 added later precisely to guard this surface.

- **#496 / #500 — Backlog drain spawn dead since #95 (lazy-import target deleted)**
  - symptom: every queued task drained straight to `failed`; 23 drain failures / 24h on a live fan-out workload; BACKLOG-001 "silently dead for weeks."
  - root_cause: `_spawn_drain` lazy-imported `_execute_task_background` from `routers/chat.py` to break a `chat ↔ backlog` import cycle; refactor #95 renamed it to `_run_async_task_with_persistence` without touching the string-literal import. The exception was swallowed at the drain catch site (`backlog_service.py:218-228`), and the happy-path test stubbed `routers.chat` in `sys.modules` (`types.SimpleNamespace(_run_async_task_with_persistence=...)`, still visible at `tests/unit/test_backlog.py:738`) so the real `ImportError` never surfaced. A fix (#95) broke a drain.
  - primary_class: PARALLEL_CODE_PATHS
  - secondary: POINT_FIX_ACCRETION, MISCLASSIFIED_FAILURE (every drained task reported `failed` regardless of the real cause)
  - fix_shape: point-guard (corrected import string) + new-sweep-or-path (AST regression test at `test_backlog.py:847` + stable `backlog_drain_spawn_failed` log token at `backlog_service.py:228` for fleet detection)
  - recurred: contained, not cured — **verified**: the import is still a string literal resolved at runtime (`backlog_service.py:255`: `from routers.chat import _run_async_task_with_persistence`); the AST test guards only the symbol *name* (`LAZY_IMPORT_TARGETS = ("_run_async_task_with_persistence",)`), and the cross-module cycle that forced the lazy import still exists. No follow-up PR has touched `backlog_service.py` since #500. #428 reduced the blast radius by folding the drain into `CapacityManager` but did not remove the lazy import.

- **#230 / #233 — Fan-out primitive shares the slot budget without owning admission**
  - symptom: agents had no batch-and-collect map-reduce primitive; once added, N parallel subtasks compete for the same `max_parallel_tasks` budget as all other traffic.
  - root_cause: fan-out is a *second* parallel dispatcher (`asyncio.Semaphore(max_concurrency)`, confirmed at `fan_out_service.py:107`) layered on top of the slot service's `max_parallel_tasks`. The two limiters are sequential, not coordinated: the semaphore throttles the *rate of dispatch into* `task_execution_service.execute_task()`, which then hits the slot/`CapacityManager` admit path — so the semaphore admits work the slot layer can still overflow to backlog or reject (each subtask calls `execute_task(...timeout_seconds=None, fan_out_id=...)` at `fan_out_service.py:128-138`). Partial-result join state lives only in process RAM as a `results: dict[str, FanOutTaskResult]` keyed by task id under `fan_out_id` (`fan_out_service.py:109`).
  - primary_class: PARALLEL_CODE_PATHS
  - secondary: SPLIT_STATE_AUTHORITY (two concurrency authorities for one budget), PUSH_DISPATCH_BLOCKING (coordinator holds the request open for the whole barrier-wait)
  - fix_shape: new-sweep-or-path (new service + new `fan_out_id` column + new endpoint/tool)
  - recurred: yes — #418/#423 (timeout).

- **#418 / #423 — Inter-agent / fan-out subtask timeout default of 600s shadows the per-agent config across every dispatch layer**
  - symptom: `[TaskExecService] TIMEOUT after 610s (limit=600s)` → "empty response" / "Broken pipe"; ~120–150 failed sub-executions across 7 delegate agents in ~80 min during one parent schedule.
  - root_cause: **[CORRECTED — the issue's hypothesis was wrong; the PR found a different, shallower-store cause]**. Issue #418 *guessed* a hardcoded 600s ceiling buried *inside* `TaskExecService`. The actual fix (#423, commit `eb4dad98`) shows the root cause was **upstream of the service, duplicated across 5 distinct dispatch sites in 3 files**: `routers/fan_out.py` (`timeout_seconds: int = 600`), `mcp-server/src/tools/chat.ts` (`.default(600)` on *both* `chat_with_agent` and `fan_out` Zod schemas), and `mcp-server/src/client.ts` (three separate `options?.timeout_seconds || 600` sites: request body + sync ceiling + async ceiling). Each copy filled in `600` *before* a `None` could reach `TaskExecutionService`'s per-agent resolution (TIMEOUT-001), so the long subtask was killed at the deadline and the killed turn surfaced as an empty body / broken pipe. The fix was N parallel deletions of the same default plus raising the HTTP client ceiling to `7200+60`. A real per-agent timeout *signal* existed (`execution_timeout_seconds`) — the bug was N drifted copies of a default value shadowing it, not a heuristic standing in for a missing signal.
  - primary_class: PARALLEL_CODE_PATHS **[CHANGED from TTL_HEURISTIC]** — the same default lived in 5 dispatch layers that each had to independently learn to defer to the per-agent config; this is the duplicated-path signature, and the analyst's own `recurred` note ("a convention 'always pass None' replicated across every dispatcher") names it.
  - secondary: TTL_HEURISTIC (a fixed 600s value did stand as the operative ceiling), MISCLASSIFIED_FAILURE (SIGKILL-at-deadline reported as "empty response"/"broken pipe"), READER_RACE (truncated stdout on kill)
  - fix_shape: point-guard *replicated N times* — borderline new-sweep-or-path-by-convention. The fix is "drop the default / pass None" applied at every dispatch site plus a `tests/test_inter_agent_timeout_unit.py` guard; there is no single enforced ceiling, so a new dispatcher can re-introduce the default. Call it **point-guard (multi-site)**.
  - recurred: **verified no recurrence** — `git log` since 2026-04-20 shows zero further commits touching `fan_out_service.py`. But the fix is a convention ("always pass None / no default") replicated across every dispatcher, not a single enforced ceiling — the latent re-introduction risk remains.

- **#740 / #902 — Sequential loops, a third execution primitive sharing the budget**
  - symptom: no primitive for sequential bounded repetition; client-side looping broke on the 60s MCP timeout.
  - root_cause: loops are a *third* dispatcher (`single-turn / parallel fan-out / sequential loop`) all funnelling into `task_execution_service.execute_task()` and `capacity_manager` (confirmed: `loop_service.py` iteration calls `execute_task(triggered_by='loop', loop_id=...)`). Loop iteration state lives in `agent_loops`/`agent_loop_runs` (SQL) **and** in an in-process `_LoopHandle` dataclass (`loop_service.py:56`, holding `task: asyncio.Task` + `should_stop: bool`, stored in `self._handles: dict[str, _LoopHandle]` at `:86`) — the same "is this loop alive / should it stop" fact in two stores, reconciled only by a startup sweep.
  - primary_class: PARALLEL_CODE_PATHS
  - secondary: SPLIT_STATE_AUTHORITY (SQL row vs in-RAM handle), ORPHAN_PROCESS (handle lost on restart → row stuck non-terminal)
  - fix_shape: new-sweep-or-path (new service + new tables + `mark_orphan_loops_interrupted()` startup sweep)
  - recurred: no later fix yet (newest in family — **verified**: `loop_service.py` has exactly one commit, the original #902) — but it re-imports the same split-state + restart-recovery pattern the backlog already paid for.

- **#740/#902 — Loop restart recovery is a sweep, not a guarantee**
  - symptom: on backend restart, a `running`/`queued` loop row has no in-process runner; `stop_loop` on such a row finds no handle.
  - root_cause: the in-process `asyncio.Task` is the only thing advancing a loop; it does not survive a restart, so durability is faked by flipping orphan rows to `interrupted` at startup (`cleanup_service` → `db.mark_orphan_loops_interrupted()`, referenced at `loop_service.py:17`) and by `stop_loop` finalizing handle-less non-terminal rows (`loop_service.py:151-158`: if `self._handles.get(loop_id)` is None, finalize as `interrupted`). Loops never auto-resume — durable-looking SQL state, ephemeral execution authority.
  - primary_class: ORPHAN_PROCESS
  - secondary: SPLIT_STATE_AUTHORITY, RACE_TOCTOU (startup recovery vs a loop spawned by a sibling worker)
  - fix_shape: new-sweep-or-path
  - recurred: no evidence (recent).

- **Canary B-01 — Queue-status coherence guard**
  - symptom: (preventive) the day `db.get_queued_count` gains a cache/Redis read-through with wrong invalidation, a queued task becomes invisible or double-counted and the drain skips or stalls it.
  - root_cause: queue depth is a single fact that *was* split across stores pre-#428 and could be re-split by any future cache; B-01 cross-checks the production accessor (`db.get_queued_count`, `snapshot.py:360`) against an independent id-count collected in a different code path. Author's own note: "trivially-green today" after #428 removed the secondary representation.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: POINT_FIX_ACCRETION (a standing guard added because the area kept re-splitting state)
  - fix_shape: new-sweep-or-path (deterministic invariant, 5-min watcher)
  - recurred: n/a (guard).

- **Canary B-02 — No queued-without-slots-full (stuck-drain detector)**
  - symptom: queued rows exist, free slots exist, yet nothing drains — the user's task is invisible and the agent looks idle (exactly the #496 failure mode, but for any future stall).
  - root_cause: the drain pipeline depends on a release callback firing + a 60s maintenance heartbeat written to `canary:drain_tick_at` at the END of the sweep (`snapshot.py:187-193, 642`); if either silently dies (as the lazy import did), there is no other signal. B-02 reconciles {queued rows} × {free slots} × {drain-tick freshness} to catch a wedged drain.
  - primary_class: SPLIT_STATE_AUTHORITY
  - secondary: TTL_HEURISTIC (60s grace window stands in for a real "drain is alive" signal), ORPHAN_PROCESS
  - fix_shape: new-sweep-or-path
  - recurred: n/a (guard) — note it exists *because* #496 proved a drain can die invisibly.

### Family pattern
Every member is the same move: **add another trigger type that funnels into the one slot/execution budget, then discover the budget's authority is split across Redis + SQL + process RAM and must be reconciled by hand.** `chat → fan-out → backlog → loop` each added a parallel dispatcher and at least one new place where "is-X-running / how-many-queued / should-this-stop / what-timeout" is stored. The bugs are never in the feature logic — they're in the seams between the new dispatcher and the shared capacity machinery (drain spawn import, **the 600s default duplicated across five dispatch sites**, in-RAM handle vs SQL row, semaphore vs slot-cap). Detection is the recurring second failure: #496 ran for weeks because the failure was swallowed and the test stubbed the real path — which is why the family terminates in *canary invariants* (B-01/B-02) rather than another feature.

### Design pathology
The original sin is **no single admission/queue authority, made unavoidable by push-in-process dispatch.** Capacity is a Redis ZSET; the persistent queue is a SQL status column; per-operation coordination (fan-out semaphore, loop `should_stop` handle, fan-out join `results` dict) lives in backend process RAM. Because dispatch is **push and in-process** — the backend coroutine *is* the execution's lifeline (`asyncio.Task` per loop, semaphore-gated coroutine per fan-out subtask, lazy-imported drain spawn) — durability is bolted on with SQL rows that the process must reconcile against on every restart via sweeps. (#428's own dependency note confirms this is causal, not incidental: "much of the current TTL/drain machinery exists to compensate for blocking HTTP dispatch.") Each new trigger type had to re-thread identity through enqueue/drain metadata, re-derive the timeout *in each of its own dispatch layers*, and re-implement restart recovery, multiplying the reconciliation surface linearly with the number of producers. The cross-module import cycle (`chat ↔ backlog`) that forced the fragile string-literal lazy import is a direct symptom of this: the producer and the queue can't cleanly depend on each other because neither is the authority. **The #418 timeout bug is the same pathology in the timeout dimension** — because every producer is its own dispatch path, the per-agent timeout had to be independently honored in five places, and five copies of a `600` default drifted out of sync with the one config column.

### Residual risk
- The backlog drain still resolves `_run_async_task_with_persistence` via a **string-literal lazy import** inside a try/except (`backlog_service.py:255`); the only protection is an AST unit test that checks the *name* exists (`LAZY_IMPORT_TARGETS`) — a signature/semantics change still passes the guard. The `chat ↔ backlog` cycle that forced this is unchanged. **(Verified live in repo.)**
- Three independent concurrency authorities remain live: slot ZSET cap, fan-out `asyncio.Semaphore` (`fan_out_service.py:107`), and loop sequential-by-construction — fan-out can still admit past the semaphore into backlog/reject, and loop iterations contend with all other traffic for the same `max_parallel_tasks` with no fairness.
- **Timeout defaults remain a per-dispatcher convention, not an enforced ceiling.** #423 removed the `600` default from five sites and replaced it with "pass `None` / `?? 7200`"; a future sixth dispatcher (or a re-added Zod default) re-introduces the #418 class. The only guard is `test_inter_agent_timeout_unit.py`, which tests the existing paths, not new ones.
- Loop and fan-out coordination state (`_LoopHandle`, the `results` dict) is **single-process, non-durable**. On a restart mid-fan-out there is no join recovery at all (unlike loops, which at least get marked `interrupted`); partial fan-out results are simply lost.
- B-01 is self-admittedly trivially-green; B-02's "drain is alive" signal is a 60s TTL heartbeat (`canary:drain_tick_at`) — a time guess, not a liveness proof. Detection improved; the split state it watches is contained, not cured.

### Link to consolidation / pull redesign
**#428 (CapacityManager)** is the direct structural response to this family: it dissolved the `SlotService + ExecutionQueue + BacklogService` pyramid into one facade with a single `acquire(...)` admit path (confirmed at `capacity_manager.py:207`, parameterized by `overflow_policy ∈ {reject, queue_in_memory, queue_persistent}`) and one policy axis (`queue_in_memory` for `/chat`, `queue_persistent` for `/task`), moved the drain-on-release callback into the facade, and — per B-01's own docstring — *removed the queue's secondary representation* so depth is now only `status='queued'` rows. That collapsed several seams (the bespoke `main.py` callback registration, the standalone backlog maintenance loop) into one owner, and it's why the dispatch-breaker no-enqueue invariant (#526) could be expressed at a single point. But #428 consolidated the *producers' admission*, not the *authority of the fact*: capacity still lives in Redis, the queue in SQL, per-op coordination in RAM. Notably, **#428 did NOT touch the two seams that produced the two confirmed in-the-wild incidents** — the string-literal lazy import (#496) and the per-dispatcher timeout default (#418) both survive it. **#1081 pull / work-stealing** is the cure this family points to: if agents *pull* from a single durable queue (Postgres, #300) instead of the backend *pushing* into per-agent slots, the slot ZSET, the SQL backlog, the fan-out semaphore, and the loop in-RAM handle all collapse into one queue with one authority — eliminating the reconcile-two-stores class (#260/#316/B-01/B-02), the push-lifeline class (loops/fan-out lost on restart), and the per-producer re-threading that made "more trigger types" multiply the bug surface (including the timeout-default duplication of #418) in the first place. #1084 effect-idempotency is the prerequisite, because a pull/redelivery model trades this family's reconciliation bugs for duplicate-execution (MISSING_IDEMPOTENCY) bugs unless every effect is idempotent first.

### Verifier notes

**Changed:**

1. **#418/#423 primary_class: `TTL_HEURISTIC` → `PARALLEL_CODE_PATHS`** (the one substantive reclassification). The original took Issue #418's *hypothesis* (a single hardcoded 600s ceiling inside `TaskExecService`) at face value. The actual fix commit `eb4dad98` shows the cause was duplicated across **five dispatch sites in three files**: `fan_out.py` router default, two Zod `.default(600)` schemas in `chat.ts`, and three `options?.timeout_seconds || 600` sites in `client.ts`. Per the vocabulary's "prefer the deeper cause" rule, this is the duplicated-path signature, not a heuristic: a real per-agent timeout *signal* existed (`execution_timeout_seconds`); the bug was N drifted copies of a default shadowing it. Demoted TTL_HEURISTIC to secondary (the operative ceiling was still a fixed time value). The analyst's own `recurred` note already named the PARALLEL_CODE_PATHS character ("a convention replicated across every dispatcher"), so the body and the label were internally inconsistent — now reconciled.

2. **#418/#423 fix_shape: `point-guard` → `point-guard (multi-site)`** — relabeled to reflect that the same "drop the default / pass None" edit was applied at every dispatch site, with no single enforced ceiling. It is closer to "fix-by-convention" than a true single point-guard; flagged as a latent re-introduction surface.

**Confirmed unchanged (spot-checked, held up):**

- `recurred: no` for #418/#423, #740/#902, and #500 — `git log` confirms zero follow-up commits on `fan_out_service.py` (since 2026-04-20), `loop_service.py` (one commit ever), and `backlog_service.py` (last touched by #500). No missed recurrences.
- #260/#316 = SPLIT_STATE_AUTHORITY — correct and the deepest cause; PR #316 confirms the slot-before-claim reconciliation harness and the `chat ↔ backlog` cycle.
- #496/#500 = PARALLEL_CODE_PATHS primary with MISCLASSIFIED_FAILURE secondary — confirmed; the string-literal import, swallowed exception, `sys.modules` stub, AST test, and `backlog_drain_spawn_failed` log token all verified live in the repo.
- Fan-out semaphore-vs-slot dual-authority claim — verified accurate against `fan_out_service.py` (semaphore at `:107`, per-subtask `execute_task` dispatch at `:128`).
- Loop `_LoopHandle` vs SQL split-state and sweep-based restart recovery — verified against `loop_service.py` (`:56`, `:86`, `:151`).
- B-01/B-02 classification (both SPLIT_STATE_AUTHORITY) — verified against `canary/snapshot.py`.
- Design pathology ("no single admission authority" + push-in-process) — confirmed as co-original-sins by #428's explicit dependency on #306 push-completion. Added one sentence tying the #418 timeout bug into the same pathology, and one sentence to the consolidation section noting #428 left both incident-producing seams (#496 import, #418 default) intact — both additive, no claims removed.


---

