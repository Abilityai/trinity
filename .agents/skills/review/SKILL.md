---
name: review
description: Pre-landing PR review — analyzes branch diff for structural issues tests don't catch. SQL safety, race conditions, auth boundary violations, scope drift, plan completion audit, and Trinity-specific security patterns.
allowed-tools: [Agent, Bash, Read, Write, Edit, Grep, Glob, AskUserQuestion]
user-invocable: true
argument-hint: "[branch-name]"
automation: gated
---

# /review — Pre-Landing PR Review

Structural code review of the branch diff. Catches what tests miss.

## Purpose

Analyze the current branch's diff against the base branch for issues that automated tests don't catch: data safety, concurrency, auth boundaries, scope drift, and Trinity-specific patterns. Complementary to `/validate-pr` which checks docs/process.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Git Diff | `.git` | ✅ | | Branch changes |
| CLAUDE.md | `CLAUDE.md` | ✅ | | Project rules |
| Architecture | `docs/memory/architecture.md` | ✅ | | System design |
| Requirements | `docs/memory/requirements.md` | ✅ | | Feature specs |
| GitHub Issues | `abilityai/trinity` | ✅ | | Issue context |
| Source Code | `src/`, `docker/` | ✅ | | Full file context |

## Arguments

- No argument: review current branch against `dev` (or the PR's actual base if a PR exists)
- Branch name: review that branch against `dev`

## Process

### Step 0: Detect Base Branch

```bash
git fetch origin dev --quiet 2>/dev/null
BASE=$(gh pr view --json baseRefName -q .baseRefName 2>/dev/null || echo "dev")
echo "BASE: $BASE"
```

Note: release-cut PRs use `main` as base — the `gh pr view` lookup picks that up automatically. The `dev` fallback only applies when no PR exists yet.

### Step 1: Check Branch

```bash
CURRENT=$(git branch --show-current)
echo "CURRENT: $CURRENT"
```

If on the base branch or no diff against it: "Nothing to review — you're on the base branch or have no changes against it." Stop.

```bash
git diff origin/$BASE --stat
```

If no diff, stop.

### Step 2: Scope Drift Detection

Before reviewing code quality, check: **did they build what was requested?**

**Step 2.1: Identify stated intent**

```bash
# Get issue context from branch name (feature/NUMBER-slug)
ISSUE_NUM=$(echo "$CURRENT" | grep -oE '[0-9]+' | head -1)
if [ -n "$ISSUE_NUM" ]; then
  gh issue view "$ISSUE_NUM" --repo abilityai/trinity --json title,body,labels 2>/dev/null
fi

# Get PR description if exists
gh pr view --json body --jq .body 2>/dev/null || true

# Commit messages
git log origin/$BASE..HEAD --oneline
```

**Step 2.2: Compare diff against intent**

```bash
git diff origin/$BASE...HEAD --stat
```

Evaluate:

**SCOPE CREEP detection:**
- Files changed that are unrelated to the stated intent
- New features or refactors not mentioned in the issue
- "While I was in there..." changes that expand blast radius

**MISSING REQUIREMENTS detection:**
- Requirements from issue/PR description not addressed in the diff
- Test coverage gaps for stated requirements
- Partial implementations (started but not finished)

Output:
```
Scope Check: [CLEAN / DRIFT DETECTED / REQUIREMENTS MISSING]
Intent: [1-line summary of what was requested]
Delivered: [1-line summary of what the diff actually does]
[If drift: list each out-of-scope change]
[If missing: list each unaddressed requirement]
```

This is **INFORMATIONAL** — does not block the review.

### Step 3: Get the Full Diff

```bash
git fetch origin $BASE --quiet
git diff origin/$BASE
```

Read the full diff. For large diffs, also get the file list:
```bash
git diff origin/$BASE --name-only
```

### Step 4: Two-Pass Review

Apply the checklist against the diff in two passes.

#### Pass 1: CRITICAL (block merge if found)

**4.1 SQL & Data Safety**
- Raw SQL with string interpolation (check `database.py` and any new DB code)
- Missing parameterized queries
- Unvalidated user input in DB operations
- Mass assignment / bulk update without field restrictions
- Migration that could lose data

**4.2 Race Conditions & Concurrency**
- Shared state modified without locks (check agent operations, container management)
- TOCTOU (time-of-check-time-of-use) patterns
- WebSocket broadcast ordering issues
- Docker container state races (check-then-act patterns)

**4.3 Auth Boundary Violations**
- New endpoints missing `get_current_user` dependency
- Endpoints that don't check resource ownership (user A accessing user B's agents)
- Admin-only operations accessible to regular users
- MCP tool calls without permission checks
- WebSocket connections without authentication

**4.4 Credential Exposure**
- Credential values logged (not just operations)
- Secrets in error messages or API responses
- `.env` values leaked through stack traces
- Hardcoded credentials (even in tests with real values)

#### Pass 2: INFORMATIONAL (review required, don't block)

**4.5 Conditional Side Effects**
- Side effects (DB writes, container ops, WebSocket broadcasts) inside conditional branches that might not execute
- Fire-and-forget async operations without error handling
- Event emissions that could fail silently

