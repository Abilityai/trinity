---
name: autoplan
description: Auto-review pipeline — runs strategy, engineering, and security reviews sequentially with auto-decisions using 6 decision principles. Surfaces taste decisions at a final approval gate. One command, fully reviewed plan.
allowed-tools: [Agent, Bash, Read, Write, Edit, Glob, Grep, Skill, AskUserQuestion]
user-invocable: true
argument-hint: "[plan-file-path|issue-number]"
automation: gated
---

# /autoplan — Auto-Review Pipeline

One command. Rough plan in, fully reviewed plan out.

## Purpose

Before implementation, review the plan from 3 angles — strategy, engineering, and security — with auto-decisions. Surfaces taste decisions (where reasonable people could disagree) at a final approval gate for human judgment.

Replaces 15-30 intermediate questions with 6 decision principles. You still do the full analysis — the only thing that changes is who answers the questions.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Plan File | conversation or `~/.claude/plans/` | ✅ | ✅ | Plan to review |
| CLAUDE.md | `CLAUDE.md` | ✅ | | Project rules |
| Requirements | `docs/memory/requirements.md` | ✅ | | Feature specs |
| Architecture | `docs/memory/architecture.md` | ✅ | | System design |
| Feature Flows | `docs/memory/feature-flows/` | ✅ | | Existing patterns |
| Source Code | `src/`, `docker/` | ✅ | | Implementation context |
| GitHub Issues | `abilityai/trinity` | ✅ | | Issue details |
| Git | `.git/` | ✅ | | Branch, diff |

## Arguments

- Plan file path: review a specific plan file
- Issue number: `#42` or `42` — fetch issue and review as the plan
- No argument: look for active plan in conversation context or most recent plan file

## The 6 Decision Principles

These rules auto-answer every intermediate question:

1. **Choose completeness** — Ship the whole thing. Pick the approach that covers more edge cases.
2. **Boil lakes** — Fix everything in the blast radius (files modified + direct importers). Auto-approve expansions that are in blast radius AND < 1 day effort (< 5 files, no new infra).
3. **Pragmatic** — If two options fix the same thing, pick the cleaner one. 5 seconds choosing, not 5 minutes.
4. **DRY** — Duplicates existing functionality? Reject. Reuse what exists.
5. **Explicit over clever** — 10-line obvious fix > 200-line abstraction. Pick what a new contributor reads in 30 seconds.
6. **Bias toward action** — Merge > review cycles > stale deliberation. Flag concerns but don't block.

**Conflict resolution (context-dependent tiebreakers):**
- **Strategy phase**: P1 (completeness) + P2 (boil lakes) dominate
- **Eng phase**: P5 (explicit) + P3 (pragmatic) dominate
- **Security phase**: P1 (completeness) + P5 (explicit) dominate

## Decision Classification

Every auto-decision is classified:

**Mechanical** — one clearly right answer. Auto-decide silently.

**Taste** — reasonable people could disagree. Auto-decide with recommendation, but surface at the final gate. Three natural sources:
1. **Close approaches** — top two are both viable with different tradeoffs
2. **Borderline scope** — in blast radius but 3-5 files, or ambiguous radius
3. **Subagent disagreements** — independent reviewer recommends differently with valid reasoning

## Prerequisites

- A plan exists (plan file, issue, or description in conversation)
- On a feature branch (not `main` or `dev`)
- `docs/memory/requirements.md` and `docs/memory/architecture.md` readable

## Process

### Phase 0: Intake

