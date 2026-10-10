# Phase 34: Canvas and shared canvas links

> **Purpose**: Create one canvas on a fixture agent, read it in the Canvas tab, pin it, share it at a link, open that link logged out, revoke it, and delete the canvas
> **Duration**: ~20 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: A canvas renders its blocks, the share link opens read-only without an account, a revoked or unknown link says so honestly, and `test-echo` ends with the canvases it started with
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

A canvas is a surface an agent keeps current (as opposed to a report, published once). It lives
on the agent's **Canvas** tab (`components/canvas/AgentCanvasTab.vue` → `CanvasPanel.vue`) and
can be shared at `/canvas/s/<token>` (`views/SharedCanvas.vue`), a standalone page with no
navigation bar that needs no login for a link shared with "Anyone with the link".

Agents normally write a canvas through their `set_canvas` tool, but the same write is a REST
call — `PUT /api/agents/{name}/canvas/{canvas_id}` — which an admin token may use. This phase
creates its canvas that way, so it sends **no** message to any agent.

Two controls print: the "PDF" button on the Canvas tab and "Download PDF" on the shared page
both open the browser's print dialog, which blocks an unattended browser. This phase checks
they are present and never clicks them.

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`; window about 1280 px wide
- [ ] A token for the API calls:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d "username=admin&password=$ADMIN_PASSWORD" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

## Setup

Record the canvases `test-echo` already has (Cleanup compares against this):

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/canvas \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d), [c['canvas_id'] for c in d])"
```

If `sweep-tmp-34` is already listed (a previous run died), run the delete command in Cleanup first.

## Test: Before the canvas exists

### Step 1: Canvas tab starting state
**Action**:
- Navigate to `http://localhost/agents/test-echo?tab=canvas`

**Expected**:
- [ ] The "Canvas" tab is selected
- [ ] An intro line starting "A canvas is a surface this agent keeps current" that mentions `set_canvas`
- [ ] If Setup recorded 0 canvases: a dashed box titled "No canvas yet" whose text ends "…or have it call set_canvas.", with no button inside it
- [ ] If Setup recorded 1 or more: a canvas panel is shown instead — note the title in its header

### Step 2: Create the canvas over REST
**Action**:
```bash
curl -s -X PUT http://localhost:8000/api/agents/test-echo/canvas/sweep-tmp-34 \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title":"sweep-tmp-34","blocks":[
        {"id":"notes","kind":"markdown","title":"Notes",
         "payload":{"markdown":"## Sweep heading\n\nSome **bold** text.\n\n- first item\n- second item"}},
        {"id":"grid","kind":"table","title":"Grid",
         "payload":{"columns":["Item","Status"],"rows":[["alpha","done"],["beta","open"]]}}
      ]}'
```
- Reload `http://localhost/agents/test-echo?tab=canvas`

**Expected**:
- [ ] The call returns JSON with `"canvas_id":"sweep-tmp-34"` and `"audience":"operator"`
- [ ] The tab now shows one control row: a dropdown (accessible name "Canvas") and a "Manage" button on the same line
- [ ] If this is the only canvas the dropdown is disabled and shows "sweep-tmp-34"; if there are others it is enabled — choose "sweep-tmp-34" in it

## Test: Reading the canvas

### Step 3: Blocks render
**Action**:
- Read the panel under the control row

**Expected**:
- [ ] Panel header: title "sweep-tmp-34", a "PDF" button, a "Share" button, and a line beginning "Updated " (for example "Updated just now"); do **not** click "PDF"
- [ ] A block labelled "Notes" with "Sweep heading" as a heading, the word "bold" in bold, and a two-item bullet list — no raw `##` or `**` characters visible
- [ ] A block labelled "Grid" with a table: header cells "Item" and "Status", rows "alpha / done" and "beta / open"
- [ ] No "This canvas is empty." and no "Could not load this canvas."

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/canvas/sweep-tmp-34 \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['title'], [b['kind'] for b in d['blocks']], d.get('pinned'))"
```
- [ ] `sweep-tmp-34 ['markdown', 'table'] False`

### Step 4: Manage and pin
**Action**:
- Click "Manage"
- In the row for `sweep-tmp-34`, click the pin button (tooltip "Pin to the top")
- Click it again (tooltip now "Unpin"), then click it a third time so the canvas ends pinned
- Click "Done"

**Expected**:
- [ ] "Manage" turns into "Done" and a list opens under the control row with one line per canvas: a checkbox, the title, an age (for example "just now"), a pin button and a "Delete" button
- [ ] The control row itself does not move when the list opens
- [ ] After pinning, the button shows 📌 and the dropdown entry reads "📌 sweep-tmp-34"; after unpinning it returns to 📍 and the plain title
- [ ] Ticking the checkbox shows a bar "1 selected" with "Delete selected" — untick it again without deleting
- [ ] "Done" closes the list and the same canvas is still shown

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/canvas \
  | python3 -c "import sys,json; [print(c['canvas_id'], c['pinned']) for c in json.load(sys.stdin)]"
```
- [ ] `sweep-tmp-34 True`, and it is the first row

