# Phase 09: File Browser (Files tab)

> **Purpose**: Verify the agent Files tab — tree, search, hidden-file toggle, preview pane, download, protected-file guard and folder create/delete — and the three read endpoints behind it.
> **Duration**: ~12 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: The Files tab lists and previews a running agent's workspace honestly in every state, and leaves the workspace exactly as it found it
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

Every agent has a **Files** tab (`components/FilesPanel.vue`) — it is unconditional, including for the system agent. It is a two-pane browser over `/home/developer`: a tree on the left, a preview pane on the right. Paths shown in the UI and passed to the API are relative to `/home/developer`.

Controls that exist: `Refresh`, new-folder, a `Hidden` checkbox, a `Search files...` box, and in the preview pane `Edit` (text files), `Download` and `Delete`. There is **no upload control and no right-click menu**.

This phase uses `test-counter`, whose `counter.txt` is a known small text file. Replaces the January flow that used a `test-files` agent (it does not exist) and created files through chat.

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `test-counter` shows `Running`
- [ ] `$TOKEN` holds an admin Bearer token

This phase sends **at most 1 task** (only if the state file is missing) and creates one empty folder, `sweep-tmp-09`, which it deletes in Cleanup. It never edits or deletes a file it did not create.

## Setup

### Step 1: Make sure the state file exists
**Action**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/agents/test-counter/files?path=/home/developer&show_hidden=false" \
  | python3 -c "import sys,json; t=json.load(sys.stdin)['tree']; print([i['name'] for i in t])"
```
- If `counter.txt` is in the list, send nothing.
- If it is not, open `http://localhost/agents/test-counter?tab=tasks`, type `reset` in the task textarea, click `Run`, and wait up to 60 s for the row to read `success`.

**Expected**:
- [ ] HTTP 200 and a `tree` array
- [ ] `counter.txt` exists at the top level before Step 2 starts
- [ ] Record whether the task was needed, and the top-level names returned

---

## Test: Tree and controls

### Step 2: Open the Files tab
**Action**:
- Navigate to `http://localhost/agents/test-counter?tab=files`

**Expected**:
- [ ] Left pane, top row: a button titled `Refresh`, a button titled `New folder in workspace root`, and a checkbox labelled `Hidden`
- [ ] Below it an input with placeholder `Search files...`
- [ ] The tree lists the same top-level names as Step 1, folders before files
- [ ] A footer line reading `N files`, optionally followed by `•` and a total size
- [ ] Right pane shows `No File Selected` and `Select a file from the tree to preview`
- [ ] Record the current state of the `Hidden` checkbox as `HIDDEN_ORIGINAL` (it is remembered per browser)

### Step 3: Search
**Action**:
- Type `counter` in `Search files...`
- Replace it with `zzz-no-such-file`
- Clear the box

**Expected**:
- [ ] `counter`: the tree narrows to entries whose name contains `counter`; `counter.txt` is listed and highlighted
- [ ] `zzz-no-such-file`: the tree area reads `No matching files found`
- [ ] Cleared: the full tree returns

### Step 4: Hidden files
**Action**:
- Tick `Hidden` (or, if `HIDDEN_ORIGINAL` was ticked, untick it), observe, then return it to `HIDDEN_ORIGINAL`

**Expected**:
- [ ] The tree reloads on each change
- [ ] With `Hidden` ticked, entries whose names start with `.` appear; unticked, none do
- [ ] The `N files` footer changes accordingly — record both counts

---

## Test: Preview pane

### Step 5: Preview a text file
**Action**:
- Click `counter.txt`

**Expected**:
- [ ] The row is highlighted and the preview shows the file body as monospace text (a single number)
- [ ] Under the preview: the name `counter.txt`, then a line with its path, a size and a relative time (`just now`, `5m ago`, …)
- [ ] Buttons `Edit`, `Download`, `Delete` are shown; `Delete` is enabled for this file (do not click it)

### Step 6: Edit mode opens and cancels without writing
**Action**:
- Click `Edit`, change nothing, click `Cancel`

**Expected**:
- [ ] In edit mode the body becomes a textarea and the buttons become `Save` (disabled — nothing changed) and `Cancel`
- [ ] `Cancel` returns to the read-only preview with no dialog and the same content

### Step 7: Download
**Action**:
- Click `Download`

**Expected**:
- [ ] The browser starts a download named `counter.txt`
- [ ] A toast reads `Downloaded counter.txt`
- [ ] No context menu is involved; right-clicking a tree row shows only the browser's own menu

### Step 8: Protected file
**Action**:
- If `CLAUDE.md` is in the tree, click it. If it is not, record `SKIPPED (CLAUDE.md not in tree)`.

**Expected**:
- [ ] The markdown is previewed as text
- [ ] `Delete` is disabled and its title is `Protected file cannot be deleted`
- [ ] The line `This is a protected system file and cannot be deleted.` is shown under the buttons
- [ ] `Edit` is still offered (do not click it)

### Step 9: A file type with no preview
**Action**:
- Tick `Hidden` and look for a file whose extension is not text, image, audio, video or PDF (for example a `.db`, `.bin`, `.gz` or `.lock` file). Click it. Return `Hidden` to `HIDDEN_ORIGINAL`.
- If no such file exists, record `SKIPPED (no binary file present)`.

**Expected**:
- [ ] The preview reads `Preview not available for this file type` followed by the MIME type (or `Unknown type`)
- [ ] `Edit` is not offered; `Download` is

---

## Test: Folders

### Step 10: Create an empty folder
**Action**:
- Click an empty area or a top-level file so that no folder is selected, then click the new-folder button (title `New folder in workspace root`)
- In the dialog type `sweep-tmp-09` and click `Create`

