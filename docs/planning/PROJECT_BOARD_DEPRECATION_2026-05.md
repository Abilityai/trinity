# Project Board Deprecation — Issues-Only SDLC

> **Status**: Planned. Migration to be executed in a future session.
> **Decision date**: 2026-05-28
> **Owner**: Eugene

---

## Decision

Deprecate **GitHub Project #6 ("Trinity Roadmap")**. Move all prioritization metadata to labels + GitHub native sub-issues. Skills, docs, and SDLC operate on issues only from this point forward.

## Why

1. **The board's metadata is mostly unmaintained.** As of 2026-05-28: all 88 open P1 items are untiered; all 5 P0/P1 items have no Epic assigned. The discipline to maintain Rank/Tier/Epic on the board does not exist today.
2. **Skills produce hollow output.** `/roadmap` shows Epic columns that are universally `—`. The apparatus exists but the data doesn't.
3. **Bulk editing isn't the actual bottleneck.** Grooming friction is high regardless of UI; the board's table view isn't getting used.
4. **Sub-issues are now native to GitHub** (shipped 2024) — they replace the Epic field structurally and bring built-in progress rollup.
5. **One source of truth** — labels + open/closed state — eliminates a recurring class of drift between board Status and reality.

## What we give up

- Numeric `Rank` field — already unused, no real loss.
- Bulk Epic/Theme editing in board table UI — replaced by `gh issue edit` loops.
- Saved cross-cutting views in the board UI — replaced by ad-hoc `gh issue list --label …` queries.

## What we gain

- One source of truth (labels + open/closed).
- Six skills shrink ~30–40% in aggregate (no more GraphQL field mutations, no `gh project item-list`).
- New issues auto-participate (no "add to project" step that gets forgotten).
- Sub-issues give better epic progress tracking than the current `/roadmap epics` math.
- Docs stop describing machinery that isn't being maintained.

---

## Final label schema

| Family | Labels | Notes |
|---|---|---|
| Priority | `priority-p0`, `priority-p1`, `priority-p2`, `priority-p3` | Already exist — no change |
| Type | `type-bug`, `type-feature`, `type-refactor`, `type-docs`, `type-epic` | Add `type-epic` for parent issues |
| Status | `status-in-progress`, `status-in-dev`, `status-blocked` | Already exist; `status-ready` optional |
| Theme | `theme-reliability`, `theme-ui-ux`, `theme-security`, `theme-channels`, `theme-devex`, `theme-monetization`, `theme-infrastructure` | New family; **informational only** (filtering, not ordering) |

**Explicitly dropped**:
- `tier-*` labels (Tier ladder is fake precision — was only used at P1 and only ~0% adopted)
- `rank-*` labels (numeric ranking dropped entirely)
- `epic-N` labels (replaced by sub-issues — see below)

## Epics → GitHub native sub-issues

- An epic is an **issue** with the `type-epic` label.
- Child issues are linked via GitHub's built-in **sub-issue** feature (Issue UI → "Add sub-issue").
- Progress rollup is rendered automatically by GitHub (`X / Y completed` on parent).
- Single-parent enforcement is structural — no convention-policing needed.

This replaces the board's single-select Epic field one-for-one, with better UX and no label proliferation.

---

## Ordering rule (final)

Replaces WORKFLOW.md §Prioritization (lines 53–66, 88):

1. **P0** — all of them, today.
2. **P1** — `type-bug` before `type-feature`; within type, lowest issue number first (oldest).
3. **P2 / P3** — picked when no P1 work fits the current session.
4. **Theme focus** (per `CLAUDE.md` "Current Product Focus" — currently Reliability primary, UI/UX secondary) is a **filter**, not a tiebreaker. When two P1s look equivalent, prefer the focus theme. Don't formalize further.

No Tier ladder. No numeric Rank. The ordering you can defend is the ordering you write down.

---

## Skill rewrites

| Skill | Change |
|---|---|
| `.claude/skills/roadmap/SKILL.md` | Pure `gh issue list` with label filters. Epic view uses GitHub sub-issue API. Drop Project Constants block, all `gh project item-list 6` calls (9 occurrences), all GraphQL field mutations. Drop the `arch` view's reliance on board fields. |
| `.claude/skills/groom/SKILL.md` | Label-only edits via `gh issue edit --add-label / --remove-label`. Drop all GraphQL field-update mutations. Audit checks become label-presence queries. Skill drops to roughly half its current size. |
| `.claude/skills/create-issue/SKILL.md` | Drop the "add to project + set Epic/Theme via GraphQL" block (~60 lines). Add optional `--parent #N` argument for sub-issue linking via GraphQL `addSubIssue` mutation. |
| `.claude/skills/metrics/SKILL.md` | Strip Complexity/Tier reads from the board; if those metrics are needed, move them to labels (`complexity-low/med/high`) or drop them. |
| `.claude/skills/read-docs/SKILL.md` | Remove "Ranked P1 pipeline from Trinity Roadmap project" section. Replace with `gh issue list --label priority-p1 --state open`. |
| `.claude/skills/sprint/SKILL.md` | Strip `gh project item-list 6` calls; use `gh issue list` for issue selection. |
| `/claim`, `/commit`, `/validate-pr`, `/review`, etc. | Already label-only — no change. |

---

## Documentation changes

### `.claude/DEVELOPMENT_WORKFLOW.md`
- **Delete** §"GitHub Project Board" (line 799 and the section that follows).
- **Strip** Tier and Rank references from §Prioritization (lines 53–66).
- **Strip** Tier/Rank from §Backlog Grooming (line 88) — replace with label-presence checks.
- **Remove** "Trinity Roadmap project board" mentions in §Stage Details (line 123 and elsewhere).
- **Update** the State Model table (line ~806) — already label-correct, just remove board references in surrounding prose.

