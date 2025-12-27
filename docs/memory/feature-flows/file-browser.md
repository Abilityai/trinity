# Feature: File Browser (Tree Structure)

> **Updated**: 2025-12-27 - Refactored to service layer architecture. File operations moved to `services/agent_service/files.py`.

## Overview
Users can browse and download files from agent workspaces through the Trinity web UI without requiring SSH access. The feature displays files in a **hierarchical tree structure** similar to macOS Finder, with expandable/collapsible folders. Users can navigate folder hierarchies, search files, and download individual files.

## User Story
As a Trinity user, I want to browse files in my agent's workspace using a familiar folder tree interface so that I can easily navigate directory structures and access agent-generated artifacts without needing SSH or Docker command-line access.

## Entry Points
- **UI**: `src/frontend/src/views/AgentDetail.vue:275` - "Files" tab button
- **API**: `GET /api/agents/{agent_name}/files`
- **API**: `GET /api/agents/{agent_name}/files/download`

---

## Frontend Layer

### Components
**File**: `/Users/eugene/Dropbox/Coding/N8N_Main_repos/project_trinity/src/frontend/src/views/AgentDetail.vue`

#### Tab Button (Line 275-285)
```vue
<button
  @click="activeTab = 'files'"
  :class="[
    'px-6 py-3 border-b-2 font-medium text-sm transition-colors',
    activeTab === 'files'
      ? 'border-indigo-500 text-indigo-600'
      : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
  ]"
>
  Files
</button>
```

#### Files Tab Content (Line 765-830)
- **Guard**: Shows "Agent must be running" message if status !== 'running'
- **Search Box**: `v-model="fileSearchQuery"` - filters files by name, auto-expands matching folders
- **File Tree**: Recursive FileTreeNode components displaying:
  - **Folders**: Chevron icon (rotates when expanded), folder icon (color changes), file count badge
  - **Files**: File icon, name, size, download button (visible on hover)
  - Proper indentation (20px per level)
  - Collapsed by default, click to expand/collapse
- **States**: Loading spinner, error display, empty state, refresh button

### State Management (Line 1100-1142)

**File**: `/Users/eugene/Dropbox/Coding/N8N_Main_repos/project_trinity/src/frontend/src/views/AgentDetail.vue`

```javascript
// File browser state
const fileTree = ref([])               // Hierarchical tree structure
const filesLoading = ref(false)
const filesError = ref(null)
const fileSearchQuery = ref('')
const expandedFolders = ref(new Set()) // Track which folders are open
const totalFileCount = ref(0)

const filteredFileTree = computed(() => {
  if (!fileSearchQuery.value) return fileTree.value

  const query = fileSearchQuery.value.toLowerCase()

  const filterTree = (items) => {
    return items.filter(item => {
      if (item.type === 'file') {
        return item.name.toLowerCase().includes(query)
      } else {
        // For directories, include if name matches or has matching children
        const nameMatches = item.name.toLowerCase().includes(query)
        const filteredChildren = filterTree(item.children || [])
        if (nameMatches || filteredChildren.length > 0) {
          // Auto-expand folders when searching
          if (fileSearchQuery.value) {
            expandedFolders.value.add(item.path)
          }
          return true
        }
        return false
      }
    }).map(item => {
      if (item.type === 'directory') {
        return {
          ...item,
          children: filterTree(item.children || [])
        }
      }
      return item
    })
  }

  return filterTree(fileTree.value)
})
```

### FileTreeNode Component (Line 866-993)

Recursive component using Vue's `h()` render function:

```javascript
const FileTreeNode = defineComponent({
  name: 'FileTreeNode',
  props: {
    item: Object,           // Tree node (file or directory)
    depth: Number,          // Nesting level for indentation
    searchQuery: String,    // Current search query
    expandedFolders: Set   // Set of expanded folder paths
  },
  emits: ['toggle-folder', 'download'],
  setup(props, { emit }) {
    // Renders folder with expand/collapse or file with download button
    // Recursively renders children when folder is expanded
  }
})
```

### Functions (Line 1689-1713)

#### loadFiles()
```javascript
const loadFiles = async () => {
  if (!agent.value || agent.value.status !== 'running') return
  filesLoading.value = true
  filesError.value = null
  try {
    const response = await agentsStore.listAgentFiles(agent.value.name)
    fileTree.value = response.tree || []         // Tree structure
    totalFileCount.value = response.total_files || 0
  } catch (err) {
    console.error('Failed to load files:', err)
    filesError.value = err.response?.data?.detail || 'Failed to load files'
  } finally {
    filesLoading.value = false
  }
}

const toggleFolder = (path) => {
  if (expandedFolders.value.has(path)) {
    expandedFolders.value.delete(path)
  } else {
    expandedFolders.value.add(path)
  }
  // Trigger reactivity
  expandedFolders.value = new Set(expandedFolders.value)
}
```

