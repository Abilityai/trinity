# Phase 35: Settings: MCP Keys, Agents, Retention, Integrations

> **Purpose**: Walk the Settings tab bar and the four tabs Phase 13 does not cover — MCP Keys (one throwaway key, created and deleted), Agents, Retention and Integrations (all three observe-only).
> **Duration**: ~20 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: tab deep links, the key create → one-time reveal → revoke → delete lifecycle, and the read-only state of the other three tabs are proven against the API
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

`/settings` (`views/Settings.vue`) is one page with a tab strip. The active tab is mirrored in
the URL as `?tab=<id>`. The ids every admin has are `general`, `access`, `integrations`,
`mcp-keys`, `agents`, `retention`; a non-admin has only `mcp-keys`. `/api-keys` is a legacy
route that redirects to `/settings?tab=mcp-keys`.

Phase 13 owns the `general` and `access` tabs — this phase only passes through them.

**Two hard rules for this phase**
1. **Never transcribe a key value.** The one-time reveal dialog prints the full key in clear
   text inside the "MCP Configuration" block. Do not copy it into the report, a screenshot
   caption, or a shell command. Refer to keys by name and by the masked prefix shown in the list.
2. **Never lower a retention value: these are data-deletion floors.** A lower number
   hard-deletes history on the next cleanup cycle. The Retention tab is read-only here. So are
   Agents and Integrations — type nothing into them and press no Save / Remove / Reset button.

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`; viewport 1440 × 900 unless a step says otherwise
- [ ] `$TOKEN` holds an admin session token (`POST http://localhost:8000/api/token`, form-encoded)

## Setup

```bash
# Key inventory before the run — record the count, and confirm no leftover throwaway
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/mcp/keys \
  | python3 -c "import sys,json; k=json.load(sys.stdin); print(len(k), [x['id'] for x in k if x['name']=='sweep-tmp-35'])"
```

Record the count as `KEYS_BEFORE`. If the id list is not empty, a previous run leaked a key: delete each with `curl -s -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/mcp/keys/<id>` and re-read the count.

## Test: Tab bar and deep links
### Step 1: Default tab and the tab strip
**Action**:
- Navigate to `http://localhost/settings`

**Expected**:
- [ ] Heading "Settings" with the line "System-wide configuration for the Trinity platform"
- [ ] The tab strip contains at least: "General", "Access", "Integrations", "MCP Keys", "Agents", "Retention"
- [ ] "General" is the active tab (coloured underline) and the "Platform" section heading is visible
- [ ] Browser tab title is `Trinity — Settings`

### Step 2: Deep links, unknown value, reload
**Action**:
- Navigate in turn to `/settings?tab=integrations`, `/settings?tab=mcp-keys`, `/settings?tab=agents`, `/settings?tab=retention`
- On `/settings?tab=retention`, reload the page
- Navigate to `/settings?tab=foobar`

**Expected**:
- [ ] `integrations` shows the "API Keys" and "Slack Integration" headings
- [ ] `mcp-keys` shows the "MCP API Keys" heading
- [ ] `agents` shows the "GitHub Templates" and "Agent Quotas" headings
- [ ] `retention` shows the "Data Retention" heading, and still does after the reload (URL keeps `?tab=retention`)
- [ ] `?tab=foobar` does not error: the page renders with "General" active and the "Platform" heading visible

### Step 3: Clicking tabs, history, and the `/api-keys` redirect
**Action**:
- From `/settings`, click "Integrations", then "MCP Keys"
- Click "MCP Keys" a second time
- Press the browser Back button once, then Forward once
- Navigate to `http://localhost/api-keys` (legacy route)

**Expected**:
- [ ] Each click changes the URL to `?tab=integrations`, then `?tab=mcp-keys`, with no full page reload
- [ ] Back lands on `?tab=integrations` (the repeat click added no history entry); Forward returns to `?tab=mcp-keys`
- [ ] `/api-keys` ends at `/settings?tab=mcp-keys` with the "MCP API Keys" heading visible

