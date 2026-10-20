# Phase 13: Settings — General & Access

> **Purpose**: Verify the Settings page shell and its General and Access tabs, including a save/restore round trip of the Trinity Prompt and an add/remove round trip of the Email Whitelist.
> **Duration**: ~12 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: A pass proves Settings tabs deep-link and switch, the Trinity Prompt saves and persists, and the whitelist accepts and removes an address — with both restored to their original state.
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

Settings lives at `/settings` (`views/Settings.vue`) and is split into tabs selected by
`?tab=`: General, Access, Integrations, MCP Keys, Agents, Retention, plus a few that only
appear on some instances. An admin lands on **General**. The **Trinity Prompt** card is
on the General tab; the **Email Whitelist** card is on the Access tab, alongside
"User Management" and "SSH Access".

Replaces the January flow that treated Settings as one long page, toggled a per-agent
"Allow Agent API Key" control in a Terminal tab (that tab no longer exists) and queried
an audit service on port 8001 (gone). This phase covers the `general` and `access`
tabs only; the other tabs are covered by Phase 35.

**This phase changes two global values** — the Trinity Prompt (read by every agent on
every turn) and the login whitelist. Both are restored in Cleanup; do not skip it.

## Prerequisites

- [ ] Logged in as `admin` (password is `ADMIN_PASSWORD` from `.env`)
- [ ] A Bearer token for API checks, referred to as `$TOKEN`
- [ ] Do not send any message or task to an agent during this phase

## Setup

Record the original values before touching anything.

```bash
# Trinity Prompt — 200 with {"key","value","updated_at"} when set, 404 when never set
curl -s -w '\n%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/settings/trinity_prompt

# Whitelist — {"whitelist": [ {email, source, added_at, ...}, ... ]}
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/settings/email-whitelist
```
- Record `ORIGINAL_PROMPT` = the exact `value` string, or `UNSET` if the call returned 404. Save it to a file so it can be restored byte-for-byte.
- Record `ORIGINAL_WHITELIST` = the list of emails, and whether `user@example.com` is already in it (`PRESENT` / `ABSENT`).

## Test: Page shell and tabs

### Step 1: Settings opens on General
**Action**:
- Click **Settings** in the top navigation

**Expected**:
- [ ] URL is `http://localhost/settings`; heading "Settings" with the line "System-wide configuration for the Trinity platform"
- [ ] A tab strip is shown and **General** is the selected tab
- [ ] The strip contains at least General, Access, Integrations, MCP Keys, Agents, Retention (some may sit under a "More" menu at the right; record any additional tabs without opening them)

### Step 2: Tabs switch and deep-link
**Action**:
- Click the **Access** tab
- Use the browser Back button
- Navigate to `http://localhost/settings?tab=access`
- Navigate to `http://localhost/settings?tab=does-not-exist`

**Expected**:
- [ ] Clicking Access changes the URL to `/settings?tab=access` and shows the "Email Whitelist" card
- [ ] Back returns to the General tab content (the "Trinity Prompt" card is visible again)
- [ ] The direct link opens straight on Access
- [ ] The unknown tab id falls back to General — no blank page

### Step 3: General tab contents
**Action**:
- Navigate to `http://localhost/settings?tab=general` and scroll the whole tab

**Expected**:
- [ ] Cards headed "Admin sign-in email", "Platform", "Trinity Prompt", "Build Info" and "Default Avatars" are present (other cards may appear above them — record their headings)
- [ ] A blue "How it works" box lists five bullet points about the Trinity Prompt and links to a docs guide
- [ ] The "Email Whitelist" card is NOT on this tab
- [ ] Do not change anything in "Admin sign-in email", "Platform" or "Default Avatars"

## Test: Trinity Prompt (General tab)

### Step 4: Card state matches the stored value
**Action**:
- Look at the "Trinity Prompt" card

