---
name: trinity-safe-upgrade
description: Use when upgrading, deploying, rolling forward, pruning, or cleaning a running Trinity instance with real state. Ensures agents preserve database data, backend /data, .env encryption keys, and agent workspace volumes before changing Docker services or worktrees.
---

# Trinity Safe Upgrade

Use this skill before changing a running Trinity instance that may contain work the operator cares about.

## Non-Negotiables

- Keep the same Docker Compose project name for the instance. Do not accidentally create a second set of volumes.
- Run a persistent-state backup before changing code, images, containers, or worktrees.
- Never run `docker compose down -v` or `docker volume rm` during a routine upgrade.
- Do not delete or recreate agent containers as a hidden side effect. Agent recreation is an explicit follow-up, and only after a backup exists.
- Treat external PostgreSQL as incomplete until a managed snapshot or operator-provided dump exists.

## Standard Path

From the Trinity repo on the target host:

```bash
./scripts/deploy/safe-upgrade.sh --project-name trinity
```

For production with a host env file or override compose file, pass the same inputs that created the running stack:

```bash
./scripts/deploy/safe-upgrade.sh \
  --project-name trinity \
  --env-file /path/to/.env \
  -f docker-compose.prod.yml \
  -f /path/to/host-override.yml
```

The wrapper runs `scripts/deploy/backup-persistent-state.sh`, rebuilds platform services, starts the selected services, waits for backend health, and prints `/api/version`.

## Backup-Only Path

Use this before any destructive operation or manual migration:

```bash
./scripts/deploy/backup-persistent-state.sh \
  --project-name trinity \
  --env-file /path/to/.env \
  --output-dir /srv/trinity-backups/persistent-state
```

Expected evidence:

- `manifest.txt` exists in the new backup bundle.
- `postgres.dump` exists and verifies with `pg_restore -l` when bundled PostgreSQL is active.
- `backend-data.tgz` exists when the backend container is running.
- `agent-workspaces/*.tgz` exists for each `agent-*-workspace` volume.
- `env.backup` exists when `.env` is present.

## References

- Upgrade runbook: `docs/user-docs/guides/deploying/upgrading.md`
- Backup details: `docs/user-docs/guides/deploying/backup-and-restore.md`
- Requirements entry: `docs/memory/requirements/infrastructure.md`
- Feature flow: `docs/memory/feature-flows/safe-upgrade-persistent-state.md`
