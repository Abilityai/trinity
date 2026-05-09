---
name: code-health
description: Autonomous weekly technical debt scan for src/backend/. Combines git churn × cyclomatic complexity into a hotspot score, tracks trends against a JSON baseline, and creates/updates a GitHub issue with the top 3 findings. Emits a Trinity event on completion.
automation: autonomous
schedule: "0 9 * * 1"
allowed-tools: [Read, Grep, Glob, Bash, Agent]
user-invocable: true
---

# Code Health

## Purpose

Weekly monitoring of technical debt in `src/backend/`. Static metrics alone are noisy — a
large file that never changes isn't urgent. The core signal is **hotspot score = git churn ×
cyclomatic complexity**: files that change frequently AND are hard to understand are where
refactoring effort pays off most.

Each run:
1. Computes hotspot scores, size violations, coupling issues, and stale smell inventory
2. Compares against `docs/metrics/code-health-baseline.json` to show ↑↓→ trends
3. Writes a new baseline
4. Creates or updates a single "Code Health" GitHub issue with top-3 findings per category
5. Emits `code.health.report.complete` Trinity event

The companion `/refactor-audit` skill does deep manual dives; this skill does lightweight
scheduled monitoring.

## State Dependencies

| Source | Location | Read | Write |
|--------|----------|------|-------|
| Previous baseline | `docs/metrics/code-health-baseline.json` | ✅ | ✅ |
| Backend source | `src/backend/` | ✅ | — |
| Git history | local repo | ✅ | — |
| GitHub Issues | `abilityai/trinity` | ✅ | ✅ |

## Prerequisites

- Python 3 + pip (for `radon`)
- Git history available (non-shallow clone)
- `gh` CLI authenticated

## Process

### Step 1: Load Previous Baseline

```bash
cat docs/metrics/code-health-baseline.json 2>/dev/null || echo '{}'
```

Parse the JSON. If the file is missing or malformed, treat all trends as `NEW` this run.
Key fields used for comparison: `hotspot_scores` (dict file→score), `size_violations_count`,
`stale_todo_count`, `high_coupling_count`, `circular_import_count`.

Record the baseline in memory for Step 8.

### Step 2: Measure File Sizes

```bash
find src/backend -name "*.py" | xargs wc -l 2>/dev/null \
  | grep -v " total$" | sort -rn | head -40
```

Thresholds:
- **> 800 lines** — critical (likely needs service extraction)
- **500–800 lines** — high (consider splitting)
- **300–500 lines** — informational

Record all files > 300 lines. Save output to `/tmp/ch_sizes.txt`.

### Step 3: Measure Cyclomatic Complexity (radon)

Install radon if absent, then run:

```bash
pip install radon --quiet 2>/dev/null || pip install --user radon --quiet 2>/dev/null

radon cc src/backend/ -s -n C --json 2>/dev/null > /tmp/ch_radon.json
```

Flag `--min C` means grade C (complexity 6+) and worse. Grades:
- **A (1–5)**: fine
- **C (6–10)**: informational
- **D/E (11+)**: high — refactor candidate
- **F (16+)**: critical

For each file, sum all function complexity scores into a **file-level score**. Record top 20.
If radon fails, fall back to a proxy: count `if/elif/for/while/except/and/or` keywords per file.

```bash
# Fallback complexity proxy
for f in $(find src/backend -name "*.py" | sort); do
  score=$(grep -c "^\s*\(if \|elif \|for \|while \|except\|and \|or \)" "$f" 2>/dev/null || echo 0)
  echo "$score $f"
done | sort -rn | head -20
```

Save file→score mapping to `/tmp/ch_complexity.txt` (one `score path` per line).

### Step 4: Measure Git Churn (last 90 days)

```bash
git log --since="90 days ago" --name-only --pretty=format: -- src/backend/ \
  | grep "\.py$" \
  | sort \
  | uniq -c \
  | sort -rn \
  | head -30 > /tmp/ch_churn.txt

cat /tmp/ch_churn.txt
```

Files not in the output have churn = 0.

### Step 5: Compute Hotspot Scores

Join the complexity and churn data:

```bash
python3 - <<'EOF'
import sys, json

# Load complexity (score path)
complexity = {}
with open('/tmp/ch_complexity.txt') as f:
    for line in f:
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            complexity[parts[1]] = int(parts[0])

# Load churn (count path)
churn = {}
with open('/tmp/ch_churn.txt') as f:
    for line in f:
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            churn[parts[1]] = int(parts[0])

# Compute hotspot score
all_files = set(complexity) | set(churn)
hotspots = []
for path in all_files:
    c = complexity.get(path, 1)
    ch = churn.get(path, 0)
    score = c * ch
    if score > 0:
        hotspots.append({'file': path, 'complexity': c, 'churn': ch, 'score': score})

hotspots.sort(key=lambda x: -x['score'])
for h in hotspots[:15]:
    print(f"{h['score']:6d}  churn={h['churn']:3d}  cc={h['complexity']:3d}  {h['file']}")
EOF
```

