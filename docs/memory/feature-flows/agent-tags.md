# Feature: Agent Tags & System Views (ORG-001)

## Overview

Lightweight agent organization system with four phases:
- **Phase 1 (Tags)**: Agents can have multiple tags for flexible categorization
- **Phase 2 (System Views)**: Saved filters that group agents by tags, displayed in Dashboard sidebar
- **Phase 3 (Polish)**: MCP tools, quick tag filter in Dashboard header, bulk tag operations on Agents page
- **Phase 4 (System Manifest Integration)**: Auto-apply system prefix as tag, default_tags in manifest, auto-create System View

Tags are normalized (lowercase, alphanumeric + hyphens). System Views appear in a collapsible sidebar on the Dashboard. When a view is selected, the Dashboard filters to show only agents matching the view's tags (OR logic).

## User Story

As a Trinity platform user, I want to:
- Tag agents with descriptive labels so I can organize them into logical systems
- Save tag-based filters as "System Views" for quick access to agent groups
- Share System Views with other users for team collaboration
- Use MCP tools to programmatically manage tags from Claude Code
- Quickly filter agents by tag directly in the Dashboard header
- Apply bulk tag operations to multiple agents at once

---

## Architecture

```
                         ┌─────────────────────────────────────────────────────────────┐
                         │                     FRONTEND                                │
                         │  Dashboard.vue ─────────────────────► SystemViewsSidebar.vue│
                         │       │                                      │              │
                         │       ├── Quick Tag Filter (header pills)    │              │
                         │       ├── SystemViewEditor.vue (modal)   ◄───┘              │
                         │       └── network.js:setFilterTags() ───────────────────────┤
                         │                                                             │
                         │  AgentListPanel.vue (Dashboard List mode, ent#260) ─────────┤
                         │       ├── Bulk selection checkboxes                         │
                         │       └── Add/Remove tag bulk actions                       │
                         │       (tag FILTERING migrated to the chassis quick-tag      │
                         │        filter above — the list-local dropdown is gone)      │
                         │                                                             │
                         │  AgentDetail.vue ──► AgentHeader.vue ──► TagsEditor.vue     │
                         │       └── loadTags(), addTag(), removeTag()                 │
                         └─────────────────────────────────────────────────────────────┘
                                                    │
                                                    ▼
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│                                     BACKEND API                                           │
│                                                                                           │
│  Tags API (routers/tags.py)              │    System Views API (routers/system_views.py) │
│  ─────────────────────────────────────── │    ────────────────────────────────────────── │
│  GET  /api/tags                          │    GET  /api/system-views                     │
│  GET  /api/agents/{name}/tags            │    POST /api/system-views                     │
│  PUT  /api/agents/{name}/tags            │    GET  /api/system-views/{id}                │
│  POST /api/agents/{name}/tags/{tag}      │    PUT  /api/system-views/{id}                │
│  DELETE /api/agents/{name}/tags/{tag}    │    DELETE /api/system-views/{id}              │
│                                                                                           │
│  Agents API (routers/agents.py)                                                          │
│  ───────────────────────────────                                                         │
│  GET /api/agents?tags=a,b,c  (OR logic filtering)                                        │
└───────────────────────────────────────────────────────────────────────────────────────────┘
                                                    │
                                                    ▼
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│                                   DATABASE LAYER                                          │
│                                                                                           │
│  TagOperations (db/tags.py)              │    SystemViewOperations (db/system_views.py)  │
│  ─────────────────────────────────────── │    ────────────────────────────────────────── │
│  get_agent_tags()                        │    create_view()                              │
│  set_agent_tags()                        │    get_view()                                 │
│  add_tag()                               │    list_user_views()                          │
│  remove_tag()                            │    update_view()                              │
│  list_all_tags()                         │    delete_view()                              │
│  get_agents_by_tags()                    │    can_user_edit_view()                       │
│  get_tags_for_agents()                   │    _row_to_view() with agent_count            │
└───────────────────────────────────────────────────────────────────────────────────────────┘
                                                    │
                                                    ▼
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│                                  MCP SERVER (Phase 3)                                     │
│                                                                                           │
│  tools/tags.ts (5 tools)                 │    client.ts (API methods)                    │
│  ─────────────────────────────────────── │    ────────────────────────────────────────── │
│  list_tags                               │    listAllTags()                              │
│  get_agent_tags                          │    getAgentTags()                             │
│  tag_agent                               │    addAgentTag()                              │
│  untag_agent                             │    removeAgentTag()                           │
│  set_agent_tags                          │    setAgentTags()                             │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Phase 1: Agent Tags

### Entry Points

| Entry Point | Location | Description |
|-------------|----------|-------------|
| **Tags Row** | `src/frontend/src/components/AgentHeader.vue:150-161` | TagsEditor component in agent header |
| **Filter Query** | `GET /api/agents?tags=a,b,c` | Filter agents list by tags (OR logic) |
| **List All Tags** | `GET /api/tags` | Get all unique tags with counts |

### Frontend Layer

#### TagsEditor.vue (`src/frontend/src/components/TagsEditor.vue`)

Reusable tag editor with inline editing and autocomplete.

| Line | Element | Description |
|------|---------|-------------|
| 1-77 | `<template>` | Tag pills with remove buttons, add button, input with dropdown |
| 6-21 | Tag pills | Purple rounded badges with `#tag` text and X button |
| 11-20 | Remove button | Calls `removeTag(tag)` on click |
| 23-34 | Add button | Shows when editable, opens input on click |
| 36-62 | Input with autocomplete | Text input with dropdown suggestions |
| 48-61 | Suggestions dropdown | Filters `allTags` prop, max 5 results |
| 79-95 | Props | `modelValue` (array), `editable` (bool), `allTags` (array) |
| 97 | Emits | `update:modelValue`, `add`, `remove` |
| 106-112 | `filteredSuggestions` | Computed - filters allTags by search, excludes existing |
| 122-152 | `addTag()` | Validates format, emits add event |
| 154-158 | `removeTag(tag)` | Filters out tag, emits remove event |
| 160-163 | `selectSuggestion(tag)` | Selects from autocomplete dropdown |

#### AgentHeader.vue (`src/frontend/src/components/AgentHeader.vue`)

Displays tags row in agent detail header.

| Line | Element | Description |
|------|---------|-------------|
| 150-161 | Tags row | "Tags:" label + TagsEditor component |
| 153-160 | TagsEditor | Binds `tags`, `editable`, `allTags` props |
| 157-159 | Events | Emits `update-tags`, `add-tag`, `remove-tag` |
| 258 | Import | `TagsEditor` component import |
| 332-341 | Props | `tags` (Array), `allTags` (Array) |
| 352-354 | Emits | `update-tags`, `add-tag`, `remove-tag` |

