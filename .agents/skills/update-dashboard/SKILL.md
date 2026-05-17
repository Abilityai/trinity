---
name: update-dashboard
description: Update dashboard.yaml with current Trinity project metrics — codebase stats, dev velocity, and test health
disable-model-invocation: true
user-invocable: true
allowed-tools:
  - Read
  - Write
  - Bash
  - Glob
---

# Update Dashboard

Refresh the Trinity project dashboard with current codebase, development, and test metrics.

## Output Location

Write to: `/home/developer/dashboard.yaml`

---

## STEP 1: Gather Codebase Metrics

Run the following commands from the project root:

```bash
# Backend Python lines of code (excluding __pycache__)
BACKEND_LOC=$(find src/backend -name "*.py" -not -path "*__pycache__*" | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1}')

# Frontend source lines (Vue + JS, exclude dist/ and node_modules/)
FRONTEND_LOC=$(find src/frontend/src -name "*.vue" -o -name "*.js" | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1}')

# Agent server lines
AGENT_LOC=$(find docker/base-image/agent_server -name "*.py" -not -path "*__pycache__*" | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1}')

# Test lines
TEST_LOC=$(find tests -name "*.py" -not -path "*__pycache__*" -not -path "*.venv*" | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1}')

# MCP server lines
MCP_LOC=$(find src/mcp-server -name "*.py" -not -path "*__pycache__*" | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1}')

# File counts
ROUTER_COUNT=$(ls src/backend/routers/*.py 2>/dev/null | wc -l | tr -d ' ')
SKILL_COUNT=$(ls .claude/skills/*/SKILL.md 2>/dev/null | wc -l | tr -d ' ')
TEST_FILE_COUNT=$(find tests -name "test_*.py" -not -path "*.venv*" | wc -l | tr -d ' ')
FRONTEND_COMPONENT_COUNT=$(find src/frontend/src -name "*.vue" | wc -l | tr -d ' ')
```

Compute total LOC:
```bash
TOTAL_LOC=$((BACKEND_LOC + FRONTEND_LOC + AGENT_LOC + TEST_LOC + MCP_LOC))
```

---

## STEP 2: Gather Development Velocity

```bash
# Commits in last 7 days
COMMITS_7D=$(git log --oneline --since="7 days ago" 2>/dev/null | wc -l | tr -d ' ')

# Commits in last 30 days
COMMITS_30D=$(git log --oneline --since="30 days ago" 2>/dev/null | wc -l | tr -d ' ')

# Open issues count
OPEN_ISSUES=$(gh issue list --repo abilityai/trinity --state open --json number --jq length 2>/dev/null || echo "?")

# Closed issues last 7 days
CLOSED_7D=$(gh issue list --repo abilityai/trinity --state closed --json closedAt --jq '[.[] | select(.closedAt > (now - 604800 | todate))] | length' 2>/dev/null || echo "?")

# Current branch
CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")

# Last commit info
LAST_COMMIT_HASH=$(git log --oneline -1 --format="%h" 2>/dev/null)
LAST_COMMIT_MSG=$(git log --oneline -1 --format="%s" 2>/dev/null | head -c 60)
LAST_COMMIT_DATE=$(git log -1 --format="%ci" 2>/dev/null | cut -d' ' -f1)

# Contributors (last 30 days)
CONTRIBUTORS_30D=$(git log --since="30 days ago" --format="%an" | sort -u | wc -l | tr -d ' ')
```

---

## STEP 3: Gather Test Health

```bash
# Total test count (grep for def test_ across all test files)
TOTAL_TESTS=$(grep -r "def test_" tests/ --include="*.py" 2>/dev/null | grep -v ".venv" | wc -l | tr -d ' ')

# Test file count (already gathered above as TEST_FILE_COUNT)

# Smoke test count
SMOKE_TESTS=$(grep -r "@pytest.mark.smoke" tests/ --include="*.py" 2>/dev/null | grep -v ".venv" | wc -l | tr -d ' ')

# Unit test count
UNIT_TESTS=$(grep -r "@pytest.mark.unit" tests/ --include="*.py" 2>/dev/null | grep -v ".venv" | wc -l | tr -d ' ')

# Slow test count
SLOW_TESTS=$(grep -r "@pytest.mark.slow" tests/ --include="*.py" 2>/dev/null | grep -v ".venv" | wc -l | tr -d ' ')

# Core tests (total minus slow)
CORE_TESTS=$((TOTAL_TESTS - SLOW_TESTS))
```

Also check for the most recent test report if available:
```bash
LATEST_REPORT=$(ls -t tests/reports/test-report-*.md 2>/dev/null | head -1)
```
If a report exists, extract pass/fail counts from it.

