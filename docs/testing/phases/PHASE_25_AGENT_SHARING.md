# Phase 25: Agent Access & Sharing

> **Purpose**: Verify granting and revoking operator access on the Access tab, the login-whitelist side effect, and that the Sharing tab's external-client sections render.
> **Duration**: ~15 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: An operator row can be added, is rejected as a duplicate, and is removed; the whitelist and access policy end exactly as they started
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

Two owner-only tabs on `/agents/:name` cover who can reach an agent:

- **Access** (`components/AccessPanel.vue`) — Trinity operators (platform users) granted access by email. Backed by `POST /api/agents/{name}/share`, `DELETE /api/agents/{name}/share/{email}`, `GET /api/agents/{name}/shares` and `GET /api/agents/{name}/access`.
- **Sharing** (`components/SharingPanel.vue`) — external clients: the Restricted / Open policy, public chat model, channel connections, the client roster, and Distribution (public links, file sharing).

Replaces the January flow in which the Sharing tab held a "share with a teammate" form: that form moved to the Access tab, and Sharing is now about clients outside the platform.

Granting access by email also adds that email to the login whitelist when email login is enabled (`Settings → Access → Email Whitelist`), so this phase reads the whitelist first and restores it. The access **policy** on `test-echo` is set by the seed script and relied on by other phases — this phase reads it and must not click it.

## Prerequisites

- [ ] Logged in at http://localhost as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `test-echo` exists (running or not; these tabs do not need the container)
- [ ] A token in `$TOKEN`:
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d "username=admin&password=$ADMIN_PASSWORD" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

## Setup

Record the starting state — Cleanup restores exactly this.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/access
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/shares
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/settings/email-whitelist
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/access-policy
```

- [ ] Record the list of emails from `/access` as **ACCESS_0**
- [ ] Record the number of entries from `/shares` as **SHARES_0**
- [ ] Record the list of emails under `whitelist` as **WL_0**, and specifically whether `user@example.com` is in it
- [ ] Record `require_email` and `open_access` as **POLICY_0**
- [ ] If `user@example.com` is already in ACCESS_0 (left by an earlier run), record it as a finding, and in Step 2 click `Remove` on that row before continuing

---

## Test: Access tab

### Step 1: Open the tab
**Action**:
- Open http://localhost/agents/test-echo?tab=access

**Expected**:
- [ ] `Access` is the selected tab, heading `Access`, with intro text describing Trinity operators and pointing to the `Sharing` tab for external clients
- [ ] A form with an email field (placeholder `operator@company.com`) and a button `Add operator`, disabled while the field is empty
- [ ] Below it either a roster list or the empty state `No operators yet. Add a Trinity user by email above.`
- [ ] The roster matches ACCESS_0 (same emails, same count)

### Step 2: Add an operator
**Action**:
- Type `user@example.com` in the email field and click `Add operator`

**Expected**:
- [ ] The button reads `Adding…` briefly
- [ ] A green message reads `Added user@example.com.` and the field is emptied
- [ ] A new roster row shows `user@example.com`, a status chip `Pending` (or `Active` if such an account exists — record which), the line `Invited — no account yet` for a pending invite, a `Proactive` switch and a `Remove` link
- [ ] Under the list a note begins `Proactive lets the agent message a user without being prompted`

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/shares
# The array contains an entry for user@example.com
```

### Step 3: Duplicate is refused
**Action**:
- Type `user@example.com` again and click `Add operator`

**Expected**:
- [ ] A red message reads `Agent is already shared with user@example.com`
- [ ] The roster still has exactly one `user@example.com` row

### Step 4: Malformed address
**Action**:
- Type `not-an-email` and click `Add operator`

**Expected**:
- [ ] No row is added. Record what stopped it: the browser's own email-field validation bubble (no request sent), or a red message from the server
- [ ] Clear the field afterwards

### Step 5: The share is visible elsewhere
**Action**:
- Reload http://localhost/agents/test-echo (Overview tab) and read the `Footprint` card
- Open http://localhost/settings?tab=access and find the `Email Whitelist` section

**Expected**:
- [ ] `Footprint` shows `N shares` with N equal to SHARES_0 + 1
- [ ] The whitelist table has columns `Email`, `Source`, `Added`
- [ ] If email login is enabled on this instance, `user@example.com` is now listed with the source chip `🤝 Auto (Agent Sharing)` (when it was not in WL_0). If it is not listed, record `not auto-added (email login off)` — both are valid
- [ ] The tip under the table reads `💡 Tip: When you share an agent with someone by email, they're automatically added to this whitelist.`

### Step 6: Remove the operator
**Action**:
- Back on http://localhost/agents/test-echo?tab=access, click `Remove` on the `user@example.com` row

