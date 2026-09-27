# Exploratory E2E Test Report — Trinity `dev`

**Date:** 2026-08-14
**Branch:** `dev` @ `c1cc22bc`
**Target:** local stack (`http://localhost`), backend `0.8.5`, edition `enterprise`
**Method:** Playwright baseline suite + creative exploratory testing (browser + API)

> Stack note: `trinity-frontend` and `trinity-backend` both **bind-mount local source**
> (`src/frontend → /app`, `src/backend → /app`), so this run exercised the current dev
> working tree, not a stale image.

---

## 1. Executive summary

| | |
|---|---|
| Baseline Playwright suite | **15 failed / 93 passed / 11 skipped** |
| Pages swept | 31 (all render; 1 page-level console error) |
| Product defects found | **9** (1 critical, 3 medium, 5 low) |
| Test-suite-only failures | 7 specs across 3 causes (not product bugs) |
| Journeys verified working | 14 |

**The headline:** a **dev-only regression makes 5 of 6 stopped agents impossible to start**
(HTTP 500). This is the single most fundamental user journey on the platform and it is
broken on `dev`. It is **not on `main`**, so it has not shipped — but it blocks a release cut.

All 15 baseline-suite failures were root-caused. Only **8 of 15** reflect product defects;
the other 7 are stale test constants or missing fixtures.

---

## 2. Critical

### 🔴 C-1 — Stopped agents cannot be started (HTTP 500)

**Impact:** 5 of 6 stopped agents on this instance fail to start. Core journey broken.

```
POST /api/agents/{name}/start
→ 500 {"detail":"Failed to start agent (ValueError — details in backend logs)"}
```

| agent | start result |
|---|---|
| `cornelius`, `acme-scout`, `acme-scribe`, `my-second-brain`, `test-harness-agent` | **500** |
| `acme-sage` | 200 (no config drift → skipped the recreate branch) |

**Root cause** — captured by invoking the service directly in-container:

```
File "/app/services/agent_service/lifecycle.py", line 414, in start_agent_internal
    await recreate_container_with_updated_config(
File "/app/services/agent_service/lifecycle.py", line 842, in recreate_container_with_updated_config
    raise ValueError(
ValueError: recreate_container_with_updated_config would START agent 'test-harness-agent',
whose container is 'exited'. Pass require_running=False to do that deliberately, and
preserve_run_state=True to leave it stopped afterwards (#2092).
```

