# Phase 22: Telemetry & Logs

> **Purpose**: Verify host and per-agent telemetry in the UI (Dashboard meters, agent header stats, Overview trends) and container logs through the API.
> **Duration**: ~15 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: Live CPU/memory figures, the Overview panel and the stats/logs endpoints all answer for a running fixture agent
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

Telemetry is spread over three surfaces:

- **Dashboard (`/`)** — an inline host strip (`components/HostTelemetry.vue`) with CPU, Mem and Disk meters, polled every 5 s from `GET /api/telemetry/host`.
- **Agent header (`/agents/:name`)** — live CPU / MEM / uptime with sparklines (`components/AgentHeader.vue`), polled every 10 s from `GET /api/agents/{name}/stats`, plus a token-usage row.
- **Overview tab** (the default agent tab, `components/OverviewPanel.vue`) — Activity trends, Health & reliability, Recent activity, Footprint.

Replaces the January flow that opened a "Logs" tab and a Terminal: the agent page has no Logs tab today (`utils/agentTabs.js`), and `?tab=logs` resolves to nothing, so container logs are checked through `GET /api/agents/{name}/logs` only. The phase is observe-only and sends no messages.

## Prerequisites

- [ ] Logged in at http://localhost as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `test-echo` is running (it has at least one seeded execution)
- [ ] Browser window at 1280 px wide or more
- [ ] For API checks, a token in `$TOKEN`:
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d "username=admin&password=$ADMIN_PASSWORD" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

---

## Test: Host telemetry on the Dashboard

### Step 1: Host meters render
**Action**:
- Open http://localhost/
- Find the stats strip at the top of the Dashboard (the line containing "working now" and "messages (…h)")

**Expected**:
- [ ] Three meters follow on the same strip, labelled `CPU`, `Mem` and `Disk`
- [ ] CPU shows a whole-number percentage (e.g. `12%`) with a small sparkline
- [ ] Mem shows used/total in the form `7.9/16G` with a small sparkline
- [ ] Disk shows a small fill bar and a whole-number percentage
- [ ] Hovering each meter shows a tooltip starting `CPU `, `Memory ` and `Disk ` respectively

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/telemetry/host
# Expect "cpu": {"percent": …}, "memory": {"percent","used_gb","total_gb"}, "disk": {"percent","used_gb","total_gb"}
```

### Step 2: Host meters refresh
**Action**:
- Note the CPU percentage, wait 15 seconds without reloading

**Expected**:
- [ ] The CPU sparkline has gained points (the strip polls every 5 s); the percentage may or may not have changed
- [ ] No console errors from `/api/telemetry/host`

### Step 3: Host strip at 390 px
**Action**:
- Resize the browser to 390 px wide and look at the same strip
- Resize back to 1280 px

**Expected**:
- [ ] At 390 px the page body does not scroll horizontally
- [ ] Record which of the CPU / Mem / Disk meters are still shown (the strip hides them progressively as its container narrows; all three hidden is acceptable)
- [ ] Back at 1280 px all three meters return

---

## Test: Agent header stats

### Step 4: Live CPU / MEM / uptime
**Action**:
- Open http://localhost/agents/test-echo
- Look at the second row of the header card, right-hand side

**Expected**:
- [ ] The status badge under the agent name reads `running`
- [ ] After at most a brief `Loading...`, the row shows `CPU`, a sparkline, a percentage and `/ N cores`
- [ ] It shows `MEM`, a sparkline, a used-memory figure and `/ NG` (the configured ceiling, upper-case)
- [ ] An uptime value sits to the right of MEM
- [ ] A small icon button with the tooltip "Configure resources (Memory/CPU)" ends the row (do not click it in this phase)

### Step 5: Stats endpoint shape
**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/stats
```
**Expected**:
- [ ] HTTP 200 with the keys `cpu_percent`, `memory_used_bytes`, `memory_limit_bytes`, `memory_percent`, `network_rx_bytes`, `network_tx_bytes`, `uptime_seconds`, `status`
- [ ] `status` is `running`
- [ ] `cpu_percent` in the response is consistent with the header figure (allow drift; it is a live sample)

### Step 6: Header stats refresh
**Action**:
- Stay on the page for 25 seconds, tab in the foreground

**Expected**:
- [ ] The uptime value has advanced
- [ ] The CPU and MEM sparklines have gained points (polling is every 10 s)
- [ ] The header never drops back to `Loading...` between polls

### Step 7: Token usage row
**Action**:
- Look directly under the stats row of the header card

