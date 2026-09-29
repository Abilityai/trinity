# Agent Files

Two-panel file manager in the Agent Detail Files tab for browsing, previewing, and editing agent workspace files.

## How It Works

1. Open the agent detail page and click the **Files** tab.
2. The left panel displays a file tree with search and expandable directories.
3. The right panel shows a preview of the selected file.
4. Supported previews: images, video, audio, PDF, and text files.
5. Click the edit button on any text file to modify and save it inline.
6. Click **New folder** on any directory to create a subfolder in place (workspace-confined; edit-protected paths are rejected).
7. Delete files directly from the file manager. Protected path warnings appear for critical files.
8. Toggle **Show hidden files** to reveal dotfiles (`.env`, `.claude/`, etc.).
9. The agent workspace root is `/home/developer/`.

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

The agent drops a file into `/home/developer/public/`, then calls `share_file` with the filename (optionally `display_name`, `expires_in`, and `audience_email`). Trinity extracts it, stores it securely, and returns a signed URL valid for 7 days.

**Opening a link.** The link works wherever it is pasted, including in-app browsers on a phone (Telegram, iOS Safari): audio, video, images, and PDFs open inline and stream with range requests, so a voice note plays without a forced download. Anything that could carry script — HTML and SVG — is always delivered as a download. Append `?download=1` to force a download for any file. A media player's chunked reads count as one download.

**Who sees a shared file.** Two different things:

- **The link** works for anyone it is given to, until it expires or you revoke it.
- **The Workspace Files tab** lists a file for one person only — the person the conversation was with. Trinity works that out from the turn the file was shared in; the agent does not choose. A file shared during Ada's chat appears in Ada's Files tab and in nobody else's.
  - A file shared from a schedule, an operator chat, or an agent-to-agent call has no person behind it, so it is listed for the agent's **owner** only.
  - A file sent over WhatsApp is addressed to that number, and reaches a Workspace Files tab only if that number verified an email with the agent.
  - An agent can address a file to a *different* person it is shared with by passing `audience_email`. An address the agent is not shared with is refused (`AUDIENCE_NOT_ON_ROSTER`) — it is never widened silently.
  - If Trinity cannot tell which conversation a share came from, it does not guess: the file is listed for the owner only, and the tool result says so (`visible_to_requester: false`, with a `visibility_note` explaining how to fix it). Passing the turn's `execution_id` to `share_file` is what makes this reliable.
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
| `/api/agents/{name}/files` | PUT | Save a text file (what the inline editor calls) |
| `/api/agents/{name}/files` | DELETE | Delete a file (protected paths are refused) |
| `/api/agents/{name}/files/download` | GET | Download file content (100 MB limit) |
| `/api/agents/{name}/files/preview` | GET | File content with its real MIME type, for previews |
| `/api/agents/{name}/files/mkdir` | POST | Create a directory in the workspace |
| `/api/agents/{name}/file-sharing` | GET | File sharing status and quota |
| `/api/agents/{name}/file-sharing` | PUT | Enable or disable file sharing (owner/admin) |
| `/api/agents/{name}/shared-files` | POST | Mint a download URL for a file in `/home/developer/public/` |
| `/api/agents/{name}/shared-files` | GET | List active shared files |
| `/api/agents/{name}/shared-files/{id}` | DELETE | Revoke a shared file |
| `/api/files/{file_id}` | GET/HEAD | Public download — query param `?sig={token}`; supports `Range` requests; `?download=1` forces a download |

## See Also

- [Creating Agents](creating-agents.md)
- [Managing Agents](managing-agents.md)
- [Workspace](../sharing-and-access/workspace.md) — the Workspace has its own Files tab for files you and the agent exchange in a conversation