---

## STEP 4: Query Platform Status (via curl)

```bash
# Get auth token
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d 'username=admin&password=password' 2>/dev/null | \
  python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)

if [ -n "$TOKEN" ]; then
  AGENT_COUNT=$(curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents 2>/dev/null | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
  RUNNING_AGENTS=$(curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents 2>/dev/null | python3 -c "import sys,json; print(len([a for a in json.load(sys.stdin) if a.get('status')=='running']))" 2>/dev/null || echo "?")
  BACKEND_STATUS="healthy"
else
  AGENT_COUNT="?"
  RUNNING_AGENTS="?"
  BACKEND_STATUS="offline"
fi
```

---

## STEP 5: Build Dashboard YAML

Construct the dashboard using gathered values. Write the following to `/home/developer/dashboard.yaml`, substituting all variables:

```yaml
title: "Trinity Platform Dashboard"
refresh: 60

sections:
  - title: "Codebase"
    layout: grid
    columns: 4
    widgets:
      - type: metric
        label: "Total LOC"
        value: {TOTAL_LOC}
        unit: "lines"
        description: "All Python + Vue + JS source"
      - type: metric
        label: "Backend"
        value: {BACKEND_LOC}
        unit: "lines"
        description: "src/backend/ Python"
      - type: metric
        label: "Frontend"
        value: {FRONTEND_LOC}
        unit: "lines"
        description: "src/frontend/src/ Vue + JS"
      - type: metric
        label: "Tests"
        value: {TEST_LOC}
        unit: "lines"
        description: "tests/ Python"

  - title: "Architecture"
    layout: grid
    columns: 4
    widgets:
      - type: metric
        label: "API Routers"
        value: {ROUTER_COUNT}
      - type: metric
        label: "Vue Components"
        value: {FRONTEND_COMPONENT_COUNT}
      - type: metric
        label: "Skills"
        value: {SKILL_COUNT}
      - type: metric
        label: "Test Files"
        value: {TEST_FILE_COUNT}

  - title: "Development Velocity"
    layout: grid
    columns: 3
    widgets:
      - type: metric
        label: "Commits (7d)"
        value: {COMMITS_7D}
      - type: metric
        label: "Commits (30d)"
        value: {COMMITS_30D}
      - type: metric
        label: "Open Issues"
        value: {OPEN_ISSUES}
      - type: metric
        label: "Closed (7d)"
        value: {CLOSED_7D}
      - type: status
        label: "Branch"
        value: "{CURRENT_BRANCH}"
        color: green
      - type: text
        content: "{LAST_COMMIT_HASH} {LAST_COMMIT_MSG}"
        size: small

  - title: "Platform"
    layout: grid
    columns: 3
    widgets:
      - type: status
        label: "Backend"
        value: "{BACKEND_STATUS}"
        color: green
      - type: metric
        label: "Agents"
        value: {AGENT_COUNT}
        description: "{RUNNING_AGENTS} running"
      - type: metric
        label: "Agent Server"
        value: {AGENT_LOC}
        unit: "lines"

  - title: "Test Health"
    layout: grid
    columns: 4
    widgets:
      - type: metric
        label: "Total Tests"
        value: {TOTAL_TESTS}
      - type: metric
        label: "Smoke"
        value: {SMOKE_TESTS}
        description: "Fast, no agent needed"
      - type: metric
        label: "Core"
        value: {CORE_TESTS}
        description: "Standard validation"
      - type: metric
        label: "Slow"
        value: {SLOW_TESTS}
        description: "Chat execution, fleet ops"
```

**Important:** Replace all `{VARIABLE}` placeholders with the actual values gathered in steps 1-4. Do NOT leave any placeholders in the output file.

If `BACKEND_STATUS` is `"offline"`, change the Platform status widget color to `red`.

---

## STEP 6: Write Dashboard

```
Write dashboard.yaml to /home/developer/dashboard.yaml
```

---

## STEP 7: Confirm Update

Report what was updated:

```
Dashboard updated at {timestamp}

Codebase: {TOTAL_LOC} total LOC ({BACKEND_LOC} backend, {FRONTEND_LOC} frontend, {TEST_LOC} tests)
Architecture: {ROUTER_COUNT} routers, {FRONTEND_COMPONENT_COUNT} components, {SKILL_COUNT} skills
Velocity: {COMMITS_7D} commits (7d), {OPEN_ISSUES} open issues
Tests: {TOTAL_TESTS} total ({SMOKE_TESTS} smoke, {CORE_TESTS} core, {SLOW_TESTS} slow)
Platform: {AGENT_COUNT} agents ({RUNNING_AGENTS} running), backend {BACKEND_STATUS}
```
