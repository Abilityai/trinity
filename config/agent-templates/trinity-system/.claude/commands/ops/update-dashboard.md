# Update Dashboard

Gather current platform metrics and update the dashboard.yaml file.

## Instructions

1. **Gather fleet data** using `mcp__trinity__list_agents`:
   - Total agent count
   - Running vs stopped counts
   - Count by health status (healthy/warning/critical)

2. **Get execution stats** via API:
```bash
curl -s "http://backend:8000/api/agents/execution-stats" \
  -H "Authorization: Bearer $TRINITY_MCP_API_KEY"
```
   - Total executions (24h)
   - Success rate
   - Total cost

3. **Get schedule stats** via API:
```bash
curl -s "http://backend:8000/api/ops/schedules" \
  -H "Authorization: Bearer $TRINITY_MCP_API_KEY"
```
   - Total schedules
   - Enabled vs disabled
   - Next scheduled run

4. **Determine overall health**:
   - `healthy` (green): All agents healthy, success rate >90%
   - `degraded` (yellow): Some warnings, or success rate 70-90%
   - `critical` (red): Any critical issues, or success rate <70%

5. **Write dashboard.yaml** to `/home/developer/dashboard.yaml`:

```yaml
title: "Trinity Platform Status"
description: "Real-time platform health and metrics"
refresh: 60

sections:
  - title: "Platform Health"
    layout: grid
    columns: 4
    widgets:
      - type: status
        label: "Platform Status"
        value: "{healthy|degraded|critical}"
        color: "{green|yellow|red}"
        colspan: 1

      - type: metric
        label: "Agents"
        value: {total_agents}
        description: "{running} running, {stopped} stopped"

      - type: metric
        label: "Healthy"
        value: {healthy_count}
        trend: {up if improved, down if worse, omit if same}

      - type: metric
        label: "Issues"
        value: {warning_count + critical_count}
        trend: {down if improved, up if worse}

  - title: "Executions (24h)"
    layout: grid
    columns: 4
    widgets:
      - type: metric
        label: "Tasks"
        value: {execution_count}
        description: "Last 24 hours"

      - type: progress
        label: "Success Rate"
        value: {success_rate}
        color: "{green if >90, yellow if >70, red otherwise}"

      - type: metric
        label: "Cost"
        value: "${total_cost}"
        description: "Last 24 hours"

      - type: text
        content: "Last: {last_execution_time}"
        size: sm
        color: gray

  - title: "Schedules"
    layout: grid
    columns: 3
    widgets:
      - type: metric
        label: "Total"
        value: {schedule_count}
        description: "{enabled} enabled"

      - type: status
        label: "Automation"
        value: "{Active|Paused}"
        color: "{green|gray}"

      - type: text
        content: "Next: {next_run_time}"
        size: sm

  - title: "Agent Status"
    layout: list
    widgets:
      - type: table
        columns:
          - { key: agent, label: Agent }
          - { key: status, label: Status }
          - { key: health, label: Health }
          - { key: context, label: Context }
        rows:
          # One row per agent, e.g.:
          # - { agent: "research-bot", status: "running", health: "healthy", context: "45%" }
        max_rows: 10

  - title: ""
    layout: list
    widgets:
      - type: text
        content: "Updated: {ISO timestamp}"
        size: xs
        color: gray
        align: right
```

6. **Record the same numbers** with the `record_metrics` MCP tool — never a
   `metrics.json` file, which is retired and read by nothing:

```
record_metrics(points=[
  {"metric": "agents_managed", "value": {total_agents}},
  {"metric": "agents_healthy", "value": {healthy_count}},
  {"metric": "agents_unhealthy", "value": {warning_count + critical_count}},
  {"metric": "system_health", "value": "{healthy|degraded|critical}"},
], execution_id="<from your Execution Context block>")
```

No `last_updated` field — Trinity stamps every point, and that stamp is what
the dashboard shows as the metric's freshness.

## Output

After writing the files, confirm:
```
Dashboard updated: {timestamp}
- Agents: {running}/{total} running
- Health: {status}
- Executions (24h): {count} tasks, {rate}% success
- Schedules: {enabled}/{total} enabled
```

## Notes

- Skip yourself (trinity-system) when counting agent health
- Round percentages to whole numbers
- Format costs as "$X.XX"
- Use relative times for "Last" and "Next" when recent (e.g., "2m ago", "in 15m")
- If API calls fail, use "N/A" for those metrics and note the error
