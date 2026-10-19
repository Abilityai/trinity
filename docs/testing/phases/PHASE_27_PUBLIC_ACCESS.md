# Phase 27: Public Chat Links

> **Purpose**: Verify the owner's public-link controls (create, copy, disable, delete) and the logged-out public chat page behind a link.
> **Duration**: ~20 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: A new link opens a working public chat for a logged-out visitor, stops working when disabled, and is deleted; the seeded link is untouched
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

A public link lets someone without a Trinity account chat with one agent.

- **Owner side** — `components/PublicLinksPanel.vue`, embedded in the agent's `Sharing` tab under `Distribution → Public links`. Backed by `GET/POST /api/agents/{name}/public-links` and `PUT/DELETE /api/agents/{name}/public-links/{link_id}`. Usage counts are returned inline on the list.
- **Visitor side** — `/chat/:token` (`views/PublicChat.vue`), no login required. Backed by `GET /api/public/link/{token}`, `GET /api/public/intro/{token}`, `POST /api/public/chat/{token}`, `GET /api/public/history/{token}`.

Replaces the January flow that used a "Public Links" agent tab, `/public/{id}` URLs, per-link email settings and a usage endpoint: the panel now sits inside Sharing, the URL is `/chat/<token>`, email verification is an agent-wide access policy rather than a per-link option, and there is no separate usage endpoint.

Model-call budget: opening a public page with no history asks the agent for an introduction (one call), and this phase sends one message (one call). **Open the working public URL only as often as the steps say** — each extra fresh open can cost another introduction.

## Prerequisites

- [ ] Logged in at http://localhost as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `test-echo` is running
- [ ] fixtures.json provides the seeded public link's id and token for `test-echo` — **never toggle, edit or delete that link**
- [ ] A token in `$TOKEN`:
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d "username=admin&password=$ADMIN_PASSWORD" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

## Setup

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/public-links
```

- [ ] Record the ids and names of all existing links as **LINKS_0** (it includes the seeded link, `enabled: true`)
- [ ] If a link named `sweep-tmp-27` already exists (left by an earlier run), record it as a finding and delete it using the Cleanup steps before starting

---

## Test: Owner view

### Step 1: Find the panel
**Action**:
- Open http://localhost/agents/test-echo?tab=sharing
- Scroll to `Distribution` and click the row `Public links` (subtitle `Shareable public chat URLs`)

**Expected**:
- [ ] The row expands to a panel headed `Public Links` with the text `Generate shareable links that allow anyone to chat with this agent.` and a `Create Link` button
- [ ] The tab strip has no separate `Public Links` tab
- [ ] The seeded link is listed with the status chip `Active`, a URL of the form `http://localhost/chat/<token>` and a copy button (tooltip `Copy link`)
- [ ] Each link card has a `Slack` row with a `Connect Slack` button (do not click) and three icon buttons with the tooltips `Disable link` (or `Enable link`), `Edit link`, `Delete link`
- [ ] If the seeded card shows a stats line (`N messages`, `N users`), record it as **SEED_STATS_0**

### Step 2: Create one link
**Action**:
- Click `Create Link`
- In the dialog, type `sweep-tmp-27` into `Name (optional)`; leave `Expiration (optional)` empty
- Click the dialog's `Create Link` button

**Expected**:
- [ ] The dialog is titled `Create Public Link` and its name field has the placeholder `e.g., Customer Support Demo`
- [ ] A note in the dialog says email verification and the allow-list are controlled by the agent's `Channel Access Policy` — there is no per-link email option
- [ ] The dialog closes and a new card `sweep-tmp-27` appears with the chip `Active`
- [ ] Its URL is `http://localhost/chat/<token>` with a token different from the seeded link's
- [ ] Record the new link's full URL as **NEW_URL** and its token as **NEW_TOKEN** (read the URL text from the card)

### Step 3: Copy the link
**Action**:
- Click the copy button on the `sweep-tmp-27` card

**Expected**:
- [ ] A small notification reads `Link copied!`
- [ ] If the clipboard can be read, it equals NEW_URL (otherwise record `clipboard not readable` and continue)

