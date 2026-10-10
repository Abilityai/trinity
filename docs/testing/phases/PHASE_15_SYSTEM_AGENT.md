# Phase 15: System Agent (observe-only)

> **Purpose**: Verify how the system agent `trinity-system` is presented — its redirect, reduced tab set, protected header — and the read-only fleet operations endpoints.
> **Duration**: ~10 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: The system agent is reachable, clearly marked, offers no delete or owner-management surfaces, and the read-only ops API answers for an admin
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

`trinity-system` is the platform's own agent. It has no page of its own: `/system-agent` redirects to the ordinary agent detail page at `/agents/trinity-system`, and there is no "System" item in the top nav (Dashboard, Library, Operations, Settings, Workspace).

On that page the tab builder (`utils/agentTabs.js`) drops the owner-management tabs for a system agent, and the header hides the controls that would rename, re-avatar or delete it.

Fleet-wide operations live in the API under `/api/ops/*` and `/api/system-agent/*`. Only the `GET` routes are exercised here.

Replaces the January flow that expected a System nav link, a dedicated System Agent page with a terminal and quick-action buttons, and that ran emergency stop and fleet restart.

**This phase changes nothing.** It sends no messages, clicks no toggle on `trinity-system`, and calls no `POST`/`PUT`/`DELETE` endpoint.

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `$TOKEN` holds an admin Bearer token
- [ ] Viewport 1280 × 800

---

## Test: Routing and identity

### Step 1: Legacy route redirects
**Action**:
- Navigate to `http://localhost/system-agent`

**Expected**:
- [ ] The address bar ends at `http://localhost/agents/trinity-system`
- [ ] The agent detail page renders with the `Overview` tab active
- [ ] The top nav has no `System` item

### Step 2: Header of the system agent
**Action**:
- Inspect the page header. Do not click anything.

**Expected — present**:
- [ ] The agent name as the page heading
- [ ] A purple `SYSTEM` badge with title `System Agent - Platform Orchestrator with full access`
- [ ] A `Workspace` button (title `Open this agent in the Workspace`) and a `Talk` button
- [ ] The running-state toggle, reading `Running` or `Stopped` — record which. **Do not click it.**
- [ ] A `Tags:` row

**Expected — absent**:
- [ ] No button titled `Delete agent`
- [ ] No pencil button titled `Rename label` next to the name
- [ ] No `AUTO` / `Manual` autonomy toggle
- [ ] No `Read-Only` / `Editable` toggle

### Step 3: Tab set of the system agent
**Action**:
- Read every tab label in the tab strip, including those inside the `More` overflow menu

**Expected — present, in this order**:
- [ ] `Overview`, `Tasks`, `Chat`, `Reports`, `Canvas`, `Schedules`, `Loops`, `Playbooks`, `Credentials`, `Payments`, `Files`, `Info`
- [ ] `Dashboard`, `Brain` and `Git` may additionally appear depending on the agent and instance — record which do

**Expected — absent**:
- [ ] `Access`, `Sharing`, `Permissions`, `Folders`, `Skills`, `Settings`
- [ ] `A2A` (if this tab is not present on any agent of this instance, record `SKIPPED (not present)` for it)

### Step 4: Same checks on a normal agent, for contrast
**Action**:
- Navigate to `http://localhost/agents/test-echo` and read the header and the full tab list (including `More`)

**Expected**:
- [ ] Tabs `Access`, `Sharing`, `Permissions`, `Folders`, `Skills`, `Settings` ARE present
- [ ] A button titled `Delete agent` IS present (do not click it)
- [ ] The `Rename label` pencil and the autonomy and read-only toggles ARE present
- [ ] No `SYSTEM` badge

### Step 5: Deep links to hidden tabs fall back
**Action**:
- Navigate to `http://localhost/agents/trinity-system?tab=permissions`
- Then navigate to `http://localhost/agents/trinity-system?tab=settings`

**Expected**:
- [ ] Each time the page settles on the `Overview` tab; no permissions list and no settings sections are rendered
- [ ] No error banner and no blank page

### Step 6: Deep link to an allowed tab works
**Action**:
- Navigate to `http://localhost/agents/trinity-system?tab=files`

**Expected**:
- [ ] The `Files` tab is active
- [ ] If the agent is running: a file tree with a `Search files...` box. If it is stopped: `Agent must be running to browse files`. Either is a pass — record which.
- [ ] Do not create, edit or delete anything here

---

## Test: Markers on the Dashboard

### Step 7: System badge in all three modes
**Action**:
- Navigate to `http://localhost/`. Click `Timeline`, then `Grid`, then `List` in the mode switcher; afterwards click the mode that was active when you arrived.

**Expected**:
- [ ] Timeline: the `trinity-system` row carries a `SYS` badge and has no autonomy toggle
- [ ] Grid: the `trinity-system` tile carries a `SYSTEM` badge (title `System Agent - Platform Orchestrator`)
- [ ] List: the `trinity-system` row carries `SYSTEM`
- [ ] Clicking the tile's `Details` button (Grid) lands on `/agents/trinity-system`; go back

---

## Test: Read-only API

### Step 8: System agent status
**Action**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/system-agent/status | python3 -m json.tool
```

**Expected**:
- [ ] HTTP 200 with `"exists": true`, `"name": "trinity-system"`, a `status` string and a `container_id`
- [ ] `"is_system": true`
- [ ] If `status` is `running`: `base_image_state` is one of `current`, `stale`, `unknown`, and either `health` or `health_error` is present
- [ ] `status` agrees with the toggle label recorded in Step 2

### Step 9: Fleet status
**Action**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/ops/fleet/status \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['summary']); [print(a['name'], a['status'], a['is_system']) for a in d['agents']]"
```

