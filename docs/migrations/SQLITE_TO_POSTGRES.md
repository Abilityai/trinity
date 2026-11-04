# SQLite → PostgreSQL Migration & SQLite End-of-Support

> **SQLite reached end-of-support on September 1, 2026.** Every compose file now runs a
> bundled PostgreSQL by default, and the Trinity images refuse to boot on SQLite.

## TL;DR

- A fresh install on any compose file runs on PostgreSQL. No `trinity.db` is created.
- An install with data in `trinity.db` refuses to boot until it moves. It never comes up on
  an empty database.
- Move it with one command, below. The SQLite file is not modified.
- The SQLite runner stays for one more release cycle, then leaves the code.

## What an install sees after upgrading

| State | Result |
|---|---|
| `DATABASE_URL` unset, no SQLite data | Runs on the bundled PostgreSQL |
| `DATABASE_URL=postgresql://...`, database has users | Runs. A kept `trinity.db` is ignored |
| `trinity.db` has data, PostgreSQL has no users | **Refused**: backend logs the guidance, `start.sh` stops before pulling |
| `DATABASE_URL=` (empty) | **Refused**, unless `TRINITY_ALLOW_SQLITE=1` is also set |

A refused backend appears as an unhealthy container whose workers keep respawning. The reason
is in `docker compose logs backend`.

`POSTGRES_PASSWORD` is required by every compose file. `start.sh` generates it. Running compose
directly without it stops at a render error that names this page.

## Migrating an existing deployment

**Minimum version:** the first release after v1.0.0 carries the copy command. v1.0.0 and earlier do
not have it. Later releases drop the SQLite runner and cannot read a SQLite database, so pass through
a release that has both. The release notes name the last one.

1. Check out the target release. On a hosted install, also set `TRINITY_IMAGE_TAG` to it in `.env`.
   Do this by hand: `update.sh` restores the old checkout and tag when it hits the refusal, and the old
   release has no copy command.
2. Make sure `.env` has `POSTGRES_PASSWORD` and no `DATABASE_URL=` line. `start.sh` writes the password,
   even when it refuses. Otherwise generate one with `openssl rand -hex 24`. Every compose command stops
   at a render error until it is set.
3. Get the image for that release. Without this, `run` uses the old local image, which has no copy command
   and no refusal.
   ```bash
   docker compose -f docker-compose.hosted.yml pull backend     # hosted
   docker compose <your -f files> build backend                 # prod and dev
   ```
4. Stop the stack and back up:
   ```bash
   docker compose <your -f files> stop backend scheduler
   cp trinity-data/trinity.db ~/trinity-before-postgres.db
   ```
   On the dev stack the file is in the `trinity-data` volume.
5. Copy:
   ```bash
   docker compose <your -f files> run --rm backend python -m db.sqlite_to_postgres
   ```
   This takes a snapshot beside `trinity.db`, brings the snapshot to this release's schema, builds
   the PostgreSQL schema with Alembic, copies every table in one transaction, resets sequences and
   verifies rowcounts. If it fails, no rows are committed and you can run it again. On success it writes
   `trinity.db.copied-to-postgres` beside the file, which tells `start.sh` the copy is done.
6. Start the stack (`./scripts/deploy/start.sh`, or your usual `docker compose up -d`) and check it
   as described under [Verifying the cutover](#verifying-the-cutover).

Keep `trinity.db` and the snapshot until you have checked the result. Writes made after the copy
exist only in PostgreSQL.

**Rolling back:** check out the previous release, set `DATABASE_URL=` (empty), delete
`trinity.db.copied-to-postgres`, and start the stack. `trinity.db` is unchanged.

| Exit code | Meaning | Action |
|---|---|---|
| 1 | No source file, `DATABASE_URL` is not PostgreSQL, the SQLite database has no users, or the enterprise module cannot read a URL containing `%` | Fix `.env`. With no users there is nothing to copy: move `trinity.db` aside and start the stack. For `%`, update the enterprise module or use a password that needs no URL encoding |
| 2 | Rowcounts differ | Investigate before starting the stack |
| 3 | SQLite holds data PostgreSQL has no place for, or its schema stopped short | Investigate; a table or column left over from an old release is the usual cause |
| 4 | Load error, a NUL byte in a text value, or the target already holds data | The message names the table and row. Remove the NUL byte from `trinity.db` and run it again |

**Starting fresh instead:** move `trinity.db` aside and start the stack.

**Staying on SQLite for this release only:** set both `DATABASE_URL=` (empty) and
`TRINITY_ALLOW_SQLITE=1` in `.env`. The next release removes this.

### An install already on a separate PostgreSQL container

A PostgreSQL container started by hand collides with the bundled service, which also uses the name
`trinity-postgres` and the hostname `postgres`. Adopt the compose service on the existing volume:

1. Note the volume: `docker inspect trinity-postgres --format '{{range .Mounts}}{{.Name}}{{end}}'`.
2. In `.env`, set `POSTGRES_PASSWORD` to the current password and `TRINITY_POSTGRES_VOLUME` to that
   volume.
3. `docker compose <your -f files> stop backend scheduler`, then `docker rm -f trinity-postgres`.
   The volume stays. Never add `-v`.
4. `docker compose <your -f files> up -d`.

An operator-managed server on another host needs only `POSTGRES_PASSWORD` set to any generated value:
the bundled container then runs idle.

Nightly backups (#2216) on PostgreSQL are `pg_dump -Fc` files
(`trinity-backup-YYYYMMDD.dump`) under `trinity-data/backups/`.

## Verifying the cutover

- `GET /health` returns healthy (and reports no pending migration).
- Alembic is at head:
  ```bash
  docker exec trinity-postgres psql -U trinity -d trinity -tAc \
    "SELECT version_num FROM alembic_version;"
  ```
- Agents list, schedules, and recent executions render as expected in the UI.
- **The canary invariant harness (CANARY-001) is backend-agnostic since #1540** —
  every SQL-tier collector reads the configured backend via the
  `get_engine()`/`DATABASE_URL` seam, so it is safe to keep `CANARY_ENABLED=1` on
  PostgreSQL. Do **not** re-derive the retired SQLite-only constraint from this
  doc; run-state is observable at `GET /api/canary/status` (#2217).


## See also

- `.env.example` — `DATABASE_URL` and the bundled PostgreSQL settings.
- `docs/memory/architecture.md` — Invariant #3 (dual-track migration policy).
- `docs/planning/TARGET_ARCHITECTURE.md` — backend direction.
- Related issues: #300 (configurable backend), #1183 (Alembic for Postgres),
  #746 (single-source-of-truth schema), #1160 (migration runner safety).
- **#1279** — the in-repo migration reminder (CLAUDE.md/README/AGENTS.md), the
  always-visible companion to this announcement, removed at SQLite EOL.
