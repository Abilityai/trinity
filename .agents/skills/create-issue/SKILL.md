---
name: create-issue
description: Create GitHub issue with proper structure, labels, Epic/Theme assignment, and add to Project board
allowed-tools: [Bash, Read]
user-invocable: true
argument-hint: "<title> [--type feature|bug|refactor|docs] [--priority p0|p1|p2|p3]"
---

# Create Issue

Create a properly structured GitHub issue following the Trinity development workflow.

## Purpose

Automate issue creation with:
- Proper labels (priority, type)
- Epic/Theme assignment based on context
- Acceptance criteria template
- Automatic addition to Project #6

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| GitHub Project #6 | `abilityai` org | ✅ | ✅ | Epic/Theme field options, add item |
| GitHub Issues | `abilityai/trinity` | | ✅ | Create issue |
| Conversation | Context | ✅ | | Infer Epic/Theme from discussion |

### Project Constants

```
PROJECT_ID     = PVT_kwDOB8r7us4BRY6-
PROJECT_NUM    = 6
EPIC_FIELD_ID  = PVTSSF_lADOB8r7us4BRY6-zhKSsd8
THEME_FIELD_ID = PVTSSF_lADOB8r7us4BRY6-zhKSr-g
```

## Process

### Step 1: Parse Arguments

Extract from `$ARGUMENTS` and conversation context:

| Field | Source | Default |
|-------|--------|---------|
| **Title** | Required in args | — |
| **Type** | `--type` flag or infer from title | `feature` |
| **Priority** | `--priority` flag or infer from context | `p2` |
| **Description** | Conversation context | Auto-generated |

**Type inference:**
- Title starts with "bug:", "fix:", contains "broken", "fails", "error" → `bug`
- Title starts with "refactor:", contains "cleanup", "reorganize" → `refactor`
- Title starts with "docs:", contains "document", "readme" → `docs`
- Otherwise → `feature`

**Priority inference from conversation:**
- Mentioned as urgent, blocking, critical → `p0`
- Part of current sprint focus, P1 discussion → `p1`
- Important but not urgent → `p2`
- Nice-to-have, low priority mentioned → `p3`

### Step 2: Fetch Current Epic/Theme Options

```bash
gh project field-list 6 --owner abilityai --format json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for field in data['fields']:
    if field['name'] == 'Epic':
        print('EPICS:')
        for opt in field.get('options', []):
            print(f\"  {opt['id']}: {opt['name']}\")
    if field['name'] == 'Theme':
        print('THEMES:')
        for opt in field.get('options', []):
            print(f\"  {opt['id']}: {opt['name']}\")
"
```

### Step 3: Auto-Assign Epic and Theme

Analyze title, description, and conversation context to match:

**Theme matching:**
| Keywords | Theme |
|----------|-------|
| auth, audit, credential, access, permission, token, security | Security |
| event, health, error, retry, timeout, reliability, redis | Reliability |
| slack, telegram, whatsapp, channel, message, notification | Channels |
| cli, api, sdk, developer, tool, dx | DevEx |
| payment, subscription, credit, monetize, x402, nevermined | Monetization |
| docker, deploy, storage, infrastructure, server, volume | Infrastructure |
| ui, frontend, dashboard, view, component, style | UI/UX |

**Epic matching:**
| Keywords | Epic |
|----------|------|
| audit, log, trail, sec-001 | #20 Audit Trail |
| event, bus, websocket, reliability-003 | #306 Event Bus |
| file, storage, upload, download, files-001 | #295 File Storage |
| webhook, trigger, callback, webhook-001 | #291 Webhooks |
| slack, identity, channel | #350 Slack Identity |
| cloudflare, tunnel, public | #303 Cloudflare |

If no match, leave Epic blank (orphan — `/groom` will catch it).

### Step 4: Generate Issue Body

```markdown
## Summary

[One paragraph describing the problem or feature]

## Context

[Why this is needed, what triggered it]

## Acceptance Criteria

- [ ] [Specific, testable criterion 1]
- [ ] [Specific, testable criterion 2]
- [ ] [Specific, testable criterion 3]

## Technical Notes

[Optional: implementation hints, related files, dependencies]
```

Fill in from conversation context. If insufficient context, use placeholders that make it obvious what needs to be filled in.

### Step 5: Create the Issue

```bash
gh issue create --repo abilityai/trinity \
  --title "$TITLE" \
  --label "priority-$PRIORITY,type-$TYPE" \
  --body "$BODY"
```

Capture the issue number from output.

### Step 6: Add to Project Board

```bash
# Get issue node ID
ISSUE_NUM=XXX
NODE_ID=$(gh issue view $ISSUE_NUM --repo abilityai/trinity --json id --jq '.id')

# Add to project
ITEM_ID=$(gh api graphql -f query="mutation {
  addProjectV2ItemById(input: {
    projectId: \"PVT_kwDOB8r7us4BRY6-\",
    contentId: \"$NODE_ID\"
  }) { item { id } }
}" --jq '.data.addProjectV2ItemById.item.id')

echo "Added to project, item ID: $ITEM_ID"
```

### Step 7: Set Epic and Theme Fields

If Epic was matched:
```bash
gh api graphql -f query='mutation {
  updateProjectV2ItemFieldValue(input: {
    projectId: "PVT_kwDOB8r7us4BRY6-",
    itemId: "'"$ITEM_ID"'",
    fieldId: "PVTSSF_lADOB8r7us4BRY6-zhKSsd8",
    value: {singleSelectOptionId: "'"$EPIC_OPTION_ID"'"}
  }) { projectV2Item { id } }
}'
```

If Theme was matched:
```bash
gh api graphql -f query='mutation {
  updateProjectV2ItemFieldValue(input: {
    projectId: "PVT_kwDOB8r7us4BRY6-",
    itemId: "'"$ITEM_ID"'",
    fieldId: "PVTSSF_lADOB8r7us4BRY6-zhKSr-g",
    value: {singleSelectOptionId: "'"$THEME_OPTION_ID"'"}
  }) { projectV2Item { id } }
}'
```

### Step 8: Report Result

```
## Issue Created

**#[number]**: [title]
**URL**: https://github.com/abilityai/trinity/issues/[number]

**Labels**: priority-[p], type-[t]
**Epic**: [epic or "—"]
**Theme**: [theme or "—"]

Added to Trinity Roadmap board (Todo).
```

## Outputs

- GitHub Issue URL
- Issue number
- Confirmation of project board assignment
- Epic/Theme assignment (or note if orphaned)

## Error Recovery

| Error | Recovery |
|-------|----------|
| Missing title | Error: "Title required. Usage: /create-issue <title>" |
| gh CLI not authenticated | Error: "Run `gh auth login` first" |
| Project mutation fails | Warn but don't fail — issue still created, just not on board |
| No Epic/Theme match | Proceed without assignment, note as orphan |

## Examples

```
/create-issue Add copy button to chat responses --type feature --priority p2

/create-issue bug: Slack messages not rendering markdown

/create-issue Webhook trigger support for agent schedules
```