**Expected**:
- [ ] Subtitle begins "Custom instructions added to every agent's instructions on each chat and task turn."
- [ ] A textarea labelled "Custom Instructions" holds exactly `ORIGINAL_PROMPT` (empty, showing the placeholder "Enter custom instructions for all agents...", when `UNSET`)
- [ ] The counter reads "<N> characters" where N is the length of the text
- [ ] **Save Changes** is disabled; **Clear** is disabled when the textarea is empty
- [ ] No "Unsaved changes" label is shown

### Step 5: Editing marks the form dirty
**Action**:
- Click into the textarea, move the caret to the very end, press Enter and type `ui-sweep-13-marker`

**Expected**:
- [ ] "Unsaved changes" appears next to the character counter and the counter increases
- [ ] **Save Changes** becomes enabled

### Step 6: Save the change
**Action**:
- Click **Save Changes**

**Expected**:
- [ ] The button briefly reads "Saving..."
- [ ] A green banner "Settings saved successfully!" appears at the bottom of the page and disappears after about 3 seconds
- [ ] "Unsaved changes" disappears and **Save Changes** is disabled again
- [ ] No red "Error" box is shown

**Verify** (API):
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/settings/trinity_prompt \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['value'].endswith('ui-sweep-13-marker'))"
# True
```

### Step 7: The saved value survives a reload
**Action**:
- Reload `http://localhost/settings?tab=general`

**Expected**:
- [ ] The textarea ends with `ui-sweep-13-marker`; **Save Changes** is disabled; no "Unsaved changes"

### Step 8: Clear (only when the prompt was originally unset)
**Action**:
- If `ORIGINAL_PROMPT` is `UNSET`: click **Clear**
- Otherwise: record `SKIPPED (prompt had an original value — restored in Cleanup instead)` and go to Step 9

**Expected**:
- [ ] The textarea empties, "0 characters" is shown and the "Settings saved successfully!" banner appears
- [ ] `GET /api/settings/trinity_prompt` now returns 404 (the setting is deleted, not stored empty)

## Test: Email Whitelist (Access tab)

### Step 9: Access tab contents
**Action**:
- Navigate to `http://localhost/settings?tab=access`

**Expected**:
- [ ] Cards headed "Email Whitelist", "User Management" and "SSH Access" are present
- [ ] The whitelist card has an email input with placeholder `user@example.com`, an **Add Email** button (disabled while the input is empty) and a table with columns Email, Source, Added
- [ ] The table rows match `ORIGINAL_WHITELIST`, or the single line "No whitelisted emails. Add one above to get started." if it is empty
- [ ] Each row shows a source pill — "✋ Manual" or "🤝 Auto (Agent Sharing)" — and a **Remove** button
- [ ] The tip line beginning "💡 Tip: When you share an agent with someone by email" is shown
- [ ] Record whether the blue "How it works" box about the Trinity Prompt is also shown at the bottom of this tab (it is not tab-scoped in source)
- [ ] Do not change any role in "User Management" and do not touch the "SSH Access" toggle

### Step 10: Add an address
**Action**:
- If `user@example.com` was `PRESENT` in Setup: record `SKIPPED (address already whitelisted)` for Steps 10 and 12, run Step 11 only, and leave the row alone
- Otherwise type `user@example.com` in the input and click **Add Email**

**Expected**:
- [ ] The input clears and a row `user@example.com` appears with the "✋ Manual" pill and a date in "Added"
- [ ] The green "Settings saved successfully!" banner appears briefly

**Verify** (API):
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/settings/email-whitelist \
  | python3 -c "import sys,json; print([e['email'] for e in json.load(sys.stdin)['whitelist']])"
# contains user@example.com exactly once
```

### Step 11: A duplicate is refused
**Action**:
- Type `user@example.com` again and click **Add Email**

**Expected**:
- [ ] A red "Error" box appears reading "Email user@example.com is already whitelisted"
- [ ] The table still has exactly one `user@example.com` row

### Step 12: Remove the address
**Action**:
- Click **Remove** on the `user@example.com` row — ONLY that row
- A native browser confirm dialog "Remove user@example.com from whitelist?" appears: accept it (arm the dialog handler to accept before clicking)

**Expected**:
- [ ] The row disappears; the other rows are unchanged
- [ ] The green banner appears briefly
- [ ] The API list from Step 10 no longer contains `user@example.com`

### Step 13: Narrow viewport (390 px) and dark theme
**Action**:
- On `http://localhost/settings?tab=access`, resize to 390 × 844
- Open the user menu (avatar button, top right), choose **Dark** under "Theme", look at both the Access and General tabs, then restore the previous theme
- Resize back to ≥ 1280 px

