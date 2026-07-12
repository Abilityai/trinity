# Safe Upgrade

Prepare or execute a persistent-state-safe Trinity platform upgrade.

## Usage

```text
/ops/safe-upgrade
```

## Instructions

1. Confirm whether you have host shell access to the Trinity checkout that owns the running Docker Compose project.

2. If you do not have host shell access, do not improvise from inside the agent container. Return the exact host command for the operator to run:

```bash
./scripts/deploy/safe-upgrade.sh --project-name trinity
```

For production with host-specific files:

```bash
./scripts/deploy/safe-upgrade.sh \
  --project-name trinity \
  --env-file /path/to/.env \
  -f docker-compose.prod.yml \
  -f /path/to/host-override.yml
```

3. If you do have host shell access, run the safe wrapper from the Trinity repo. Do not run `docker compose down -v` or `docker volume rm`.

4. Verify and report:
   - backup bundle path;
   - manifest path;
   - whether `postgres.dump`, `backend-data.tgz`, `env.backup`, and agent workspace archives were created;
   - backend `/health`;
   - backend `/api/version`;
   - any agent containers that were unhealthy before or after the upgrade.

5. Save a report to `~/reports/upgrades/YYYY-MM-DD_HHMM.md`.

## Safety Rules

- Keep the same Docker Compose project name.
- Back up persistent state before changing code, images, containers, or worktrees.
- Agent containers are not deleted during a routine platform upgrade.
- If the agent base image changed, recreate agents only as a separate explicit step after a backup exists.
- If the instance uses external PostgreSQL, require a managed snapshot or operator-provided dump before proceeding.
