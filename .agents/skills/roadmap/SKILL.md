---
name: roadmap
description: Query GitHub Issues for roadmap priorities, epic rollups, and theme coverage
allowed-tools: [Bash, Read, Write]
user-invocable: true
automation: manual
---

# Roadmap

Query GitHub Issues and Project board for roadmap status, epic progress, and theme coverage.

## Purpose

Provides strategic views of the roadmap:
- **Priority view**: P0/P1 issues by urgency
- **Epic view**: Progress rollup by epic (% complete, blockers, next up)
- **Theme view**: Coverage by strategic theme
- **Issue view**: Deep dive on specific issue with epic context

## State Dependencies

| Source | Location | Read | Write |
|--------|----------|------|-------|
| GitHub Issues | `abilityai/trinity` | Yes | No |
| GitHub Project #6 | `abilityai` org | Yes | No |
| Labels | priority-p0/p1/p2/p3, type-*, status-* | Yes | No |

### Project Constants

```
PROJECT_ID     = PVT_kwDOB8r7us4BRY6-
PROJECT_NUM    = 6
EPIC_FIELD_ID  = PVTSSF_lADOB8r7us4BRY6-zhKSsd8
THEME_FIELD_ID = PVTSSF_lADOB8r7us4BRY6-zhKSr-g
```

## Process

### Step 1: Parse Command Arguments

| Command | Description |
|---------|-------------|
| `/roadmap` | Show P0/P1 priorities (default) |
| `/roadmap epics` | Epic rollup view with progress |
| `/roadmap epic #N` | Deep dive on one epic |
| `/roadmap themes` | Issues grouped by theme |
| `/roadmap all` | All open issues by priority |
| `/roadmap blocked` | Blocked items |
| `/roadmap in-progress` | Work in progress |
| `/roadmap orphans` | Issues without Epic assigned |
| `/roadmap #N` or `/roadmap N` | Single issue with epic context |
| `/roadmap create <title>` | Create new issue |

### Step 2: Query and Display

#### Default (P0/P1 priorities)

```bash
# Fetch project items with Epic/Theme fields
gh project item-list 6 --owner abilityai --format json --limit 200 | python3 -c "
import json, sys
data = json.load(sys.stdin)

# Group by priority from labels
p0, p1 = [], []
for item in data['items']:
    c = item.get('content', {})
    if not c.get('number'):
        continue
    labels = [l['name'] for l in c.get('labels', [])]
    status = item.get('status', '')
    if status == 'Done':
        continue
    epic = item.get('Epic', '')
    row = (c['number'], c['title'][:55], epic or '—', status)
    if 'priority-p0' in labels:
        p0.append(row)
    elif 'priority-p1' in labels:
        p1.append(row)

print('## Roadmap Status\n')
if p0:
    print('### P0 - Blocking/Urgent')
    print('| # | Title | Epic | Status |')
    print('|---|-------|------|--------|')
    for num, title, epic, status in p0:
        print(f'| #{num} | {title} | {epic} | {status} |')
    print()

if p1:
    print('### P1 - Critical Path')
    print('| # | Title | Epic | Status |')
    print('|---|-------|------|--------|')
    for num, title, epic, status in p1:
        print(f'| #{num} | {title} | {epic} | {status} |')
"
```

#### Epics View (`/roadmap epics`)

```bash
gh project item-list 6 --owner abilityai --format json --limit 200 | python3 -c "
import json, sys
from collections import defaultdict

data = json.load(sys.stdin)
epics = defaultdict(lambda: {'todo': 0, 'in_progress': 0, 'blocked': 0, 'items': []})

for item in data['items']:
    c = item.get('content', {})
    if not c.get('number'):
        continue
    status = item.get('status', 'Todo')
    if status == 'Done':
        continue  # Skip closed issues
    epic = item.get('Epic', '') or 'No Epic'
    labels = [l['name'] for l in c.get('labels', [])]
    
    if status == 'In Progress':
        epics[epic]['in_progress'] += 1
        epics[epic]['items'].append(('#' + str(c['number']), c['title'][:40]))
    else:
        if 'status-blocked' in labels:
            epics[epic]['blocked'] += 1
        else:
            epics[epic]['todo'] += 1
        epics[epic]['items'].append(('#' + str(c['number']), c['title'][:40]))

print('## Epic Rollup (Open Issues)\n')
print('| Epic | In Progress | Todo | Blocked | Total |')
print('|------|-------------|------|---------|-------|')

for epic in sorted(epics.keys(), key=lambda e: (e == 'No Epic', e)):
    d = epics[epic]
    total = d['in_progress'] + d['todo'] + d['blocked']
    if total > 0:
        print(f\"| {epic} | {d['in_progress']} | {d['todo']} | {d['blocked']} | {total} |\")

print()
print('### In Progress by Epic')
for epic in sorted(epics.keys(), key=lambda e: (e == 'No Epic', e)):
    d = epics[epic]
    if d['in_progress'] > 0:
        print(f'**{epic}**:')
        for num, title in d['items'][:3]:
            print(f'  - {num} {title}')
"
```

