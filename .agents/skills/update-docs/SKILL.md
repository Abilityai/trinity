---
name: update-docs
description: Update project documentation after making changes. Updates architecture, feature flows, and requirements as needed.
allowed-tools: [Read, Write, Edit, Bash, Task]
user-invocable: true
automation: manual
---

# Update Project Documentation

Update project documentation after making changes.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Architecture | `docs/memory/architecture.md` | ✅ | ✅ | System design |
| Feature Flows | `docs/memory/feature-flows/` | ✅ | ✅ | Flow docs |
| Requirements | `docs/memory/requirements.md` | ✅ | ✅ | Feature specs |
| Project Index | `docs/memory/project_index.json` | ✅ | ✅ | Machine state |
| GitHub Issues | `abilityai/trinity` | ✅ | ✅ | Task tracking |

## Process

### Step 1: Update Architecture (if applicable)

If API/schema/integration changed:
- Update `docs/memory/architecture.md`
- Add new endpoints to API table
- Update database schema if changed
- Update architecture diagram if needed

### Step 2: Update Feature Flows (if applicable)

If feature behavior changed:
- Use the `feature-flow-analyzer` agent to update impacted feature flows
- Example: `Task tool with subagent_type=feature-flow-analyzer`
- The agent will automatically analyze and update relevant flow documents in `docs/memory/feature-flows/`

### Step 3: Update Requirements (if applicable)

If feature scope changed:
- Update `docs/memory/requirements.md`
- Mark requirements as completed if done
- Add new requirements if scope expanded

### Step 4: Update Project Index (if applicable)

Update `docs/memory/project_index.json` if:
- New feature implemented (add to `features.implemented`)
- Feature status changed (move between arrays)
- New API endpoints added (update counts)
- Tech stack changed

### Step 5: Update GitHub Issues (if applicable)

- Task completed → close the issue
- New tasks discovered → create issue with priority/type labels

## Principle

Make MINIMAL necessary documentation changes only. Don't add unnecessary detail.
Change history is tracked via git commits — no separate changelog file needed.

## Completion Checklist

- [ ] Architecture updated (if API/schema changed)
- [ ] Feature flows updated (if behavior changed)
- [ ] Requirements updated (if scope changed)
- [ ] Project index updated (if applicable)
- [ ] GitHub Issues updated (if applicable)
