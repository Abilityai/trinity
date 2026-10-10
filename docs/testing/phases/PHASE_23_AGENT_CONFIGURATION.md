# Phase 23: Agent Configuration

> **Purpose**: Verify the per-agent configuration controls — autonomy toggle, resource limits, per-surface model selector, the Settings tab and the Info tab.
> **Duration**: ~20 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: Each control reads its real value, one autonomy flip and one resource change round-trip through the API, and everything is back to its starting value
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

Agent configuration lives in four places on `/agents/:name`:

- **Header row 2** (`components/AgentHeader.vue`) — the `AUTO` / `Manual` autonomy switch, the `Read-Only` / `Editable` switch, tags, and a gear button "Configure resources (Memory/CPU)" that opens `components/ResourceModal.vue`.
- **Model selector** (`components/ModelSelector.vue`) — there is no single per-agent model setting; a model field appears on each surface that starts work: Chat, Tasks, the Schedules form and the Loops form.
- **Settings tab** (owner-only, `components/settings/SettingsPanel.vue`) — a stack of section cards.
- **Info tab** (`components/InfoPanel.vue`) — read-only template metadata.

Replaces the January flow that used a "Full Capabilities" toggle and a single model dropdown: container capabilities are API-only today (no UI control), and the model is chosen per surface. Host-level `docker inspect` / database checks are gone.

All steps use `test-counter`. Saving a changed resource limit **restarts the container** (stop, then start), so this phase makes exactly one change and one restore. No messages are sent.

## Prerequisites

- [ ] Logged in at http://localhost as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `test-counter` is running
- [ ] A token in `$TOKEN`:
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d "username=admin&password=$ADMIN_PASSWORD" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

## Setup

Record the starting values — the Cleanup section restores exactly these.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-counter/autonomy
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-counter/resources
```

- [ ] Record `autonomy_enabled` as **AUTONOMY_0**
- [ ] Record `memory`, `cpu` (the stored overrides; `null` means "inherit") as **MEM_OVERRIDE_0**, **CPU_OVERRIDE_0**
- [ ] Record `current_memory`, `current_cpu` (what the container runs with) as **MEM_0**, **CPU_0**

---

## Test: Autonomy toggle

### Step 1: Read the toggle
**Action**:
- Open http://localhost/agents/test-counter
- In the second row of the header card, find the switch labelled `AUTO` or `Manual`

**Expected**:
- [ ] The label reads `AUTO` when AUTONOMY_0 is `true`, `Manual` when it is `false`
- [ ] The switch tooltip reads `Autonomy Mode ON - Click to disable scheduled tasks` or `Autonomy Mode OFF - Click to enable scheduled tasks` accordingly
- [ ] Next to it a second switch reads `Read-Only` or `Editable` (do not click it)

### Step 2: Flip autonomy
**Action**:
- Click the autonomy switch once

**Expected**:
- [ ] The label flips (`Manual` → `AUTO` or the reverse)
- [ ] A success notification appears whose text starts `Autonomy enabled` or `Autonomy disabled` (it continues with a sentence about this agent's schedules — record it)

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-counter/autonomy
# autonomy_enabled is now the opposite of AUTONOMY_0
```

### Step 3: Flip it back and check the Dashboard tile
**Action**:
- Click the autonomy switch again
- Open http://localhost/?view=grid and find the `test-counter` tile

**Expected**:
- [ ] The header label is back to its Step 1 value and a second notification appeared
- [ ] `GET /api/agents/test-counter/autonomy` returns `autonomy_enabled` equal to AUTONOMY_0
- [ ] The tile's action row has a switch labelled `Auto` whose on/off state matches AUTONOMY_0 (do not click it)

---

## Test: Resource limits

### Step 4: Open the resource dialog
**Action**:
- Back on http://localhost/agents/test-counter, click the gear button at the right end of header row 2 (tooltip "Configure resources (Memory/CPU)")

**Expected**:
- [ ] A dialog titled `Configure Resource Allocation` opens
- [ ] It carries the warning `If the agent is running, it will be automatically restarted to apply changes.`
- [ ] A `Memory` select offers `Inherit default (…)`, `1 GB`, `2 GB`, `4 GB`, `8 GB`, `16 GB`, `32 GB`, `64 GB`
- [ ] A `CPU Cores` select offers `Inherit default (…)`, `1 Core`, `2 Cores`, `4 Cores`, `8 Cores`, `16 Cores`
- [ ] The selected options correspond to MEM_0 and CPU_0
- [ ] No red banner starting `Could not load current resource limits` is shown
- [ ] Buttons `Cancel` and `Save Changes` are present

