---
name: groom
description: Backlog grooming — audit board coverage, rank issues, assign Epics/Themes, review priority ordering
automation: gated
allowed-tools: [Bash, Read, Write, Edit]
user-invocable: true
---

# Backlog Grooming

Interactive backlog grooming session for the Trinity Roadmap GitHub Project board.

## Purpose

Ensure all open issues are on the board with correct rank, tier, Epic, and Theme. Surfaces orphans (missing Epic/Theme), suggests categorization, and applies changes after user approval.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| GitHub Issues | `abilityai/trinity` | Yes | No | All open issues |
| GitHub Project #6 | `abilityai` org, project 6 | Yes | Yes | Trinity Roadmap board — Rank, Tier, Status, Epic, Theme fields |
| Project Constants | This skill | Yes | No | Project ID, field IDs |

### Project Constants

```
PROJECT_ID     = PVT_kwDOB8r7us4BRY6-
PROJECT_NUM    = 6
RANK_FIELD_ID  = PVTF_lADOB8r7us4BRY6-zg_O1jU
TIER_FIELD_ID  = PVTSSF_lADOB8r7us4BRY6-zg_O1kA
EPIC_FIELD_ID  = PVTSSF_lADOB8r7us4BRY6-zhKSsd8
THEME_FIELD_ID = PVTSSF_lADOB8r7us4BRY6-zhKSr-g
```

### Current Epic Options

Query current options:
```bash
gh project field-list 6 --owner abilityai --format json | python3 -c "
import json,sys
d=json.load(sys.stdin)
for f in d['fields']:
    if f['name'] == 'Epic':
        for o in f.get('options', []):
            print(f\"  {o['id']}: {o['name']}\")
"
```

### Current Theme Options

```bash
gh project field-list 6 --owner abilityai --format json | python3 -c "
import json,sys
d=json.load(sys.stdin)
for f in d['fields']:
    if f['name'] == 'Theme':
        for o in f.get('options', []):
            print(f\"  {o['id']}: {o['name']}\")
"
```

## Prerequisites

- `gh` CLI authenticated with access to `abilityai/trinity`
- Project board field IDs current (verify if mutations fail)

## Process

### SDLC Context

Trinity follows a **4-stage SDLC** (see `docs/DEVELOPMENT_WORKFLOW.md`):

```
 Todo → In Progress → In Dev → Done
```

| Stage | Label | Board Column | Code location |
|-------|-------|--------------|---------------|
| Todo | *(none or `status-ready`)* | Todo | — |
| In Progress | `status-in-progress` | In Progress | feature branch |
| In Dev | `status-in-dev` | In Dev | `origin/dev` (merged, awaiting release) |
| Done | *(issue closed)* | Done | `origin/main` |

**Key implications for grooming:**
- `status-in-dev` issues are **shipping**, not stale — never propose closing them, never re-rank/re-tier them. They auto-close on release PR merge (dev → main).
- `status-*` labels are authoritative; the board column is a mirror. Detect drift between them (Step 1b).
- Grooming primarily acts on the **Todo** column. Leave In Progress/In Dev untouched unless filling missing Theme.

### Step 1: Audit Board Coverage

Find open issues that are NOT on the project board.

```bash
gh issue list --repo abilityai/trinity --state open --limit 200 \
  --json number,title,labels,projectItems \
  --jq '.[] | select(.projectItems | length == 0) | "#\(.number)\t\([.labels[].name] | join(", "))\t\(.title)"'
```

If any are found, add them to the board using GraphQL (the CLI `gh project item-add` is unreliable):

```bash
# For each missing issue, get its node ID and add via GraphQL
for issue in NNN MMM ...; do
  node_id=$(gh issue view $issue --repo abilityai/trinity --json id --jq '.id')
  gh api graphql -f query="mutation {
    addProjectV2ItemById(input: {
      projectId: \"PVT_kwDOB8r7us4BRY6-\",
      contentId: \"$node_id\"
    }) { item { id } }
  }" && echo "Added #$issue" || echo "Failed #$issue"
done
```

Report findings:
- Count of issues not on board
- List each with issue number, labels, title
- Add missing issues to board before continuing

**Verify** issues were added by re-querying the board before proceeding.

### Step 1b: Reconcile status-* Labels with Board Column

Labels are authoritative. If an issue has a `status-*` label but the board column doesn't match, fix the board (the auto-promotion workflow at `.github/workflows/issue-status-on-merge.yml` should keep them in sync — drift indicates a missed promotion).