#### downloadFile(filePath, fileName)
```javascript
const downloadFile = async (filePath, fileName) => {
  if (!agent.value) return
  try {
    const content = await agentsStore.downloadAgentFile(agent.value.name, filePath)
    // Create blob and download
    const blob = new Blob([content], { type: 'text/plain' })
    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = fileName
    document.body.appendChild(a)
    a.click()
    window.URL.revokeObjectURL(url)
    document.body.removeChild(a)
    showNotification(`Downloaded ${fileName}`, 'success')
  } catch (err) {
    console.error('Failed to download file:', err)
    showNotification(err.response?.data?.detail || 'Failed to download file', 'error')
  }
}
```

#### Helper Functions
- `formatFileSize(bytes)` - Converts bytes to B/KB/MB/GB
- `formatDate(dateString)` - Relative time (e.g., "2h ago", "3d ago")

### Watchers (Line 1006-1010)

```javascript
// Watch for Files tab activation to load files
watch(activeTab, (newTab) => {
  if (newTab === 'files' && agent.value?.status === 'running') {
    loadFiles()
  }
})
```

### Store Actions

**File**: `/Users/eugene/Dropbox/Coding/N8N_Main_repos/project_trinity/src/frontend/src/stores/agents.js`

#### listAgentFiles (Line 312-319)
```javascript
async listAgentFiles(name, path = '/home/developer') {
  const authStore = useAuthStore()
  const response = await axios.get(`/api/agents/${name}/files`, {
    params: { path },
    headers: authStore.authHeader
  })
  return response.data  // Returns: { tree: [...], total_files: N }
}
```

#### downloadAgentFile (Line 321-329)
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

---

## Backend Layer

### Architecture (Post-Refactoring)

The file browser feature uses a **thin router + service layer** architecture:

| Layer | File | Purpose |
|-------|------|---------|
| Router | `src/backend/routers/agents.py:547-566` | Endpoint definitions |
| Service | `src/backend/services/agent_service/files.py` (137 lines) | File listing and download logic |

### Endpoints

#### GET /api/agents/{agent_name}/files

**Router**: `src/backend/routers/agents.py:547-555`
**Service**: `src/backend/services/agent_service/files.py:20-77`

**Purpose**: List all files in agent workspace as hierarchical tree structure

**Parameters**:
- `agent_name` (path) - Agent identifier
- `path` (query, optional) - Directory path (default: `/home/developer`)

**Business Logic** (in `list_agent_files_logic()`):
1. Check user authentication (`get_current_user` dependency)
2. Verify user has access to agent (`db.can_user_access_agent()`)
3. Get agent container (`get_agent_container()`)
4. Verify container exists (404 if not)
5. Check container is running (400 if not)
6. Proxy request to agent's internal API at `http://agent-{name}:8000/api/files`
7. Log audit event on success/error
8. Return hierarchical tree JSON

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
  "total_files": 6
}
```

**Audit Logging**:
```python
await log_audit_event(
    event_type="file_access",
    action="file_list",
    user_id=current_user.username,
    agent_name=agent_name,
    ip_address=request.client.host,
    metadata={"path": path},
    result="success"  # or "error"
)
```

#### GET /api/agents/{agent_name}/files/download

**Router**: `src/backend/routers/agents.py:558-566`
**Service**: `src/backend/services/agent_service/files.py:80-137`

**Purpose**: Download file content from agent workspace

**Parameters**:
- `agent_name` (path) - Agent identifier
- `path` (query, required) - File path relative to workspace

**Business Logic** (in `download_agent_file_logic()`):
1. Check user authentication
2. Verify user has access to agent
3. Get agent container
4. Verify container exists and is running
5. Proxy request to agent's internal API at `http://agent-{name}:8000/api/files/download`
6. Log audit event with file_path in metadata
7. Return file content as PlainTextResponse

**Response**: Plain text content of the file

**Audit Logging**:
```python
await log_audit_event(
    event_type="file_access",
    action="file_download",
    user_id=current_user.username,
    agent_name=agent_name,
    ip_address=request.client.host,
    metadata={"file_path": path},
    result="success"  # or "error"
)
```

---

## Agent Layer

> **Architecture Change (2025-12-06)**: The agent-server has been refactored from a monolithic file into a modular package structure at `docker/base-image/agent_server/`.