## Test: MCP Keys
### Step 4: The tab at rest
**Action**:
- Stay on `/settings?tab=mcp-keys`
- If a dialog titled "Your MCP API Key is Ready!" opens by itself (the page mints a default key for a user who has none), click "I've copied the configuration" and note that it happened — do not transcribe anything from it

**Expected**:
- [ ] "Create API Key" button top right
- [ ] Info box "Connect to MCP Server" with a `.mcp.json` snippet whose Authorization value is the literal placeholder `Bearer YOUR_API_KEY`
- [ ] A search field with placeholder "Search by name, prefix or agent" and a badge reading "N active"
- [ ] Each key row shows: name, an "Active" or "Revoked" pill, a short prefix followed by `...` (never a full key), "Created …", "Last used …", "N requests", and a "Delete" button ("Revoke" too when active)
- [ ] Further down: a "Personal GitHub Token" panel and an "MCP Server URL" section showing either an "Auto-detect" or a "Custom" pill — observe only

### Step 5: Create the throwaway key
**Action**:
- Click "Create API Key"
- In the dialog "Create MCP API Key", confirm the "Create" button is disabled while "Name" is empty
- Type `sweep-tmp-35` into "Name" (placeholder "My Claude Code Key"); leave "Description (optional)" empty; leave "Key scope" on "Standard"
- Click "Create"

**Expected**:
- [ ] "Key scope" offers three radio options: "Standard", "Ops (read-only)", "Portal delegate"
- [ ] The create dialog closes and "Your MCP API Key is Ready!" opens
- [ ] A warning reads "Copy the configuration below before closing - the key won't be shown again!"

### Step 6: One-time reveal and copy controls
**Action**:
- Without transcribing the value, look at the "MCP Configuration" block and the "API Key Only" field
- Click "Copy Config"
- Click the eye button (title "Show/hide key") once, then once more
- Click the button titled "Copy key"
- Click "I've copied the configuration"

**Expected**:
- [ ] "API Key Only" is masked by default; the eye button reveals it and hides it again
- [ ] "Copy Config" changes to "Copied!" for about 2 seconds (if a browser alert "Failed to copy config…" appears instead, the clipboard is blocked in this browser — record it, accept the alert, continue)
- [ ] "Copy key" swaps its icon to a check mark for about 2 seconds
- [ ] The dialog closes; there is no way to reopen it and no control in the list that shows the key again
- [ ] **The report contains the key name only — no key value**

### Step 7: The key is listed, masked
**Action**:
- Type `sweep-tmp-35` into the search field

**Expected**:
- [ ] Exactly one row: `sweep-tmp-35`, pill "Active", prefix chip ending in `...`, "Last used never", "0 requests"
- [ ] Typing `zzz-no-such-key` instead shows "No keys match “zzz-no-such-key”" with a "Clear search" button; clicking it restores the list

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/mcp/keys \
  | python3 -c "import sys,json; [print(k['id'], k['name'], k['is_active'], k['key_prefix']) for k in json.load(sys.stdin) if k['name']=='sweep-tmp-35']"
```
- [ ] One line, `True`; record the id as `KEY_ID`. The response carries a prefix only, no full key

## Test: Agents tab (observe only)
### Step 8: What the tab lists
**Action**:
- Click "Agents"

**Expected**:
- [ ] "GitHub Templates": inputs "owner/repo" and "Display name (optional)", an "Add" button, a table with columns "Repository" and "Display Name", and "Reset to Defaults" / "Save Templates" buttons. With no templates the table reads "No GitHub templates configured. Trinity ships no defaults — add an owner/repo above to publish it to the Library."
- [ ] "Template registry" panel renders
- [ ] "Agent Quotas": "Admin" shows "Unlimited"; "Creator", "Operator" and "User" each have a number input; a "Save Quotas" button. Record the three numbers
- [ ] "Skills Library" and "Skill managers" panels render, followed by an "Automation" block with the checkboxes "Scheduled auto-sync" and "Re-inject across the fleet after a sync" and a "Save Automation" button
- [ ] Nothing was typed or saved

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/settings/agent-quotas
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/settings/github-templates
```
- [ ] The quota numbers and the template rows match the page

