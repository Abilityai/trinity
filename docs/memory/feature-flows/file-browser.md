# Feature: Per-Agent File Manager

> **Updated**: 2026-03-03 - **Files tab restored to AgentDetail.vue** (Issue #51). FilesPanel.vue rewritten with full two-panel file manager using same components as standalone FileManager. Standalone `/files` route removed.
>
> **Previous (2026-02-18)**: Files tab removed from AgentDetail.vue. Users directed to standalone `/files` page.

## Revision History
| Date | Changes |
|------|---------|
| 2026-10-06 | **Credential files are owner-tier to read; reads never follow a link; runtime config files are write-protected** (trinity-enterprise#819, #823): download and preview refuse an owner-tier path (credential files, their `/proc` / `/dev` spellings, `.trinity/git-credential`, `.trinity/backup/*`, the secret file classes, the runtime config files) unless the caller is a person who passes the owner tier; refused and non-owner allowed reads are audited. The agent server opens download/preview paths without following any link and serves that descriptor; a link is refused with `resolved_path_mismatch` for every caller. Agents not yet on that image refuse below-owner reads until restarted. `~/.claude.json`, `~/.claude/.credentials.json`, `~/.gemini/settings.json` and `~/.tmp/codex/*` join every write list; deleting `.tmp` is refused. The Credentials panel shows a refusal instead of opening an empty editor. |
| 2026-10-06 | **Backend deny list covers DELETE; paths normalised before every check** (trinity-enterprise#792): `_normalize_user_path` collapses any run of leading slashes (`posixpath.normpath` keeps exactly two). `DELETE /files` refuses deny-listed paths and any directory that holds one (`_is_user_deletable_path`). PUT, mkdir and DELETE send the agent the normalised path that was checked. |
| 2026-03-03 | **Per-agent Files tab restored** (Issue #51): FilesPanel.vue rewritten with full file manager (tree + preview). Uses `file-manager/FileTreeNode.vue` and `file-manager/FilePreview.vue`. Standalone `/files` route removed. |
| 2026-02-18 | Files tab removed from AgentDetail.vue. Users directed to standalone File Manager. |
| 2026-01-23 | Verified all line numbers. Updated frontend architecture (FilesPanel + composable). Documented protected paths (delete/edit). |
| 2025-12-30 | Updated line numbers. Service file grew from 137 to 413 lines. |
| 2025-12-27 | Service layer refactoring. File operations moved to `services/agent_service/files.py`. |
| 2025-12-06 | Agent-server modular refactoring (`agent_server/routers/files.py`). |
| 2025-12-01 | Initial tree structure implementation. |

## Overview
Users can browse, preview, edit, download, and delete files from agent workspaces through the Trinity web UI without requiring SSH access. The file manager is embedded in the **Files tab** of each agent's detail page, providing a **two-panel layout** (tree on left, preview on right) with rich media support.

## User Story
As a Trinity user, I want to manage files in my agent's workspace using a familiar two-panel file manager so that I can browse, preview, edit, and delete files without needing SSH or Docker command-line access.

## Entry Points
- **UI**: `src/frontend/src/views/AgentDetail.vue` - Files tab in Agent Detail page
- **Component**: `src/frontend/src/components/FilesPanel.vue` - Full file manager panel
- **API**: `GET /api/agents/{agent_name}/files`
- **API**: `GET /api/agents/{agent_name}/files/download`
- **API**: `GET /api/agents/{agent_name}/files/preview`
- **API**: `DELETE /api/agents/{agent_name}/files`
- **API**: `PUT /api/agents/{agent_name}/files`

---

## Frontend Layer

### Architecture

The file browser uses a **component-based** architecture with shared sub-components:

| Layer | File | Purpose |
|-------|------|---------|
| View | `src/frontend/src/views/AgentDetail.vue` | Files tab host (visibleTabs) |
| Component | `src/frontend/src/components/FilesPanel.vue` | Full two-panel file manager |
| Component | `src/frontend/src/components/file-manager/FileTreeNode.vue` | Rich recursive tree node with icons |
| Component | `src/frontend/src/components/file-manager/FilePreview.vue` | Rich media preview (image/video/audio/PDF/text/edit) |
| Store | `src/frontend/src/stores/agents.js` | API calls (listAgentFiles, downloadAgentFile, deleteAgentFile, updateAgentFile, getFilePreviewBlob) |

### FilesPanel Component

**File**: `src/frontend/src/components/FilesPanel.vue` (~607 lines)

Rendered in AgentDetail.vue (line 184-186):
```vue
<div v-if="activeTab === 'files'">
  <FilesPanel :agent-name="agent.name" :agent-status="agent.status" />
</div>
```

**Two-Panel Layout** (Lines 11-195):
- **Left Panel** (w-80, lines 14-80): File tree with controls, search, and footer stats
- **Right Panel** (flex-1, lines 83-194): FilePreview component with file info bar and action buttons

**Left Panel Features**:
- **Controls Row** (Lines 16-36): Refresh button (`@click="loadFiles"`) + "Hidden" checkbox (`v-model="showHidden"`, persisted to `localStorage`)
- **Search Box** (Lines 39-51): `v-model="searchQuery"` - filters tree recursively via `filteredTree` computed
- **Tree View** (Lines 54-74): Loading spinner, error state, empty state, or `FileTreeNode` components
- **Footer Stats** (Lines 77-79): Total file count and total size

**Right Panel Features**:
- **No File Selected** (Lines 85-93): Placeholder prompting user to select a file
- **FilePreview** (Lines 96-109): Renders `FilePreview` component with preview data, edit state props
- **File Info Bar** (Lines 112-192): File name, path, size, modified date. Action buttons: Edit (text files only, not edit-protected), Download, Delete (with confirmation modal)
- **Delete Confirmation Modal** (Lines 198-244): Warns about folder contents, disables during delete

**Props** (Lines 264-273):
```javascript
const props = defineProps({
  agentName: { type: String, required: true },
  agentStatus: { type: String, default: 'stopped' }
})
```

**Key State** (Lines 287-303):
- `fileTree`, `selectedFile`, `searchQuery`, `loading`, `error` - tree and selection state
- `previewData`, `previewLoading`, `previewError` - preview state
- `isEditing`, `editContent`, `saving`, `hasUnsavedChanges` - edit mode state
- `showHidden` - persisted to localStorage
- `showDeleteConfirm`, `downloading`, `deleting` - action state

**Key Methods**:
- `loadFiles()` (Line 387-402): Calls `agentsStore.listAgentFiles()`, populates `fileTree`
- `onFileSelect(item)` (Line 404-424): Sets `selectedFile`, resets edit state, calls `loadPreview()` for files
- `loadPreview(file)` (Line 426-437): Calls `agentsStore.getFilePreviewBlob()`, sets `previewData`
- `downloadFile()` (Line 439-469): Fetches blob from preview URL, triggers browser download via `<a>` element
- `deleteFile()` (Line 471-493): Calls `agentsStore.deleteAgentFile()`, clears selection, reloads tree
- `startEdit()` (Line 496-508): Fetches text from preview blob URL, enters edit mode
- `saveFile()` (Line 526-547): Calls `agentsStore.updateAgentFile()`, exits edit mode, reloads preview

**Lifecycle & Watchers** (Lines 574-605):
- `onMounted`: Loads files if agent is running
- `onUnmounted`: Revokes blob URLs
- `watch(agentStatus)`: Reloads when agent starts, clears when agent stops
- `watch(agentName)`: Reloads when switching agents

### FileTreeNode Component

**File**: `src/frontend/src/components/file-manager/FileTreeNode.vue` (220 lines)

Recursive template-based component with rich file type icons:

```javascript
// Lines 64-68
const props = defineProps({
  item: { type: Object, required: true },
  selectedPath: { type: String, default: null },
  searchQuery: { type: String, default: '' }
})
```

**Features**:
- **Folders** (Lines 13-22): Chevron icon (rotates 90deg when expanded), expand/collapse on click
- **Files** (Lines 23, 34-36): File size display, click to select
- **File Type Icons** (Lines 93-113, 145-218): Color-coded SVG icons for folders (yellow), video (purple), audio (green), image (blue), code (gray), PDF (red), text, and generic documents - all defined as inline render-function components
- **Selection Highlight** (Line 8): Indigo background when `selectedPath` matches
- **Search Highlight** (Line 9, 83-86): Yellow background on name match
- **Indentation**: `ml-4` per nesting level via recursive children div (Line 45)
- **Recursion** (Lines 46-53): Renders children via self-reference, shows "Empty folder" for empty directories
- **Auto-expand on search** (Lines 73-78): Watches `item.expanded` prop set by parent's `filteredTree` computed

### FilePreview Component

**File**: `src/frontend/src/components/file-manager/FilePreview.vue` (245 lines)

Rich media preview component supporting multiple file types:

**Props** (Lines 146-155):
```javascript
const props = defineProps({
  file: { type: Object, required: true },
  agentName: { type: String, required: true },
  previewData: { type: Object, default: null },
  previewLoading: { type: Boolean, default: false },
  previewError: { type: String, default: null },
  isEditing: { type: Boolean, default: false },
  editContent: { type: String, default: '' }
})
```

**Preview Types** (determined by extension + MIME type):
- **Image** (Lines 40-47): `<img>` with dimension overlay
- **Video** (Lines 50-59): `<video>` with controls
- **Audio** (Lines 62-81): `<audio>` with decorative card
- **PDF** (Lines 84-90): `<embed>` filling container
- **Text/Code** (Lines 93-107): Edit mode textarea or read-only `<pre><code>` block. Text content loaded via `fetch(previewData.url)` in watcher (Lines 219-231)
- **Binary/Unknown** (Lines 110-123): Fallback with "Preview not available" message
- **Directory** (Lines 25-35): Folder icon with item count

### Legacy: useFileBrowser Composable

**File**: `src/frontend/src/composables/useFileBrowser.js`

> **Note (2026-03-03)**: This composable is NO LONGER used by FilesPanel. It was replaced when FilesPanel was rewritten with inline state management. The composable still exists in the codebase and is exported from `composables/index.js` but has no active consumers. It may be a candidate for removal.

### Legacy: components/FileTreeNode.vue

**File**: `src/frontend/src/components/FileTreeNode.vue`

> **Note (2026-03-03)**: This is the OLD render-function based tree node (141 lines). It has been superseded by `file-manager/FileTreeNode.vue` (220 lines, template-based with icons). No components import it. It may be a candidate for removal.

### Store Actions

**File**: `src/frontend/src/stores/agents.js`

#### listAgentFiles (Line 452-459)
```javascript
async listAgentFiles(name, path = '/home/developer', showHidden = false) {
  const authStore = useAuthStore()
  const response = await axios.get(`/api/agents/${name}/files`, {
    params: { path, show_hidden: showHidden },
    headers: authStore.authHeader
  })
  return response.data  // Returns: { tree: [...], total_files: N, show_hidden: bool }
}
```

#### downloadAgentFile (Line 461-469)
```javascript
async downloadAgentFile(name, filePath) {
  const authStore = useAuthStore()
  const response = await axios.get(`/api/agents/${name}/files/download`, {
    params: { path: filePath },
    headers: authStore.authHeader,
    responseType: 'text'
  })
  return response.data
}
```

#### deleteAgentFile (Line 471-478)
```javascript
async deleteAgentFile(name, filePath) {
  const authStore = useAuthStore()
  const response = await axios.delete(`/api/agents/${name}/files`, {
    params: { path: filePath },
    headers: authStore.authHeader
  })
  return response.data
}
```

#### updateAgentFile (Line 480-488)
```javascript
async updateAgentFile(name, filePath, content) {
  const authStore = useAuthStore()
  const response = await axios.put(`/api/agents/${name}/files`, {
    content
  }, {
    params: { path: filePath },
    headers: authStore.authHeader
  })
  return response.data
}
```

---

## Backend Layer

### Architecture

The file browser feature uses a **thin router + service layer** architecture:

| Layer | File | Purpose |
|-------|------|---------|
| Router | `src/backend/routers/agents.py:500-569` | Endpoint definitions |
| Service | `src/backend/services/agent_service/files.py` (309 lines) | File listing, download, preview, delete, and update logic |

### Endpoints

#### GET /api/agents/{agent_name}/files

**Router**: `src/backend/routers/agents.py:500-514`
**Service**: `src/backend/services/agent_service/files.py:20-78`

**Purpose**: List all files in agent workspace as hierarchical tree structure

**Parameters**:
- `agent_name` (path) - Agent identifier
- `path` (query, optional) - Directory path (default: `/home/developer`)
- `show_hidden` (query, optional) - Include hidden files (default: `false`)

**Business Logic** (in `list_agent_files_logic()`):
1. Check user authentication (`get_current_user` dependency)
2. Verify user has access to agent (`db.can_user_access_agent()`)
3. Get agent container (`get_agent_container()`)
4. Verify container exists (404 if not)
5. Check container is running (400 if not)
6. Proxy request to agent's internal API at `http://agent-{name}:8000/api/files`
7. Return hierarchical tree JSON

**Response Format** (Tree Structure):
```json
{
  "base_path": "/home/developer",
  "requested_path": ".",
  "tree": [
    {
      "name": "scripts",
      "path": "scripts",
      "type": "directory",
      "file_count": 5,
      "modified": "2025-12-01T10:30:00",
      "children": [
        {
          "name": "deploy.sh",
          "path": "scripts/deploy.sh",
          "type": "file",
          "size": 1234,
          "modified": "2025-12-01T10:30:00"
        }
      ]
    },
    {
      "name": "README.md",
      "path": "README.md",
      "type": "file",
      "size": 5678,
      "modified": "2025-12-01T10:25:00"
    }
  ],
  "total_files": 6,
  "show_hidden": false
}
```

#### GET /api/agents/{agent_name}/files/download

**Router**: `src/backend/routers/agents.py:517-525`
**Service**: `src/backend/services/agent_service/files.py:81-131`

**Purpose**: Download file content from agent workspace

**Parameters**:
- `agent_name` (path) - Agent identifier
- `path` (query, required) - File path relative to workspace

**Business Logic** (in `download_agent_file_logic()`; preview is the same):
1. Check user authentication
2. Verify user has access to agent (accessor tier)
3. Refuse a path containing a NUL byte: 400 `{"code": "invalid_path"}`, no agent call
4. **Owner-tier paths** (`_enforce_owner_tier_read`, trinity-enterprise#819): if the path matches `_OWNER_TIER_READ_PATTERNS`, the caller must be a person (`assert_person`: a signed-in session or the person's own user-scoped key) and pass the owner tier (`assert_agent_owner`: the owner, or an admin). Refused → 403 `owner_tier_path` (or `person_required` for a key) and a `file_read_refused` audit row; allowed for anyone but the owner's own signed-in session → a `file_read_allowed` row
5. Get agent container; verify it exists and is running
6. **Older images** (`_refuse_below_owner_on_unverified_image`): below the owner tier, the container is probed once per container and image for the no-link read (`grep` of the agent server's `files.py` for `_open_for_read`); an unverified agent answers 403 `agent_restart_required`, logged with a running count. The owner and admins never trigger the probe
7. Proxy to the agent's internal API with the **normalised** path that was checked
8. **Links are not followed**: an agent answer of 403 `resolved_path_mismatch` becomes a structured 403 and a `file_read_refused` row (`rule: resolved_path_mismatch`), for every caller, the owner included
9. Return file content as PlainTextResponse

**Owner-tier set** (`services/agent_service/files.py`): `_CREDENTIAL_PATH_PATTERNS` (`.env`, `.env.*`, `.mcp.json`, `.mcp.json.template`, `.credentials.enc`, `.ssh/*`, `.aws/*`, `.gcp/*`, `.claude/settings.json`, `.claude/settings.local.json`, `.git/config`, and the runtime config files `.claude.json`, `.claude/.credentials.json`, `.gemini/settings.json`, `.tmp/codex/*`), `_PATH_ALIAS_PATTERNS` (`/proc/*`, `/dev/*`), `_TRINITY_CREDENTIAL_COPY_PATTERNS` (`.trinity/git-credential`, `.trinity/backup/*`) and `_SECRET_FILE_CLASS_PATTERNS` (`.kube/config`, `.config/gcloud/*`, `*.key`, `*.pem`, `*.p12`, `*.pfx`). Every pattern but the secret classes is also write-denied. `.trinity/pipelines` and `.trinity/pipeline-state` stay at the accessor tier, so the MCP pipeline tools work for anyone with access.

**Response**: Plain text content of the file

#### GET /api/agents/{agent_name}/files/preview

**Router**: `src/backend/routers/agents.py:528-536`
**Service**: `src/backend/services/agent_service/files.py:187-246`

**Purpose**: Get file with proper MIME type for preview (images, video, audio, etc.)

**Parameters**:
- `agent_name` (path) - Agent identifier
- `path` (query, required) - File path relative to workspace

**Response**: StreamingResponse with correct Content-Type header

#### DELETE /api/agents/{agent_name}/files

**Router**: `src/backend/routers/agents.py:539-547`
**Service**: `src/backend/services/agent_service/files.py:134-184`

**Purpose**: Delete a file or directory from agent workspace

**Parameters**:
- `agent_name` (path) - Agent identifier
- `path` (query, required) - File path to delete

**Protected Paths**: two layers.
- **Backend** (`_is_user_deletable_path`, trinity-enterprise#792), checked after the access check and the `skills.manage` fence, before the container lookup. It refuses everything the write deny list refuses, plus any directory that holds a path-anchored protected path: `.ssh`, `.aws`, `.gcp`, `.trinity`, `.git`, **`.claude`** (holds `settings.json`), the home dir and `/`. Anchors are derived from `_FILE_WRITE_DENY_PATTERNS` (`_DENY_ANCHORS`). `/proc/*` is load-bearing, because `/proc/self/root/…` and `/proc/self/cwd/…` resolve into the home dir inside the agent container. Basename patterns (`.env`, `.credentials.enc`) are name-only on DELETE, so a directory that merely contains one is not refused. Refusal: 403 `Cannot delete protected path: {path}`, which the Files tab shows as `Failed to delete: …`.
- **Agent server** (`PROTECTED_PATHS`, by name at any depth and on any parent): `CLAUDE.md`, `.trinity`, `.git`, `.gitignore`, `.env`, `.mcp.json`, `.mcp.json.template`.

**Path normalisation** (all three write routes): `_normalize_user_path` resolves `.`/`..` lexically, anchors relative paths at `/home/developer`, and collapses any run of leading slashes to one. The agent is sent that normalised path, never the raw input, so the check and the action read one string. The agent echoes the string it received, so the `path` / `deleted` fields and the agent's own 404/409 messages (`File not found: …`, `Directory already exists: …`) carry the absolute normalised path, not the string the caller sent.

**Response**:
```json
{
  "success": true,
  "deleted": "/home/developer/path/to/file",
  "type": "file",
  "file_count": 1
}
```

#### PUT /api/agents/{agent_name}/files

**Router**: `src/backend/routers/agents.py:555-569`
**Service**: `src/backend/services/agent_service/files.py:249-308`

**Purpose**: Update a file's content in agent workspace

**Parameters**:
- `agent_name` (path) - Agent identifier
- `path` (query, required) - File path to update
- `body.content` (body, required) - New file content

**Protected Paths** (#590, AISEC-C2): Cannot edit `.trinity`, `.git`, `.gitignore`, `.env`, `.mcp.json`, `.mcp.json.template`, `.credentials.enc` (agent server). The backend deny list (`_is_user_writable_path`, broader: `.ssh/*`, `.aws/*`, `.gcp/*`, `.claude/settings*.json`, `/opt/trinity/*`, `/proc/*`, …) runs first on the normalised path; a `//`-prefixed spelling is refused like the canonical one (trinity-enterprise#792).
**Note**: `CLAUDE.md` IS editable (owners manage agent instructions). `.mcp.json` is no longer editable here — raw content defines executable tool commands; use the platform regenerate-from-template flow.

**Response**:
```json
{
  "success": true,
  "path": "/home/developer/CLAUDE.md",
  "size": 1234,
  "modified": "2025-12-01T10:30:00.123456"
}
```

---

## Agent Layer

> **Architecture**: The agent-server uses a modular package structure at `docker/base-image/agent_server/`.

### Agent Server Endpoints

**File**: `docker/base-image/agent_server/routers/files.py` (370 lines)

#### GET /api/files (Line 23-109)

**Purpose**: Recursively list files in workspace directory as hierarchical tree

**Parameters**:
- `path` (query, optional) - Directory to list (default: `/home/developer`)
- `show_hidden` (query, optional) - Include hidden files (default: `false`)

**Security**:
- Only allows access to `/home/developer` and subdirectories
- Path traversal protection via `Path.resolve()` and prefix check
- Skips hidden files/directories unless `show_hidden=true`

**Business Logic**:
1. Resolve requested path and validate it's within workspace (Line 36-41)
2. Check path exists (404 if not) (Line 43-44)
3. Call recursive `build_tree(directory)` function (Line 46-94)
4. Return structured tree response (Line 96-109)

**build_tree() Function** (Line 46-94):
```python
def build_tree(directory: Path, base_path: Path, include_hidden: bool) -> dict:
    """Build a hierarchical tree structure from a directory."""
    items = []
    total_files = 0

    dir_items = sorted(directory.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))

    for item in dir_items:
        if item.name.startswith('.') and not include_hidden:
            continue

        if item.is_dir():
            subtree = build_tree(item, base_path, include_hidden)  # Recursive
            items.append({
                "name": item.name,
                "path": str(relative_path),
                "type": "directory",
                "children": subtree["children"],
                "file_count": subtree["file_count"],
                "modified": timestamp
            })
            total_files += subtree["file_count"]
        else:
            items.append({
                "name": item.name,
                "path": str(relative_path),
                "type": "file",
                "size": stat.st_size,
                "modified": timestamp
            })
            total_files += 1

    return {"children": items, "file_count": total_files}
```

**Response**:
```json
{
    "base_path": "/home/developer",
    "requested_path": ".",
    "tree": [
        {
            "name": "scripts",
            "path": "scripts",
            "type": "directory",
            "file_count": 2,
            "modified": "2025-12-01T10:30:00.123456",
            "children": [...]
        },
        {
            "name": "example.txt",
            "path": "example.txt",
            "type": "file",
            "size": 1234,
            "modified": "2025-12-01T10:30:00.123456"
        }
    ],
    "total_files": 3,
    "show_hidden": false
}
```

#### GET /api/files/download (Line 112-153)

**Links are not followed** (trinity-enterprise#819): `_open_for_read` walks the path one component at a time from `_HOME` with `O_NOFOLLOW` (each step relative to the directory opened before it) and the handler reads that descriptor, so the checked file is the file served. A component that is a link → 403 `{"code": "resolved_path_mismatch", "message": "This path is a link. Links are not opened by the file routes; open the file it points to."}`; a missing file → 404; a directory, FIFO, or a file used as a directory → 400; a path outside the home (resolved containment, not a string prefix) → 403. Preview streams the same descriptor with `Content-Length` and an inline `Content-Disposition`.

**Purpose**: Download file content as plain text

**Parameters**:
- `path` (query, required) - File path (absolute or relative to workspace)

**Security**:
- Only allows access to `/home/developer`
- Path traversal protection
- **Max file size: 100MB** (413 error if exceeded)
- Verifies path is a file (400 if directory)

**Business Logic**:
1. Handle both absolute and relative paths (Line 125-128)
2. Resolve path and validate workspace access (Line 130-132)
3. Check file exists (404 if not) (Line 134-135)
4. Verify it's a file, not directory (400 if directory) (Line 137-138)
5. Check file size <= 100MB (413 if too large) (Line 140-144)
6. Read file as UTF-8 text (with error replacement for binary) (Line 146-149)
7. Return as PlainTextResponse

**Response**: Plain text content

#### DELETE /api/files (Line 202-259)

**Purpose**: Delete a file or directory from workspace

**Parameters**:
- `path` (query, required) - File path to delete

**Protected Paths** (Line 157-165):
```python
PROTECTED_PATHS = [
    "CLAUDE.md",
    ".trinity",
    ".git",
    ".gitignore",
    ".env",
    ".mcp.json",
    ".mcp.json.template",
    ".claude.json",        # trinity-enterprise#823
    ".credentials.json",   # trinity-enterprise#823
]
```

**Runtime config** (`_RUNTIME_CONFIG_PATHS`, trinity-enterprise#823): PUT, mkdir and DELETE refuse a **resolved** target at or under `.claude.json`, `.claude/.credentials.json`, `.gemini/settings.json` or `.tmp/codex`, whatever `platform` says; DELETE also refuses a directory above one (`.claude`, `.gemini`, `.tmp`). Matched on the resolved path, so a link elsewhere in the home that points at one is refused too.

**Business Logic**:
1. Validate path is within workspace
2. Check not deleting home directory itself
3. Check path is not protected (403 if protected)
4. Delete file or directory recursively

#### GET /api/files/preview (Line 262-311)

**Purpose**: Get file with proper MIME type for preview

**Parameters**:
- `path` (query, required) - File path to preview

**Security**:
- Same as download (workspace restriction, 100MB limit)

**Business Logic**:
1. Validate path
2. Detect MIME type via `mimetypes.guess_type()`
3. Return FileResponse with correct Content-Type

#### PUT /api/files (Line 314-369)

**Purpose**: Update a file's content

**Parameters**:
- `path` (query, required) - File path to update
- `content` (body, required) - New file content

**Edit-Protected Paths** (refreshed by #590 / AISEC-C2):
```python
EDIT_PROTECTED_PATHS = [
    ".trinity",
    ".git",
    ".gitignore",
    ".env",
    ".mcp.json",          # added #590 — raw injection = RCE-by-config
    ".mcp.json.template", # ditto (envsubst doesn't sanitize attacker JSON)
    ".credentials.enc",   # added #590 — overwrite swaps encrypted backup
    ".claude.json",       # trinity-enterprise#823
    ".credentials.json",  # trinity-enterprise#823
]
```
**Defense in depth**: the backend `update_agent_file_logic` / `create_agent_folder_logic` / `delete_agent_file_logic` in `src/backend/services/agent_service/files.py` run the same deny check (broader: also blocks `.ssh/*`, `.aws/*`, `/opt/trinity/*`, etc.; DELETE also refuses directories holding a protected path) BEFORE proxying to the agent-server, so a future router/proxy gap can't bypass the agent-server's check. They forward the normalised path they checked.

**Business Logic**:
1. Validate path is within workspace
2. Check path is not edit-protected
3. Write new content as UTF-8
4. Return updated file info

**Error Handling** (all endpoints):
- 403: Access denied (outside workspace or protected path)
- 404: File not found
- 400: Not a file (is directory)
- 413: File too large (>100MB)
- 500: Read/write error

---

## Data Flow

### File List Request Flow
```
User clicks "Files" tab in Agent Detail
  |
FilesPanel mounts / status watcher triggers
  |
loadFiles() called (FilesPanel inline method)
  |
agentsStore.listAgentFiles(name)
  |
GET /api/agents/{name}/files (backend router)
  |
list_agent_files_logic() - Authorization check (JWT + ownership)
  |
agent_http_request() - proxy to agent
  |
GET http://agent-{name}:8000/api/files (agent-server)
  |
build_tree() - Walk /home/developer recursively
  |
Filter hidden files/dirs (unless show_hidden=true)
  |
Collect metadata (name, path, size, modified)
  |
Return hierarchical tree JSON
  |
Display files in UI with search/filter
```

### File Download Flow
```
User clicks Download button in right panel
  |
downloadFile() called (FilesPanel inline method)
  |
Fetches blob from previewData.url (or calls agentsStore.getFilePreviewBlob)
  |
GET /api/agents/{name}/files/preview?path=... (backend router, if not cached)
  |
preview_agent_file_logic() - Authorization check
  |
agent_http_request() - proxy to agent
  |
GET http://agent-{name}:8000/api/files/preview?path=... (agent-server)
  |
Validate path within workspace
  |
Check file size <= 100MB
  |
Return FileResponse with correct Content-Type
  |
Create Blob in browser (previewData.url)
  |
Create object URL, trigger download via <a> element
  |
Show success notification
```

---

## Side Effects

### Owner-tier reads are audited
Download and preview write one platform audit row (`file_read_refused` / `file_read_allowed`, `AuditEventType.AUTHORIZATION`, the routed endpoint, the normalised path and the rule) for every refused owner-tier read, every allowed owner-tier read by anyone other than the owner's own signed-in session, and every link refusal (trinity-enterprise#819). A person is filed as `actor_user`; an agent key as its agent; other keys by scope and key. Best-effort: an audit failure never changes the answer. Other file operations are not audited.

### No Database Operations
The file browser (list, download, preview) is read-only and does not modify database tables.
The delete and update operations modify agent workspace files only (not database).

### No WebSocket Broadcasts
This feature does not emit real-time events.

---

## Error Handling

| Error Case | HTTP Status | Message | Where |
|------------|-------------|---------|-------|
| Agent not found | 404 | "Agent not found" | Backend |
| Agent not running | 400 | "Agent must be running to browse/download files" | Backend |
| Agent server not ready | 503 | "Agent server not ready. The agent may still be starting up." | Backend |
| No access permission | 403 | "You don't have permission to access this agent" | Backend |
| Protected path (edit, backend deny list) | 403 | "Cannot edit protected path: {path}" | Backend |
| Protected path (mkdir, backend deny list) | 403 | "Cannot create folder in protected path: {path}" | Backend |
| Protected path (delete, backend deny list incl. holding directories) | 403 | "Cannot delete protected path: {path}" | Backend |
| Owner-tier read below the owner tier | 403 | `{"code": "owner_tier_path", "message": "Credential files can be opened only by the agent's owner or an admin. You can still chat with this agent, and it keeps using its credentials. To view or change them, ask the agent's owner or an admin.", "path": ...}` | Backend |
| Owner-tier read with a non-person key | 403 | `{"code": "person_required", ...}` | Backend |
| NUL byte in the path | 400 | `{"code": "invalid_path", "message": "Invalid path"}` | Backend |
| Below-owner read on an agent not yet on the current image | 403 | `{"code": "agent_restart_required", ...}` | Backend |
| Path is, or passes through, a link (download/preview) | 403 | `{"code": "resolved_path_mismatch", "message": "This path is a link. ...", "path": ...}` | Agent Server → Backend |
| Path outside workspace | 403 | "Access denied: only /home/developer accessible" | Agent Server |
| Protected path (delete) | 403 | "Cannot delete protected path: {name}" | Agent Server |
| Protected path (edit) | 403 | "Cannot edit protected path: {name}" | Agent Server |

The UI shows the `message` of a structured refusal: the store parses the text/blob error body back into `{detail, code, path}` (`normalizeFileErrorBody`); the Files tab shows it in the preview pane and on Download, and the Credentials panel shows it in an `InlineError` beside the file list and does not open the editor (only content or a 404 opens it).
| Cannot delete home | 403 | "Cannot delete home directory" | Agent Server |
| File not found | 404 | "File not found: {path}" | Agent Server |
| Path is directory | 400 | "Not a file: {path}" | Agent Server |
| File too large | 413 | "File too large: {size} bytes (max 104857600)" | Agent Server |
| File read error | 500 | "Failed to read file: {error}" | Agent Server |
| Network timeout (list) | 504 | "File listing timed out" | Backend (30s) |
| Network timeout (download) | 504 | "File download timed out" | Backend (60s) |
| Network timeout (delete) | 504 | "File deletion timed out" | Backend (30s) |
| Network timeout (update) | 504 | "File update timed out" | Backend (60s) |
| Network timeout (preview) | 504 | "File preview timed out" | Backend (30s) |

---

## Security Considerations

### Authentication & Authorization
- JWT token required (Auth0)
- User must own agent OR be in shared_users list
- Checks performed at backend layer (not bypassed via agent-server)

### Path Traversal Prevention
- All paths resolved via `Path.resolve()`
- Prefix check: must start with `/home/developer`
- No access to system files, .env, .git, or other agent workspace directories

### File Access Restrictions
- Hidden files (.env, .git) skipped by default (use `show_hidden=true` to include)
- Hidden directories (.git, .vscode) not traversed by default
- Max file size: 100MB download/preview limit
- Only text file content for download (binary treated as text with error replacement)
- Preview endpoint returns files with proper MIME types

### Protected Path Handling
**Delete-Protected** (cannot be deleted):
- Agent server, by name: `CLAUDE.md`, `.trinity`, `.git`, `.gitignore`, `.env`, `.mcp.json`, `.mcp.json.template`, `.claude.json`, `.credentials.json`; by resolved path, the runtime config paths and every directory above them (`.claude`, `.gemini`, `.tmp`)
- Backend (trinity-enterprise#792): everything on the write deny list, plus directories holding a protected path (`.ssh`, `.aws`, `.gcp`, `.claude`, `.gemini`, `.tmp`, the home dir). See DELETE above.

**Edit-Protected** (cannot be modified):
- Agent server: `.trinity`, `.git`, `.gitignore`, `.env`, `.mcp.json`, `.mcp.json.template`, `.credentials.enc`, `.claude.json`, `.credentials.json`; by resolved path, `.gemini/settings.json` and `.tmp/codex/` too
- Backend deny list: the above plus `.env.*`, `.ssh/*`, `.aws/*`, `.gcp/*`, `.claude/settings*.json`, `.claude/.credentials.json`, `.gemini/settings.json`, `.tmp/codex/*`, `.git/config`, `/opt/trinity/*`, `/etc/*`, `/proc/*`, `/dev/*`, `/sys/*`
- Note: `CLAUDE.md` IS editable (owners manage agent instructions); `.mcp.json` is not (#590)

### Rate Limiting
- Not currently implemented (consider for future if abuse occurs)
- Audit logging provides visibility for abuse detection

### Data Leakage Prevention
- Agent containers are network-isolated (only backend can access)
- Cannot access other agents' workspaces
- Error messages don't expose system paths

---

## Testing

### Prerequisites
1. Running Trinity platform (backend, frontend, agent containers)
2. Authenticated user with an agent (owned or shared)
3. Agent must be in "running" status

### Test Steps

#### 1. File List Display
**Action**: Navigate to Agent Detail page -> Click "Files" tab
**Expected**:
- Two-panel layout appears: tree on left, preview area on right
- Loading spinner appears briefly in tree panel
- File tree loads showing workspace contents
- Each file shows name and size; folders show file count badge
- Footer shows total file count and total size

#### 2. File Preview
**Action**: Click a text file (e.g., `README.md`) in the tree
**Expected**:
- File is highlighted in tree (indigo background)
- Right panel shows file content in monospace `<pre>` block
- File info bar at bottom shows name, path, size, modified date
- Edit, Download, and Delete buttons appear

#### 3. Search/Filter Files
**Action**: Type "test" in search box
**Expected**:
- File tree filters in real-time (recursive filtering)
- Only files with "test" in name shown, parent folders auto-expanded
- Matching items highlighted with yellow background

#### 4. File Download
**Action**: Select a file in tree, click "Download" button in right panel
**Expected**:
- Browser downloads file with original filename
- Success notification appears
- File content matches workspace file

**Verify**:
```bash
# Compare downloaded file with agent workspace file
docker exec agent-{name} cat /home/developer/path/to/file.txt
```

#### 5. File Edit & Save
**Action**: Select a text file, click "Edit" button, modify content, click "Save"
**Expected**:
- Textarea replaces preview with file content
- "You have unsaved changes" warning appears after editing
- "Save" and "Cancel" buttons replace Edit/Download/Delete
- After save, returns to preview mode with updated content
- Success notification "Saved {filename}"

#### 6. File Delete
**Action**: Select a file, click "Delete" button, confirm in modal
**Expected**:
- Confirmation modal warns about action being irreversible
- For folders, shows count of files inside
- After delete, file removed from tree, selection cleared
- Success notification "Deleted {filename}"
- Protected files (CLAUDE.md, .env, etc.) have Delete button disabled

#### 7. Stopped Agent Guard
**Action**: Stop agent, navigate to "Files" tab
**Expected**:
- Empty state with message "Agent must be running to browse files"
- No API calls made
- No errors in console

#### 8. Large File Handling
**Action**: Create 101MB file in workspace, try to download
**Expected**:
- Download fails with error notification
- Error message mentions file size limit
- No browser hang or timeout

**Setup**:
```bash
docker exec agent-{name} dd if=/dev/zero of=/home/developer/large.bin bs=1M count=101
```

#### 9. Hidden Files Toggle
**Action**: Check the "Hidden" checkbox in the controls row
**Expected**:
- Tree reloads including hidden files (.env, .git, .gitignore, etc.)
- Setting persisted to localStorage (`filesPanel.showHidden`)
- Unchecking reloads without hidden files

#### 10. Permission Checks
**Action**: Try to access agent owned by another user
**Expected**:
- Backend returns 403 Forbidden
- Error notification appears
- No file data exposed

### Edge Cases
- **Empty workspace**: Shows "This folder is empty" in tree panel
- **Hidden files**: Hidden by default, toggle with "Hidden" checkbox
- **Binary files**: Preview shows "Preview not available for this file type"; download still works
- **Image/video/audio files**: Rich preview (img/video/audio elements) in right panel
- **Unsaved changes on file switch**: Confirmation dialog before losing edits
- **Network error**: Shows error text in tree panel
- **Path with spaces**: Handles correctly (URL encoding)

### Cleanup
```bash
# Remove test files
docker exec agent-{name} rm /home/developer/large.bin
```

### Status
Working - Feature tested and operational as of 2026-03-03 (Issue #51)

---

## Related Flows

### Upstream
- **Agent Lifecycle** - Agent must be running to browse files
- **Auth0 Authentication** - JWT required for API access
- **Agent Sharing** - Shared users can browse files too

### Downstream
- None (no downstream dependencies; audit logging removed during earlier refactoring)

### Similar Features
- **Agent Logs & Telemetry** - Also provides read-only agent data view
- **GitHub Sync** - Another way to extract agent workspace content

---

## Future Enhancements

### Implemented
- [x] **File Preview**: Preview endpoint with proper MIME types (images, video, audio, PDFs)
- [x] **File Delete**: Delete files and directories (with protected path handling)
- [x] **File Edit/Update**: Update file content (with edit-protected path handling)
- [x] **Show Hidden Files**: Toggle to include hidden files in listing

### Potential Improvements
1. **Upload Files**: Allow file upload to workspace
2. **Bulk Download**: Zip multiple files or entire directory
3. **Rename/Move Files**: Rename or move files within workspace
4. **Syntax Highlighting**: Code preview with language detection
5. **File Versioning**: Track file changes over time
6. **Remember expanded state**: Save which folders were expanded across sessions

### Performance Optimizations
- Pagination for workspaces with 1000+ files
- Virtual scrolling for long file lists
- Incremental loading (load on scroll)
- Cache file list for 30 seconds

### Security Enhancements
- Rate limiting per user/agent
- File type whitelist/blacklist
- Virus scanning for downloads
- Max total workspace size check

---

## Implementation Notes

### Why Plain Text Download + Rich Preview?
- Download endpoint returns UTF-8 text (simple, works for code/config/logs)
- Preview endpoint returns proper MIME types (images, video, audio, PDF rendered natively)
- FilePreview component detects type by extension + MIME and renders appropriate element
- Binary files without a recognized preview type show "Preview not available" fallback

### Why 100MB Limit?
- Prevents memory issues in Python/JavaScript
- Protects against abuse (downloading GB files)
- Large files should use alternative methods (SSH, Docker cp)

### Why Skip Hidden Files (by default)?
- Prevents accidental exposure of secrets (.env)
- Reduces clutter in file list
- Git repositories (.git) can be huge
- `show_hidden=true` parameter available when needed
- FilesPanel has a "Hidden" checkbox toggle (persisted to localStorage)

### Why Recursive Tree vs Flat List?
- **Hierarchical structure**: Mirrors actual filesystem organization
- **Familiar UX**: Similar to macOS Finder, Windows Explorer
- **Scalability**: Better for large workspaces (only show what's expanded)
- **Navigation**: Easy to drill down into specific directories
- **Visual clarity**: Indentation shows hierarchy at a glance

---

## Changelog

- **2026-03-03**: **Files tab restored to Agent Detail** (Issue #51)
  - FilesPanel.vue rewritten from 130-line tree-only panel to ~607-line two-panel file manager
  - Left panel: file tree with refresh, hidden toggle, search, footer stats
  - Right panel: FilePreview component with file info bar and Edit/Download/Delete actions
  - Uses shared `file-manager/FileTreeNode.vue` (220 lines, template-based with file type icons)
  - Uses shared `file-manager/FilePreview.vue` (245 lines, image/video/audio/PDF/text/edit support)
  - All state managed inline (no longer uses `useFileBrowser` composable)
  - Standalone `/files` route removed from router
  - Old `components/FileTreeNode.vue` (141 lines, render-function) now unused/legacy

- **2026-01-23**: Verified all line numbers and updated documentation
  - Frontend refactored: FilesPanel component + useFileBrowser composable
  - FileTreeNode now in separate file (141 lines)
  - Backend router lines: 500-569 (was 551-619)
  - Service file: 309 lines (was 413)
  - Agent server: 370 lines with protected path handling
  - Added show_hidden parameter to file listing
  - Documented DELETE_PROTECTED_PATHS and EDIT_PROTECTED_PATHS

- **2025-12-30**: Updated line numbers to reflect current codebase. Service file grew from 137 to 413 lines (added update function). Fixed file paths.
- **2025-12-27**: **Service layer refactoring**: File operations moved to `services/agent_service/files.py`. Router reduced to thin endpoint definitions.
- **2025-12-06**: Updated agent-server references to new modular structure (`agent_server/routers/files.py`)
- **2025-12-06**: Updated line numbers for files endpoints (15-140 in modular file vs 1701-1842 in old monolithic)

- **2025-12-01 (PM)**: Tree structure implementation
  - Converted from flat file list to hierarchical tree structure
  - Added FileTreeNode recursive component using Vue h() render function
  - Implemented expand/collapse functionality with chevron icons
  - Added folder file count badges
  - Auto-expand folders during search
  - Track expanded state with Set-based storage
  - Modified agent-server build_tree() for recursive tree building
  - Folders collapsed by default, 20px indentation per level
  - Download button visible on hover for files

- **2025-12-01 (AM)**: Initial flat list implementation
  - Added Files tab to AgentDetail.vue
  - Implemented backend proxy endpoints
  - Added agent-server file listing/download APIs (flat structure)
  - Added audit logging for file operations
  - Created feature flow documentation