**Expected**:
- [ ] At 390 px the tabs that do not fit the strip move into a "More" menu, from which they can still be opened
- [ ] The page itself does not scroll horizontally (the whitelist table may scroll inside its own card); **Add Email** and the Trinity Prompt buttons are reachable
- [ ] In dark theme the cards, table, pills and the blue "How it works" box are readable — no white cards or dark-on-dark text

## Cleanup / Restore

Restore both values, then prove it.

1. **Trinity Prompt** — original read in Setup as `ORIGINAL_PROMPT`.
   - If `UNSET`: Step 8 already cleared it. If Step 8 did not run or failed, open the General tab and click **Clear**, or run `curl -s -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/settings/trinity_prompt`.
   - Otherwise write the saved original back exactly:
     ```bash
     python3 -c "import json,sys; print(json.dumps({'value': open('original_prompt.txt').read()}))" > body.json
     curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
       --data @body.json http://localhost:8000/api/settings/trinity_prompt
     ```
   - Confirm: `GET /api/settings/trinity_prompt` returns the same status and `value` as in Setup, and contains no `ui-sweep-13-marker`.
2. **Email Whitelist** — original read in Setup as `ORIGINAL_WHITELIST`.
   - If `user@example.com` was `ABSENT` in Setup and is still listed, remove it: `curl -s -X DELETE -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/settings/email-whitelist/user%40example.com"`.
   - If it was `PRESENT` in Setup, it must still be there.
   - Confirm: the whitelist email set equals `ORIGINAL_WHITELIST`.
3. **Theme** — set back to the value it had before Step 13.
4. Delete the temporary `original_prompt.txt` / `body.json` files.

## Manual-only (not run unattended)

- A non-admin user sees only the MCP Keys tab and gets "Access denied. Admin privileges required." paths (needs a second account).
- Confirming a whitelisted address can actually log in by email code (needs a mailbox) — see Phase 17.
- Confirming the Trinity Prompt text reaches an agent's instructions on its next turn (costs a model call and is covered by agent-level phases).
- Changing user roles, the admin sign-in email, the SSH Access toggle, or generating default avatars.

## Critical Validations

1. Settings opens on General for admin and `?tab=access` deep-links (Steps 1–2).
2. The Trinity Prompt card is on General and the Email Whitelist card is on Access — not the other way round (Steps 3, 9).
3. A prompt edit saves, shows the success banner and survives reload (Steps 6–7).
4. `user@example.com` can be added once, a duplicate is refused by name, and it can be removed (Steps 10–12).
5. After Cleanup the prompt and the whitelist equal what Setup recorded.

## Success Criteria

- [ ] Tab strip works by click, Back button and deep link; unknown tab falls back to General
- [ ] Trinity Prompt round trip completed and restored
- [ ] Whitelist round trip completed and restored
- [ ] No other setting was modified
- [ ] Both tabs usable at 390 px and readable in dark theme

## Troubleshooting

- **Settings opens on MCP Keys with only that tab**: the signed-in user is not an admin; sign in as `admin`.
- **Pulsing placeholder that never resolves, or a red "Error" box with "Access denied. Admin privileges required."**: the settings load failed — check the token is still valid (backend restarts invalidate sessions).
- **Step 12 does nothing**: the native confirm dialog was dismissed rather than accepted; the row is only removed on accept.
- **Remove reports "Email user@example.com not found in whitelist"**: it was already removed (for example by Cleanup run twice); re-read the list and continue.
- **Save reports a 4xx in the "Error" box**: record the text verbatim; the prompt was not changed, so only the whitelist needs checking in Cleanup.