#### AgentDetail.vue (`src/frontend/src/views/AgentDetail.vue`)

Manages tags state and API integration for agent detail page.

**Bug Fix (2026-02-18)**: API calls now use `axios` with `authStore.authHeader` instead of the Pinia store's `api` wrapper to ensure proper authentication headers are sent.

| Line | Element | Description |
|------|---------|-------------|
| 46-47 | Props to AgentHeader | `:tags="agentTags"`, `:all-tags="allTags"` |
| 56-58 | Events from AgentHeader | `@update-tags`, `@add-tag`, `@remove-tag` |
| 282-283 | State refs | `agentTags` and `allTags` reactive refs |
| 548-558 | `loadTags()` | Fetches agent's tags via `GET /api/agents/{name}/tags` |
| 560-569 | `loadAllTags()` | Fetches all tags for autocomplete via `GET /api/tags` |
| 571-583 | `updateTags(newTags)` | Bulk replace via `PUT /api/agents/{name}/tags` |
| 585-598 | `addTag(tag)` | Add single tag via `POST /api/agents/{name}/tags/{tag}` |
| 600-611 | `removeTag(tag)` | Remove tag via `DELETE /api/agents/{name}/tags/{tag}` |
| 635 | resetTags | Clear tags when navigating to different agent |
| 645 | loadTags | Called after agent navigation |
| 714-715 | onMounted | Calls `loadTags()` and `loadAllTags()` |

### API Calls (Tags)

```javascript
// Load agent's tags (uses axios + authStore.authHeader)
const response = await axios.get(`/api/agents/${agent.value.name}/tags`, {
  headers: authStore.authHeader
})
agentTags.value = response.data.tags || []

// Load all tags for autocomplete
const response = await axios.get('/api/tags', {
  headers: authStore.authHeader
})
allTags.value = (response.data.tags || []).map(t => t.tag)

// Bulk replace tags
await axios.put(`/api/agents/${name}/tags`, { tags: newTags }, {
  headers: authStore.authHeader
})

// Add single tag
await axios.post(`/api/agents/${name}/tags/${tag}`, {}, {
  headers: authStore.authHeader
})

// Remove single tag
await axios.delete(`/api/agents/${name}/tags/${tag}`, {
  headers: authStore.authHeader
})
```

### Backend Layer (Tags)

#### API Router (`src/backend/routers/tags.py`)

5 endpoints for tag management.

| Line | Endpoint | Method | Description |
|------|----------|--------|-------------|
| 22-30 | `/api/tags` | GET | List all unique tags with counts |
| 37-49 | `/api/agents/{name}/tags` | GET | Get agent's tags |
| 52-66 | `/api/agents/{name}/tags` | PUT | Replace all tags |
| 69-92 | `/api/agents/{name}/tags/{tag}` | POST | Add single tag |
| 95-108 | `/api/agents/{name}/tags/{tag}` | DELETE | Remove single tag |

#### Authorization (Tags)

| Endpoint | Dependency | Access Level |
|----------|------------|--------------|
| `GET /api/tags` | `get_current_user` | Any authenticated user |
| `GET /api/agents/{name}/tags` | `AuthorizedAgentByName` | Owner, shared users, or admin |
| `PUT /api/agents/{name}/tags` | `OwnedAgentByName` | Owner or admin only |
| `POST /api/agents/{name}/tags/{tag}` | `OwnedAgentByName` | Owner or admin only |
| `DELETE /api/agents/{name}/tags/{tag}` | `OwnedAgentByName` | Owner or admin only |

#### Validation Rules (lines 82-89)

```python
# Tag format validation
normalized = tag.lower().strip()
if not normalized:
    raise HTTPException(status_code=400, detail="Tag cannot be empty")
if len(normalized) > 50:
    raise HTTPException(status_code=400, detail="Tag too long (max 50 characters)")
if not all(c.isalnum() or c == '-' for c in normalized):
    raise HTTPException(status_code=400, detail="Tags can only contain letters, numbers, and hyphens")
```

#### Agents List Filtering (`src/backend/routers/agents.py:132-167`)

| Line | Function | Description |
|------|----------|-------------|
| 135 | `tags` param | Query parameter for comma-separated tags |
| 153-158 | Tag filtering | OR logic - agents with ANY matching tag |
| 160-165 | Batch tag fetch | Adds `tags` array to each agent response |

```python
# Filter by tags (OR logic)
if tags:
    tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]
    if tag_list:
        matching_agents = set(db.get_agents_by_tags(tag_list))
        agents = [a for a in agents if a.get("name") in matching_agents]

# Add tags to response
agent_names = [a.get("name") for a in agents]
all_tags = db.get_tags_for_agents(agent_names)
for agent in agents:
    agent["tags"] = all_tags.get(agent.get("name"), [])
```

### Data Layer (Tags)

#### Database Schema (`src/backend/database.py:727-736`)

```sql
CREATE TABLE IF NOT EXISTS agent_tags (
    agent_name TEXT NOT NULL,
    tag TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (agent_name, tag),
    FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name) ON DELETE CASCADE
)
```

#### Indexes (`src/backend/database.py:792-794`)

```sql
CREATE INDEX IF NOT EXISTS idx_agent_tags_tag ON agent_tags(tag)
CREATE INDEX IF NOT EXISTS idx_agent_tags_agent ON agent_tags(agent_name)
```

#### TagOperations Class (`src/backend/db/tags.py`)

| Line | Method | Description |
|------|--------|-------------|
| 19-27 | `get_agent_tags(agent_name)` | Get tags for agent, sorted alphabetically |
| 29-61 | `set_agent_tags(agent_name, tags)` | Replace all tags (delete + insert) |
| 63-89 | `add_tag(agent_name, tag)` | Add single tag with `INSERT OR IGNORE` |
| 91-112 | `remove_tag(agent_name, tag)` | Delete single tag |
| 114-132 | `list_all_tags()` | Get all unique tags with counts, sorted by count DESC |
| 134-152 | `get_agents_by_tag(tag)` | Get agent names with specific tag |
| 154-179 | `get_agents_by_tags(tags)` | Get agents with ANY of specified tags (OR logic) |
| 181-194 | `delete_agent_tags(agent_name)` | Remove all tags (for agent deletion) |
| 196-222 | `get_tags_for_agents(agent_names)` | Batch fetch tags for multiple agents |

---

## Phase 2: System Views

### Entry Points