`start_agent_internal` takes the config-drift recreate branch (base-image drift, MCP-key
drift per #1854, git-env drift …) and calls the helper **without `require_running=False`**.
The #2092 guard defaults to `require_running=True`, so it rejects the one case `start`
exists to serve: starting a stopped agent.

**Introduced by:** `ecf1327f` — *"fix(agents): recreate no longer starts a stopped agent silently (#2092) (#2155)"*, 2026-08-13.
**Ancestry:** on `dev` ✅ · on `origin/main` ❌ → **dev-only, unreleased.**

**Suggested fix:** pass the documented flags at the `start_agent_internal` call site:

```python
await recreate_container_with_updated_config(
    agent_name, container, "system",
    env_overrides=mcp_env_overrides,
    require_running=False,     # starting a stopped agent is this path's job
)
```

**Audit — the other two production call sites also omit the flag:**

| call site | risk |
|---|---|
| `services/agent_service/lifecycle.py:414` | **confirmed broken** (above) |
| `services/agent_service/repo_binding.py:462` | binding a **stopped** agent would now fail at step 6 → `BIND_RECREATE_FAILED` **after** history was pushed and `origin` repointed (partial state). Not reproduced; no upstream running-gate visible at the call site. |
| `services/agent_mcp_key_service.py:795` (`_recreate_with_env`) | docstring says "replace the **running** container"; lower risk, but unguarded at the call site. |

Recommend fixing all three deliberately rather than only the one that reproduced.

**UI behaviour is honest** — the failure surfaces as a toast, not a silent no-op:
> "Failed to start agent (ValueError — details in backend logs)"

…though the wording is developer-facing and gives an operator nothing actionable.

---

## 3. Medium

### 🟠 M-1 — Horizontal page overflow on Settings and Agent Detail (tablet + phone)

The design-system contract requires the page body never scroll horizontally (wide content
must scroll inside its own container). Two pages violate it:

| viewport | page | body overflow | worst offender |
|---|---|---|---|
| 375×812 | `/settings` | **+563 px** | `BUTTON …:: "Activation"` (tab strip) |
| 375×812 | `/agents/{name}` | **+517 px** | `BUTTON.ml-auto :: "More"` |
| 768×1024 | `/settings` | **+178 px** | tab strip |
| 768×1024 | `/agents/{name}` | **+132 px** | `NAV.-mb-px.flex :: "Overview Tasks Chat Repor…"` |
| ≥1024 px | all pages | 0 | — |

Dashboard (all 3 modes), Library, Operations and Workspace are **clean at every size**.
Notably, Agent Detail already uses `OverflowTabs` (#1114), which is meant to collapse
overflow into "More ▾" — at 375 px the overflowing element *is* the "More" button, so the
collapse is not preventing body overflow at narrow widths.

### 🟠 M-2 — Grid toolbar controls overlap the stats cluster at 640 px

Reproduced independently of the spec via `getBoundingClientRect()`:

```
viewport 640 · grid mode
stat "agents"  x 94.5 → 132.9   (y 79)
button "Tags"  x 103.4 → 178.6  (y 77)
→ ~30 px horizontal overlap in the same vertical band
```

`Create Agent` also ends at x=95.4 against the stat starting at x=94.5 — touching.
This is exactly what `dashboard-stats-overflow.spec.js` (#1830) exists to catch; the spec
measured 9 px at its own layout, I measured ~30 px at mine. **Real defect.**
Evidence: `e2e/test-results/dashboard-stats-overflow-*/test-failed-1.png`.

### 🟠 M-3 — Workspace roster and fleet list disagree about which agents exist

`GET /api/enterprise/client-portal/my-agents` returns **4 agents that `GET /api/agents` does not**:
`test-1423-31c51f`, `test-1423-9ed8c0`, `test-clr-agent-6a0cc1`, `test-opq-agent-262e7b`.

They are **not** soft-deleted — `agent_ownership` rows are live (`deleted_at IS NULL`,
`is_system=0`, `is_ephemeral=0`) — they simply have **no container**. The fleet list is
Docker-as-truth (Invariant #11); the Workspace roster is DB-sourced, so orphaned rows leak
through to what is the **customer-facing** surface.

The leaked rows here are pytest residue, so the trigger is environment. The product
behaviour worth fixing is that the roster does not reconcile against container existence —
an end user sees agents that cannot be used. (Canary **L-03** covers orphan rows pointing at
*missing* agents, but not live rows with no container.)

---

## 4. Low

### 🟡 L-1 — `/` type-to-filter hotkey is dead for the first ~50 ms after the fleet paints

Measured by instrumenting `document.addEventListener`:

```
[data-agent="trinity-system"] visible at   t = 261 ms
handleDashboardKeydown registered at       t = 311 ms
```

The fleet is painted by a **child** component; the hotkey is registered in the **parent's**
`onMounted`, which Vue always runs after children mount. A `/` pressed in that window is
silently dropped (the event reaches `document` with `defaultPrevented:false`).

Probe matrix (fresh context each row):

| scenario | pill opens? |
|---|---|
| press `/` immediately | ❌ |
| press `/` twice | ❌ then ✅ |
| wait 6 s, press `/` | ✅ |
| click body, then `/` | ❌ (it's time, not focus) |
| click the toolbar `/` button | ✅ (direct `@click`) |

**User impact is negligible** (~50 ms). **Test impact is total** — Playwright acts the
instant its wait resolves, so all 7 `dashboard-type-filter` specs fail deterministically.
The feature itself works correctly ("1 of 7 match", Esc restores, filters by slug *and*
display label).

### 🟡 L-2 — `404 /api/settings/public_chat_url` on every Settings tab

Every Settings page load emits a console error for a setting that has never been written.
Confirmed: `GET /api/settings/public_chat_url → 404 {"detail":"Setting 'public_chat_url' not found"}`.
Should be seeded, defaulted, or fetched tolerantly rather than logging an error each visit.

### 🟡 L-3 — Agent Detail over-fetches on load (21 redundant requests)

One agent page: **62 API calls, 41 unique, 21 redundant**. Timestamps separate genuine
duplicates from legitimate polling:

| endpoint | calls | start times (ms) | verdict |
|---|---|---|---|
| `/api/agent-dashboard/{a}` | 9 | 220,488,548 · 3506,3568,3574 · 9548,9593,9611 | **3× per poll cycle** |
| `/api/agents/{a}` | 2 | 93, 93 | concurrent duplicate |
| `/api/agent-dashboard/{a}/exists` | 3 | 145,158,504 | duplicate |
| `/api/agents/{a}/info` | 3 | 156,158, 9660 | duplicate |
| `/api/users/me`, `feature-flags`, `playbooks`, `avatar/emotions` | 2 each | ms apart | duplicate |
| `/api/agents/{a}/activity` | 4 | 145,5148,9612,14614 | ✅ 5 s poll, correct |

Relevant because `c1cc22bc` (HEAD) just fixed roster over-fetching on this same page — the
N+1 is gone, but per-agent duplicate fetches remain.

### 🟡 L-4 — Workspace bootstrap is N+1 across the roster

`/workspace` issues one `GET /api/enterprise/client-portal/agents/{name}/sessions` **per agent**
(10 calls for 10 roster entries) on every load. Same class as L-3; scales linearly with fleet size.

### 🟡 L-5 — Accessibility: contrast below WCAG AA on recurring elements

Computed with proper sRGB linearization (an initial approximate scan was recalculated):

| element | ratio | AA small (4.5) | AA large (3.0) |
|---|---|---|---|
| `99+` nav badge — white on orange-500 | **2.80** | ❌ | ❌ |
| secondary text — gray-400 on white (`v0.8.5`, timestamps, `Tags:`, `CPU`) | **2.54** | ❌ | ❌ |
| `AUTO` badge — amber-600 on white | 3.19 | ❌ | ✅ |
| `Running` — green-600 on white | 3.30 | ❌ | ✅ |
| dark mode: `Send` — gray-500 on gray-700 | 2.13 | ❌ | ❌ |

`gray-400` on white is the systemic one — it is the standard muted-text treatment and
appears on every page. (Disabled controls measuring 1.24 are exempt and excluded.)
Both themes otherwise render correctly and `html.dark` toggles as expected.

### 🟡 L-6 — Minor observations

- **MCP Keys page renders all 306 keys, 294 of them inactive** (96%) → 71 KB of DOM text,
  no pagination or active-only filter. Environment-inflated, but the page has no bound.
- **Agent-not-found double-fetches** — a clean navigation to a missing agent issues
  `GET /api/agents/{name}` **twice** (both 404). The page itself is correct and
  enumeration-safe: *"doesn't exist, or you don't have access to it"*.
- **`DELETE` on an already-deleted schedule returns 404**, while `architecture.md` says
  `delete_schedule()` is idempotent on a soft-deleted row. Defensible REST semantics —
  flagging only as a doc/behaviour nuance.
- **`docker logs trinity-backend` is unreadable**: `invalid character '\x00' looking for
  beginning of value`. Vector's aggregate is also large — `/data/logs` is **10.4 GB**.

---

## 5. Baseline suite — all 15 failures root-caused

| # | spec(s) | cause | product bug? |
|---|---|---|---|
| 7 | `dashboard-type-filter` (all) | L-1 hotkey registration race | ✅ yes (minor) |
| 1 | `dashboard-stats-overflow` @640 | M-2 control/stat overlap | ✅ yes |
| 2 | `dashboard-grid-view` drag + tidy | **stale spec constant** | ❌ no |
| 1 | `system-install` preview | **spec strict-mode drift** | ❌ no |
| 4 | `loops-panel` ×2, `workspace-absorbs-session`, `continue-as-chat` | **missing fixture agent** | ❌ no |

**Stale spec constant (2 specs).** `src/stores/fleetGrid.js` uses
`LAYOUT_KEY = 'trinity-grid-layout-v2'`; the spec still asserts `'trinity-grid-layout-v1'`,
so it reads `null`. The bump landed in `309c9ef1` (*grid widget chassis, ent#325 / #2042*)
and the spec was never updated — `git log -S "trinity-grid-layout-v2"` on the spec returns
nothing.

**Spec strict-mode drift (1 spec).** `system-install` expects one match for the previewed
agent name; the preview now renders it in **4 places** (2 table cells + 2 spans) →
strict-mode violation. Needs `.first()` or tighter scoping.

**Missing fixture agent (4 specs).** All default to `LOOPS_TEST_AGENT` /
`SESSION_TEST_AGENT` = **`testfix`**, which does not exist here; `continue-as-chat` fails
earlier with `task trigger failed: 404`. Both affordances these specs assert on **do exist**
and were verified manually — the `Loops` tab renders, and
`Continue in Workspace → /workspace?agent={name}` is present on the Chat tab.

> **Recommendation:** these 4 specs silently degrade into false failures on any instance
> without a `testfix` agent. Either seed the fixture in `auth.setup.js` or `test.skip()`
> when the agent is absent, so a missing fixture reads as "skipped", not "broken".

---

## 6. Journeys verified working

| journey | result |
|---|---|
| Admin login (password) → Dashboard | ✅ |
| Legacy route redirects — `/monitoring`, `/executions`, `/operating-room`, `/events`, `/templates`, `/agents` | ✅ 10/10 exactly as documented |
| Unknown route → Dashboard; unknown agent → enumeration-safe 404 page | ✅ |
| Dashboard Timeline / Grid / List modes | ✅ |
| `?view=list` as non-persisting intent (localStorage stays `grid`, ent#260) | ✅ |
| Type-to-filter — matches slug *and* display label, "1 of 7 match", Esc restores | ✅ (see L-1) |
| 31-page sweep — Library/Operations/Settings/Workspace/Enterprise/all agent tabs | ✅ render, 0 page errors |
| Schedule create — `0 9 * * MON` → `next_run_at 2026-08-17T09:00:00Z` | ✅ |
| Schedule enable / disable / delete / webhook mint + revoke | ✅ |
| Validation — bad cron → precise 400; timeout > agent cap → `schedule_timeout_exceeds_agent_cap` with remedy | ✅ excellent |
| Webhook trigger 202 · **rate limit exactly 10/60 s → 429** · bad token → 404 · revoked → 404 | ✅ |
| Capacity control — burst of 10 → 3 dispatched, 6 rejected `Agent at capacity (3/3 parallel tasks running)` | ✅ no-enqueue invariant holds |
| Execution recording — status, `triggered_by`, `duration_ms`, `error_summary` all populated | ✅ |
| Create Agent modal — slug + display name (ent#1640) + template picker | ✅ |
| Dark ⇄ light theme toggle, persisted | ✅ |

---

## 7. Corrections to in-flight hypotheses

Recorded so they are not re-investigated:

- **`/workspace` does *not* log you out.** An early observation that visiting Workspace
  cleared the JWT was an **expired token in a stale browser profile**, not a product bug.
  With a fresh session, `/workspace` loads normally, token intact, all requests 200.
- **The type-to-filter feature is not broken.** It works; only the *hotkey timing* is racy.
- **`error` is not missing from failed executions.** The field is `error_summary`, and it is
  correctly populated.
- **Login does not hardcode a default password.** `adminIdentifier = ref('admin')` prefills
  only the username (username is fixed as `admin` by design); the password field was
  browser autofill from a saved credential.

---

## 8. Environment constraints (limited coverage)

- **Anthropic credit balance exhausted** — no real LLM turn could complete:
  > `Credit balance is too low. To resolve: (1) add credits … or (2) assign a subscription token …`

  This blocked live chat, loop execution, and Workspace/session round-trips. Everything
  *around* the LLM was still exercised (dispatch, capacity, execution rows, failure
  attribution). The error message itself is exemplary — specific and actionable.
- Only `trinity-system` was running at start; `acme-sage` was the only agent that could be
  started (see C-1).
- `agent_ownership` carries substantial pytest residue, which produced M-3.

---

## 9. State changes made by this run

| change | note |
|---|---|
| `acme-sage` **started** and left running | Deliberately not stopped — C-1 means it might not restart. |
| 9 failed executions on `acme-sage` | From webhook trigger + rate-limit tests. |
| 1 failed chat execution on `acme-sage` | Credit-balance failure. |
| Test schedule + webhook | **Created and fully cleaned up** (webhook revoked 204, schedule deleted 204, verified 404). |

No repository files were modified; all probe scripts were removed (`git status` clean apart
from pre-existing `docs/user-docs` changes).

---

## 10. Recommended next actions

1. **Fix C-1 before any release cut** — pass `require_running=False` at
   `lifecycle.py:414`, and decide deliberately for `repo_binding.py:462` and
   `agent_mcp_key_service.py:795`. Add a regression test that starts a **stopped agent with
   config drift** — the existing suite did not catch a total break of the start path.
2. **Fix M-1/M-2** — the tab strips and the grid toolbar at ≤768 px.
3. **Refresh the 4 stale/fixture specs** so the suite goes green and stays honest
   (`v1`→`v2` key, `.first()` on system-install, fixture guard for the `testfix` four).
4. **Consider L-1's root pattern** — registering document hotkeys in a parent `onMounted`
   while children paint first is reusable footgun; a readiness signal the tests can wait on
   would fix both product and spec.
5. **Track L-3/L-4** as follow-ups to `c1cc22bc`'s over-fetch work.
