# Tags and Organization

Organize agents with tags and saved system views; tags also drive the Grid view's org overlay on the Dashboard.

## How It Works

1. Tag agents from the agent detail page, or programmatically via API/MCP.
2. Tags appear as colored badges on agent tiles throughout the UI.
3. On the Dashboard's Grid view, `dept-<name>` and `reports-to-<agent>` tags draw department zones and reporting lines — see [Dashboard → Org overlay](../operations/dashboard.md#org-overlay--departments-and-reporting-lines). These org tags can only be set by people, never by agent-scoped keys.
4. On the Dashboard (any view mode), filter by tags. Filters persist across navigation.
5. Create **System Views** to save filter combinations (tags plus other criteria) for quick access.

### Tag Management

- Add or remove individual tags from any agent.
- Replace all tags at once with a bulk set operation.
- Tags are shared across the platform -- any tag applied to one agent is available for others.

### System Views

System Views are saved filters that combine tags with other criteria. Create, update, and delete views to build custom agent groupings that persist across sessions.

## For Agents

### MCP Tools

| Tool | Description |
|------|-------------|
| `list_tags` | List all tags in the system |
| `get_agent_tags` | Get tags for a specific agent |
| `tag_agent` | Add a tag to an agent |
| `untag_agent` | Remove a tag from an agent |
| `set_agent_tags` | Replace all tags on an agent |

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/tags` | GET | List all tags |
| `/api/agents/{name}/tags` | GET | Get tags for an agent |
| `/api/agents/{name}/tags/{tag}` | POST | Add a tag to an agent |
| `/api/agents/{name}/tags/{tag}` | DELETE | Remove a tag from an agent |
| `/api/agents/{name}/tags` | PUT | Replace all tags on an agent |
| `/api/system-views` | GET | List saved system views |
| `/api/system-views` | POST | Create a system view |
| `/api/system-views/{id}` | PUT | Update a system view |
| `/api/system-views/{id}` | DELETE | Delete a system view |

## See Also

- [Dashboard](../operations/dashboard.md)
- [Managing Agents](../agents/managing-agents.md)
