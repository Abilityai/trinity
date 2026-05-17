---
name: release
description: Cut a Trinity release by merging dev → main with a pre-release checklist, version bump, release notes, and tag push. Triggers publish-cli and docs-sync workflows.
allowed-tools: [Bash, Read, Edit, Grep, Glob, AskUserQuestion]
user-invocable: true
argument-hint: "[version-tag]"
automation: gated
---

# /release — Cut a Trinity Release

Merge `dev` → `main` and tag the release. Triggers CLI publish (PyPI + Homebrew) and docs sync (Vertex AI).

## Purpose

Trinity follows the dev/main convention (see `docs/DEVELOPMENT_WORKFLOW.md` §4b): day-to-day work merges into `dev`, and `main` receives merges only at release time. This playbook automates the release cut:

1. Run pre-release checklist (CI, P0/P1 regressions, docs sync, secrets scan)
2. Propose version bump from commit scope since last tag
3. Draft release notes from `$LAST_TAG..dev` commits
4. Open and squash-merge the dev → main PR
5. Tag `main` — triggers `publish-cli.yml` and `sync-docs-to-vertex.yml`
6. Verify publish workflows fire
7. Resync `dev` with `main`

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Git refs | `.git/` | ✅ | ✅ | `dev`, `main`, tags |
| GitHub PRs | `abilityai/trinity` | ✅ | ✅ | Release PR |
| GitHub Issues | `abilityai/trinity` | ✅ | | P0/P1 regression check |
| GitHub Actions | `abilityai/trinity` | ✅ | | CI status + publish workflows |
| Source Code | `src/` | ✅ | | CLI scope detection |
| Docs | `docs/memory/` | ✅ | | Doc-sync sanity |

## Arguments

- No argument: propose next semver based on last tag + commit scope
- Version tag: use this tag verbatim (e.g. `cli-v0.4.0`, `v1.2.0`)

## Prerequisites

- Current branch is `dev`
- Working tree clean
- `gh` CLI authenticated against `abilityai/trinity`
- Remote `origin` reachable

## Process

### Step 1: Pre-Flight

```bash
CURRENT=$(git branch --show-current)
if [ "$CURRENT" != "dev" ]; then
  echo "Must be on 'dev' to cut a release. Currently on '$CURRENT'."
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree dirty. Commit or stash before releasing."
  exit 1
fi

git fetch origin dev main --tags --quiet

LOCAL_DEV=$(git rev-parse dev)
REMOTE_DEV=$(git rev-parse origin/dev)
if [ "$LOCAL_DEV" != "$REMOTE_DEV" ]; then
  echo "Local dev ($LOCAL_DEV) differs from origin/dev ($REMOTE_DEV). Sync first."
  exit 1
fi
```

### Step 2: Pre-Release Checklist

Run each check. Classify results as **BLOCKER** (must resolve) or **ADVISORY** (review before proceeding).

**2.1 CI green on `dev`** (BLOCKER)
```bash
gh run list --repo abilityai/trinity --branch dev --limit 5 \
  --json status,conclusion,name,headSha
```
All runs at the tip of `dev` must be `conclusion: success`.

**2.2 No open P0 bugs** (BLOCKER)
```bash
gh issue list --repo abilityai/trinity --state open \
  --label priority-p0,type-bug --json number,title
```
Any open → blocker unless user explicitly overrides.

**2.3 P1 bugs review** (ADVISORY)
```bash
gh issue list --repo abilityai/trinity --state open \
  --label priority-p1,type-bug --json number,title
```
Present list; user decides ship vs. hold.

**2.4 Cumulative diff** (context)
```bash
LAST_TAG=$(git describe --tags --abbrev=0 origin/main 2>/dev/null || echo "")
RANGE="${LAST_TAG:+$LAST_TAG..}dev"
git log --oneline "$RANGE" | wc -l       # commit count
git diff --stat "$RANGE" | tail -1        # files/insertions/deletions
git shortlog -sn "$RANGE"                 # contributors
```

**2.5 Secrets scan** (BLOCKER)
```bash
# Delegate to /security-check for added rigor; inline grep catches common cases
git diff "$RANGE" -- ':(exclude)*.example' ':(exclude)*.md' ':(exclude)docs/**' \
  | grep -iE '^\+.*((api[_-]?key|password|secret|token)[[:space:]]*=|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,})' \
  | head -30
```
Any hit → STOP and investigate. Never force a release with suspected credential leakage.

**2.6 Docs sync check** (ADVISORY)
```bash
SRC_CHANGED=$(git log "$RANGE" --name-only --format= | grep -c '^src/' || echo 0)
DOC_CHANGED=$(git log "$RANGE" --name-only --format= | grep -c '^docs/memory/' || echo 0)
```
If `SRC_CHANGED > 0` and `DOC_CHANGED == 0`, suggest running `/sync-feature-flows` before release.

