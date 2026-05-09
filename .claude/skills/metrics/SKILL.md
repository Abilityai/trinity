---
name: metrics
description: Engineering analytics — velocity, cycle time, bug ratio, backlog health. Run to measure team throughput and development process efficiency.
automation: manual
allowed-tools: [Bash, Read, Write]
user-invocable: true
---

# Engineering Metrics

Analytics report for development performance: velocity, cycle time, bug ratio, and backlog health.

## Purpose

Measure and surface engineering performance using GitHub Issues and project board data. Provides trend analysis and current-state snapshots across four dimensions: throughput (velocity), flow (cycle time + lead time), quality (bug ratio), and backlog health.

## Usage

```
/metrics [mode] [options]
```

**Modes:**

| Mode | What it measures |
|------|-----------------|
| `full` | All four dimensions (default) |
| `velocity` | Complexity points shipped per week |
| `flow` | Cycle time, lead time, WIP |
| `quality` | Bug ratio, stale WIP, bug net flow |
| `backlog` | Tier coverage, age, orphans, unranked |

**Options:**

| Flag | Default | Effect |
|------|---------|--------|
| `--weeks N` | 4 | Lookback window for closed-issue metrics |
| `--deep` | off | Per-issue Events API calls for exact cycle time (slower, ~1s/issue) |

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| GitHub Issues | `abilityai/trinity` | Yes | No | Closed issues with labels and timestamps |
| GitHub Project #6 | `abilityai` org | Yes | No | Complexity, Tier, Status fields |
| GitHub PRs | `abilityai/trinity` | Yes | No | Merge times for PR lead time |
| GitHub Events API | per-issue | Yes | No | Label timestamps for cycle time (`--deep` only) |
| Report archive | `docs/metrics/` | No | Yes | Timestamped markdown report saved each run |

### Project Constants

```
PROJECT_ID  = PVT_kwDOB8r7us4BRY6-
PROJECT_NUM = 6
```

## Prerequisites

- `gh` CLI authenticated with access to `abilityai/trinity`
- `python3` available

## Process

### Step 1: Parse Arguments

Determine mode and options from the invocation. Defaults: mode=`full`, weeks=`4`, deep=`false`.

If no arguments, assume `full` and `--weeks 4`.

### Step 2: Fetch Closed Issues

```bash
WEEKS=4  # replace with parsed value
SINCE=$(python3 -c "
from datetime import datetime, timedelta
print((datetime.utcnow() - timedelta(weeks=$WEEKS)).strftime('%Y-%m-%dT%H:%M:%SZ'))
")

gh issue list --repo abilityai/trinity --state closed --limit 500 \
  --json number,title,labels,closedAt,createdAt \
  --jq "[.[] | select(.closedAt >= \"$SINCE\")]" > /tmp/metrics_closed.json

echo "Closed issues in window: $(python3 -c "import json; print(len(json.load(open('/tmp/metrics_closed.json'))))")"
```

### Step 3: Fetch Project Board (Complexity + Tier + Status)

```bash
gh project item-list 6 --owner abilityai --format json --limit 300 \
  > /tmp/metrics_board.json

python3 -c "
import json
data = json.load(open('/tmp/metrics_board.json'))
print(f'Board items: {len(data[\"items\"])}')
"
```

If board has >300 items, paginate — but the project rarely exceeds this.

### Step 4: Fetch Open Issues (Backlog + WIP)

```bash
gh issue list --repo abilityai/trinity --state open --limit 300 \
  --json number,title,labels,createdAt \
  > /tmp/metrics_open.json

echo "Open issues: $(python3 -c "import json; print(len(json.load(open('/tmp/metrics_open.json'))))")"
```

### Step 5: Compute Velocity

Cross-reference closed issues with board complexity. Group by week and sum points.