**Verify** (public endpoint — no token needed):
```bash
curl -s http://localhost:8000/api/public/link/NEW_TOKEN
# Expect "valid": true, "agent_available": true, and "require_email": false for this fixture
```
- [ ] If `require_email` is `true`, record `SKIPPED (gate)` for Steps 5–7: do Step 4 only to confirm the `Verify Your Email` card appears, **never type an address**, then continue at Step 8

---

## Test: Visitor view (logged out)

### Step 4: Open the link as a logged-out visitor
**Action**:
- In the browser, clear `localStorage` and `sessionStorage` for http://localhost (this logs the admin out), then navigate to NEW_URL
- Wait up to 60 seconds for the page to settle

**Expected**:
- [ ] The page is not redirected to the login screen
- [ ] The header shows the `Trinity` wordmark and, below it, a green dot with the agent's display name (record it) and a one-line description if the agent has one
- [ ] There is no application navigation bar (no Dashboard / Library / Operations / Settings links)
- [ ] `AUTO` and/or `READ-ONLY` badges may appear next to the name — record them
- [ ] While the introduction loads the page reads `Getting ready...` / `The agent is preparing to assist you.`; it then shows either one assistant message (the fixture's reply to the introduction prompt — record its first line) or the fallback `Start a Conversation` / `Type a message below to begin chatting.`
- [ ] A message box with the placeholder `Type your message or / for playbooks...` is at the bottom

### Step 5: Send one message
**Action**:
- Type `sweep phase 27` into the message box and press Enter
- Wait up to 60 seconds

**Expected**:
- [ ] The user message appears immediately; a working indicator (`Thinking...` or `Working...`) shows while the agent runs
- [ ] An assistant reply arrives containing `Echo: sweep phase 27` (the fixture also adds `Words:` and `Characters:` lines — record them)
- [ ] No red error box appears above the input
- [ ] A `New` button is now in the page header (do not click it — it opens a native confirm and clears the conversation)

### Step 6: Reload once
**Action**:
- Reload the page **once** and wait up to 60 seconds

**Expected**:
- [ ] The page still loads the chat (no login redirect, no `Link Not Available`)
- [ ] Record exactly what is shown: (a) the `sweep phase 27` exchange restored, (b) only a fresh introduction, or (c) the empty `Start a Conversation` state. (a) is the intended behaviour; report (b) or (c) as a finding
- [ ] Do not reload again

### Step 7: Public page at 390 px
**Action**:
- Resize to 390 px wide, look at the header, messages and input, then resize back to 1280 px

**Expected**:
- [ ] Header, message bubbles and the input fit the width; the send control is reachable
- [ ] The page body does not scroll horizontally

---

## Test: Usage, disable, invalid

### Step 8: Log back in and read the usage line
**Action**:
- Open http://localhost/login, choose `Admin Login`, enter username `admin` and `ADMIN_PASSWORD` from `.env`, click `Sign In as Admin`
- Open http://localhost/agents/test-echo?tab=sharing and expand `Distribution → Public links`

**Expected**:
- [ ] The `sweep-tmp-27` card is still `Active`
- [ ] If the card shows a stats line, record it (`N messages`, `N users`, and `Last used: …` when present). After Step 5, `N messages` is expected to be at least 1 — record the value either way
- [ ] The seeded link's stats line equals SEED_STATS_0

### Step 9: Disable the new link
**Action**:
- On the `sweep-tmp-27` card click the button with the tooltip `Disable link`

**Expected**:
- [ ] The chip changes from `Active` to `Disabled` and the button's tooltip becomes `Enable link`
- [ ] The seeded link's chip is still `Active`

**Verify**:
```bash
curl -s http://localhost:8000/api/public/link/NEW_TOKEN
# Expect "valid": false and "reason": "invalid_or_expired"
```

### Step 10: Disabled link shows the unavailable state
**Action**:
- Navigate to NEW_URL

**Expected**:
- [ ] A centred card reads `Link Not Available` with the text `This link is no longer valid.`
- [ ] There is no message box and no earlier conversation on the page
- [ ] The page does not say whether the link was disabled, expired or never existed

### Step 11: An unknown token shows the same state
**Action**:
- Navigate to http://localhost/chat/sweep-not-a-real-token

**Expected**:
- [ ] The same `Link Not Available` card with `This link is no longer valid.` — indistinguishable from Step 10
- [ ] `curl -s http://localhost:8000/api/public/link/sweep-not-a-real-token` returns the same body as the Step 9 check

### Step 12: Re-enable round-trip
**Action**:
- Back in `Distribution → Public links`, click `Enable link` on `sweep-tmp-27`, then `Disable link` again

**Expected**:
- [ ] The chip goes `Disabled` → `Active` → `Disabled`, one request each, no error
- [ ] Do not open NEW_URL while it is `Active` (a fresh open would cost another introduction)

### Step 13: Edit dialog (observe, then cancel)
**Action**:
- Click `Edit link` on `sweep-tmp-27`, read the dialog, click `Cancel`

**Expected**:
- [ ] The dialog is titled `Edit Public Link`, pre-fills the name `sweep-tmp-27`, and has an extra checkbox `Link enabled` (unticked) and a `Save Changes` button
- [ ] `Cancel` closes it with no change to the card

---

## Cleanup / Restore

1. In `Distribution → Public links`, click `Delete link` on the **`sweep-tmp-27`** card (check the name first). An in-page dialog titled `Delete Link` asks `Are you sure you want to delete this link? All sessions will be invalidated.` — click `Delete`. (This is a page dialog, not a native browser confirm.)
2. The card disappears; the seeded link remains, `Active`.
3. Verify:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/public-links
# Ids and names equal LINKS_0; the seeded link still has "enabled": true
```
   (The `$TOKEN` from Prerequisites is still valid; logging out in the browser does not revoke it.)
4. You are logged in as admin again (Step 8). The browser's stored theme and view preferences were cleared in Step 4; that is expected.

## Manual-only (not run unattended)

- Email-verified flow: with the agent's policy requiring email, the visitor sees `Verify Your Email`, `Send Code`, then a 6-digit `Verify` step (`POST /api/public/verify/request`, `POST /api/public/verify/confirm`). Needs a real mailbox.
- Rate limiting on `/api/public/*` (per-IP) — needs deliberate hammering.
- Link expiry: a link with a past `Expiration` shows `Expired <date>` on the card and the same `Link Not Available` page.
- `Connect Slack` on a link — needs a Slack workspace.
- `Agent Unavailable` / `The agent is currently offline. Please try again later.` — needs the fixture stopped.
- The header `New` button and the logged-in visitor's chat-history dropdown.

## Critical Validations

1. A link created in the UI yields a `/chat/<token>` URL that loads for a logged-out visitor without redirecting to login.
2. One message through the public page returns the fixture's `Echo:` reply.
3. Disabling the link makes both the page (`Link Not Available`) and `GET /api/public/link/{token}` (`valid: false`) refuse it.
4. An unknown token is indistinguishable from a disabled one.
5. After Cleanup the link list equals LINKS_0 and the seeded link is still enabled.

## Success Criteria

- [ ] Steps 1–13 pass (or `SKIPPED (gate)` for Steps 5–7), with observations recorded where asked
- [ ] At most 3 model calls were caused (introduction, one message, and possibly one more introduction on reload)
- [ ] `sweep-tmp-27` deleted; seeded link untouched
- [ ] No email address was entered anywhere

## Troubleshooting

- **`Verify Your Email` instead of the chat** — the agent's access policy requires email; record `SKIPPED (gate)` and do not enter an address.
- **`Agent Unavailable`** — `test-echo` is not running.
- **Introduction never arrives** — `GET /api/public/intro/{token}` failed or timed out; the page falls back to `Start a Conversation` and chat still works.
- **Card URL shows only a path** — the panel resolves the backend's link against the page origin; a bare `/chat/<token>` in the card would be a regression.
- **`Create Link` dialog shows a red message** — the server refused the create; the text is the API's `detail`.
- **Owner page shows the login screen after Step 4** — expected; storage was cleared. Log in again as in Step 8.