### Agent Server Endpoints

**File**: `/Users/eugene/Dropbox/Coding/N8N_Main_repos/project_trinity/docker/base-image/agent_server/routers/files.py`

#### GET /api/files (Line 15-96)

**Purpose**: Recursively list files in workspace directory as hierarchical tree

**Parameters**:
- `path` (query, optional) - Directory to list (default: `/home/developer`)

**Security**:
- Only allows access to `/home/developer` and subdirectories
- Path traversal protection via `Path.resolve()` and prefix check
- Automatically skips hidden files/directories (starting with `.`)

**Business Logic**:
1. Resolve requested path and validate it's within workspace
2. Check path exists (404 if not)
3. Call recursive `build_tree(directory)` function:
   - Lists directory contents
   - Sorts items (directories first, then files, alphabetically)
   - For directories: recursively builds subtree
   - For files: collects metadata
   - Returns `{"children": [...], "file_count": N}`
4. Return structured tree response

**build_tree() Function** (`agent_server/routers/files.py:34-82`):
```python
def build_tree(directory: Path, base_path: Path) -> dict:
    """Build a hierarchical tree structure from a directory."""
    items = []
    total_files = 0

    dir_items = sorted(directory.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))

    for item in dir_items:
        if item.name.startswith('.'):  # Skip hidden
            continue

        if item.is_dir():
            subtree = build_tree(item, base_path)  # Recursive
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
```python
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
    "total_files": 3
}
```

#### GET /api/files/download (Line 99-140)

**Purpose**: Download file content as plain text

**Parameters**:
- `path` (query, required) - File path (absolute or relative to workspace)

**Security**:
- Only allows access to `/home/developer`
- Path traversal protection
- Max file size: 100MB (413 error if exceeded)
- Verifies path is a file (400 if directory)

**Business Logic**:
1. Handle both absolute and relative paths
2. Resolve path and validate workspace access
3. Check file exists (404 if not)
4. Verify it's a file, not directory (400 if directory)
5. Check file size <= 100MB (413 if too large)
6. Read file as UTF-8 text (with error replacement for binary)
7. Return as PlainTextResponse

**Response**: Plain text content

**Error Handling**:
- 403: Access denied (outside workspace)
- 404: File not found
- 400: Not a file (is directory)
- 413: File too large (>100MB)
- 500: Read error

---

## Data Flow

### File List Request Flow
```
User clicks "Files" tab
  |
activeTab watcher triggers
  |
loadFiles() called
  |
agentsStore.listAgentFiles(name)
  |
GET /api/agents/{name}/files (backend)
  |
Authorization check (JWT + ownership)
  |
GET http://agent-{name}:8000/api/files (agent-server)
  |
Walk /home/developer
  |
Filter hidden files/dirs
  |
Collect metadata (name, path, size, modified)
  |
Return JSON array
  |
Audit log: file_list
  |
Display files in UI with search/filter
```

### File Download Flow
```
User clicks download icon
  |
downloadFile(path, name) called
  |
agentsStore.downloadAgentFile(name, path)
  |
GET /api/agents/{name}/files/download?path=... (backend)
  |
Authorization check
  |
GET http://agent-{name}:8000/api/files/download?path=... (agent-server)
  |
Validate path, check size
  |
Read file content (UTF-8)
  |
Return PlainTextResponse
  |
Audit log: file_download with file_path
  |
Create Blob in browser
  |
Trigger download via <a> element
  |
