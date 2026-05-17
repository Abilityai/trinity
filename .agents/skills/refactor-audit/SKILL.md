---
name: refactor-audit
description: Audit codebase for refactoring candidates. Identifies complexity issues, large files/functions, code duplication, and AI-unfriendly patterns. Outputs report to docs/reports/.
disable-model-invocation: true
argument-hint: "[scope] [--quick]"
automation: manual
---

# Refactor Audit

Analyze code to identify refactoring candidates with AI-specific thresholds.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Source Code | `src/` | ✅ | | Files to analyze |
| Radon Output | (runtime) | ✅ | | Complexity metrics |
| Vulture Output | (runtime) | ✅ | | Dead code detection |
| Audit Report | `docs/reports/refactor-audit-{date}.md` | | ✅ | Generated report |

## Usage

```
/refactor-audit                    # Full src/ scan
/refactor-audit backend            # Only src/backend/
/refactor-audit frontend           # Only src/frontend/
/refactor-audit src/backend/routers/agents.py  # Single file
/refactor-audit --quick            # Top 10 issues only
```

## Arguments

- `$ARGUMENTS` - Scope (backend/frontend/path) and flags

## Procedure

### Phase 1: Parse Arguments

Determine scope from `$ARGUMENTS`:
- Empty or "all" → `src/`
- "backend" → `src/backend/`
- "frontend" → `src/frontend/`
- "mcp" or "mcp-server" → `src/mcp-server/`
- Path (contains `/` or `.py`/`.vue`/`.ts`) → use as-is
- "--quick" flag → limit to top 10 issues

### Phase 2: Install Dependencies (if needed)

Check if `radon` and `vulture` are installed. The script works without them but provides richer analysis with them:

```bash
# Option 1: pipx (recommended for macOS)
pipx install radon
pipx install vulture

# Option 2: In a venv
pip install radon vulture

# Option 3: User install
pip install --user radon vulture
```

**Note**: On macOS with Homebrew Python, use `pipx` or a venv. The script gracefully degrades if tools are missing.

### Phase 3: Run Analysis

Run the analysis script:

```bash
python .claude/skills/refactor-audit/scripts/analyze.py [scope] [--quick]
```

The script outputs JSON with findings. If the script doesn't exist or fails, fall back to manual analysis (see Phase 3b).

### Phase 3b: Manual Analysis Fallback

If the script isn't available, analyze manually:

#### For Python files:
```bash
# Cyclomatic complexity (show C grade or worse)
radon cc -s -a --min C [scope]

# Find large files (>500 lines)
find [scope] -name "*.py" -exec wc -l {} \; | awk '$1 > 500 {print}'

# Find long functions (requires reading files)
# Look for def/async def and count lines to next def or class

# Dead code
vulture [scope] --min-confidence 80
```

#### For Vue/TypeScript files:
```bash
# Find large files
find [scope] -name "*.vue" -o -name "*.ts" | xargs wc -l | awk '$1 > 400 {print}'

# Check for deep nesting (manual inspection needed)
```

### Phase 4: Categorize Findings

Group issues by severity using these thresholds:

**🚨 Critical (P0)** - Must fix:
- Files >1000 lines (can't fit in AI context)
- Functions >200 lines
- Cyclomatic complexity >30
- Security issues (if detected)

**🔴 High (P1)** - Strongly recommended:
- Files 800-1000 lines
- Functions 100-200 lines
- Cyclomatic complexity 20-30
- Significant duplication (>10 similar lines)

**⚠️ Medium (P2)** - Recommended:
- Files 500-800 lines
- Functions 50-100 lines
- Cyclomatic complexity 15-20
- Parameters >7
- Nesting depth >4

**📝 Low (P3)** - Nice to have:
- Files 300-500 lines
- Functions 30-50 lines
- Cyclomatic complexity 10-15
- Parameters >5
- Minor duplication (6-10 lines)

### Phase 5: Generate Report

Create report at `docs/reports/refactor-audit-YYYY-MM-DD.md`:

```markdown
# Refactor Audit Report

**Generated**: YYYY-MM-DD HH:MM
**Scope**: [scope analyzed]
**Tool**: /refactor-audit

## Summary

| Severity | Count | Description |
|----------|-------|-------------|
| 🚨 Critical | X | Must fix - blocks AI maintenance |
| 🔴 High | X | Strongly recommended |
| ⚠️ Medium | X | Recommended |
| 📝 Low | X | Nice to have |

**Total issues**: X
**AI-Refactorable**: X/X (Y%) - Issues that AI can safely fix with tests

## Critical Issues

### [File Path]
| Issue | Metric | Line | Recommendation |
|-------|--------|------|----------------|
| Function too long | 187 lines | 234 | Extract into smaller functions |

## High Priority Issues
...

## Medium Priority Issues
...

## Low Priority Issues
...

## Hotspots (Files with Multiple Issues)

Files appearing multiple times - prioritize these:

| File | Issues | Total Severity Score |
|------|--------|---------------------|
| routers/agents.py | 5 | 12 |

## Recommendations

### Quick Wins (Low Risk, High Impact)
1. ...

### Requires Tests First
1. ...

### Architectural Changes
1. ...

## Next Steps

1. Run `/refactor-audit --quick` to verify fixes
2. Add tests for critical functions before refactoring
3. Use small, incremental PRs for each change
```

### Phase 6: Report Location

Save the report and confirm:

```
Report saved to: docs/reports/refactor-audit-YYYY-MM-DD.md

Found X issues:
- 🚨 Critical: X
- 🔴 High: X
- ⚠️ Medium: X
- 📝 Low: X

Top 3 hotspots:
1. [file] - X issues
2. [file] - X issues
3. [file] - X issues
```

## Thresholds Reference

See [reference.md](reference.md) for detailed threshold values and best practices.

## Trinity-Specific Notes

- Backend uses FastAPI/Python - check `src/backend/routers/` and `src/backend/services/`
- Frontend uses Vue.js 3 - check `src/frontend/src/views/` and `src/frontend/src/components/`
- MCP Server uses TypeScript - check `src/mcp-server/src/`
- Agent server has its own codebase at `docker/base-image/agent_server/`

## Completion Checklist

- [ ] Scope correctly parsed from arguments
- [ ] Analysis tools run (radon, vulture or manual fallback)
- [ ] Findings categorized by severity (P0-P3)
- [ ] Hotspots identified (files with multiple issues)
- [ ] Report saved to `docs/reports/`
- [ ] Summary output provided