```bash
gh issue list --repo abilityai/trinity --state open --limit 200 \
  --json number,labels > /tmp/groom_labels.json

python3 << 'EOF'
import json
labels = {x['number']: [l['name'] for l in x['labels']]
          for x in json.load(open('/tmp/groom_labels.json'))}
board = json.load(open('/tmp/groom_board.json'))

LABEL_TO_COL = {
    'status-in-progress': 'In Progress',
    'status-in-dev': 'In Dev',
}
mismatches = []
for item in board['items']:
    n = item.get('content', {}).get('number')
    if not n or n not in labels:
        continue
    col = item.get('status', '') or '(none)'
    for lbl, expected in LABEL_TO_COL.items():
        if lbl in labels[n] and col != expected:
            mismatches.append((n, lbl, expected, col))

print(f'Label↔board mismatches: {len(mismatches)}')
for n, lbl, exp, actual in mismatches:
    print(f'  #{n} has {lbl} but column is {actual!r} (expected {exp!r})')
EOF
```

For each mismatch, propose moving the board column to match the label (auto-fix in Step 5).

### Step 2: Detect Unranked Items

Query all Todo items and identify those without a rank.

**IMPORTANT**: Parse project items correctly. The `rank`, `status`, and `tier` fields are **top-level** on each item object — NOT nested inside `fieldValues` or extracted from labels. Example item structure:
```json
{
  "rank": 1,
  "status": "Todo",
  "tier": "P1a",
  "content": { "number": 128, "title": "...", "labels": [...] }
}
```

```bash
gh project item-list 6 --owner abilityai --format json --limit 200 | python3 -c "
import json, sys
data = json.load(sys.stdin)
unranked = []
for item in data['items']:
    c = item.get('content', {})
    num = c.get('number', 0)
    status = item.get('status', '')
    rank = item.get('rank')
    tier = item.get('tier', '')
    title = c.get('title', '')[:65]
    if status == 'Todo' and rank is None:
        unranked.append((num, tier, title))
unranked.sort(key=lambda x: ({'P1a': 0, 'P1b': 1, 'P1c': 2}.get(x[1], 3), x[0]))
print(f'Unranked Todo items: {len(unranked)}')
for num, tier, title in unranked:
    print(f'  #{num} [{tier or \"NO TIER\"}] {title}')
"
```

Also detect items missing a Tier label (should have P1a/P1b/P1c):

```bash
gh project item-list 6 --owner abilityai --format json --limit 200 | python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data['items']:
    c = item.get('content', {})
    status = item.get('status', '')
    tier = item.get('tier', '')
    if status == 'Todo' and not tier:
        print(f'  #{c.get(\"number\",\"?\")} [NO TIER] {c.get(\"title\",\"\")[:65]}')
"
```

### Step 2b: Detect Orphans (Missing Epic/Theme)

Find items without Epic or Theme assigned — these need categorization.

```bash
gh project item-list 6 --owner abilityai --format json --limit 200 | python3 -c "
import json, sys
data = json.load(sys.stdin)

no_epic = []
no_theme = []

for item in data['items']:
    c = item.get('content', {})
    if not c.get('number'):
        continue
    if item.get('status') == 'Done':
        continue
    
    epic = item.get('epic', '')
    theme = item.get('theme', '')
    labels = [l['name'] for l in c.get('labels', [])]
    priority = next((l for l in labels if l.startswith('priority-')), 'p3')
    row = (c['number'], c['title'][:55], priority)
    
    if not epic:
        no_epic.append(row)
    if not theme:
        no_theme.append(row)

print(f'## Orphan Issues\n')
print(f'**Missing Epic**: {len(no_epic)} items')
print(f'**Missing Theme**: {len(no_theme)} items\n')

if no_epic:
    print('### No Epic Assigned')
    print('| # | Title | Priority |')
    print('|---|-------|----------|')
    for num, title, priority in sorted(no_epic, key=lambda x: x[0])[:15]:
        print(f'| #{num} | {title} | {priority} |')
    if len(no_epic) > 15:
        print(f'... and {len(no_epic) - 15} more')
    print()

if no_theme:
    print('### No Theme Assigned')
    print('| # | Title | Priority |')
    print('|---|-------|----------|')
    for num, title, priority in sorted(no_theme, key=lambda x: x[0])[:15]:
        print(f'| #{num} | {title} | {priority} |')
    if len(no_theme) > 15:
        print(f'... and {len(no_theme) - 15} more')
"
```

**Available Epics** (for assignment):
- #20 Audit Trail (Security)
- #306 Event Bus (Reliability)
- #295 File Storage (Infrastructure)
- #291 Webhooks (Integration)
- #350 Slack Identity (Channels)
- #303 Cloudflare (Infrastructure)

**Available Themes**:
- Security — auth, audit, credentials, access control
- Reliability — event bus, health checks, error handling
- Channels — Slack, Telegram, WhatsApp integrations
- DevEx — CLI, API ergonomics, developer tools
- Monetization — payments, subscriptions, x402
- Infrastructure — Docker, deployment, storage
- UI/UX — frontend, dashboard, visualization

