---
name: sprint
description: Full development cycle — pick issue, plan review, implement, code review, sync docs, commit to feature branch, and create PR. Orchestrates /cso, /autoplan, /implement, /review, /validate-pr, /sync-feature-flows.
allowed-tools: [Agent, Bash, Edit, Glob, Grep, Read, Skill, Write, AskUserQuestion]
user-invocable: true
argument-hint: "[issue-number]"
automation: gated
---

# Sprint

Full development cycle from issue selection to pull request.

## Purpose

Automate the complete Trinity development workflow:
1. Select the highest-priority issue from the backlog
2. Validate requirements and acceptance criteria
3. Create a feature branch
4. Plan review (via `/autoplan`) — strategy + eng + security review
5. Human reviews and approves the plan
6. Implement the feature (via `/implement`)
7. Pre-landing code review (via `/review`)
8. Security audit (via `/cso --diff`) — recommended for P0/P1
9. Verify tests exist and pass
10. Sync feature flow documentation (via `/sync-feature-flows`)
11. Commit, push, and create a PR

## Pipeline Overview

```
/sprint X → /autoplan → approve → /implement → /review → /cso --diff → /sync-feature-flows → PR
```

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| GitHub Project Board | `abilityai project #6` | ✅ | ✅ | Ranked issue pipeline |
| GitHub Issues | `abilityai/trinity` | ✅ | ✅ | Issue details, labels |
| Requirements | `docs/memory/requirements.md` | ✅ | ✅ | Feature requirements |
| Architecture | `docs/memory/architecture.md` | ✅ | ✅ | System design |
| Feature Flows | `docs/memory/feature-flows/` | ✅ | ✅ | Feature documentation |
| Source Code | `src/`, `docker/base-image/` | ✅ | ✅ | Implementation |
| Tests | `tests/` | ✅ | ✅ | Test files |
| Git | `.git/` | ✅ | ✅ | Branches, commits |

## Arguments

- `$ARGUMENTS`:
  - Empty: Present backlog sorted by board rank for user selection
  - `recent`: Show recently created open issues (last 30 days)
  - Issue number: `#68` or `68` — work on a specific issue

## Prerequisites

- Git working tree is clean (`git status` shows no uncommitted changes)
- On `dev` branch (default base for feature work)
- Docker running (if implementation touches containers)

## Process

### Step 1: Select Issue

**If issue number provided in `$ARGUMENTS`:**
```bash
gh issue view ${ARGUMENTS#\#} --repo abilityai/trinity --json number,title,body,labels,state
```

**If no argument — present backlog for user selection:**

```bash
# Get ALL Todo items from Trinity Roadmap project, sorted by rank
gh project item-list 6 --owner abilityai --format json --limit 200
```

**IMPORTANT**: Parse project items correctly. The `rank`, `status`, and `tier` fields are **top-level** on each item object — NOT nested inside `fieldValues` or extracted from labels. Example item structure:
```json
{
  "rank": 1,
  "status": "Todo",
  "tier": "P1a",
  "content": { "number": 128, "title": "...", "labels": [...] }
}
```

Parse with:
```python
import json, sys
data = json.load(sys.stdin)
items = [i for i in data['items'] if i.get('status') == 'Todo']
items.sort(key=lambda x: x.get('rank') or 9999)
for item in items[:15]:
    c = item['content']
    tier = item.get('Tier', '') or '—'
    epic = item.get('Epic', '') or '—'
    rank = item.get('rank', '?')
    print(f"| {rank} | {tier} | #{c['number']} | {c['title'][:50]} | {epic[:20]} |")
```

Present as a ranked table:

```
## Backlog (Todo) — sorted by rank

| Rank | Tier | Issue | Title | Epic |
|------|------|-------|-------|------|
| 1 | P1a | #20 | Audit Trail System (SEC-001) | — |
| 2 | P1b | #132 | bug: APScheduler max_instances=1 | #306 Event Bus |
| 3 | P1b | #61 | bug: Orphaned Claude processes | #306 Event Bus |
...

Which issue should I work on? (number or #issue)
```

**Important**: Always sort by `rank` field, not by tier or issue number. The rank reflects the actual board priority order set by the user. Tier (P1a/P1b/P1c) comes from the project field, NOT from issue labels.

**If `$ARGUMENTS` is "recent" — show recently created open issues:**

```bash
# Get open issues created in the last 30 days, sorted by creation date (newest first)
gh issue list --repo abilityai/trinity --state open --limit 30 \
  --json number,title,labels,createdAt \
  --jq 'sort_by(.createdAt) | reverse'
```

