# Collaboration Dashboard Demo

This guide demonstrates how to use the agent collaboration dashboard to visualize real-time agent-to-agent communication.

## Overview

The Trinity collaboration dashboard provides a live graph visualization showing:
- All agents as draggable nodes
- Animated edges when agents communicate via Trinity MCP
- Real-time WebSocket updates
- Collaboration history and statistics

## Prerequisites

1. **Running Services**: All Trinity services must be operational
   ```bash
   ./scripts/deploy/start.sh
   ```

2. **Active Agents**: At least 2 running agents with Trinity MCP configured
   - Agents are auto-configured with Trinity MCP during creation
   - Check `.mcp.json` in agent containers for Trinity MCP settings

3. **Browser**: Modern browser with WebSocket support

## Quick Start

### Step 1: Open the Dashboard

Navigate to the collaboration dashboard in your browser:
```
http://localhost:3000/collaboration
```

For production deployments, use your configured domain.

### Step 2: Run the Demo Script

The demo script triggers multiple agent-to-agent collaborations to demonstrate the visualization:

```bash
python3 /tmp/trigger_collaboration_final.py
```

**What it does:**
1. Authenticates as admin user
2. Prompts you to open the dashboard
3. Waits for you to press ENTER
4. Triggers 8 collaborations with 1.5-second intervals
5. Allows you to watch edges animate in real-time

### Step 3: Observe the Visualization

As the script runs, you'll see:
- **Blue animated edges** appear between communicating agents
- **Flowing dots** along the edges (3-second duration)
- **Edges fade to gray** after animation completes
- **Active collaboration count** updates in the header
- **Collaboration history** displays recent events in the bottom panel

## Dashboard Features

### Interactive Nodes
- **Drag & Drop**: Click and drag nodes to rearrange the graph
- **Status Colors**:
  - 🟢 Green = Running
  - ⚪ Gray = Stopped
  - 🟡 Orange = Starting
  - 🔴 Red = Error
- **Click Through**: "View Details" button navigates to agent detail page
- **Owner Display**: Shows agent owner email

### Graph Controls
- **Zoom**: Mouse wheel or controls in bottom-left corner
- **Pan**: Click and drag on empty canvas space
- **Minimap**: Overview in bottom-right corner
- **Fit View**: Automatically centers all nodes
- **Reset Layout**: Clears saved positions and returns to grid

### Real-Time Features
- **WebSocket Connection**: Green pulsing dot = connected, Red = disconnected
- **Auto-Reconnect**: Attempts reconnection after 5 seconds if disconnected
- **Live Updates**: Agent status changes reflect immediately
- **Position Persistence**: Node positions saved to localStorage

### Collaboration Tracking
- **Active Collaborations**: Count of currently animated edges
- **Last Event Time**: Relative time since last collaboration (e.g., "2m ago")
- **History Panel**: Last 10 collaboration events with timestamps
- **Agent Count**: Total agents in the system

## How It Works

### Backend Detection

When an agent sends a chat message to another agent via Trinity MCP:

1. **MCP Tool Call**: Agent A calls `trinity__chat_with_agent(target="agent-b", message="...")`
2. **Header Injection**: MCP server adds `X-Source-Agent: agent-a` header
3. **Backend Detection**: Chat endpoint detects the header in `src/backend/routers/chat.py:59`
4. **WebSocket Broadcast**: Collaboration event sent to all connected clients
5. **Frontend Animation**: Dashboard animates edge from A → B

### Event Format

```json
{
  "type": "agent_collaboration",
  "source_agent": "agent-a",
  "target_agent": "agent-b",
  "action": "chat",
  "timestamp": "2025-12-01T21:35:00.123456"
}
```

### Edge Animation Lifecycle