### Step 5: Cancel leaves everything alone
**Action**:
- Change `Memory` to any other value, then click `Cancel`

**Expected**:
- [ ] The dialog closes with no notification and no restart
- [ ] `GET /api/agents/test-counter/resources` still returns the Setup values

### Step 6: Change CPU once
**Action**:
- Reload the page, reopen the dialog
- In `CPU Cores` choose `2 Cores` if CPU_0 is `1`, otherwise choose `1 Core`. Leave `Memory` untouched
- Click `Save Changes` and wait up to 60 seconds

**Expected**:
- [ ] The button reads `Saving...` briefly, then the dialog closes
- [ ] Notifications appear in order: `Resource limits updated`, `Restarting agent to apply new resource limits...`, `Agent restarted with new resource limits.`
- [ ] The status badge passes through a non-running state and returns to `running`
- [ ] The header stats row reads `/ 2 cores` (or `/ 1 cores`) matching the value chosen

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-counter/resources
# cpu and current_cpu both equal the chosen value; memory and current_memory unchanged
```

If instead a notification reads `Agent did not stop within 30s — not restarting automatically…` or starts `Restart failed:`, record it as a Critical finding and go straight to Cleanup.

---

## Test: Model selector (per surface)

### Step 7: Tasks tab
**Action**:
- Click the `Tasks` tab. Find the field labelled `Model` above the task input and record its current text as **TASK_MODEL_0**
- Click the chevron at the right of the field

**Expected**:
- [ ] A dropdown lists preset models, each a label starting `Claude ` with a one-line note underneath (e.g. `Claude Sonnet 5`, `Claude Haiku 4.5` — record the full list as shown)
- [ ] The entry matching the current value carries a check mark
- [ ] Clicking a different entry closes the dropdown and puts that model's id (a `claude-…` string) in the field
- [ ] Typing in the field filters the list; any free text is accepted

**Action** (restore now):
- Clear the field and type TASK_MODEL_0 back, then click elsewhere

### Step 8: Chat, Schedules and Loops
**Action**:
- `Chat` tab: find the narrow model field in the chat toolbar, left of `New Chat`
- `Schedules` tab: click `New Schedule`, find the `Model` field, then click `Cancel`
- `Loops` tab: click `Run Loop`, find the `Model` field, then click the same button (now `Cancel`)

**Expected**:
- [ ] Chat: the field's placeholder is `Default model` when empty, and its chevron opens the same preset list (do not select anything — this choice is remembered across agents)
- [ ] Schedules: the field is labelled `Model`, with the helper text `Leave blank to use the platform default`
- [ ] Loops: the field is labelled `Model` with the placeholder `Agent default`
- [ ] Neither form created anything (no new schedule or loop row after cancelling)

---

## Test: Settings tab

### Step 9: Sections load
**Action**:
- Open http://localhost/agents/test-counter?tab=settings and scroll through the tab. **Do not change or save anything in Steps 9–11.**

**Expected**:
- [ ] `Settings` is the selected tab
- [ ] Section `Guardrails` with number fields `Max turns (chat)` and `Max turns (task)` and a `Save` button
- [ ] Section `Parallel Capacity` with the field `Max parallel tasks` and a `Save` button
- [ ] Section `Expose via MCP` with a switch and the state text `Exposed` or `Not exposed`
- [ ] Section `Trinity access key`
- [ ] Section `Reliability — dispatch circuit breaker` with the control `Enable the dispatch breaker for this agent`; if an amber notice `Globally disabled — this toggle won't take effect yet` is shown, record it
- [ ] Section `Wake this agent when its asks end` with the control `Wake this agent when an ask it raised ends`
- [ ] Section `Git sync` — for this fixture it starts `This agent isn't connected to a GitHub repository, so there is nothing to sync.` (record it if toggles are shown instead)
- [ ] Section `Voice` with the row `Voice replies`
- [ ] No section is stuck on `Loading…` after 10 seconds, and none shows a `Couldn't load …` card

### Step 10: Admin-only and optional sections
**Action**:
- Look for a section titled `Permissions to change itself` and one titled `Cross-model validation`

