# Phase 28: Agent Dashboard

> **Purpose**: Validate the per-agent Dashboard tab — when it appears, what it shows without a `dashboard.yaml`, and widget rendering from a `dashboard.yaml` written through the files API.
> **Duration**: ~15 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: tab gating matches the API flags; a written `dashboard.yaml` renders its widgets; removing the file falls back to the cached dashboard with a visible banner
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

The agent Dashboard tab (tab id `dashboard`, `components/DashboardPanel.vue`) is **hidden**
unless `GET /api/agent-dashboard/{name}/exists` reports `has_dashboard` or
`has_declared_metrics` (`utils/agentTabs.js`). The agent serves `/home/developer/dashboard.yaml`
(top-level `title` + `sections[].widgets[]`); the backend adds a "Platform Metrics" section and
caches the last valid dashboard.

Replaces the January flow that wrote the file through a Terminal tab (there is no Terminal tab)
with a flat `widgets:` list, and used host `docker exec`. The file is now written with
`PUT /api/agents/{name}/files?path=…`.

Because the backend keeps serving the **cached** dashboard after the file is deleted, writing
one to a fixture would leave a permanent Dashboard tab on it. The write steps therefore use ONE
throwaway agent, `sweep-tmp-28`; the fixtures are only read. No messages are sent to any agent.

## Prerequisites

- [ ] Logged in to `http://localhost` as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `$TOKEN` holds an admin Bearer token
- [ ] `test-echo` and `test-counter` are `running`

## Test: Tab gating on the fixtures (read-only)

### Step 1: API flags
**Action**:
```bash
for a in test-counter test-echo; do
  curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agent-dashboard/$a/exists | jq -c --arg a $a '{agent: $a} + .'
done
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agent-dashboard/test-counter \
  | jq -c '{agent_name, has_dashboard, status, settled, stale, error}'
```
**Expected**:
- [ ] Each `exists` line has boolean `has_dashboard` and `has_declared_metrics` — **record all four values**
- [ ] The third line has `agent_name: "test-counter"` and `status: "running"`; if `has_dashboard` is `false` it also has `settled: true` and `error: "No dashboard.yaml found at /home/developer/dashboard.yaml"`

### Step 2: Tab presence matches the flags
**Action**:
- Navigate to `http://localhost/agents/test-counter`, wait for the tab strip to settle (up to 10 s), and read the tab labels.
- Navigate to `http://localhost/agents/test-echo` and do the same.
**Expected**:
- [ ] For each agent a "Dashboard" tab (between "Chat" and "Reports") is present **if and only if** one of its two Step 1 flags is `true`
- [ ] There is no "Terminal" tab on either agent

### Step 3: Dashboard tab without a dashboard.yaml
**Action**:
- If `test-echo` has `has_declared_metrics: true` and `has_dashboard: false`: open `http://localhost/agents/test-echo?tab=dashboard`.
- Otherwise record `SKIPPED (fixture flags differ)` with the flags, and continue.
**Expected**:
- [ ] The "Dashboard" tab is selected
- [ ] A declared-metrics block leads the tab (tiles, or its own empty message — record which)
- [ ] Below it: "No Dashboard Defined" with "The declared metrics above are this agent's numbers. A dashboard.yaml adds tables, lists and links around them." and a hint mentioning `~/dashboard.yaml`
- [ ] No "Dashboard Error" card and no endless loading state

### Step 4: Deep link to a hidden tab falls back
**Action**:
- Pick a fixture whose two Step 1 flags are both `false` (normally `test-counter`) and navigate to `http://localhost/agents/<that agent>?tab=dashboard`. If neither fixture qualifies, record `SKIPPED (no fixture without the tab)`.
**Expected**:
- [ ] No "Dashboard" tab appears; record which tab ends up selected (source drops a deep link to a tab the viewer cannot see)
- [ ] No blank content area and no console errors

## Setup