### Step 3: Review Current Ordering

Display the full ranked Todo backlog for review.

```bash
gh project item-list 6 --owner abilityai --format json --limit 200 | python3 -c "
import json, sys
data = json.load(sys.stdin)
items = [i for i in data['items'] if i.get('status') == 'Todo']
items.sort(key=lambda x: x.get('rank') or 9999)
print(f'## Todo Backlog ({len(items)} items)\n')
print('| Rank | Tier | Issue | Title |')
print('|------|------|-------|-------|')
for item in items:
    c = item['content']
    tier = item.get('tier', '') or '—'
    rank = item.get('rank', '?')
    print(f'| {rank} | {tier} | #{c[\"number\"]} | {c[\"title\"][:60]} |')
"
```

Present observations (Todo column only — never flag In Dev as stale):
- Are P1a items ranked highest?
- Are bugs ranked above features within the same tier?
- Are there stale items in Todo that should be closed? (Skip anything with `status-in-dev` — those are shipping in the next release.)
- Are there items that seem mis-tiered?

### Step 4: Propose Changes (APPROVAL GATE)

Based on the audit, propose specific changes:

1. **Rank assignments** for unranked items — slot by tier (P1a first, then P1b, then P1c, then untiered)
2. **Tier suggestions** for items missing tier labels
3. **Epic assignments** for orphan items — match to existing epic or suggest new epic
4. **Theme assignments** for items missing theme — categorize by strategic area
5. **Re-ordering suggestions** for items that seem mis-prioritized
6. **Close candidates** for stale or resolved items in Todo only — **never** propose closing issues with `status-in-dev` (they auto-close at the next release cut per `docs/DEVELOPMENT_WORKFLOW.md` §4a)

**Ranking strategy:**
- Within each tier, prioritize: bugs > security > features > refactors
- Within same type, order by issue number (older first, unless context says otherwise)
- Use fractional ranks (e.g., 8.1, 8.2) to slot between existing ranked items without displacing them

**Epic/Theme assignment strategy:**
- Security issues → Theme: Security, Epic: #20 Audit Trail (if related)
- Reliability issues → Theme: Reliability, Epic: #306 Event Bus (if related)
- Slack/Telegram/WhatsApp → Theme: Channels, Epic: #350 Slack Identity (if Slack)
- Storage/Docker/Deploy → Theme: Infrastructure, Epic: #295 File Storage (if storage)
- Standalone features may need a new Epic created

Present the proposal as a table and **wait for user approval or adjustments** before proceeding.

```
## Proposed Changes

### Rank Assignments (N items)
| Issue | Tier | Proposed Rank | Rationale |
|-------|------|---------------|-----------|

### Tier Suggestions (N items)
| Issue | Current | Proposed | Rationale |
|-------|---------|----------|-----------|

### Epic Assignments (N items)
| Issue | Proposed Epic | Rationale |
|-------|---------------|-----------|

### Theme Assignments (N items)
| Issue | Proposed Theme | Rationale |
|-------|----------------|-----------|

### New Epics Needed
| Epic Name | Theme | Related Issues | Rationale |
|-----------|-------|----------------|-----------|

### Re-ordering (N items)
| Issue | Current Rank | Proposed Rank | Rationale |
|-------|-------------|---------------|-----------|

### Close Candidates (N items)
| Issue | Reason |
|-------|--------|

Approve these changes? (You can modify any row before I apply)
```

### Step 5: Apply Approved Changes

After user approves (with any modifications), apply via GraphQL mutations.

**Set rank (number field):**

```bash
gh api graphql -f query='mutation {
  updateProjectV2ItemFieldValue(input: {
    projectId: "PVT_kwDOB8r7us4BRY6-",
    itemId: "ITEM_NODE_ID",
    fieldId: "PVTF_lADOB8r7us4BRY6-zg_O1jU",
    value: {number: RANK_VALUE}
  }) { projectV2Item { id } }
}'
```

**Set Epic (single-select field):**

First, get the option ID for the epic name:
```bash
gh project field-list 6 --owner abilityai --format json | python3 -c "
import json,sys
d=json.load(sys.stdin)
for f in d['fields']:
    if f['name'] == 'Epic':
        for o in f.get('options', []):
            print(f\"{o['name']}: {o['id']}\")
"
```

Then set the field:
```bash
gh api graphql -f query='mutation {
  updateProjectV2ItemFieldValue(input: {
    projectId: "PVT_kwDOB8r7us4BRY6-",
    itemId: "ITEM_NODE_ID",
    fieldId: "PVTSSF_lADOB8r7us4BRY6-zhKSsd8",
    value: {singleSelectOptionId: "OPTION_ID"}
  }) { projectV2Item { id } }
}'
```