**2.7 CLI scope** (context)
```bash
CLI_CHANGED=$(git diff --name-only "$RANGE" | grep -c '^src/cli/' || echo 0)
```
If `CLI_CHANGED > 0`, the release tag should use the `cli-v*` prefix (triggers `publish-cli.yml`).

**2.8 Breaking changes** (ADVISORY)
```bash
git log "$RANGE" --format='%s' | grep -iE '^(feat!|fix!|breaking|chore!):' | head -10
```
Any hits must be called out in the release notes.

### Step 3: Checklist Report

Present formatted results:

```
## Pre-Release Checklist: dev → main

Last release: [LAST_TAG] ([date])
Commits since: [N] | Files changed: [N] | Contributors: [list]

### Blockers
- [ ✓ / ✗ ] CI green on dev
- [ ✓ / ✗ ] No open P0 bugs  ([N])
- [ ✓ / ✗ ] No secrets in diff

### Advisory
- [ ✓ / ℹ ] P1 bugs reviewed  ([N] open — list)
- [ ✓ / ℹ ] Docs synced with code changes
- [ ✓ / ℹ ] Breaking changes called out  ([N] found)
- [ ℹ ]     CLI files changed  ([N])
```

[APPROVAL GATE] — Present checklist.

- All blockers pass → proceed
- Any blocker fails → offer: (a) abort and fix, (b) override with written justification for the release notes, (c) abort

### Step 4: Determine Version

Parse last tag and propose next version based on commit scope:

- `feat!:` / `breaking` / `chore!:` commits present → **major** bump
- `feat:` commits present → **minor** bump
- only `fix:` / `refactor:` / `docs:` / `chore:` → **patch** bump

```
Last tag: cli-v0.3.2
Proposed: cli-v0.4.0  (minor — [N] feat commits)
Alternatives:
  patch: cli-v0.3.3
  major: cli-v1.0.0
```

[APPROVAL GATE] — Accept proposal, pick alternative, or enter custom tag.

Validate tag format: `cli-v<semver>` if CLI scope changed, otherwise `v<semver>` (or project convention).

### Step 5: Draft Release Notes

**Primary source: issues labeled `status-in-dev`.**

Every PR that lands on `dev` promotes its linked issues to `status-in-dev` (via `.github/workflows/issue-status-on-merge.yml`). That label set is the authoritative "what's shipping in this release" list. The commit range is used as a sanity check, not the primary source.

```bash
# Authoritative shipping list — open issues labeled status-in-dev
gh issue list --repo abilityai/trinity --state open \
  --label status-in-dev --limit 100 \
  --json number,title,labels,url

# Sanity check: issue references in the commit range
git log "$RANGE" --format='%s%n%b' \
  | grep -oiE '(fix(es|ed)?|close[sd]?|resolve[sd]?) #[0-9]+' \
  | grep -oE '#[0-9]+' | sort -u
```

Reconcile the two lists. Flag and discuss:
- Issues in `status-in-dev` but NOT referenced in commits → label may be stale (leftover from a reverted change)
- Issues referenced in commits but NOT in `status-in-dev` → the automation missed them (retroactively add the label, or include manually)

Group release notes by issue type (read from issue labels `type-feature` / `type-bug` / `type-refactor` / `type-docs`):

```markdown
# [VERSION]

## Features
- #N Title (short description)

## Fixes
- #N Title

## Refactors
- #N Title

## Documentation
- #N Title

## Breaking Changes
[if any from 2.8, list here]

**Full diff**: [LAST_TAG]...[VERSION]
**Contributors**: [names]
```

Include standalone commits (without a linked issue) under an "Other changes" heading if material.

[APPROVAL GATE] — Present drafted notes. User edits or approves.

### Step 6: Open the Release PR

The PR body MUST include a `Closes #N #M ...` line listing every issue in the release so GitHub auto-closes them when the release squash-merges to `main`. Build this from the `status-in-dev` list gathered in Step 5.

```bash
# Build the "Closes" line from status-in-dev issues
CLOSES_LINE=$(gh issue list --repo abilityai/trinity --state open \
  --label status-in-dev --limit 100 --json number \
  --jq '[.[].number] | map("#\(.)") | join(" ")')

PR_BODY=$(cat <<EOF
Release [VERSION] — cumulative changes since [LAST_TAG].

[release notes]

---

Closes ${CLOSES_LINE}

Pre-release checklist passed [date]. Ready for squash-merge.

- Tag after merge: \`[VERSION]\`
- Triggers: \`publish-cli.yml\` + \`sync-docs-to-vertex.yml\`
EOF
)

gh pr create --repo abilityai/trinity \
  --base main --head dev \
  --title "Release: [VERSION]" \
  --body "$PR_BODY"
```