### Step 5: Create the throwaway agent
**Action**:
- Create it from the counter fixture template; if the plain name answers `409` (still reserved by an earlier run's soft delete), use a timestamped name:
```bash
TMP=sweep-tmp-28
mk() { curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8000/api/agents \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\": \"$TMP\", \"template\": \"local:test-counter\"}"; }
code=$(mk); if [ "$code" = "409" ]; then TMP=sweep-tmp-28-$(date +%s); code=$(mk); fi
echo "$TMP $code"
```
- Poll every 5 s (at most 60 s) until `curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/$TMP | jq -r .status` prints `running`.
**Expected**:
- [ ] Final create status `200`; **record `$TMP`**
- [ ] Status `running` within 60 s
- [ ] `GET /api/agent-dashboard/$TMP/exists` returns both flags `false`

## Test: Write and render a dashboard

### Step 6: Write dashboard.yaml through the files API
**Action**:
```bash
BODY=$(jq -Rs '{content: .}' <<'YAML'
title: "Sweep 28 Dashboard"
description: "Written by the UI sweep"
refresh: 30
sections:
  - title: "Sweep Section"
    layout: grid
    columns: 2
    widgets:
      - type: metric
        label: "Sweep Metric"
        value: 42
      - type: status
        label: "Sweep Status"
        value: "OK"
        color: green
      - type: text
        content: "sweep-28 text widget"
      - type: list
        title: "Sweep List"
        items: ["alpha", "beta"]
YAML
)
curl -s -o /dev/null -w '%{http_code}\n' -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "$BODY" "http://localhost:8000/api/agents/$TMP/files?path=/home/developer/dashboard.yaml"
```
- If it returns `5xx` because the agent's internal server is still starting, retry every 5 s for at most 60 s.
**Expected**:
- [ ] `200`
**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agent-dashboard/$TMP \
  | jq -c '{has_dashboard, title: .config.title, sections: [.config.sections[].title], error}'
# has_dashboard true, title "Sweep 28 Dashboard", sections include "Sweep Section" and "Platform Metrics"
```

### Step 7: The tab appears and the widgets render
**Action**:
- Navigate to `http://localhost/agents/<$TMP>` (a fresh page load), wait up to 10 s for the tab strip, then click "Dashboard".
**Expected**:
- [ ] A "Dashboard" tab is now present
- [ ] Heading "Sweep 28 Dashboard" with "Written by the UI sweep" and an "Updated …" relative time
- [ ] Section "Sweep Section" containing: `42` with the label "Sweep Metric"; "Sweep Status" with the value "OK"; the text "sweep-28 text widget"; "Sweep List" with items "alpha" and "beta"
- [ ] A second section "Platform Metrics" with an "Auto" badge and at least a "Tasks (24h)" tile
- [ ] No "widgets skipped due to validation errors" banner and no "Showing cached dashboard" banner

### Step 8: Manual refresh keeps the content in place
**Action**:
- Click the round-arrow button with tooltip "Refresh dashboard".
**Expected**:
- [ ] The same title, section and four widgets are shown afterwards; the layout does not collapse to an empty state in between
- [ ] A `GET /api/agent-dashboard/<$TMP>` request with status `200` in the network log

### Step 9: Narrow width and dark theme
**Action**:
- Resize the viewport to 390 px wide.
- Click the theme button in the top nav (tooltip ends "(click to switch)") until the page is dark; record the starting tooltip first.
**Expected**:
- [ ] At 390 px all four widgets and the "Platform Metrics" tiles remain visible with no horizontal page scrollbar
- [ ] In dark theme widget cards have dark backgrounds with readable values; the "Sweep Status" value keeps a visible colour

### Step 10: Removing the file falls back to the cache
**Action**:
- Restore the viewport, then delete the file:
```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/agents/$TMP/files?path=/home/developer/dashboard.yaml"
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agent-dashboard/$TMP \
  | jq -c '{has_dashboard, stale, stale_reason, title: .config.title}'
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agent-dashboard/$TMP/exists
```
- Reload `http://localhost/agents/<$TMP>?tab=dashboard`.
**Expected**:
- [ ] Delete returns `200`
- [ ] The dashboard call reports `stale: true`, `stale_reason: "No dashboard.yaml found at /home/developer/dashboard.yaml"` and still the title "Sweep 28 Dashboard"
- [ ] `exists` still reports `has_dashboard: true` (it reads the cache)
- [ ] In the UI the Dashboard tab is still present and shows a banner "Showing cached dashboard" with that reason, above the cached widgets
- [ ] Record this as observed behaviour: deleting `dashboard.yaml` does not remove the tab

## Cleanup / Restore

### Step 11: Delete the throwaway and confirm the fixtures are unchanged
**Action**:
- Click the theme button until its tooltip matches the one recorded in Step 9.
- Delete the throwaway (only this name, never a fixture) and re-read the fixture flags:
```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/$TMP
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/$TMP   # 404
for a in test-counter test-echo; do
  curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agent-dashboard/$a/exists
done
```
**Expected**:
- [ ] Delete returns `{"message": "Agent <$TMP> deleted"}` and the follow-up `GET` is `404`
- [ ] Both fixtures report the same four flag values recorded in Step 1
- [ ] **Record the throwaway name in the report**: its workspace volume `agent-<$TMP>-workspace` persists after the delete and must be removed by whoever maintains the host

## Manual-only (not run unattended)

- **Invalid YAML / invalid widgets**: write a `dashboard.yaml` with no `title` (expect "Dashboard
  Error" on an agent with no cache, or "Showing cached dashboard" on one with a cache), or a
  widget missing a required field (expect "N widget(s) skipped due to validation errors").
  Left out of the unattended run to keep the throwaway's cache state deterministic.
- **Remaining widget types** (`progress`, `markdown`, `table`, `link`, `image`, `divider`,
  `spacer`) and bound `metric:` widgets.
- **Auto-refresh on the `refresh` interval** (30 s default, minimum 5) and the "Update
  Dashboard" button, which appears only when the agent has an `update-dashboard` playbook.
- **"Agent Not Running"** ("Start the agent to view its dashboard.") requires stopping an agent
  that has a Dashboard tab.

## Critical Validations

1. The Dashboard tab is present exactly when `…/exists` reports `has_dashboard` or `has_declared_metrics`.
2. A `dashboard.yaml` written via `PUT /api/agents/{name}/files` makes the tab appear and all four widgets render with their exact labels and values.
3. A "Platform Metrics" section is appended by the platform.
4. After the file is deleted the UI says "Showing cached dashboard" rather than silently showing stale data as current.
5. The fixtures' dashboard flags are identical before and after the phase.

## Success Criteria

- [ ] All five critical validations hold
- [ ] Only `sweep-tmp-28` (or its timestamped fallback) was created, written to and deleted
- [ ] The throwaway name is recorded in the report for volume removal

## Troubleshooting

- **PUT returns `400` "Agent must be running to update files"**: the throwaway has not finished
  starting; keep polling within the 60 s budget.
- **PUT returns `403` "Cannot edit protected path"**: the path is wrong — it must be exactly
  `/home/developer/dashboard.yaml`.
- **Tab does not appear after Step 6**: the page was not reloaded (the probe runs on page
  load), or Step 6's Verify shows `has_dashboard: false` with an `error` — record the error.
- **Create returns `409` twice**: record the response `detail` and skip Steps 5–11 as
  `BLOCKED (could not create throwaway)`; Steps 1–4 still count.