| Entry Point | Location | Description |
|-------------|----------|-------------|
| **Sidebar** | `src/frontend/src/views/Dashboard.vue:7-10` | SystemViewsSidebar in Dashboard |
| **List Views** | `GET /api/system-views` | Get user's accessible views |
| **Create View** | `POST /api/system-views` | Create new system view |
| **Edit View** | `PUT /api/system-views/{id}` | Update existing view |
| **Delete View** | `DELETE /api/system-views/{id}` | Delete a view |

### Frontend Layer (System Views)

#### SystemViewsSidebar.vue (`src/frontend/src/components/SystemViewsSidebar.vue`)

Collapsible sidebar displaying system views on Dashboard.

| Line | Element | Description |
|------|---------|-------------|
| 1-110 | `<template>` | Sidebar with header, views list, create button |
| 9-25 | Header | "Systems" title with collapse toggle button |
| 29-43 | All Agents | Default option to show all agents (clears filter) |
| 53-90 | Views List | Iterates sorted views with icon, name, count, edit button |
| 66-71 | View Icon | Shows emoji/icon with optional color styling |
| 74-75 | View Info | Name (truncated) and agent count badge |
| 78-88 | Edit Button | Hover-visible edit icon, emits `edit` event |
| 94-108 | Create Button | "New View" button at sidebar footer |
| 112-144 | `<script setup>` | Store integration, collapse persistence |
| 122-130 | Collapse State | Defaults to collapsed, persisted to localStorage (`trinity-sidebar-collapsed`). Uses `toggleCollapse()` function for proper persistence. |
| 136-138 | `selectView()` | Delegates to store's `selectView()` |
| 140-143 | `onMounted` | Initialize from localStorage, fetch views |

#### SystemViewEditor.vue (`src/frontend/src/components/SystemViewEditor.vue`)

Modal form for creating/editing system views.

| Line | Element | Description |
|------|---------|-------------|
| 1-227 | `<template>` | Modal overlay with form |
| 24-38 | Name Input | Required, max 100 characters |
| 40-52 | Description | Optional text input |
| 54-98 | Icon & Color | Emoji picker (12 options), color picker (8 options) |
| 100-161 | Filter Tags | Tag input with autocomplete from existing tags |
| 124-136 | Available Tags | Shows existing tags with counts for quick add |
| 139-156 | Selected Tags | Blue pills with remove buttons |
| 163-188 | Shared Toggle | Switch to make view visible to all users |
| 196-223 | Actions | Delete (edit mode), Cancel, Create/Update buttons |
| 243-250 | Form State | Reactive form with defaults |
| 257-258 | UI Options | `commonEmojis` array, `colorOptions` array |
| 267-278 | Watch editingView | Populate form when editing existing view |
| 281-289 | Watch isOpen | Reset form, fetch available tags on open |
| 291-298 | `fetchAvailableTags()` | Calls `GET /api/tags` for autocomplete |
| 310-316 | `addTag()` | Normalizes and adds tag to filter_tags |
| 328-357 | `handleSubmit()` | Creates or updates view via store |
| 359-378 | `handleDelete()` | Confirms and deletes view |

#### systemViews.js Store (`src/frontend/src/stores/systemViews.js`)

Pinia store for system views state management.

| Line | Element | Description |
|------|---------|-------------|
| 7-10 | State | `views`, `activeViewId`, `isLoading`, `error` |
| 13-16 | `activeView` | Computed - finds view by activeViewId |
| 18-20 | `activeFilterTags` | Computed - returns active view's filter_tags or [] |
| 22-24 | `sortedViews` | Computed - views sorted alphabetically |
| 27-39 | `fetchViews()` | GET /api/system-views |
| 41-55 | `createView()` | POST /api/system-views |
| 57-74 | `updateView()` | PUT /api/system-views/{id} |
| 76-92 | `deleteView()` | DELETE /api/system-views/{id}, clears selection if deleted |
| 94-102 | `selectView()` | Sets activeViewId, persists to localStorage |
| 104-107 | `clearSelection()` | Clears activeViewId, removes from localStorage |
| 109-115 | `initialize()` | Restores activeViewId from localStorage |

#### Dashboard.vue Integration (`src/frontend/src/views/Dashboard.vue`)

| Line | Element | Description |
|------|---------|-------------|
| 7-10 | SystemViewsSidebar | Sidebar component with create/edit event handlers |
| 412-418 | SystemViewEditor | Modal component for create/edit |
| 426-427 | Imports | SystemViewsSidebar, SystemViewEditor |
| 451 | Store | `useSystemViewsStore()` |
| 518-525 | Watch activeFilterTags | Calls `networkStore.setFilterTags(tags)` on change |

#### network.js Store Integration (`src/frontend/src/stores/network.js`)

| Line | Element | Description |
|------|---------|-------------|
| 123 | `filterTags` | Ref for current tag filter |
| 126-128 | `setFilterTags(tags)` | Updates filterTags, triggers fetchAgents() |
| 134-136 | fetchAgents params | Adds `?tags=` query param when filterTags set |

### API Calls (System Views)

```javascript
// List user's views (owned + shared)
const response = await axios.get('/api/system-views')
views.value = response.data.views || []

// Create new view
const response = await axios.post('/api/system-views', {
  name: 'Due Diligence Team',
  description: 'Agents for DD workflows',
  icon: '?',
  color: '#8B5CF6',
  filter_tags: ['due-diligence', 'research'],
  is_shared: false
})

// Update view
await axios.put(`/api/system-views/${viewId}`, {
  name: 'Updated Name',
  filter_tags: ['tag1', 'tag2']
})

// Delete view
await axios.delete(`/api/system-views/${viewId}`)
```

### Backend Layer (System Views)

#### API Router (`src/backend/routers/system_views.py`)

5 endpoints for system view management.

| Line | Endpoint | Method | Description |
|------|----------|--------|-------------|
| 24-33 | `/api/system-views` | GET | List views (owned + shared) |
| 36-63 | `/api/system-views` | POST | Create view |
| 66-85 | `/api/system-views/{id}` | GET | Get single view |
| 88-124 | `/api/system-views/{id}` | PUT | Update view |
| 127-147 | `/api/system-views/{id}` | DELETE | Delete view |

#### Authorization (System Views)

| Endpoint | Dependency | Access Level |
|----------|------------|--------------|
| `GET /api/system-views` | `get_current_user` | Any authenticated user |
| `POST /api/system-views` | `get_current_user` | Any authenticated user |
| `GET /api/system-views/{id}` | `get_current_user` | Owner, shared views, or admin |
| `PUT /api/system-views/{id}` | `get_current_user` | Owner or admin only |
| `DELETE /api/system-views/{id}` | `get_current_user` | Owner or admin only |

#### Validation Rules

