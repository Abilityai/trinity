---
name: update-tests
description: Create or update pytest tests in the tests/ directory and sync the test-runner agent catalog (.claude/agents/test-runner.md). Invokes the test-runner subagent to execute the new/updated tests and verify they pass. Use after building a feature, fixing a bug, or when adding test coverage.
allowed-tools: Read, Edit, Write, Grep, Glob, Bash, Agent
user-invocable: true
---

# Update Tests

## Purpose

Keep the Trinity test suite and its catalog in sync with code changes. When a feature is built or modified, this skill:

1. Adds or updates the corresponding pytest tests under `tests/`
2. Updates the `test-runner` agent definition (`.claude/agents/test-runner.md`) to reflect new test files, counts, and categories
3. Invokes the `test-runner` subagent to run the new/updated tests and confirm they pass

The test-runner agent's catalog is the authoritative index of what is tested. It must not drift from reality.

## State Dependencies

| Source | Location | Read | Write |
|--------|----------|------|-------|
| Test catalog | `.claude/agents/test-runner.md` | ✅ | ✅ |
| Test files | `tests/test_*.py`, `tests/scheduler_tests/`, `tests/agent_server/` | ✅ | ✅ |
| Test fixtures | `tests/conftest.py`, `tests/fixtures/` | ✅ | ➖ (only if needed) |
| Recent code changes | `git log`, `git diff`, current branch | ✅ | ❌ |
| Architecture docs | `docs/memory/architecture.md`, `docs/memory/feature-flows/` | ✅ | ❌ |

## Prerequisites

- The Trinity backend must be running at `http://localhost:8000` for tests that hit the API
- `tests/.venv/` virtualenv must exist (created on first run via `cd tests && python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`)
- Code under test must already be implemented — this skill writes tests for existing code, not the other way around

## Process

### Step 1: Determine What Changed

Identify the feature, bug fix, or area needing test coverage. Sources, in order of preference:

1. **Explicit input**: User-provided description, requirement ID (e.g., `MEM-001`, `SUB-002`), or file paths
2. **Conversation context**: Code that was just written/modified in this session
3. **Git changes**: `git status` for working-tree changes; `git log --oneline -20` for recent commits

If unclear, ask the user one targeted question: *"Which feature/files should I add tests for?"*

### Step 2: Read the Current Catalog

Read `.claude/agents/test-runner.md` end-to-end. Pay attention to:

- **Test Categories** section — where the new test file slots in
- **Recent Test Additions** tables (dated) — append new dated row
- **Tests Needed** section — if the change closes a gap, remove the entry
- **Known Issues / Recently Fixed** — if a bug fix, move to "Recently Fixed"
- **Test Suite Statistics** — update totals if non-trivial change

### Step 3: Locate or Create the Test File

- Map the feature to a test file name following existing conventions: `tests/test_<feature>.py`, `tests/scheduler_tests/test_<topic>.py`, etc.
- Use `Grep`/`Glob` to check whether a test file already exists for this area
- If updating an existing file, read it first to understand fixture usage and patterns
- If creating a new file, model it on a similar recent test file (e.g., `test_subscriptions.py`, `test_capacity.py`)

### Step 4: Write or Update the Tests

Follow Trinity test conventions:

- Use `@pytest.mark.smoke` for fast, no-agent-creation tests
- Use `@pytest.mark.slow` for chat execution tests
- Use the **module-scoped** `created_agent` fixture (not function-scoped — see Performance Notes in test-runner.md)
- Group tests by behavior class (auth, validation, CRUD, edge cases)
- Use the `client` and `admin_token` fixtures from `conftest.py`
- Match the docstring/naming style of neighboring tests

For each new test, decide its tier (smoke / core / slow) and mark accordingly.

### Step 5: Update `.claude/agents/test-runner.md`

Apply edits in this order:

1. **Test Categories table** — add or update the row for the test file with a one-line description and any tags (`[SMOKE]`, `[SMOKE + Agent]`, `[SLOW]`)
2. **Recent Test Additions** — add a dated entry (use today's date in YYYY-MM-DD form). Include test file, description, count
3. **Recently Implemented Tests** table — if this closes a feature gap (`✅ N tests`)
4. **Tests Needed** section — remove entries that this skill just implemented
5. **Test Suite Statistics** — bump totals if the delta is significant (>10 tests)
6. **Known Issues → Recently Fixed** — move rows when this skill fixes a known issue

Keep edits minimal and structurally consistent with existing entries. Do not reformat unrelated sections.

### Step 6: Run the New/Updated Tests via test-runner Agent

Invoke the `test-runner` subagent with a focused scope — only the tests this skill touched:

```
Agent(
  description: "Run updated tests and report",
  subagent_type: "test-runner",
  prompt: "Run only these test files (which were just created/updated): <list of files>.
           Use: cd tests && source .venv/bin/activate && python -m pytest <files> -v --tb=short 2>&1
           Report pass/fail counts, any failures with stack traces, and whether the catalog in
           .claude/agents/test-runner.md accurately reflects what you ran. Under 300 words."
)
```

The subagent runs the targeted tests, not the full suite — both because we only need to validate what changed and because full runs take 15-30 minutes.

### Step 7: Reconcile Failures

If the subagent reports failures:

- **Test bug** (assertion wrong, fixture misuse) → fix the test, re-run
- **Code bug** (real regression) → report to user with details; do NOT silently "fix" by weakening assertions
- **Environment issue** (backend down, missing fixture) → report and ask user to resolve

If passing: confirm the catalog and reality match, then proceed to completion.

### Step 8: Report

Output a concise summary:

```
Tests added/updated: <N> tests in <files>
Catalog updated: .claude/agents/test-runner.md (sections: <list>)
Test run: <N passed, M failed, K skipped> in <duration>
```

## Outputs

- Modified test files under `tests/`
- Updated `.claude/agents/test-runner.md`
- Test execution report from the test-runner subagent
- Summary message to user with file list and test results

## Conventions Reference

**Test file location patterns:**
- API/router tests → `tests/test_<feature>.py`
- Scheduler tests → `tests/scheduler_tests/test_<topic>.py`
- Direct agent tests → `tests/agent_server/test_<topic>.py` (skipped without `TEST_AGENT_NAME`)
- Process engine tests → `tests/process_engine/` (DEPRECATED — do not add tests here)

**Pytest markers:**
- `smoke` — fast, no agent creation, runs in ~45s suite
- `slow` — chat execution, fleet ops; excluded from default core run
- (unmarked) — core tests, run in default `not slow` selection

**Required fixtures** (from `tests/conftest.py`):
- `client` — FastAPI TestClient or httpx client
- `admin_token` — admin JWT for authenticated calls
- `created_agent` — **module-scoped** test agent (one per file, ~30-45s creation cost)

## Self-Improvement

After completing this skill's primary task, consider tactical improvements:

- [ ] **Review execution**: Were there friction points, unclear steps, or inefficiencies?
- [ ] **Identify improvements**: Could error handling, step ordering, or instructions be clearer?
- [ ] **Scope check**: Only tactical/execution changes — NOT changes to core purpose or goals
- [ ] **Apply improvement** (if identified):
  - [ ] Edit this SKILL.md with the specific improvement
  - [ ] Keep changes minimal and focused
- [ ] **Version control** (if in a git repository):
  - [ ] Stage: `git add .claude/skills/update-tests/SKILL.md`
  - [ ] Commit: `git commit -m "refactor(update-tests): <brief improvement description>"`
