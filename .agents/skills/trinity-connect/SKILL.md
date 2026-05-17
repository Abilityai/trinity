---
name: trinity-connect
description: Listen for real-time events from Trinity agents using WebSocket connection.
allowed-tools: [Bash, Read]
user-invocable: true
argument-hint: "[agent-name] [state-filter]"
automation: manual
---

# Trinity Connect - Real-time Event Listener

Listen for real-time events from Trinity agents using WebSocket connection.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| API Key | Environment `TRINITY_API_KEY` | ✅ | | MCP authentication |
| WebSocket | Trinity backend `/ws/events` | ✅ | | Event stream |
| Agent Events | Trinity platform | ✅ | | Real-time events |

## Prerequisites

1. **MCP API Key**: Get from Trinity Settings → API Keys
2. **websocat**: Install with `brew install websocat` (or wscat via npm)
3. **jq**: Install with `brew install jq`

## Usage

Set your API key and run the listener:

```bash
export TRINITY_API_KEY="trinity_mcp_xxx"
./scripts/trinity-listen.sh [agent-name] [state-filter]
```

### Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `agent-name` | Filter to specific agent or `all` | `all` |
| `state-filter` | Filter by state: `completed`, `started`, `stopped`, `failed` | `all` |

### Examples

```bash
# Listen for all events from all accessible agents
./scripts/trinity-listen.sh

# Listen for events from a specific agent
./scripts/trinity-listen.sh my-research-agent

# Listen for completed events only
./scripts/trinity-listen.sh all completed

# Listen for when a specific agent completes work
./scripts/trinity-listen.sh my-agent completed
```

## Event Types

| Event | Trigger |
|-------|---------|
| `agent_activity` | Chat, task, tool call completions |
| `agent_started` | Agent container started |
| `agent_stopped` | Agent container stopped |
| `schedule_execution_completed` | Scheduled task finished |

## Event-Driven Loop Pattern

Use this pattern for local-remote agent coordination:

```bash
while true; do
    echo "Waiting for agent to complete..."
    EVENT=$(./scripts/trinity-listen.sh my-agent completed)

    # Parse the event
    AGENT=$(echo "$EVENT" | grep -A20 "TRINITY EVENT" | jq -r '.name // .agent_name')

    # React to the event
    echo "Agent $AGENT completed! Processing results..."
    # ... your processing logic here ...
done
```

## Troubleshooting

| Error | Solution |
|-------|----------|
| "TRINITY_API_KEY required" | Export your MCP API key |
| "websocat or wscat required" | Install: `brew install websocat` |
| "Connection closed" | Check backend is running, API key is valid |

## Related

- Feature docs: `docs/memory/feature-flows/trinity-connect.md`
- MCP API Keys: Settings → API Keys in Trinity UI

## Completion Checklist

- [ ] API key configured
- [ ] WebSocket tools installed
- [ ] Connection established
- [ ] Events received and parsed
- [ ] Filters applied correctly