**Expected**:
- [ ] Dialog heading `New Folder`, the line `Create in /home/developer`, an input with placeholder `folder-name`, the hint `Use / to create nested folders.`, and `Create` disabled until a name is typed
- [ ] Toast `Created sweep-tmp-09`; the dialog closes and `sweep-tmp-09` appears in the tree

### Step 11: Empty-folder states
**Action**:
- Click `sweep-tmp-09` in the tree

**Expected**:
- [ ] The folder expands and shows the italic line `Empty folder` beneath it
- [ ] The preview pane shows the folder name and `0 items`
- [ ] The new-folder button's title is now `New folder in sweep-tmp-09`
- [ ] `Edit` is not offered for a folder

---

## Test: Read endpoints

### Step 12: List, preview, download
**Action**:
```bash
B=http://localhost:8000/api/agents/test-counter
# list (path and show_hidden are query params; path defaults to /home/developer)
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" "$B/files"
# preview and download take the file path as the ?path= query param
curl -s -H "Authorization: Bearer $TOKEN" --get --data-urlencode "path=counter.txt" "$B/files/preview"
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" --get --data-urlencode "path=counter.txt" "$B/files/download"
```

**Expected**:
- [ ] List: `200`
- [ ] Preview: the body is the same number shown in Step 5
- [ ] Download: `200`

### Step 13: Honest failures
**Action**:
```bash
B=http://localhost:8000/api/agents/test-counter
curl -s -w "\n%{http_code}\n" -H "Authorization: Bearer $TOKEN" --get --data-urlencode "path=zzz-no-such-file.txt" "$B/files/preview"
curl -s -w "\n%{http_code}\n" -H "Authorization: Bearer $TOKEN" --get --data-urlencode "path=/etc/passwd" "$B/files/preview"
curl -s -o /dev/null -w "%{http_code}\n" "$B/files"
```

**Expected**:
- [ ] Missing file: `404`
- [ ] Path outside the workspace: `403` with a detail beginning `Access denied`; no file content is returned
- [ ] No token: `401`

### Step 14: 390 px and dark theme
**Action**:
- With `counter.txt` selected, resize to 390 × 844; then restore 1280 × 800
- Note the title of the theme button in the top bar (`Light mode (click to switch)`, `Dark mode (click to switch)` or `System theme (click to switch)`), click it until dark is active, look at the tree and preview, then keep clicking until the original title is back

**Expected**:
- [ ] At 390 px record what happens to the two panes (the tree has a 280 px minimum width) and whether the page scrolls horizontally
- [ ] In dark theme the selected tree row, the preview text and the three action buttons are all legible
- [ ] The theme button's title equals the one noted at the start

---

## Cleanup / Restore

Original state: no `sweep-tmp-09` folder; `Hidden` checkbox as `HIDDEN_ORIGINAL`; theme as noted in Step 14.

- In the tree click `sweep-tmp-09`, click `Delete`. The dialog is titled `Delete Folder` and says `This will delete all 0 files inside.` and `This action cannot be undone.` Click the dialog's `Delete`. Expect the toast `Deleted sweep-tmp-09` and the folder gone from the tree.
- If the UI delete failed, remove it by API (this is the only write call in the phase):

```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" --get \
  --data-urlencode "path=sweep-tmp-09" \
  http://localhost:8000/api/agents/test-counter/files
```

- Confirm `sweep-tmp-09` is absent and `counter.txt` is still present by re-running the Step 1 command.
- Set `Hidden` back to `HIDDEN_ORIGINAL`.

## Manual-only (not run unattended)

- **Stopped agent**: the tab shows `Agent must be running to browse files` and the list endpoint returns 400 with the same sentence. Seeing it requires stopping a fixture; Phase 04 covers it during its single restart.
- **Edit and save** a file (`Save` → toast `Saved <name>`) and the unsaved-changes prompts — these modify a fixture's file.
- **Large files**: preview and download refuse files over 100 MB with a 413; needs a large file on disk.
- **Image / audio / video / PDF previews**: need media files in the workspace.

## Critical Validations

1. The tree matches what `GET /api/agents/test-counter/files` returns, and `Hidden` controls whether dot-entries appear.
2. Selecting a text file previews its real content; the preview endpoint returns the same bytes.
3. A protected file cannot be deleted from the UI (button disabled with the stated title).
4. A path outside `/home/developer` is refused with 403.
5. The workspace after Cleanup equals the workspace before Step 10.

## Success Criteria

- [ ] Search, hidden toggle and both empty messages (`No matching files found`, `Empty folder`) behave as stated
- [ ] Text preview, edit-cancel and download work on `counter.txt` without changing it
- [ ] Protected-file guard observed (or explicitly skipped because `CLAUDE.md` is absent)
- [ ] Folder created and deleted; nothing else changed
- [ ] All three read endpoints return 200; the three failure cases return 404 / 403 / 401

## Troubleshooting

- **Tree area shows red text and a `Failed to load files: …` toast**: the list call failed. `Agent server not ready. The agent may still be starting up.` means the container was just started — wait 10 s and click `Refresh`.
- **`This folder is empty` at the top level**: the workspace has no non-hidden entries; tick `Hidden` to confirm the listing works at all.
- **Preview pane shows red text**: it is the preview endpoint's error detail, shown verbatim — record it.
- **`sweep-tmp-09` already exists at Step 10**: an earlier run did not clean up. Use it as-is for Step 11 and delete it in Cleanup.
- **`Invalid folder name` toast**: the name contained a `.` or `..` path segment.
