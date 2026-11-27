# Autonomous Agent Design Guide

> Designing Trinity agents that run autonomously via scheduled commands

---

## The Autonomy Lifecycle

Trinity agents achieve autonomy through a three-phase development cycle:

```
1. DEVELOP          2. PACKAGE              3. SCHEDULE
   Interactive         Slash Command           Trinity Scheduler
   ──────────────►     ──────────────►         ──────────────►

   Refine procedure    Codify as               Run on cron
   with Claude         .claude/commands/       via UI or template
```

**Principle**: Don't schedule raw prompts. Develop and validate procedures interactively first, then package the proven procedure as a command.

---

## Phase 1: Develop the Procedure

Work interactively with your agent to refine a task until it consistently produces good results.

```
You: Check all containers for memory leaks and report findings

Claude: [performs task, you observe results]

You: Add container restart recommendations when memory exceeds 80%

Claude: [improved procedure]

You: Also include the last restart timestamp in the report
```

**When the procedure is stable**:
- It produces consistent, useful output
- It handles edge cases appropriately
- It completes without requiring clarification

Move to Phase 2.

---

## Phase 2: Package as a Slash Command

Create a command file in `.claude/commands/` that codifies your refined procedure.

### Command Structure

```
.claude/commands/
├── daily-health-check.md      # /daily-health-check
├── weekly-report.md           # /weekly-report
└── ops/
    └── memory-audit.md        # /memory-audit (namespace: ops)
```

### Command Template

```markdown
---
description: Brief description (shown in help)
allowed-tools: Tool1, Tool2, Bash(specific:*)
---

# Task Name

## Objective
Clear statement of what this task accomplishes.

## Procedure

1. **Step one**: Description with specific tool usage
   - Use `mcp__trinity__list_agents` to get all agents

2. **Step two**: Next action
   - Check [specific condition]
   - If [threshold], then [action]

3. **Output format**:
   ```
   ## Report Title
   Generated: {timestamp}

   ### Summary
   - Key finding 1
   - Key finding 2

   ### Details
   [Structured output]
   ```

## Edge Cases
- If no agents are running: Report "Fleet idle"
- If API errors: Log error, continue with available data
```

### Design Principles for Scheduled Commands

| Principle | Why | Example |
|-----------|-----|---------|
| **Self-contained** | No user input during execution | Include all parameters in the command |
| **Deterministic output** | Consistent format for parsing/alerts | Use structured markdown templates |
| **Graceful degradation** | Partial results better than failure | Continue on individual errors |
| **Bounded scope** | Predictable runtime and cost | Limit iterations, set timeouts |
| **Idempotent** | Safe to run multiple times | Read and report, don't mutate state |

---

## Phase 3: Schedule via Trinity

### Option A: Template Definition (Recommended)

Define schedules in `template.yaml` for consistent deployment:

```yaml
name: my-autonomous-agent
description: Agent with scheduled tasks

schedules:
  - name: Daily Health Check
    cron: "0 9 * * *"           # 9 AM daily
    message: "/daily-health-check"
    timezone: "America/New_York"
    enabled: true

  - name: Weekly Summary
    cron: "0 10 * * 1"          # 10 AM Monday
    message: "/weekly-report"
    timezone: "UTC"
    enabled: true
```

### Option B: UI Configuration

1. Open agent in Trinity UI
2. Go to **Schedules** tab
3. Click **New Schedule**
4. Set cron expression, message (your slash command), timezone
5. Enable the schedule

### Cron Expression Reference

| Pattern | Meaning |
|---------|---------|
| `0 9 * * *` | Daily at 9 AM |
| `0 9 * * 1-5` | Weekdays at 9 AM |
| `0 */6 * * *` | Every 6 hours |
| `*/30 * * * *` | Every 30 minutes |
| `0 0 1 * *` | First day of month |

---

## Example: Complete Autonomous Agent

### `template.yaml`

```yaml
name: fleet-monitor
display_name: Fleet Monitor
description: Autonomous fleet health monitoring with scheduled checks

type: operations

capabilities:
  - health-monitoring
  - alerting
  - reporting

slash_commands:
  - name: /health-check
    description: Check fleet health and report issues
  - name: /cost-report
    description: Generate daily cost summary
  - name: /idle-cleanup
    description: Identify and report idle agents

schedules:
  - name: Morning Health Check
    cron: "0 8 * * *"
    message: "/health-check"
    timezone: "UTC"
    enabled: true

  - name: Evening Cost Report
    cron: "0 18 * * *"
    message: "/cost-report"
    timezone: "UTC"
    enabled: true
```