```bash
python3 << 'EOF'
import json
from datetime import datetime, timedelta
from collections import defaultdict

closed = json.load(open('/tmp/metrics_closed.json'))
board = json.load(open('/tmp/metrics_board.json'))

# Build complexity lookup from board (by issue number)
complexity_by_num = {}
for item in board['items']:
    c = item.get('content', {})
    num = c.get('number')
    complexity = item.get('complexity')
    if num and complexity:
        complexity_by_num[num] = int(complexity)

# Group closed issues by week and type
weeks = defaultdict(lambda: {'points': 0, 'bugs': 0, 'features': 0, 'issues': 0})
for issue in closed:
    closed_at = datetime.fromisoformat(issue['closedAt'].replace('Z', '+00:00'))
    week = closed_at.strftime('%Y-W%U')
    labels = [l['name'] for l in issue['labels']]
    num = issue['number']
    complexity = complexity_by_num.get(num, 0)
    weeks[week]['points'] += complexity
    weeks[week]['issues'] += 1
    if 'type-bug' in labels:
        weeks[week]['bugs'] += 1
    elif 'type-feature' in labels:
        weeks[week]['features'] += 1

print('\n### Velocity (Complexity Points Shipped)\n')
print(f'{"Week":<12} {"Points":>6} {"Issues":>7} {"Bugs":>5} {"Features":>9}')
print('-' * 45)
total_points = 0
total_issues = 0
for week in sorted(weeks.keys()):
    d = weeks[week]
    total_points += d['points']
    total_issues += d['issues']
    print(f'{week:<12} {d["points"]:>6} {d["issues"]:>7} {d["bugs"]:>5} {d["features"]:>9}')
n_weeks = max(len(weeks), 1)
print('-' * 45)
print(f'{"Average":<12} {total_points//n_weeks:>6} {total_issues//n_weeks:>7}')
print(f'\nTotal points shipped: {total_points} over {n_weeks} weeks')
EOF
```

Note: Issues without a Complexity field on the board count as 0 points. This under-counts velocity until all issues have complexity assigned (run `/groom` to fill gaps).

### Step 6: Compute Flow (Lead Time + Cycle Time + WIP)

**Lead time** (issue created → closed) — always available:

```bash
python3 << 'EOF'
import json
from datetime import datetime

closed = json.load(open('/tmp/metrics_closed.json'))
open_issues = json.load(open('/tmp/metrics_open.json'))

# Lead times
lead_times = []
for issue in closed:
    created = datetime.fromisoformat(issue['createdAt'].replace('Z', '+00:00'))
    closed_at = datetime.fromisoformat(issue['closedAt'].replace('Z', '+00:00'))
    days = (closed_at - created).days
    lead_times.append(days)

if lead_times:
    lead_times.sort()
    n = len(lead_times)
    p50 = lead_times[n // 2]
    p90 = lead_times[int(n * 0.9)]
    avg = sum(lead_times) // n
    print('\n### Flow\n')
    print(f'Lead time (created → closed):')
    print(f'  Median (P50): {p50} days')
    print(f'  P90:          {p90} days')
    print(f'  Average:      {avg} days')

# WIP
wip = [i for i in open_issues if any(l['name'] == 'status-in-progress' for l in i['labels'])]
print(f'\nActive WIP: {len(wip)} issues in progress')

# Stale WIP (>7 days since created — proxy for stale since no label timestamp without --deep)
now = datetime.utcnow().replace(tzinfo=None)
stale = []
for issue in wip:
    created = datetime.fromisoformat(issue['createdAt'].replace('Z', '+00:00')).replace(tzinfo=None)
    age = (now - created).days
    if age > 7:
        stale.append((issue['number'], age, issue['title'][:50]))
if stale:
    print(f'\nStale WIP (>7 days since created): {len(stale)} issues')
    for num, age, title in sorted(stale, key=lambda x: -x[1]):
        print(f'  #{num} ({age}d) {title}')
EOF
```

**Cycle time with `--deep`** (status-in-progress label → closed, requires per-issue API calls):