Capture the PR URL. The `Closes` line ensures every `status-in-dev` issue auto-closes when the release lands on `main`.

### Step 7: Wait for CI

```bash
gh pr checks --watch
```

If any check fails, STOP. Investigate and fix on `dev`, then re-run from Step 1.

### Step 8: Merge + Tag

[APPROVAL GATE] — Final confirmation. Show the PR URL, CI results, and chosen tag. User says "merge".

```bash
gh pr merge [PR_URL] --squash
```

Wait for the merge to land on `main`, then tag:

```bash
git checkout main
git pull origin main
git tag [VERSION]
git push origin [VERSION]
```

### Step 9: Verify Publishing

Confirm the downstream workflows fire:

```bash
gh run list --repo abilityai/trinity --workflow=publish-cli.yml --limit 3
gh run list --repo abilityai/trinity --workflow=sync-docs-to-vertex.yml --limit 3
```

Poll until both the CLI publish (if tag is `cli-v*`) and docs sync reach `conclusion: success`. Report run URLs.

### Step 10: Resync `dev` with `main`

Squash-merging creates a new commit on `main` with a different SHA than `dev`'s tip. Bring `dev` back in sync via a merge-commit (avoids force-push disruption for in-flight feature branches):

```bash
git checkout dev
git pull origin dev
git merge main --no-ff -m "chore: sync dev with release [VERSION]"
git push origin dev
```

If the merge is unexpectedly non-trivial (conflicts, divergent tree), STOP — something else is going on and needs human review.

### Step 11: Final Report

```
Release [VERSION] shipped:
- PR:            [URL]
- Tag:           [VERSION]  pushed to origin
- publish-cli:   [success/failed — URL]
- docs sync:     [success/failed — URL]
- dev resynced:  [yes/no]

Next: announce (consider /announce) and close any released-in-this-version issues.
```

## Completion Checklist

- [ ] On `dev`, clean tree, up-to-date with origin
- [ ] Pre-release checklist ran; all blockers resolved
- [ ] Version tag determined and validated
- [ ] Release notes drafted and approved
- [ ] Release PR opened against `main`
- [ ] CI green on release PR
- [ ] Squash-merged to `main`
- [ ] Tag pushed
- [ ] `publish-cli.yml` verified (if CLI release)
- [ ] `sync-docs-to-vertex.yml` verified
- [ ] `dev` resynced with `main`

## Error Recovery

| Error | Recovery |
|-------|----------|
| Not on `dev` | Checkout `dev` and re-run |
| Dirty working tree | Commit or stash, then re-run |
| CI failing on `dev` | Fix on `dev` first; release is blocked |
| Local `dev` ahead/behind origin | `git pull` or push; reconcile before releasing |
| Open P0 bug | Fix first, or document explicit override in release notes |
| Secrets in diff | **STOP** — rotate credentials, rewrite history if needed, do not release |
| CI fails on release PR | Abort merge, fix on `dev`, reopen |
| `gh pr merge` fails | Check merge conflicts; if dev diverged from main (shouldn't happen), rebase `dev` onto `main` |
| Tag push rejected | Tag may already exist — bump version or delete the stale tag with team alignment |
| Publish workflow fails | Re-run via Actions UI or `gh run rerun`; tag doesn't need to be repushed |
| `dev` resync conflicts | STOP — unexpected; investigate manually before next release |

## Important Rules

- **Never force-push `main`.** Branch protection should block this; respect it.
- **Never skip the secrets scan.** A leaked credential in the diff means rotate and halt, not ship-and-fix.
- **Tag format matters.** `cli-v*` triggers `publish-cli.yml`; other prefixes do not. Choose deliberately.
- **Release notes are the squash commit message.** Draft them as if they'll be the primary record — because they will be.
- **Don't amend tags after pushing.** Delete + re-push is disruptive; prefer a new patch version.

## Self-Improvement

After completing this skill's primary task, consider tactical improvements:

- [ ] **Review execution**: Were there friction points, unclear steps, or inefficiencies?
- [ ] **Identify improvements**: Could error handling, step ordering, or instructions be clearer?
- [ ] **Scope check**: Only tactical/execution changes — NOT changes to core purpose or goals
- [ ] **Apply improvement** (if identified):
  - [ ] Edit this SKILL.md with the specific improvement
  - [ ] Keep changes minimal and focused
- [ ] **Version control** (if in a git repository):
  - [ ] Stage: `git add .claude/skills/release/SKILL.md`
  - [ ] Commit: `git commit -m "refactor(release): <brief improvement description>"`
