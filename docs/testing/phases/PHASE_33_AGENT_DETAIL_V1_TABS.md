# Phase 33: Agent detail — Overview, Reports, Loops, Playbooks, Schedules, Skills, Payments, Info

> **Purpose**: Cover the agent detail tabs no other phase touches, including one full create → toggle → delete of a schedule
> **Duration**: ~25 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: Every tab on `test-echo` loads its real content or its honest empty state, tab deep links work, and the one schedule created here is gone again
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

`/agents/:name` (`views/AgentDetail.vue`) lands on the **Overview** tab. The tab list is built
by `utils/agentTabs.js`: Overview, Tasks, Chat, [Dashboard], [Brain], Reports, Canvas,
Schedules, Loops, Playbooks, Credentials, Payments, [Access, Sharing, Permissions], [A2A],
[Git], Files, [Folders, Skills, Settings], Info — bracketed tabs are conditional. Tabs that do
not fit the width fold into a "More" menu. A tab can be opened directly with `?tab=<id>`
(the Payments tab's id is `nevermined`).

Replaces the retired January scheduling phase (Phase 7), which drove a `test-scheduler` agent
that does not exist by typing into a Terminal tab that is no longer shown. Schedules are
exercised here through the real Schedules tab.

This phase sends **no** message to any agent: it never clicks "Run now", "Start Loop" or a
playbook's "Run".

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`; window about 1280 px wide
- [ ] `test-echo` is running
- [ ] A token for the API checks (every `curl` below sends `-H "Authorization: Bearer $TOKEN"`, written as `$AUTH`):

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d "username=admin&password=$ADMIN_PASSWORD" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
AUTH="Authorization: Bearer $TOKEN"; API=http://localhost:8000
```

## Setup

Record the schedules `test-echo` already has, so Cleanup can prove it left the same set:

```bash
curl -s -H "$AUTH" $API/api/agents/test-echo/schedules \
  | python3 -c "import sys,json; [print(s['id'], s['name'], s['enabled'], s['cron_expression']) for s in json.load(sys.stdin)]"
```

If a schedule named `sweep-tmp-33` is already listed (a previous run died), delete it first
with the command in Cleanup.

## Test: Overview and navigation

### Step 1: Overview is the landing tab
**Action**:
- Navigate to `http://localhost/agents/test-echo`
- Click `14d`, then `7d`; click "Full details →"; click the "Overview" tab again

**Expected**:
- [ ] The "Overview" tab is selected without clicking anything
- [ ] A lead card with the agent's name, a "Full details →" link and a "New task" button; "Full details →" opens the Info tab
- [ ] A one-line compatibility summary (for example "Compatible — all checks passing")
- [ ] Section headings "Activity trends" (with `7d` / `14d` / `30d` buttons and an "N running · N queued" counter), "Health & reliability", "Recent activity" (with "View all →") and "Footprint"
- [ ] "Activity trends" shows charts ("Executions by type", "Execution completion rate", "Duration") or "No runs in the last 7d" — the text names whichever range is selected
- [ ] "Health & reliability" shows a badge (Healthy / Degraded / Unhealthy / Unknown) and either two small charts or "No health data yet"
- [ ] "Footprint" shows chips "N schedules", "N skills", "N shares" and "Sync: …"

### Step 2: Header doors
**Action**:
- Look at the page header; click the "Workspace" button; press browser Back
- Do **not** click "Talk" (it starts a voice call)

**Expected**:
- [ ] The header carries a "Workspace" button and a "Talk" button
- [ ] "Workspace" goes to `/workspace?agent=test-echo`; Back returns to the agent page

### Step 3: Tab deep links
**Action**:
- Navigate to `http://localhost/agents/test-echo?tab=loops`, then `?tab=nevermined`, then `?tab=does-not-exist`
- On the last page click the "Reports" tab and look at the address bar

**Expected**:
- [ ] `?tab=loops` opens with the Loops tab selected; `?tab=nevermined` opens with "Payments" selected
- [ ] An unknown id lands on Overview with no error
- [ ] Record whether clicking a tab changes the address bar (the source writes no `?tab=` on click, so it is expected to stay as it was)

### Step 4: Tab bar at 390 px
**Action**:
- Resize the window to 390 × 844 on `http://localhost/agents/test-echo`
- Click the "More" control at the right end of the tab strip and choose "Info"
- Resize back to about 1280 px

**Expected**:
- [ ] Only a few tabs are inline; the rest are in the "More" menu, and every tab from the Background list that applies to this agent is in one place or the other
- [ ] The tab strip does not scroll sideways and the page has no horizontal scroll
- [ ] Choosing "Info" shows the Info tab; at 1280 px more tabs move back inline

## Test: Reports

### Step 5: Reports tab
**Action**:
- Click the "Reports" tab; set the range selector to "All time"

**Expected**:
- [ ] Heading "Reports", the line "Structured reports this agent has published." and a "Refresh" button
- [ ] A type selector starting at "All types", a range selector with `24h` / `7d` / `30d` / `All time`, and a search box with placeholder "Search title / type"
- [ ] Either report rows (title, a type chip, a timestamp, a ▸ marker) or one of "No reports yet. Agents publish reports via the report MCP tool." / "No reports match these filters."
- [ ] If rows exist, clicking one expands it and shows "Export .xlsx" and "Export PDF" — do not click them; click the row again to collapse

**Verify**: `curl -s -H "$AUTH" "$API/api/agents/test-echo/reports?hours=0"`
- [ ] The array length equals the number of rows shown under "All time"

## Test: Schedules

### Step 6: Schedules tab and the create form
**Action**:
- Click the "Schedules" tab, then "New Schedule"

**Expected**:
- [ ] Heading "Scheduled Tasks" with "Automate agent tasks with cron schedules" and a "New Schedule" button
- [ ] Below: existing schedule rows, or "No schedules configured" with a "Create a schedule" button
- [ ] The form is titled "Create Schedule" with fields "Name" (placeholder "Daily report"), "Cron Expression" (placeholder `0 9 * * *`, preset buttons under it), "Task Message", "Description (optional)", "Timezone" (default UTC), "Timeout", "Max Retries", "Retry Delay", "Allowed Tools", and a ticked box "Enable schedule immediately"

### Step 7: Create one disabled, far-future schedule
**Action**:
- Name `sweep-tmp-33`; Cron Expression `0 3 1 1 *`; Task Message `sweep-tmp-33 placeholder — never meant to run`
- Leave Timezone at UTC; **untick** "Enable schedule immediately"; click "Create"

**Expected**:
- [ ] No red text appears under the cron field
- [ ] The form closes and a row "sweep-tmp-33" appears with the status pill "Disabled"
- [ ] The row shows the cron `0 3 1 1 *` in code style, the timezone `UTC`, and four icon buttons with tooltips "Run now", "Enable", "Edit", "Delete"

**Verify**: re-run the Setup command
- [ ] `sweep-tmp-33` is listed with `enabled` False. Record its id.

### Step 8: Toggle it on, then off again
**Action**:
- Click the "Enable" icon button on the `sweep-tmp-33` row; read the row; click the same button (now "Disable")
- Do **not** click "Run now"

**Expected**:
- [ ] After the first click the pill reads "Active" and the row shows either "Next: …" or "Will not fire — autonomy off"
- [ ] If autonomy is off on `test-echo`, a banner "Autonomy is off — this schedule will not fire" with an "Enable autonomy" button appears — do not click it; record whether it showed
- [ ] After the second click the pill reads "Disabled" again

## Test: Loops, Playbooks, Skills

### Step 9: Loops tab
**Action**:
- Click the "Loops" tab; click "Run Loop", read the form, then click the same button (now "Cancel")

**Expected**:
- [ ] Heading "Loops" with "Run a task repeatedly — fixed count or until a stop signal. Each iteration runs sequentially."
- [ ] A list of past loops, or "No loops yet. Start one with “Run Loop”."
- [ ] The form shows "Message template *", "Max runs *", "Stop signal", "Delay between runs (seconds)", "Max cost (USD)", "On iteration failure", and buttons "Reset" and "Start Loop"; "Start Loop" is disabled while the message is empty
- [ ] "Cancel" hides the form; no loop was started (`curl -s -H "$AUTH" $API/api/agents/test-echo/loops` is unchanged)

### Step 10: Playbooks tab
**Action**:
- Click the "Playbooks" tab (in the "More" menu if not inline)

**Expected**:
- [ ] Heading "Playbooks" with "Invoke agent skills directly. Skills are loaded from `.claude/skills/`"
- [ ] Either "No playbooks found", or a search box (placeholder "Search playbooks...") above cards that each have a "Run" button — do not click "Run"
- [ ] If cards exist, typing `zzzz` in the search shows `No playbooks found matching "zzzz"`; clear the box afterwards
- [ ] The screen agrees with `curl -s -H "$AUTH" $API/api/agents/test-echo/playbooks` (its `skills` array)

### Step 11: Skills tab
**Action**:
- Click the "Skills" tab. If the Skills tab is not present on this instance, record `SKIPPED (not present)` and continue.
- Tick nothing, save nothing, and do not click "Sync now" or "Unassign library skill"

**Expected**:
- [ ] Heading "Skills" with a line beginning "Assign skills from the shared library to this agent."
- [ ] Exactly one of: "No skills library is configured" (with a "Configure the library" link); "The library is configured but has no skills yet"; or two sections — "Assigned to this agent" (a list, or "No skills assigned yet — pick some from the library below and save.") and "Library" (a checkbox list with "Tick to assign, then save.")
- [ ] The assigned list has as many entries as `curl -s -H "$AUTH" $API/api/agents/test-echo/skills` returns

## Test: Payments and Info

### Step 12: Payments tab, unconfigured
**Action**:
- Click the "Payments" tab; type nothing

**Expected**:
- [ ] Heading "Nevermined x402 Payments" with "Configure per-request monetization via the x402 payment protocol"
- [ ] Fields "NVM API Key", "Environment", "Agent ID", "Plan ID", "Credits per Request"
- [ ] With no payment config: the button reads "Save Configuration" and is disabled, and there is no enable/disable switch, no "Remove" button and no "Payment Log" section

**Verify**: `curl -s -o /dev/null -w "%{http_code}\n" -H "$AUTH" $API/api/nevermined/agents/test-echo/config`
- [ ] `404` when unconfigured; `200` means a config exists — record it and leave it alone

### Step 13: Info tab
**Action**:
- Click the "Info" tab

**Expected**:
- [ ] A header card titled "Test Echo" with `v1.0.0` and "Trinity Platform", and the description "Simple echo agent for testing basic chat functionality"
- [ ] A "Resources" card showing "CPU:" and "Memory:", either directly or inside a collapsed "Technical details" section — record which
- [ ] Not "Couldn't load template info" (the running agent did not answer — record it as a defect)
- [ ] The values agree with `curl -s -H "$AUTH" $API/api/agents/test-echo/info`

## Test: Conditional tabs and edge routes

### Step 14: Dashboard, Brain, A2A, Git
**Action**:
- For each of "Dashboard", "Brain", "A2A", "Git": if the tab is present on `test-echo`, click it and wait for it to settle. If the tab is not present on this instance, record `SKIPPED (not present)` and continue.
- Open `http://localhost/agents/test-delegator` and read its tab list (click nothing that changes state)

**Expected**:
- [ ] Each tab that is present renders content or a named empty state, with no console errors
- [ ] `test-delegator` shows the same fixed tabs (Overview … Info); record any conditional tab that differs from `test-echo`

### Step 15: Unknown agent and the brain route
**Action**:
- Navigate to `http://localhost/agents/does-not-exist`; click "Back to Dashboard"
- Navigate to `http://localhost/agents/test-echo/brain`

**Expected**:
- [ ] The first page shows "Agent not found" and "`does-not-exist` doesn't exist, or you don't have access to it." — not a blank page, and no "Retry" button
- [ ] "Back to Dashboard" goes to `/`
- [ ] The brain URL either renders a full-page brain view or redirects to `/agents/test-echo` — record which (a redirect is expected for an agent without that capability)

## Cleanup / Restore

Original state: the schedule list recorded in Setup (which did not contain `sweep-tmp-33`).

1. Open `http://localhost/agents/test-echo?tab=schedules`, click the "Delete" icon on the `sweep-tmp-33` row, and in the dialog titled "Delete Schedule" ("Are you sure you want to delete the schedule "sweep-tmp-33"?") click "Delete".
   - [ ] The row disappears
2. Re-run the Setup command. Only if `sweep-tmp-33` is still listed:
   `curl -s -X DELETE -H "$AUTH" $API/api/agents/test-echo/schedules/<id-from-step-7>`
   - [ ] The list equals the Setup record

## Manual-only (not run unattended)

- Letting a schedule fire, "Run now", and the per-schedule webhook controls.
- Starting a loop, running a playbook, assigning or syncing skills, saving a payment config.
- "Talk" (needs a microphone and a voice provider); the non-owner view (needs a second account).

## Critical Validations

1. `/agents/test-echo` lands on Overview and its sections render without errors
2. `?tab=<id>` opens the named tab; an unknown id falls back to Overview
3. A schedule can be created disabled, toggled Active and back, and deleted — and the API agrees at each point
4. At 390 px every tab is reachable through "More" with no horizontal page scroll
5. `/agents/does-not-exist` shows "Agent not found", not a blank page

## Success Criteria

- [ ] Steps 1–15 pass or are recorded `SKIPPED (not present)` where allowed
- [ ] No message, task, loop or playbook run was sent to any agent
- [ ] `test-echo`'s schedule list equals the Setup record
- [ ] No console errors on any tab

## Troubleshooting

- **"Create" stays disabled / red text under the cron field**: the cron did not validate — re-type `0 3 1 1 *` with single spaces.
- **A red inline error after a toggle or delete**: the request failed; the text names the verb. Record its detail line.
- **Playbooks shows "Agent Not Running" / "Run Loop" is disabled**: `test-echo` is stopped — both read from the running container.
