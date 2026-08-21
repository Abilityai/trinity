# Feature: Agent Info Display

## Overview
Displays template metadata (capabilities, commands, sub-agents, platforms, resources) in an "Info" tab on the agent detail page, exposing what an agent can do and how it is configured.

## User Story
As a platform user, I want to see what an agent is capable of so that I understand its purpose and available commands before interacting with it.

## Entry Points
- **UI**: `src/frontend/src/views/AgentDetail.vue:79-81` - Info tab content rendering
- **API**: `GET /api/agents/{name}/info`

---

## Frontend Layer

### Components

#### AgentDetail.vue:59-76
Tab navigation using dynamic visibleTabs computed property:
```vue
<nav class="-mb-px flex whitespace-nowrap">
  <button
    v-for="tab in visibleTabs"
    :key="tab.id"
    @click="activeTab = tab.id"
    :class="[
      'px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
      activeTab === tab.id
        ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
        : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
    ]"
  >
    {{ tab.label }}
  </button>
</nav>
```

#### AgentDetail.vue:79-81
Info tab content rendering:
```vue
<!-- Info Tab Content -->
<div v-if="activeTab === 'info'" class="p-6">
  <InfoPanel :agent-name="agent.name" :agent-status="agent.status" @item-click="handleInfoItemClick" />
</div>
```

#### AgentDetail.vue:577-588 - Item Click Handler
Handles clicks from Info tab items (use cases, commands, sub-agents) and redirects to Tasks tab:
```javascript
// Handle item click from Info tab - switch to Tasks tab with prefilled message
const handleInfoItemClick = ({ type, text }) => {
  // Set the prefill message and switch to Tasks tab
  taskPrefillMessage.value = text
  activeTab.value = 'tasks'
  // Clear the prefill after a short delay so it can be used again
  nextTick(() => {
    setTimeout(() => {
      taskPrefillMessage.value = ''
    }, 100)
  })
}
```

#### TasksPanel.vue - Initial Message Prop
```javascript
const props = defineProps({
  agentName: { type: String, required: true },
  agentStatus: { type: String, default: 'stopped' },
  highlightExecutionId: { type: String, default: null },
  initialMessage: { type: String, default: '' }  // Prefilled task message from Info tab
})

// Watch for initial message changes (from Info tab clicks)
// immediate: true ensures it fires on mount when component is conditionally rendered
watch(() => props.initialMessage, (newMessage) => {
  if (newMessage) {
    newTaskMessage.value = newMessage
  }
}, { immediate: true })
```

#### InfoPanel.vue:1-375
The complete Info Panel component displaying template metadata.

**Key sections displayed:**
| Section | Lines | Data Field | Visual Style | Clickable |
|---------|-------|------------|--------------|-----------|
| Header (name, version, author) | 27-67 | `display_name`, `tagline`, `version`, `author`, `type` | Gradient header box | No |
| Use Cases | 70-91 | `use_cases[]` | Clickable prompts grid | Yes |
| Resources (CPU, Memory) | 93-111 | `resources.cpu`, `resources.memory` | White card with icon | No |
| Sub-Agents | 113-141 | `sub_agents[]` | Blue grid cards | Yes |
| Slash Commands | 143-168 | `commands[]` | Purple mono pills | Yes |
| MCP Servers | 170-190 | `mcp_servers[]` | Yellow mono pills | No |
| Skills | 192-212 | `skills[]` | Teal pills | No |
| Capabilities | 214-231 | `capabilities[]` | Green pills | No |
| Platforms | 233-250 | `platforms[]` | Gray pills | No |
| Enabled Tools | 252-270 | `tools[]` | Orange pills | No |