**Expected**:
- [ ] `summary` has `total`, `running`, `stopped`, `high_context`
- [ ] `agents` includes `test-echo`, `test-counter`, `test-delegator` with `is_system` false, and `trinity-system` with `is_system` true
- [ ] Each agent entry has `name`, `status`, `is_system`, `created_at`, `context`, `last_activity`

### Step 10: Fleet health
**Action**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/ops/fleet/health | python3 -m json.tool
```

**Expected**:
- [ ] `overall` is one of `healthy`, `degraded`, `critical`
- [ ] Keys `critical_issues`, `warnings`, `healthy_count`, `healthy_agents` are present
- [ ] Record `overall` and any issue entries (each has `agent`, `issue`, `recommendation`); a non-`healthy` value is a finding to report, not a phase failure

### Step 11: Schedules, alerts, costs, auth report
**Action**:
```bash
for p in schedules alerts costs auth-report; do
  echo "== $p"; curl -s -w "\n%{http_code}\n" -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/ops/$p" | tail -c 600
done
```

**Expected**:
- [ ] `schedules`: 200; `summary` has `total`, `enabled`, `disabled`, `agents_with_schedules`; `schedules` is a list
- [ ] `alerts`: 200; `alerts` is `[]` and `message` is `Alerts feature coming soon. Check fleet health for current issues.`
- [ ] `costs`: 200 with an `enabled` boolean — record its value and the other top-level keys
- [ ] `auth-report`: 200; `summary` has `total_agents`, `using_subscription`, `using_api_key`, `not_configured`, `subscription_count`

### Step 12: The ops API is not public
**Action**:
```bash
for u in system-agent/status ops/fleet/status ops/fleet/health; do
  curl -s -o /dev/null -w "$u %{http_code}\n" "http://localhost:8000/api/$u"
done
```

**Expected**:
- [ ] All three return `401` without a token

---

## Test: Narrow width and themes

### Step 13: Header at 390 px, both themes
**Action**:
- Open `http://localhost/agents/trinity-system`. Note the theme button's title in the top bar. Resize to 390 × 844.
- Click the theme button until dark is active, then until light is active, then until the original title is back. Restore 1280 × 800.

**Expected**:
- [ ] The header wraps: name, `SYSTEM` badge, `Workspace`, `Talk` and the running toggle are all visible without horizontal page scroll (record `scrollWidth` vs `innerWidth` if it does scroll)
- [ ] The tab strip collapses the tabs that do not fit into `More`
- [ ] The `SYSTEM` badge is legible in both themes
- [ ] The theme button's title equals the one noted at the start

---

## Cleanup / Restore

Nothing to restore. The phase read state only; the Dashboard mode and theme were returned to their starting values inside Steps 7 and 13.

Confirm nothing drifted:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/system-agent/status \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])"
# Must equal the status recorded in Step 8
```

## Manual-only (not run unattended)

These act on the whole fleet or on the system agent itself. Never run them from an unattended sweep.

| Action | Route | Why it is excluded |
|---|---|---|
| Emergency stop | `POST /api/ops/emergency-stop` | Stops agents and halts schedules fleet-wide |
| Restart fleet | `POST /api/ops/fleet/restart` | Restarts every running agent |
| Stop fleet | `POST /api/ops/fleet/stop` | Stops every agent |
| Pause / resume schedules | `POST /api/ops/schedules/pause`, `/resume` | Rewrites every schedule's enabled flag |
| Acknowledge alert | `POST /api/ops/alerts/{alert_id}/acknowledge` | Write call |
| Restart system agent | `POST /api/system-agent/restart` | Bounces the platform's own agent |
| Reinitialize system agent | `POST /api/system-agent/reinitialize` | Resets it to a clean state |
| Stop it from the header | the `Running` toggle on `/agents/trinity-system` | Same effect as a stop |
| Delete protection | `DELETE /api/agents/trinity-system` → expect 403 `System agents cannot be deleted. Use re-initialization to reset to clean state.` | A regression here would delete the system agent |
| Non-admin denial | any `/api/ops/*` route as a non-admin → 403 | Needs a second account |
| Auto-deploy on boot | restart the backend, confirm the agent comes back | Needs host access |

## Critical Validations

1. `/system-agent` redirects to `/agents/trinity-system`.
2. The system agent shows none of `Access`, `Sharing`, `Permissions`, `Folders`, `Skills`, `Settings`, and no `Delete agent` button — while a normal agent shows all of them.
3. A `?tab=` deep link to a hidden tab falls back to Overview instead of rendering it.
4. `GET /api/system-agent/status` reports `exists: true` and `is_system: true`.
5. The read-only ops routes return 200 for admin and 401 without a token.

## Success Criteria

- [ ] Redirect, header and tab-set assertions hold on `trinity-system` and the contrast holds on `test-echo`
- [ ] System markers visible in Timeline, Grid and List
- [ ] All six `GET` ops routes plus the status route answered as stated
- [ ] No write call was made and no toggle on `trinity-system` was clicked
- [ ] System agent status is unchanged at the end

## Troubleshooting

- **`/agents/trinity-system` shows a not-found panel**: the system agent does not exist or is not visible to this user. Step 8 will say `"exists": false, "status": "not_found"` — report it; do not try to create it.
- **A hidden tab appears**: check first that you are on `trinity-system`, not a fixture; the gate is the agent's `is_system` flag from `GET /api/agents/trinity-system`.
- **Ops routes return 403 with an admin login**: the token belongs to an agent-scoped or other non-interactive key. Use a token from `POST /api/token`.
- **`health_error` in Step 8**: the container is running but its internal server did not answer within 5 s. Record it; it is a finding, not a blocker for the remaining steps.