**4.6 Magic Numbers & String Coupling**
- Hardcoded port numbers, timeout values, retry counts
- String comparisons for status/type checks (should be enums/constants)
- Duplicated configuration values

**4.7 Dead Code & Consistency**
- Unused imports, functions, or variables introduced by the diff
- Inconsistent naming patterns (mixing camelCase and snake_case)
- Copy-paste code that should be a shared function

**4.8 Error Handling**
- Bare `except:` or `except Exception:` that swallows errors
- Missing error handling on Docker API calls
- HTTP calls without timeout
- File operations without proper cleanup

**4.9 Test Gaps**
- New endpoints without corresponding tests
- New error paths without test coverage
- Changed behavior without updated tests
- Edge cases mentioned in issue but not tested

**4.10 Frontend Concerns** (if Vue/JS files changed)
- `v-html` with user-provided content (XSS)
- Missing loading/error states
- API calls without error handling
- Reactive state mutations outside proper patterns

**4.11 Performance**
- N+1 query patterns (loop with DB call inside)
- Large collections loaded into memory
- Missing pagination on list endpoints
- WebSocket broadcast to all connected clients when targeted would work

**4.12 Enum & Value Completeness**
- New enum value, status, or type constant introduced in the diff
- Use Grep to find ALL files that reference sibling values
- Read those files to check if the new value is handled everywhere

### Step 5: Findings Report

Present findings in this format:

```
## /review Report

**Branch**: [current] → [base]
**Files Changed**: [count] (+[additions]/-[deletions])
**Scope**: [CLEAN / DRIFT DETECTED / REQUIREMENTS MISSING]

### Critical Findings (block merge)
[For each:]
**[C1] [Category]: [title]**
File: `path/to/file.py:123`
Issue: [what's wrong]
Fix: [specific code change needed]
Why: [exploit scenario or data loss scenario]

### Informational Findings (review required)
[For each:]
**[I1] [Category]: [title]**
File: `path/to/file.py:456`
Issue: [what's wrong]
Suggestion: [recommended fix]

### Clean Categories
[List categories where nothing was found — 1 line each stating what was checked]

### Summary
- Critical: [count] — [MUST FIX before merge / none found]
- Informational: [count] — [review recommended]
- Scope: [clean/drift/missing]
```

### Step 6: Fix-First Flow

For each CRITICAL finding, offer to fix it:

```
Found [N] critical issues. Want me to fix them?
A) Fix all critical issues now
B) Fix specific issues (list numbers)
C) Skip — I'll fix manually
```

If user chooses A or B:
- Apply minimal fixes
- Show the diff of each fix
- Re-run the affected checks to verify

### Step 7: Next Steps

After review is complete:

```
Review complete. Next steps:
- [If critical issues] Fix the [N] critical issues before merging
- [If informational] Review the [N] informational findings
- Run /validate-pr [PR number] for docs/process validation
- Run /sync-feature-flows recent to update documentation
```

## Completion Checklist

- [ ] Base branch detected and diff fetched
- [ ] Scope drift detection completed
- [ ] Pass 1 (CRITICAL) review completed
- [ ] Pass 2 (INFORMATIONAL) review completed
- [ ] Enum/value completeness checked (with code outside diff)
- [ ] Findings report generated
- [ ] Fix-first flow offered for critical issues
- [ ] Next steps provided

## Error Recovery

| Error | Recovery |
|-------|----------|
| No diff found | Report "nothing to review" and stop |
| Git fetch fails | Use local refs, warn about stale comparison |
| Issue not found | Continue without scope drift detection |
| Diff too large (>5000 lines) | Focus on CRITICAL pass only, note skipped checks |

## Important Rules

- **Read full files, not just diff hunks.** Context matters — a function might look fine in the diff but be called unsafely elsewhere.
- **Enum completeness requires reading code OUTSIDE the diff.** This is the one category where within-diff review is insufficient.
- **Be specific.** "There might be a security issue" is not a finding. "Line 47 of agents.py passes unsanitized `agent_name` to `subprocess.run`" is a finding.
- **Don't flag style.** This review catches structural issues, not formatting preferences.
- **Framework-aware.** Know FastAPI's dependency injection for auth, Vue's auto-escaping, SQLAlchemy's parameterized queries.
- **PUBLIC REPO.** Never include actual secret values found in the diff in the report.

## Self-Improvement

After completing this skill's primary task, consider tactical improvements:

- [ ] **Review execution**: Were there friction points, unclear steps, or inefficiencies?
- [ ] **Identify improvements**: Could error handling, step ordering, or instructions be clearer?
- [ ] **Scope check**: Only tactical/execution changes — NOT changes to core purpose or goals
- [ ] **Apply improvement** (if identified):
  - [ ] Edit this SKILL.md with the specific improvement
  - [ ] Keep changes minimal and focused
- [ ] **Version control** (if in a git repository):
  - [ ] Stage: `git add .claude/skills/review/SKILL.md`
  - [ ] Commit: `git commit -m "refactor(review): <brief improvement description>"`
