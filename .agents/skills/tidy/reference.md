# Tidy Skill Reference

Detailed audit procedures and patterns for the `/tidy` skill.

## Root Folder Audit Checklist

### Files That Belong in Root
- `README.md` - Project overview
- `CONTRIBUTING.md` - Contribution guidelines
- `CODE_OF_CONDUCT.md` - Community standards
- `SECURITY.md` - Security disclosure policy
- `LICENSE` - License file
- `CHANGELOG.md` - Version history (if not in docs/)
- `VERSION` - Current release tag
- `CLAUDE.md` - Claude Code instructions
- `CLAUDE.local.md` - Local Claude instructions (gitignored)
- `CLAUDE.local.md.example` - Template for CLAUDE.local.md
- `.gitignore` - Git ignore rules
- `.gitattributes` - Git attributes
- `docker-compose.yml` - Docker composition
- `docker-compose.*.yml` - Docker variants (prod, override, gitea, ...)
- `.env.example` - Environment template
- `deploy.config.example` - Deployment template
- `install.sh` - Installer script
- `template.yaml` - Trinity agent template manifest
- `package.json` - If Node.js project root
- `pyproject.toml` - If Python project root
- `Makefile` - If using make

### Files That Should Be Relocated
| Pattern | Destination |
|---------|-------------|
| `*.sh` scripts | `scripts/` |
| `*.py` scripts (not packages) | `scripts/` |
| `config-*.yaml` | `config/` |
| `*.template` | `config/` or relevant dir |
| `test-*.md` | `docs/testing/` or `tests/` |
| `draft-*.md` | `docs/drafts/` |
| `notes-*.md` | `docs/notes/` or archive |
| `old-*.md` | `archive/` |
| `backup-*` | Delete or archive |
| `*.bak` | Delete |
| `*.tmp` | Delete |
| `*.log` | Delete |

## Docs Folder Audit Checklist

### Structure Expectations

The `docs/` tree has grown organically. Current top-level subdirs (not exhaustive):

```
docs/
├── memory/               # Project memory — requirements.md, architecture.md,
│                         # feature-flows.md (index), feature-flows/ (70+ flow files),
│                         # roadmap-archive.md, project_index.json
├── feature-flows/        # (legacy location; prefer docs/memory/feature-flows/)
├── demos/                # Demo documentation
├── demo-screenshots/
├── drafts/               # Work in progress
├── testing/              # Testing guides
├── development/          # Dev workflow docs
├── user-docs/            # End-user documentation
├── user-scenarios/       # User journey docs
├── onboarding/
├── planning/ , plans/    # Planning artifacts
├── research/ , reports/  # Research output and reports
├── requirements/         # Per-feature requirement files
├── concepts/ , diagrams/ , assets/
├── bugs/                 # Bug write-ups
├── security/ , security-reports/
├── screenshots/          # Documentation screenshots (gitignored root *.png, allowed here)
├── archive/              # Older archived docs (alongside top-level /archive/)
└── *.md                  # Top-level topic docs
```

When auditing, walk the tree and cross-reference only — don't assume any particular subdir layout.

### Cross-Reference Checks

1. **Requirements Cross-Check**
   - Read `docs/memory/requirements.md`
   - Find features with status `REMOVED` or `DEPRECATED`
   - Search for docs mentioning those features
   - Flag for archive

2. **Feature Flow Cross-Check**
   - Read `docs/memory/feature-flows.md` (the index)
   - Walk `docs/memory/feature-flows/*.md` (the actual flow files — 70+ today)
   - Check every link in the index resolves to an existing file
   - Find orphans on either side:
     - Files in `feature-flows/` that the index never links to
     - Links in the index pointing to files that don't exist
   - Also check the legacy `docs/feature-flows/` location for strays that should move into `docs/memory/feature-flows/`

3. **Demo Cross-Check**
   - List `docs/demos/*/`
   - Check if demo agents/features still exist
   - Flag obsolete demos

### Patterns Indicating Outdated Docs

