# Development Workflow

> **For developers and AI assistants** working on this project.
> This guide defines Trinity's Software Development Lifecycle (SDLC) and explains how to use the project's tools, agents, and documentation effectively.

---

## Software Development Lifecycle (SDLC)

Trinity follows a 4-stage lifecycle. Each stage maps 1:1 to an issue's location in the commit graph, tracked via `status-*` labels (the authoritative surface — `gh issue list --label status-in-dev` etc.). The GitHub Project board mirrors these for visual tracking but is optional.

```
 Todo → In Progress → In Dev → Done
```

```
┌─────────────────────────────────────────────────────────────────────┐
│                    TRINITY SDLC                                     │
├──────────┬──────────────────────────────────────────────────────────┤
│          │                                                          │
│ TODO     │  Issue created, triaged with priority + type labels      │
│          │  Acceptance criteria defined before work begins           │
│          │  Label: status-ready (optional) | Board: Todo            │
│          │                                                          │
├──────────┼──────────────────────────────────────────────────────────┤
│          │                                                          │
│ IN       │  /claim → /autoplan → approve → /implement              │
│ PROGRESS │  → /review → /cso --diff → /sync-feature-flows          │
│          │  → open PR to dev, /validate-pr                          │
│          │  Label: status-in-progress | Board: In Progress          │
│          │  Code location: feature branch                           │
│          │                                                          │
├──────────┼──────────────────────────────────────────────────────────┤
│          │                                                          │
│ IN DEV   │  PR squash-merged to dev — feature is shippable          │
│          │  Awaiting next release cut (dev → main)                  │
│          │  Label: status-in-dev | Board: In Dev                    │
│          │  Code location: origin/dev                               │
│          │                                                          │
│          │  Promoted automatically by                               │
│          │  .github/workflows/issue-status-on-merge.yml             │
│          │                                                          │
├──────────┼──────────────────────────────────────────────────────────┤
│          │                                                          │
│ DONE     │  Release PR (dev → main) squash-merged + tagged          │
│          │  Issues auto-closed via `Closes #N` in release body      │
│          │  Label: (none — issue closed) | Board: Done              │
│          │  Code location: origin/main                              │
│          │                                                          │
└──────────┴──────────────────────────────────────────────────────────┘
```

### Prioritization

| Priority | Label | Meaning |
|----------|-------|---------|
| **P0** | `priority-p0` | Blocking/urgent — drop everything |
| **P1** | `priority-p1` | Critical path — current focus |
| **P2** | `priority-p2` | Important — next up |
| **P3** | `priority-p3` | Nice-to-have — when time allows |

Within P1, the **Tier** field on the project board provides sub-prioritization: **P1a** (highest) → **P1b** → **P1c**.

**Rule**: Work P0 first, then P1 by Tier (P1a → P1b → P1c), then by Rank (lowest number first).

### Epics and Themes

The project board uses two additional **single-select** fields for strategic grouping (one value per issue):

| Field | Purpose | Examples |
|-------|---------|----------|
| **Epic** | Groups related issues into a deliverable | `#20 Audit Trail`, `#306 Event Bus` |
| **Theme** | Strategic category | Security, Reliability, Channels, DevEx, Monetization, Infrastructure, UI/UX |

- **Epic**: Links issue to one parent epic. Epics track % complete via child issue status. Pick the *primary* deliverable.
- **Theme**: Categorizes by one strategic area. Helps balance roadmap coverage. Pick the *primary* intent.

**Commands:**
- `/roadmap epics` — epic rollup with progress bars
- `/roadmap themes` — coverage by strategic theme  
- `/roadmap orphans` — issues missing Epic or Theme
- `/groom` — assign Epics/Themes to orphan issues

#### Backlog Grooming

Run `/groom` periodically to keep the backlog healthy. It audits board coverage, detects unranked items, reviews priority ordering, and applies rank updates after approval. Key checks:

- All open issues are on the project board
- All Todo items have a Rank and Tier assigned
- P1a items ranked highest, bugs above features within same tier
- Issues advancing `docs/planning/TARGET_ARCHITECTURE.md` ranked above same-type issues that do not
- Stale or resolved items flagged for closure

### Issue Types

| Label | Purpose |
|-------|---------|
| `type-feature` | New functionality |
| `type-bug` | Bug fix |
| `type-refactor` | Code improvement |
| `type-docs` | Documentation |

