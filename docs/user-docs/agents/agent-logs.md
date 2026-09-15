# Agent Logs and Telemetry

View container logs for debugging and real-time telemetry metrics in the agent header.

## How It Works

### Logs

1. Open the agent detail page and click the **Logs** tab.
2. A fixed-height scrollable container displays Docker container stdout/stderr.
3. **Auto-refresh (10s)** is a toggle; a line-count selector sets how many lines are fetched, and a refresh button fetches on demand. Auto-scroll is smart: new content scrolls to the bottom automatically, but scrolling stops if you scroll up manually.
4. API: `GET /api/agents/{name}/logs`
5. MCP: `get_agent_logs(name)`

### Live Telemetry (Agent Header)

The agent header bar on the detail page displays live resource metrics:

- **CPU** -- usage percentage
- **Memory** -- usage in MB
- **Network I/O** -- bytes in and out
- **Uptime** -- how long the agent container has been running

Metrics auto-refresh every 10 seconds.

API: `GET /api/agents/{name}/stats`

### Host Telemetry (Dashboard Header)

Host-level CPU, memory, and disk usage are shown in the Dashboard header.

API: `GET /api/telemetry/host`

### Centralized Logging via Vector

All container logs are captured by the Vector log aggregator and written to structured JSON files:

- Platform logs: `/data/logs/platform.json`
- Agent logs: `/data/logs/agents.json`

Logs are enriched with container metadata (name, labels). To query agent logs directly:

```bash
docker exec trinity-vector sh -c "tail -50 /data/logs/agents.json" | jq .
```

### OpenTelemetry

Claude Code agents export OTel metrics including cost, token usage, and productivity. These metrics are available on the Dashboard.

## For Agents

- Anything an agent writes to stdout or stderr inside its container will appear in the Logs tab and be captured by Vector.
- Agents do not need to configure logging -- Vector collects logs automatically from all Trinity containers.
- OTel metrics export is built into the Claude Code agent runtime.

## See Also

- [Managing Agents](managing-agents.md)
- [Creating Agents](creating-agents.md)
