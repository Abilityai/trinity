---
name: commit
description: Commit, push, and link to GitHub Issues
allowed-tools: [Bash]
user-invocable: true
automation: manual
---

# Commit

Commit changes and link to relevant GitHub Issues.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Git Status | Working directory | ✅ | | Staged/unstaged changes |
| Git Log | `.git/` | ✅ | | Recent commit style |
| Git Diff | Working directory | ✅ | | Changes to commit |
| GitHub Issues | `abilityai/trinity` | ✅ | | Issue to reference |
| GitHub Issues | `abilityai/trinity` | | ✅ | Reference issue in commit |
| Git Commit | `.git/` | | ✅ | New commit created |
| Git Remote | Remote repository | | ✅ | Push to origin |

## Usage

```
/commit [message]
/commit closes #17
/commit fixes #23 - added validation
```

## Process

### Step 1: Check Status

```bash
git status
git diff --stat
```

### Step 2: Stage Changes

Stage specific files (avoid `.env`, credentials):
```bash
git add <files>
```

### Step 3: Commit with Issue Reference

Include issue reference in commit message when applicable:
- `Closes #N` - closes issue when merged
- `Fixes #N` - closes issue (for bugs)
- `Refs #N` - references without closing

```bash
git commit -m "$(cat <<'EOF'
<type>: <description>

<optional body>

Closes #N

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

### Step 4: Push

```bash
git push
```

## Commit Types

| Type | Use |
|------|-----|
| `feat` | New feature |
| `fix` | Bug fix |
| `refactor` | Code change (no new feature/fix) |
| `docs` | Documentation |
| `chore` | Maintenance |

## Completion Checklist

- [ ] Changes staged (specific files, no secrets)
- [ ] Commit message follows type convention
- [ ] Issue reference included (Closes/Fixes/Refs #N)
- [ ] Co-Authored-By line present
- [ ] Push successful