> **#1107 redesign (2026-06-09):** the **About** block (header gradient +
> description) and the **"What You Can Ask"** use-cases grid stay above the
> fold. Everything from **Resources** through **Enabled Tools** is now wrapped
> in a single collapsible native `<details>` **"Technical details"**
> disclosure (gated on a `hasTechnicalDetails` computed — true when any of
> resources / sub-agents / commands / mcp_servers / skills / capabilities /
> platforms / tools has content). The underlying data fields and click-through
> behaviour are unchanged; only the visual hierarchy changed (About leads,
> exhaustive `template.yaml` metadata tucked behind the disclosure). The Info
> tab is reached from the new Overview tab's "Full details →" link — see
> [agent-overview-dashboard.md](agent-overview-dashboard.md).

**Click handlers (lines 332-354):**
```javascript
const emit = defineEmits(['item-click'])

const handleItemClick = (type, text) => {
  // Emit event to parent to open Tasks tab with this text
  emit('item-click', { type, text })
}

const handleUseCaseClick = (text) => {
  handleItemClick('use-case', text)
}

const handleCommandClick = (command) => {
  const commandName = getItemName(command)
  handleItemClick('command', `/${commandName}`)
}

const handleSubAgentClick = (subAgent) => {
  const agentName = getItemName(subAgent)
  const description = getItemDescription(subAgent)
  // Create a prompt to delegate to the sub-agent
  const prompt = description
    ? `Ask ${agentName} to help with: ${description}`
    : `Ask ${agentName} to help with a task`
  handleItemClick('sub-agent', prompt)
}
```

### State Management

#### stores/agents.js:284-290
```javascript
async getAgentInfo(name) {
  const authStore = useAuthStore()
  const response = await axios.get(`/api/agents/${name}/info`, {
    headers: authStore.authHeader
  })
  return response.data
}
```

### API Calls
```javascript
GET /api/agents/{agent_name}/info
Headers: Authorization: Bearer {token}
```

---

## Backend Layer

### Endpoint

#### src/backend/routers/agent_files.py
```python
@router.get("/{agent_name}/info")
async def get_agent_info_endpoint(
    agent_name: AuthorizedAgentByName,
    request: Request
):
    """Get template info and metadata for an agent."""
```

### Business Logic

**Decision tree:**
```
1. Check user has access to agent (via AuthorizedAgentByName dependency)
   |
   +-> NO  --> 403 Forbidden
   |
   +-> YES --> Check if container exists
               |
               +-> NO  --> 404 Not Found
               |
               +-> YES --> Check container status
                           |
                           +-> STOPPED --> Return basic info from Docker labels
                           |
                           +-> RUNNING --> Proxy to agent container API
```

**When agent is STOPPED (lines 445-458):**
Returns limited info from Docker container labels:
```python
labels = container.labels
return {
    "has_template": bool(labels.get("trinity.template")),
    "agent_name": agent_name,
    "template_name": labels.get("trinity.template", ""),
    "resources": {
        "cpu": labels.get("trinity.cpu", ""),
        "memory": labels.get("trinity.memory", "")
    },
    "status": "stopped",
    "message": "Agent is stopped. Start the agent to see full template info."
}
```

**When agent is RUNNING (lines 460-467):**
Proxies request to agent container:
```python
agent_url = f"http://agent-{agent_name}:8000/api/template/info"
async with httpx.AsyncClient(timeout=10.0) as client:
    response = await client.get(agent_url)
    if response.status_code == 200:
        data = response.json()
        data["status"] = "running"
        return data
```

### Docker Operations
- `get_agent_container(agent_name)` - Retrieves container by name
- `container.reload()` - Refreshes container state
- `container.labels` - Reads Docker labels for basic info

---

## Agent Layer

### Agent Server Endpoint

#### docker/base-image/agent_server/routers/info.py:81-140
```python
@router.get("/api/template/info")
async def get_template_info():
    """
    Get template metadata from template.yaml if available.
    Returns information about what this agent is, its capabilities, commands, etc.
    """
```

### Template Discovery
Template is read from a fixed path (line 87):
```python
template_path = Path("/home/developer/template.yaml")
```

### Response Schema

