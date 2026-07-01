# Feature: File Manager (Standalone Page) — DEPRECATED

> **Created**: 2025-12-27 - Phase 11.5, Requirement 12.2
>
> **Deprecated**: 2026-03-03 (Issue #51) - Standalone `/files` route removed. File management is now per-agent via the Files tab in Agent Detail. See [file-browser.md](file-browser.md).
>
> **Previous (2026-02-18)**: Files tab removed from AgentDetail.vue; File Manager was the primary file browsing interface
>
> **Related**: [file-browser.md](file-browser.md) - Documents the per-agent FilesPanel component (restored 2026-03-03)

## Overview

**DEPRECATED**: The standalone File Manager page at `/files` has been replaced by per-agent Files tabs in Agent Detail (Issue #51, 2026-03-03). The `FileManager.vue` component is preserved but the route has been removed. The shared sub-components (`file-manager/FileTreeNode.vue` and `file-manager/FilePreview.vue`) are now used by the per-agent `FilesPanel.vue`.

## User Story

As a Trinity user, I want to manage files across all my running agents from a single page so that I can quickly browse, preview, download, and delete files without navigating to each agent's detail page.

## Entry Points

- **UI**: `src/frontend/src/views/FileManager.vue` - Standalone page at `/files` route
- **Router**: `src/frontend/src/router/index.js:48-52` - Route definition
- **API Endpoints**:
  - `GET /api/agents/{agent_name}/files?show_hidden=true|false` - List files (with hidden toggle)
  - `GET /api/agents/{agent_name}/files/preview` - Preview with MIME type
  - `PUT /api/agents/{agent_name}/files?path=...` - Update file content (text files)
  - `DELETE /api/agents/{agent_name}/files` - Delete file/folder

---

## Frontend Layer

### Router Configuration

**File**: `src/frontend/src/router/index.js`

```javascript
// Lines 48-52
{
  path: '/files',
  name: 'FileManager',
  component: () => import('../views/FileManager.vue'),
  meta: { requiresAuth: true }
}
```

### Main Component

**File**: `src/frontend/src/views/FileManager.vue` (639 lines)

#### Component Structure

```
FileManager.vue
  |-- Header: Agent selector dropdown + Refresh button
  |-- Notification Banner (success/error messages)
  |-- Two-Panel Layout:
  |     |-- Left Panel (w-80): File Tree Browser
  |     |     |-- Search input
  |     |     |-- FileTreeNode components (recursive)
  |     |     |-- Footer stats (file count, total size)
  |     |
  |     |-- Right Panel (flex-1): File Preview
  |           |-- FilePreview component
  |           |-- File Info & Actions (download, delete buttons)
  |
  |-- Delete Confirmation Modal
```

#### State Management (Lines 302-320)

```javascript
// Component state (Lines 302-320)
const selectedAgentName = ref(localStorage.getItem('fileManager.selectedAgent') || '')
const fileTree = ref([])
const selectedFile = ref(null)
const searchQuery = ref('')
const loading = ref(false)
const error = ref(null)
const previewData = ref(null)
const previewLoading = ref(false)
const previewError = ref(null)
const downloading = ref(false)
const deleting = ref(false)
const showDeleteConfirm = ref(false)
const showHidden = ref(localStorage.getItem('fileManager.showHidden') === 'true')
// Edit mode state (Lines 316-320)
const isEditing = ref(false)
const editContent = ref('')
const saving = ref(false)
const hasUnsavedChanges = ref(false)
```

#### Protected Paths (Lines 322-326)

```javascript
// Protected paths (cannot be deleted) - Line 323
const DELETE_PROTECTED_PATHS = ['CLAUDE.md', '.trinity', '.git', '.gitignore', '.env', '.mcp.json', '.mcp.json.template']

// Edit protected paths (cannot be edited) - CLAUDE.md and .mcp.json ARE editable - Lines 325-326
const EDIT_PROTECTED_PATHS = ['.trinity', '.git', '.gitignore', '.env', '.mcp.json.template']
```

#### Agent Selector (Lines 26-39)

- Dropdown shows only running agents (`agentsStore.runningAgents`) - computed at Line 329
- Selection persisted to `localStorage.fileManager.selectedAgent`
- Auto-loads files on agent change via `onAgentChange()` handler

#### Search/Filter (Lines 355-375)

```javascript
const filteredTree = computed(() => {
  if (!searchQuery.value) return fileTree.value

  const query = searchQuery.value.toLowerCase()
  const filterItems = (items) => {
    const result = []
    for (const item of items) {
      const matches = item.name.toLowerCase().includes(query)
      if (item.type === 'directory') {
        const filteredChildren = filterItems(item.children || [])
        if (matches || filteredChildren.length > 0) {
          result.push({ ...item, children: filteredChildren, expanded: true })
        }
      } else if (matches) {
        result.push(item)
      }
    }
    return result
  }
  return filterItems(fileTree.value)
})
```

#### Key Methods

| Method | Lines | Purpose |
|--------|-------|---------|
| `loadFiles()` | 406-422 | Fetch file tree from agent (with showHidden) |
| `onAgentChange()` | 424-429 | Handle agent selection, clear state |
| `onFileSelect()` | 431-451 | Select file and load preview, reset edit state |
| `loadPreview()` | 453-464 | Fetch file preview as blob |
| `downloadFile()` | 466-498 | Download file via blob URL |
| `deleteFile()` | 500-523 | Delete file/folder after confirmation |
| `startEdit()` | 526-539 | Enter edit mode, load text content |
| `cancelEdit()` | 541-550 | Exit edit mode with unsaved changes check |
| `saveFile()` | 557-579 | Save edited content to agent |
| `onEditContentChange()` | 552-555 | Track content changes |
| `formatFileSize()` | 581-587 | Format bytes to human-readable size |
| `formatDate()` | 589-603 | Format ISO date to relative time |

#### Protected Path Check (Lines 377-387)

```javascript
const isDeleteProtected = computed(() => {
  if (!selectedFile.value) return false
  const name = selectedFile.value.name
  return DELETE_PROTECTED_PATHS.includes(name)
})

const isEditProtected = computed(() => {
  if (!selectedFile.value) return false
  const name = selectedFile.value.name
  return EDIT_PROTECTED_PATHS.includes(name)
})
```

#### Text File Extensions (Lines 390-397)

```javascript
const TEXT_EXTENSIONS = [
  'txt', 'md', 'json', 'yaml', 'yml', 'toml', 'xml', 'csv', 'log',
  'js', 'ts', 'jsx', 'tsx', 'py', 'go', 'rs', 'rb', 'java', 'c', 'cpp', 'h',
  'css', 'scss', 'sass', 'less', 'html', 'vue', 'svelte',
  'sh', 'bash', 'zsh', 'ps1', 'bat', 'cmd',
  'sql', 'graphql', 'prisma', 'dockerfile', 'makefile',
  'env', 'ini', 'conf', 'cfg', 'gitignore', 'editorconfig'
]
```

When a delete-protected file is selected, the Delete button is disabled with tooltip "Protected file cannot be deleted" and a warning message appears. Note: CLAUDE.md and .mcp.json are editable but not deletable.

### FileTreeNode Component

**File**: `src/frontend/src/components/file-manager/FileTreeNode.vue` (220 lines)

#### Props (Lines 64-68)

```javascript
const props = defineProps({
  item: { type: Object, required: true },
  selectedPath: { type: String, default: null },
  searchQuery: { type: String, default: '' }
})
```

#### File Type Icons (Lines 93-113)

Maps file extensions to icon components:

| Category | Extensions | Icon | Color |
|----------|------------|------|-------|
| Directory | - | FolderIcon/FolderOpenIcon | yellow-500 |
| Video | mp4, webm, mov, avi, mkv | FilmIcon | purple-500 |
| Audio | mp3, wav, ogg, m4a, flac, aac | MusicalNoteIcon | green-500 |
| Image | jpg, jpeg, png, gif, webp, svg, ico, bmp | PhotoIcon | blue-500 |
| Code | js, ts, py, go, vue, css, html, etc. | CodeBracketIcon | gray-500 |
| PDF | pdf | DocumentIcon | red-500 |
| Text | txt, md, json, yaml, sh, etc. | DocumentTextIcon | gray-400 |

#### Expand/Collapse Behavior

- Folders start collapsed
- Click folder row to toggle expansion
- Search auto-expands matching folders
- Chevron rotates 90 degrees when expanded

### FilePreview Component

**File**: `src/frontend/src/components/file-manager/FilePreview.vue` (245 lines)

#### Props (Lines 146-155)

```javascript
const props = defineProps({
  file: { type: Object, required: true },
  agentName: { type: String, required: true },
  previewData: { type: Object, default: null },
  previewLoading: { type: Boolean, default: false },
  previewError: { type: String, default: null },
  // Edit mode props
  isEditing: { type: Boolean, default: false },
  editContent: { type: String, default: '' }
})
```

#### File Type Detection (Lines 169-216)

```javascript
// Lines 169-175
const isImage = computed(() => {
  if (!props.previewData) return false
  const ext = fileExtension.value
  const imageExts = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'ico', 'bmp']
  const mimeMatch = props.previewData.type?.startsWith('image/')
  return imageExts.includes(ext) || mimeMatch
})

const isVideo = computed(() => { /* mp4, webm, mov, avi, mkv */ })  // Lines 177-183
const isAudio = computed(() => { /* mp3, wav, ogg, m4a, flac, aac */ })  // Lines 185-191
const isPdf = computed(() => { /* pdf */ })  // Lines 193-198
const isText = computed(() => { /* comprehensive text/code extensions */ })  // Lines 200-216
```

#### Preview Types

| Type | Component | Features |
|------|-----------|----------|
| Directory | Folder icon + item count | Shows folder info |
| Image | `<img>` (Lines 40-47) | Max size constraints, shows dimensions |
| Video | `<video controls>` (Lines 50-59) | Native player controls |
| Audio | `<audio controls>` (Lines 62-81) | Centered card with icon |
| PDF | `<embed type="application/pdf">` (Lines 84-90) | Full-height embedded viewer |
| Text/Code | `<textarea>` or `<pre><code>` (Lines 93-107) | Edit mode textarea or view mode monospace |
| Unknown | Document icon | "Preview not available" message |

#### Text Content Loading (Lines 219-231)

```javascript
watch(() => props.previewData, async (data) => {
  if (data && isText.value) {
    try {
      const response = await fetch(data.url)
      textContent.value = await response.text()
    } catch (e) {
      textContent.value = 'Failed to load text content'
    }
  } else {
    textContent.value = ''
  }
  imageDimensions.value = null
}, { immediate: true })
```

> **CSP dependency (#1400).** Every preview type renders a `blob:` URL
> (`URL.createObjectURL`), so each depends on the Content-Security-Policy allowing
> `blob:` in the directive that governs its load: text/download → `connect-src`
> (the `fetch(data.url)` above), `<img>` → `img-src`, `<video>/<audio>` →
> `media-src`, PDF `<embed>` → `object-src` **and** `frame-src` (Chrome renders
> the embedded PDF in an internal viewer frame). `blob:` is **not** covered by
> `'self'` — it must be listed explicitly per directive, in **both**
> `src/frontend/security-headers.conf` (prod nginx) and
> `src/frontend/vite.config.js` (dev). A missing scheme surfaces here as
> "Failed to load text content" (text) or a silently blank pane (media/PDF).
> Guarded by `tests/unit/test_1400_csp_blob_preview.py`.

### Store Actions

**File**: `src/frontend/src/stores/agents.js`

#### listAgentFiles (Lines 452-459)

```javascript
async listAgentFiles(name, path = '/home/developer', showHidden = false) {
  const authStore = useAuthStore()
  const response = await axios.get(`/api/agents/${name}/files`, {
    params: { path, show_hidden: showHidden },
    headers: authStore.authHeader
  })
  return response.data
}
```

#### deleteAgentFile (Lines 471-478)

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

#### updateAgentFile (Lines 480-489)

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

#### getFilePreviewBlob (Lines 491-504)

```javascript
async getFilePreviewBlob(name, filePath) {
  const authStore = useAuthStore()
  const response = await axios.get(`/api/agents/${name}/files/preview`, {
    params: { path: filePath },
    headers: authStore.authHeader,
    responseType: 'blob'
  })
  // Return blob URL for media elements
  return {
    url: URL.createObjectURL(response.data),
    type: response.data.type,
    size: response.data.size
  }
}
```

---

## Backend Layer

### Architecture

| Layer | File | Purpose |
|-------|------|---------|
| Router | `src/backend/routers/agents.py:500-569` | Endpoint definitions |
| Service | `src/backend/services/agent_service/files.py` (309 lines) | File operations logic |

### Endpoints

#### GET /api/agents/{agent_name}/files

**Router**: `src/backend/routers/agents.py:500-514`
**Service**: `src/backend/services/agent_service/files.py:20-79`

See [file-browser.md](file-browser.md) for full documentation.

#### GET /api/agents/{agent_name}/files/preview

**Router**: `src/backend/routers/agents.py:528-536`

```python
@router.get("/{agent_name}/files/preview")
async def preview_agent_file_endpoint(
    agent_name: str,
    request: Request,
    path: str,
    current_user: User = Depends(get_current_user)
):
    """Get file with proper MIME type for preview (images, video, audio, etc.)."""
    return await preview_agent_file_logic(agent_name, path, current_user, request)
```

**Service**: `src/backend/services/agent_service/files.py:187-246`

```python
async def preview_agent_file_logic(
    agent_name: str,
    path: str,
    current_user: User,
    request: Request
) -> StreamingResponse:
    """Get file with proper MIME type for preview."""
    # 1. Check user access permission (Line 197-198)
    # 2. Get and verify agent container is running (Lines 200-206)
    # 3. Proxy to agent's /api/files/preview endpoint (Lines 208-218)
    # 4. Stream response with correct Content-Type (Lines 225-233)
```

**Business Logic**:
1. Check user authentication and agent access permission
2. Get agent container and verify it's running
3. Proxy request to agent's internal API
4. Stream response back with correct MIME type
5. Log audit event (`file_preview` action)

**Response**: `StreamingResponse` with proper `Content-Type` header

#### DELETE /api/agents/{agent_name}/files

**Router**: `src/backend/routers/agents.py:539-547`

```python
@router.delete("/{agent_name}/files")
async def delete_agent_file_endpoint(
    agent_name: str,
    request: Request,
    path: str,
    current_user: User = Depends(get_current_user)
):
    """Delete a file or directory from the agent's workspace."""
    return await delete_agent_file_logic(agent_name, path, current_user, request)
```

**Service**: `src/backend/services/agent_service/files.py:127-185`

```python
async def delete_agent_file_logic(
    agent_name: str,
    path: str,
    current_user: User,
    request: Request
) -> dict:
    """Delete a file or directory from the agent's workspace."""
    # 1. Check user access permission (Lines 135-136)
    # 2. Get and verify agent container is running (Lines 138-145)
    # 3. Proxy DELETE to agent's /api/files endpoint (Lines 147-164)
    # 4. Return result with deleted path, type, and file_count
```

#### PUT /api/agents/{agent_name}/files

**Router**: `src/backend/routers/agents.py:555-569`

```python
@router.put("/{agent_name}/files")
async def update_agent_file_endpoint(
    agent_name: str,
    request: Request,
    path: str,
    body: FileUpdateRequest,
    current_user: User = Depends(get_current_user)
):
    """Update a file's content in the agent's workspace."""
    return await update_agent_file_logic(agent_name, path, body.content, current_user, request)
```

**Service**: `src/backend/services/agent_service/files.py:249-308`

```python
async def update_agent_file_logic(
    agent_name: str,
    path: str,
    content: str,
    current_user: User,
    request: Request
) -> dict:
    """Update a file's content in the agent's workspace."""
    # 1. Check user access permission (Lines 266-267)
    # 2. Get and verify agent container is running (Lines 269-275)
    # 3. Proxy PUT to agent's /api/files endpoint with content (Lines 277-288)
    # 4. Return result with path, size, and modified timestamp
```

**Response**:
```json
{
  "success": true,
  "deleted": "path/to/file",
  "type": "file",  // or "directory"
  "file_count": 1  // number of files deleted (for directories)
}
```

---

## Agent Layer

**File**: `docker/base-image/agent_server/routers/files.py`

### GET /api/files/preview (Lines 262-311)

```python
@router.get("/api/files/preview")
async def preview_file(path: str):
    """Get file with proper MIME type for preview."""
    # Security: Only allow /home/developer access (Lines 270-281)
    # Max file size: 100MB (Lines 289-293)
    # Detect MIME type with mimetypes.guess_type() (Lines 296-300)
    # Return FileResponse with correct media_type (Lines 302-307)
```

**Features**:
- MIME type detection via `mimetypes.guess_type()`
- 100MB file size limit
- Path security (only `/home/developer`)
- Returns `FileResponse` for efficient streaming

### DELETE /api/files (Lines 202-259)

```python
@router.delete("/api/files")
async def delete_file(path: str):
    """Delete a file or directory from the workspace."""
    # Security checks (Lines 209-220)
    # Protected path check (Lines 226-231)
    # Recursive directory deletion with shutil.rmtree() (Lines 240-244)
    # Single file deletion with Path.unlink() (Lines 245-247)
```

**Protected Paths** (Lines 157-175):
```python
# Protected paths that cannot be deleted (Lines 157-165)
PROTECTED_PATHS = [
    "CLAUDE.md",
    ".trinity",
    ".git",
    ".gitignore",
    ".env",
    ".mcp.json",
    ".mcp.json.template",
]

# Paths that cannot be edited (subset of PROTECTED_PATHS) - Lines 169-175
# CLAUDE.md and .mcp.json ARE editable since users need to modify them
EDIT_PROTECTED_PATHS = [
    ".trinity",
    ".git",
    ".gitignore",
    ".env",
    ".mcp.json.template",
]
```

**_is_protected_path()** (Lines 178-187):
```python
def _is_protected_path(path: Path) -> bool:
    """Check if path is a protected file/directory (for deletion)."""
    for protected in PROTECTED_PATHS:
        if path.name == protected:
            return True
        # Check parent directories too
        for parent in path.parents:
            if parent.name == protected:
                return True
    return False
```

**_is_edit_protected_path()** (Lines 190-199):
```python
def _is_edit_protected_path(path: Path) -> bool:
    """Check if path is protected from editing."""
    for protected in EDIT_PROTECTED_PATHS:
        if path.name == protected:
            return True
        # Check parent directories too
        for parent in path.parents:
            if parent.name == protected:
                return True
    return False
```

### PUT /api/files (Lines 314-369)

```python
@router.put("/api/files")
async def update_file(path: str, request: FileUpdateRequest):
    """Update a file's content in the workspace."""
    # Security: Only allow /home/developer access (Lines 328-339)
    # Protected path check - cannot edit .trinity, .git, etc. (Lines 341-346)
    # Note: CLAUDE.md and .mcp.json ARE editable
    # Write content with path.write_text() (Lines 354-364)
```

**Request Body**:
```json
{
  "content": "new file content here"
}
```

**Response**:
```json
{
  "success": true,
  "path": "path/to/file",
  "size": 1234,
  "modified": "2026-01-23T15:30:00"
}
```

---

## Data Flow

### File Tree Loading

```
User selects agent from dropdown
  |
onAgentChange() -> localStorage.setItem + loadFiles()
  |
agentsStore.listAgentFiles(name)
  |
GET /api/agents/{name}/files (backend)
  |
Authorization check (JWT + ownership)
  |
GET http://agent-{name}:8000/api/files (agent-server)
  |
Recursive directory traversal
  |
Return hierarchical tree JSON
  |
Render FileTreeNode components
```

### File Preview Loading

```
User clicks file in tree
  |
onFileSelect(item) -> selectedFile = item
  |
loadPreview(file)
  |
agentsStore.getFilePreviewBlob(name, path)
  |
GET /api/agents/{name}/files/preview (backend)
  |
GET http://agent-{name}:8000/api/files/preview (agent)
  |
Return file with Content-Type header
  |
Create blob URL (URL.createObjectURL)
  |
Render appropriate preview component
```

### File Deletion

```
User clicks Delete button
  |
showDeleteConfirm = true (modal opens)
  |
User confirms deletion
  |
deleteFile()
  |
agentsStore.deleteAgentFile(name, path)
  |
DELETE /api/agents/{name}/files?path=... (backend)
  |
DELETE http://agent-{name}:8000/api/files?path=... (agent)
  |
Protected path check (403 if protected)
  |
Directory: shutil.rmtree() / File: path.unlink()
  |
Return { success, deleted, type, file_count }
  |
Audit log: file_delete
  |
Refresh file tree
  |
Show success notification
```

---

## Side Effects

### Audit Logs

| Action | Event Type | Details |
|--------|------------|---------|
| `file_list` | `file_access` | `{"path": "/home/developer"}` |
| `file_preview` | `file_access` | `{"file_path": "...", "content_type": "..."}` |
| `file_delete` | `file_access` | `{"path": "...", "type": "file/directory", "file_count": N}` |

### Local Storage

```javascript
localStorage.setItem('fileManager.selectedAgent', agentName)
```

Persists selected agent across page reloads.

### Blob URL Cleanup

```javascript
onUnmounted(() => {
  if (previewData.value?.url) {
    URL.revokeObjectURL(previewData.value.url)
  }
})
```

Prevents memory leaks from blob URLs.

---

## Error Handling

| Error Case | HTTP Status | Message | Location |
|------------|-------------|---------|----------|
| Agent not found | 404 | "Agent not found" | Backend |
| Agent not running | 400 | "Agent must be running" | Backend |
| No permission | 403 | "You don't have permission to access this agent" | Backend |
| Path outside workspace | 403 | "Access denied: only /home/developer accessible" | Agent |
| Protected path | 403 | "Cannot delete protected path: {name}" | Agent |
| Cannot delete home | 403 | "Cannot delete home directory" | Agent |
| File not found | 404 | "File not found: {path}" | Agent |
| Not a file (preview) | 400 | "Not a file: {path}" | Agent |
| File too large | 413 | "File too large: {size} bytes (max 104857600)" | Agent |
| Timeout (list) | 504 | "File listing timed out" | Backend (30s) |
| Timeout (delete) | 504 | "File deletion timed out" | Backend (30s) |
| Timeout (preview) | 504 | "File preview timed out" | Backend (120s) |

---

## Security Considerations

### Authentication & Authorization

- JWT token required (Auth0 or dev mode)
- User must own agent OR be in shared_users list
- All checks performed at backend layer

### Path Traversal Prevention

- All paths resolved via `Path.resolve()`
- Prefix check: must start with `/home/developer`
- No access to system files outside workspace

### Protected File Deletion Prevention

Agent-level protection for critical files:
- `CLAUDE.md` - Agent instructions
- `.trinity/` - Trinity configuration
- `.git/` - Version control
- `.gitignore` - Git configuration
- `.env` - Environment secrets
- `.mcp.json` - MCP configuration
- `.mcp.json.template` - MCP template

UI-level protection:
- Delete button disabled for protected paths
- Warning message displayed when protected file selected

### File Size Limits

- 100MB maximum for preview and download
- Prevents memory exhaustion attacks
- Large files should use SSH/Docker cp

### Hidden File Visibility

- Hidden files (starting with `.`) skipped in tree listing
- Prevents accidental exposure of secrets

---

## Testing

### Prerequisites

1. Running Trinity platform (backend, frontend, agents)
2. Authenticated user with at least one running agent
3. Agent with files in `/home/developer`

### Test Steps

#### 1. Navigate to File Manager

**Action**: Click "Files" in navigation menu or go to `/files`

**Expected**:
- File Manager page loads
- Agent dropdown shows running agents
- "Select an agent" placeholder if no agent selected

#### 2. Select Agent and Browse

**Action**: Select an agent from dropdown

**Expected**:
- File tree loads in left panel
- Folders show with expand arrows
- Files show with type-appropriate icons
- Footer shows file count and total size

#### 3. Search/Filter Files

**Action**: Type search term in search box

**Expected**:
- Tree filters to show matching items
- Matching folders auto-expand
- Matching names highlighted (yellow background)
- Clear search restores full tree

#### 4. Preview Different File Types

**Action**: Click various file types

| File Type | Expected Preview |
|-----------|------------------|
| Image (png, jpg) | Inline image with dimensions |
| Video (mp4) | Video player with controls |
| Audio (mp3) | Audio player in card |
| PDF | Embedded PDF viewer |
| Code (js, py) | Syntax-highlighted code |
| Text (md, txt) | Plain text in monospace |
| Unknown | "Preview not available" |

#### 5. Download File

**Action**: Click Download button on selected file

**Expected**:
- File downloads with original filename
- Success notification appears

#### 6. Delete File (Non-Protected)

**Action**: Select non-protected file -> Click Delete -> Confirm

**Expected**:
- Confirmation modal appears with file name
- File removed from tree after confirmation
- Success notification shows

#### 7. Protected File Warning

**Action**: Select a protected file (CLAUDE.md, .env, etc.)

**Expected**:
- Delete button is disabled
- Warning message: "This is a protected system file and cannot be deleted"

#### 8. Delete Folder

**Action**: Select folder with files -> Delete -> Confirm

**Expected**:
- Modal shows "This will delete all X files inside"
- Folder and contents removed
- File count updated

#### 9. Agent No Longer Running

**Action**: Stop selected agent while on File Manager page

**Expected**:
- Error notification: "Agent X is no longer running"
- Agent deselected, tree cleared
- Empty state shown

### Edge Cases

- **Empty workspace**: Shows "This folder is empty"
- **Large files (>100MB)**: Preview/download fails with 413 error
- **Binary files**: Shows "Preview not available"
- **Deep nesting**: Tree handles arbitrary depth
- **Special characters in names**: Properly escaped

### Cleanup

```bash
# Remove test files if created
docker exec agent-{name} rm -rf /home/developer/test-dir
```

### Status

Working - Feature implemented 2025-12-27, last verified 2026-01-23

---

## Related Flows

### Upstream

- **Agent Lifecycle** - Agents must be running to browse files
- **Auth0 Authentication** - JWT required for all API calls
- **Agent Sharing** - Shared users can access files

### Downstream

- **Audit Logging** - All file operations logged

### Comparison with File Browser Tab (Historical)

> **Note (2026-02-18)**: The Files tab has been removed from AgentDetail.vue. File Manager is now the only UI for file browsing.

| Feature | File Manager (`/files`) | Files Tab (REMOVED) |
|---------|-------------------------|------------------------|
| Location | Standalone page | ~~Tab in agent detail~~ |
| Agent scope | All running agents | ~~Single agent~~ |
| Agent selector | Dropdown | ~~N/A (implicit)~~ |
| File preview | Rich media support | ~~Download only~~ |
| Delete support | Yes, with confirmation | ~~No~~ |
| **Edit support** | **Yes, for text files** | ~~No~~ |
| **Show hidden files** | **Yes, toggle** | ~~No~~ |
| Protected warnings | Yes | ~~No~~ |
| Persistence | localStorage agent, showHidden | ~~N/A~~ |

---

## Changelog

- **2026-02-18**: Files tab removed from AgentDetail.vue - File Manager is now the primary/only file browsing UI. Updated Overview, Related section, and Comparison table to reflect this change.

- **2026-01-23**: Verified and updated line numbers for all components
  - FileManager.vue now 639 lines (was 494)
  - FileTreeNode.vue now 220 lines (was 186)
  - FilePreview.vue now 245 lines (was 230)
  - Updated router index line numbers (48-52)
  - Updated store action line numbers (452-504)
  - Updated backend router line numbers (500-569)
  - Updated agent server line numbers (157-369)
  - Added TEXT_EXTENSIONS documentation
  - Added _is_edit_protected_path() documentation
  - Verified two-panel layout, media preview types, delete with confirmation

- **2025-12-30**: Updated line numbers to reflect current codebase. Fixed protected path documentation to show split into DELETE_PROTECTED_PATHS and EDIT_PROTECTED_PATHS.

- **2025-12-29**: Hidden files toggle and inline text editing
  - Added "Show hidden" checkbox in header (persisted to localStorage)
  - Agent server and backend accept `show_hidden` query parameter
  - Edit button for text files (js, py, md, json, yaml, etc.)
  - Textarea editor in FilePreview with Save/Cancel actions
  - PUT /api/files endpoint for file updates
  - Unsaved changes warning when switching files or canceling
  - Separate protection lists: delete-protected vs edit-protected
  - CLAUDE.md and .mcp.json are editable (but not deletable)

- **2025-12-27**: Initial implementation (Phase 11.5, Requirement 12.2)
  - Standalone File Manager page at `/files`
  - Two-panel layout with agent selector
  - FileTreeNode recursive component with file type icons
  - FilePreview component with image/video/audio/PDF/text support
  - Delete operations with confirmation modal
  - Protected file warnings (CLAUDE.md, .trinity, .git, .env, etc.)
  - Backend preview and delete endpoints
  - Agent server preview and delete handlers
  - Search/filter with auto-expand
  - localStorage agent persistence