### Key Rules

- **All work on feature branches** — direct pushes to `main` and `dev` are blocked (branch protection)
- **Default base branch is `dev`** — PRs target `dev`; `main` only receives merges at release cuts (see §4b)
- **Every PR links to an issue** — use `Fixes #N` in the PR description
- **Claim issues via `/claim`** — comment `/claim` on an issue to auto-assign yourself (or assign manually). Use `/unclaim` to release.
- **No merge without passing `/validate-pr`**

---

## Stage Details

### 1. Todo

Issues are created via GitHub issue templates (bug report or feature request). On creation:

1. Apply **priority** label (P0-P3)
2. Apply **type** label (feature/bug/refactor/docs)
3. Add to **Trinity Roadmap** project board (lands in Todo)
4. Add description with enough context to understand the problem
5. Define acceptance criteria (how do we know it's done?)

An issue is ready to pick up when it has a clear description, acceptance criteria, and no unresolved blockers (if blocked, apply `status-blocked` label).

### 2. In Progress

When picking up work:

1. **Claim the issue** — comment `/claim` on the issue (GitHub Action auto-assigns you and adds `status-in-progress` label), or assign yourself manually
2. Move to **In Progress** on the project board
3. Create a feature branch from `dev`

#### Branch Convention

All work happens on feature branches cut from `dev`. Direct pushes to `main` and `dev` are blocked by branch protection.

**Naming**: `feature/<issue-number>-<short-slug>`
- Example: `feature/68-live-execution-output`

**Merge strategy**: Squash merge via PR into `dev` with `Fixes #N`. `main` receives merges only at release cuts (see §4b).

Then follow the development pipeline:

#### Development Pipeline

The full pipeline for a sprint (each step can also be run standalone):

```
/sprint X → /autoplan → approve → /implement → /review → /cso --diff → /sync-feature-flows → PR
```

| Step | Skill | What it does |
|------|-------|-------------|
| 1. Claim issue | `/claim` (GitHub) | Auto-assign + `status-in-progress` label |
| 2. Plan review | `/autoplan` | Strategy + engineering + security review with auto-decisions |
| 3. Human approval | *(manual)* | Review autoplan output, approve or revise |
| 4. Implement | `/implement` | Code the feature, write tests |
| 5. Code review | `/review` | Pre-landing diff review for structural issues |
| 6. Security audit | `/cso --diff` | Scan actual code changes for vulnerabilities (P0/P1 recommended) |
| 7. Sync docs | `/sync-feature-flows` | Update feature flow documentation |
| 8. Ship | `/commit` + PR | Commit, push, create pull request |

#### Context Loading

Always start by loading context.

```
/read-docs
```

This loads requirements, architecture, and recent git history. For targeted work, read the relevant feature flow directly:

```
@docs/memory/feature-flows/user-login.md
```

See `docs/memory/feature-flows.md` for the complete index.

#### Development

1. **Check requirements**: Does `requirements.md` cover this feature?
2. **Read feature flow**: Understand existing data flow before modifying
3. **Implement**: Follow patterns established in existing code
4. **Local testing**: Run tests and verify locally

```bash
# Health check
curl http://localhost:8000/health

# Run tests
/test-runner              # Run full test suite
/test-runner auth         # Run tests matching "auth"
/test-runner --verbose    # Include detailed output
```

#### Documentation

After tests pass, update documentation:

```
/update-docs
```

| Change Type | Required Docs |
|-------------|---------------|
| Bug fix | Descriptive commit message only |
| Feature / API change | `architecture.md` or `feature-flows/*.md` as needed |
| New capability | `requirements.md` + `feature-flows/*.md` |

### 3. Review

When local development is complete:

1. **Run `/review`** — pre-landing code review for structural issues (SQL safety, race conditions, auth boundaries, scope drift)
2. **Fix critical findings** — `/review` offers a fix-first flow for critical issues
3. **Open a PR** — reference the issue with `Fixes #N`
4. **Run `/validate-pr`** — process and documentation validation
5. **For P0/P1 features** (recommended): deploy to dev server for additional validation

**Code review (`/review`) checks:**

| Category | Check |
|----------|-------|
| **SQL & Data Safety** | Raw queries, missing parameterization, mass assignment |
| **Race Conditions** | Shared state, TOCTOU, Docker container races |
| **Auth Boundaries** | Missing auth, resource ownership, admin access |
| **Credential Exposure** | Secrets in logs, error messages, responses |
| **Scope Drift** | Did the diff match the issue requirements? |
| **Enum Completeness** | New values handled everywhere they're referenced |
| **Test Gaps** | New endpoints/paths without tests |

**Process validation (`/validate-pr`) checks:**

| Category | Check |
|----------|-------|
| **Commit Messages** | Descriptive, with conventional prefix (feat/fix/refactor/docs) |
| **Requirements** | Updated if new feature or scope change |
| **Architecture** | Updated if API/schema/integration changes |
| **Feature Flows** | Created/updated for behavior changes |
| **Security** | No secrets, keys, emails, IPs in diff |
| **Code Quality** | Minimal changes, follows patterns |
| **Traceability** | Links to requirements and issue |

Both produce reports with recommendations: **APPROVE**, **REQUEST CHANGES**, or **NEEDS DISCUSSION**.

If changes are requested, fix and re-run the failing check.

### 3b. Reviewer / Admin Workflow

When a PR lands in your queue:

#### Quick Triage (30 seconds)

1. Check the PR has an issue link (`Fixes #N`)
2. Check priority label — P0/P1 get deeper review
3. Check size — large PRs (50+ files) may need to be split

#### Review Pipeline

Run these based on PR type:

| PR Type | `/review` | `/validate-pr` | `/cso --diff` |
|---------|-----------|-----------------|----------------|
| **Feature (P0/P1)** | Required | Required | Required |
| **Feature (P2/P3)** | Required | Required | Recommended |
| **Bug fix** | Required | Required | Skip unless auth/security related |
| **Refactor** | Required | Required | Skip |
| **Docs only** | Skip | Required | Skip |

**Step 1: Code review**
```
/review
```
Checks structural issues: SQL safety, race conditions, auth boundaries, scope drift, test gaps. Produces a findings report with CRITICAL (block merge) and INFORMATIONAL (review) categories.

**Step 2: Process validation**
```
/validate-pr <number>
```
Checks docs, commit messages, requirements, feature flows, security (no secrets in diff), traceability.

**Step 3: Security audit (P0/P1 or security-sensitive changes)**
```
/cso --diff
```
Runs a scoped security audit on the branch changes only. Checks secrets, dependencies, auth boundaries, injection vectors, Trinity-specific patterns.

#### Decision

| Outcome | When |
|---------|------|
| **APPROVE** | All checks pass, no critical findings |
| **REQUEST CHANGES** | Critical findings in `/review` or `/validate-pr` — list what to fix |
| **NEEDS DISCUSSION** | Scope drift detected, architecture concerns, or taste decisions |

#### After Approval

1. **Squash merge** the PR
2. Verify the issue **auto-closes** via `Fixes #N`
3. Move to **Done** on the project board
4. Remove `status-in-progress` label (if not auto-removed)

#### Red Flags to Watch For

- Secrets, credentials, or real emails in the diff
- New endpoints without auth checks
- Changes to `docker-compose.yml` or `Dockerfile` without justification
- Large unrelated changes bundled with the feature
- Missing tests for new behavior
- `requirements.md` not updated for new features

### 4. In Dev (merged to dev, awaiting release)

When the feature PR squash-merges to `dev`:

1. **Automation fires** (`.github/workflows/issue-status-on-merge.yml`)
   - Parses `Fixes #N` / `Closes #N` from the PR body + title
   - Adds `status-in-dev` to each referenced issue
   - Removes `status-in-progress`
2. Issue remains **open** — it ships when the next release cuts `dev` → `main`
3. Board column (if used): **In Dev**

Querying what's shipping in the next release:
```bash
gh issue list --repo abilityai/trinity --state open --label status-in-dev
```

### 4a. Done (released)

The issue transitions to Done when the release PR squash-merges to `main` (see §4b):

1. The release PR body includes `Closes #N #M ...` for every `status-in-dev` issue — GitHub auto-closes them on merge
2. `status-in-dev` label remains on the closed issue (cosmetic, harmless — `--state open` queries ignore it)
3. Board column: **Done**

### 4b. Release cut (`dev` → `main`)

`main` represents the current release. Day-to-day development flows into `dev`; `main` only receives merges at release time.

Run `/release` to automate this flow (pre-release checklist → version bump → release notes → PR → tag). The manual steps it orchestrates:

1. Verify `dev` is green — tests pass, no open P0/P1 regressions
2. Open a PR from `dev` → `main`
3. Review the cumulative diff since the last release
4. Squash-merge — the squash commit message is the release notes
5. Tag `main` (e.g., `cli-v0.4.0` for a CLI release — triggers `publish-cli.yml`)

Automations that fire on push to `main` (keep `main` releasable):
- `publish-cli.yml` — publishes CLI to PyPI + updates Homebrew formula on tag
- `sync-docs-to-vertex.yml` — syncs `docs/` to the public Vertex AI search index

**Branch protection (setup required — see repo Settings → Branches):**

| Branch | Require PR | Require status checks | Restrict pushes |
|--------|------------|----------------------|-----------------|
| `main` | ✅ | ✅ | Maintainers only |
| `dev`  | ✅ | ✅ | All contributors via PR |

Both branches must block direct pushes. Feature branches push freely.

### 5. Release mechanics (CLI only)

The release cut in §4b updates `main`. If the release includes CLI changes (`src/cli/`), publish a new CLI version by tagging `main`:

```bash
git tag cli-v0.3.0
git push --tags
```

The `publish-cli.yml` workflow automatically:
1. Extracts the version from the tag name
2. Injects it into `pyproject.toml` at build time
3. Publishes to [PyPI](https://pypi.org/project/trinity-cli/)
4. Updates the [Homebrew formula](https://github.com/abilityai/homebrew-tap) (version + sha256)

**No manual version edits.** The source has a placeholder `0.0.0`; the real version comes from the tag. Runtime reads it via `importlib.metadata`.

**Requires**: `HOMEBREW_TAP_TOKEN` repo secret (fine-grained PAT with Contents read/write on `abilityai/homebrew-tap`).

---

## GitHub Project Board

**Trinity Roadmap** (GitHub Project #6) is the single view of all work.

| Column | Meaning |
|--------|---------|
| **Todo** | Backlog + Ready issues |
| **In Progress** | Actively being worked on |
| **Done** | Merged and shipped |

### Label ↔ Board Sync

Labels are the authoritative surface (`gh issue list --label ...`). The project board mirrors them:

| Stage | Label | Board Column | Code location |
|-------|-------|--------------|---------------|
| Todo | *(none or `status-ready`)* | Todo | — |
| In Progress | `status-in-progress` | In Progress | feature branch |
| Blocked | `status-blocked` | In Progress | feature branch |
| In Dev | `status-in-dev` | In Dev | `origin/dev` |
| Done | *(issue closed)* | Done | `origin/main` |

Transitions:
- **Todo → In Progress**: `/claim` adds `status-in-progress` (via `.github/workflows/claim.yml`)
- **In Progress → In Dev**: PR merge to `dev` adds `status-in-dev`, removes `status-in-progress` (via `.github/workflows/issue-status-on-merge.yml`)
- **In Dev → Done**: Release PR (dev → main) includes `Closes #N` for each `status-in-dev` issue; GitHub auto-closes on merge

---

## Environments

| Environment | URL | Purpose |
|-------------|-----|---------|
| **Local** | `http://localhost` | Development and primary testing |
| **Dev Server** | *(configured separately)* | Optional pre-merge validation for P0/P1 features |

---

## Sub-Agents Reference

| Agent / Skill | Use When |
|---------------|----------|
| `/test-runner` | After development to validate changes (run full test suite) |
| `feature-flow-analyzer` | After modifying feature behavior |
| `security-analyzer` | Before commits touching auth, credentials, or APIs |

Agents are invoked automatically by Claude Code when appropriate. The `/test-runner` skill can be invoked directly with optional arguments (e.g., `/test-runner auth --verbose`).

---

## Slash Commands Reference

| Command | Purpose | SDLC Stage |
|---------|---------|------------|
| `/read-docs` | Load project context | In Progress |
| `/cso [--diff\|--comprehensive]` | Security audit (CSO mode) | In Progress |
| `/autoplan [issue-number]` | Auto-review pipeline (strategy + eng + security) | In Progress |
| `/implement <issue-number>` | End-to-end feature implementation | In Progress |
| `/review` | Pre-landing code review (structural issues) | In Progress |
| `/update-docs` | Update documentation | In Progress |
| `/feature-flow-analysis <feature>` | Document feature flow | In Progress |
| `/sync-feature-flows` | Sync feature flow docs with code changes | In Progress |
| `/security-check` | Validate no secrets in staged files | In Progress |
| `/add-testing` | Add tests for a feature | In Progress |
| `/test-runner [filter] [--verbose]` | Run API test suite with report | In Progress / Review |
| `/validate-pr <number>` | Validate PR against methodology | Review |
| `/validate-architecture` | Validate codebase against 16 architectural invariants | Weekly / Review |
| `/validate-schema` | Check schema.py vs migrations.py vs architecture.md for drift | Weekly |
| `/validate-config` | Check env vars across docker-compose, .env.example, and code | Weekly |
| `/groom` | Backlog grooming — audit board, rank issues, review priorities | Todo |
| `/sprint [issue-number]` | Full dev cycle (orchestrates all above) | All |
| `/release [version-tag]` | Cut a release — pre-release checklist, version bump, notes, `dev` → `main` merge, tag push | Release (§4b) |

---

## Memory Files

The `docs/memory/` directory contains persistent project state:

```
docs/memory/
├── requirements.md      ← SINGLE SOURCE OF TRUTH for features
├── architecture.md      ← Current system design (~1000 lines max)
├── feature-flows.md     ← Index of all feature flow documents
└── feature-flows/       ← Individual feature documentation

docs/planning/
└── TARGET_ARCHITECTURE.md  ← Target system design — optimal destination;
                               consult when evaluating tradeoffs and prioritizing work
```

### How They Connect

```
requirements.md        ──defines──►  What features exist
       │
       ▼
GitHub Issues          ──prioritizes──►  What to work on next
       │                                 (guided by TARGET_ARCHITECTURE.md)
       ▼
feature-flows/*        ──documents──►  How features work
       │
       ▼
git log                ──records──►  What changed and when
       │
       ▼
architecture.md        ──maintains──►  Current system state
TARGET_ARCHITECTURE.md ──defines──►   Target system state (destination)
```

---

## Development Skills

Skills in `.claude/skills/` define HOW to approach specific tasks:

| Skill | Principle | When |
|-------|-----------|------|
| `verification` | No "done" claims without evidence | Before saying "done" |
| `systematic-debugging` | Find root cause BEFORE fixing | When fixing bugs |
| `tdd` | Failing test first, then minimal code | When writing new code |
| `code-review` | Verify feedback technically first | When responding to PR comments |

---

## Quick Start Checklist

**For every development session (or just run `/sprint`):**

- [ ] `/claim` the issue (or assign yourself manually)
- [ ] Create feature branch: `feature/<issue-number>-<slug>`
- [ ] Load context (`/read-docs` or read relevant feature flows)
- [ ] `/autoplan` — plan review (strategy + eng + security)
- [ ] Review and approve the plan
- [ ] `/implement` — build the feature
- [ ] `/review` — pre-landing code review
- [ ] `/cso --diff` — security audit of changes (recommended for P0/P1)
- [ ] `/test-runner` — run API test suite
- [ ] `/sync-feature-flows` — update documentation
- [ ] Open PR with `Fixes #N`, run `/validate-pr`
- [ ] Squash merge when approved

**For PR reviews (reviewer/admin):**

- [ ] Quick triage: issue link, priority label, PR size
- [ ] `/review` — code quality (structural issues, auth, races)
- [ ] `/validate-pr <number>` — docs and process
- [ ] `/cso --diff` — security (P0/P1 or security-sensitive PRs)
- [ ] Verify all critical findings resolved
- [ ] Squash merge, verify issue auto-closes

**Weekly maintenance:**

- [ ] `/validate-architecture` — check codebase against architectural invariants
- [ ] `/validate-schema` — check schema.py vs migrations.py vs architecture.md for drift
- [ ] `/validate-config` — check env vars across docker-compose, .env.example, and code
- [ ] `/generate-user-docs` — regenerate user documentation from code
- [ ] `/groom` — audit backlog, rank issues, review priorities (manual, requires human review)

> **Automated schedules**: The validation tasks and `/generate-user-docs` run automatically on the `trinity` dev agent (Mon-Thu 9:00 UTC). `/groom` remains manual as it requires human judgment for prioritization decisions.