Present as:
```
## Recently Created Issues (last 30 days)

| # | Created | Issue | Title |
|---|---------|-------|-------|
| 1 | 2026-04-15 | #380 | feat: New dashboard widget |
| 2 | 2026-04-14 | #379 | bug: Login fails on Safari |
...

Which issue should I work on? (number or #issue)
```

**Note**: If many issues are missing from the project board, run `/groom` first to add them and assign ranks.

**GATE: Wait for user to select an issue.**

Present the selected issue to the user with epic context:

```bash
# Get issue details and epic context from project
gh project item-list 6 --owner abilityai --format json --limit 200 | python3 -c "
import json, sys

ISSUE = $SELECTED_ISSUE_NUMBER
data = json.load(sys.stdin)

# Find this issue and its epic
issue_epic = None
issue_theme = None
issue_tier = None
for item in data['items']:
    c = item.get('content', {})
    if c.get('number') == ISSUE:
        issue_epic = item.get('Epic', '')
        issue_theme = item.get('Theme', '')
        issue_tier = item.get('Tier', '')
        break

# If issue has an epic, count epic progress
if issue_epic:
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
    pct = int(done / total * 100) if total else 0
    print(f'Epic: {issue_epic} ({done}/{total} complete, {pct}%)')
else:
    print('Epic: — (not assigned)')

print(f'Theme: {issue_theme or \"—\"}')
print(f'Tier: {issue_tier or \"—\"}')
"
```

Present as:
```
Selected: #[number] — [title]
Epic: #20 Audit Trail (3/7 complete, 43%)
Theme: Security
Tier: P1a
Labels: [labels]

[First 5 lines of body]

Proceed with this issue?
```

**GATE: Wait for user approval before continuing.**

### Step 2: Validate Issue

Check the issue has enough detail to implement:

1. **Has acceptance criteria?** Look for `## Acceptance Criteria` or checkbox list
2. **Has scope?** Files to change, endpoints, components mentioned
3. **Has clear problem statement?** Summary or Problem section exists

If missing critical detail:
- Warn the user: "Issue #N is missing [acceptance criteria / scope / problem statement]. Proceed anyway or add detail first?"
- **GATE: Wait for user decision.**

### Step 3: Claim Issue & Create Branch

```bash
# Claim via the GitHub Action (auto-assigns you AND adds status-in-progress
# via .github/workflows/claim.yml — single source of truth per
# DEVELOPMENT_WORKFLOW.md §2). Do not edit labels directly.
gh issue comment [NUMBER] --repo abilityai/trinity --body "/claim"

# Create feature branch from dev
git checkout dev
git pull origin dev
git checkout -b feature/[NUMBER]-[slug]
```

Branch naming: `feature/[issue-number]-[2-3-word-slug]`
- Example: `feature/68-live-execution-output`

### Step 4: Plan Review

Run the auto-review pipeline:

```
/autoplan #[NUMBER]
```

This reviews the plan from 3 angles:
- **Strategy**: Is this the right approach? Premises valid? Scope calibrated?
- **Engineering**: Architecture sound? Edge cases? Test coverage?
- **Security**: New attack surface? Auth boundaries? Input validation?

Auto-decides intermediate questions using 6 decision principles. Surfaces taste decisions at the end.

**GATE: Wait for user to approve the autoplan output before implementing.**

### Step 5: Implement

Run the `/implement` skill with the issue number:

```
/implement #[NUMBER]
```

This handles:
- Reading requirements and existing patterns
- Implementing backend, frontend, and agent changes
- Writing initial tests
- Updating `requirements.md` if needed

### Step 6: Pre-Landing Code Review

Run the code review on the implementation:

```
/review
```

This catches structural issues tests miss: SQL safety, race conditions, auth boundary violations, scope drift, enum completeness.

**If critical findings**: Fix them before proceeding. The `/review` skill offers a fix-first flow.

### Step 7: Security Audit (P0/P1 recommended)

For P0 and P1 issues, run a security audit on the actual code changes:

```
/cso --diff
```

Now that code exists on the branch, this scans: secrets archaeology, dependency supply chain, CI/CD pipeline, auth boundaries, and Trinity-specific security patterns.

**For P2/P3 issues**: Ask the user if they want to run `/cso --diff` or skip.

**GATE: If critical findings, present them. User must acknowledge before proceeding.**

### Step 8: Verify Tests

After `/implement` completes, explicitly verify tests were created:

```bash
# Check if new test files were created
git diff --name-only --diff-filter=A | grep -E "^tests/"

# Check if existing test files were modified
git diff --name-only | grep -E "^tests/"
```

