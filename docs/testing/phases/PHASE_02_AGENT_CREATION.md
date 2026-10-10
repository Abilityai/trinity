# Phase 02: Agent Creation

> **Purpose**: Exercise the Create Agent modal end to end — open, validate, create one throwaway agent, see it running, delete it.
> **Duration**: ~12 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: A pass proves an agent can be created and deleted entirely through the UI, that bad or duplicate names are refused with a named error, and that nothing is left behind.
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

Agents are created from one modal, `components/CreateAgentModal.vue` (title "Create New
Agent"). It opens from the **Create Agent** button in the Dashboard's control cluster
(every Dashboard mode) and from **Use Template** on a Library template card. The modal
sends only a slug, an optional display name and a template; the backend sanitises the
slug (non-alphanumerics → `-`, lower-cased) and returns a named error if nothing is left
or the name is taken. Deletion is the trash-can button in the agent header and is
confirmed by a "Delete Agent" dialog. Deletion is a soft delete: the container is removed
but the slug stays reserved (and the workspace volume kept) until the retention sweep
purges it, so a deleted slug cannot be reused straight away. That is why the throwaway
carries a per-run suffix.

Replaces the January flow that created eight GitHub-template agents and asserted SSH
ports and "Context 0%". The fixtures already exist, so this phase now creates ONE
throwaway agent and also absorbs the old Phase 12 cleanup. The fixture templates
(`test-echo` etc.) are hidden from the template catalog, so the throwaway uses the
"Blank Agent (Claude Code)" option.

## Prerequisites

- [ ] Logged in as `admin` (password is `ADMIN_PASSWORD` from `.env`)
- [ ] `test-echo`, `test-counter`, `test-delegator` exist and are running
- [ ] A run suffix chosen in Setup — the throwaway is `sweep-tmp-02-<run>`
- [ ] A Bearer token for API checks, referred to as `$TOKEN`

## Setup

- Pick a run suffix `<run>`: the current local time as six digits `HHMMSS` (e.g. `142507`).
  The throwaway agent is `sweep-tmp-02-<run>`, written `$NAME` below. **Record `$NAME`.**
- Record the current list of agent names so Cleanup can prove the list is unchanged:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents \
  | python3 -c "import sys,json; print(sorted(a['name'] for a in json.load(sys.stdin)))"
```
- If that list already contains any `sweep-tmp-02-…` agent, an earlier run left it behind:
  open `http://localhost/agents/<that name>` and delete it via Steps 12–13 first.

## Test: Opening the modal

### Step 1: Create Agent button on the Dashboard
**Action**:
- Navigate to `http://localhost/?view=list`
- Find the button with the accessible name "Create Agent" (plus icon; the text label shows at desktop width) in the controls at the top right of the Dashboard

**Expected**:
- [ ] The agent list is visible with a "Search agents..." box and the three fixtures
- [ ] The **Create Agent** button is present and enabled

### Step 2: Modal contents
**Action**:
- Click **Create Agent**

**Expected**:
- [ ] A modal titled "Create New Agent" opens above the page
- [ ] Field "Slug / Identifier" with placeholder `my-agent` and the help text beginning "The permanent identifier used in URLs, containers, and API keys."
- [ ] Field "Display name (optional)" with placeholder "e.g. Marketing Assistant"
- [ ] A "Template" list containing "Blank Agent (Claude Code)" (selected by default — highlighted with a check mark) and "GitHub Repository"
- [ ] Record whether a "Local Templates" and/or "GitHub Templates" group is listed and how many entries each has. "Test Echo", "Test Counter" and "Test Delegator" must NOT be listed
- [ ] Footer buttons **Create Agent** and **Cancel**

### Step 3: Template options toggle correctly, Cancel discards
**Action**:
- Click "GitHub Repository"
- Click "Blank Agent (Claude Code)" again
- Click **Cancel**

**Expected**:
- [ ] Selecting "GitHub Repository" reveals an input with placeholder "owner/repo or https://github.com/owner/repo"; re-selecting Blank Agent hides it
- [ ] **Cancel** closes the modal; no agent was created (the list still shows only the agents recorded in Setup)

### Step 4: Library entry point
**Action**:
- Navigate to `http://localhost/library`
- If a "Starter Templates" section with cards is shown, click **Use Template** on the first card, read the modal, then click **Cancel**

**Expected**:
- [ ] The Library opens on the "Agent Templates" tab
- [ ] If starter cards exist: the same "Create New Agent" modal opens with that card's template pre-selected (highlighted) in the Template list; **Cancel** closes it
- [ ] If no "Starter Templates" section is shown, record `SKIPPED (no starter templates on this instance)`

## Test: Name validation

### Step 5: Empty slug is blocked in the browser
**Action**:
- Back on `http://localhost/?view=list`, open the modal, leave "Slug / Identifier" empty, click **Create Agent**

**Expected**:
- [ ] The browser's own required-field prompt appears on the slug field; the modal stays open
- [ ] No `POST /api/agents` request is sent

### Step 6: A slug with no usable characters is refused
**Action**:
- Type `!!!` in "Slug / Identifier", keep "Blank Agent (Claude Code)", click **Create Agent**

**Expected**:
- [ ] The modal stays open and shows, in red above the footer: "Invalid agent name - must contain at least one alphanumeric character"
- [ ] No new agent appears

### Step 7: A duplicate slug is refused
**Action**:
- Replace the slug with `test-echo`, click **Create Agent**

**Expected**:
- [ ] The modal stays open and shows "Agent already exists"
- [ ] `test-echo` is untouched (still listed, still running)

## Test: Create, observe, delete

### Step 8: Create the throwaway (slug is sanitised)
**Action**:
- Replace the slug with `Sweep Tmp 02 <run>` — capitals and spaces exactly as written, with your suffix (e.g. `Sweep Tmp 02 142507`)
- Set "Display name" to `Sweep throwaway`
- Keep "Blank Agent (Claude Code)" selected
- Click **Create Agent** and wait up to 60 seconds

**Expected**:
- [ ] The button reads "Creating..." with a spinner while the request runs
- [ ] The modal closes by itself with no error (the modal shows no preview of the final slug — that is expected)
- [ ] The agent list gains exactly one new entry

**Verify** (API):
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/$NAME \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('name'), d.get('status'))"
# sweep-tmp-02-<run> running
```
- [ ] The agent exists under the sanitised slug `sweep-tmp-02-<run>` (lower-case, spaces turned into hyphens). **Record the actual name.** If the API returns 404, find the new name by diffing `GET /api/agents` against the Setup list, record it as a defect, and use that name for every remaining step

### Step 9: The new agent appears on the Dashboard
**Action**:
- On `http://localhost/?view=list`, type `sweep` in "Search agents..."