**When template exists (lines 114-140):**
```python
return {
    "has_template": True,
    "template_path": str(template_path),
    "agent_name": agent_state.agent_name,
    # Core metadata
    "name": template_data.get("name", ""),
    "display_name": template_data.get("display_name", template_data.get("name", "")),
    "tagline": template_data.get("tagline", ""),
    "description": template_data.get("description", ""),
    "version": template_data.get("version", ""),
    "author": template_data.get("author", ""),
    "updated": template_data.get("updated", ""),
    # Type and resources
    "type": template_data.get("type", ""),
    "resources": template_data.get("resources", {}),
    # Use cases - example prompts for users
    "use_cases": template_data.get("use_cases", []),
    # Capabilities and features (can be strings or {name, description} objects)
    "capabilities": template_data.get("capabilities", []),
    "sub_agents": template_data.get("sub_agents", []),
    "commands": template_data.get("commands", []),
    "platforms": template_data.get("platforms", []),
    "tools": template_data.get("tools", []),
    "skills": template_data.get("skills", []),
    # MCP servers (can be strings or {name, description} objects)
    "mcp_servers": mcp_servers_raw,
}
```

**When no template (lines 98-105):**
```python
return {
    "has_template": False,
    "agent_name": agent_state.agent_name,
    "template_name": os.getenv("TEMPLATE_NAME", ""),
    "message": "No template.yaml found - this agent was created without a template"
}
```

---

## Template Schema

### Example: ruby-social-media-agent/template.yaml

**Location**: `config/agent-templates/ruby-social-media-agent/template.yaml`

```yaml
name: ruby-social-media-agent
display_name: "Ruby - Social Media Content Manager"
description: |
  Multi-platform content production and distribution agent...
version: "1.3"
author: "Eugene Vyborov"
updated: "2025-11-25"

resources:
  cpu: "2"
  memory: "4g"

credentials:
  mcp_servers:
    heygen:
      env_vars:
        - HEYGEN_API_KEY
    twitter-mcp:
      env_vars:
        - TWITTER_API_KEY
        - TWITTER_API_SECRET_KEY
        - TWITTER_ACCESS_TOKEN
        - TWITTER_ACCESS_TOKEN_SECRET
    # ... more servers

capabilities:
  - multi_platform_posting
  - video_generation
  - media_management
  - content_scheduling
  - analytics_tracking
  - ai_image_generation
  - gif_management

sub_agents:
  - ai-ruminator
  - gemini-agent
  - giphy-manager
  - metricool-manager
  - social-media-manager
  - video-editor
  - video-generator
  - video-repurposer

commands:
  - clip-video
  - create-article
  - create-video
  - generate-image
  - get-perspective
  - post-now
  - schedule-post
  - schedule-prepared-content
  - upload-media

platforms:
  - twitter
  - linkedin
  - instagram
  - tiktok
  - youtube
  - threads
  - bluesky
  - facebook
  - pinterest
```

---

## Data Flow Diagram

```
+------------------+     +------------------+     +------------------+
|  InfoPanel.vue   |     |   agents.js      |     |   Backend API    |
|                  |     |   (Store)        |     |                  |
|  onMounted() ----+---->| getAgentInfo() --+---->| GET /agents/     |
|                  |     |                  |     |   {name}/info    |
+------------------+     +------------------+     +--------+---------+
                                                          |
                              +---------------------------+
                              |
                              v
              +---------------+---------------+
              |                               |
              v                               v
    +------------------+            +------------------+
    | Agent STOPPED    |            | Agent RUNNING    |
    |                  |            |                  |
    | Return Docker    |            | Proxy to agent:  |
    | container labels |            | http://{name}:   |
    | (basic info)     |            | 8000/api/        |
    +------------------+            | template/info    |
                                    +--------+---------+
                                             |
                                             v
                                    +------------------+
                                    | Agent Container  |
                                    |                  |
                                    | Read template.   |
                                    | yaml from disk   |
                                    | Parse YAML       |
                                    | Return metadata  |
                                    +------------------+
```

