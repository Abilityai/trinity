# Manual Test Plan — #302 Settings Tabbed Layout

> **Goal**: Verify every behavior the PR introduces or modifies, plus regression-check the 13 sections that were rearranged.
>
> **Time budget**: ~25 minutes for the full flow; ~10 minutes for the smoke path (Sections A, B, C, D.4 only).
>
> **PR**: [#700](https://github.com/abilityai/trinity/pull/700) — `feature/302-settings-tabbed-layout`
>
> **Companion doc**: [`docs/planning/302-settings-test-list.md`](../planning/302-settings-test-list.md) (Canon TDD behavior list — what the automated tests cover)

---

## Setup

```bash
# Make sure stack is up and on the new code
docker compose ps                          # all services up
curl -sf http://localhost/                 # frontend reachable
curl -sf http://localhost:8000/health      # backend reachable

# Branch must be checked out + frontend dev server running.
# Vite hot-reloads the new files automatically.
git checkout feature/302-settings-tabbed-layout
```

Open **http://localhost** in a browser. Login as **admin**.

DevTools — open with `Cmd+Opt+I` (Mac) / `F12`. We'll use Console + Application tabs in Sections E and F.

---

## Section A — Admin tab strip & navigation (5 min)

### A.1 Settings page loads with tab strip

- [ ] Click the **Settings** link in the NavBar (or navigate to `http://localhost/settings`)
- [ ] **Expected**: page shows **5 tabs** in a horizontal strip near the top:
  - General · Access · Integrations · MCP Keys · Agents
- [ ] **Expected**: **General** tab is highlighted (indigo underline + indigo text)
- [ ] **Expected**: only General-tab content is rendered below the strip — i.e., **Platform**, **Trinity Prompt**, **Default Avatars** sections are visible; nothing else.
- [ ] **Expected URL**: still `/settings` (no `?tab=`)

### A.2 Click each tab in order

For each tab in this order — **General → Access → Integrations → MCP Keys → Agents** → back to General:

- [ ] Click the tab
- [ ] **Expected**: URL updates to `/settings?tab=<id>` where id is `general` / `access` / `integrations` / `mcp-keys` / `agents`
- [ ] **Expected**: tab gets indigo highlight; previous tab loses it
- [ ] **Expected**: page content swaps — only the new tab's sections are visible
- [ ] **Expected**: NO full-page reload (the page transition is instant; you can verify by setting a JS variable in DevTools console: `window._mark = 'A'`, click a tab, then check `window._mark` — it should still be `'A'`)

### A.3 Browser back/forward

- [ ] After clicking through several tabs, hit the browser **Back** button
- [ ] **Expected**: previous tab becomes active (URL `?tab=` reflects it, content updates)
- [ ] Hit **Forward**
- [ ] **Expected**: returns to the later tab

---

## Section B — Deep links (3 min)

Open each URL directly in a new browser tab (paste in URL bar + enter):

| URL | Expected |
|---|---|
| `http://localhost/settings` | General tab active, no `?tab=` in URL |
| `http://localhost/settings?tab=general` | General active |
| `http://localhost/settings?tab=access` | Access active |
| `http://localhost/settings?tab=integrations` | Integrations active |
| `http://localhost/settings?tab=mcp-keys` | MCP Keys active |
| `http://localhost/settings?tab=agents` | Agents active |
| `http://localhost/settings?tab=foobar` | **Falls back to General** (default) — no error, no crash |
| `http://localhost/settings?tab=` | Falls back to General |

- [ ] All 8 cases behave as listed
- [ ] No console errors in DevTools

---

## Section C — NavBar + `/api-keys` redirect (2 min)

### C.1 NavBar "Keys" link removed

- [ ] Look at the top NavBar
- [ ] **Expected**: NO **Keys** link between **Ops** and **Settings**
- [ ] **Expected**: Settings link is still visible

### C.2 `/api-keys` redirect

- [ ] Paste `http://localhost/api-keys` in the URL bar, press Enter
- [ ] **Expected**: URL changes to `http://localhost/settings?tab=mcp-keys`
- [ ] **Expected**: MCP Keys tab is selected and its content is rendered (key list, "Create API Key" button, MCP Configuration snippet)

### C.3 Direct old-URL bookmark behavior

- [ ] Bookmark `http://localhost/api-keys` (or remember the URL was bookmarked previously)
- [ ] Open the bookmark
- [ ] **Expected**: lands on `/settings?tab=mcp-keys` — no error page, no broken link

---

## Section D — Tab-content regression (the big one) (10 min)

This is the most important section: **does every existing feature still work?** Each tab below has multiple sections; verify a key control in each.

### D.1 General tab

- [ ] Click General tab
- [ ] **Platform** section: input "Public Chat URL" — enter `http://test.local`, click Save → success toast or no error. (Don't forget to revert to original.)
- [ ] **Trinity Prompt** section: scroll down, see textarea with current prompt. Make a tiny edit, click Save, see success indicator. (Revert.)
- [ ] **Default Avatars** section: see "Generate Default Avatars" button. (Don't click — it's expensive.)

### D.2 Access tab

- [ ] Click Access tab
- [ ] **Email Whitelist** section: see list of allowed emails. Try adding a fake email like `test-302-cleanup@example.com`, click Add → row appears. Then click X to remove → row disappears.
- [ ] **User Management** section: see list of users with role dropdowns. Verify your admin user is shown with role `admin`. (Don't change.)
- [ ] **SSH Access** section: see toggle for "Enable SSH Access". (Don't toggle.)

### D.3 Integrations tab

- [ ] Click Integrations tab
- [ ] **API Keys** section: see Anthropic key status (configured or not), GitHub PAT status. Don't modify.
- [ ] **Slack Integration** section: see OAuth status, App Token field, Connect button.
- [ ] **Claude Subscriptions** section: see list of subscriptions (you should see `ps`). Each row should be expandable.

### D.4 MCP Keys tab

- [ ] Click MCP Keys tab
- [ ] **Expected**: header **MCP API Keys** with description
- [ ] **Expected**: blue info banner showing `.mcp.json` configuration template with the URL filled in
- [ ] **Expected**: list of existing API keys (or empty state with "No API keys")
- [ ] **MCP Server URL** section (admin-only): see the configurable URL setting below the keys list. Don't modify.

#### D.4.1 Full CRUD flow

- [ ] Click **Create API Key** button (top right)
- [ ] **Expected**: modal opens with "Create MCP API Key" title, Name + Description fields
- [ ] Type name `test-key-302`, leave description blank
- [ ] Click **Create**
- [ ] **Expected**: success modal shows the new key + MCP Configuration JSON
- [ ] Click **Copy Config** → button shows "Copied!" briefly
- [ ] Click eye icon next to "API Key Only" → key is revealed (starts `trinity_mcp_...`)
- [ ] Click **I've copied the configuration**
- [ ] **Expected**: modal closes; new key appears in the list with status "Active"
- [ ] Click **Revoke** on `test-key-302` → confirm dialog → confirm
- [ ] **Expected**: status changes to "Revoked", "Revoke" button is gone, "Delete" button still there
- [ ] Click **Delete** → confirm
- [ ] **Expected**: key disappears from list

### D.5 Agents tab

- [ ] Click Agents tab
- [ ] **GitHub Templates** section: see list of configured template repos (likely 4-5 entries)
- [ ] **Agent Quotas** section: see role-based quota inputs (admin/creator/operator/user)
- [ ] **Skills Library** section: see Skills Library URL field

---

## Section E — Non-admin role testing (5 min)

We don't have a non-admin user in the local DB, so we'll **simulate** one via DevTools by editing the localStorage role.

### E.1 Switch to non-admin via DevTools

- [ ] Make sure you're on `http://localhost/settings` (any tab)
- [ ] Open DevTools → **Application** tab → **Local Storage** → `http://localhost`
- [ ] Find the key `auth0_user` — click it, see JSON like `{"sub":"local|admin","email":"admin@localhost","name":"admin","email_verified":true,"role":"admin"}`
- [ ] **Edit the value**: change `"role":"admin"` to `"role":"user"`
- [ ] **Refresh the page** (Cmd+R / F5)

### E.2 Non-admin behavior

- [ ] **Expected**: tab strip now shows **only "MCP Keys"** — General, Access, Integrations, Agents are gone
- [ ] **Expected**: MCP Keys tab is selected (it's the only option)
- [ ] **Expected**: MCP Keys content renders normally — list of keys, Create button, etc.
- [ ] **Expected**: URL is `/settings` (or `/settings?tab=mcp-keys`) — you're NOT bounced to `/`
- [ ] **Expected**: NavBar still shows the Settings link (it's now visible to all users, not just admin)

### E.3 Try to access an admin-only tab

- [ ] Navigate directly to `http://localhost/settings?tab=integrations`
- [ ] **Expected**: URL falls back to `/settings?tab=mcp-keys` (or similar valid default) because `integrations` isn't in the user's `validTabIds`. No crash, no error.

### E.4 Backend security boundary verification

This is the key test: the UI hides admin tabs, but **does the backend actually reject a non-admin who tries the API directly?** It should — `require_admin` is unchanged on every backend endpoint.

- [ ] Open DevTools → Console
- [ ] Run:
  ```js
  fetch('/api/settings/api-keys', {
    headers: { Authorization: 'Bearer ' + localStorage.getItem('token') }
  }).then(r => r.status)
  ```
- [ ] **Expected**: `200` — the **token** is still admin's (we only mocked the `role` field client-side). This proves the security model: client-side `role` editing is cosmetic; the JWT is the real auth.

> **The honest demo of the security model**: even though the UI is showing the user as "non-admin", the backend trusts the JWT (which is still an admin token). To genuinely test as non-admin, you'd need a real non-admin user (different JWT). The UI hiding is convenience — backend `require_admin` against the JWT-mapped user record is the actual gate.

### E.5 Restore admin

- [ ] DevTools → Application → Local Storage → `auth0_user`
- [ ] Edit `"role":"user"` back to `"role":"admin"`
- [ ] Refresh
- [ ] **Expected**: all 5 tabs return

---

## Section F — Auth flow regression (3 min)

### F.1 Logout + re-login

- [ ] Click the user avatar / logout (top right)
- [ ] **Expected**: redirect to login page
- [ ] Login again as admin
- [ ] **Expected**: lands on dashboard
- [ ] Navigate to `/settings`
- [ ] **Expected**: all 5 tabs visible (admin role correctly fetched from `/api/users/me` after login)

### F.2 Verify `/api/users/me` is being called

- [ ] DevTools → **Network** tab → filter by `users/me`
- [ ] Reload the page
- [ ] **Expected**: at least one `GET /api/users/me` request, status 200
- [ ] Click the request, check the **Response** tab
- [ ] **Expected**: response JSON includes `"role": "admin"`

This confirms the new `fetchUserProfile()` action is firing and the role getter has data to read.

### F.3 Session restore (refresh)

- [ ] On `/settings`, hit Cmd+R / F5 (full reload, not soft navigation)
- [ ] **Expected**: tab strip still shows 5 tabs (admin role survives via localStorage `auth0_user`)
- [ ] **Expected**: in Network tab, `/api/users/me` is fetched again (fresh role on every load — defensive against stale localStorage data)

---

## Section G — Edge cases (2 min)

### G.1 No double history entry on re-click

- [ ] Click MCP Keys tab → URL is `?tab=mcp-keys`
- [ ] Click MCP Keys tab AGAIN
- [ ] **Expected**: URL still `?tab=mcp-keys`, no new history entry. Test by hitting browser Back — should go to whatever was before MCP Keys, not "to the same tab".

### G.2 Modal state on tab switch (known minor UX issue)

- [ ] On MCP Keys tab, click **Create API Key** → modal opens
- [ ] Without closing the modal, click the **Agents** tab
- [ ] **Expected**: modal closes (component unmounts because of `v-if`)
- [ ] Click MCP Keys again
- [ ] **Expected**: modal does NOT re-open (state is lost)

This is the documented minor UX issue (`v-if` vs `v-show` tradeoff). Confirm it's just minor, not surprising.

### G.3 Concurrent operations

- [ ] On Integrations tab, click into the Anthropic key field, type something, then quickly click another tab BEFORE submitting
- [ ] Click back to Integrations
- [ ] **Expected**: section re-mounts with original state (typed text is gone — same `v-if` behavior). Acceptable but worth noticing.

---

## Pass/fail criteria

| Section | If any step fails | Action |
|---|---|---|
| A — tab strip & nav | block merge | re-open PR for fix |
| B — deep links | block merge | re-open PR |
| C — NavBar + redirect | block merge | re-open PR |
| D — tab content regression | **block merge** | re-open PR — this is the regression-coverage section |
| E — non-admin | block merge | re-open PR |
| F — auth flow | block merge | re-open PR |
| G — edge cases | log as known issue, don't block | the `v-if` behaviors are documented as deferred |

---

## What to NOT test

These are intentionally out of scope (per the deferred-items list in the PR description):
- Modal state preservation across tabs (G.2 is informational only)
- Component-extraction completeness (4 of 5 tabs still inline — flagged for follow-up)
- Real non-admin user (we don't have one in DB; localStorage spoof is sufficient for UI verification)
- Vitest unit tests (frontend has only Playwright e2e)

---

## Bonus: re-run the automated suite

If you want to confirm what was tested matches what you're testing:

```bash
cd src/frontend
ADMIN_PASSWORD=$(grep -E '^ADMIN_PASSWORD' /Users/pash/projects/trinity/.env | cut -d'=' -f2-) \
  npx playwright test e2e/settings-tabs.spec.js --reporter=line
```

Should report **14 passed in ~5–7s.**

---

**Last Tested**: _(fill in YYYY-MM-DD when you run this)_
**Tested By**: _(your name)_
**Status**: _(✅ All passed / ⚠️ Issues found — list / ❌ Blocking issues)_