Show success notification
```

---

## Side Effects

### Audit Logs
**Event Type**: `file_access`

**Actions**:
1. `file_list` - User listed files in workspace
   - Metadata: `{"path": "/home/developer"}`
2. `file_download` - User downloaded a specific file
   - Metadata: `{"file_path": "path/to/file.txt"}`

**Fields Logged**:
- `user_id`: Username (from JWT)
- `agent_name`: Agent identifier
- `ip_address`: Client IP
- `timestamp`: ISO 8601 timestamp
- `result`: "success" or "error"
- `metadata`: Additional context

### No Database Operations
This feature is read-only and does not modify any database tables.

### No WebSocket Broadcasts
This feature does not emit real-time events.

---

## Error Handling

| Error Case | HTTP Status | Message | Where |
|------------|-------------|---------|-------|
| Agent not found | 404 | "Agent not found" | Backend |
| Agent not running | 400 | "Agent must be running to browse/download files" | Backend |
| No access permission | 403 | "You don't have permission to access this agent" | Backend |
| Path outside workspace | 403 | "Access denied: only /home/developer accessible" | Agent Server |
| File not found | 404 | "File not found: {path}" | Agent Server |
| Path is directory | 400 | "Not a file: {path}" | Agent Server |
| File too large | 413 | "File too large: {size} bytes (max 104857600)" | Agent Server |
| File read error | 500 | "Failed to read file: {error}" | Agent Server |
| Network timeout (list) | 504 | "File listing timed out" | Backend (30s) |
| Network timeout (download) | 504 | "File download timed out" | Backend (60s) |

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
- Hidden files (.env, .git) automatically skipped
- Hidden directories (.git, .vscode) not traversed
- Max file size: 100MB download limit
- Only text file content (binary treated as text with error replacement)

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
**Action**: Navigate to agent detail page -> Click "Files" tab
**Expected**:
- Loading spinner appears briefly
- File list loads showing workspace contents
- Each file shows: name, path, size, modified time

**Verify**:
```bash
# Check audit logs
docker-compose logs audit-logger | grep file_list
```

#### 2. Search/Filter Files
**Action**: Type "test" in search box
**Expected**:
- File list filters in real-time
- Only files with "test" in name or path shown
- Counter updates (e.g., "5 file(s)")

#### 3. File Download
**Action**: Click download icon on a file
**Expected**:
- Browser downloads file with original filename
- Success notification appears
- File content matches workspace file

**Verify**:
```bash
# Compare downloaded file with agent workspace file
docker exec agent-{name} cat /home/developer/path/to/file.txt
```

#### 4. Stopped Agent Guard
**Action**: Stop agent, click "Files" tab
**Expected**:
- Empty state with message "Agent must be running to browse files"
- No API calls made
- No errors in console

#### 5. Large File Handling
**Action**: Create 101MB file in workspace, try to download
**Expected**:
- Download fails with error notification
- Error message mentions file size limit
- No browser hang or timeout

**Setup**:
```bash
docker exec agent-{name} dd if=/dev/zero of=/home/developer/large.bin bs=1M count=101
```

#### 6. Permission Checks
**Action**: Try to access agent owned by another user
**Expected**:
- Backend returns 403 Forbidden
- Error notification appears
- No file data exposed

### Edge Cases
- **Empty workspace**: Shows "No files found in workspace"
- **Hidden files**: .env, .git files not shown in list
- **Binary files**: Download works but content may be garbled (treated as text)
- **Network error**: Shows error with retry button
- **Path with spaces**: Handles correctly (URL encoding)

### Cleanup
```bash
# Remove test files
docker exec agent-{name} rm /home/developer/large.bin
```

### Status
Working - Feature tested and operational as of 2025-12-01

---

## Related Flows

### Upstream
- **Agent Lifecycle** - Agent must be running to browse files
- **Auth0 Authentication** - JWT required for API access
- **Agent Sharing** - Shared users can browse files too

### Downstream
- **Audit Logging** - All file operations logged for compliance
- None (read-only feature, no downstream dependencies)

### Similar Features
- **Agent Logs & Telemetry** - Also provides read-only agent data view
- **GitHub Sync** - Another way to extract agent workspace content

---

## Future Enhancements

### Potential Improvements
1. **Upload Files**: Allow file upload to workspace
2. **File Preview**: Show content preview for small text files
3. **Bulk Download**: Zip multiple files or entire directory
4. **File Actions**: Delete, rename, move files
5. **Syntax Highlighting**: Code preview with language detection
6. **Image Preview**: Display images inline
7. **File Versioning**: Track file changes over time
8. **Remember expanded state**: Save which folders were expanded across sessions

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

### Why Plain Text?
- Simplifies implementation (no MIME type detection)
- Covers 90% of use cases (logs, code, configs)
- Browser can handle text encoding issues
- Future enhancement can add proper binary support

### Why 100MB Limit?
- Prevents memory issues in Python/JavaScript
- Protects against abuse (downloading GB files)
- Large files should use alternative methods (SSH, Docker cp)

### Why Skip Hidden Files?
- Prevents accidental exposure of secrets (.env)
- Reduces clutter in file list
- Git repositories (.git) can be huge
- User can SSH if they need hidden files

### Why Recursive Tree vs Flat List?
- **Hierarchical structure**: Mirrors actual filesystem organization
- **Familiar UX**: Similar to macOS Finder, Windows Explorer
- **Scalability**: Better for large workspaces (only show what's expanded)
- **Navigation**: Easy to drill down into specific directories
- **Visual clarity**: Indentation shows hierarchy at a glance

---

## Changelog

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
