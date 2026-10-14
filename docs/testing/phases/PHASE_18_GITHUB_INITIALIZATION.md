# Phase 18: GitHub Initialization (observe-only)

> **Purpose**: Verify the agent Git tab, the "Initialize GitHub Sync" dialog and the read-only git endpoints — without ever submitting the dialog.
> **Duration**: ~8 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: A pass proves the Git tab renders the correct state for a fixture agent, the initialise dialog opens with the right fields and cancels cleanly, and the git status / config / PAT-status endpoints return their documented shapes.
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

Every agent page has a **Git** tab (`components/GitPanel.vue`); `composables/useGitSync.js`
enables it for all agents so that any agent can be pushed to GitHub. For an agent with
no git binding the panel shows "Git sync not enabled for this agent" and an
**Initialize GitHub Sync** button, which opens a dialog (owner, name, two checkboxes).
Submitting that dialog creates a REAL GitHub repository and pushes the agent's
workspace, so this phase opens it, inspects it and cancels. Three read-only endpoints
back the panel: `GET /api/agents/{name}/git/status`, `GET /api/agents/{name}/git/config`
and `GET /api/agents/{name}/github-pat`.

Replaces the January flow that ran a real initialisation against GitHub, read a global
`/api/credentials` list (no such endpoint) and reset state with
`DELETE …/git/config` (no such endpoint). The real flow is now Manual-only.

## Prerequisites

- [ ] Logged in as `admin` (password is `ADMIN_PASSWORD` from `.env`)
- [ ] `test-echo` and `test-counter` are running
- [ ] A Bearer token for API checks, referred to as `$TOKEN`
- [ ] **Hard rule**: never click the dialog's **Initialize** button, and never click Push, Pull, "Configure", "Change" or "Clear agent PAT" anywhere in this phase

## Test: Read-only endpoints

### Step 1: Git status shape
**Action**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/git/status
```

**Expected**:
- [ ] HTTP 200 and a JSON object with a boolean `git_enabled`
- [ ] Record the branch this fixture is in:
  - **Unbound** — `git_enabled` is `false` (a `message` such as "Git sync not enabled for this agent" may accompany it). This is the expected state for the fixtures.
  - **Bound** — `git_enabled` is `true`, with `branch`, `remote_url`, `sync_status` and, when a binding row exists, a `db_config` object (`last_sync_at`, `last_commit_sha`, `sync_enabled`, `source_mode`, `pushes`)

### Step 2: Git config shape
**Action**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/git/config
```

**Expected**:
- [ ] HTTP 200
- [ ] Unbound: `{"git_enabled": false, "message": "Git sync not configured for this agent"}`
- [ ] Bound: `git_enabled` is `true` with `github_repo`, `working_branch`, `source_branch`, `source_mode`, `instance_id`, `created_at`, `last_sync_at`, `last_commit_sha`, `sync_enabled`
- [ ] The answer agrees with Step 1 (both unbound or both bound)