```bash
# Only run if --deep flag was passed
# Collect issue numbers from closed set
CLOSED_NUMS=$(python3 -c "import json; print(' '.join(str(i['number']) for i in json.load(open('/tmp/metrics_closed.json'))))")

python3 << 'EOF'
import json, subprocess
from datetime import datetime

closed_map = {i['number']: i for i in json.load(open('/tmp/metrics_closed.json'))}
cycle_times = []

for num, issue in closed_map.items():
    try:
        events = json.loads(subprocess.check_output([
            'gh', 'api', f'repos/abilityai/trinity/issues/{num}/events',
            '--jq', '[.[] | select(.event == "labeled" and .label.name == "status-in-progress")] | first | .created_at'
        ], stderr=subprocess.DEVNULL).decode().strip() or 'null')
        if events:
            claimed_at = datetime.fromisoformat(events.replace('Z', '+00:00'))
            closed_at = datetime.fromisoformat(issue['closedAt'].replace('Z', '+00:00'))
            days = (closed_at - claimed_at).days
            if days >= 0:
                cycle_times.append(days)
    except Exception:
        pass

if cycle_times:
    cycle_times.sort()
    n = len(cycle_times)
    print(f'\nCycle time (in-progress → closed, n={n}):')
    print(f'  Median (P50): {cycle_times[n // 2]} days')
    print(f'  P90:          {cycle_times[int(n * 0.9)]} days')
    print(f'  Average:      {sum(cycle_times) // n} days')
else:
    print('\nNo cycle time data found (no status-in-progress label events in window)')
EOF
```

### Step 7: Compute Quality

```bash
python3 << 'EOF'
import json
from datetime import datetime, timedelta
from collections import defaultdict

closed = json.load(open('/tmp/metrics_closed.json'))
open_issues = json.load(open('/tmp/metrics_open.json'))

# Closed: bug vs feature breakdown
closed_bugs = [i for i in closed if any(l['name'] == 'type-bug' for l in i['labels'])]
closed_features = [i for i in closed if any(l['name'] == 'type-feature' for l in i['labels'])]
total_closed = len(closed)

bug_ratio = len(closed_bugs) / total_closed * 100 if total_closed > 0 else 0

# Open bugs — exclude status-in-dev (fixed, awaiting release cut to main)
all_open_bugs = [i for i in open_issues if any(l['name'] == 'type-bug' for l in i['labels'])]
open_bugs = [i for i in all_open_bugs if not any(l['name'] == 'status-in-dev' for l in i['labels'])]
in_dev_bugs = len(all_open_bugs) - len(open_bugs)
open_p1 = [i for i in open_bugs if any(l['name'] in ('priority-p0', 'priority-p1') for l in i['labels'])]

# Bug net flow by week
weeks = defaultdict(lambda: {'opened': 0, 'closed': 0})
for issue in closed_bugs:
    week = datetime.fromisoformat(issue['closedAt'].replace('Z', '+00:00')).strftime('%Y-W%U')
    weeks[week]['closed'] += 1

print('\n### Quality\n')
print(f'Closed this period: {total_closed} total | {len(closed_bugs)} bugs ({bug_ratio:.0f}%) | {len(closed_features)} features')
print(f'\nOpen bugs: {len(open_bugs)} active | {in_dev_bugs} fixed-in-dev (shipping) | {len(open_p1)} are P0/P1')

if open_p1:
    print('\nOpen P0/P1 bugs:')
    for issue in open_p1:
        labels = [l['name'] for l in issue['labels']]
        pri = next((l for l in labels if l.startswith('priority-')), '?')
        print(f'  #{issue["number"]} [{pri}] {issue["title"][:60]}')
EOF
```

### Step 8: Compute Backlog Health