**Expected**:
- [ ] `Permissions to change itself`: if not present on this instance, record `SKIPPED (not present)` and continue. If present, it lists four grants as switches; record each one's on/off state and whether the line `None granted. Without them the agent is refused, by name, when it tries any of the changes below.` is shown. Logged in as admin, the notice `Only an instance admin can change these.` is **not** shown
- [ ] `Cross-model validation`: if not present on this instance, record `SKIPPED (not present)` and continue. If present, record whether `Enabled` is ticked

### Step 11: Legacy deep link
**Action**:
- Open http://localhost/agents/test-counter?tab=guardrails

**Expected**:
- [ ] The page lands on the `Settings` tab with `Guardrails` as its first section

---

## Test: Info tab

### Step 12: Template metadata
**Action**:
- Click the `Info` tab (last in the strip; it may sit in the overflow menu)

**Expected**:
- [ ] Either a profile card with the agent's name as its heading, or the empty state `No Template Information` — record which
- [ ] If the profile is shown: a description paragraph, and a collapsible `Technical details` row may be present; any `Resources` card lists the template's `CPU` / `Memory` values (these are the template's declaration, not the live limits changed in Step 6)
- [ ] No red load-failure card

### Step 13: Header at 390 px
**Action**:
- Resize to 390 px wide on http://localhost/agents/test-counter, then back to 1280 px

**Expected**:
- [ ] The autonomy and read-only switches, tags and stats wrap onto several lines; nothing is clipped and the page body does not scroll horizontally
- [ ] The resource gear button is still reachable

---

## Cleanup / Restore

1. **Autonomy** — confirm `GET /api/agents/test-counter/autonomy` returns AUTONOMY_0. If not, click the header switch once.
2. **CPU, live value** — open the resource dialog, set `CPU Cores` to the option matching CPU_0 (e.g. `1 Core`), click `Save Changes`, wait for `Agent restarted with new resource limits.` and the `running` badge.
3. **CPU, stored override** — only if CPU_OVERRIDE_0 was `null`: reopen the dialog, set `CPU Cores` to `Inherit default (…)`, click `Save Changes`. Expect only `Resource limits updated` and **no** restart. (The select may snap back to showing the current value; that is expected.)
4. **Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-counter/resources
# Must equal the Setup reading: memory=MEM_OVERRIDE_0, cpu=CPU_OVERRIDE_0, current_memory=MEM_0, current_cpu=CPU_0
```
   If the stored overrides still differ, restore them through the API (substitute the recorded values, `null` unquoted):
```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"memory": null, "cpu": null}' http://localhost:8000/api/agents/test-counter/resources
```
5. **Task model** — the `Model` field on the Tasks tab reads TASK_MODEL_0.
6. Confirm `test-counter` shows `running`.

## Manual-only (not run unattended)

- Container capabilities (`GET`/`PUT /api/agents/{name}/capabilities`) — API-only, no UI control, and a change recreates the container.
- Toggling `Read-Only`, saving Guardrails / Parallel Capacity, exposing the agent via MCP, rotating the Trinity access key, and granting any `Permissions to change itself` switch — each changes how the fixture behaves for other phases.
- Non-owner view: a second, non-owner account should see no autonomy switch, no gear button and no `Settings` tab.
- Verifying that a raised memory limit is actually enforced needs host-level inspection.

## Critical Validations

1. One autonomy flip is reflected by `GET /api/agents/test-counter/autonomy` and flips back.
2. One CPU change saves, restarts the agent, and shows in both the header and `GET …/resources`.
3. After Cleanup, `GET …/resources` equals the Setup reading exactly and the agent is `running`.
4. The Settings tab renders every listed section without an endless spinner.
5. The model selector opens a preset list on all four surfaces.

## Success Criteria

- [ ] Steps 1–13 pass, with observations recorded where asked
- [ ] Autonomy, resource limits and the Tasks model are back at their starting values
- [ ] `test-counter` restarted exactly twice (Step 6 and Cleanup item 2) and is running
- [ ] Nothing on the Settings tab was saved

## Troubleshooting

- **No autonomy switch, gear button or Settings tab** — these render only for a viewer who owns (or administers) the agent and never for the system agent.
- **`Save Changes` closes the dialog but nothing restarts** — the chosen values equal the current ones, so no restart is triggered; only `Resource limits updated` appears.
- **`Agent did not stop within 30s`** — the UI gives up waiting and does not start the agent; start it with the header's running switch, then continue with Cleanup.
- **Invalid value rejected** — the API accepts memory `1g…64g` and CPU `1, 2, 4, 8, 16` only and answers `400` otherwise.