```python
# Name validation
if not data.name or not data.name.strip():
    raise HTTPException(status_code=400, detail="View name is required")
if len(data.name) > 100:
    raise HTTPException(status_code=400, detail="View name too long (max 100 characters)")

# Tags validation
if not data.filter_tags or len(data.filter_tags) == 0:
    raise HTTPException(status_code=400, detail="At least one filter tag is required")

# Color validation
if data.color and not data.color.startswith("#"):
    raise HTTPException(status_code=400, detail="Color must be a hex code (e.g., #8B5CF6)")
```

### Data Layer (System Views)

#### Database Schema (`src/backend/database.py:740-755`)

```sql
CREATE TABLE IF NOT EXISTS system_views (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    icon TEXT,
    color TEXT,
    filter_tags TEXT NOT NULL,  -- JSON array
    owner_id TEXT NOT NULL,
    is_shared INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
)
```

#### Indexes (`src/backend/database.py:818`)

```sql
CREATE INDEX IF NOT EXISTS idx_system_views_owner ON system_views(owner_id)
```

#### SystemViewOperations Class (`src/backend/db/system_views.py`)

| Line | Method | Description |
|------|--------|-------------|
| 20-58 | `create_view(owner_id, data)` | Insert new view, returns SystemView |
| 60-78 | `get_view(view_id)` | Get view by ID with owner email join |
| 80-107 | `list_user_views(user_id)` | Get owned + shared views, sorted by name |
| 109-127 | `list_all_views()` | Get all views (admin use) |
| 129-187 | `update_view(view_id, data)` | Partial update, only provided fields |
| 189-203 | `delete_view(view_id)` | Delete view, returns success bool |
| 205-211 | `get_view_owner(view_id)` | Get owner_id for authorization |
| 213-218 | `can_user_edit_view()` | Check if user can edit (owner or admin) |
| 220-228 | `can_user_view()` | Check if user can view (owner or shared) |
| 230-259 | `_row_to_view()` | Convert DB row to SystemView with agent count |

#### Agent Count Calculation (lines 234-244)

```python
# Count agents matching any of the filter tags
agent_count = 0
if filter_tags:
    placeholders = ",".join("?" * len(filter_tags))
    cursor.execute(f"""
        SELECT COUNT(DISTINCT agent_name)
        FROM agent_tags
        WHERE tag IN ({placeholders})
    """, filter_tags)
    result = cursor.fetchone()
    agent_count = result[0] if result else 0
```

#### Database Delegation (`src/backend/database.py:1503-1525`)

```python
# DatabaseManager delegation to SystemViewOperations
def create_system_view(self, owner_id: str, data):
    return self._system_view_ops.create_view(owner_id, data)

def list_user_system_views(self, user_id: str):
    return self._system_view_ops.list_user_views(user_id)

def can_user_edit_system_view(self, user_id: str, view_id: str, is_admin: bool = False):
    return self._system_view_ops.can_user_edit_view(user_id, view_id, is_admin)
```

### Pydantic Models (`src/backend/db_models.py:489-553`)

```python
class AgentTagList(BaseModel):
    """Response model for agent tags."""
    agent_name: str
    tags: List[str]

class AgentTagsUpdate(BaseModel):
    """Request model for setting agent tags."""
    tags: List[str]

class TagWithCount(BaseModel):
    """Tag with agent count."""
    tag: str
    count: int

class AllTagsResponse(BaseModel):
    """Response model for listing all tags."""
    tags: List[TagWithCount]

class SystemViewCreate(BaseModel):
    """Request model for creating a system view."""
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None  # Emoji or icon identifier
    color: Optional[str] = None  # Hex color (e.g., "#8B5CF6")
    filter_tags: List[str]  # Tags to filter by (OR logic)
    is_shared: bool = False  # Visible to all users?

class SystemViewUpdate(BaseModel):
    """Request model for updating a system view."""
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    filter_tags: Optional[List[str]] = None
    is_shared: Optional[bool] = None

class SystemView(BaseModel):
    """A saved system view (filter for agents)."""
    id: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    filter_tags: List[str]
    owner_id: str
    owner_email: Optional[str] = None
    is_shared: bool = False
    agent_count: int = 0  # Number of agents matching the filter
    created_at: str
    updated_at: str

class SystemViewList(BaseModel):
    """Response model for listing system views."""
    views: List[SystemView]
```

---

## Phase 3: Polish & Integration

### 3a. MCP Tools for Tag Management

5 MCP tools for programmatic tag management from Claude Code.

#### Entry Points

| Entry Point | Location | Description |
|-------------|----------|-------------|
| **Tool Registry** | `src/mcp-server/src/tools/tags.ts` | 5 tag management tools |
| **Client Methods** | `src/mcp-server/src/client.ts:694-747` | API client methods |

#### MCP Tools (`src/mcp-server/src/tools/tags.ts`)

| Line | Tool Name | Description |
|------|-----------|-------------|
| 40-54 | `list_tags` | List all unique tags with counts |
| 59-77 | `get_agent_tags` | Get tags for a specific agent |
| 82-118 | `tag_agent` | Add a tag to an agent (with validation) |
| 123-145 | `untag_agent` | Remove a tag from an agent |
| 150-189 | `set_agent_tags` | Replace all tags for an agent |

**Tool Definitions:**

```typescript
// list_tags - No parameters
list_tags() -> { tags: [{ tag: string, count: number }] }

// get_agent_tags
get_agent_tags(agent_name: string) -> { agent_name: string, tags: string[] }

// tag_agent - Add single tag with validation
tag_agent(agent_name: string, tag: string) -> { agent_name: string, tags: string[] }

// untag_agent - Remove single tag
untag_agent(agent_name: string, tag: string) -> { agent_name: string, tags: string[] }

// set_agent_tags - Replace all tags
set_agent_tags(agent_name: string, tags: string[]) -> { agent_name: string, tags: string[] }
```

**Validation in `tag_agent` (lines 103-112):**

```typescript
const normalized = tag.toLowerCase().trim();
if (!normalized) {
  return JSON.stringify({ error: "Tag cannot be empty" }, null, 2);
}
if (normalized.length > 50) {
  return JSON.stringify({ error: "Tag too long (max 50 characters)" }, null, 2);
}
if (!/^[a-z0-9-]+$/.test(normalized)) {
  return JSON.stringify({ error: "Tags can only contain lowercase letters, numbers, and hyphens" }, null, 2);
}
```

#### Client Methods (`src/mcp-server/src/client.ts:694-747`)