#### Single Epic Deep Dive (`/roadmap epic #N`)

```bash
EPIC_NAME="#20 Audit Trail"  # Replace with matched epic name
gh project item-list 6 --owner abilityai --format json --limit 200 | python3 -c "
import json, sys

EPIC = '$EPIC_NAME'
data = json.load(sys.stdin)

items = {'in_progress': [], 'todo': [], 'blocked': []}
for item in data['items']:
    c = item.get('content', {})
    if not c.get('number'):
        continue
    if item.get('Epic', '') != EPIC:
        continue
    status = item.get('status', 'Todo')
    if status == 'Done':
        continue  # Skip closed issues
    
    labels = [l['name'] for l in c.get('labels', [])]
    tier = item.get('Tier', '') or '—'
    rank = item.get('rank', '?')
    row = (rank, c['number'], tier, c['title'][:50], status)
    
    if status == 'In Progress':
        items['in_progress'].append(row)
    elif 'status-blocked' in labels:
        items['blocked'].append(row)
    else:
        items['todo'].append(row)

total = sum(len(v) for v in items.values())

print(f'## {EPIC} (Open Issues)\n')
print(f'**Total Open**: {total}')
print(f'**In Progress**: {len(items[\"in_progress\"])} | **Todo**: {len(items[\"todo\"])} | **Blocked**: {len(items[\"blocked\"])}\n')

for section, label in [('in_progress', 'In Progress'), ('blocked', 'Blocked'), ('todo', 'Todo')]:
    if items[section]:
        print(f'### {label}')
        print('| Rank | # | Tier | Title |')
        print('|------|---|------|-------|')
        for rank, num, tier, title, _ in sorted(items[section], key=lambda x: x[0] if isinstance(x[0], (int, float)) else 9999):
            print(f'| {rank} | #{num} | {tier} | {title} |')
        print()
"
```

#### Themes View (`/roadmap themes`)

```bash
gh project item-list 6 --owner abilityai --format json --limit 200 | python3 -c "
import json, sys
from collections import defaultdict

data = json.load(sys.stdin)
themes = defaultdict(lambda: {'count': 0, 'items': []})

for item in data['items']:
    c = item.get('content', {})
    if not c.get('number'):
        continue
    status = item.get('status', 'Todo')
    if status == 'Done':
        continue  # Skip closed issues
    theme = item.get('Theme', '') or 'No Theme'
    
    themes[theme]['count'] += 1
    themes[theme]['items'].append(('#' + str(c['number']), c['title'][:45], status))

print('## Theme Coverage (Open Issues)\n')
print('| Theme | Open |')
print('|-------|------|')

for theme in sorted(themes.keys(), key=lambda t: (t == 'No Theme', -themes[t]['count'])):
    d = themes[theme]
    if d['count'] > 0:
        print(f'| {theme} | {d[\"count\"]} |')
"
```

#### Orphans View (`/roadmap orphans`)

```bash
gh project item-list 6 --owner abilityai --format json --limit 200 | python3 -c "
import json, sys

data = json.load(sys.stdin)
orphans = []

for item in data['items']:
    c = item.get('content', {})
    if not c.get('number'):
        continue
    if item.get('status') == 'Done':
        continue
    epic = item.get('Epic', '')
    theme = item.get('Theme', '')
    if not epic or not theme:
        labels = [l['name'] for l in c.get('labels', [])]
        priority = next((l for l in labels if l.startswith('priority-')), '—')
        orphans.append((c['number'], c['title'][:50], epic or '—', theme or '—', priority))

print(f'## Orphan Issues ({len(orphans)} items)\n')
print('Issues missing Epic or Theme assignment:\n')
print('| # | Title | Epic | Theme | Priority |')
print('|---|-------|------|-------|----------|')
for num, title, epic, theme, priority in sorted(orphans, key=lambda x: x[0]):
    print(f'| #{num} | {title} | {epic} | {theme} | {priority} |')
print()
print('Run /groom to assign Epics and Themes to these issues.')
"
```

#### Single Issue with Context (`/roadmap #N`)

```bash
ISSUE_NUM=123  # Replace with actual number
gh issue view $ISSUE_NUM --repo abilityai/trinity --json number,title,body,labels,state,assignees

# Also get epic context
gh project item-list 6 --owner abilityai --format json --limit 200 | python3 -c "
import json, sys

ISSUE = $ISSUE_NUM
data = json.load(sys.stdin)

# Find this issue
issue_epic = None
for item in data['items']:
    c = item.get('content', {})
    if c.get('number') == ISSUE:
        issue_epic = item.get('Epic', '')
        issue_theme = item.get('Theme', '')
        issue_tier = item.get('Tier', '')
        issue_rank = item.get('rank', '?')
        break

if issue_epic:
    # Count epic progress
    done = in_progress = todo = 0
    for item in data['items']:
        if item.get('Epic') == issue_epic:
            status = item.get('status', 'Todo')
            if status == 'Done':
                done += 1
            elif status == 'In Progress':
                in_progress += 1
            else:
                todo += 1
    total = done + in_progress + todo
    print(f'\n**Epic**: {issue_epic} ({done}/{total} complete)')
    print(f'**Theme**: {issue_theme or \"—\"}')
    print(f'**Tier**: {issue_tier or \"—\"} | **Rank**: {issue_rank}')
"
```