**Expected**:
- [ ] One entry matches; it shows the display name `Sweep throwaway` and/or the slug `$NAME`
- [ ] Clear the search box afterwards — the three fixtures are listed again

### Step 10: Agent detail opens and the agent is running
**Action**:
- Navigate to `http://localhost/agents/$NAME`

**Expected**:
- [ ] The header shows the agent and a status pill reading `running` (allow up to 60 seconds; reload once if it still reads another value)
- [ ] The Overview tab is the active tab
- [ ] The tab strip includes at least Overview, Tasks, Chat, Files and Info
- [ ] The header has a trash-can button whose title is "Delete agent"
- [ ] Do not send this agent any chat message or task

### Step 11: Narrow viewport (390 px)
**Action**:
- Resize to 390 × 844 on `http://localhost/?view=list`, open the Create Agent modal, then **Cancel** and resize back to ≥ 1280 px

**Expected**:
- [ ] The Create Agent button is still present (icon only; accessible name "Create Agent")
- [ ] The modal fits the width with no horizontal page scroll; the Template list scrolls inside the modal and **Create Agent** / **Cancel** are reachable

### Step 12: Delete is confirmed first, and Cancel keeps the agent
**Action**:
- On `http://localhost/agents/$NAME`, click the trash-can button titled "Delete agent"
- In the dialog, click **Cancel**