| Line | Method | Description |
|------|--------|-------------|
| 701-706 | `listAllTags()` | GET /api/tags |
| 711-716 | `getAgentTags(name)` | GET /api/agents/{name}/tags |
| 721-726 | `addAgentTag(name, tag)` | POST /api/agents/{name}/tags/{tag} |
| 731-736 | `removeAgentTag(name, tag)` | DELETE /api/agents/{name}/tags/{tag} |
| 741-747 | `setAgentTags(name, tags)` | PUT /api/agents/{name}/tags |

#### Authentication

All MCP tools require authentication via MCP API key:
- `requireApiKey` parameter controls enforcement
- API key passed via `authContext.mcpApiKey`
- Creates new TrinityClient with key as bearer token

### 3b. Quick Tag Filter (Dashboard Header)

Inline tag pills in Dashboard header for quick filtering without opening sidebar.

#### Entry Points

| Entry Point | Location | Description |
|-------------|----------|-------------|
| **Tag Pills** | `src/frontend/src/views/Dashboard.vue:71-127` | Quick tag filter UI |

#### Dashboard.vue Quick Tag Filter

| Line | Element | Description |
|------|---------|-------------|
| 72 | Container | `v-if="availableTags.length > 0"` - only shows when tags exist |
| 73 | Label | "Tags:" text |
| 74-88 | Displayed Tags | First 5 tags as clickable pills (computed `displayedTags`) |
| 76-87 | Tag Pill Button | Blue when selected, gray otherwise, X button when active |
| 90-114 | More Tags Dropdown | Shows remaining tags if >5 exist |
| 117-126 | Clear Filter Button | X icon to clear all selected tags |
| 509-511 | State | `availableTags`, `selectedQuickTags`, `showTagDropdown` refs |
| 515 | `displayedTags` | Computed - first 5 tags for inline display |
| 549 | `fetchAvailableTags()` | Called on mount, fetches `GET /api/tags` |
| 679-687 | `fetchAvailableTags()` | Fetches and populates availableTags ref |
| 689-699 | `toggleQuickTag(tag)` | Add/remove tag from selection, clears system view, applies filter |
| 702-705 | `clearQuickTags()` | Clears selection and filter |

**Quick Tag Filter Flow:**

```
User clicks tag pill
       │
       ▼
toggleQuickTag(tag)
       │
       ├── Add/remove from selectedQuickTags
       │
       ├── systemViewsStore.clearSelection()  ← Clear any system view selection
       │
       └── networkStore.setFilterTags([...selectedQuickTags])
                    │
                    ▼
              fetchAgents() with ?tags= param
```

**Interaction with System Views:**
- When a System View is selected, `selectedQuickTags` syncs to view's tags (lines 518-525)
- When a quick tag is clicked, the system view selection is cleared (line 697)
- Both methods call `networkStore.setFilterTags()` to apply the filter

### 3c. Bulk Tag Operations (Agents Page)

Multi-select checkboxes and bulk add/remove tag actions on the Dashboard List
mode (`components/AgentListPanel.vue` — the Agents page was retired into it in
trinity-enterprise#260; list-local tag *filtering* migrated to the chassis
quick-tag filter, and bulk ops emit `tags-changed` before awaiting the fleet
refresh so the chassis tag counts refetch even across a mode switch).

#### Entry Points

| Entry Point | Location | Description |
|-------------|----------|-------------|
| **Selection Checkboxes** | `src/frontend/src/components/AgentListPanel.vue` | Per-agent selection |
| **Bulk Actions Toolbar** | `src/frontend/src/components/AgentListPanel.vue` | Sticky toolbar when agents selected |
| **Quick Tag Filter** | `src/frontend/src/views/Dashboard.vue` | Chassis header dropdown — filters all three dashboard modes (server-side via `fetchAgents`) |

#### Agents.vue Bulk Operations (historical line references — page retired, ent#260)

| Line | Element | Description |
|------|---------|-------------|
| 22-33 | Tag Filter Dropdown | Select dropdown to filter agents by tag |
| 57-167 | Bulk Actions Toolbar | Appears when agents selected |
| 62-71 | Selection Counter | "N agent(s) selected" with Clear button |
| 74-129 | Add Tag Dropdown | Input + existing tags picker |
| 130-166 | Remove Tag Dropdown | Shows tags common to selected agents |
| 189-193 | Checkbox | Per-agent selection checkbox |
| 270-288 | Tags Display | Fixed-height container (`h-6 overflow-hidden`), show up to 3 tags with truncation (`max-w-20 truncate`), "+N" for more |
| 381-388 | State | `availableTags`, `agentTags`, `selectedFilterTag`, `selectedAgents`, bulk UI refs |
| 391-403 | `displayAgents` | Computed - filters by `selectedFilterTag` |
| 405-414 | `commonTagsInSelection` | Computed - union of tags across selected agents |
| 441-444 | onMounted | Calls `fetchAvailableTags()`, `fetchAllAgentTags()`, `fetchAllReadOnlyStates()` |
| 616-624 | `fetchAvailableTags()` | GET /api/tags for filter dropdown |
| 626-643 | `fetchAllAgentTags()` | Batch fetch tags for all agents |
| 645-647 | `getAgentTags(agentName)` | Get tags for specific agent from local cache |
| 649-656 | `toggleSelection(agentName)` | Add/remove from selectedAgents array |
| 658-662 | `clearSelection()` | Clear all selections and close dropdowns |
| 664-689 | `applyBulkTag()` | Add tag to all selected agents via parallel API calls |
| 691-706 | `removeBulkTag(tag)` | Remove tag from all selected agents via parallel API calls |

#### Tags Display Layout Fix (2026-02-18)

Tags on agent cards now use a fixed-height container to prevent layout breaking:

```vue
<!-- Tags display (fixed height for consistent layout) -->
<div class="h-6 mb-2 overflow-hidden">
  <div v-if="getAgentTags(agent.name).length > 0" class="flex flex-wrap gap-1">
    <span
      v-for="tag in getAgentTags(agent.name).slice(0, 3)"
      :key="tag"
      class="px-1.5 py-0.5 text-xs rounded-full bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 truncate max-w-20"
      :title="'#' + tag"
    >
      #{{ tag }}
    </span>
    <span
      v-if="getAgentTags(agent.name).length > 3"
      class="px-1.5 py-0.5 text-xs rounded-full bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-500"
    >
      +{{ getAgentTags(agent.name).length - 3 }}
    </span>
  </div>
</div>
```

Key styling:
- **Container**: `h-6 overflow-hidden` - Fixed 24px height, hides overflow
- **Tag pills**: `max-w-20 truncate` - Max 80px width with ellipsis for long tags
- **Shows 3 tags max** with "+N" overflow indicator

**Bulk Add Tag Flow:**