```bash
python3 << 'EOF'
import json
from datetime import datetime

open_issues = json.load(open('/tmp/metrics_open.json'))
board = json.load(open('/tmp/metrics_board.json'))

now = datetime.utcnow().replace(tzinfo=None)

# Build board lookup
board_items = {
    item.get('content', {}).get('number'): item
    for item in board['items']
    if item.get('content', {}).get('number')
}

todo_items = [i for i in board['items'] if i.get('status') == 'Todo']
in_progress = [i for i in board['items'] if i.get('status') == 'In Progress']

# Backlog age (age of Todo items)
ages = []
for item in todo_items:
    num = item.get('content', {}).get('number')
    matched = next((i for i in open_issues if i['number'] == num), None)
    if matched:
        created = datetime.fromisoformat(matched['createdAt'].replace('Z', '+00:00')).replace(tzinfo=None)
        ages.append((now - created).days)

# Tier coverage
tiers = {'P1a': 0, 'P1b': 0, 'P1c': 0, 'none': 0}
for item in todo_items:
    tier = item.get('tier') or 'none'
    tiers[tier] = tiers.get(tier, 0) + 1

# Orphans (no epic or theme) — skip Done
no_epic = [i for i in board['items'] if not i.get('epic') and i.get('status') != 'Done' and i.get('content', {}).get('number')]
no_theme = [i for i in board['items'] if not i.get('theme') and i.get('status') != 'Done' and i.get('content', {}).get('number')]

# Unranked Todo
unranked = [i for i in todo_items if i.get('rank') is None]

# Missing complexity (Todo + In Progress)
no_complexity = [
    i for i in todo_items + in_progress
    if i.get('complexity') is None and i.get('content', {}).get('number')
]

print('\n### Backlog Health\n')
print(f'Todo:        {len(todo_items)} items | P1a: {tiers["P1a"]} | P1b: {tiers["P1b"]} | P1c: {tiers["P1c"]} | No tier: {tiers["none"]}')
print(f'Unranked:    {len(unranked)} Todo items')
print(f'No Complexity: {len(no_complexity)} Todo/In-Progress items (run /groom to fill)')
print(f'Orphans:     {len(no_epic)} no Epic | {len(no_theme)} no Theme')

if ages:
    ages.sort()
    n = len(ages)
    print(f'\nBacklog age: median {ages[n//2]}d | P90 {ages[int(n*0.9)]}d | oldest {ages[-1]}d')
EOF
```

### Step 9: Print Report Header and Summary

Wrap all mode outputs with:

```
## Engineering Metrics Report
Period: [start] → [end] ([N] weeks)
Generated: [timestamp]

[section outputs from Steps 5–8]

---
Note: Velocity undercounts if issues lack Complexity (run /groom to fill).
Cycle time requires --deep for accuracy; lead time is always available.
```

### Step 10: Save Timestamped Report

After printing the report, save it to a timestamped file using the Write tool:

1. Generate filename from the current UTC time: `docs/metrics/YYYY-MM-DDTHHMMSS.md`
2. Create the `docs/metrics/` directory if it does not exist (`mkdir -p docs/metrics`)
3. Assemble the full report content — identical to what was printed in Step 9 (header, all section outputs, footer note)
4. Write the file using the Write tool
5. Print confirmation: `Report saved: docs/metrics/[filename]`

## Interpretation Guide

| Signal | Healthy | Needs attention |
|--------|---------|----------------|
| Velocity trend | Stable or rising | >20% drop week-over-week |
| Lead time P90 | <14 days | >30 days |
| Bug ratio | <30% of closed work | >50% (shipping too many bugs) |
| Stale WIP | 0–2 issues | >5 (context switching or blockers) |
| Backlog P90 age | <60 days | >120 days (stale backlog) |
| Open P0/P1 bugs | 0 | Any (should be prioritized above all work) |

## Error Recovery

| Error | Recovery |
|-------|---------|
| `gh` auth error | Run `gh auth login` |
| Empty board | Verify `gh project item-list 6 --owner abilityai --limit 5` |
| Rate limit with `--deep` | Wait 60s and retry; omit `--deep` for faster run |
| No closed issues in window | Widen with `--weeks 8` |
| Python3 not found | Install via `brew install python3` |

## Related Skills

| Skill | Purpose |
|-------|---------|
| `/groom` | Assigns Complexity, Tier, Rank — fills data gaps this skill measures |
| `/roadmap` | Quick issue query without analytics |
| `/release` | Release cut — resets the velocity baseline |
| `/sprint` | Full dev cycle — generates the data this skill measures |

## Self-Improvement

After completing this skill's primary task, consider tactical improvements:

- [ ] **Review execution**: Were there friction points, unclear steps, or inefficiencies?
- [ ] **Identify improvements**: Could error handling, step ordering, or instructions be clearer?
- [ ] **Scope check**: Only tactical/execution changes — NOT changes to core purpose or goals
- [ ] **Apply improvement** (if identified):
  - [ ] Edit this SKILL.md with the specific improvement
  - [ ] Keep changes minimal and focused
- [ ] **Version control** (if in a git repository):
  - [ ] Stage: `git add .claude/skills/metrics/SKILL.md`
  - [ ] Commit: `git commit -m "refactor(metrics): <brief improvement description>"`