## Test: Retention tab (observe only)
### Step 9: Windows and the approval panel
**Action**:
- Click "Retention". **Do not edit any field.**

**Expected**:
- [ ] Card "Data Retention" with the line "How long Trinity keeps logs, executions, health checks, metric points, and soft-deleted agents/schedules." and a small pill to the right of the heading (record its text)
- [ ] Either the grey note starting "✓ No deletions are awaiting approval." or an amber block "⚠ Deletion awaiting your approval" with "Approve deletion" buttons — record which. **Never click "Approve deletion".**
- [ ] Number fields labelled "Log archival", "Execution logs", "Execution rows", "Health checks", "Soft-deleted agents", "Soft-deleted schedules", each with the unit "days" — record all six values
- [ ] "Audit log" field, always disabled, with "days (365-day integrity floor)"
- [ ] Record whether the six fields are editable or disabled, and whether a "Save retention" button is present. If present it is disabled (nothing was changed)
- [ ] If rows "Metric points" and "Metric point quota" are not present on this instance, record `SKIPPED (not present)` and continue. If present: record both values and whether either carries an "env" badge (an "env" row is disabled)
- [ ] Below the card: "Workspace sessions" and "Room budgets" panels render — observe only

**Verify** (the page has no backup section today; backup status comes from the same endpoint and is checked by API only):
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/settings/retention \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps({'windows':d['windows'],'pending':len(d['pending_acknowledgements']),'backup':d['backup']}, indent=1))"
```
- [ ] Every value in `windows` equals the field shown on the page
- [ ] `pending` is 0 exactly when the grey "No deletions are awaiting approval" note was shown
- [ ] `backup` is an object with `enabled`, `schedule_utc`, `scope` equal to `"same-disk"`, `retention_days`, `min_keep`, `last_status`, `last_success_at`, `stale` and an `artifacts` block (`count`, `total_bytes`, `newest`). Record `last_status`, `stale` and `artifacts.count`. `{"error": "unavailable"}` is a FAIL

## Test: Integrations tab (configure nothing)
### Step 10: Each card states configured or not, with secrets masked
**Action**:
- Click "Integrations". **Do not type into any field; do not press Test, Save, Remove, Connect or Install.**

**Expected**:
- [ ] "API Keys" card, "Anthropic API Key": status line is "Configured (from settings)", "Configured (from environment)" or "Not configured - required for agents". When configured, the field's placeholder is a masked value, never a whole key. "Test" and "Save" are disabled while the field is empty
- [ ] "GitHub Personal Access Token (PAT)": "Configured (…)" or "Optional - required for GitHub repository initialization"; same masking and disabled buttons
- [ ] "Email provider (Resend)" and "Gemini" blocks each show one status line — a configured line, or one starting "Not set."
- [ ] "Slack Integration" card: top-right status reads "Socket Mode", "Webhook" or "Disconnected". "Client Secret", "Signing Secret" and "App Token" are password fields; a configured one shows "✓ Configured (…)" under it. "Save Credentials" is disabled while the three fields are empty. "Setup Instructions" expands and collapses
- [ ] "Claude Subscriptions" panel renders
- [ ] No full secret is readable anywhere on the tab

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/settings/api-keys
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/settings/slack/status
```
- [ ] `anthropic.configured` / `github.configured` agree with the two status lines, and every `masked` value is masked
- [ ] The Slack status agrees with the top-right label