**Set Theme (single-select field):**

```bash
gh api graphql -f query='mutation {
  updateProjectV2ItemFieldValue(input: {
    projectId: "PVT_kwDOB8r7us4BRY6-",
    itemId: "ITEM_NODE_ID",
    fieldId: "PVTSSF_lADOB8r7us4BRY6-zhKSr-g",
    value: {singleSelectOptionId: "THEME_OPTION_ID"}
  }) { projectV2Item { id } }
}'
```

**Add new Epic option** (if creating a new epic):

Note: `updateProjectV2Field` does NOT accept `projectId` — only `fieldId`, `name`, and `singleSelectOptions`. Also, you must pass the **full list** of desired options (new + existing) since this mutation replaces all options. Replacing the list regenerates option IDs, but existing assignments on project items survive because GitHub re-binds them by name. Colors are unquoted enum values (`BLUE`, `PURPLE`, `GREEN`, `ORANGE`, `YELLOW`, `PINK`, `RED`, `GRAY`).

```bash
gh api graphql -f query='mutation {
  updateProjectV2Field(input: {
    fieldId: "PVTSSF_lADOB8r7us4BRY6-zhKSsd8",
    name: "Epic",
    singleSelectOptions: [
      {name: "#20 Audit Trail", color: BLUE, description: ""},
      {name: "#306 Event Bus", color: PURPLE, description: ""},
      {name: "#NNN New Epic Name", color: RED, description: ""}
    ]
  }) { projectV2Field { ... on ProjectV2SingleSelectField { options { id name } } } }
}'
```

**Batch mutations** (up to 10 per GraphQL call):

```bash
gh api graphql -f query='mutation {
  a1: updateProjectV2ItemFieldValue(input: {projectId: "...", itemId: "...", fieldId: "...", value: {number: N}}) { projectV2Item { id } }
  a2: updateProjectV2ItemFieldValue(input: {projectId: "...", itemId: "...", fieldId: "...", value: {singleSelectOptionId: "..."}}) { projectV2Item { id } }
}'
```

**To get item node IDs** (needed for mutations), query via GraphQL since `item-list` doesn't expose them:

```bash
gh api graphql -f query='
{
  organization(login: "abilityai") {
    projectV2(number: 6) {
      items(first: 100) {
        nodes {
          id
          content { ... on Issue { number } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}'
```

Paginate if >100 items (use `after: "CURSOR"` parameter).

After applying, re-query and display the updated backlog to confirm.

## Completion Checklist

- [ ] All open issues are on the project board
- [ ] No `status-*` label drift between issue labels and board column (Step 1b)
- [ ] No `status-in-dev` issue was re-ranked, re-tiered, or proposed for closure
- [ ] All Todo items have a rank
- [ ] All Todo items have a tier (P1a/P1b/P1c) or are intentionally untiered
- [ ] All P1 items have an Epic assigned (or flagged as standalone)
- [ ] All items have a Theme assigned
- [ ] P1a items ranked highest, then P1b, then P1c
- [ ] Bugs ranked above features within same tier
- [ ] User approved all changes before they were applied
- [ ] Final backlog displayed for confirmation

## Error Recovery

| Error | Recovery |
|-------|----------|
| GraphQL mutation fails | Verify project/field IDs haven't changed. Re-query field IDs from Step 2 of State Dependencies |
| Item not found | Issue may have been closed or removed from board. Skip and note |
| Rate limit | Wait and retry. GitHub GraphQL rate limit is 5000 points/hour |
| Pagination miss | Always check `pageInfo.hasNextPage` and follow cursors |

## Related Skills

| Skill | Purpose |
|-------|---------|
| `/sprint` | Pick an issue from the ranked backlog and implement it end-to-end |
| `/read-docs` | Load project documentation and show current backlog state |
| `/roadmap` | Quick query of GitHub Issues without full grooming |

## Self-Improvement

After completing this skill's primary task, consider tactical improvements:

- [ ] **Review execution**: Were there friction points, unclear steps, or inefficiencies?
- [ ] **Identify improvements**: Could error handling, step ordering, or instructions be clearer?
- [ ] **Scope check**: Only tactical/execution changes — NOT changes to core purpose or goals
- [ ] **Apply improvement** (if identified):
  - [ ] Edit this SKILL.md with the specific improvement
  - [ ] Keep changes minimal and focused
- [ ] **Version control** (if in a git repository):
  - [ ] Stage: `git add .claude/skills/groom/SKILL.md`
  - [ ] Commit: `git commit -m "refactor(groom): <brief improvement description>"`