1. **Creation**: Edge created with blue color and animated dots
2. **Active State**: `animated: true`, blue color (#3b82f6), 3px stroke
3. **Timer Set**: 3-second timeout starts
4. **Fade**: After 3s, edge becomes gray (#94a3b8), 1.5px stroke
5. **Inactive**: Edge remains visible but no longer animated

## Demo Script Details

**Location**: `/tmp/trigger_collaboration_final.py`

**Demo Agents:**
- **Fred** - Orchestrator: routes tasks, coordinates the ecosystem
- **Ruby** - Content: social media posting, video generation
- **Corbin** - Business: Gmail, Calendar, Contacts, Tasks
- **Cornelius** - Knowledge: Obsidian KB, insight extraction
- **Marvin** - Beliefs: autonomous worldview evolution

**Collaboration Patterns**:
```
1. fred → ruby (content request)
2. ruby → cornelius (get perspective for post)
3. cornelius → marvin (check belief relevance)
4. marvin → fred (belief update notification)
5. fred → corbin (schedule meeting about content)
6. ruby → marvin (request recent insights)
7. cornelius → fred (knowledge synthesis complete)
8. corbin → ruby (calendar confirmed, proceed with post)
```

**Features**:
- JWT authentication
- Rate limiting (1.5s intervals)
- Success/failure reporting
- Comprehensive instructions

## Triggering Collaborations Manually

### Via API

```bash
# Get auth token
TOKEN=$(curl -s -X POST http://localhost:8000/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=YOUR_PASSWORD" | jq -r '.access_token')

# Trigger collaboration from agent-a to agent-b
curl -X POST http://localhost:8000/api/agents/agent-b/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Source-Agent: agent-a" \
  -d '{"message": "Hello from agent-a!"}'
```

**Key**: The `X-Source-Agent` header is what triggers the collaboration visualization.

### Via Trinity MCP

From within an agent that has Trinity MCP configured:

```bash
# Inside agent container
claude

# In Claude prompt
> Use the trinity__chat_with_agent tool to send a message to agent-b saying "Hello!"
```

Trinity MCP automatically adds the `X-Source-Agent` header when calling the chat endpoint.

## Troubleshooting

### No Edges Appearing

**Symptoms**: Nodes visible, but no edges animate when agents communicate

**Causes & Solutions**:
1. **WebSocket Disconnected**: Check connection status indicator
   - Red dot = disconnected
   - Reload page to reconnect
   - Check browser console for WebSocket errors

2. **No X-Source-Agent Header**: Verify requests include the header
   - Check backend logs: `docker logs trinity-backend --tail 50`
   - Should see collaboration event broadcasts

3. **Backend Not Broadcasting**: Ensure WebSocket manager is set
   - Restart backend: `docker-compose restart backend`
   - Check main.py WebSocket manager injection

### Nodes Not Updating

**Symptoms**: Agent status changes don't reflect on dashboard

**Solutions**:
1. Click "Refresh" button in top-right corner
2. Reload page to fetch latest agent data
3. Check WebSocket connection status

### Position Not Saving

**Symptoms**: Node positions reset after page reload

**Solutions**:
1. Check localStorage quota: Open DevTools → Application → localStorage
2. Clear localStorage and try again
3. Use "Reset Layout" button to force grid layout

### Authentication Errors

**Symptoms**: 401/403 errors when triggering collaborations

**Solutions**:
1. Verify JWT token is valid
2. Check user has access to agents
3. Ensure agents exist and are running

## Architecture

### Frontend Components

| Component | Location | Purpose |
|-----------|----------|---------|
| AgentCollaboration.vue | `src/frontend/src/views/` | Main dashboard view |
| AgentNode.vue | `src/frontend/src/components/` | Custom node component |
| collaborations.js | `src/frontend/src/stores/` | Graph state & WebSocket |

### Backend Integration

| File | Function | Purpose |
|------|----------|---------|
| `chat.py` | `broadcast_collaboration_event()` | WebSocket broadcast |
| `chat.py` | `chat_with_agent()` | Detects X-Source-Agent |
| `main.py` | `websocket_endpoint()` | WebSocket connection |

### Data Flow

```
Agent A (Trinity MCP tool call)
    ↓
MCP Server (adds X-Source-Agent header)
    ↓
Backend API (/api/agents/agent-b/chat)
    ↓
Collaboration Event Broadcast (WebSocket)
    ↓
All Connected Dashboards (animate edge A→B)
```

## Future Enhancements

### Planned Features
- **Auto-Layout Algorithms**: Force-directed, hierarchical layouts
- **Persistent History**: Database-backed collaboration logs
- **Edge Weights**: Thickness based on message frequency
- **Agent Grouping**: Cluster by owner, type, or project
- **Export**: PNG/SVG graph export
- **Analytics**: Collaboration heatmaps and metrics

### Current Limitations
1. No collaboration filtering (shows all globally)
2. History lost on page refresh (in-memory only)
3. No historical analytics
4. Grid layout only (no auto-arrange)

## See Also

- **Feature Flow**: `.claude/memory/feature-flows/collaboration-dashboard.md`
- **Requirements**: `.claude/memory/requirements.md` (REQ-9.6)
- **Architecture**: `.claude/memory/architecture.md` (Collaboration Dashboard section)
- **Agent-to-Agent**: `.claude/memory/feature-flows/agent-to-agent-collaboration.md`