### `CLAUDE.md`
- **Lines 82, 111, 158**: replace "Trinity Roadmap GitHub Project board" / "Trinity Roadmap board" with "GitHub Issues".
- **Keep** the "Current Product Focus" section — recast as: "Set the `theme-reliability` filter when picking work; `theme-ui-ux` as secondary."
- **Update** Rules of Engagement §3 — remove board reference.

### `docs/GITHUB_ISSUES_MIGRATION.md`
- **Append** a closing note: Project #6 archived on `<date>`. Historical Rank/Tier data preserved in archive only.

### Six skill SKILL.md files
- Strip Project Constants blocks (PROJECT_ID, PROJECT_NUM, EPIC_FIELD_ID, THEME_FIELD_ID).
- Strip State Dependencies rows referencing "GitHub Project #6".
- Strip example code containing `gh project item-list 6`, `gh project field-list 6`, GraphQL `updateProjectV2ItemFieldValue` mutations.

### Other doc touch-ups
- `docs/planning/WORKFLOW_PRIORITIES_2026-02.md` line 197 — update reference if still load-bearing (it may be historical, leave if so).
- `docs/onboarding/README.md` line 199 — already links to Issues, no change.

---

## Migration steps (one PR, one afternoon)

### Step 1 — Bulk-add `theme-*` labels from current board data

```bash
# Read current Theme field values from Project #6
gh project item-list 6 --owner abilityai --format json --limit 500 \
  | python3 -c "
import json, subprocess, sys

THEME_MAP = {
    'Reliability': 'theme-reliability',
    'UI/UX': 'theme-ui-ux',
    'Security': 'theme-security',
    'Channels': 'theme-channels',
    'DevEx': 'theme-devex',
    'Monetization': 'theme-monetization',
    'Infrastructure': 'theme-infrastructure',
}

data = json.load(sys.stdin)
for item in data['items']:
    c = item.get('content', {})
    n = c.get('number')
    theme = item.get('Theme', '')
    if not n or not theme:
        continue
    label = THEME_MAP.get(theme)
    if not label:
        print(f'SKIP #{n}: unknown theme {theme!r}', file=sys.stderr)
        continue
    subprocess.run(['gh', 'issue', 'edit', str(n),
                    '--repo', 'abilityai/trinity',
                    '--add-label', label], check=False)
"
```

Create the labels first via `gh label create` if they don't exist. Verify with a sample of 5 issues before the bulk run.

### Step 2 — Convert epics to sub-issue parents

For each issue currently used as an Epic value on the board:

1. Add `type-epic` label to the epic issue.
2. For each child issue (everything currently tagged with this Epic on the board), open the child issue and add it as a sub-issue of the parent via the GitHub Web UI **or**:
   ```bash
   gh api graphql -f query='mutation {
     addSubIssue(input: {issueId: "PARENT_NODE_ID", subIssueId: "CHILD_NODE_ID"})
       { issue { id } }
   }'
   ```
3. Verify the parent's progress rollup renders.

### Step 3 — Land skill + doc rewrites in one PR

- Branch: `feature/<issue-number>-deprecate-project-board`
- Rewrites: 6 skills + DEVELOPMENT_WORKFLOW.md + CLAUDE.md + GITHUB_ISSUES_MIGRATION.md
- Open a tracking issue first; use `Fixes #N` in PR.

### Step 4 — Archive Project #6

GitHub Settings → Project → **Archive** (not delete). Preserves historical Rank/Tier data.

### Step 5 — Announce

- Slack / Discord: "Trinity Roadmap project board archived. Use labels + sub-issues. See `docs/planning/PROJECT_BOARD_DEPRECATION_2026-05.md`."

---

## Verification checklist

After migration, the following must hold:

- [ ] `gh project item-list 6 --owner abilityai` returns archived-project response (or board is read-only).
- [ ] `/roadmap` runs without invoking `gh project item-list`.
- [ ] `/groom` runs label-only — `grep -rn "PVT_kwDOB8r7us4BRY6\|project item-list 6" .claude/skills/` returns no matches.
- [ ] All `theme-*` labels exist and at least one issue carries each.
- [ ] At least one epic issue (`type-epic`) has sub-issues attached and the rollup renders.
- [ ] WORKFLOW.md and CLAUDE.md contain no surviving references to "Trinity Roadmap project board" or "Project #6".
- [ ] A grooming pass with the new tooling is faster than the previous board-based flow (subjective — confirm with the operator).

---

## Open questions to resolve before migration

1. **Are there labels currently in use that overlap with the proposed scheme?** Check `gh label list --repo abilityai/trinity` — particularly for existing `theme-*`, `tier-*`, or `epic-*` labels that may need cleanup or coexistence handling.
2. **Should we keep a `complexity-*` label family** to replace the board's Complexity field used by `/metrics`, or drop the metric entirely?
3. **`/sprint` and `/read-docs` reference the board for issue selection** — confirm the replacement `gh issue list` ordering matches the operator's mental model before the rewrite ships.

---

## Files to read at the start of the migration session

- This document
- `.claude/DEVELOPMENT_WORKFLOW.md` (sections to be rewritten)
- `CLAUDE.md` (sections to be rewritten)
- `.claude/skills/roadmap/SKILL.md`
- `.claude/skills/groom/SKILL.md`
- `.claude/skills/create-issue/SKILL.md`
- `.claude/skills/metrics/SKILL.md`
- `.claude/skills/read-docs/SKILL.md`
- `.claude/skills/sprint/SKILL.md`
