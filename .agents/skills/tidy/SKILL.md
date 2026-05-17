---
name: tidy
description: Audit and clean up repository structure. Identifies outdated docs, misplaced files, orphan configs, and test artifacts. Reports findings first and requires approval before making changes (except safe artifacts).
disable-model-invocation: true
argument-hint: "[scope] [--report-only]"
automation: gated
---

# Repository Tidy Skill

Audit and clean up the repository structure without breaking code.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Requirements | `docs/memory/requirements.md` | ✅ | | Feature status |
| Feature Flows | `docs/memory/feature-flows.md` | ✅ | | Active flows |
| Root Files | Project root | ✅ | | Misplaced files |
| Docs Folder | `docs/` | ✅ | | Outdated docs |
| Config Folder | `config/` | ✅ | | Orphan configs |
| Tests Folder | `tests/` | ✅ | | Test artifacts |
| Archive | `archive/` | | ✅ | Archived files |

## Usage

```
/tidy                    # Full audit of all areas
/tidy docs               # Audit only docs/ folder
/tidy root               # Audit only root folder
/tidy tests              # Audit only tests/ folder
/tidy config             # Audit only config/ folder
/tidy --report-only      # Generate report without any changes
/tidy docs --report-only # Combine scope and report-only
```

## Arguments

- `$ARGUMENTS` - Scope and flags passed by user

## Core Principles

1. **Never break code** - Only touch non-code files (docs, configs, test artifacts)
2. **Report before action** - Always generate audit report first
3. **Archive over delete** - Move outdated files to `archive/` preserving structure
4. **Safe deletes are automatic** - `__pycache__`, `.pyc`, test outputs don't need approval
5. **Everything else needs approval** - Wait for explicit user confirmation

## Procedure

### Phase 1: Safe Cleanup (Automatic)

Delete these without asking (they're regenerable artifacts, all gitignored):
- `__pycache__/` directories
- `*.pyc`, `*.pyo` files
- `.DS_Store`, `Thumbs.db`
- `tests/reports/raw-test-output-*.txt`
- `tests/reports/test-report-*.md`, `test-report-*.html`
- `tests/reports/test-summary-*.json`
- `tests/reports/coverage/` (regenerable)
- `*.log` files in non-essential locations

Report what was cleaned.

### Phase 2: Audit by Scope

Run the appropriate audit based on arguments. See [reference.md](reference.md) for detailed audit procedures.

#### Root Folder Audit
Check for files that don't belong:
- Stray `.md` files (except README.md, CONTRIBUTING.md, LICENSE, CHANGELOG.md, CLAUDE.md)
- Config files that should be in `config/`
- Scripts that should be in `scripts/`
- Temporary or backup files

#### Docs Folder Audit
Cross-reference with project state:
- Read `docs/memory/requirements.md` for feature status
- Read `docs/memory/feature-flows.md` for active flows
- Identify docs for REMOVED features
- Find drafts that were never finalized
- Find specs that are now fully implemented
- Check `docs/demos/` for demos of removed features

#### Tests Folder Audit
- Find orphan test outputs not in `.gitignore`
- Check for stale test fixtures
- Identify tests for removed features

#### Config Folder Audit
- Check `config/agent-templates/` against current templates list
- Find unused template directories
- Identify orphan config files

### Phase 3: Generate Report

Create a structured report:

```markdown
## Tidy Report - [DATE]

### Safe Cleanup Completed
| Type | Count | Space Freed |
|------|-------|-------------|

### Root Folder Issues
| File | Issue | Recommendation |
|------|-------|----------------|

### Documentation Issues
| File | Issue | Recommendation |
|------|-------|----------------|

### Config/Template Issues
| File | Issue | Recommendation |
|------|-------|----------------|

### Test Artifact Issues
| File | Issue | Recommendation |
|------|-------|----------------|

### Recommended Actions
**Archive** (move to archive/ preserving structure):
- [ ] file1 - reason
- [ ] file2 - reason

**Relocate** (move to correct location):
- [ ] file1 -> new/location

**Delete** (truly orphan, no value):
- [ ] file1 - reason
```

### Phase 4: Wait for Approval

If `--report-only` was specified, stop here.

Otherwise, present the report and ask:
- "Which actions should I take? (all / archive only / specific items / none)"

### Phase 5: Execute Approved Changes

For approved items:
1. Create `archive/` directory if needed
2. Move files to `archive/` preserving original path structure
   - Example: `docs/old-feature.md` → `archive/docs/old-feature.md`
3. Relocate misplaced files to correct locations
4. Delete approved orphan files
5. Update any index files that reference moved files (e.g., feature-flows.md)

### Phase 6: Summary

Report what was done:
- Files archived
- Files relocated
- Files deleted
- Index files updated

## Exclusions (Never Touch)

These directories are always excluded from audit:
- `.git/`
- `node_modules/`
- `.venv/`, `venv/`
- `__pycache__/` (auto-cleaned, not audited)
- `.claude/` (except for orphan detection)
- `archive/` (already archived)
- `.playwright-mcp/` (browser-test artifacts, gitignored)
- `backups/` (database backups, gitignored, sensitive)
- `trinity-data/` (SQLite DB bind mount)
- `repositories/` (dynamic test repos, gitignored)
- `*.db` files (databases)
- `.env*` files (secrets)

## Reference Files

- [reference.md](reference.md) - Detailed audit checklists and patterns
- [scripts/audit.py](scripts/audit.py) - Helper script for file analysis

## Completion Checklist

- [ ] Safe artifacts cleaned (pycache, .pyc, .DS_Store)
- [ ] Audit report generated
- [ ] User approved changes (or --report-only)
- [ ] Archive directory structure preserved
- [ ] Index files updated if needed
- [ ] No code files touched