**Step 1: Read context**
- Read CLAUDE.md, `docs/memory/requirements.md`, `docs/memory/architecture.md`
- `git log --oneline -20`
- `git diff dev --stat` (or the PR's actual base branch — `main` for release-cut PRs)
- Identify the plan source from `$ARGUMENTS` or conversation context

**Step 2: Load plan**

If plan file path → read it.
If issue number → `gh issue view $NUMBER --repo abilityai/trinity --json title,body,labels`.
If no argument → check conversation context for an active plan.

**Step 3: Assess scope**

- Count files likely affected
- Identify which layers are touched (backend, frontend, agent, docker, config)
- Detect if UI scope is involved (look for component/view/store references)

Output: "Here's what I'm working with: [plan summary]. Layers: [list]. Starting full review pipeline with auto-decisions."

---

### Phase 1: Strategy Review

Evaluate the plan's strategic foundations.

**Step 1.1: Premise Challenge**

Identify every assumption the plan makes. For each:
- Is it stated or just assumed?
- What evidence supports it?
- What happens if it's wrong?

**GATE: Present premises to user for confirmation.** This is the ONE question that is NOT auto-decided. Premises require human judgment.

```
## Premises Found

1. [Premise] — Evidence: [evidence] — Risk if wrong: [risk]
2. ...

Confirm these premises are valid?
```

**Step 1.2: Existing Code Leverage**

Map each sub-problem in the plan to existing code:
```
Sub-problem → Existing Code → Reuse or Build?
```

Check `docs/memory/feature-flows/` for related flows. Check `src/` for similar patterns.

**Step 1.3: Alternatives Analysis**

For each major decision in the plan, identify 2-3 approaches:
```
| Approach | Effort | Risk | Pros | Cons |
```

Auto-decide using P1 (completeness) + P3 (pragmatic). If top 2 are close → mark TASTE DECISION.

**Step 1.4: Scope Calibration**

- What's in scope? What's explicitly out?
- Are there scope expansions that are in blast radius and cheap? (P2: approve if < 5 files)
- Are there scope items that duplicate existing functionality? (P4: reject)

**Step 1.5: Independent Strategy Voice**

Spawn an Agent subagent for independent review:

```
"Read the plan at [path/content]. You are an independent strategist reviewing this plan.
You have NOT seen any prior review. Evaluate:
1. Is this the right problem to solve? Could a reframing yield 10x impact?
2. Are the premises stated or just assumed? Which could be wrong?
3. What's the 6-month regret scenario?
4. What alternatives were dismissed without sufficient analysis?
For each finding: what's wrong, severity (critical/high/medium), and the fix."
```

**Phase 1 outputs:**
- Premise challenge with specific premises named
- Existing code leverage map
- Alternatives table with auto-decisions
- Scope calibration (in scope / out of scope / deferred)
- Independent voice findings

---

### Phase 2: Engineering Review

Evaluate architecture, code quality, test coverage, and performance.

**Step 2.1: Architecture Analysis**

Read the actual code referenced by the plan. For each component:
- Is the component structure sound? Coupling concerns?
- Does it follow existing patterns in `docs/memory/architecture.md`?
- New API endpoints — do they follow the existing router pattern in `src/backend/main.py`?

Produce an ASCII dependency graph showing new components and their relationships to existing ones.

**Step 2.2: Edge Cases & Error Paths**

For each new endpoint/feature:
- What breaks under load?
- What's the null/empty/error path?
- What happens on partial failure?

Produce an Error & Rescue Registry:
```
| Error Case | Handler | Recovery | Tested? |
```

**Step 2.3: Test Coverage Analysis**

- What new codepaths does this plan introduce?
- For each: what type of test covers it? Does one exist?
- What test gaps remain?

Produce a test diagram mapping codepaths to coverage.

**Step 2.4: Performance**

- N+1 queries?
- Memory concerns (large collections, streaming)?
- WebSocket broadcast storms?
- Docker container resource limits?

**Step 2.5: Independent Engineering Voice**

Spawn an Agent subagent:

```
"Read the plan at [path/content]. You are an independent senior engineer reviewing this plan.
You have NOT seen any prior review. Evaluate:
1. Architecture: component structure sound? Coupling?
2. Edge cases: what breaks under 10x load? Nil/empty/error paths?
3. Tests: what's missing? What breaks at 2am Friday?
4. Security: new attack surface? Auth boundaries?
5. Hidden complexity: what looks simple but isn't?
For each finding: what's wrong, severity, and the fix."
```

**Phase 2 outputs:**
- Architecture ASCII diagram
- Error & Rescue Registry
- Test coverage diagram with gaps
- Performance assessment
- Independent voice findings
- Failure modes registry

---

### Phase 3: Security Review

Evaluate security implications of the plan.

**Step 3.1: New Attack Surface**

What new endpoints, WebSocket channels, Docker permissions, or MCP tools does this plan introduce?

**Step 3.2: Auth Boundary Check**

For each new endpoint:
- Does it require authentication?
- Does it check authorization (user can only access their own resources)?
- Is there rate limiting on sensitive operations?

**Step 3.3: Input Validation**

For each new input path:
- Is user input validated before use?
- Are there injection vectors (SQL, command, template)?
- Are file paths sanitized?

**Step 3.4: Credential Safety**

If the plan touches credentials:
- Are credential values masked in logs?
- Are secrets stored in Redis (not SQLite)?
- Is the credential injection path secure?

**Step 3.5: Trinity-Specific Security**

- Agent container isolation: can agents escape their container?
- Docker socket: is access appropriately restricted?
- MCP server: are tool calls validated?
- WebSocket: is authentication checked before accept?

**Phase 3 outputs:**
- New attack surface map
- Auth boundary audit
- Input validation assessment
- Credential safety check
- Trinity-specific security findings

---

### Decision Audit Trail

After each auto-decision, append a row to the plan (or conversation):

```markdown
## Decision Audit Trail

| # | Phase | Decision | Principle | Rationale | Rejected Alternative |
|---|-------|----------|-----------|-----------|---------------------|
| 1 | Strategy | Use existing WebSocket pattern | P4 (DRY) | Already have broadcast infra | Build custom event system |
```

Log every decision. No silent auto-decisions.

---

### Phase 4: Final Approval Gate

**STOP and present the final state to the user.**

```
## /autoplan Review Complete

### Plan Summary
[1-3 sentence summary]

### Decisions Made: [N] total ([M] auto-decided, [K] choices for you)

### Your Choices (taste decisions)
[For each taste decision:]
**Choice [N]: [title]** (from [phase])
I recommend [X] — [principle]. But [Y] is also viable:
  [1-sentence downstream impact if you pick Y]

### Auto-Decided: [M] decisions [see Decision Audit Trail]

### Review Scores
- Strategy: [summary]
- Engineering: [summary]
- Security: [summary]

### Deferred Items
[Items auto-deferred with reasons]
```

**Options**:
- A) Approve as-is (accept all recommendations)
- B) Approve with overrides (specify which taste decisions to change)
- C) Interrogate (ask about any specific decision)
- D) Revise (the plan needs changes — re-run affected phases, max 3 cycles)
- E) Reject (start over)

