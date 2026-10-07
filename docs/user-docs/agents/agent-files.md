# Agent Files

Two-panel file manager in the Agent Detail Files tab for browsing, previewing, and editing agent workspace files.

## How It Works

1. Open the agent detail page and click the **Files** tab.
2. The left panel displays a file tree with search and expandable directories.
3. The right panel shows a preview of the selected file.
4. Supported previews: images, video, audio, PDF, and text files.
5. Click **Edit** on a text file to modify it inline, then **Save**. Edit-protected files (`.env`, `.git`, `.gitignore`, `.trinity`, `.mcp.json.template`) have no Edit button. **Download** saves the selected file (up to 100 MB).
6. Click the new-folder icon in the tree header to create a subfolder in the selected folder, or in the workspace root when the selection is not a folder (workspace-confined; protected paths are rejected).
7. Click **Delete** to remove a file or folder after a confirmation. Protected files show *This is a protected system file and cannot be deleted.* and their Delete button is disabled.
8. Tick **Hidden** to reveal dotfiles (`.env`, `.claude/`, etc.).
9. The agent workspace root is `/home/developer/`. The agent must be running to browse, preview, edit, or delete files.

### Protected paths

The backend refuses writes, new folders, and deletes on credential and platform-managed paths, whatever the UI shows: `.env` and `.env.*`, `.mcp.json`, `.mcp.json.template`, `.credentials.enc`, `.gitignore`, `.claude/settings.json`, `.claude/settings.local.json`, and anything under `.ssh/`, `.aws/`, `.gcp/`, `.trinity/`, `.git/`, `/etc/`, `/opt/trinity/`, `/proc/` or `/sys/`. A delete is also refused for a folder that *contains* one of those directories — `.ssh`, `.claude`, or the home directory itself — since deleting the folder deletes everything in it. `CLAUDE.md` can be edited but not deleted. Refusals answer `403` (`Cannot edit protected path: …`, `Cannot delete protected path: …`). Paths are normalised before the check, so `..` segments and extra leading slashes (`//home/developer/.ssh/…`) cannot slip past it.

Writing or deleting under `.claude/skills/` is the same act as assigning a skill, so an agent's own key needs skill-management permission for it; people and the system agent are unaffected.

### Content Folder Convention

The `content/` directory is gitignored by default. Use it for large generated assets such as images, audio, and video.

### Shared Folders

Agents can expose their workspace folder for other agents to mount as a collaboration mechanism.

- Configure in the agent's **Sharing** tab using the Expose and Consume toggles.
- Permission-gated: only permitted agents can mount a shared folder.
- Relevant API endpoints: `GET/PUT /api/agents/{name}/folders`, `GET /api/agents/{name}/folders/available`, `GET /api/agents/{name}/folders/consumers`.

## Outbound File Sharing

Agents can publish files to a signed download URL that works universally — web, Slack, Telegram, WhatsApp, email — without per-channel upload handling.

### Enabling File Sharing

1. Open the agent detail page and go to the **Sharing** tab.
2. In the **File Sharing** panel, switch the toggle on (it reads **Enabled** / **Disabled**).
3. A restart-required banner appears — restart the agent to mount the publish volume.
4. After restart, the agent's `/home/developer/public/` directory is live.

### Sharing a File

**From inside the agent** (via MCP `share_file` tool):
```
share_file({ filename: "report.csv", execution_id: "<from the Execution Context block>" })
# Returns: { url, expires_at, size_bytes, mime_type, visible_to_requester, visibility_note, addressed_to }
```

The agent drops a file into `/home/developer/public/`, then calls `share_file` with the filename (optionally `display_name`, `expires_in`, `audience_email`, and `dedup_label`). Trinity extracts it, stores it securely, and returns a signed URL valid for 7 days by default (`expires_in` takes 60 seconds to 7 days). Sharing the same file to the same person twice in one turn returns the one share; pass a different `dedup_label` to mint a second one deliberately.