**Expected**:
- [ ] A dialog titled "Delete Agent" asks "Are you sure you want to delete this agent?" with buttons **Delete** and **Cancel**
- [ ] After **Cancel** the dialog closes and the agent page is unchanged

### Step 13: Delete the throwaway
**Action**:
- Click the trash-can button again, then **Delete**

**Expected**:
- [ ] The app navigates to the Dashboard (`http://localhost/`)
- [ ] `$NAME` is no longer listed in any Dashboard mode; the three fixtures still are

**Verify** (API):
```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/$NAME
# 404
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/admin/soft-deleted/agents
# read-only: record whether $NAME appears in this list (expected — deletion is a soft delete)
```

## Cleanup / Restore

- The only thing this phase creates is the agent `$NAME` (`sweep-tmp-02-<run>`), deleted in Step 13.
  If any step after Step 8 failed, delete it now: open `http://localhost/agents/<recorded name>`,
  click the "Delete agent" trash-can, confirm **Delete**.
- Report the recorded agent name in the run output. Its slug stays reserved and its
  workspace volume (`agent-<name>-workspace`) is kept until the retention sweep purges
  the soft-deleted agent; whoever maintains the host can remove the volume earlier.
- Do NOT recover the agent via `/api/admin/soft-deleted/agents/{name}/recover`.
- Confirm `GET /api/agents` lists the same names recorded in Setup.
- No setting, fixture or template was changed.

## Manual-only (not run unattended)

- Creating from "GitHub Repository" or a GitHub template (needs external GitHub; shows a post-create validation step instead of closing).
- Templates that copy a repository into your own account (need a GitHub token and create a real repository).
- Creating as a non-admin user / role limits and per-user agent quota errors (need a second account).

## Critical Validations

1. The modal opens from the Dashboard and lists "Blank Agent (Claude Code)" selected by default (Step 2).
2. `!!!` and `test-echo` are each refused with the named error and create nothing (Steps 6–7).
3. `Sweep Tmp 02 <run>` is created as `sweep-tmp-02-<run>` and reaches `running` (Steps 8, 10).
4. Deletion requires the "Delete Agent" confirmation and removes the agent (Steps 12–13).
5. The fixture trio is untouched and the agent list equals the Setup list at the end.

## Success Criteria

- [ ] Modal opens from Dashboard (and Library, where starter cards exist) and cancels cleanly
- [ ] Empty, unusable and duplicate slugs are all refused without creating anything
- [ ] One throwaway agent created, sanitised slug confirmed, status `running`
- [ ] Throwaway deleted through the header control; API returns 404 afterwards
- [ ] Modal and Create button usable at 390 px

## Troubleshooting

- **"Loading templates..." never resolves or "Failed to load templates" with "Try again"**: `GET /api/templates` failed; the Blank Agent option is not shown in that state — click "Try again".
- **Create returns a quota message**: the per-user agent limit is reached; the modal shows the backend's message verbatim. Record it and stop — do not delete other agents to make room.
- **Step 8 returns "Agent already exists"**: the slug is still reserved by a soft-deleted agent from an earlier run that used the same suffix. Choose a new `<run>` suffix and repeat Step 8 once.
- **Create fails mentioning an existing data volume**: a volume with that name is on the host and no agent claims it. Choose a new `<run>` suffix and repeat Step 8 once; record the message.
- **No trash-can button in the header**: the signed-in user may not delete this agent (`can_delete` false) — confirm you are signed in as `admin`.
- **Status pill never reaches `running`**: check `GET /api/agents/$NAME` for `status`; record the value and still run Steps 12–13 to clean up.