On approval, output: "Plan approved. Run `/implement` to begin implementation, or `/sprint` to run the full cycle."

## Completion Checklist

- [ ] Plan loaded and context read
- [ ] Premises identified and presented to user
- [ ] Existing code leverage map produced
- [ ] Alternatives analyzed with auto-decisions
- [ ] Scope calibrated
- [ ] Independent strategy voice ran
- [ ] Architecture diagram produced
- [ ] Error & Rescue Registry produced
- [ ] Test coverage analyzed
- [ ] Performance assessed
- [ ] Independent engineering voice ran
- [ ] New attack surface mapped
- [ ] Auth boundaries checked
- [ ] Input validation assessed
- [ ] Credential safety verified
- [ ] Decision audit trail populated
- [ ] Final approval gate presented

## Error Recovery

| Error | Recovery |
|-------|----------|
| No plan found | Ask user to provide plan file path or issue number |
| Subagent fails | Continue with primary review, note "[single-reviewer mode]" |
| Too many taste decisions (8+) | Group by phase, add warning about high ambiguity |
| Premise gate rejected | User provides corrections, re-run Phase 1 |
| Plan revision requested | Re-run affected phases only, max 3 cycles |

## Self-Improvement

After completing this skill's primary task, consider tactical improvements:

- [ ] **Review execution**: Were there friction points, unclear steps, or inefficiencies?
- [ ] **Identify improvements**: Could error handling, step ordering, or instructions be clearer?
- [ ] **Scope check**: Only tactical/execution changes — NOT changes to core purpose or goals
- [ ] **Apply improvement** (if identified):
  - [ ] Edit this SKILL.md with the specific improvement
  - [ ] Keep changes minimal and focused
- [ ] **Version control** (if in a git repository):
  - [ ] Stage: `git add .claude/skills/autoplan/SKILL.md`
  - [ ] Commit: `git commit -m "refactor(autoplan): <brief improvement description>"`