```
User clicks "+ Add Tag"
       │
       ▼
Opens dropdown with input + existing tags
       │
       ├── Type tag name OR
       └── Click existing tag button
              │
              ▼
       applyBulkTag()
              │
              ├── Validate format (alphanumeric + hyphens)
              │
              └── Promise.all(
                    selectedAgents.map(name =>
                      axios.post(`/api/agents/${name}/tags/${tag}`)
                    )
                  )
              │
              ▼
       showNotification("Added tag to N agent(s)")
       fetchAllAgentTags()  ← Refresh local tag cache
       fetchAvailableTags() ← Refresh tag counts
```

**Bulk Remove Tag Flow:**

```
User clicks "- Remove Tag"
       │
       ▼
Shows tags that exist on ANY selected agent (commonTagsInSelection)
       │
       ▼
User clicks tag to remove
       │
       ▼
removeBulkTag(tag)
       │
       └── Promise.all(
             selectedAgents.map(name =>
               axios.delete(`/api/agents/${name}/tags/${tag}`)
             )
           )
       │
       ▼
showNotification("Removed tag from N agent(s)")
fetchAllAgentTags()
fetchAvailableTags()
```

---

## Phase 4: System Manifest Integration

Auto-apply tags when deploying multi-agent systems via YAML manifest.

### Entry Points

| Entry Point | Location | Description |
|-------------|----------|-------------|
| **Deploy System** | `POST /api/systems/deploy` | YAML manifest deployment endpoint |
| **Migration Script** | `scripts/management/migrate_prefixes_to_tags.py` | Retroactive prefix-to-tag migration |

### YAML Schema Extension

```yaml
name: research-team
description: Research and analysis system

# ORG-001 Phase 4: Global tags applied to all agents
default_tags:
  - research
  - production

# ORG-001 Phase 4: Auto-create System View
system_view:
  name: Research Team
  icon: "🔬"
  color: "#8B5CF6"
  shared: true

agents:
  orchestrator:
    template: github:Org/orchestrator
    # ORG-001 Phase 4: Per-agent tags (in addition to default_tags)
    tags:
      - lead

  analyst:
    template: github:Org/analyst
    tags:
      - worker
```

### Tag Application Logic

When a system is deployed, tags are applied in this order:

1. **System prefix** (auto-applied): `research-team` → tag `research-team`
2. **Default tags** (from manifest): `research`, `production`
3. **Per-agent tags** (from agent config): `lead`, `worker`

All tags are normalized (lowercase, deduplicated).

### Models (`src/backend/models.py`)

```python
class SystemAgentConfig(BaseModel):
    template: str
    resources: Optional[dict] = None
    folders: Optional[dict] = None
    schedules: Optional[List[dict]] = None
    tags: Optional[List[str]] = None  # ORG-001 Phase 4

class SystemViewConfig(BaseModel):
    """Configuration for auto-creating a System View on deploy."""
    name: str
    icon: Optional[str] = None
    color: Optional[str] = None
    shared: bool = True

class SystemManifest(BaseModel):
    name: str
    description: Optional[str] = None
    prompt: Optional[str] = None
    agents: Dict[str, SystemAgentConfig]
    permissions: Optional[SystemPermissions] = None
    default_tags: Optional[List[str]] = None  # ORG-001 Phase 4
    system_view: Optional[SystemViewConfig] = None  # ORG-001 Phase 4

class SystemDeployResponse(BaseModel):
    status: str
    system_name: str
    agents_created: List[str]
    prompt_updated: bool
    permissions_configured: int = 0
    schedules_created: int = 0
    tags_configured: int = 0  # ORG-001 Phase 4
    system_view_created: Optional[str] = None  # ORG-001 Phase 4
    warnings: List[str] = []
```

### Service Layer (`src/backend/services/system_service.py`)

| Function | Description |
|----------|-------------|
| `parse_manifest()` | Parses YAML including tags and system_view fields |
| `validate_manifest()` | Validates tag format (alphanumeric + hyphens) |
| `configure_tags()` | Applies system prefix + default_tags + per-agent tags |
| `create_system_view()` | Creates System View with filter tags from manifest |

#### configure_tags()

```python
def configure_tags(
    system_name: str,
    agent_names: Dict[str, str],  # {short_name: final_name}
    agents_config: Dict[str, SystemAgentConfig],
    default_tags: Optional[List[str]] = None
) -> int:
    """
    Configure tags for all agents.

    Tag sources (in order):
    1. system_name (auto-applied as tag)
    2. default_tags (from manifest root)
    3. per-agent tags (from agent config)
    """
```

#### create_system_view()

```python
def create_system_view(
    system_name: str,
    system_view: SystemViewConfig,
    default_tags: Optional[List[str]],
    owner_id: str
) -> Optional[str]:
    """
    Create a System View for the deployed system.

    Filter tags include:
    1. system_name (always included)
    2. default_tags (if provided)
    """
```

### Router Integration (`src/backend/routers/systems.py`)

The `deploy_system()` endpoint calls tag and view functions after agent creation:

```python
# Step 10: Configure tags
tags_count = configure_tags(
    system_name=manifest.name,
    agent_names=agent_names,
    agents_config=manifest.agents,
    default_tags=manifest.default_tags
)

# Step 11: Create System View (optional)
system_view_id = None
if manifest.system_view:
    system_view_id = create_system_view(
        system_name=manifest.name,
        system_view=manifest.system_view,
        default_tags=manifest.default_tags,
        owner_id=str(current_user.id)
    )
```

### Migration Script

For existing agents deployed before Phase 4, run the migration script to extract prefixes as tags:

```bash
# Dry run (preview changes)
python scripts/management/migrate_prefixes_to_tags.py --dry-run

# Apply changes
python scripts/management/migrate_prefixes_to_tags.py
```

The script:
1. Lists all existing agents
2. Extracts system prefix (everything before last `-`)
3. Adds prefix as tag to each agent (if not already present)

Example: Agent `research-team-analyst` gets tag `research-team`.

### Test Steps

1. **Deploy with default_tags**
   - Action: Deploy manifest with `default_tags: [research, production]`
   - Expected: All agents have `research` and `production` tags
   - Verify: `GET /api/agents/{name}/tags` returns both tags

2. **Auto system prefix tag**
   - Action: Deploy system named `research-team`
   - Expected: All agents have `research-team` tag
   - Verify: Tag appears even without explicit `default_tags`

3. **Per-agent tags**
   - Action: Deploy manifest with per-agent `tags: [lead]`
   - Expected: Only that agent has `lead` tag
   - Verify: Other agents don't have the per-agent tag

4. **Auto System View creation**
   - Action: Deploy manifest with `system_view` section
   - Expected: System View appears in Dashboard sidebar
   - Verify: View filters to agents in deployed system

