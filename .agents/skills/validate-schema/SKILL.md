---
name: validate-schema
description: Validate database schema consistency — DDL in schema.py vs migrations.py vs architecture.md. Flags drift between the three sources of truth.
allowed-tools: [Read, Grep, Glob, Bash]
user-invocable: true
---

# Validate Schema

## Purpose

Check that the three places defining database schema are consistent: `db/schema.py` (DDL), `db/migrations.py` (ALTER/CREATE for upgrades), and `docs/memory/architecture.md` (documentation). Report drift with specific mismatches. No changes are made — read-only analysis.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Schema DDL | `src/backend/db/schema.py` | R | | Authoritative table definitions |
| Migrations | `src/backend/db/migrations.py` | R | | Schema evolution history |
| Architecture docs | `docs/memory/architecture.md` | R | | Documented schema |

## Process

### Step 1: Extract Tables from schema.py

Read `src/backend/db/schema.py` and extract:
- All `CREATE TABLE` statements
- Table names
- Column names, types, constraints per table
- All `CREATE INDEX` statements

Build a map: `table_name -> { columns: [{name, type, constraints}], indexes: [name] }`

### Step 2: Extract Tables from architecture.md

Read the "Database Schema" section of `docs/memory/architecture.md` and extract:
- All documented tables
- Column names, types, constraints per table
- Documented indexes

Build the same map structure.

### Step 3: Extract Migration Additions

Read `src/backend/db/migrations.py` and extract:
- All `ALTER TABLE ... ADD COLUMN` statements
- All `CREATE TABLE IF NOT EXISTS` statements
- All `CREATE INDEX IF NOT EXISTS` statements
- Map each to the migration version that introduced it

### Step 4: Cross-Reference — schema.py vs architecture.md

For each table in schema.py:
1. Check table exists in architecture.md docs
2. Compare column lists — flag missing or extra columns in either direction
3. Compare column types — flag type mismatches
4. Compare indexes — flag missing indexes in docs

For each table in architecture.md:
1. Check table exists in schema.py — flag documented-but-nonexistent tables

### Step 5: Cross-Reference — migrations.py vs schema.py

For each migration that adds a column or table:
1. Verify the column/table exists in schema.py's DDL
2. Flag migrations that add something not reflected in schema.py (migration applied but DDL not updated)
3. Flag columns in schema.py that have no corresponding migration and aren't in the original CREATE TABLE (column added to DDL but no migration to apply it to existing databases)

### Step 6: Check for Ad-Hoc Schema

Grep `src/backend/` (excluding `db/schema.py` and `db/migrations.py`) for:
- `CREATE TABLE` — tables created outside the schema system
- `ALTER TABLE` — schema changes outside the migration system
- `ADD COLUMN` — ad-hoc column additions

Flag any findings as violations of Architectural Invariant #3.

### Step 7: Generate Report

Output a summary:

```
## Schema Validation Report

### Tables Summary
| Table | schema.py | architecture.md | Migrations | Status |
|-------|-----------|-----------------|------------|--------|
| users | Y | Y | - | PASS/FAIL |
...

### Drift Findings

#### schema.py vs architecture.md
- **Table X**: Column `foo` in schema.py but missing from docs
- **Table Y**: Type mismatch — schema says `INTEGER`, docs say `TEXT`

#### migrations.py vs schema.py
- **Migration v12**: Adds `bar` column to `users`, but schema.py DDL missing it
- **schema.py**: Column `baz` in `agents` has no migration (added directly to DDL?)

#### Ad-Hoc Schema
- **File**: path/to/file.py:line — `CREATE TABLE` outside schema system

**Result: X issues found (Y critical, Z informational)**
```

### Step 8: Create or Update Issue if Critical

If any critical (P0-P1) drift was found, create or update a GitHub issue.

**P0-P1 criteria** (critical — cause runtime errors or data loss):
- Tables in schema.py with no migration (new deployments fail)
- Migrations adding columns not in schema.py (code/DB mismatch)
- Ad-hoc `CREATE TABLE` or `ALTER TABLE` outside schema system
- Type mismatches between schema.py and actual usage

**Check**: Count critical issues from the report.

**If critical issues > 0**, find or create a tracking issue:

**Dedupe guard — check for an existing open Schema Validation issue before creating:**

```bash
EXISTING=$(gh issue list --repo abilityai/trinity \
  --label "automated" --state open \
  --search "\"Schema Validation\" in:title" \
  --json number --jq '.[0].number')
```

**Branch on `$EXISTING`. The two paths are mutually exclusive — execute exactly one.**

**Path A — `$EXISTING` is non-empty (open issue found): COMMENT, then STOP.**

```bash
gh issue comment "$EXISTING" --repo abilityai/trinity --body "Re-run on $(date -u +%Y-%m-%d): [N] critical schema drift issues still present.

### Updated Critical Findings (P0-P1)

[List each critical finding with table name, issue type, and specific mismatch]

### Recommended Actions

1. [Prioritized fix for each — typically add migration or update schema.py]

---
*Generated by scheduled /validate-schema run*"
```

After commenting, **DO NOT** execute Path B. The skill workflow ends here for this run.

**Path B — `$EXISTING` is empty (no open issue found): CREATE a new issue.**

Only run this block when Path A did not run.

```bash
gh issue create \
  --repo abilityai/trinity \
  --title "Schema Validation: [N] critical drift issues found ($(date -u +%Y-%m-%d))" \
  --body "## Automated Schema Validation Report

**Date**: $(date -u +%Y-%m-%d)
**Result**: [N] critical schema drift issues require attention

### Critical Findings (P0-P1)

[List each critical finding with table name, issue type, and specific mismatch]

### Recommended Actions

1. [Prioritized fix for each — typically add migration or update schema.py]

### Tracking Notes

- Future runs will comment on this issue rather than open a new one.
- Close this issue once all critical schema drift issues are resolved.

---
*Generated by scheduled /validate-schema run*" \
  --label "type-bug,priority-p1,automated"
```

**Concurrency caveat**: best-effort, not atomic. Two simultaneous runners could both create issues; the next run will comment on the first one found. Close any duplicate manually.

**If no critical issues** (only informational like doc drift), skip issue creation — report only.

## Outputs

- Markdown report printed to conversation
- GitHub issue created if critical schema drift found (labeled `automated`, `priority-p1`)
- No files modified
