# Cost Report

Generate a cost report using OpenTelemetry metrics from the platform operations API.

## Instructions

1. **Fetch cost data from the ops API**:
   - Use Bash to call the costs endpoint:
   ```bash
   curl -s http://backend:8000/api/ops/costs \
     -H "Authorization: Bearer $TRINITY_MCP_API_KEY" \
     -H "Content-Type: application/json"
   ```

2. **Check the response**:
   - If `enabled: false` - OTel is not configured, show setup instructions
   - If `available: false` - OTel is enabled but collector is unreachable
   - If `available: true` - Parse and display the metrics

3. **Generate the report** based on the response:

### When OTel is Available

```
## Platform Cost Report
Generated: {timestamp}

### Summary
- Total Cost: ${summary.total_cost} USD
- Total Tokens: {summary.total_tokens:,}
- Daily Limit: ${summary.daily_limit} ({summary.cost_percent_of_limit}% used)

### Alerts
{For each alert in response.alerts}
- [{severity}] {message}
  Recommendation: {recommendation}
{End for}

### Cost by Model
| Model | Cost | Input Tokens | Output Tokens | Cache Read |
|-------|------|--------------|---------------|------------|
{For each model in cost_by_model}
| {model.model} | ${model.cost} | {model.input_tokens:,} | {model.output_tokens:,} | {model.cache_read_tokens:,} |
{End for}

### Token Breakdown
- Input: {tokens_by_type.input:,}
- Output: {tokens_by_type.output:,}
- Cache Read: {tokens_by_type.cacheRead:,}
- Cache Creation: {tokens_by_type.cacheCreation:,}

### Productivity Metrics
- Sessions: {productivity.sessions}
- Active Time: {productivity.active_time_formatted}
- Commits: {productivity.commits}
- PRs Created: {productivity.pull_requests}
- Lines Added: {productivity.lines_added:,}
- Lines Removed: {productivity.lines_removed:,}

### Notes
{Include any critical/warning alerts or observations}
```

### When OTel is Not Enabled

```
## Cost Report Unavailable

OpenTelemetry metrics are not enabled on this platform.

{Include the setup_instructions from the response}

See docs/DEPLOYMENT.md for detailed setup instructions.
```

### When Collector is Unreachable

```
## Cost Report Error

OTel is enabled but the collector is not reachable.

Error: {error}

Troubleshooting:
1. Check if the otel-collector container is running: `docker ps | grep otel`
2. Check collector logs: `docker logs trinity-otel-collector`
3. Verify network connectivity between backend and collector
```

## API Response Structure

The `/api/ops/costs` endpoint returns:

```json
{
  "enabled": true,
  "available": true,
  "timestamp": "2025-12-20T...",
  "summary": {
    "total_cost": 0.0234,
    "total_tokens": 48093,
    "daily_limit": 50.0,
    "cost_percent_of_limit": 0.05
  },
  "alerts": [
    {
      "severity": "warning",
      "type": "cost_limit_approaching",
      "message": "...",
      "recommendation": "..."
    }
  ],
  "cost_by_model": [
    {
      "model": "Claude Sonnet 4",
      "model_id": "claude-sonnet-4-20250514",
      "cost": 0.0234,
      "input_tokens": 1523,
      "output_tokens": 892,
      "cache_read_tokens": 45678,
      "cache_creation_tokens": 0
    }
  ],
  "tokens_by_type": {
    "input": 1523,
    "output": 892,
    "cacheRead": 45678,
    "cacheCreation": 0
  },
  "productivity": {
    "sessions": 5,
    "active_time_seconds": 3600,
    "active_time_formatted": "1h",
    "commits": 3,
    "pull_requests": 1,
    "lines_added": 42,
    "lines_removed": 15
  }
}
```

## Notes

- Cost data is aggregated across ALL agents on the platform
- Per-agent cost attribution requires agent-level labels (future feature)
- The daily_limit is configurable via `ops_cost_limit_daily_usd` setting
- Metrics are updated every 60 seconds by default
- Historical cost data requires Prometheus/Grafana integration (Phase 3)
