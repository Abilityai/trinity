---
name: feature-flow-analysis
description: Create or update a feature flow document for end-to-end understanding of a feature.
allowed-tools: [Read, Write, Edit, Grep, Glob]
user-invocable: true
argument-hint: "<feature-name>"
automation: manual
---

# Feature Flow Analysis

Create or update a feature flow document for end-to-end understanding.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Feature Flows Index | `docs/memory/feature-flows.md` | ✅ | ✅ | Flow index (keep minimal!) |
| Feature Flow Doc | `docs/memory/feature-flows/{name}.md` | ✅ | ✅ | Flow document |
| Frontend Code | `src/frontend/src/` | ✅ | | Vue components |
| Backend Code | `src/backend/` | ✅ | | FastAPI endpoints |
| Agent Code | `docker/base-image/` | ✅ | | Agent server |

## Arguments

- `$ARGUMENTS` - Feature name (e.g., "agent-chat", "credential-injection")

## Process

### Step 1: Identify Feature

If no feature name provided, ask for one.

### Step 2: Trace Execution Path

**Frontend (UI → Store → API)**
- Find UI entry point (Vue component, button/action)
- Trace to store action (Pinia)
- Document API call made

**Backend (Endpoint → Logic → Docker/DB)**
- Find FastAPI endpoint in `src/backend/routers/`
- Trace business logic in `src/backend/services/`
- Document database operations

**Agent (if applicable)**
- Find agent_server endpoint
- Document Claude Code invocation

### Step 3: Document Side Effects

- WebSocket broadcasts
- Activity tracking
- State changes

### Step 4: Document Error Handling

- What can fail?
- HTTP status codes
- Error messages

### Step 5: Add Testing Section

Include step-by-step testing instructions:
```markdown
## Testing
### Prerequisites
- Services running

### Test Steps
1. **Action**: Do X
   **Expected**: Y happens
   **Verify**: Check Z
```

### Step 6: Save Flow Document

Write to: `docs/memory/feature-flows/{feature-name}.md`

### Step 7: Update Index (MINIMAL!)

**CRITICAL**: The index is for navigation, not documentation.

**For NEW flows**, add:
1. ONE row to "Recent Updates" table:
   ```markdown
   | YYYY-MM-DD | FEAT-ID | Feature name | [flow.md](feature-flows/flow.md) |
   ```
2. ONE row to the appropriate category table in "Documented Flows":
   ```markdown
   | Feature Name | [flow.md](feature-flows/flow.md) | One-line description |
   ```

**For UPDATED flows**:
- Only update the flow document itself
- Do NOT add entries to Recent Updates for minor changes
- Only update the index row if name/description changed

**WRONG** (too verbose):
```markdown
| Agent Chat | [agent-chat.md](...) | Chat UI with sessions. **Files**: ChatPanel.vue (363 lines), chat.py:130-450... |
```

**RIGHT** (minimal):
```markdown
| Agent Chat | [agent-chat.md](feature-flows/agent-chat.md) | Real-time chat with agents |
```

## Output Template

```markdown
# Feature: {Feature Name}

## Overview
Brief description.

## Entry Points
- **UI**: `path/to/Component.vue:line` - Action trigger
- **API**: `METHOD /api/endpoint`

## Frontend Layer
### Components
- `Component.vue:line` - handler()

### State Management
- `stores/store.js` - actionName

## Backend Layer
### Endpoints
- `src/backend/routers/file.py:line` - handler()

### Business Logic
1. Step one
2. Step two

## Data Layer
- Query: description
- Update: description

## Side Effects
- WebSocket: `{type, data}`
- Activity: tracked

## Error Handling
- Case → HTTP status

## Testing
### Prerequisites
- Services running

### Test Steps
1. **Action**: X
   **Expected**: Y
   **Verify**: Z

## Related Flows
- [related-flow.md](feature-flows/related-flow.md)
```

## Principle

Information density over completeness. Think debugging notes, not comprehensive docs.

## Completion Checklist

- [ ] Feature identified
- [ ] Frontend traced
- [ ] Backend traced
- [ ] Agent layer documented (if applicable)
- [ ] Side effects listed
- [ ] Testing section added
- [ ] Flow document saved
- [ ] Index updated (ONE LINE only)

## Related Skills

- [sync-feature-flows](../sync-feature-flows/) - Batch update flows from code changes
- [update-docs](../update-docs/) - General documentation updates
