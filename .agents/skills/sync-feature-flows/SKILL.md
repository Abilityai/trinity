---
name: sync-feature-flows
description: Analyze code changes and update affected feature flow documentation. Creates new flows for new features, updates existing flows for modified code.
allowed-tools: [Task, Read, Write, Edit, Grep, Glob, Bash]
user-invocable: true
argument-hint: "[commit-range|file-list|'recent']"
automation: gated
---

# Sync Feature Flows

Analyze code changes and synchronize feature flow documentation.

## Purpose

Keep feature flow documentation in sync with code changes by:
- Detecting which features were affected by recent changes
- Updating existing flow documents with new file paths, line numbers, endpoints
- Creating new flow documents for newly introduced features
- Maintaining a minimal, navigable index in feature-flows.md

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Feature Flows Index | `docs/memory/feature-flows.md` | ✅ | ✅ | Flow index (keep minimal) |
| Feature Flow Docs | `docs/memory/feature-flows/*.md` | ✅ | ✅ | Individual flow documents |
| Git History | `.git` | ✅ | | Recent changes |
| Frontend Code | `src/frontend/src/` | ✅ | | Vue components, stores |
| Backend Code | `src/backend/` | ✅ | | FastAPI routers, services |
| Agent Code | `docker/base-image/` | ✅ | | Agent server |

## Arguments

- `$ARGUMENTS`:
  - `recent` or empty: Analyze last 5 commits
  - `HEAD~N..HEAD`: Specific commit range
  - `file1.py file2.vue`: Specific files to analyze

## Process

### Step 1: Identify Changed Files

```bash
# If 'recent' or no argument
git log --oneline -5 --name-only | grep -E '\.(py|vue|js|ts)$' | sort -u

# If commit range provided
git diff --name-only $ARGUMENTS | grep -E '\.(py|vue|js|ts)$'

# If file list provided
# Use the provided file list directly
```

Store the list of changed files.

### Step 2: Map Files to Features

Analyze changed files and map to feature flows:

| File Pattern | Likely Feature Flow |
|--------------|---------------------|
| `routers/agents.py` | agent-lifecycle.md |
| `routers/chat.py` | execution-queue.md, parallel-headless-execution.md |
| `routers/schedules.py` | scheduling.md |
| `routers/credentials.py` | credential-injection.md |
| `routers/git.py` | github-sync.md |
| `services/agent_service/` | agent-lifecycle.md |
| `services/scheduler_service.py` | scheduling.md |
| `stores/agents.js` | agent-lifecycle.md, agents-page-ui-improvements.md |
| `stores/network.js` | agent-network.md, dashboard-timeline-view.md |
| `views/AgentDetail.vue` | Multiple (check tabs modified) |
| `views/Dashboard.vue` | agent-network.md, dashboard-timeline-view.md |
| `components/*Panel.vue` | Feature-specific (Tasks, Chat, Credentials, etc.) |
| `agent_server/routers/` | Various agent-side flows |

### Step 3: Check Existing Flows

For each identified feature:

```bash
# Check if flow document exists
ls docs/memory/feature-flows/{feature-name}.md 2>/dev/null

# If exists, check last modified date
stat -f "%Sm" docs/memory/feature-flows/{feature-name}.md
```

Categorize:
- **Needs Update**: Flow exists but code changed significantly
- **Needs Creation**: No flow exists for this feature
- **Up to Date**: Flow exists and changes are minor (comments, formatting)

### Step 4: Present Analysis

[APPROVAL GATE]

Present findings to user/agent:

```
## Feature Flow Sync Analysis

### Changed Files
- path/to/file1.py (added/modified/deleted)
- path/to/file2.vue

### Affected Flows

**Needs Update** (existing docs with outdated info):
1. [scheduling.md] - routers/schedules.py changed (lines 150-200)
2. [agent-lifecycle.md] - new endpoint added

**Needs Creation** (new features without docs):
1. [new-feature.md] - new router/component detected

**Up to Date** (no action needed):
- credential-injection.md

### Recommended Actions
1. Update scheduling.md with new endpoint documentation
2. Create new-feature.md flow document

Proceed with updates? [Y/n]
```

Wait for approval before making changes.

### Step 5: Spawn Feature Flow Analyzer

For each flow that needs update or creation:

```
Use the Task tool to spawn the feature-flow-analyzer agent:

Task(
  subagent_type: "feature-flow-analyzer",
  prompt: "Analyze and document/update the {feature-name} feature flow.

  Focus on these changed files:
  - {file1}
  - {file2}

  The flow document is at: docs/memory/feature-flows/{feature-name}.md

  IMPORTANT: When updating feature-flows.md index:
  - Add only ONE row to the appropriate table
  - Keep descriptions to ONE LINE
  - Put all details in the flow document itself, not the index

  If creating new flow: Add to Recent Updates table AND appropriate category table.
  If updating existing flow: Only update the flow document, not the index (unless name/description changed)."
)
```

Run agents sequentially (one at a time) to avoid conflicts in feature-flows.md.

### Step 6: Verify Index Size

After all updates:

```bash
# Check index file size
wc -l docs/memory/feature-flows.md
```

If > 400 lines, the index may be bloated. Review and condense any verbose entries.

### Step 7: Report Completion

```
## Feature Flow Sync Complete

### Updated Flows
- [scheduling.md](docs/memory/feature-flows/scheduling.md) - Updated endpoints
- [agent-lifecycle.md](docs/memory/feature-flows/agent-lifecycle.md) - Added new method

### Created Flows
- [new-feature.md](docs/memory/feature-flows/new-feature.md) - New feature documented

### Index Status
- Lines: {count}
- Status: ✅ Minimal / ⚠️ Needs condensing

### Next Steps
- Review created/updated flows for accuracy
- Run tests to verify documented behavior
```

## For Agent Execution

When triggered by another agent (via MCP or scheduled task):

1. Skip the approval gate (automation: gated becomes autonomous for agent callers)
2. Use conservative updates (only update flows with significant changes)
3. Create summary report in `content/reports/feature-flow-sync-{date}.md`
4. Return JSON summary:
   ```json
   {
     "updated": ["scheduling.md", "agent-lifecycle.md"],
     "created": ["new-feature.md"],
     "skipped": ["credential-injection.md"],
     "index_lines": 325
   }
   ```

## Completion Checklist

- [ ] Changed files identified
- [ ] Files mapped to feature flows
- [ ] Existing flows checked
- [ ] Analysis presented (if manual)
- [ ] Feature-flow-analyzer spawned for each affected flow
- [ ] Index size verified (< 400 lines)
- [ ] Completion report generated

## Error Recovery

| Error | Recovery |
|-------|----------|
| Git not available | Ask user for file list manually |
| Flow document locked | Skip and report, try again later |
| Index too large | Run condensation pass before proceeding |
| Agent spawn fails | Log error, continue with remaining flows |

## Related Flows

- [feature-flow-analysis](../feature-flow-analysis/) - Manual single-flow analysis
- [update-docs](../update-docs/) - General documentation updates
