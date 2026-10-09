# Phase 24: Credential Management

> **Purpose**: Verify the per-agent Credentials tab — the setup checklist, the credential file list, Quick Inject and the file editor — with one harmless key injected and removed.
> **Duration**: ~15 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: A key injected through the UI lands in the agent's `.env`, is counted by the status API without its value being returned, and is removed again
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

Credentials are managed **per agent**, as files in the agent's workspace. Everything lives on the agent's `Credentials` tab (`components/CredentialsPanel.vue`):

- **Credential Setup** — a checklist of what the agent's template declares (`components/CredentialSetupChecklist.vue`).
- **Credential Files** — `.env`, `.mcp.json`, `.mcp.json.template` with `View` / `Edit` / `Add`, plus `Export to Git` / `Import from Git`.
- **Quick Inject** — paste `KEY=VALUE` lines; they are merged into `.env`.
- **Upload Credential File** — for key files and certificates.

Replaces the January flow that used a global `/credentials` page with create/edit/delete and a central credential store: that page and route no longer exist, and there is no platform-wide credential list.

Two facts shape this phase. Quick Inject **rewrites `.env` as a whole** (existing keys are kept, but the file is re-serialised as `KEY="value"` lines under a header comment), so the original text is captured first and written back in Cleanup. And `.env` cannot be deleted from the UI or the API — only edited.

## Prerequisites

- [ ] Logged in at http://localhost as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `test-echo` is running (every input on this tab is disabled for a stopped agent)
- [ ] A token in `$TOKEN`:
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d "username=admin&password=$ADMIN_PASSWORD" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

---

## Test: Read the tab

### Step 1: Open the Credentials tab
**Action**:
- Open http://localhost/agents/test-echo?tab=credentials

**Expected**:
- [ ] `Credentials` is the selected tab; after a brief `Loading credentials...` four cards render, headed `Credential Setup`, `Credential Files`, `Quick Inject`, `Upload Credential File`
- [ ] Separately, open http://localhost/credentials and record where it lands — it must not render a credentials page (there is no such route)

### Step 2: Credential Setup checklist
**Action**:
- Read the `Credential Setup` card, then click its `Refresh` button

**Expected**:
- [ ] The subtitle reads `What this agent needs, what is already set, and where to get each one.`
- [ ] A headline line is shown. For this fixture (its template declares no credentials) expect `Ready — this agent needs no credentials.`; record the exact text if it is anything else
- [ ] If groups are listed, their headings are from this set, each with a count: `Required — not set`, `Optional — not set`, `Status unknown`, `Configured`, `Referenced but not declared`
- [ ] `Refresh` reads `Checking…` while the request runs and the card does not go blank

### Step 3: Credential Files list
**Action**:
- Read the `Credential Files` card

**Expected**:
- [ ] Exactly three rows: `.env`, `.mcp.json`, `.mcp.json.template`
- [ ] A row for an existing file shows `<size> · Modified <date>` with `View` and `Edit` buttons; a missing file shows `Not present` with an `Add` button
- [ ] The footer has `Export to Git` and `Import from Git` buttons and the note `.credentials.enc exists` or `No encrypted backup` (do not click either button)
- [ ] Record the `.env` row state as **ENV_STATE_0** (`present` or `Not present`)

### Step 4: Status endpoint baseline
**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/credentials/status
```
**Expected**:
- [ ] HTTP 200 with `agent_name`, a `files` object keyed by `.env`, `.mcp.json`, `.mcp.json.template`, `.credentials.enc` (each with `exists`, and `size` / `modified` when present) and an integer `credential_count`
- [ ] The response carries no credential names or values and no `env_drift` key
- [ ] Record `credential_count` as **COUNT_0**

### Step 5: Capture the original `.env`
**Action**:
- If ENV_STATE_0 is `present`: click `View` on the `.env` row. A dialog titled `.env` opens with the file in a text area. Copy the **entire** text and keep it as **ENV_TEXT_0**. Click `Cancel`.
- If ENV_STATE_0 is `Not present`: record ENV_TEXT_0 as `(absent)`.

**Expected**:
- [ ] The dialog closes on `Cancel` without a notification
- [ ] If `SWEEP_TEST_KEY` already appears in ENV_TEXT_0 (left by an earlier run), record it as a finding and continue; Cleanup removes it

---

## Test: Quick Inject

### Step 6: Input that is not a pair
**Action**:
- In `Quick Inject`, type two lines into the text area: `# just a comment` and `not a pair`
- Click `Inject`

**Expected**:
- [ ] The counter under the text area reads `0 credential(s) detected`
- [ ] A red box reads `No valid KEY=VALUE pairs found`
- [ ] The `.env` row is unchanged (nothing was written)

### Step 7: Inject one harmless pair
**Action**:
- Replace the text area content with exactly `SWEEP_TEST_KEY=sweep-not-a-secret`
- Click `Inject`

**Expected**:
- [ ] Before clicking, the counter reads `1 credential(s) detected`
- [ ] The button reads `Injecting...` briefly
- [ ] A green box reads `Injected 1 credential(s) into .env` and a `Credentials injected` notification appears
- [ ] The text area is emptied
- [ ] The `.env` row now shows a size and `Modified` with the current time

### Step 8: Status reflects it, without the value
**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/credentials/status \
  | tee /dev/stderr | grep -c -e "sweep-not-a-secret" -e "SWEEP_TEST_KEY"
