# Phase 32: Library — templates, systems, skills

> **Purpose**: Walk the three Library tabs, open and cancel the Create Agent form, and confirm the page stays read-only-safe at narrow width and in both themes
> **Duration**: ~15 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: The Library renders every asset kind with honest empty states, its tabs are addressable, and nothing was created, installed, assigned or synced
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

The Library (`/library`, `views/Library.vue`) is the one page for installable assets. It has a
tab strip addressed by `?tab=`: **Agent Templates** (`templates`, the default), **Systems**
(`systems`, only for accounts that may create agents) and **Skills** (`skills`). The old
`/templates` path redirects here and carries its query and hash.

Two things that surprise people: the fixture templates (`test-echo`, `test-counter`,
`test-delegator`) are marked `hidden: true` and do **not** appear as cards — the bundled cards
are the starters under `config/agent-templates/` that are not hidden (`sage`, `scout`,
`scribe`). And there is no search box on the Templates or Skills tab, and no skill "detail"
page: a skill is a card, and only a skill *set* expands.

This phase is observe-only. It sends no message to any agent.

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] Browser window at about 1280 px wide
- [ ] For the API checks, a token:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d "username=admin&password=$ADMIN_PASSWORD" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

- [ ] Record the agent count now, to compare at the end:
  `curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents | python3 -c "import sys,json; print(len(json.load(sys.stdin)))"`

## Test: Getting there

### Step 1: Open the Library from the nav
**Action**:
- Click **Library** in the top navigation

**Expected**:
- [ ] Page heading "Library" with the line "Installable assets for your fleet — agent templates, systems, and skills"
- [ ] A tab strip with "Agent Templates" and "Skills"; "Systems" sits between them for an admin
- [ ] "Agent Templates" is the active tab and the address bar reads `/library?tab=templates`

### Step 2: Legacy redirect and tab addressing
**Action**:
- Navigate to `http://localhost/templates?tab=skills`
- Then navigate to `http://localhost/templates#skills`
- Then navigate to `http://localhost/library?tab=nope`

**Expected**:
- [ ] First URL ends at `/library?tab=skills` with the Skills tab showing
- [ ] Second URL also ends on the Skills tab, at `/library?tab=skills` with the `#skills` hash removed
- [ ] Third URL falls back to the Agent Templates tab and the address is rewritten to `?tab=templates`
- [ ] Click each tab in turn, then press browser Back once: you leave the Library rather than stepping back through tabs (tab clicks replace history, they do not push)

## Test: Agent Templates tab

### Step 3: Template cards
**Action**:
- Open `http://localhost/library?tab=templates`

**Expected**:
- [ ] Section heading "Agent Templates" with a refresh icon button (tooltip "Refresh templates")
- [ ] A "Starter Templates" group with a count in brackets, and one card per bundled template
- [ ] Each card shows the template's display name, the caption "Bundled template", a description (at most three lines), and a full-width "Use Template" button; a card may also show "N skills", "N MCPs" or "N credentials" counters when the template declares them
- [ ] No card is named Test Echo, Test Counter or Test Delegator
- [ ] A "GitHub Templates" group: either cards captioned with an `owner/repo`, or the placeholder "No GitHub templates configured" with a "Create from a GitHub repository" button and a "Settings → GitHub Templates" link
- [ ] A "Custom Agent" group with a "Blank Agent" card and a "Create Blank Agent" button
- [ ] No search or filter control anywhere on this tab

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/templates \
  | python3 -c "import sys,json; [print(t['id'], '|', t.get('display_name'), '|', t.get('source')) for t in json.load(sys.stdin)]"
```
- [ ] The number of `local` rows equals the bracketed "Starter Templates" count, and no id is `local:test-echo`

### Step 4: Open the Create Agent form from a card, then cancel
**Action**:
- Click "Use Template" on the first Starter card
- Look at the form; do **not** type a name
- Click "Cancel"

**Expected**:
- [ ] A modal titled "Create New Agent" opens
- [ ] It has a "Slug / Identifier" field (placeholder `my-agent`), a display-name field (placeholder `e.g. Marketing Assistant`), and a "Template" chooser listing "Blank Agent (Claude Code)", "GitHub Repository" and a "Local Templates" group
- [ ] The card you clicked is the highlighted (ringed) entry in the chooser
- [ ] Buttons "Create Agent" and "Cancel"
- [ ] After Cancel the modal is gone and you are still on `/library?tab=templates`

### Step 5: Blank agent entry point, cancelled
**Action**:
- Click "Create Blank Agent"
- Click "Cancel"

**Expected**:
- [ ] The same modal opens with "Blank Agent (Claude Code)" highlighted
- [ ] Cancel closes it; no agent was created (confirmed in Cleanup)

## Test: Systems tab

### Step 6: Systems install surface (observe only)
**Action**:
- Click the "Systems" tab. If the Systems tab is not present on this instance, record `SKIPPED (not present)` and continue at Step 8.

**Expected**:
- [ ] Address becomes `/library?tab=systems`; heading "Systems" with the line beginning "Install a multi-agent system from a manifest"
- [ ] Three source buttons: "Pick a system" (selected), "Upload a file", "Paste YAML"
- [ ] Under "Pick a system": either cards for bundled manifests — each with a name, an "N agent(s)" chip, optional "N schedule(s)" / "already installed" / "cannot deploy" marks and a "Load this manifest" link — or the text "No bundled system manifests are available on this instance."
- [ ] A "Manifest YAML" editor, a "Preview" button and a "Deploy" button; both are disabled while the editor is empty

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/systems/manifests \
  | python3 -c "import sys,json; [print(m['id'], m.get('agent_count'), m.get('valid')) for m in json.load(sys.stdin)]"
```
- [ ] One row per card