#### All Issues (`/roadmap all`)

```bash
gh project item-list 6 --owner abilityai --format json --limit 200 | python3 -c "
import json, sys

data = json.load(sys.stdin)
items = []

for item in data['items']:
    c = item.get('content', {})
    if not c.get('number'):
        continue
    if item.get('status') == 'Done':
        continue
    labels = [l['name'] for l in c.get('labels', [])]
    priority = next((l.replace('priority-', '').upper() for l in labels if l.startswith('priority-')), 'P3')
    items.append((priority, c['number'], c['title'][:55], item.get('Epic', '') or '—'))

items.sort(key=lambda x: ({'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}.get(x[0], 4), x[1]))

print('## All Open Issues\n')
print('| Priority | # | Title | Epic |')
print('|----------|---|-------|------|')
for priority, num, title, epic in items:
    print(f'| {priority} | #{num} | {title} | {epic} |')
"
```

#### Blocked Items (`/roadmap blocked`)

```bash
gh issue list --repo abilityai/trinity --label "status-blocked" --state open \
  --json number,title,labels,assignees \
  --jq '.[] | "| #\(.number) | \(.title[:50]) | \([.assignees[].login] | join(\", \") // \"—\") |"'
```

#### In Progress (`/roadmap in-progress`)

```bash
gh project item-list 6 --owner abilityai --format json --limit 200 | python3 -c "
import json, sys

data = json.load(sys.stdin)
items = []

for item in data['items']:
    c = item.get('content', {})
    if not c.get('number'):
        continue
    if item.get('status') != 'In Progress':
        continue
    epic = item.get('Epic', '') or '—'
    items.append((c['number'], c['title'][:50], epic))

print('## In Progress\n')
print('| # | Title | Epic |')
print('|---|-------|------|')
for num, title, epic in items:
    print(f'| #{num} | {title} | {epic} |')
"
```

### Step 3: Create Issue (if requested)

If user runs `/roadmap create <title>`:

1. Ask for:
   - Priority: p0, p1, p2, p3
   - Type: feature, bug, refactor, docs
   - Epic: (show available epics)
   - Theme: (show available themes)
   - Description (optional)

2. Create issue:
```bash
gh issue create --repo abilityai/trinity \
  --title "Title here" \
  --label "priority-p1,type-feature" \
  --body "Description"
```

3. Add to project with Epic/Theme:
```bash
# Get issue node ID
NODE_ID=$(gh issue view NUMBER --repo abilityai/trinity --json id --jq '.id')

# Add to project
ITEM_ID=$(gh api graphql -f query="mutation {
  addProjectV2ItemById(input: {
    projectId: \"PVT_kwDOB8r7us4BRY6-\",
    contentId: \"$NODE_ID\"
  }) { item { id } }
}" --jq '.data.addProjectV2ItemById.item.id')

# Set Epic field (get option ID first)
gh api graphql -f query='mutation {
  updateProjectV2ItemFieldValue(input: {
    projectId: "PVT_kwDOB8r7us4BRY6-",
    itemId: "'"$ITEM_ID"'",
    fieldId: "PVTSSF_lADOB8r7us4BRY6-zhKSsd8",
    value: {singleSelectOptionId: "OPTION_ID"}
  }) { projectV2Item { id } }
}'
```

4. Return the issue URL

## Outputs

- Formatted tables for each view
- Epic progress bars and rollup stats
- Theme coverage overview
- Orphan detection for grooming
- Links to GitHub for full view

## Quick Reference

| Command | Output |
|---------|--------|
| `/roadmap` | P0/P1 with epic context |
| `/roadmap epics` | All epics with progress bars |
| `/roadmap epic #20` | Deep dive on Audit Trail epic |
| `/roadmap themes` | Coverage by strategic theme |
| `/roadmap orphans` | Issues needing epic/theme assignment |
| `/roadmap #123` | Single issue with epic context |
| `/roadmap all` | All open issues |
| `/roadmap blocked` | Blocked items |
| `/roadmap in-progress` | Active work |
| `/roadmap create X` | Create and categorize new issue |

## Completion Checklist

- [ ] Command arguments parsed correctly
- [ ] GitHub data queried successfully
- [ ] Results formatted with epic/theme context
- [ ] Progress indicators accurate
- [ ] GitHub link provided for full view