**Expected**:
- [ ] The link reads `Removing…` briefly and the row disappears with no confirmation dialog
- [ ] The roster equals ACCESS_0 again (the empty state returns if it was empty)

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/shares
# No entry for user@example.com
```

### Step 7: Removing access does not remove the whitelist entry
**Action**:
- Reload http://localhost/settings?tab=access

**Expected**:
- [ ] Record whether `user@example.com` is still in the whitelist. If Step 5 saw it added, it is expected to remain (revoking access does not touch the whitelist) — Cleanup removes it

---

## Test: Sharing tab (observe only)

**Do not click `Restricted` or `Open` in this section, even the one already selected** — a click can rewrite the policy the seeded public link depends on, and the UI offers no way to put the email-gate value back.

### Step 8: Policy control and framing
**Action**:
- Open http://localhost/agents/test-echo?tab=sharing

**Expected**:
- [ ] Heading `Share this agent`, with text pointing teammates to the `Access` tab
- [ ] Sub-heading `Who can chat with this agent?` with a two-button group `🔒 Restricted` / `🌐 Open`; exactly one is shown as selected
- [ ] The helper line under it starts `Restricted — only people you approve can chat.` or `Open — anyone with a verified email can chat without approval.` matching the selected button, and matching `open_access` in POLICY_0
- [ ] Record whether the amber `Heads up:` notice is shown, and whether a `Pending requests (N)` list is shown

### Step 9: Public chat model and instructions
**Action**:
- Read the next two blocks without changing them

**Expected**:
- [ ] `Public chat model` with a select whose first option starts `Use platform default`; record the selected option
- [ ] `Additional instructions — public & channel chats only` with a text area, a character counter in the form `N / MAX`, and `Clear` / `Save` buttons; `Save` is disabled while nothing was edited

### Step 10: Channels and client roster
**Action**:
- Read the `Channels` block and the `Client roster` block

**Expected**:
- [ ] `Channels` lists rows `Slack`, `Telegram`, `WhatsApp`, each with a status (`Not connected` unless configured) and a `Configure` or `Manage` button (do not click)
- [ ] A `Voice calls` row may or may not be present — record which
- [ ] A collapsed row `MCP connector` with the subtitle `Add this agent to an AI client; playbooks become tools`
- [ ] `Client roster — who's reaching this agent` shows either a table (`Client`, `Channel`, `Verified email`, `Messages`, `Last active`) or the empty state `No external clients yet`

### Step 11: Distribution
**Action**:
- In `Distribution`, click the `Public links` row to expand it, read it, click the row again to collapse it
- Do the same for `File sharing`

**Expected**:
- [ ] `Distribution` has two collapsed rows: `Public links` (`Shareable public chat URLs`) and `File sharing` (`Outbound shared files`)
- [ ] Expanded, `Public links` shows a `Public Links` heading, a `Create Link` button and at least the seeded link (do not create, toggle or delete anything)
- [ ] Each row collapses again on a second click

### Step 12: Policy untouched; Permissions tab loads
**Action**:
- Click the `Permissions` tab

**Expected**:
- [ ] The tab shows the heading `Agent Collaboration Permissions` and finishes loading (no endless `Loading permissions...`); change nothing

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/access-policy
# require_email and open_access equal POLICY_0
```

### Step 13: Both tabs at 390 px
**Action**:
- Resize to 390 px wide; view `?tab=access`, then `?tab=sharing`; resize back to 1280 px

**Expected**:
- [ ] Access: the email field and `Add operator` stay usable; roster rows do not overflow the card
- [ ] Sharing: the policy buttons, channel rows and Distribution rows fit; a wide client-roster table scrolls inside its own box
- [ ] The page body does not scroll horizontally on either tab

---

## Cleanup / Restore

1. **Access roster** — `GET /api/agents/test-echo/access` must list exactly ACCESS_0. If `user@example.com` is still there, click `Remove` on its row on the Access tab.
2. **Whitelist** — only if `user@example.com` was **not** in WL_0 and is in the whitelist now: on http://localhost/settings?tab=access click `Remove` on its row. A native browser confirm reads `Remove user@example.com from whitelist?` — accept it. Do not remove any other entry.
3. **Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/settings/email-whitelist
# The email list equals WL_0
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/access-policy
# Equals POLICY_0
```
4. If the policy differs from POLICY_0 (it should not), restore it through the API, substituting the recorded values:
```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"require_email": false, "open_access": true}' \
  http://localhost:8000/api/agents/test-echo/access-policy
```

## Manual-only (not run unattended)

- Self-share: adding the logged-in user's own email is refused with `Cannot share an agent with yourself` (400). Needs the admin account's real address.
- The shared user's view: the agent appears for them with a `Shared by <owner>` badge and no Access / Sharing / Settings tabs. Needs a second logged-in account.
- Flipping `Restricted` / `Open`, approving or denying a pending request, and the `Proactive` switch taking effect — need an external client and change the fixture's policy.
- Connecting Slack, Telegram, WhatsApp or voice — need external accounts.

## Critical Validations

1. Adding `user@example.com` creates one roster row and one entry in `GET …/shares`.
2. A second add of the same email is refused with `Agent is already shared with user@example.com`.
3. `Remove` deletes the row and the `GET …/shares` entry.
4. The whitelist ends equal to WL_0 and the access policy equal to POLICY_0.
5. The Sharing tab renders all of its sections without a console error.

## Success Criteria

- [ ] Steps 1–13 pass, with observations recorded where asked
- [ ] No `user@example.com` left in the agent's access list or (unless it was there before) the whitelist
- [ ] Neither policy button was clicked; no channel, link or instruction was changed
- [ ] No console errors from `/api/agents/test-echo/access`, `/shares`, `/access-policy`, `/clients`

## Troubleshooting

- **No Access / Sharing / Permissions tabs** — they render only for a viewer who owns or administers the agent, and never for the system agent.
- **`Couldn't load access list.` with a `Retry` link** — `GET /api/agents/test-echo/access` failed; click `Retry` once and report if it persists.
- **`user@example.com` never reaches the whitelist** — the auto-add only runs when email login is enabled on the instance.
- **Whitelist `Remove` does nothing** — the native confirm dialog was dismissed; it must be accepted.
- **`Failed to add operator.`** — the server returned an error without a detail; check the response of `POST /api/agents/test-echo/share`.