**Opening a link.** The link works wherever it is pasted, including in-app browsers on a phone (Telegram, iOS Safari): audio, video, images, and PDFs open inline and stream with range requests, so a voice note plays without a forced download. Anything that could carry script — HTML and SVG — is always delivered as a download. Append `?download=1` to force a download for any file. A media player's chunked reads count as one download.

**Who sees a shared file.** Two different things:

- **The link** works for anyone it is given to, until it expires or you revoke it.
- **The Workspace Files tab** lists a file for one person only — the person the conversation was with. Trinity works that out from the turn the file was shared in; the agent does not choose. A file shared during Ada's chat appears in Ada's Files tab and in nobody else's.
  - A file shared from a schedule, an operator chat, or an agent-to-agent call has no person behind it, so it is listed for the agent's **owner** only.
  - A file sent over WhatsApp is addressed to that number, and reaches a Workspace Files tab only if that number verified an email with the agent.
  - An agent can address a file to a *different* person it is shared with by passing `audience_email`. An address the agent is not shared with is refused (`AUDIENCE_NOT_ON_ROSTER`) — it is never widened silently.
  - If Trinity cannot tell which conversation a share came from, it does not guess: the file is listed for the owner only, and the tool result says so (`visible_to_requester: false`, with a `visibility_note` explaining how to fix it). Trinity normally supplies the turn's execution id to `share_file` automatically; passing `execution_id` yourself is the fallback for agents on an older base image.
- Files shared before this behaviour existed are listed for the owner only; every share expires within seven days.

**From the UI:**
- Active shared files appear in the File Sharing panel with filename, **who the file is for**, size, expiry, and download count. "Owner only" carries a second line saying why — no person in that turn, or Trinity could not tell which conversation it came from.
- Click **Copy URL** to get the link.
- Click **Revoke** to invalidate a link immediately (returns `410 Gone` on download).

### Limits

| | |
|---|---|
| Max file size | 50 MB per file |
| Per-agent storage quota | 500 MB across all active shares |
| Default expiry | 7 days |
| Blocked types | Executables (PE/ELF/Mach-O), scripts with shebangs |

The signed URL is the only credential a download needs: it is not tied to a chat session or a verified email, so a recipient outside Trinity can open it. Revoke the link if it reaches the wrong hands.

## For Agents

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/files` | GET | List workspace files (tree structure) |
| `/api/agents/{name}/files` | PUT | Save a text file (what the inline editor calls); protected paths are refused with `403` |
| `/api/agents/{name}/files` | DELETE | Delete a file or folder. Protected paths, and folders that hold a protected directory, are refused with `403` |
| `/api/agents/{name}/files/download` | GET | Download file content (100 MB limit) |
| `/api/agents/{name}/files/preview` | GET | File content with its real MIME type, for previews |
| `/api/agents/{name}/files/mkdir` | POST | Create a directory in the workspace |
| `/api/agents/{name}/file-sharing` | GET | File sharing status and quota |
| `/api/agents/{name}/file-sharing` | PUT | Enable or disable file sharing (owner/admin) |
| `/api/agents/{name}/shared-files` | POST | Mint a download URL for a file in `/home/developer/public/` |
| `/api/agents/{name}/shared-files` | GET | List active shared files. A signed-in person sees who each file is for; an agent or other key-authenticated caller gets the rows with `addressed_to`, `addressed_to_channel` and `audience_source` withheld, so it cannot tell who a file was for or whether it was addressed at all |
| `/api/agents/{name}/shared-files/{id}` | DELETE | Revoke a shared file |
| `/api/files/{file_id}` | GET/HEAD | Public download — query param `?sig={token}`; supports `Range` requests; `?download=1` forces a download |

## See Also

- [Creating Agents](creating-agents.md)
- [Managing Agents](managing-agents.md)
- [Workspace](../sharing-and-access/workspace.md) — the Workspace has its own Files tab for files you and the agent exchange in a conversation