## Test: Narrow width and console
### Step 11: 390 px tab-bar overflow
**Action**:
- Resize to 390 × 844 and open `/settings?tab=retention`
- Click the "More" trigger at the right end of the tab strip
- Choose "MCP Keys" from the menu, then resize back to 1440 × 900

**Expected**:
- [ ] The strip shows only the tabs that fit plus a "More" trigger; nothing is clipped mid-label
- [ ] With "Retention" hidden in the menu, the "More" trigger carries the active styling and a small dot
- [ ] The menu lists every tab that is not inline; choosing "MCP Keys" switches the content and the URL to `?tab=mcp-keys`
- [ ] The page body does not scroll sideways (`document.documentElement.scrollWidth <= window.innerWidth`). The `.mcp.json` code block may scroll inside its own box — that is intended
- [ ] At 1440 px all tabs are inline again or the "More" menu is shorter

### Step 12: Console is clean across every tab
**Action**:
- Clear the console, then click every tab in the strip once — the six named in Step 1 and any others

**Expected**:
- [ ] No uncaught errors and no Vue warnings on the six standard tabs. A single quiet 403/404 for a feature this instance does not have is acceptable — record the URL
- [ ] Any additional tab in the bar loads without console errors; do not describe its contents. If there are none, record `SKIPPED (not present)` and continue

## Cleanup / Restore

The only thing this phase created is the key `sweep-tmp-35` (read in Setup as absent).

1. On `/settings?tab=mcp-keys`, find the `sweep-tmp-35` row and click "Revoke". An in-page dialog "Revoke API Key" opens (this is not a native `confirm()`); click its "Revoke" button.
   - [ ] The pill changes to "Revoked", the "Revoke" button disappears and the row leaves the default list
   - [ ] A "Show revoked (N)" toggle is present; switch it on to see the row again
2. Click "Delete" on the row, then "Delete" in the "Delete API Key" dialog.
   - [ ] The row is gone
3. Verify:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/mcp/keys \
  | python3 -c "import sys,json; k=json.load(sys.stdin); print(len(k), [x['id'] for x in k if x['name']=='sweep-tmp-35'])"
```
   - [ ] The id list is empty. The count equals `KEYS_BEFORE` (or `KEYS_BEFORE + 1` if Step 4 reported an auto-minted default key — leave that one in place)
4. If the UI delete failed: `curl -s -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/mcp/keys/$KEY_ID`
5. Viewport back to 1440 × 900. Nothing on Agents, Retention or Integrations was changed, so nothing else to restore.

## Manual-only (not run unattended)

- Non-admin view (only the "MCP Keys" tab, no "MCP Server URL" section) — needs a second account.
- Saving, testing or removing a provider key, Slack credential or subscription — needs real third-party credentials.
- Editing a retention window or approving a pending deletion — destructive and irreversible.

## Critical Validations

1. `?tab=` deep links, reload and the unknown-value fallback all land on the right tab.
2. The full key is shown exactly once; the list and the API only ever expose a prefix.
3. `sweep-tmp-35` is created, revoked and deleted, and the API confirms it is gone.
4. Retention values on the page equal `GET /api/settings/retention`; nothing was edited; no secret is readable on Integrations.

## Success Criteria

- [ ] All six standard tabs render; deep links, history and the `/api-keys` redirect behave
- [ ] Key lifecycle completed and cleaned up; no key value in the report; the other three tabs observed with zero writes
- [ ] 390 px strip collapses into "More" with no sideways page scroll; console clean

## Troubleshooting

- **A "Your MCP API Key is Ready!" dialog opens on arrival**: the tab calls `POST /api/mcp/keys/ensure-default` on mount and reveals a newly minted default key. Close it; it is not the throwaway.
- **`sweep-tmp-35` vanished after revoke**: revoked keys are hidden by default — use "Show revoked (N)".
- **`GET /api/mcp/keys` returns 403**: the key inventory accepts a signed-in session token only, not an MCP key. Re-issue `$TOKEN` from `/api/token`.