### Step 7: Load a manifest into the editor, deploy nothing
**Action**:
- If there is at least one bundled card, click its "Load this manifest" link. Otherwise record `SKIPPED (no bundled manifests)`.
- Click "Upload a file", then "Paste YAML", then back to "Pick a system"
- Do **not** click "Preview" or "Deploy"

**Expected**:
- [ ] The link text changes to "Loaded below" and the "Manifest YAML" editor fills with YAML, with a character count beside its label
- [ ] "Upload a file" shows a drop area reading "Choose a .yaml or .yml manifest"
- [ ] Switching to Agent Templates and back to Systems keeps the loaded YAML in the editor
- [ ] If a card titled "Remove a deployed system" appears below, leave it untouched

## Test: Skills tab

### Step 8: Skills library state
**Action**:
- Click the "Skills" tab

**Expected**:
- [ ] Address becomes `/library?tab=skills`; heading "Skills"
- [ ] Exactly one of these states — record which:
  - "No skills library is configured" with a "Configure a skills library in Settings" button
  - "Configured but never synced" with a "Sync now" button
  - "The library has no skills yet"
  - a status line (short commit id · "N skills" · branch) with a "Sync now" button, above a grid of skill cards
  - "Couldn't load the skills library" with a retry (this is a defect — record it)
- [ ] No search or filter control on this tab

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/skills/library/status
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/skills/library \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d), [s['name'] for s in d][:5])"
```
- [ ] `configured` / `cloned` / `skill_count` agree with the state on screen, and the status payload contains no repository URL

### Step 9: Skill cards and sets (only if skills are listed)
**Action**:
- If the tab shows skill cards, read the first card. Otherwise record `SKIPPED (no skills listed)`.
- If a "Skill sets" section is shown, click one set's name to expand it, then again to collapse
- Do **not** use any "Assign to…" selector, "Assign" button, "×" on an agent chip, or "Sync now"

**Expected**:
- [ ] A card shows the skill name, an optional source badge, a description, small chips (for example a file count or size) and a line starting "Assigned to" — followed by a count of agents with name chips, or a "none" wording
- [ ] Expanding a set lists its member skill names; collapsing hides them
- [ ] Clicking a skill name does nothing — there is no skill detail view in the Library

**Verify** (use a name from the list):
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/skills/library/<skill-name> | head -c 400
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/skills/assignments | head -c 400
```
- [ ] The skill endpoint returns that skill; the "Assigned to" count on its card matches the assignments payload

## Test: Layout and themes

### Step 10: Card grid at 390 px
**Action**:
- Resize the window to 390 × 844
- Visit `?tab=templates`, then `?tab=systems` (if present), then `?tab=skills`
- Open "Use Template" once more at this width, then "Cancel"

**Expected**:
- [ ] Cards stack in a single column; no card or button is cut off
- [ ] The page does not scroll horizontally on any tab
- [ ] All tab labels are reachable — inline, or under a "More" control in the tab strip
- [ ] The Create Agent modal fits the width and its "Cancel" button is reachable by scrolling inside the modal

- Resize back to about 1280 px wide

### Step 11: Light and dark
**Action**:
- Note the current theme: the theme button in the top bar has a tooltip "Light mode (click to switch)", "Dark mode (click to switch)" or "System theme (click to switch)"
- Click it until the tooltip reads "Dark mode (click to switch)"; look at the Templates and Skills tabs
- Click it until it reads "Light mode (click to switch)"; look again

**Expected**:
- [ ] In both themes card titles, descriptions, counters and the dashed "Blank Agent" card are legible against their background
- [ ] The active tab is visibly marked in both themes
- [ ] No console errors appeared during the phase

## Cleanup / Restore

- Theme: click the theme button until its tooltip matches the value noted at the start of Step 11.
- Nothing else was changed. Confirm:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents \
  | python3 -c "import sys,json; print(len(json.load(sys.stdin)))"
```
- [ ] Agent count equals the number recorded in Prerequisites

## Manual-only (not run unattended)

- Deploying a system from a manifest (creates a fleet of agents) and removing one.
- "Sync now" on the skills library, and assigning or unassigning a skill from a Library card.
- The non-admin view (Systems tab hidden, "ask an admin" wording) — needs a second account.
- Creating an agent from a GitHub repository — needs external GitHub.

## Critical Validations

1. `/templates` lands on `/library` and keeps `?tab=` / migrates `#skills`
2. The fixture templates are not offered as cards, and the card count matches `GET /api/templates`
3. The Create Agent modal opens from a card with that template preselected and Cancel creates nothing
4. The Skills tab shows a named state that agrees with `GET /api/skills/library/status`
5. No horizontal page scroll at 390 px on any tab

## Success Criteria

- [ ] All three tabs (or two, with Systems recorded as not present) render without console errors
- [ ] Unknown `?tab=` values fall back to Agent Templates
- [ ] Agent count is unchanged; nothing was deployed, assigned or synced
- [ ] Theme restored

## Troubleshooting

- **Templates tab shows "Failed to load templates" / "Try again"**: `GET /api/templates` failed — check the backend log; the Skills tab should still work (each tab owns its own fetch).
- **"No templates configured"**: the catalog is completely empty — no non-hidden directory under `config/agent-templates/` is mounted into the backend.
- **Systems tab missing for admin right after a hard reload of `?tab=systems`**: the role arrives after first paint; the tab should appear and be selected within a second. If it stays on Agent Templates, record it.
- **Skills tab keeps a skeleton**: `GET /api/skills/library/status` has not answered — look at the network panel.
