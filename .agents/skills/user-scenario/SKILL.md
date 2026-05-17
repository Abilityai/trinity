---
name: user-scenario
description: Create or update user scenario documents that describe concrete end-to-end paths users take through the system
allowed-tools: ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Agent"]
user-invocable: true
---

# User Scenario

Create or update user scenario documents in `docs/user-scenarios/`.

## Purpose

Author structured markdown descriptions of concrete user scenarios — step-by-step paths a user takes to accomplish a specific goal. These serve as context for future sessions, regression awareness during reviews, and onboarding material.

## State Dependencies

| Source | Location | Read | Write |
|--------|----------|------|-------|
| CLI commands | `src/cli/trinity_cli/` | Yes | No |
| Frontend views | `src/frontend/src/views/` | Yes | No |
| Backend routers | `src/backend/routers/` | Yes | No |
| User docs | `docs/user-docs/` | Yes | No |
| Feature flows | `docs/memory/feature-flows/` | Yes | No |
| Scenario index | `docs/user-scenarios/README.md` | Yes | Yes |
| Scenario files | `docs/user-scenarios/**/*.md` | Yes | Yes |

## Process

### Step 1: Determine Scope

Identify what the user wants to document:
- A specific scenario (e.g., "deploy an agent via CLI")
- A category of scenarios (e.g., "all CLI onboarding paths")
- An update to an existing scenario

Check existing scenarios:
```bash
ls docs/user-scenarios/**/*.md 2>/dev/null
```

### Step 2: Trace the User Path

Read the relevant source code to understand the actual steps:
- For CLI scenarios: read `src/cli/trinity_cli/commands/` for the commands involved
- For UI scenarios: read `src/frontend/src/views/` and `src/frontend/src/stores/`
- For API scenarios: read `src/backend/routers/`

Cross-reference with `docs/user-docs/` for user-facing documentation and `docs/memory/feature-flows/` for implementation details.

### Step 3: Write the Scenario

Use this template for each scenario file:

```markdown
# Scenario: <title>

**Persona**: <who is doing this — e.g., "New user with a Claude Code agent repo">
**Goal**: <what they want to accomplish>
**Interface**: <CLI | Web UI | API | MCP>
**Prerequisites**:
- <what must be true before starting>

---

## Steps

### 1. <Step name>

**Action**: What the user does
```
<exact command or UI action>
```

**Expected result**: What should happen

**Notes**: Gotchas, edge cases, or context (optional)

### 2. <Step name>
...

---

## Success Criteria

- <How the user knows they succeeded>

## Error Scenarios

| Problem | Symptom | Resolution |
|---------|---------|------------|
| <what goes wrong> | <what the user sees> | <what to do> |

## Related

- <links to user docs, feature flows, other scenarios>
```

### Step 4: Organize by Category

Place scenarios in subdirectories by interface:

```
docs/user-scenarios/
├── README.md          ← Index of all scenarios
├── cli/               ← CLI-based scenarios
├── ui/                ← Web UI scenarios
├── api/               ← Direct API scenarios
└── mcp/               ← MCP integration scenarios
```

### Step 5: Update the Index

Add or update the entry in `docs/user-scenarios/README.md`. Each entry should include the scenario name, persona, goal, and interface.

## Outputs

- One or more scenario markdown files in `docs/user-scenarios/<category>/`
- Updated `docs/user-scenarios/README.md` index