5. **Migration script dry-run**
   - Action: Run `python migrate_prefixes_to_tags.py --dry-run`
   - Expected: Shows what tags would be added
   - Verify: No database changes made

6. **Migration script apply**
   - Action: Run `python migrate_prefixes_to_tags.py`
   - Expected: Tags added to agents
   - Verify: `GET /api/agents/{name}/tags` returns prefix tags

### Status

- [x] Phase 4: Pydantic models updated (SystemAgentConfig, SystemManifest, SystemDeployResponse)
- [x] Phase 4: SystemViewConfig model added
- [x] Phase 4: parse_manifest() updated for tags
- [x] Phase 4: validate_manifest() validates tag format
- [x] Phase 4: configure_tags() function implemented
- [x] Phase 4: create_system_view() function implemented
- [x] Phase 4: deploy_system() router integration
- [x] Phase 4: Migration script created

---

## Side Effects

- **No WebSocket broadcasts** - Tags and views are fetched on demand, no real-time sync
- **No audit logging** - Tag/view changes not logged to activity stream
- **Cascade delete** - Tags auto-deleted when agent is deleted (FK constraint)
- **localStorage persistence** - Active view ID and sidebar collapsed state persisted

---

## Error Handling

### Tags

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Empty tag | 400 | "Tag cannot be empty" |
| Tag too long | 400 | "Tag too long (max 50 characters)" |
| Invalid characters | 400 | "Tags can only contain letters, numbers, and hyphens" |
| Agent not found | 404 | "Agent not found" |
| Not authorized | 403 | "Access forbidden" |
| Not owner (modify) | 403 | "Only agent owner can modify tags" |

### System Views

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Empty name | 400 | "View name is required" |
| Name too long | 400 | "View name too long (max 100 characters)" |
| No filter tags | 400 | "At least one filter tag is required" |
| Invalid color | 400 | "Color must be a hex code (e.g., #8B5CF6)" |
| View not found | 404 | "View not found" |
| Access denied | 403 | "Access denied to this view" |
| Not owner (modify) | 403 | "Only the owner or admin can edit/delete this view" |

---

## Security Considerations

### Tags
- **Read access**: Any authorized user (owner, shared, or admin) can view tags
- **Write access**: Only owner or admin can add/remove tags
- **Input validation**: Tags normalized to lowercase, restricted to alphanumeric + hyphens
- **Length limit**: Max 50 characters per tag
- **SQL injection**: Parameterized queries in all database operations

### System Views
- **Read access**: Users see their own views + all shared views
- **Write access**: Only view owner or admin can edit/delete
- **Sharing**: Any user can create shared views (visible to all)
- **ID generation**: Secure random IDs via `secrets.token_urlsafe(12)`
- **SQL injection**: Parameterized queries in all database operations

### MCP Tools
- **Authentication**: MCP API key required for all tag tools
- **Authorization**: Same rules as REST API (owner/admin for write operations)
- **Validation**: Client-side validation before API call, server validates again

---

## Known Issues (Fixed)

### Tag API Authentication Bug (Fixed 2026-02-18)

**Issue**: Tag operations in AgentDetail.vue were failing silently because API calls were using the Pinia store's `api` wrapper which didn't include authentication headers.

**Root Cause**: The `agentsStore.api.get()` pattern was inconsistent with how the store's axios instance was configured - it didn't automatically include the JWT bearer token.

**Fix**: Changed all tag-related API calls to use `axios` directly with `authStore.authHeader`:
```javascript
// Before (broken)
const response = await agentsStore.api.get(`/api/agents/${name}/tags`)

// After (fixed)
const response = await axios.get(`/api/agents/${name}/tags`, {
  headers: authStore.authHeader
})
```

**Files Changed**:
- `src/frontend/src/views/AgentDetail.vue` - Lines 548-611 (loadTags, loadAllTags, updateTags, addTag, removeTag)

**Impact**: Users can now add, remove, and view tags on the agent detail page without authentication errors.

---

## Testing

### Prerequisites

- Trinity backend running at `http://localhost:8000`
- Trinity frontend running at `http://localhost`
- At least one agent created with tags
- Logged in as admin or regular user

### Phase 1 Test Steps (Tags)

1. **View agent tags**
   - Action: Navigate to agent detail page
   - Expected: Tags row displays below agent name
   - Verify: Empty state shows "No tags" if no tags assigned

2. **Add tag via button**
   - Action: Click "Add" button, type "test-tag", press Enter
   - Expected: Tag appears as purple pill with # prefix
   - Verify: `GET /api/agents/{name}/tags` returns new tag

3. **Add tag via autocomplete**
   - Action: Click "Add", type partial tag name
   - Expected: Dropdown shows matching existing tags
   - Verify: Clicking suggestion adds tag

4. **Remove tag**
   - Action: Click X button on tag pill
   - Expected: Tag disappears from list
   - Verify: `GET /api/agents/{name}/tags` no longer returns removed tag

5. **Filter agents by tag**
   - Action: Call `GET /api/agents?tags=test-tag`
   - Expected: Only agents with "test-tag" returned
   - Verify: Response includes `tags` array for each agent

6. **List all tags**
   - Action: Call `GET /api/tags`
   - Expected: Array of `{tag, count}` objects
   - Verify: Tags sorted by count descending

7. **Tag validation**
   - Action: Try adding tag with spaces or special characters
   - Expected: Error message "Tags can only contain letters, numbers, and hyphens"
   - Verify: Tag not added

8. **Non-owner access**
   - Action: As shared user, try to add/remove tags
   - Expected: 403 error
   - Verify: Only viewing allowed for non-owners

### Phase 2 Test Steps (System Views)

1. **View sidebar**
   - Action: Navigate to Dashboard
   - Expected: Systems sidebar visible on left with "All Agents" option
   - Verify: Sidebar can be collapsed/expanded

2. **Create system view**
   - Action: Click "New View" button, fill form, click Create
   - Expected: View appears in sidebar with icon and agent count
   - Verify: `GET /api/system-views` returns new view

3. **Select view**
   - Action: Click on a view in sidebar
   - Expected: Dashboard filters to show only agents with matching tags
   - Verify: Agent count matches view's agent_count

4. **Clear filter**
   - Action: Click "All Agents" option
   - Expected: Dashboard shows all agents
   - Verify: activeViewId is null

5. **Edit view**
   - Action: Hover over view, click edit icon
   - Expected: Editor modal opens with current values
   - Verify: Changes saved after clicking Update

6. **Delete view**
   - Action: In edit modal, click Delete, confirm
   - Expected: View removed from sidebar
   - Verify: Filter cleared if deleted view was active