### Step 5: Search box and the bound on the pile
**Action**:
- Count the canvases in the dropdown
- Read the per-agent limit: `canvas_max_per_agent` in `curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/settings/feature-flags`

**Expected**:
- [ ] With 6 or fewer canvases there is **no** search box in the control row; with 7 or more there is one with placeholder "Search…" — if present, type `sweep-tmp` and confirm the dropdown narrows to the match, then clear it
- [ ] The limit is a positive number (100 unless configured otherwise)
- [ ] An "N of <limit> canvases" note appears in the control row only when the count is within 10% of the limit — with a handful of canvases, expect none

### Step 6: Narrow width and themes
**Action**:
- Resize to 390 × 844 on the Canvas tab; then back to about 1280 px
- Note the theme button's tooltip in the top bar ("Light mode (click to switch)", "Dark mode (click to switch)" or "System theme (click to switch)"); click until it reads "Dark mode…", look at the canvas, then until it reads "Light mode…", and look again

**Expected**:
- [ ] At 390 px the dropdown and "Manage" stay on one row, the "Grid" table stays inside its block (it may scroll within the block), and the page has no horizontal scroll
- [ ] In both themes the heading, body text, table header and table rows are legible, and block labels are visible
- [ ] Restore the theme to the tooltip value you noted

## Test: Share at a link

### Step 7: Create a link
**Action**:
- Click "Share" in the panel header
- Select "Anyone with the link"
- Click "Create link"

**Expected**:
- [ ] A section "Share this canvas" opens with two options: "People who already have access" (selected by default) and "Anyone with the link" (carrying a "wider" tag and the text "No sign-in. Anyone you send the link to can open this canvas, and so can anyone they forward it to.")
- [ ] After "Create link" a line appears reading "Anyone with the link · 0 views" with "Copy" and "Revoke" links
- [ ] A note reads "Link copied." — or, if the browser refused clipboard access, the full link itself

**Verify** (this is how to get the link reliably):
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/agents/test-echo/canvas/shares?canvas_id=sweep-tmp-34" \
  | python3 -c "import sys,json; [print(s['id'], s['scope'], s['url']) for s in json.load(sys.stdin)]"
```
- [ ] One row with scope `public` and a `url` of the form `/canvas/s/<token>`. Record the share id and the url.

### Step 8: Open the link logged out
**Action**:
- Clear the browser's local storage and session storage for `http://localhost` (this logs you out)
- Navigate to `http://localhost` + the recorded url (`/canvas/s/<token>`)

**Expected**:
- [ ] The canvas opens with no login prompt and no redirect to `/login`
- [ ] No top navigation bar; a title "sweep-tmp-34" and a line "by <agent name> · Updated …"
- [ ] A "Download PDF" button is present — do **not** click it
- [ ] The notice "This view stays current — it shows the canvas as the agent updates it, not a copy taken when it was shared."
- [ ] The "Notes" and "Grid" blocks render exactly as in Step 3
- [ ] No "Manage", "Share", pin or "Delete" control anywhere on the page

### Step 9: An unknown token
**Action**:
- Still logged out, navigate to `http://localhost/canvas/s/not-a-real-token`

**Expected**:
- [ ] A centred card titled "This link does not work" with "It may be mistyped, or the canvas it pointed at is gone."

- [ ] `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/api/public/canvas/not-a-real-token` prints `404`