The **top 3 hotspots** are the primary output for the GitHub issue.

### Step 6: Measure Coupling (import fan-out + circular deps)

**Fan-out per file:**

```bash
for f in $(find src/backend -name "*.py" | sort); do
  count=$(grep -cE "^(import |from )" "$f" 2>/dev/null || echo 0)
  echo "$count $f"
done | sort -rn | head -20 > /tmp/ch_coupling.txt

cat /tmp/ch_coupling.txt
```

Flag files with **> 20 import lines** as high fan-out.

**Circular import detection** (mutual dependency pairs):

```bash
python3 - <<'EOF'
import re
from pathlib import Path

imports = {}
base = Path('src/backend')
for f in base.rglob('*.py'):
    rel = str(f.relative_to(base)).replace('/', '.').replace('.py', '')
    text = f.read_text(errors='ignore')
    found = re.findall(r'^(?:from|import)\s+([\w.]+)', text, re.MULTILINE)
    imports[rel] = found

cycles = []
for mod, imps in imports.items():
    for imp in imps:
        if imp in imports and mod in imports.get(imp, []):
            pair = tuple(sorted([mod, imp]))
            if pair not in cycles:
                cycles.append(pair)
                print(f"CYCLE: {pair[0]}  ↔  {pair[1]}")

if not cycles:
    print("No circular imports detected.")
EOF
```

### Step 7: Stale Smell Inventory

Count TODO/FIXME markers in `src/backend/` and estimate age via blame:

```bash
# Count total markers
grep -rn "TODO\|FIXME\|HACK\|XXX" src/backend/ --include="*.py" \
  | grep -v "^Binary" | wc -l

# Find the oldest markers (by git blame date)
git log --all --format="%ad %H" --date=short \
  | head -1000 \
  | while read date sha; do
    git show --name-only --format="" "$sha" -- 'src/backend/*.py' 2>/dev/null \
      | grep "\.py$" \
      | while read f; do
        git show "$sha:$f" 2>/dev/null \
          | grep -n "TODO\|FIXME\|HACK" \
          | head -2 \
          | sed "s|^|$date $f:|"
      done
  done 2>/dev/null | head -20
```

Simpler alternative that works reliably:

```bash
grep -rn "TODO\|FIXME\|HACK\|XXX" src/backend/ --include="*.py" \
  | grep -v "^Binary" \
  | head -20
```

Count total. Files with the most markers are smell hotspots.

### Step 8: Compute Trends vs Baseline

Compare each metric to the baseline loaded in Step 1:

| Metric | Trend Rule |
|--------|-----------|
| Top hotspot score | ↑ if > baseline×1.1, ↓ if < baseline×0.9, → otherwise |
| Size violations count | ↑/↓/→ by same 10% threshold |
| Stale TODO count | ↑/↓/→ |
| High fan-out files count | ↑/↓/→ |
| Circular import count | ↑/↓/→ (any new circular import = ↑) |

If no baseline exists, all show `NEW`.

### Step 9: Write New Baseline

Construct and write `docs/metrics/code-health-baseline.json`:

```json
{
  "last_run": "YYYY-MM-DD",
  "commit_sha": "<short sha>",
  "hotspot_top3": [
    {"file": "...", "churn": 45, "complexity": 87, "score": 3915},
    {"file": "...", "churn": 30, "complexity": 60, "score": 1800},
    {"file": "...", "churn": 25, "complexity": 55, "score": 1375}
  ],
  "hotspot_scores": {
    "src/backend/routers/agents.py": 3915,
    "...": 0
  },
  "size_violations_count": 8,
  "stale_todo_count": 23,
  "high_coupling_count": 5,
  "circular_import_count": 2
}
```

Then commit it so the trend history is preserved in git:

```bash
COMMIT_SHA=$(git rev-parse --short HEAD)
git add docs/metrics/code-health-baseline.json
git commit -m "chore(metrics): code-health baseline $(date +%Y-%m-%d) @ $COMMIT_SHA" \
  --no-verify 2>/dev/null || true
```

If committing fails (protected branch, dirty tree), write the file and log a warning — the
baseline will still be read correctly on the next run.

### Step 10: Generate Report

Assemble the markdown report in memory (printed to conversation and embedded in GitHub issue):

