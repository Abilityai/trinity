---
name: read-docs
description: Load all project documentation into context for this session.
allowed-tools: [Read, Bash]
user-invocable: true
automation: manual
---

# Read Project Documentation

Load all project documentation into context for this session.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Requirements | `docs/memory/requirements.md` | ✅ | | Feature requirements |
| Architecture | `docs/memory/architecture.md` | ✅ | | System design |
| Feature Flows | `docs/memory/feature-flows.md` | ✅ | | Flow index |
| Deployment | `docs/DEPLOYMENT.md` | ✅ | | Deploy guide |
| Testing | `docs/TESTING_GUIDE.md` | ✅ | | Test standards |
| GitHub Issues | `abilityai/trinity` | ✅ | | P0 issues |
| GitHub Project | `abilityai project #6` | ✅ | | Ranked P1 pipeline |
| Git History | `git log` | ✅ | | Recent changes |

## Process

### Step 1: Read Core Documentation

Read these files in full (no summaries until all are loaded):
- `docs/memory/requirements.md` - Feature requirements (SINGLE SOURCE OF TRUTH)
- `docs/memory/architecture.md` - Current system design
- `docs/memory/feature-flows.md` - Feature flow index
- `docs/DEPLOYMENT.md` - Production deployment guide
- `docs/TESTING_GUIDE.md` - Testing approach and standards

### Step 2: Query GitHub Roadmap

Query P0 issues and the ranked P1 pipeline from the GitHub Project:

```bash
# P0 issues (urgent, do immediately)
gh issue list --repo abilityai/trinity --label "priority-p0" --state open --json number,title,labels

# Ranked P1 pipeline from Trinity Roadmap project (sorted by Rank field)
gh project item-list 6 --owner abilityai --format json --limit 100
```

**IMPORTANT**: Parse project items correctly. The `rank`, `status`, and `tier` fields are **top-level** on each item object — NOT nested inside `fieldValues`. Example item structure:
```json
{
  "rank": 1,
  "status": "Todo",
  "tier": "P1a",
  "content": { "number": 128, "title": "..." }
}
```

Parse with:
```python
import json, sys
data = json.load(sys.stdin)
items = sorted([i for i in data['items'] if i.get('status') == 'Todo'], key=lambda x: x.get('rank') or 9999)
for item in items[:15]:
    c = item['content']
    print(f"[{item.get('rank','')}] #{c['number']} [{item.get('tier','')}] {c['title']}")
```

The project includes Rank (1-N) and Tier (P1a/P1b/P1c) fields:
- **P1a** — Do next (bugs + high-impact)
- **P1b** — Important (user-facing features)
- **P1c** — Backlog (architecture + future)

**Find issues NOT on the project board:**

```bash
# Get all open issues with their project board membership
gh issue list --repo abilityai/trinity --state open --limit 100 \
  --json number,title,labels,projectItems \
  --jq '.[] | select(.projectItems | length == 0) | "#\(.number)\t\([.labels[].name] | join(", "))\t\(.title)"'
```

### Step 3: Read Recent Changes from Git History

Get recent changes from git log (the single source of truth for what changed):
```bash
git log --oneline --since="2 weeks ago" -30
```

For more detail on specific changes:
```bash
git log --since="2 weeks ago" --format="%h %s (%an, %ar)" -30
```

Note: `CLAUDE.md` is loaded automatically at session start - no need to read it again.

### Step 4: Understand Project State

- What features are implemented?
- What's currently in progress?
- What are the current priorities from GitHub Issues?

### Step 5: Report Completion

```
Documentation loaded. Ready to work on Trinity.

P0: [P0 issues or "None"]
```

**Top 15 Backlog** (from Trinity Roadmap project, ranked):

| Rank | Tier | Issue | Title |
|------|------|-------|-------|
| 1 | P1a | #NNN | ... |
| ... | | | |

**Not on Board** (open issues missing from Trinity Roadmap):

| Issue | Labels | Title |
|-------|--------|-------|
| #NNN | ... | ... |

If no issues are missing from the board, report "All open issues are on the board."

```
Recent: [most recent git commits]
```

## When to Use

- At the start of a new session
- When you need to understand the current project state
- Before starting work on a new task
- When switching between different areas of the codebase

## Principle

Load context first, then act. Never modify code without understanding the current state.

## Related Skills

| Skill | Purpose |
|-------|---------|
| `/groom` | Audit board coverage, add missing issues, assign ranks and tiers |
| `/sprint` | Pick an issue from the ranked backlog and implement it end-to-end |
| `/roadmap` | Query GitHub Issues for priorities without loading full docs |

## Completion Checklist

- [ ] Requirements.md read
- [ ] Architecture.md read
- [ ] Feature-flows.md read
- [ ] Deployment guide read
- [ ] Testing guide read
- [ ] P0 issues queried
- [ ] Ranked P1 pipeline queried from Project #6
- [ ] Issues not on board identified
- [ ] Recent git history reviewed
- [ ] Top 15 backlog table shown
- [ ] Not-on-board table shown
