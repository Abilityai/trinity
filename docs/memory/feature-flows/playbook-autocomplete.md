# Feature: Playbook Autocomplete in Agent Chat

## Overview
Claude Code-style slash-command autocomplete for agent playbooks in the chat input, with ghost-text completion, dropdown navigation, and argument hint bar.

## User Story
As an agent operator, I want to type `/` in the chat input and see a filtered list of available playbooks so that I can invoke skills quickly without memorizing command names.

## Entry Points
- **UI**: `src/frontend/src/components/ChatPanel.vue:154` — `<ChatInput>` receives `agentName` and `agentStatus` props
- **UI**: `src/frontend/src/components/chat/ChatInput.vue:98` — textarea `@keydown` / `@input` handlers
- **API**: `GET /api/agents/{name}/playbooks`

## Frontend Layer

### Components

#### `src/frontend/src/components/ChatPanel.vue:154`
Passes two new props to `ChatInput`:
```html
<ChatInput
  ref="chatInputRef"
  v-model="message"
  :disabled="loading"
  :agent-name="agentName"
  :agent-status="agentStatus"
  @submit="sendMessage"
/>
```

#### `src/frontend/src/components/chat/ChatInput.vue`
Owns all autocomplete UI. Key sections:

| Lines | Element | Purpose |
|-------|---------|---------|
| 5–78 | `<Transition>` dropdown | Floating list above the input, shown when `ac.showDropdown` is true; max 8 items visible; headed **Skills** (`:25`, trinity-enterprise#754) |
| 84–110 | Ghost-text overlay `<div>` + `<textarea>` | `<div aria-hidden>` sits behind the `<textarea>`; renders typed text as transparent + completion as gray |
| 125–146 | Argument hint bar `<Transition>` | Below the form; shown after `/command ` is completed; displays `argument_hint` and description |
| 199–207 | `watch([agentName, agentStatus])` | Calls `ac.load()` whenever the agent switches to `running` |
| 210–213 | `handleInput()` | Calls `ac.parse(value, caretPos)` on every keystroke |
| 229–265 | `handleKeydown()` | Tab/ArrowRight → accept; ↑↓ → navigate; Enter → accept (dropdown open) or submit; Escape → dismiss |
| 267–295 | `_commitAccept()` / `onClickSuggestion()` | Write accepted value back into `localMessage`, reposition caret via `setSelectionRange` |

Props accepted by `ChatInput`:
```javascript
modelValue: String     // v-model content
placeholder: String    // default: 'Type your message or / for skills…'  (:248, trinity-enterprise#754)
disabled: Boolean
agentName: String      // passed from ChatPanel
agentStatus: String    // 'running' | 'stopped' | ...
```

### Composable

#### `src/frontend/src/composables/usePlaybookAutocomplete.js`

Exported from `src/frontend/src/composables/index.js:15`.

**Internal state:**
```javascript
playbooks        // ref([])  — full list loaded from API
showDropdown     // ref(false)
query            // ref('')  — text typed after /
selectedIndex    // ref(0)
slashStart       // ref(-1) — char index of the / that triggered autocomplete
activeArgHint    // ref(null) — { name, argument_hint, description }
```

**Computed:**
- `filteredPlaybooks` — filters `playbooks` where `user_invocable !== false`; starts-with matches appear before contains matches
- `ghostCompletion` — trailing characters of the top suggestion the user has not yet typed (empty string if no match or dropdown hidden)

**Functions:**

| Function | Signature | Description |
|----------|-----------|-------------|
| `load` | `(agentName, authHeader)` | `GET /api/agents/{name}/playbooks` → sets `playbooks.value`; fails silently |
| `parse` | `(value, caretPos)` → `'completion' \| 'argument' \| 'none'` | Walks backward from caret to find word starting with `/`; sets dropdown or argHint state |
| `accept` | `(value, caretPos)` → `{value, caretPos, playbook} \| null` | Accepts highlighted suggestion |
| `acceptPlaybook` | `(playbook, value, caretPos)` → `{value, caretPos, playbook}` | Accepts a specific playbook (click) |
| `hide` | `()` | Resets all dropdown state |
| `moveDown` / `moveUp` | `()` | Wrapping circular navigation through `filteredPlaybooks` |

**`parse()` state machine:**
1. Walk backward from caret to find the current word boundary
2. If word does not start with `/` → `_hide()`, clear argHint, return `'none'`
3. If word contains a space after the command name → look up matched playbook, set `activeArgHint` if it has `argument_hint`, return `'argument'`
4. Otherwise → set `query`, `showDropdown = true`, clamp `selectedIndex`, return `'completion'`

**`_complete()` internal helper:**
```javascript
// Builds: before + '/' + playbook.name + ' ' + after.trimStart()
// New caret: before.length + playbook.name.length + 2
```

### API Call
```javascript
// usePlaybookAutocomplete.js:49
const response = await axios.get(`/api/agents/${agentName}/playbooks`, {
  headers: authHeader   // { Authorization: 'Bearer <token>' }
})
playbooks.value = response.data.skills || []
```

## Backend Layer

### Endpoint
- `src/backend/routers/agent_files.py:79` — `GET /{agent_name}/playbooks` → `services/agent_skills_listing.py::list_skills` (trinity-enterprise#754)
- Router prefix: `/api/agents`
- Full path: `GET /api/agents/{agent_name}/playbooks`
- Auth: `AuthorizedAgentByName` dependency (JWT required, agent ACL checked)

### Business Logic
1. Resolve agent name via `AuthorizedAgentByName` dependency
2. Look up Docker container with `get_agent_container(agent_name)`; 404 if missing
3. Reload container state via `container_reload(container)`
4. Return 503 if container status is not `running`
5. Forward request to agent-server: `GET http://agent-{agent_name}:8000/api/skills` (10s timeout)
6. Return agent-server JSON response directly

Steps 2-6 live in `agent_skills_listing.fetch_live`. A live success also refreshes the agent's last-known list in Redis; this caller sends no `last_known`, so its answers are unchanged. See [skills-tab.md](skills-tab.md).

### Error Responses
| Case | Status | Detail |
|------|--------|--------|
| Container not found | 404 | "Agent not found" |
| Container not running | 503 | "Agent is not running. Start the agent to view its skills." |
| Agent-server timeout | 504 | "Agent is starting up, please try again" |
| Connection refused | 503 | "Could not connect to agent" |
| Other exception | 500 | "Failed to fetch playbooks: {error}" |

## Agent Layer

### Agent Server Endpoint
- `docker/base-image/agent_server/routers/skills.py:366` — `GET /api/skills`
- Returns `SkillsResponse { skills: SkillInfo[], count: int, skill_paths: string[] }`

### `SkillInfo` Schema
```python
class SkillInfo(BaseModel):
    name: str
    description: Optional[str] = None
    path: str
    user_invocable: bool = True
    automation: Optional[str] = None   # autonomous | gated | manual | null
    allowed_tools: Optional[List[str]] = None
    argument_hint: Optional[str] = None
    has_schedule: bool = False
    source: Optional[str] = None       # trinity-enterprise#754: "platform" | "agent"
    dir: Optional[str] = None          # trinity-enterprise#754: the directory name
    approval: Optional[str] = None     # trinity-enterprise#754: "recommended" | None
```

The three #754 fields are informational; this popup does not read them ([skills-tab.md](skills-tab.md) → Agent Layer).

### Skill Discovery
Scans the agent's `.claude/skills/` directories for `SKILL.md` files. Parses YAML frontmatter (between `---` delimiters) for skill metadata. Handles BOM characters and CRLF line endings.

## Side Effects
None. This is a read-only feature. No database writes, no WebSocket broadcasts, no activity tracking.

## Autocomplete UX Details

### Dropdown
- Appears above the textarea (floated `bottom-full mb-2`)
- Shows up to 8 items; overflow hint for additional matches
- Highlighted item shown with indigo background + "Tab ⇥" badge
- `@mousedown.prevent` on the container prevents textarea blur before click fires
- `handleBlur` delays `ac.hide()` by 150ms for the same reason

### Ghost Text
- CSS class `.ghost-overlay` (`font-size: 0.875rem; line-height: 1.5rem`) must match textarea metrics exactly for alignment
- Typed portion rendered as `text-transparent`; completion as `text-gray-400`
- Hidden via `aria-hidden="true"` from assistive technology
- `pointer-events: none` — does not intercept input

### Argument Hint Bar
- Appears below the form after completing `/command ` (with trailing space)
- Only shown for playbooks that have a non-null `argument_hint` field
- Default textarea placeholder is hidden while dropdown or arg hint is active (`showPlaceholder` computed)

### Keyboard Contract
| Key | Context | Action |
|-----|---------|--------|
| Tab or ArrowRight | Dropdown open | Accept top/highlighted suggestion |
| ArrowDown | Dropdown open | Move selection down (wraps) |
| ArrowUp | Dropdown open | Move selection up (wraps) |
| Enter | Dropdown open | Accept top/highlighted suggestion |
| Escape | Dropdown open | Dismiss dropdown |
| Enter (no Shift) | No dropdown | Submit message |

## Error Handling
| Error Case | HTTP Status | User-Visible Behavior |
|------------|-------------|----------------------|
| Agent not found | 404 | Autocomplete stays empty; no UI error shown |
| Agent not running | 503 | `ac.load()` not called (guarded by `agentStatus === 'running'` watch) |
| Agent-server timeout | 504 | `load()` catches silently; autocomplete unavailable |
| Network error | any | `load()` catches silently; autocomplete unavailable |

## Testing

### Prerequisites
- Trinity backend running
- At least one agent running with skills in `.claude/skills/`

### Test Steps

1. **Action**: Open Agent Detail → Chat tab for a running agent
   **Expected**: `ChatInput` mounts with `agentStatus === 'running'`
   **Verify**: Network tab shows `GET /api/agents/{name}/playbooks` returns 200

2. **Action**: Type `/` in the chat input
   **Expected**: Dropdown appears above the input listing available playbooks
   **Verify**: `ac.showDropdown === true`, dropdown `div` rendered in DOM

3. **Action**: Continue typing (e.g. `/dep`)
   **Expected**: List filters to playbooks whose name starts with or contains "dep"; ghost text shows remainder of top match
   **Verify**: Fewer items in list; ghost overlay `<span>` contains completion suffix

4. **Action**: Press Tab
   **Expected**: Input value becomes `/deploy ` (command + space); dropdown closes; argument hint bar appears if playbook has `argument_hint`
   **Verify**: `localMessage` value, caret positioned after the space

5. **Action**: Press ↓ then Enter with dropdown open
   **Expected**: Second item in list is selected then accepted
   **Verify**: `ac.selectedIndex === 1` after ↓; Enter commits that entry

6. **Action**: Press Escape with dropdown open
   **Expected**: Dropdown closes; input value unchanged
   **Verify**: `ac.showDropdown === false`

7. **Action**: Click a suggestion in the dropdown
   **Expected**: Command accepted, focus returns to textarea
   **Verify**: `localMessage` updated; textarea focused

8. **Action**: Type a complete command + space when agent is stopped
   **Expected**: No dropdown, no arg hint (playbooks list is empty)
   **Verify**: `ac.playbooks.value` is empty array

## Related Flows
- [authenticated-chat-tab.md](feature-flows/authenticated-chat-tab.md) — the Chat tab that hosts `ChatPanel` and `ChatInput`
- [skills-tab.md](skills-tab.md) — the agent's Skills tab, which runs skills directly (absorbed the Playbooks tab, trinity-enterprise#754)
- [continue-execution-as-chat.md](feature-flows/continue-execution-as-chat.md) — resume mode in the same `ChatPanel`