| Pattern | Likely Issue |
|---------|--------------|
| `v1-*`, `v2-*` prefix | Old version, check if superseded |
| `old-*` prefix | Explicitly old |
| `draft-*` prefix | Never finalized? |
| `WIP` or `TODO` in title | Incomplete |
| References removed endpoints | Outdated |
| References removed components | Outdated |
| Last modified > 6 months + not in index | Potentially orphan |

## Config Folder Audit Checklist

### Template Validation
```bash
# List all template directories
ls -la config/agent-templates/

# Cross-reference with active templates in config.py or templates API
# Flag any not referenced
```

### Config File Checks
- `.yaml` files should be referenced by code
- `.json` files should be referenced by code
- `.template` files should have corresponding usage

## Tests Folder Audit Checklist

### Safe to Delete (Automatic)
- `tests/reports/raw-test-output-*.txt`
- `tests/reports/*.tmp`
- `tests/__pycache__/`
- `tests/**/__pycache__/`
- `tests/.pytest_cache/`
- `tests/coverage/` (regenerable)
- `htmlcov/` (regenerable)

### Potentially Orphan
- Test files for removed features
- Test fixtures for removed features
- Old snapshot files

## Archive Structure

When archiving, preserve the original path:

```
archive/
├── docs/
│   ├── old-feature-spec.md      # was docs/old-feature-spec.md
│   └── demos/
│       └── removed-demo/        # was docs/demos/removed-demo/
├── config/
│   └── agent-templates/
│       └── deprecated-template/ # was config/agent-templates/deprecated-template/
└── _archive_manifest.md         # Log of what was archived and why
```

### Archive Manifest Format

Maintain `archive/_archive_manifest.md`:

```markdown
# Archive Manifest

Files archived during repository cleanup.

## 2026-01-28

| Original Path | Reason | Archived By |
|---------------|--------|-------------|
| docs/old-spec.md | Feature removed in v2.0 | /tidy |
| config/unused-template/ | No longer referenced | /tidy |
```

## Detection Queries

### Find Orphan Markdown Files
```bash
# Files not referenced anywhere
for f in docs/**/*.md; do
  basename=$(basename "$f")
  if ! grep -r "$basename" --include="*.md" --include="*.py" --include="*.ts" . > /dev/null 2>&1; then
    echo "Potentially orphan: $f"
  fi
done
```

### Find Large Files That Might Be Artifacts
```bash
find . -type f -size +1M \
  ! -path "./.git/*" \
  ! -path "./node_modules/*" \
  ! -path "./.venv/*" \
  ! -name "*.db"
```

### Find Old Test Outputs
```bash
find tests/ -name "*.txt" -o -name "*.log" -o -name "*.html" | \
  xargs ls -la --sort=time
```

### Find Duplicate File Names
```bash
find . -type f -name "*.md" ! -path "./.git/*" | \
  xargs -I {} basename {} | sort | uniq -d
```

## Decision Tree

```
Is it in .git, node_modules, .venv?
  YES → Skip entirely

Is it __pycache__, *.pyc, *.pyo, .DS_Store?
  YES → Safe delete (automatic)

Is it a test output (raw-test-output-*, *.log in tests/)?
  YES → Safe delete (automatic)

Is it code (.py, .ts, .js, .vue)?
  YES → Do NOT touch, skip

Is it docs for a removed feature?
  YES → Archive with approval

Is it a draft > 3 months old?
  YES → Archive with approval OR flag for review

Is it in wrong location?
  YES → Relocate with approval

Is it truly orphan (not referenced anywhere)?
  YES → Flag for review, archive or delete with approval
```

## Reporting Format

### Issue Severity

| Severity | Meaning | Action |
|----------|---------|--------|
| HIGH | Clearly wrong location or outdated | Recommend archive/relocate |
| MEDIUM | Potentially orphan, needs review | Flag for user decision |
| LOW | Minor organization improvement | Suggest but don't push |

### Confidence Levels

| Confidence | Meaning |
|------------|---------|
| HIGH | Cross-reference confirms (e.g., feature marked REMOVED) |
| MEDIUM | Pattern match suggests (e.g., old-* prefix) |
| LOW | Only age/location heuristic |