### Step 10: Revoke the link
**Action**:
- Navigate to `http://localhost/login` and log in again as `admin` ("Admin Login" → `ADMIN_PASSWORD` from `.env` → "Sign In as Admin")
- Open `http://localhost/agents/test-echo?tab=canvas`, make sure "sweep-tmp-34" is the selected canvas, click "Share"
- Click "Revoke" on the link's line and accept the browser confirm ("Revoke this link? Anyone holding it will be told it was turned off.")
- Navigate to the recorded `/canvas/s/<token>` url again

**Expected**:
- [ ] The Share section lists the link created in Step 7; record whether its line now shows a view count above 0
- [ ] After revoking, the note reads "Link revoked."; record whether the line disappears or stays marked "revoked"
- [ ] The link page now shows a card titled "This link was turned off" with "Whoever shared this canvas has revoked the link. Ask them for a new one." — and none of the canvas content
- [ ] This wording differs from Step 9 on purpose: a revoked link names itself, an unknown one does not

- [ ] `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/api/public/canvas/<token>` prints `410`

### Step 11: What a chat turn would treat as "this canvas" (observe)
**Action**:
- Run `curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/canvas/context`

**Expected**:
- [ ] JSON with `"default_canvas_id":"main"`, and — because no turn is in flight — `"source":"default"` and `"open_canvas_id":null`
- [ ] Record the values; do not send a message to test this further

### Step 12: Delete from the Manage list
**Action**:
- On `http://localhost/agents/test-echo?tab=canvas` click "Manage"
- Click "Delete" on the `sweep-tmp-34` line and accept the browser confirm ("Delete this canvas? The agent can create it again, but its current contents will be gone.")

**Expected**:
- [ ] A note "Canvas deleted" appears
- [ ] `sweep-tmp-34` is gone from the list and the dropdown
- [ ] If it was the only canvas, the tab returns to the "No canvas yet" box from Step 1

## Cleanup / Restore

Original state: the canvas list recorded in Setup (without `sweep-tmp-34`); theme as noted in Step 6.

```bash
# idempotent — safe even if Step 12 already removed it
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/agents/test-echo/canvas/sweep-tmp-34
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/canvas \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d), [c['canvas_id'] for c in d])"
```
- [ ] Count and ids equal the Setup record
- [ ] You are logged in as admin again (Step 8 logged the browser out)
- [ ] Theme button tooltip matches the value noted in Step 6

If Step 10 could not revoke through the UI, revoke over the API before deleting:
`curl -s -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/canvas/shares/<share-id>`

## Manual-only (not run unattended)

- "PDF" / "Download PDF" — both open the native print dialog; check the printed document by hand.
- A link shared with "People who already have access": opening it logged out should show "Sign in to view this canvas" with a "Sign in" button, and a signed-in account without access should see "This canvas is not shared with you" — the second needs another account.
- An agent writing or updating a canvas from a chat turn, and the canvas following the turn — fixture agents are told not to use tools.
- A canvas with audience "shared" appearing on the agent's Workspace page for other people.

## Critical Validations

1. A canvas written over REST appears in the Canvas tab and its markdown and table blocks render as formatted content, not raw text
2. The canvas picker is one row — dropdown plus "Manage" — and pinning is reflected in both the list and `GET …/canvas`
3. A public share link opens logged out, read-only, with no management controls
4. A revoked link says "This link was turned off" (410) and an unknown token says "This link does not work" (404) — neither shows canvas content
5. `sweep-tmp-34` is deleted and the agent's canvas list matches Setup

## Success Criteria

- [ ] Steps 1–12 pass
- [ ] No message was sent to any agent; neither print button was clicked
- [ ] No console errors on the Canvas tab or the shared page (a 404/410 network line on Steps 9–10 is expected)
- [ ] Cleanup checks pass

## Troubleshooting

- **PUT returns 422**: the body is malformed — `blocks[].kind` must be one of table, kpi, markdown, timeline, json, chart, html, image, diagram, and unknown top-level keys are refused.
- **PUT returns 429**: canvas writes are rate-limited per agent; wait a minute and send it once.
- **Browser hangs after a click**: a print dialog or a confirm dialog is open — dismiss it; the phase only expects confirms on "Revoke" and "Delete".
