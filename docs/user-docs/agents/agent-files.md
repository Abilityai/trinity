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
2. In the **File Sharing** panel, toggle **Enable file sharing** on.
3. A restart-required banner appears — restart the agent to mount the publish volume.
4. After restart, the agent's `/home/developer/public/` directory is live.

### Sharing a File

**From inside the agent** (via MCP `share_file` tool):
```
share_file({ filename: "report.csv" })
# Returns: { url, expires_at, size_bytes, mime_type }
```

The agent drops a file into `/home/developer/public/`, then calls `share_file` with the filename. Trinity extracts it, stores it securely, and returns a signed URL valid for 7 days.

**From the UI:**
- Active shared files appear in the File Sharing panel with filename, size, expiry, and download count.
- Click **Copy URL** to get the link.
- Click **Revoke** to invalidate a link immediately (returns `410 Gone` on download).

### Limits

| | |
|---|---|
| Max file size | 50 MB per file |
| Per-agent storage quota | 500 MB across all active shares |
| Default expiry | 7 days |
| Blocked types | Executables (PE/ELF/Mach-O), scripts with shebangs |

If the agent has `require_email` enabled, download links enforce session verification automatically.

## For Agents

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/files` | GET | List workspace files (tree structure) |
| `/api/agents/{name}/files/download` | GET | Download file content (100 MB limit) |
| `/api/agents/{name}/files/mkdir` | POST | Create a directory in the workspace |
| `/api/agents/{name}/file-sharing` | GET | File sharing status and quota |
| `/api/agents/{name}/file-sharing` | PUT | Enable or disable file sharing (owner/admin) |
| `/api/agents/{name}/shared-files` | POST | Mint a download URL for a file in `/home/developer/public/` |
| `/api/agents/{name}/shared-files` | GET | List active shared files |
| `/api/agents/{name}/shared-files/{id}` | DELETE | Revoke a shared file |
| `/api/files/{file_id}` | GET | Public download — query param `?sig={token}` |

## See Also

- [Creating Agents](creating-agents.md)
- [Managing Agents](managing-agents.md)
