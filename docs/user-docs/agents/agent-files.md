# Agent Files

Two-panel file manager in the Agent Detail Files tab for browsing, previewing, and editing agent workspace files.

## How It Works

1. Open the agent detail page and click the **Files** tab.
2. The left panel displays a file tree with search and expandable directories.
3. The right panel shows a preview of the selected file.
4. Supported previews: images, video, audio, PDF, and text files.
5. Click **Edit** on a text file to modify it inline, then **Save**. A file you cannot change carries a lock in the tree (*Read-only for you*) and has no Edit button — which files those are depends on who you are, see [Who can do what](#who-can-do-what). **Download** saves the selected file (up to 100 MB).
6. Click the new-folder icon in the tree header to create a subfolder in the selected folder, or in the workspace root when the selection is not a folder (workspace-confined; protected paths are rejected).
7. Click **Delete** to remove a file or folder after a confirmation. Where a delete is not allowed the button is disabled and a note under the file says why.
8. Tick **Hidden** to reveal dotfiles (`.env`, `.claude/`, etc.). The box is enabled for the agent's owner and admins; for anyone the agent is shared with it is disabled and hidden files stay out of the tree.
9. The agent workspace root is `/home/developer/`. The agent must be running to browse, preview, edit, or delete files.

### Who can do what

Every Files action is decided per path and per caller, by one policy. Callers fall into three tiers:

| Tier | Who |
|------|-----|
| **Owner** | The agent's owner or an admin, as a person (signed in, or with their own user-scoped key) |
| **Share** | Any other person the agent is shared with |
| **Key** | An agent, system, connector or ops API key — an agent's own injected key included |

Paths fall into classes, and the tier × verb table is:

| Path class | Examples | Read | Write | Delete |
|-----------|----------|------|-------|--------|
| **Credentials and runtime config** | `.env`, `.env.*`, `.mcp.json`, `.mcp.json.template`, `.credentials.enc`, `.ssh/`, `.aws/`, `.gcp/`, `.claude/settings*.json`, `.git/config`, `~/.claude.json`, `~/.claude/.credentials.json`, `~/.gemini/settings.json`, `~/.tmp/codex/`, Trinity's copies at `.trinity/git-credential` and `.trinity/backup/`, and any path under `/proc/` or `/dev/` (another spelling of a home path) | Owner | Nobody | Nobody |
| **Secret-class files** | `*.key`, `*.pem`, `*.p12`, `*.pfx`, `.kube/config`, `.config/gcloud/`, `.netrc`, `.git-credentials`, `.docker/config.json`, `.config/gh/hosts.yml`, `.gemini/oauth_creds.json` | Owner | Everyone | Everyone |
| **Platform-managed** | `.trinity/`, `.git/` and `.gitignore` at any depth, a bare repository's `config` and `hooks/`, `/etc/`, `/opt/trinity/`, `/sys/` | Everyone | Nobody | Nobody |
| **Instructions** | `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `GEMINI.md`, `.gemini/GEMINI.md`, everything under `.claude/` (skills included) | Everyone | Owner; a key with the matching permission | Owner; a key with the matching permission. `CLAUDE.md`: nobody |
| **Files that run code** | `.bashrc`, `.profile`, `.zshrc` and the other shell startup files, `.gitconfig`, `.config/git/`, `.local/bin/`, `.local/lib/` | Everyone | Owner | Owner |
| **Everything else** | the agent's working files, `data/`, `memory/`, `uploads/`, `content/`, any other dotfile | Everyone | Everyone | Everyone |

**Listing** follows the same tiers: a share holder's tree has no hidden files or directories (anything with a dot-segment) and no credential files, and asking for a hidden or credential directory as the root is refused; the owner tier and keys list everything. `.trinity/pipelines/` and `.trinity/pipeline-state/` — the agent's published pipeline surface — are listed and readable for everyone with access.

Only an agent's own top-level instruction files count: a `docs/CLAUDE.md` is an ordinary file. A delete is also refused for a folder that *contains* a protected or owner-tier location — `.ssh`, `.claude`, `.tmp` (it holds the Codex login), or the home directory itself — since deleting the folder deletes everything in it. Paths are normalised before the check, so `..` segments and extra leading slashes (`//home/developer/.ssh/…`) cannot slip past it.

A **key** that clears the table still needs the right permission for instruction and skill paths: writing or deleting under `.claude/skills/` is the same act as assigning a skill, and an instruction file the same as editing the agent's instructions, so an agent's own key needs the skill-management or instruction-management permission (see [Permissions to Change Itself](agent-configuration.md#permissions-to-change-itself)). People and the system agent are unaffected.

**What the Files tab shows.** The tree comes back already filtered for you. Below the owner tier, hidden files are not listed and the **Hidden** box is disabled, with *Hidden files are shown only to the agent's owner or an admin* on hover. A file or folder you cannot change carries a lock marked *Read-only for you*; selecting it hides **Edit**, and the note under it says *Only the agent's owner or an admin can change this. A share covers the agent's files except credentials, runtime config and its instructions* — or, for the owner, *This is a protected system file and cannot be changed here*. **Delete** and **Download** are disabled for the same reason. A link shows as `name →` and is never opened; its note says to open the file it points to. The **Settings › People** pane states the share in one sentence.

**Old images.** A share holder's write, new folder or delete on an agent still running an image from before this rule is refused with `agent_restart_required` until the agent is stopped and started from Trinity. The owner, an admin and agent keys are not held back.

Every write, delete and new folder through these routes writes an audit row, and so does every refusal.

### Content Folder Convention

The `content/` directory is gitignored by default. Use it for large generated assets such as images, audio, and video.

### Shared Folders

Agents can expose their workspace folder for other agents to mount as a collaboration mechanism.

- Configure under **Settings › Storage › Shared folders** (owner only; an old `?tab=folders` link lands there) using the **Expose Shared Folder** and **Mount Shared Folders** toggles. A mount change that needs a restart is listed in the bar at the top of Settings, which restarts the agent for you.
- Permission-gated: only permitted agents can mount a shared folder.
- Relevant API endpoints: `GET/PUT /api/agents/{name}/folders`, `GET /api/agents/{name}/folders/available`, `GET /api/agents/{name}/folders/consumers`.

## Outbound File Sharing

Agents can publish files to a signed download URL that works universally — web, Slack, Telegram, WhatsApp, email — without per-channel upload handling.

### Enabling File Sharing

1. Open the agent detail page, click the **Settings** tab and pick **Storage** in the rail.
2. In the **File sharing** section, switch the toggle on (it reads **Enabled** / **Disabled**).
3. The bar at the top of Settings lists **File sharing** as waiting for a restart — click **Restart now** (or **Start now** for a stopped agent) to mount the publish volume. The entry clears once the agent is running again.
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

## Reading credential files

Credential files open, preview and download only for the agent's **owner** or an **admin**, signed in or using their own user-scoped MCP key. This covers `.env` and `.env.*`, `.mcp.json` and `.mcp.json.template`, `.credentials.enc`, `~/.ssh`, `~/.aws`, `~/.gcp`, the Claude Code settings and login files, the Gemini and Codex runtime config, `.git/config`, key and certificate files (`*.key`, `*.pem`, `*.p12`, `*.pfx`), `~/.kube/config`, `~/.config/gcloud`, `.netrc`, `.git-credentials`, `.docker/config.json`, `.config/gh/hosts.yml`, `.gemini/oauth_creds.json`, and Trinity's own copies at `.trinity/git-credential` and `.trinity/backup/`. Anyone else the agent is shared with sees: "Credential files can be opened only by the agent's owner or an admin. You can still chat with this agent, and it keeps using its credentials. To view or change them, ask the agent's owner or an admin." Agent, system and connector keys cannot read these files. Every refused read, and every read by anyone other than the owner signed in, is recorded in the audit log.

All other files, including `.trinity/pipelines/` and `.trinity/pipeline-state/`, stay readable by anyone the agent is shared with.

## Links

Files that are links, or that sit inside a linked folder, are not opened, previewed or downloaded through the Files tab, by anyone; the message says the path is a link. Open the file it points to instead. Ship real files, not links, at the paths Trinity reads itself (`CLAUDE.md`, `template.yaml`, dashboard images): a linked `CLAUDE.md`, for example, stops receiving the refreshed Platform Skills section.

Writes follow the same rule. Saving, creating a folder, deleting and listing refuse a path that goes through a link to somewhere of a different kind — `workspace/x/CLAUDE.md` where `x` points at `.claude`, say — with `resolved_path_mismatch`, for every caller; a link that stays within its kind keeps working. A file is never written *through* a link, and deleting a link removes the link, never its target. The tree never descends into a linked folder (links show as `name →`) and stops at 20,000 entries, flagged `truncated` in the response.

Below the owner tier, a file that has more than one name on disk (a hard link) is not opened or downloaded either, because the second name could be a credential under a different one: the message asks you to have the owner or an admin open it.

If an agent still runs an older image, opening, previewing or downloading its files is refused for anyone other than its owner or an admin (agent keys included), with: "This agent needs a restart to apply an update. Ask the agent's owner or an admin to stop and start it in Trinity." Stop and start the agent from Trinity: that recreates it on the current image.

## Protected runtime files

The Files tab cannot change or delete the agent runtimes' login and configuration files: `~/.claude.json`, `~/.claude/.credentials.json`, `~/.gemini/settings.json` and everything under `~/.tmp/codex/`. Because `~/.tmp/codex/` holds the Codex runtime's login, the `.tmp` folder cannot be deleted from the Files tab.

## For Agents

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/files` | GET | List workspace files (tree structure), already filtered for the caller. The response carries `tier` (`owner`, `share` or `key`), `hidden_withheld`, `truncated`, and on each node `restricted` — the verbs (`read`, `write`, `delete`) this caller may not use on it |
| `/api/agents/{name}/files` | PUT | Save a text file (what the inline editor calls); refused paths answer `403` (codes below) |
| `/api/agents/{name}/files` | DELETE | Delete a file or folder; a link is unlinked, never followed. Refused paths answer `403` |
| `/api/agents/{name}/files/download` | GET | Download file content (100 MB limit) |
| `/api/agents/{name}/files/preview` | GET | File content with its real MIME type, for previews |
| `/api/agents/{name}/files/mkdir` | POST | Create a directory in the workspace; ruled like a write |

A refused verb answers `403`. Protected paths that nobody may change answer a plain string (`Cannot edit protected path: …`, `Cannot delete protected path: …`); every other refusal is `{"code", "message", "path"}`:

| Code | Meaning |
|------|---------|
| `owner_tier_path` | The path takes the owner tier for this verb — a share holder on an instruction, skill or startup file, or anyone below the owner tier reading a credential or secret-class file |
| `protected_path` | No caller may use this verb here (credentials, runtime config, `.trinity/`, git metadata, system paths, deleting `CLAUDE.md`), or an agent key on a file that runs code |
| `resolved_path_mismatch` | The path goes through a link that changes what kind of path it is; nothing is followed |
| `hardlinked_file` | Below the owner tier: the file has more than one name on disk |
| `agent_restart_required` | The agent runs an image from before this rule; stop and start it from Trinity |
| `skill_management_not_permitted`, `instruction_management_not_permitted` | An agent key writing under `.claude/skills/` or to an instruction file without the matching permission |
| `invalid_path` (`400`) | An empty path or one containing a NUL byte |
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