### Step 3: Per-agent PAT status shape
**Action**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/github-pat
```

**Expected**:
- [ ] HTTP 200 with exactly these keys: `agent_name` (`"test-echo"`), `configured` (boolean), `source` (`"agent"` or `"global"`), `has_global` (boolean)
- [ ] `source` is `"agent"` only when `configured` is `true`
- [ ] No token value appears anywhere in the response. Record `configured` and `has_global`

### Step 4: Unknown agent is a uniform 404
**Action**:
```bash
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/agents/no-such-agent-18/git/status
```

**Expected**:
- [ ] `404`

## Test: Git tab

### Step 5: Git tab is present and deep-links
**Action**:
- Navigate to `http://localhost/agents/test-echo`
- Find **Git** in the tab strip (it may sit under the strip's "More" menu) and click it
- Then navigate directly to `http://localhost/agents/test-echo?tab=git`

**Expected**:
- [ ] A **Git** tab exists, placed immediately before **Files**
- [ ] Both the click and the direct link show the Git panel
- [ ] A brief pulsing placeholder may show first; it resolves within a few seconds

### Step 6: Panel state matches the API
**Action**:
- Read the panel

**Expected** (Unbound — expected for fixtures):
- [ ] The text "Git sync not enabled for this agent"
- [ ] The line "Push this agent to a GitHub repository to enable sync"
- [ ] A button **Initialize GitHub Sync**
- [ ] No repository link, branch pill, "Pending Changes" or "GitHub Authentication" section

**Expected** (Bound — only if Step 1 said so):
- [ ] A link showing the remote URL, a branch pill, and a status pill reading "Synced" or "Changes pending"
- [ ] A "GitHub Authentication" section reading "Agent-specific PAT" or "Using Global PAT", matching `configured` from Step 3
- [ ] Record Steps 7–11 as `SKIPPED (fixture already bound to a repository)` and continue at Step 12

### Step 7: Open the initialise dialog
**Action**:
- Click **Initialize GitHub Sync**

**Expected**:
- [ ] A dialog titled "Initialize GitHub Sync" opens over a dimmed page
- [ ] Field "Repository Owner" — empty, placeholder `your-username`, help text "Your GitHub username or organization name"
- [ ] Field "Repository Name" — empty, placeholder `my-agent`, help text "Name for the new repository"
- [ ] Checkbox "Create repository if it doesn't exist" — checked
- [ ] Checkbox "Make repository private" — checked, indented beneath it
- [ ] A blue note listing "Initialize git in the agent workspace", "Commit the current state", "Push to GitHub", "Enable bidirectional sync", plus a "Note:" about a GitHub PAT with `repo` scope and a "Timing:" line
- [ ] Buttons **Initialize** (disabled) and **Cancel**

### Step 8: Checkbox dependency
**Action**:
- Uncheck "Create repository if it doesn't exist", then check it again

**Expected**:
- [ ] Unchecking hides "Make repository private"; re-checking shows it again, still checked

### Step 9: Initialize enables only with both fields — and is NOT clicked
**Action**:
- Type `example-owner` in "Repository Owner"
- Type `sweep-observe-only` in "Repository Name"
- **Do not click Initialize.**

**Expected**:
- [ ] With only the owner filled, **Initialize** stays disabled
- [ ] With both filled, **Initialize** becomes enabled
- [ ] No red error box is shown

### Step 10: Cancel discards the form
**Action**:
- Click **Cancel**
- Click **Initialize GitHub Sync** again, read the fields, then click **Cancel** again

**Expected**:
- [ ] The dialog closes and the panel still reads "Git sync not enabled for this agent"
- [ ] On reopening, both text fields are empty again
- [ ] The network log for the whole phase contains no `POST …/git/initialize`

### Step 11: Nothing changed
**Action**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/git/status
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/git/config
```

**Expected**:
- [ ] Both still report `git_enabled: false`, identical to Steps 1–2

### Step 12: A second fixture shows the same surface
**Action**:
- Navigate to `http://localhost/agents/test-counter?tab=git`

**Expected**:
- [ ] The Git panel renders one of the two states from Step 6 with no error; record which
- [ ] Do not open the dialog here

### Step 13: Narrow viewport (390 px) and dark theme
**Action**:
- On `http://localhost/agents/test-echo?tab=git`, resize to 390 × 844
- If unbound, open the dialog, look, and **Cancel**
- Open the user menu (avatar button, top right), choose **Dark** under "Theme", open the dialog once more (if unbound), **Cancel**, then restore the previous theme and resize to ≥ 1280 px

**Expected**:
- [ ] At 390 px the panel and the dialog fit the width with no horizontal page scroll; **Initialize** and **Cancel** are reachable (stacked)
- [ ] In dark theme the dialog, its fields, the blue note and the empty-state text are readable — no white panel, no dark-on-dark text

## Cleanup / Restore

- Nothing to restore: no repository was created, no git binding or token was written, and both fixtures' git state equals what Steps 1–3 recorded.
- If the theme was changed in Step 13, set it back (record the original; default is System).
- If **Initialize** was clicked by mistake, stop and report it prominently with the owner/name typed — a repository may have been created externally and a binding written; neither can be undone from this phase.

## Manual-only (not run unattended)

- The real initialisation: submit the dialog with a real owner/name and a configured PAT, wait up to 60 s, confirm the repository on GitHub and the panel switching to the bound state.
- Push / Pull, conflict handling and "Recent Commits" on a bound agent.
- Setting, changing or clearing a per-agent PAT ("Configure GitHub PAT" dialog — the token is validated against GitHub).
- Error paths that need GitHub: missing PAT, insufficient scope, repository already exists.
- Binding an existing agent to a repository in your own account.

## Critical Validations

1. `git/status`, `git/config` and `github-pat` each return 200 with the documented keys, and no token value (Steps 1–3).
2. The Git tab exists on a fixture and its state matches the API (Steps 5–6).
3. The dialog opens with both fields, both checkboxes and a disabled **Initialize** (Step 7).
4. **Cancel** discards input and no `POST …/git/initialize` was ever sent (Steps 10–11).

## Success Criteria

- [ ] Three read-only endpoints verified; unknown agent returns 404
- [ ] Git tab reachable by click and by `?tab=git`
- [ ] Dialog fields, checkbox dependency and button enablement verified
- [ ] Dialog cancelled every time; fixture git state unchanged
- [ ] Panel and dialog usable at 390 px and readable in dark theme

## Troubleshooting

- **Panel stays on the pulsing placeholder**: `GET /api/agents/test-echo/git/status` did not return — check the request in the network log and that the token is still valid.
- **Panel shows "Agent must be running to view git status"**: the agent has a saved binding but is stopped; record `BLOCKED (fixture not running)` — do not start it from this phase.
- **Both "Git sync not enabled for this agent" and another state render together**: record it as a finding with a screenshot; the panel's states are meant to be mutually exclusive.
- **No Git tab**: record the full tab list — source shows the tab for every agent.