**Expected**:
- [ ] A row shows `7d` with a sparkline, `Today` with a figure, and on the right `Lifetime`, a figure and `N runs` with N ≥ 1
- [ ] If the figures are prefixed with `≈` and a `≈ API-equiv` chip is shown, record it (subscription-funded agent); a `—` in place of a cost is also valid — record which appears

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/token-stats
# lifetime_executions must match the "N runs" figure
```

---

## Test: Overview tab

### Step 8: Overview is the default tab
**Action**:
- Reload http://localhost/agents/test-echo (no `?tab=`)

**Expected**:
- [ ] The `Overview` tab is selected
- [ ] The first card shows the agent's name, a `Full details →` link and a `New task` button
- [ ] Section headings `Activity trends`, `Health & reliability`, `Recent activity` and `Footprint` are present (rendered upper-case)

### Step 9: Activity trends and window switch
**Action**:
- In `Activity trends`, note the selected window button (`7d` is the default)
- Click `30d`, then click `7d` again

**Expected**:
- [ ] Next to the window buttons a live counter reads `N running · N queued`
- [ ] With runs in the window: charts titled `Executions by type` (with `N total`), `Execution completion rate` (a percentage) and `Duration` (`avg … · p95 …`) are shown; `Context consumption` may also appear
- [ ] With no runs in the window the card instead reads `No runs in the last 7d` — record which of the two states appears for each window
- [ ] Switching windows changes the selected button and re-renders without a full-page reload

### Step 10: Health & reliability
**Action**:
- Read the `Health & reliability` card (do not enable anything)

**Expected**:
- [ ] The first chip reads one of `Healthy`, `Degraded`, `Unhealthy`, `Unknown` — record it
- [ ] Either two charts titled `Uptime (last 7 days)` and `Latency (last 7 days)` are shown, or the card reads `No health data yet` with the link `Enable it in Operations → Health →` — record which
- [ ] No chip reads `OOM killed` or `Circuit open — see header` (record it as a finding if one does)

### Step 11: Recent activity and Footprint
**Action**:
- In `Recent activity`, click the first row
- Go back to the `Overview` tab and read the `Footprint` card

**Expected**:
- [ ] `Recent activity` lists at least one row (message text, a trigger chip, a timestamp) and a `View all →` link
- [ ] Clicking a row switches to the `Tasks` tab and adds `execution=<id>` to the URL
- [ ] `Footprint` shows chips `N schedules`, `N skills`, `N shares` and `Sync: …`

### Step 12: Agent page at 390 px and in both themes
**Action**:
- On http://localhost/agents/test-echo, resize to 390 px wide, scroll the Overview
- Resize back to 1280 px
- Note the title of the theme button in the top bar (one of `Light mode (click to switch)`, `Dark mode (click to switch)`, `System theme (click to switch)`), then click it until the page is dark, then until it is light

**Expected**:
- [ ] At 390 px the header stats wrap onto extra lines and the page body does not scroll horizontally
- [ ] In dark and in light theme the CPU/MEM sparklines, the stat figures and the Overview chart axes are all legible (no dark-on-dark or white-on-white text)

---

## Test: Container logs (API only)

### Step 13: `?tab=logs` does not open a tab
**Action**:
- Open http://localhost/agents/test-echo?tab=logs

**Expected**:
- [ ] The page lands on `Overview`
- [ ] The tab strip (including its overflow menu) has no `Logs` entry

### Step 14: Logs endpoint
**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/agents/test-echo/logs?tail=50" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['logs'].splitlines()))"
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/agents/test-echo/logs?tail=5" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['logs'].splitlines()))"
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:8000/api/agents/test-echo/logs"
```
**Expected**:
- [ ] The first call returns a JSON object with a single string field `logs`, non-empty, at most 50 lines
- [ ] The second returns at most 5 lines
- [ ] The third (no token) returns `401`
- [ ] The log text contains no API key, token or password in clear (skim it; report any hit as Critical)

---

## Cleanup / Restore

- Click the theme button until its title matches the one noted in Step 12.
- Restore the browser width to 1280 px.
- Nothing else was changed (no messages sent, no settings touched).

## Manual-only (not run unattended)

- Stats for a stopped agent: `GET /api/agents/{name}/stats` answers `400 "Agent is not running"` and the header shows the agent's age plus `N CPU` / `NG` instead of live figures. Needs a fixture stopped and restarted.
- Colour thresholds on the header figures (amber above 50 %, red above 80 %) need a load generator.
- `Enable it in Operations → Health →` changes instance-wide monitoring.

## Critical Validations

1. The Dashboard strip shows CPU, Mem and Disk with real, non-zero totals.
2. The agent header shows live CPU / MEM / uptime for a running agent and keeps updating.
3. `GET /api/agents/test-echo/stats` returns 200 with the eight documented keys.
4. `GET /api/agents/test-echo/logs` returns `{"logs": "…"}`, honours `tail`, and refuses an unauthenticated call.
5. The Overview tab renders all four sections without a console error.

## Success Criteria

- [ ] Steps 1–14 pass, with observations recorded where a step asks for one
- [ ] No horizontal page scroll at 390 px on `/` or `/agents/test-echo`
- [ ] No console errors from `/api/telemetry/*`, `/api/agents/test-echo/stats` or `/api/monitoring/*`
- [ ] Theme restored to its starting value

## Troubleshooting

- **Header shows the agent's age and `N CPU` instead of live stats** — the agent is not running, or `/stats` returned an error (a 400 is swallowed silently by the UI). Check the status badge.
- **Host meters missing at full width** — `GET /api/telemetry/host` failed; the strip renders nothing on error. Check the network tab.
- **No token-usage row** — it only renders when `lifetime_executions > 0`; the seeded execution should satisfy this.
- **`No health data yet`** — fleet-health monitoring is off on this instance; that is a valid state, not a failure.
- **Sparklines flat after the tab was in the background** — header polling is skipped while the document is hidden.