```
## Code Health Report — <date> (<commit sha>)

### Executive Summary

| Metric | Value | Trend |
|--------|-------|-------|
| Top hotspot score | N | ↑/↓/→/NEW |
| Files > 400 lines | N | ↑/↓/→/NEW |
| Stale TODOs | N | ↑/↓/→/NEW |
| High fan-out files (>20 imports) | N | ↑/↓/→/NEW |
| Circular imports | N | ↑/↓/→/NEW |

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | src/backend/... | 45 | 87 | 3915 | 642 |
| 2 | ... | | | | |
| 3 | ... | | | | |

**Interpretation**: These files are both frequently changed and cognitively expensive.
Refactoring them reduces ongoing maintenance cost the most.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| ... | 900 | critical (>800) | Extract service layer |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| ... | 28 | High fan-out — consider splitting responsibilities |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: N
- Top smell-dense files: [list top 3 by count]

### Suggested Refactorings (Top 3 Hotspots)

For each hotspot: name what to extract, which layer it should move to,
estimated line-count reduction, and which architectural invariant it supports.
```

### Step 11: Create or Update GitHub Issue

Deduplicate — exactly one open Code Health issue at a time:

```bash
COMMIT_SHA=$(git rev-parse --short HEAD)

EXISTING=$(gh issue list \
  --repo abilityai/trinity \
  --label "automated" \
  --state open \
  --search '"Code Health" in:title' \
  --json number \
  --jq '.[0].number')
```

**Path A — existing issue found (`$EXISTING` non-empty):** add run as a comment, then stop.

```bash
gh issue comment "$EXISTING" \
  --repo abilityai/trinity \
  --body "Re-run on \`$COMMIT_SHA\` ($(date -u +%Y-%m-%d)):

<paste full report from Step 10>

---
*Generated by scheduled /code-health run*"
```

**Path B — no existing issue:** create one.

```bash
gh issue create \
  --repo abilityai/trinity \
  --title "Code Health: top hotspot score N, M size violations ($(date -u +%Y-%m-%d))" \
  --body "<full report from Step 10>

### Tracking Notes
- Future runs will comment on this issue rather than open a new one.
- Close this issue once all top-3 hotspots drop below a score of 500 on \`main\`.

---
*Generated by scheduled /code-health run*" \
  --label "type-refactor,priority-p2,automated"
```

Record the issue number for the event payload in Step 12.

### Step 12: Emit Trinity Event

Emit `code.health.report.complete` so subscribed agents (e.g., a chief-of-staff agent
forwarding to Slack) can react:

Use the `emit_event` MCP tool with:
```json
{
  "event_type": "code.health.report.complete",
  "payload": {
    "date": "<YYYY-MM-DD>",
    "commit_sha": "<sha>",
    "top_hotspot_file": "<path>",
    "top_hotspot_score": <N>,
    "size_violations_count": <N>,
    "stale_todo_count": <N>,
    "trend_hotspot": "↑/↓/→/NEW",
    "github_issue": <number>
  }
}
```

## Completion Checklist

- [ ] Previous baseline loaded (or initialized as NEW)
- [ ] File sizes measured — violations identified
- [ ] Cyclomatic complexity computed via radon (or fallback proxy)
- [ ] Git churn computed (90 days)
- [ ] Hotspot scores computed and ranked
- [ ] Coupling fan-out and circular imports checked
- [ ] Stale smell inventory counted
- [ ] Trends computed vs baseline
- [ ] Baseline JSON written and committed
- [ ] Report generated
- [ ] GitHub issue created or updated (exactly one)
- [ ] Trinity event emitted

## Error Recovery

| Failure | Action |
|---------|--------|
| `radon` install fails | Use keyword-count complexity proxy; note in report header |
| Git history unavailable | Skip churn; record all hotspot scores as complexity-only; note "churn: N/A" |
| Baseline JSON malformed | Treat as missing; show `NEW` for all trends; overwrite with fresh baseline |
| GitHub issue creation fails | Write report to `docs/metrics/code-health-<date>.md` as fallback; log error |
| `emit_event` fails | Log warning; do not block — report delivery is the primary output |
| Python parse error on a file | Skip that file; count it in a "parse errors" field in the baseline |

## Self-Improvement

After each run, consider:

- [ ] Did the hotspot ranking surface genuinely actionable files, or noise?
- [ ] Are thresholds (400/500/800 lines, 90-day window, 20 imports) well-calibrated for this codebase?
- [ ] Did radon fail on any files? Should the fallback be smarter?
- [ ] Should a new category be added (e.g., test-coverage gaps, duplication clusters)?
- [ ] Were suggested refactorings specific enough to act on?
- [ ] If improvements identified, edit this SKILL.md and commit:
  ```bash
  git add .claude/skills/code-health/SKILL.md
  git commit -m "refactor(code-health): <improvement>"
  ```