---

## Side Effects

- **WebSocket**: None
- **Audit Log**: None (read-only operation)
- **Redis**: None

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | "Agent not found" |
| Access denied | 403 | "You don't have permission to access this agent" |
| Agent starting up | 504 | "Agent is starting up, please try again" |
| Container proxy failure | 200 | Returns fallback with container labels |
| No template.yaml | 200 | `{"has_template": false, "message": "..."}` |

---

## Security Considerations

- **Authorization**: User must have access to the agent (owner, shared, or admin) - enforced via `AuthorizedAgentByName` dependency
- **No credentials exposed**: MCP servers list contains only names, not secrets
- **Container isolation**: Backend proxies request, frontend never talks to agent directly
- **Docker network**: Agent container only accessible via internal Docker network

---

## UI States

### Loading State
Gated on **"no data yet"**, never on fetch-in-flight (#1927, design-system p13/p14). `loading` stays the in-flight flag (it drives the Retry labels); the spinner renders only for `firstLoad` — mount, or an agent switch (the `agentName` watcher nulls `templateInfo`). The `agentStatus → running` refetch (Start/Restart) and a Retry are therefore silent: the rendered content stays and the values swap in place.
```vue
<div v-if="firstLoad" class="flex items-center justify-center py-8" data-testid="info-loading">
  <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500"></div>
</div>
```
where `firstLoad`/`loadFailed` come from `utils/loadingState.js::viewState({ loading, hasLoaded: templateInfo !== null, error: loadError })`.

### Stale State (refresh failed with content on screen)
A sibling `InlineError` **before** the loading/failed/empty chain (never an `else-if` arm, so it cannot replace the content it keeps): `staleBannerMessage('template info', lastLoadedAt)` → "Couldn't refresh template info — showing data from HH:MM.", with Retry (`Retrying…` while in flight) and Dismiss. `LoadFailed` (#1926) is now reached only with no data on screen.

### No Template State
```vue
<div v-else-if="!templateInfo?.has_template" class="text-center py-8">
  <h3>No Template Information</h3>
  <p>{{ templateInfo?.message || 'This agent was created without a template.' }}</p>
</div>
```

### Full Template Display
Renders all metadata sections when `templateInfo.has_template === true`

---

## Helper Functions

### formatCapability (InfoPanel.vue:324-330)
Converts snake_case capability names to Title Case:
```javascript
const formatCapability = (capability) => {
  // Convert snake_case to Title Case
  return capability
    .split('_')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}
```

### getItemName / getItemDescription (InfoPanel.vue:312-322)
Handle items that can be either strings or {name, description} objects:
```javascript
// Get name from item - handles both string and {name, description} object
const getItemName = (item) => {
  if (typeof item === 'string') return item
  return item?.name || ''
}

// Get description from item - returns null for strings
const getItemDescription = (item) => {
  if (typeof item === 'string') return null
  return item?.description || null
}
```

---

## MCP Tool Access

Agents can also retrieve template metadata for other agents via the MCP tool `get_agent_info`:

### MCP Tool: `get_agent_info`

**File**: `src/mcp-server/src/tools/agents.ts:103-145`

```typescript
// Tool definition
getAgentInfo: {
  name: "get_agent_info",
  description:
    "Get full template metadata and capabilities for an agent. " +
    "Returns detailed information from the agent's template.yaml including: " +
    "display name, description, version, author, capabilities, available commands, " +
    "MCP servers, tools, skills, and example use cases. " +
    "Useful for understanding what an agent can do before interacting with it. " +
    "Access control: agents can only get info about agents they have permission to call.",
  parameters: z.object({
    name: z.string().describe("The name of the agent to get information about"),
  }),
  execute: async ({ name }, context) => {
    // ... access control logic
    const info = await apiClient.getAgentInfo(name);
    return JSON.stringify(info, null, 2);
  },
}
```

### Access Control

| Key Scope | Access Rule |
|-----------|-------------|
| Agent-scoped | Self + permitted agents only (via `getPermittedAgents()`) |
| System-scoped | Full access to all agents |
| User-scoped | All accessible agents (owned + shared) |

### Client Method

**File**: `src/mcp-server/src/client.ts:200-205`

```typescript
async getAgentInfo(name: string): Promise<AgentTemplateInfo> {
  return this.request<AgentTemplateInfo>(
    "GET",
    `/api/agents/${encodeURIComponent(name)}/info`
  );
}
```

### Types

**File**: `src/mcp-server/src/types.ts:91-114`

```typescript
export interface AgentTemplateInfo {
  has_template: boolean;
  agent_name: string;
  name?: string;
  display_name?: string;
  tagline?: string;
  description?: string;
  version?: string;
  author?: string;
  type?: string;
  resources?: {
    cpu?: string;
    memory?: string;
  };
  capabilities?: string[];
  commands?: AgentCommand[];
  mcp_servers?: string[];
  sub_agents?: string[];
  platforms?: string[];
  tools?: string[];
  skills?: string[];
  use_cases?: string[];
  status?: "running" | "stopped";
}
```

### Use Case

Head agents can inspect sub-agent capabilities before delegating tasks:

```typescript
// Get sub-agent capabilities
const info = await mcp_trinity_get_agent_info({ name: "worker-agent" });
// Check if it has required skills
if (info.capabilities?.includes("code_review")) {
  await mcp_trinity_chat_with_agent({ agent_name: "worker-agent", message: "Review this PR" });
}
```

---

## Related Flows

- **Upstream**:
  - [Agent Lifecycle](agent-lifecycle.md) - Agent must exist to display info
  - [Template Processing](template-processing.md) - Template must be parsed at agent creation
- **Downstream**:
  - [Tasks Tab](tasks-tab.md) - **Click-through destination** for use cases, commands, and sub-agents
  - [Agent Terminal](agent-terminal.md) - User understands commands before interacting
  - [Credential Injection](credential-injection.md) - User sees required MCP servers
- **MCP Integration**:
  - [MCP Orchestration](mcp-orchestration.md) - `get_agent_info` tool (agents.ts:103-145)
  - [Agent Permissions](agent-permissions.md) - Access control for agent-scoped keys

---

## Testing Notes

1. **Test with stopped agent**: Should show basic info from Docker labels
2. **Test with running agent**: Should show full template metadata
3. **Test without template**: Should show "No Template Information" message
4. **Test status change**: Reload should trigger when agent starts
5. **Test use case click**: Should switch to Tasks tab with text prefilled
6. **Test command click**: Should switch to Tasks tab with `/command-name` prefilled
7. **Test sub-agent click**: Should switch to Tasks tab with delegation prompt prefilled

---

## Status
**Working** - Line numbers verified as of 2026-01-23

---

## Revision History

| Date | Changes |
|------|---------|
| **2026-06-09** | **#1107 Info redesign** — About narrative + "What You Can Ask" lead; Resources→Tools metadata moved behind a collapsible `<details>` "Technical details" disclosure (`hasTechnicalDetails` gate). Reached from the new Overview tab's "Full details →" link. |
| **2026-01-23** | Updated all line numbers and code references; verified against current codebase |
| **2026-01-12** | **Interactive Items** - Use cases, commands, and sub-agents are now clickable; event changed from `@use-case-click` to `@item-click` with `{type, text}` payload; TasksPanel has new `initialMessage` prop; commands prefill as `/command-name`, sub-agents prefill as delegation prompts |
| **2026-01-03** | Added MCP tool `get_agent_info` for programmatic access to template metadata |
| **2025-12-30** | Line numbers verified for agent server modular package |
| **2025-12-06** | Agent server refactored to modular package structure (`docker/base-image/agent_server/`) |
| **Initial** | InfoPanel.vue supports `use_cases`, `skills`, `tagline` fields; items can be strings or {name, description} objects for rich display |