7. **Shared view**
   - Action: Create view with "Share with all users" enabled
   - Expected: Other users can see and select the view
   - Verify: Other users cannot edit/delete shared view

8. **Persistence**
   - Action: Select a view, refresh page
   - Expected: Same view still selected after refresh
   - Verify: localStorage contains `trinity-active-view`

9. **Collapse persistence**
   - Action: Collapse sidebar, refresh page
   - Expected: Sidebar still collapsed
   - Verify: localStorage contains `trinity-sidebar-collapsed`

### Phase 3 Test Steps (MCP Tools)

1. **list_tags via MCP**
   - Action: Call `list_tags` tool from Claude Code
   - Expected: Returns array of tags with counts
   - Verify: Same data as `GET /api/tags`

2. **get_agent_tags via MCP**
   - Action: Call `get_agent_tags` with agent name
   - Expected: Returns agent_name and tags array
   - Verify: Tags match agent's actual tags

3. **tag_agent via MCP**
   - Action: Call `tag_agent` with agent name and new tag
   - Expected: Returns updated tags list
   - Verify: New tag appears in agent's tags

4. **untag_agent via MCP**
   - Action: Call `untag_agent` to remove existing tag
   - Expected: Returns updated tags list (without removed tag)
   - Verify: Tag no longer in agent's tags

5. **set_agent_tags via MCP**
   - Action: Call `set_agent_tags` with new tag array
   - Expected: All tags replaced with new set
   - Verify: Agent has exactly the specified tags

### Phase 3 Test Steps (Quick Tag Filter)

1. **View quick tags**
   - Action: Navigate to Dashboard with agents that have tags
   - Expected: Tag pills appear in header next to view mode toggle
   - Verify: First 5 tags displayed, "+N" button if more exist

2. **Filter by quick tag**
   - Action: Click a tag pill
   - Expected: Pill turns blue, Dashboard filters to matching agents
   - Verify: System View sidebar shows "All Agents" as active (view cleared)

3. **Multiple quick tags**
   - Action: Click multiple tag pills
   - Expected: All selected pills turn blue, OR filter applied
   - Verify: Agents with ANY selected tag shown

4. **Clear quick tags**
   - Action: Click X button next to tag pills
   - Expected: All selections cleared, all agents shown
   - Verify: selectedQuickTags array is empty

5. **Quick tags vs System View**
   - Action: Select a System View, then click a quick tag
   - Expected: System View deselected, quick tag filter active
   - Verify: Sidebar shows "All Agents" active

### Phase 3 Test Steps (Bulk Tag Operations)

1. **Select multiple agents**
   - Action: Click checkboxes on multiple agent cards
   - Expected: Bulk actions toolbar appears
   - Verify: Counter shows "N agents selected"

2. **Bulk add tag**
   - Action: Click "+ Add Tag", enter tag name, click Apply
   - Expected: Notification "Added tag to N agent(s)"
   - Verify: All selected agents now have the tag

3. **Bulk add existing tag**
   - Action: Click "+ Add Tag", click an existing tag button
   - Expected: Tag applied to all selected agents
   - Verify: Agents that already had tag are unaffected

4. **Bulk remove tag**
   - Action: Click "- Remove Tag", click a tag from the list
   - Expected: Notification "Removed tag from N agent(s)"
   - Verify: Tag removed from all selected agents

5. **Clear selection**
   - Action: Click "Clear" link in toolbar
   - Expected: All checkboxes unchecked, toolbar disappears
   - Verify: selectedAgents array is empty

6. **Filter + bulk operations**
   - Action: Filter agents by tag, then select filtered agents
   - Expected: Only visible agents selectable
   - Verify: Operations apply only to selected agents

### Status

- [x] Phase 1: Backend API implemented and tested
- [x] Phase 1: Frontend TagsEditor component working
- [x] Phase 1: AgentHeader integration complete
- [x] Phase 1: Agents list filtering by tags working
- [x] Phase 2: Backend API implemented (5 endpoints)
- [x] Phase 2: SystemViewOperations database layer
- [x] Phase 2: SystemViewsSidebar component
- [x] Phase 2: SystemViewEditor modal
- [x] Phase 2: systemViews Pinia store
- [x] Phase 2: Dashboard integration with filter reactivity
- [x] Phase 2: localStorage persistence
- [x] Phase 3: MCP tools implemented (5 tools)
- [x] Phase 3: Quick tag filter in Dashboard header
- [x] Phase 3: Bulk tag operations on Agents page
- [x] Phase 3: Tag filter dropdown on Agents page
- [x] Phase 4: Pydantic models updated for manifest tags
- [x] Phase 4: configure_tags() service function
- [x] Phase 4: create_system_view() service function
- [x] Phase 4: deploy_system() router integration
- [x] Phase 4: Migration script for existing prefixes

---

## Related Flows

- **Upstream**: [agent-lifecycle.md](agent-lifecycle.md) - Tags deleted when agent deleted
- **Related**: [agent-network.md](agent-network.md) - Dashboard displays filtered agents
- **Related**: [agents-page-ui-improvements.md](agents-page-ui-improvements.md) - Tags displayed on agent cards
- **Related**: [mcp-orchestration.md](mcp-orchestration.md) - MCP tool architecture
- **Phase 4**: [system-manifest.md](system-manifest.md) - Auto-apply tags and create System Views on system deployment

---

## Revision History

| Date | Change |
|------|--------|
| 2026-02-22 | **Sidebar Default Collapsed**: SystemViewsSidebar.vue now defaults to collapsed when no localStorage value exists (line 124). Fixed toggle button to properly call `toggleCollapse()` for localStorage persistence (line 14). |
| 2026-02-17 | Initial implementation of ORG-001 Phase 1 (Tags) |
| 2026-02-17 | Added ORG-001 Phase 2 (System Views) - sidebar, editor, store, API |
| 2026-02-17 | Added ORG-001 Phase 3 (MCP tools, Quick Tag Filter, Bulk Operations) |
| 2026-02-17 | Added ORG-001 Phase 4 (System Manifest Integration) - auto tags, system views, migration |
| 2026-02-18 | **Bug Fix**: AgentDetail.vue now uses axios + authStore.authHeader for tag API calls (was using Pinia store wrapper without proper auth) |
| 2026-02-18 | Updated line numbers for AgentDetail.vue tag methods (548-611, 714-715) |
| 2026-02-18 | **Tags Layout Fix**: Agents.vue tags container now uses fixed height (`h-6 overflow-hidden`, line 271) and truncation (`max-w-20 truncate`, line 276) to prevent tile layout breaking. Updated bulk operations line numbers. |
