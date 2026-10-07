# utilities Plugin

General-purpose ops and productivity skills for SSH-accessible services and daily workflows (utilities v1.2.2, 7 skills).

## Installation

```bash
/plugin install utilities@abilityai
```

## Skills

| Skill | Description |
|-------|-------------|
| `/utilities:save-conversation` | Save conversation as structured markdown |
| `/utilities:investigate-incident` | Structured incident investigation |
| `/utilities:bug-report` | Create sanitized GitHub issue |
| `/utilities:safe-deploy` | Safe deployment of an SSH-accessible docker-compose stack — `update`, `rollback`, `diagnose` |
| `/utilities:docker-ops` | Docker Compose service management over SSH — `logs`, `restart`, `telemetry`, `cleanup` |
| `/utilities:sync-ops-knowledge` | Update ops docs from commits |
| `/utilities:batch-claude-loop` | Batch headless Claude Code calls |

## Safe Deployment

Deploy with automatic backup and rollback capability:

```bash
# Deploy with backup
/utilities:safe-deploy update

# Roll back to a previous commit, optionally restoring the DB backup
/utilities:safe-deploy rollback

# Health analysis
/utilities:safe-deploy diagnose
```

`update` backs up the database, records the current commit, pulls, rebuilds, restarts, validates health, and writes a deploy log under `deploys/`. Rollback is not automatic: if validation fails, run `rollback`, which asks which commit to return to (default: the previous one) and whether to restore the backup, then runs `diagnose` to confirm.

## Docker Operations

Manage Docker containers:

```bash
/utilities:docker-ops logs backend
/utilities:docker-ops restart frontend
/utilities:docker-ops telemetry          # CPU, memory, disk, container stats
/utilities:docker-ops cleanup            # prune Docker resources (dry run by default)
```

## Incident Investigation

Structured approach to investigating production issues:

```bash
/utilities:investigate-incident
```

Works in phases:
1. **Context** — What's happening, and since when?
2. **Severity** — Initial classification
3. **Evidence** — Service health, recent errors across services, resource metrics, application health, database integrity, recent deployments
4. **Analysis** — What the evidence shows
5. **Hypotheses** — Ranked root-cause candidates with confidence
6. **Report** — An incident report with timeline, evidence, hypotheses, and recommended next steps

## Conversation Export

Save the current conversation as markdown:

```bash
/utilities:save-conversation
```

Writes a structured summary — not a raw transcript — to a dated file (`YYYY-MM-DD_<topic>.md`) under `saved-conversations/` (override with a `.conversation-config` file or `CONVERSATION_STORAGE_PATH`). Sections cover the summary, context, goal, key exchanges, decisions made, actions taken, outcome, insights, and follow-up.

## Batch Processing

Run Claude Code headlessly on multiple inputs:

```bash
/utilities:batch-claude-loop
```

Useful for:
- Processing multiple files
- Running the same analysis across repos
- Automated code review

## See Also

- [Abilities Overview](overview.md) — Full toolkit overview
- [GitHub: abilityai/abilities](https://github.com/abilityai/abilities) — Source repository