### `.claude/commands/health-check.md`

```markdown
---
description: Automated fleet health check with issue detection
allowed-tools: mcp__trinity__list_agents, mcp__trinity__get_agent
---

# Fleet Health Check

## Objective
Identify unhealthy agents and generate actionable report.

## Procedure

1. **List all agents** using `mcp__trinity__list_agents`

2. **For each running agent, evaluate**:
   - Context usage (warn >75%, critical >90%)
   - Last activity (warn >30min, critical >60min idle)
   - Container status

3. **Classify health**:
   - HEALTHY: All metrics normal
   - WARNING: Approaching thresholds
   - CRITICAL: Requires immediate attention

4. **Generate report**:

```
## Fleet Health Report
Generated: {timestamp}

### Status: {HEALTHY|DEGRADED|CRITICAL}

### Critical Issues
{List or "None"}

### Warnings
{List or "None"}

### Summary
- Total: X agents
- Healthy: X
- Warning: X
- Critical: X
```

## Error Handling
- If agent unreachable: Log as "Status Unknown"
- If API timeout: Report partial results with note
```

---

## Monitoring Scheduled Executions

### View Execution History

- **UI**: Agent Detail > Executions tab
- **API**: `GET /api/agents/{name}/executions`

### Execution Data Captured

| Field | Description |
|-------|-------------|
| `status` | success / failed |
| `duration_ms` | Execution time |
| `cost` | USD cost of execution |
| `context_used` | Tokens consumed |
| `response` | Agent output (truncated) |
| `tool_calls` | Tools used during execution |

### WebSocket Events

Subscribe to real-time execution updates. A scheduled run arrives as `agent_activity`:
- started: `activity_type: "chat_start"`, `activity_state: "started"`, `details.triggered_by: "schedule"`
- ended: the same `activity_id` with `activity_state` `completed`, `failed` or `cancelled`

A skipped run sends no event. It appears in the schedule's execution history with status `skipped`.

---

## Best Practices

### 1. Keep Commands Focused
One command = one task. Don't combine unrelated operations.

```markdown
# Good: Single purpose
/health-check     # Check health
/cost-report      # Report costs
/restart-unhealthy # Restart unhealthy agents

# Bad: Kitchen sink
/do-everything    # Check health, report costs, restart agents, send alerts...
```

### 2. Use Structured Output
Scheduled tasks run unattended. Output should be parseable.

```markdown
## Output Format (always include)
- Timestamp
- Status summary (one line)
- Structured details
- Next steps / recommendations
```

### 3. Bound Execution Time
Scheduled tasks share the execution queue. Long tasks block others.

```markdown
## Scope Limits
- Process maximum 50 agents per run
- Timeout individual operations at 30s
- Skip detailed analysis if >20 items need attention
```

### 4. Handle the "Nothing to Report" Case
Don't generate empty or confusing output when everything is fine.

```markdown
## If no issues found:
Report: "All systems healthy. X agents checked, no issues detected."
```

### 5. Log for Debugging
Include enough context to debug failures without re-running.

```markdown
## Always include:
- What was checked
- What criteria were used
- What was found (even if nothing)
```

---

## Troubleshooting

| Issue | Cause | Solution |
|-------|-------|----------|
| Schedule doesn't run | Disabled or invalid cron | Check Schedules tab, verify cron syntax |
| Execution fails immediately | Agent stopped | Ensure agent is running before schedule fires |
| Queue full (429) | Too many concurrent requests | Space out schedules, reduce frequency |
| High context after execution | Long output or many tool calls | Simplify command, reset session periodically |
| Inconsistent results | Command not deterministic | Remove ambiguity, add explicit criteria |

---

## Quick Reference

### File Locations

| Purpose | Path |
|---------|------|
| Slash commands | `.claude/commands/*.md` |
| Template config | `template.yaml` |
| Agent instructions | `CLAUDE.md` |

### Schedule Configuration

```yaml
schedules:
  - name: "Display Name"
    cron: "minute hour day month weekday"
    message: "/your-command"
    timezone: "UTC"
    enabled: true
```

### Command Frontmatter

```yaml
---
description: Brief description for help
allowed-tools: Tool1, Tool2, Bash(pattern:*)
---
```

---

## See Also

- [Scheduling Feature Flow](memory/feature-flows/scheduling.md) - Technical implementation
- [Agent Template Spec](AGENT_TEMPLATE_SPEC.md) - Full template.yaml reference
- [Trinity Compatible Agent Guide](TRINITY_COMPATIBLE_AGENT_GUIDE.md) - Comprehensive agent development