```
**Expected**:
- [ ] `files[".env"].exists` is `true`
- [ ] `credential_count` is COUNT_0 + 1 (or COUNT_0 if the key was already there in Step 5)
- [ ] The `grep -c` count is `0` — neither the value nor the name `SWEEP_TEST_KEY` is in the response

### Step 9: Where the value is and is not shown
**Action**:
- Without opening any dialog, read the whole Credentials tab
- Then click `View` on the `.env` row

**Expected**:
- [ ] Outside the dialog, the text `sweep-not-a-secret` appears nowhere on the tab (the result box states a count only)
- [ ] The dialog shows the raw file: a first line `# Credential file - managed by Trinity` and a line `SWEEP_TEST_KEY="sweep-not-a-secret"` — the owner's file editor shows values in clear; record this as observed
- [ ] Every key that was in ENV_TEXT_0 is still present with the same value (the inject merged, it did not replace)
- [ ] Click `Cancel`

### Step 10: Checklist after the inject
**Action**:
- Click `Refresh` on `Credential Setup`

**Expected**:
- [ ] The headline is the same as in Step 2 (an undeclared key does not change what the template requires)
- [ ] Record whether `SWEEP_TEST_KEY` appears in any group; it is not expected to

---

## Test: Other controls (observe only)

### Step 11: Upload and editor affordances
**Action**:
- Read the `Upload Credential File` card without choosing a file
- Click `Edit` on the `.mcp.json.template` row if it exists, otherwise `Add`; read the dialog; click `Cancel`

**Expected**:
- [ ] The upload card has a file picker, a text field with the placeholder `Destination path (e.g. .config/gcloud/sa.json, client.pem, .ssh/id_ed25519)`, the note `Written 0600; created on the agent's next sync if git-tracked.` and an `Upload` button that is disabled while no file is chosen
- [ ] The editor dialog is titled with the file name and has `Cancel` and `Save` buttons
- [ ] `Cancel` writes nothing: the row's state is unchanged

### Step 12: Optional vault settings tab
**Action**:
- Open http://localhost/settings?tab=credential-vault

**Expected**:
- [ ] If a `credential-vault` Settings tab is present, record that it loads without an error; otherwise record `SKIPPED (not present)` and continue. Change nothing there

### Step 13: Tab at 390 px
**Action**:
- On http://localhost/agents/test-echo?tab=credentials, resize to 390 px wide, scroll the tab, then resize back to 1280 px

**Expected**:
- [ ] All four cards remain readable; the `View` / `Edit` buttons and the `Inject` button are reachable
- [ ] The page body does not scroll horizontally

---

## Cleanup / Restore

The original `.env` was read in Step 5 as ENV_TEXT_0.

1. On the Credentials tab click `Edit` on the `.env` row.
2. Replace the **whole** text area content:
   - if ENV_TEXT_0 is real text, with ENV_TEXT_0 exactly (minus any `SWEEP_TEST_KEY` line an earlier run left behind);
   - if ENV_TEXT_0 is `(absent)`, with the single line `# Credential file - managed by Trinity`.
3. Click `Save`. Expect the notification `Saved .env` and the dialog to close.
4. Verify:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/credentials/status
# credential_count must equal COUNT_0 (COUNT_0 − 1 if an earlier run's key was found in Step 5)
```
5. Click `View` on `.env` once more and confirm no `SWEEP_TEST_KEY` line remains; `Cancel`.

Known residue: when `.env` was `Not present` at the start it now exists as a comment-only file with zero keys. There is no supported way to delete it; note this in the report. It is inert and the phase is safe to re-run.

## Manual-only (not run unattended)

- `Export to Git` / `Import from Git` — writes or reads `.credentials.enc` and is meaningful only with a git-synced agent.
- OAuth provider flows (`POST /api/oauth/{provider}/init`) — need real provider apps.
- Uploading a real key file or certificate.
- Filling a `Required — not set` row in the checklist — needs an agent whose template declares credentials.
- Non-owner view: `POST /api/agents/{name}/credentials/inject` and the checklist are owner-only; needs a second account.
- Stopped agent: the checklist shows `The agent is stopped, so Trinity cannot tell which credentials are set. Requirements below come from the template catalog.` and inputs read `Agent must be running`.

## Critical Validations

1. Quick Inject of one pair reports `Injected 1 credential(s) into .env` and the `.env` row updates.
2. `GET /api/agents/test-echo/credentials/status` counts the new key and returns neither its name nor its value.
3. Keys already in `.env` survive the inject.
4. The value is not rendered anywhere on the tab outside the file editor.
5. After Cleanup, `credential_count` is back to its starting value and `SWEEP_TEST_KEY` is gone.

## Success Criteria

- [ ] Steps 1–13 pass, with observations recorded where asked
- [ ] `.env` restored to ENV_TEXT_0 (or the documented comment-only residue)
- [ ] No Export, Import, Upload or OAuth action was triggered
- [ ] No console errors from `/api/agents/test-echo/credentials/*` or `/credential-requirements`

## Troubleshooting

- **Text area greyed out with `Agent must be running`** — the fixture is stopped; every write on this tab needs a running container.
- **Red box starting `Couldn't read this agent's current credentials, so nothing was written`** — the UI refused to inject because it could not read the existing `.env` to merge with. Nothing was changed; retry once the agent is reachable.
- **`Agent rejected credential injection: …` or `Disallowed credential file path(s)`** — the path is outside the permitted credential file set.
- **Checklist shows a single red line instead of a headline** — `GET /api/agents/test-echo/credential-requirements` failed; that endpoint is owner-only.
- **File list shows all three as `Not present` while the agent runs** — `GET …/credentials/status` failed (it needs the agent reachable); check the network tab.