**If no tests found:**
1. Identify what should be tested (new endpoints, services, edge cases)
2. Create test file: `tests/test_[feature].py`
3. Write tests following existing patterns (see `tests/` for examples)
4. Run via the project test runner (per DEVELOPMENT_WORKFLOW.md §2):
   ```
   /test-runner [feature]
   ```

**If tests exist, run them to verify they pass:**
```
/test-runner [feature]
```

Fall back to direct `pytest tests/test_[feature].py -v --tb=short` only when targeting a brand-new file the runner's catalog doesn't yet know about.

Fix any failures before proceeding.

### Step 9: Sync Feature Flows

Run the `/sync-feature-flows` skill to update documentation:

```
/sync-feature-flows recent
```

This handles:
- Detecting which feature flows are affected
- Updating existing flow documents
- Creating new flow documents if needed
- Updating the feature flows index

### Step 10: Commit, Push & Create PR

**Stage all changes:**
```bash
git add -A
git status
```

Review the diff to ensure:
- No secrets, credentials, or `.env` files
- No unrelated changes
- No debug artifacts (screenshots, test outputs)

**GATE: Present summary to user before committing.**

```
Ready to commit and create PR:

Issue: #[NUMBER] — [title]
Branch: feature/[NUMBER]-[slug]
Files changed: [count]
Tests: [count] new/modified test files
Docs: [list of updated flow docs]

Proceed?
```

Then commit and create PR:

```
/commit fixes #[NUMBER]
```

After commit succeeds, push and create PR:

```bash
git push -u origin feature/[NUMBER]-[slug]
```

```bash
gh pr create --repo abilityai/trinity \
  --title "[type]: [short description] (#[NUMBER])" \
  --body "$(cat <<'EOF'
## Summary
[2-3 bullet points describing what was implemented]

## Changes
[List of key files changed]

## Test Plan
- [ ] New tests pass: `pytest tests/test_[feature].py -v`
- [ ] Existing tests unaffected
- [ ] Manual verification: [key steps]

Fixes #[NUMBER]

Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

The PR title prefix should match the change type:
- `feat:` — new feature
- `fix:` — bug fix
- `refactor:` — code restructuring
- `docs:` — documentation only

### Step 11: Final Status

Report completion:

```
Sprint complete:

Issue: #[NUMBER] — [title]
Branch: feature/[NUMBER]-[slug]
PR: [PR URL]
Tests: [pass/fail count]
Docs updated: [list]

Next steps:
- Review PR at [URL]
- Run /validate-pr [PR number] for merge readiness check

After squash-merge to dev: .github/workflows/issue-status-on-merge.yml
auto-swaps `status-in-progress` → `status-in-dev` and the issue stays
open until the next release cut (dev → main). Do NOT manually edit
status labels post-merge — let the automation own that transition.
```

## Completion Checklist

- [ ] Issue selected and validated
- [ ] Feature branch created from latest `dev`
- [ ] Plan reviewed and approved (via /autoplan)
- [ ] Implementation complete (via /implement)
- [ ] Code review passed (via /review)
- [ ] Security audit passed (via /cso --diff, P0/P1)
- [ ] Tests exist and pass
- [ ] Feature flows synced (via /sync-feature-flows)
- [ ] No secrets in diff
- [ ] Committed with issue reference
- [ ] PR created with summary

## Error Recovery

| Error | Recovery |
|-------|----------|
| No issues in backlog | Report "Backlog empty" and stop |
| Issue lacks detail | Ask user to add detail or skip to next issue |
| Implementation fails | Show error, ask user to intervene or skip |
| Tests fail | Fix failures before proceeding; if unfixable, note in PR |
| Branch already exists | Ask user: reuse, delete and recreate, or pick different issue |
| Merge conflicts | Rebase on `dev`, resolve conflicts, continue |
| Push fails | Check remote, auth; retry or ask user |

## Self-Improvement

After completing this skill's primary task, consider tactical improvements:

- [ ] **Review execution**: Were there friction points, unclear steps, or inefficiencies?
- [ ] **Identify improvements**: Could error handling, step ordering, or instructions be clearer?
- [ ] **Scope check**: Only tactical/execution changes — NOT changes to core purpose or goals
- [ ] **Apply improvement** (if identified):
  - [ ] Edit this SKILL.md with the specific improvement
  - [ ] Keep changes minimal and focused
- [ ] **Version control** (if in a git repository):
  - [ ] Stage: `git add .claude/skills/sprint/SKILL.md`
  - [ ] Commit: `git commit -m "refactor(sprint): <brief improvement description>"`
