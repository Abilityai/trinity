# Requirements — Infrastructure, Platform Operations, CLI, Canary, Enterprise, Build Info

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 8. Infrastructure

### 8.1 Docker as Source of Truth
- **Status**: ✅ Implemented
- **Description**: No in-memory registry; query Docker directly with container labels

### 8.2 SQLite Data Persistence
- **Status**: ✅ Implemented
- **Description**: Users, ownership, API keys, chat sessions via bind mount

### 8.2a Automatic Database Backups (#2216)
- **Status**: ✅ Implemented (2026-08-16)
- **GitHub Issue**: #2216 (epic #1258 — First-run & self-host robustness)
- **Description**: The platform database gets automatic recovery points on every install, both backends, with zero operator setup. Before this, `scripts/deploy/backup-database.sh` shipped but nothing invoked it — and it was a workstation-side GCP pull doing a naive live `cp` (no journal sidecars, no writer quiescing, no PG arm, no retention). A real instance lost its entire DB to a single stray write over the SQLite header with no recovery point; the file was `sqlite3 .recover`-able but there was nothing to restore. The backup gap turns an ordinary human slip into total data loss — no code hardening addresses that; only recovery points do.
- **Requirements**:
  - **BKUP-001** (automatic, both backends, zero setup): `services/db_backup_service.py` runs a daily job (default **03:30 UTC** — before the 04:15 audit prune and 04:30 VACUUM, so a backup captures more data, and after the 03:00 log archival which touches a different volume) from the backend lifespan, the `db_vacuum_service` shape. SQLite arm: stdlib `sqlite3.Connection.backup()` (one-shot — a consistent snapshot as of backup start, standalone `.db`, no sidecars; correct in both DELETE and WAL journal modes — the live platform DB runs SQLite's default **DELETE** mode, verified on a live instance). PG arm: `pg_dump -Fc` subprocess; `postgresql-client-17` (major-pinned, #1823 rationale — the base image's Debian codename floats) is baked into `docker/backend/Dockerfile`. Default **ON**.
  - **BKUP-002** (never `cp` a live DB): the copy primitive is the online-backup API, never a file copy. A naive `cp` of a live SQLite file is the exact torn-page/hot-journal hazard the incident class rides on. The legacy scripts and the user-doc manual recipes are corrected to the safe primitive in the same change.
  - **BKUP-003** (destination + scope, stated honestly): artifacts land in `/data/backups/` (derived as `<db dir>/backups`, created `0700`), day-keyed `trinity-backup-YYYYMMDD.db` / `.dump` and `pre-migration-YYYYMMDD-HHMMSS.db`. **Same-disk scope**: protects against the incident class (stray write, fat-fingered delete, bad migration) — NOT against disk loss. The scope is machine-readable (`scope: "same-disk"` in the status block); off-site is a flagged follow-up (the `ArchiveStorage` ABC is the ready seam). `.env`/`CREDENTIAL_ENCRYPTION_KEY`/Redis stay documented operator steps; agent workspaces are #1169's domain.
  - **BKUP-004** (retention, both directions bounded — the #1638 fail-safe direction INVERTS here): `backup_retention_days` joins the ops-settings retention model (`OPS_SETTINGS_DEFAULTS` = 14, `OPS_SETTINGS_VALIDATION` bounds **1**–3650 — `0` is deliberately INVALID: in the existing model `0` means "disable the sweep", which for backups means keep-forever = the #1871 disk-fill trap; disabling backups is the separate explicit `DB_BACKUP_ENABLED=false`), `RETENTION_OPS_KEYS` membership (⇒ `/ops/reset` skips it, generic `PUT /api/settings/{key}` 422-blocks it, writes only via the validated `PUT /api/settings/ops/config`). NOT in `COMMUNITY_FRESH_INSTALL_SEED` (floor-seeding means *fewer* days = the destructive direction here).
  - **BKUP-005** (floor — never zero recovery points): fixed `BACKUP_MIN_KEEP = 3`, a constant and deliberately NOT a knob (#1644: a control that must explain which way is safe is the wrong control). Prune never deletes the newest 3 artifacts regardless of age — a `backup_retention_days=1` slip keeps 3 recovery points. Pinned ≥ 2 by test.
  - **BKUP-006** (prune runs on EVERY scheduled attempt — success, failure, or space-skip): prune-only-after-success is a disk-full Catch-22 (disk fills → preflight skips the backup → prune never runs → lowering the window frees nothing). Safety is carried by prune's own bounds: the MIN_KEEP floor, the retention window (never prune-to-make-room below it), and a pattern scope (`trinity-backup-*.db`, `trinity-backup-*.dump`, `pre-migration-*.db` — never arbitrary files). A failed copy never enters the artifact namespace (verify-before-`os.replace`), so it cannot displace anything.
  - **BKUP-007** (inverted reader coercion): the generic ops-settings readers coerce garbage → `0` → "sweep disabled" — safe for row retention, catastrophic here (keep-forever). ONE shared reader (`effective_backup_retention_days()`) coerces unparseable/out-of-bounds → **default 14 + WARNING**; `GET /api/settings/retention` EXCLUDES the key from the generic `windows` map and reports it only inside the `backup` block via that reader, and the boot retention log special-cases it the same way — so no two surfaces can disagree about the effective value.
  - **BKUP-008** (free-space preflight): before every write, destination free space must be ≥ 1.2× the source size (SQLite: file + sidecar bytes; PG: `pg_database_size(current_database())` — `-Fc` compresses, so this over-estimates, the safe direction). Short → skip + WARNING + alarm; never die, and never prune-to-make-room. An unavailable estimate proceeds (the write itself fails loudly on ENOSPC).
  - **BKUP-009** (tmp hygiene): writes go to `<final>.tmp.<pid>` then atomic `os.replace`; a `finally:` unlinks the run's own tmp on any failure/timeout; each run sweeps `*.tmp.*` older than 24h (the SIGKILL-mid-copy crash window — an orphaned tmp is unbounded growth in a dir the pattern-scoped prune deliberately won't touch).
  - **BKUP-010** (double-fire safety, `--workers 2`): two independent fail-safe guards — day-keyed idempotence (today's artifact exists → skip, works with Redis down) and a fail-open Redis SETNX lease (`db_backup:running`, own-token compare-and-delete release, TTL derived comment-linked from the pg_dump timeout). The lease is **duplicate-I/O suppression, never a correctness boundary** — correctness is pid-suffixed tmps + atomic replace + day-keyed names + pattern-scoped prune; the worst concurrent-duplicate outcome is one clean loud ENOSPC while the sibling completes.
  - **BKUP-011** (PG conninfo handling): `pg_dump -Fc -d <conninfo>` receives the operator's `DATABASE_URL` with exactly two rewrites — the SQLAlchemy driver suffix normalized away (`postgresql+psycopg2://` → `postgresql://`) and the password stripped. Query params (`sslmode`, `sslrootcert`, `options`) pass through untouched — re-parsing into `-h/-p/-U` flags would silently drop the SSL params managed PG (RDS/Cloud SQL) mandates. Password travels ONLY via subprocess-env `PGPASSWORD`, never argv (world-readable /proc), never logged. Timeout → explicit `kill` → `wait()` reap → tmp unlink. A missing `pg_dump` binary (un-rebuilt image) fails loudly with a status + alarm naming the rebuild.
  - **BKUP-012** (boot-time pre-migration backup, SQLite arm only): inside `init_database()`'s `migration_lock` window, BEFORE the first migration pass, `db/backup_primitives.maybe_backup_before_migrations()` takes a safety copy when migrations are actually pending (`migration_health`). Fresh install → **silent** skip (INFO — a first boot must not emit a scary ERROR); corrupt DB → ERROR + best-effort status row, and the subsequent migration run raises the SAME exception fingerprint as before this feature (the incident's uvicorn-dies-at-import signature is provably unchanged by test). **Fail-open, never crash-loops boot** (`init_database` runs at import — the #1638 seed contract). PG deliberately excluded in v1 (Alembic DDL is transactional; flagged follow-up).
  - **BKUP-013** (failure is operator-visible, over TIME not just at the edge): (a) edge-triggered operator-queue alarm on the success→failure transition and on a no-space skip — one alarm per failure episode, re-armed by an intervening success; (b) a staleness re-alarm once `now − last_success` exceeds 3 days (≈3 missed dailies), re-fired at most weekly — silence over time is also a failure. Alarms are platform-created (direct `db.create_operator_queue_item`, bypassing the #1632 agent-ingestion caps by construction), hosted on sentinel `_db-backup` (uncreatable — `sanitize_agent_name` strips the leading `_`), id prefix `db-backup-` registered in `_RESERVED_ID_PREFIXES` (else an agent could pre-create and, via `on_conflict_do_nothing`, silence its own backup-failure alarm), and the sentinel excluded from canary L-03's orphan scan (sentinel tuple, service↔canary parity-tested). Alarm context carries status/paths/sizes only — never row data (canary G-04 rule).
  - **BKUP-014** (status readable without shell): durable `system_settings` keys (`db_backup_last_status` ∈ `ok|failed|skipped_no_space` (a disabled job writes nothing — the status block's separate `enabled` field carries that), `db_backup_last_success_at`, `db_backup_last_error`, `db_backup_last_path`, `db_backup_last_size_bytes`, `db_backup_last_duration_ms`, `db_backup_last_trigger` ∈ `scheduled|boot_pre_migration`) written by the job and the boot hook (in-process state is invisible to the other uvicorn worker — ent#236 lesson). Surfaced as a `backup` block on the existing admin-only `GET /api/settings/retention` (no new endpoint, no new auth surface): last-run keys + live dir listing (count, total bytes, newest artifact age) + `enabled` + `retention_days` + `min_keep` + `stale` + `scope`.
  - **BKUP-015** (restore documented AND exercised): `docs/user-docs/guides/deploying/backup-and-restore.md` rewritten (automatic backups, safe manual primitive, restore incl. stale `-wal`/`-shm`/`-journal` removal beside the restored file, PG `pg_restore -Fc` into an empty DB services-stopped, both writers — backend AND scheduler — stopped first); the incident is replayed by test: seed → backup → overwrite the source header with the literal `HTTP/1.1 200 OK` bytes → restore from the artifact → row counts equal.
  - **BKUP-016** (default ON, documented disable): `DB_BACKUP_ENABLED` default `true` — read through ONE helper (`backup_primitives.backup_enabled_from_env`) by BOTH producers, so `false` disables the nightly job AND the boot pre-migration copy (the prune lives only in the job's tail; a boot hook that ignored the knob would write one un-pruned full-DB copy per upgrade forever — the #1871 class in the one configuration where the operator opted out); `DB_BACKUP_HOUR`/`DB_BACKUP_MINUTE`/`DB_BACKUP_PG_DUMP_TIMEOUT_SECONDS` env-tunable. All forwarded in **BOTH** compose files (the #1486/#1488 inert-knob class — the backend service uses explicit env lists, no `env_file`) + documented in `.env.example`. Malformed hour/minute fall back to defaults with a WARNING instead of crashing at import.
- **Explicit non-goals (flagged, not filed)**: off-site destinations (ArchiveStorage seam), PG boot-time backup, compression, manual backup-now endpoint, WAL migration, the pre-existing unforwarded `DB_VACUUM_*`/`AUDIT_RETENTION_*` env knobs.
- **Tests**: `tests/unit/test_2216_backup_primitives.py`, `test_2216_db_backup_service.py`, `test_2216_boot_pre_migration_backup.py`, `test_2216_backup_restore_roundtrip.py`, `test_2216_backup_observability.py`
- **Docs**: [database-backup.md](../feature-flows/database-backup.md); cross-ref §12.10 (retention model)

### 8.3 Redis for Secrets
- **Status**: ✅ Implemented
- **Description**: Credential storage, OAuth state with AOF persistence

### 8.4 Audit Logging
- **Status**: ✅ Implemented
- **Description**: Security event tracking via Vector log aggregation

### 8.5 Container Security
- **Status**: ✅ Implemented (Updated 2026-03-26)
- **Description**: Non-root execution, CAP_DROP ALL, isolated network, base image allowlist
- **Key Features**: Optional full capabilities mode for containers needing system access, base image allowlist validation (SEC-172)
- **Base Image Allowlist** (SEC-172): Agent creation validates `base_image` against configurable allowlist (`base_image_allowlist` system setting, default `["trinity-agent-base:*"]`). Blocks arbitrary Docker image pulls that could access internal network services. Returns HTTP 403 for disallowed images.

### 8.5b Base-Image Adoption Semantics (#1809, #1816)
- **Status**: ✅ Implemented (2026-07-28)
- **GitHub Issues**: #1809 (regular agents), #1816 (`trinity-system`)
- **Description**: A rebuilt `trinity-agent-base:latest` must be adopted by existing agent containers, which stay pinned to the image **id** they were created from. Adoption happens only at a **cold boundary**.
- **Requirements**:
  - **ADOPT-001**: An agent container whose own `Config.Image` tag no longer resolves to the image id it runs is recreated on its next **cold** start (`check_base_image_matches`, the lazy ninth predicate). Fail-open: any unreadable state skips the evaluation and logs a WARNING.
  - **ADOPT-002**: A **running** agent is never image-recreated. A start of a running agent is a load-bearing idempotent no-op (MCP ensure-running, the SUB-003 auto-switch restart, `restart_system`); image drift is armed fleet-wide by any `build-base-image.sh` run and must never turn it into a container kill. Ephemeral ghosts are excluded outright (volume-less by design).
  - **ADOPT-003** (`trinity-system`): the platform orchestrator adopts at the same cold boundary — backend boot with the container **stopped**, or an explicit `POST /api/system-agent/restart`. `ensure_deployed`'s running branch is **read-only**: it reports `base_image_state` ∈ `stale | current | unknown` and raises an edge-triggered operator-queue alarm on `stale` only (never on `unknown` — a fail-open probe must not manufacture an alert).
  - **ADOPT-004** (AC2, structural): **no** code path may replace the container of a *running* `trinity-system` without an explicit operator stop. Enforced in `start_agent_internal` as an `is_system AND was_already_running` gate over the whole `needs_recreation` block — deliberately independent of predicate count — returning `recreate_deferred: "system_agent_running"` rather than silently doing nothing.
  - **ADOPT-005** (convergence invariant): the container produced by `_create_system_agent` and the container produced by `recreate_container_with_updated_config` must both leave **all eight** config predicates `True`. A permanently-false predicate is an ADOPT-004 hole by construction, because a config-drift recreate resolves the image from a *tag* and is therefore also an image adoption.
  - **ADOPT-006** (rebuild fences): `recreate_missing_container` — the #1559 soft-delete recovery rebuild — **refuses** `trinity-system` with a 409. It reconstructs a regular agent and would irreversibly downgrade the orchestrator (deactivates the **system-scoped** MCP key and mints an agent-scoped replacement, drops `trinity.is-system` / the `/template` bind / `unless-stopped`, arms the scope-403 `TRINITY_BACKEND_URL`). `ensure_deployed`'s create branch is the only supported rebuild. Reachable because `ensure_deployed` runs per uvicorn worker with no leader lock — the race itself is #1817.
  - **ADOPT-007** (operator remedy is human-only): `POST /api/system-agent/restart` and `/reinitialize` require an admin **and** a human principal (`reject_agent_principal`). `assert_admin` alone lets an agent-scoped key through on a default admin-owned install, and #1816 turns `/restart` into a container replacement.
- **Tests**: `tests/unit/test_1809_image_drift_recreate.py`, `tests/unit/test_1816_system_agent_convergence.py`, `tests/unit/test_1816_system_agent_adoption.py`
- **Docs**: [internal-system-agent.md](../feature-flows/internal-system-agent.md) → Base-image adoption; [agent-lifecycle.md](../feature-flows/agent-lifecycle.md)

### 8.5c Container Log Rotation (#1871)
- **Status**: ✅ Implemented (2026-07-29)
- **GitHub Issue**: #1871
- **Description**: Docker's `json-file` driver ships with **no** `max-size` and **no** `max-file`, so every platform and agent container log grew without bound under `/var/lib/docker/containers/`. Nothing fails while it happens; then the Docker data root reaches 100%, dockerd can no longer parse its own logs, and the entire fleet wedges at once (2026-07-27 incident). Trinity's existing retention (`log_archive_service`, `LOG_RETENTION_DAYS`) governs only Vector's aggregate copy at `/data/logs` — the raw Docker copy had no owner.
- **Requirements**:
  - **LOG-001** (platform services): every service in **both** `docker-compose.yml` and `docker-compose.prod.yml` carries a bounded `logging:` block, sourced from one shared `x-logging` anchor so the two files cannot drift. Operator-tunable via `CONTAINER_LOG_MAX_SIZE` / `CONTAINER_LOG_MAX_FILE`.
  - **LOG-002** (agent containers): compose's `logging:` **cannot** reach agent containers — they are created through the Docker SDK, not compose. `AGENT_LOG_CONFIG` (`services/agent_service/capabilities.py`) is the agent-side half, defined once and imported by all three agent-container create sites beside `AGENT_TMPFS_MOUNT`. Operator-tunable via `AGENT_LOG_MAX_SIZE` / `AGENT_LOG_MAX_FILE`.
  - **LOG-003** (fail-safe in **both** directions): a malformed value falls back to the bounded default, **and so does a well-formed but out-of-range one** (>`1g` per file, >`10` files, or zero). A format-only check is insufficient: `1000g` parses cleanly while effectively removing the cap — the exact failure this control exists to prevent, so magnitude is bounded too. A typo must never silently disable rotation (same principle as #1638).
  - **LOG-004** (discoverability): an *explicitly set* value that is rejected logs a `WARNING` naming the variable, the rejected value and the applied default; an **unset** variable is the normal case and stays silent. A silently-ignored knob is the #1039 inert-by-obscurity class.
  - **LOG-005** (apply semantics): `log_config` is **creation-time**, like the tmpfs spec. Platform services adopt on the next `docker compose up`; existing agents adopt on **recreate**, not on a plain restart. Capping does not shrink logs already on disk — reclaiming those on deployed instances is ops-tooling follow-up, out of scope here.
  - **LOG-006** (drift guard): a CI guard fails when a **new** durable-container create site ships without `log_config`, closing the `learnings.md` (2026-07-10) "the create path is never one call site" class. Ephemeral `remove=True` helpers are exempt — Docker deletes their log with the container.
- **Non-goal**: a host-level `/etc/docker/daemon.json`. That belongs to ops provisioning and is defense-in-depth; this makes Trinity correct regardless of host configuration.
- **Tests**: `tests/unit/test_1871_container_log_rotation.py`, `tests/unit/test_1871_log_config_parity.py`
- **Docs**: [container-capabilities.md](../feature-flows/container-capabilities.md) → Container Log Rotation; [vector-logging.md](../feature-flows/vector-logging.md) → Interaction with Docker Log Rotation

### 8.5a SSRF Prevention — Skills Library URL Validation (SEC-179)
- **Status**: ✅ Implemented (2026-03-27)
- **GitHub Issue**: #179
- **Description**: Skills library URL validated against strict github.com allowlist to prevent SSRF leading to DoS (pentest finding 3.2.2, CVSS 6.7)
- **Key Features**: Hostname must be exactly `github.com`, HTTPS enforced, DNS resolution checked against private/internal IP ranges, validation at both write time (`PUT /api/settings/skills_library_url`) and sync time (`POST /api/skills/library/sync`)
- **Tests**: `tests/unit/test_ssrf_skills_library.py` — 28 tests

### 8.6 GCP Production Deployment
- **Status**: ✅ Implemented
- **Description**: SSL/TLS via Let's Encrypt, nginx reverse proxy

### 8.7 Vector Log Aggregation
- **Status**: ✅ Implemented (2025-12-31)
- **Description**: Centralized log aggregation via Vector replacing audit-logger
- **Key Features**: Docker socket capture, VRL transforms, platform.json/agents.json output
- **Flow**: `docs/memory/feature-flows/vector-logging.md`

### 8.8 Frontend E2E Test Infrastructure
- **Status**: ✅ Implemented (2026-04-29)
- **Description**: Playwright-based smoke test harness for the Trinity frontend, gated on the `ui` PR label in CI (#556)
- **Key Features**: Chromium-only smoke suite (dashboard, agents, operating room, templates), storage-state auth pattern (login once, reuse session), label-gated CI workflow (~5 min, opt-in), on-failure artifact upload (screenshots, videos, Trinity logs)

### 8.9 Prebuilt Images & Pull-Only Hosted Install (#2280)
- **Status**: 🟡 Partial (2026-08-24) — publish workflow, hosted compose, `start.sh --hosted`, TLS decision and docs landed; the cloud-init user-data example (AC4) lives in `trinity-ops-public` and is outstanding
- **GitHub Issue**: #2280 (epic #2332 — one-click hosted install); gates #2281 (DigitalOcean Marketplace), #2282 (Vultr), #2283 (Hostinger/Dokploy), #835 (Packer)
- **Description**: A fresh VM must come up serving Trinity in ~2 minutes with no on-box builds. Before this, `docker-compose.prod.yml` carried `build:` blocks and `start.sh` compiled `trinity-agent-base` on the host (Python + Node + Go + Claude Code, ~1.9 GB, 5-10 minutes) — which fails the "one click" bar for every marketplace channel and is rejected outright by managed hosts and template catalogues (Elestio, Dokploy, Coolify, Hostinger Docker Manager), all of which accept pull-only compose files only. This is the gate the whole hosting arc sits behind.
- **Requirements**:
  - **HOST-001** (published images, release-triggered): `.github/workflows/publish-images.yml` builds and pushes five images to GHCR under `ghcr.io/abilityai/` — `trinity-backend`, `trinity-frontend`, `trinity-scheduler`, `trinity-mcp-server` and **`trinity-agent-base`** — on every `v*` tag, plus `workflow_dispatch` for a smoke build. **Every mutable/version tag is gated on `github.event_name == 'push'`**, so a dispatch publishes `sha-<short>` and nothing else. `github.ref` alone answers "is this a tag ref", not "is this a release" — a dispatch started from a tag (the natural way to smoke-test the workflow) carries `refs/tags/v0.9.0` too, and dispatching an OLD tag rebuilds at a new digest (the agent base bakes `Claude Code (latest)`, so the content genuinely differs), republishes `0.9.0`/`v0.9.0`/`0.9` over an immutable version, and walks `latest` **backwards** — silently downgrading every unpinned hosted install on its next `start.sh --hosted`. `flavor: latest=false` is required alongside the gate and is not decoration: the action's default `latest=auto` applies `latest` on any semver tag ref on its own, so gating only the explicit `type=raw` line would have been inert. Re-cutting a release is done by re-pushing the git tag, which fires `push` and takes the full set. Tags: `v0.9.0`, `0.9.0`, `0.9`, `latest`, `sha-<short>` — all for one digest. The **v-prefixed alias is deliberate**: `{{version}}` strips the leading `v`, so a release cut as git tag `v0.9.0` published only `0.9.0` while every doc and `start.sh`'s own pin warning told operators to set `v0.9.0` — a pull that fails `manifest unknown`, which `start.sh` treats as fatal with a message blaming the operator's spelling. The sha tag is a raw value computed from the checked-out HEAD, **not** `type=sha`: that resolves from `github.sha`, which on a `workflow_dispatch` with `ref:` is the dispatching branch's head rather than the tree being built. `GIT_BRANCH` is likewise taken from `inputs.ref || github.ref_name` — a dispatch of `ref: v0.9.0` from `dev` otherwise stamps `GIT_BRANCH=dev` into `GET /api/version` on the exact path meant for out-of-band smoke builds.
  - **HOST-002** (the published backend is OSS-only by construction): the publish checkout sets `submodules: false`, so `src/backend/enterprise` is empty in the build context. That is a structural property of the workflow, not a `.dockerignore` rule someone can edit — and `build-without-submodule.yml` already proves the OSS tree builds standalone.
  - **HOST-003** (build args are per image, not a shared block): only `docker/backend/Dockerfile` declares the six provenance ARGs (→ ENV → `GET /api/version`, #926/#958); `trinity-agent-base` takes `VERSION` alone (mirroring `scripts/deploy/build-base-image.sh`, its only other producer); the scheduler and mcp-server Dockerfiles declare none. Passing an undeclared `--build-arg` makes BuildKit warn on every build, which trains everyone to ignore the warnings that matter. The frontend deliberately takes **none**: every `VITE_*` var is statically inlined into the world-readable bundle at build time, so a published image may carry only values correct for *every* install — which is possible at all only because `VITE_API_URL` is inert (`api.js` uses `baseURL: ''` and nginx proxies same-origin; #722 is an explicit warning against wiring it up).
  - **HOST-004** (pull-only compose, generated not hand-written): `docker-compose.hosted.yml` is `docker-compose.prod.yml` with every `build:` block replaced by a GHCR `image:` and **nothing else changed** — same `.env` contract, same ports, volumes, networks and security posture. Image selection is `${TRINITY_IMAGE_TAG:-latest}`, so an operator pins a release without editing the file. **The pin is resolved from `.env`** (shell/CI value → `.env` → `latest`): `.env` is the only place an `--unattended` or marketplace install can persist config, and exporting an unconditional default instead — as this first shipped — silently beat the operator's own line, because compose gives the shell environment precedence over `.env`. The summary then reported `Currently pinned to: latest` as though they had chosen it.
  - **HOST-005** (the two compose files are CI-guarded, never trusted): `tests/unit/test_2280_hosted_compose_parity.py` fails the build when hosted and prod disagree on the service set, any third-party image pin, the top-level volumes/networks, or **any** per-service value — the service comparison is **wholesale** (`prod` minus `{build, image}` vs `hosted` minus `{image}`), not a key allowlist. It began as an 18-key list that omitted `profiles`, `env_file`, `logging`, `labels`, `deploy` and `stop_grace_period`; `profiles` is live today on `cloudflared`, so a prod change that profile-gated or un-gated a service would have drifted silently past the guard whose entire purpose is catching that. An allowlist watches only the keys someone remembered, and the drift that matters is the key nobody thought of. Two compose files describing one platform is exactly the shape of a bug this repo has shipped five times and named — `LOG_*` (#1039), the VoIP master switch (#1056), `AGENT_AUTH_SECRET` (#1707), the container log caps (#1871), and `ADMIN_USERNAME` (#2381, present in dev compose and absent from prod, so `ADMIN_USERNAME=root` silently kept provisioning `admin`). The guard compares the **raw** YAML, not `docker compose config` output: the raw form still holds the unexpanded `${VAR:-default}` strings, so a changed default is a diff, where the resolved form would silently agree whenever the local `.env` happens to match.
  - **HOST-006** (one install path, two image sources): hosted mode is the `--hosted` flag on `scripts/deploy/start.sh` (or `TRINITY_HOSTED=1`), **not** a second script. Secret generation, the `ADMIN_PASSWORD` contract #2381 made honest, `DOCKER_GID` detection, the serving health poll and the next-steps card are identical and shared; only the image source differs. A parallel installer script would be the HOST-005 bug class one layer up.
  - **HOST-007** (the agent base image is pulled and retagged, never a compose service): the backend creates agent containers through the Docker SDK from the literal local tag `trinity-agent-base:latest` (hardcoded in `services/agent_service/lifecycle.py`, allowlisted as `trinity-agent-base:*` by SEC-172), and compose cannot retag — so `start.sh --hosted` runs `docker pull ghcr.io/abilityai/trinity-agent-base:${TRINITY_IMAGE_TAG}` followed by `docker tag … trinity-agent-base:latest` before bringing the stack up. Retagging rather than repointing the reference keeps SEC-172's allowlist and the #1809/#1816 image-drift check untouched: both read the container's own `Config.Image`, which stays `trinity-agent-base:latest` either way. **A bare `docker compose -f docker-compose.hosted.yml up -d` starts a platform that cannot create a single agent**, and the failure surfaces later as a missing-image error at agent-create time rather than at install time — so the upgrade instruction is "re-run `start.sh --hosted`", never "pull".
  - **HOST-008** (hosted-mode side effects are suppressed, not left to no-op silently): an explicit `-f` disables compose's override **auto**-merge, so the Docker Desktop Vector log-source fix (#1432) is appended to the file list **by name** — forcing it off under `--hosted` (as this first shipped) meant a hosted install on any VM-backed runtime shipped the very `docker_logs` source #1432 exists to avoid, and Vector busy-loops and pegs the Docker VM. The `vector` service is byte-identical between the dev and hosted files, so the override merges cleanly onto either; the #926 git-provenance export is skipped too, since there is no `build:` block to consume it and re-exporting the local checkout's git state would be a claim about a build this host did not perform. The closing summary prints hosted-appropriate commands (every one carrying the explicit `-f`) and a pin warning when `TRINITY_IMAGE_TAG` is `latest`.
  - **HOST-009** (a failed pull is fatal, never a silent fallback to building): hosted mode exits non-zero with a named cause if the agent base image cannot be pulled. Falling back to a local build would reintroduce the 5-10 minute first boot the mode exists to avoid, on the install least able to absorb it.
  - **HOST-013** (`FRONTEND_PORT` is honoured, in prod as well as hosted): both files bind `${FRONTEND_PORT:-80}:8080`. It was hardcoded `"80:8080"` in prod and inherited that way — while `start.sh` offers `FRONTEND_PORT` as the remedy for a port conflict and prints the resulting URL in its summary, so an operator who set it got a suggestion that changed nothing and an access URL pointing at a port nothing listened on. Fixed in prod too rather than only in hosted: it is the same inert knob, and diverging the two files would defeat HOST-005.
  - **HOST-014** (the tunnel actually starts): `cloudflared` is profile-gated (`profiles: ["tunnel"]`), so a non-empty `TUNNEL_TOKEN` alone starts nothing. `start.sh --hosted` reads the token (shell → `.env`) and appends `--profile tunnel` when it is set — unambiguous intent, honoured. Documenting the flag instead was rejected: HOST-010 names the tunnel as the *default* posture for a public instance, and a default that silently no-ops leaves the instance in exactly the plain-HTTP-on-a-public-IPv4 state HOST-010 says to avoid. Every other invocation still needs `--profile tunnel` / `COMPOSE_PROFILES=tunnel` passed by hand, which `.env.example` and `docs/DEPLOYMENT.md` now say.
  - **HOST-015** (a dev → hosted conversion in place is refused, not silently emptied): the two stacks share a compose project name but not a `/data` source — `docker-compose.yml` mounts the named volume `trinity-data`, hosted binds `${TRINITY_DATA_PATH:-./trinity-data}`. So `--hosted` in a checkout that has been running the dev stack would come up on an **empty** database and migrate from zero while the real one sat untouched in the volume — with Redis, a named volume both files share, **not** reset, i.e. a half-migrated install carrying live session and lock state pointing at rows that no longer exist. `start.sh --hosted` detects the combination (no `trinity.db` at the bind path **and** a `<project>_trinity-data` volume present) and **exits** with the copy command. Refuse rather than warn: the failure is silent, and by the time it is noticed the fresh DB may already have been written to. `TRINITY_DATA_PATH` is the documented escape for a deliberate fresh start.
  - **HOST-016** (the compose file is honest about not being standalone): it is pull-only in the sense that it does not **build** — it still needs the repository checked out beside it. The backend mounts four `./config/*` directories, Vector and the OTel collector mount their configs, and `/data` is a relative bind mount; handed to a catalogue without the repo, Docker creates every one of those as an empty directory, so the local template catalog is empty and the ent#124 first-run seed finds no manifests — with no error at install time. The header states this rather than implying otherwise; packaging the config into the images so the file can travel alone is a follow-up.
  - **HOST-017** (the publish verification step must be able to run at all): the anonymous-pull check builds its reference in the shell body and **lowercases it**. This owner is literally `Abilityai` and a GHCR repository name must be lowercase, so docker rejects the mixed-case reference LOCALLY, before any network call (`repository name must be lowercase`) — the step would burn its five retries and fail on EVERY publish, reporting a perfectly public package as private and destroying the one signal it exists to give, while training everyone to ignore the warning that matters. The push itself is unaffected (`docker/metadata-action` lowercases its `images:` input; `start.sh` hardcodes `ghcr.io/abilityai/`); a hand-written reference has to do it itself.
  - **HOST-018** (`stop.sh` is hosted-aware, and stops rather than destroys): hosted mode passes an explicit `-f`, which disables compose's file auto-merge — so a bare `docker compose` in the checkout loads the DEV file. Same project name, so it acts on the same containers, but it knows nothing about `cloudflared`, which the dev file does not define: the tunnel keeps running and the instance stays **publicly reachable** after the script prints "All services stopped" — the same hazard HOST-014's `COMPOSE_PROFILES` persistence closes, re-entering through the one entry point that change did not touch. `stop.sh` reads the running stack's own `com.docker.compose.project.config_files` label (compose's record of the files that created the project, which cannot drift the way a marker file or a `trinity.db`-at-the-bind-path heuristic can) and adds the hosted `-f` when it names the hosted file; missing container or label degrades to the dev default. It also runs `stop`, not `down`: `down` removes the platform containers and tears down `trinity-agent-network`, which every agent container is attached to, and is the command `start.sh`'s own closing summary tells operators NOT to run in **both** branches — a script named `stop.sh` running the forbidden verb was a standing contradiction.
  - **HOST-010** (TLS on a bare VM — decided and documented): Trinity serves plain HTTP and terminates TLS **outside** the application; there is no HTTPS listener in any compose file and no auto-certificate step. Three supported postures, documented in `docs/DEPLOYMENT.md`: a **tunnel** (Cloudflare Tunnel — `cloudflared` is already a service, set `TUNNEL_TOKEN`; the default for a public instance, nothing to renew), a **private network** (Tailscale/WireGuard/VPC — what the managed fleet runs; HTTP over a WireGuard tunnel is encrypted transport and a finished posture, not a compromise), or an **operator-run reverse proxy** (Caddy/nginx + Let's Encrypt). Plain HTTP on a public IPv4 with none of the three is the one combination called out as unsafe. A marketplace droplet is the deliberate exception — it comes up on a bare public IP with no domain, so that channel provisions a Caddy sidecar with Let's Encrypt short-lived IP certificates (#2281) and Trinity shows a provenance-gated first-run hardening guide (#2380) prompting for a real domain or a VPN.
  - **HOST-011** (8 GB minimum, stated where the hosted path is documented): in `docker-compose.hosted.yml`'s own header, `docs/DEPLOYMENT.md` and `docs/AGENT_INSTALL_GUIDE.md` — and pinned by the parity test, so the floor cannot quietly drop out of the compose file. Below 8 GB the agent containers and platform services contend and turns start failing under load.
  - **HOST-012** (pull-only is the documented default *for servers*): `docs/DEPLOYMENT.md` gains a server-install section leading with `--hosted`, and `docs/AGENT_INSTALL_GUIDE.md` puts the hosted invocation first in Step 1 with the source build demoted to "a dev box, or a server with no registry access". Both instruct pinning `TRINITY_IMAGE_TAG` rather than riding `latest`.
- **Outstanding (AC4)**: the cloud-init user-data example (`trinity-ops-public/provision/cloud-init.sh`) taking admin password / domain / optional Cloudflare Tunnel token. Cross-repo, so it does not land here; #2280 stays open until it does.
- **Explicit non-goals (flagged, not filed)**: arm64/multi-arch publishing (amd64 covers DigitalOcean Droplets and Vultr Cloud Compute; cross-building the ~1.9 GB agent base under QEMU is slow and flaky — gate it on Hetzner CAX / Umbrel actually needing it), image signing/attestation, and making the agent base image reference configurable (rejected: it would require widening the SEC-172 allowlist, and retagging achieves the same result with no security surface).
- **Tests**: `tests/unit/test_2280_hosted_compose_parity.py`, `tests/unit/test_2280_publish_workflow_and_stop.py`, `tests/unit/test_2390_start_sh_env_and_project_name.py`
- **Docs**: `docs/DEPLOYMENT.md` (server install + TLS), `docs/AGENT_INSTALL_GUIDE.md` (Step 1)
- **Flow**: `docs/memory/feature-flows/hosted-install.md`

---

## 12. Platform Operations

### 12.1 Internal System Agent
- **Status**: ✅ Implemented (2025-12-20)
- **Description**: Auto-deployed platform orchestrator (`trinity-system`)
- **Key Features**: Deletion-protected, system-scoped MCP key, permission bypass, ops commands
- **Flow**: `docs/memory/feature-flows/internal-system-agent.md`

### 12.2 System Agent Operations Scope
- **Status**: ✅ Implemented (2025-12-20)
- **Description**: Fleet ops, health monitoring, schedule control, emergency stop
- **Key Features**: `/ops/*` slash commands, configurable thresholds
- **Guiding Principle**: "The system agent manages the orchestra, not the music."

### 12.3 Web Terminal for System Agent
- **Status**: ✅ Implemented (2025-12-25)
- **Description**: Admin-only browser terminal for System Agent
- **Flow**: `docs/memory/feature-flows/web-terminal.md`

### 12.4 System Agent UI Page
- **Status**: ✅ Implemented (2025-12-20)
- **Description**: Admin-only `/system-agent` page with fleet overview and operations console
- **Key Features**: Fleet cards, Emergency Stop, Restart All, Pause/Resume Schedules
- **Flow**: `docs/memory/feature-flows/system-agent-ui.md`

### 12.5 OpenTelemetry Integration
- **Status**: ✅ Implemented (2025-12-20, extended 2026-04-14)
- **Description**: OTel metrics export from Claude Code agents + backend distributed tracing
- **Key Features**: Cost, tokens, productivity metrics in Dashboard; trace_id in logs for multi-agent request correlation (RELIABILITY-002)
- **Flow**: `docs/memory/feature-flows/opentelemetry-integration.md`

### 12.6 System-Wide Trinity Prompt
- **Status**: ✅ Implemented (2025-12-14, refactored 2026-03-15 Issue #136)
- **Description**: Admin-configurable prompt injected at runtime via `--append-system-prompt` on every Claude Code invocation
- **Flow**: `docs/memory/feature-flows/system-wide-trinity-prompt.md`

### 12.6.1 Execution Context Injection (#171)
- **Status**: ✅ Implemented (2026-04-14)
- **Description**: Dynamic per-invocation `## Execution Context` block appended to every agent system prompt so agents can self-calibrate. Carries mode (chat vs autonomous task), trigger source, model, timeout budget, own name, permitted collaborators, schedule metadata, and timestamp.
- **Key Features**:
  - Single composition seam (`platform_prompt_service.compose_system_prompt`) for all invocation paths (chat / task / schedule / mcp / agent-to-agent / fan-out / paid / public)
  - Behavioral guidance per mode: chat mode permits clarifying questions; task mode enforces execute-to-completion
  - User-controlled metadata (schedule name, MCP key name) sanitized before rendering — strips control chars, backticks, and markdown heading markers, caps length — to prevent prompt-injection via metadata fields
  - Builder failures never fail a request: always falls back to the base platform prompt
  - Operator kill-switch via `trinity_execution_context_enabled` setting (default enabled)
- **Flow**: `docs/memory/feature-flows/execution-context-injection.md`

### 12.7 Vector Memory
- **Status**: ❌ Removed (2025-12-24)
- **Reason**: Templates should define their own memory. Platform should not inject agent capabilities.

### 12.8 Agent Monitoring Service (MON-001)
- **Status**: ✅ Implemented (2026-02-23)
- **Requirement ID**: MON-001
- **Description**: Multi-layer health monitoring for agent fleet with real-time alerts
- **Key Features**:
  - Docker layer: Container status, CPU/memory, restart count, OOM detection
  - Network layer: Agent HTTP reachability with latency tracking
  - Business layer: Runtime availability, context usage, error rates
  - Real-time WebSocket updates for health state changes
  - Alert cooldowns to prevent notification spam
  - Fleet dashboard with health summary (admin-only)
  - 3 MCP tools: `get_fleet_health`, `get_agent_health`, `trigger_health_check`
- **Status Levels**: healthy → degraded → unhealthy → critical → unknown
- **Flow**: `docs/memory/feature-flows/agent-monitoring.md`

### 12.8a Richer Agent `/health` Signal (#1020)
- **Status**: ✅ Implemented (2026-06-02)
- **GitHub Issue**: #1020
- **Description**: Promote the agent container's `/health` from `{status}` + ad-hoc diagnostics to a named, contractual signal the platform acts on — an incremental step toward `TARGET_ARCHITECTURE.md` §Agent Runtime.
- **Key Features**:
  - New top-level fields: `active_tasks` (concurrent executions across `/api/chat` + `/api/task`), `last_task_at` (ISO), `consecutive_failures` (reset on success, incremented on failure).
  - Counters tracked in `agent_server/state.py` (`record_task_start`/`record_task_finish`), wired at both execution chokepoints in `agent_server/routers/chat.py`. Thread-safe (concurrent tasks).
  - `consecutive_failures` is the signal the dispatch circuit breaker (#526) consumes; `last_task_at` powers liveness; both feed the heartbeat push (#307).
  - Backend `monitoring_service.py` reads `consecutive_failures`/`last_task_at` into `BusinessHealthCheck` (graceful `None` default for pre-#1020 agent images).
  - `mailbox_depth` intentionally NOT emitted — no agent-side mailbox until the actor model (#945); backend derives queue depth from `CapacityManager`.
  - Back-compat: existing `/health` keys unchanged; new keys additive.

### 12.9 Cleanup Service for Stuck Resources
- **Status**: ✅ Implemented (Updated 2026-08-28, Issue #2433)
- **Requirement ID**: CLEANUP-001
- **GitHub Issue**: #94, #129, #2433
- **Description**: Background service that automatically recovers stuck intermediate states via active watchdog reconciliation and passive stale detection
- **Key Features**:
  - **Active watchdog** (Issue #129): Reconciles DB execution state against agent process registries every 5 minutes
  - Orphan recovery: Executions marked "running" in DB but not found on agent are marked failed with descriptive error
  - **Proof-of-life is two-sided (#2433)**: an admitted execution (row `running`, slot held) is an
    orphan only when the agent does not know it **and** no live backend dispatcher owns it. The
    agent side reports `executions` ∪ `recently_completed_ids` ∪ `pending_ids` (accepted at
    `/api/task` / `/api/chat` / the async spawn but not yet spawned); the backend side is the
    `agent_call_limiter` in-flight registry (in-process, exact) plus a cross-worker Redis marker
    `execution:inflight:{execution_id}` refreshed by one per-process task (60s TTL / 15s tick —
    liveness, not state: a dead worker's marker lapses and the row is recovered as before, so the
    #408 dead-coroutine class is unchanged). Read tri-state per sweep (one `MGET`): `alive` →
    withhold; `unknown` (Redis unreadable) → withhold while a dispatcher could still own the row
    (bounded by `inflight_max_age_seconds()`), never fail-open; `absent` → orphan. Applied at the
    periodic watchdog, the Phase-3 slot re-verify and the startup recovery. Withheld rows are
    counted in `CleanupReport.dispatch_inflight_skipped` (observability, not a recovery) and logged
    once per agent per cycle
  - A parked call is **re-anchored at dispatch** (#2433): a park of ≥5s in the backend agent-call
    queue re-stamps `started_at` (the admission instant is kept in `queued_at`, the drained-backlog
    shape) and renews the capacity-slot lease at grant — and the refresher renews the slot every
    tick while parked — so a park never spends the run's own budget through the registry-blind
    stale sweep, the slot TTL, canary E-01 or `duration_ms`
  - A parked execution is **cancellable** (#2433): `terminate` flags the owning dispatcher (this
    worker, or via `execution:cancel:{execution_id}` the other) so the grant refuses to POST, and
    finalizes CANCELLED with the #679 shape; a cancel requested while the agent still had the run
    pending is consumed at spawn (`register()` kills the group and keeps the cancel marker)
  - The orphan error string states what was **observed** (#2433): which agent-side sets were checked
    (`not pending` only when the image reports `pending_ids`) and that no live dispatcher owned the
    row — never "completed on agent" for a row the agent never received
  - Auto-terminate: Executions confirmed running on agent but exceeding `timeout_seconds` are terminated via agent API
  - Race-condition guard: Conditional DB update (`WHERE status='running'`) prevents overwriting normal completions
  - Capacity/queue release: Slots and queue state released on recovery; atomic Lua-script queue release prevents TOCTOU
  - WebSocket broadcast: Frontend notified of watchdog recovery actions
  - Dispatch grace period: 60s grace for newly created executions before orphan detection
  - Systemic failure detection: Warns if >50% of recovery attempts fail in a single cycle
  - **Passive stale cleanup**: Marks stale executions (`status='running'` > 120 min) as `failed`
  - Marks stale activities (`activity_state='started'` > 120 min) as `failed` — a **backstop for
    the unclaimed only** (#1804): every writer that wins a terminal CAS now closes the paired
    dispatch activity itself (§10.15 in `scheduling.md`), so a row reaching this sweep means a
    producer is unowned. Runs **after** `_sweep_stale_slots` in the cycle (it used to run one line
    before the stale-slot reaper, so within a single cycle the 120-minute duration fabricator could
    beat a legitimate closer).
  - Recovery paths (watchdog `_recover_execution`, startup recovery, the two bulk sweeps via
    `_close_bulk_swept_activities`, the lease reaper, both backend-shutdown `CancelledError`
    handlers) close their execution's activity on the CAS-won branch — counted in
    `CleanupReport.activities_closed_on_recovery` (#1804)
  - Cleans up stale Redis slots (entries older than TTL)
  - One-shot startup sweep on backend restart
  - Periodic cleanup every 5 minutes
  - Admin-only status endpoint: `GET /api/monitoring/cleanup-status`
  - Admin-only trigger endpoint: `POST /api/monitoring/cleanup-trigger`
- **Constants**: Interval 300s, execution timeout 120min, activity timeout 120min, watchdog HTTP timeout 5s, dispatch grace 60s; in-flight marker TTL 60s / tick 15s / re-stamp threshold 5s (#2433)

### 12.10 Execution & Health-Check Retention (Issue #772)
- **Status**: ✅ Implemented (2026-05-11, Issue #772)
- **Requirement ID**: RETENTION-001
- **GitHub Issue**: #772
- **Description**: Bounded growth for `schedule_executions` (driven by per-run JSONL transcripts in `execution_log`, ~150–190 KB/row) and `agent_health_checks` so active fleets don't hit disk pressure within weeks. Production observation pre-fix: ~3.3 GB / ~9k rows on `schedule_executions` and ~200 MB / ~750k rows on `agent_health_checks`.
- **Key Features**:
  - **Two-stage retention on `schedule_executions`**: nulling `execution_log` past `execution_log_retention_days` preserves row + metadata (agent, status, cost, duration) for audit; full row DELETE past `execution_row_retention_days` for deeper retention.
  - **Per-cycle row budget**: each sweep caps at 5000 rows per 5-min cleanup tick so the first post-deploy backfill spans hours rather than holding a multi-minute write lock.
  - **Chunked SQL**: prune methods iterate `SELECT id ... LIMIT N` → `DELETE/UPDATE id IN (...)`, committing per chunk (avoids `SQLITE_ENABLE_UPDATE_DELETE_LIMIT` dependency).
  - **`iso_cutoff()` cutoffs**: time-window comparisons against ISO-Z TEXT columns use the helper from `utils/helpers.py`, per Architectural Invariant #16.
  - **Partial index** `idx_executions_completed_terminal ON schedule_executions(completed_at) WHERE status IN ('completed','failed','terminated')` drives both sweeps via index range scan.
  - **WAL checkpoint** after each cycle that reclaims rows (`PRAGMA wal_checkpoint(TRUNCATE)`).
  - **Daily VACUUM** via `db_vacuum_service.py` (APScheduler, 04:30 UTC, autocommit connection) for last-mile page reclaim.
  - **Admin-configurable** via `GET/PUT /api/settings/ops/config` using new ops keys: `execution_log_retention_days` (default 30), `execution_row_retention_days` (default 90), `health_check_retention_days` (default 7). `0` disables that sweep.
  - **Backward-compatible**: existing `cleanup_old_records()` (agent_health_checks) is reused with added `chunk_size` parameter; previously orphaned (not invoked from any tick), now wired into the cleanup service.
- **Constants**: Cleanup tick 300s, per-cycle row budget 5000, vacuum cron 04:30 UTC.

### 12.11 Terminal `backlog_metadata` PII Scrub (Issue #1449)
- **Status**: ✅ Implemented (2026-07-17, Issue #1449)
- **Requirement ID**: RETENTION-002
- **GitHub Issue**: #1449
- **Description**: `services/backlog_service.py::enqueue` `json.dumps`es the full drain-replay request — including `user_message`, `user_email`, and `system_prompt` — into `schedule_executions.backlog_metadata` so a queued task can be reconstructed at drain. That blob is read **only while `status='queued'`** (the backlog drain claims only queued rows; the #1083/#1081 result callbacks read the POST payload, not the row's metadata; canary E-04/G-04 are queued-scoped). On a **terminal** row it is stale PII sitting in the DB indefinitely, bounded only by the 90-day `execution_row_retention_days` DELETE. The scrub NULLs it as soon as the row reaches an authoritative terminal.
- **Key Features**:
  - **`db.scrub_terminal_backlog_metadata(chunk_size)`** — chunked `SELECT id ... LIMIT N` → `UPDATE ... SET backlog_metadata=NULL WHERE id IN (...)`, each chunk its own transaction (short write lock), mirroring `prune_execution_logs`.
  - **Authoritative terminals only** — `status IN ('success','cancelled','skipped')` (the `_AUTHORITATIVE_TERMINALS` set). **FAILED is deliberately EXCLUDED**: a FAILED row is resurrectable to SUCCESS via a late token-gated CAS (`park_expired_lease` keeps its `claim_token`), so its drain-replay intent must survive; FAILED PII stays bounded by the 90-day `prune_execution_rows`.
  - **Not age-gated, not operator-configurable** — the scrub is a **security invariant**, not a retention window. It runs unconditionally every cleanup tick (even when every #772 window is `0`) and has **no ops-settings key** — a fixed default sidesteps the #1638 floor-by-seed trap.
  - **Count-only logging** — the scrubbed count feeds the sweep report + the `_maybe_wal_checkpoint` sum (a scrub-only cycle still truncates the WAL); the `backlog_metadata` blob itself is **never** logged (it carries PII).
- **Location**: `services/cleanup_service.py::_sweep_retention_772` (sub-sweep), `db/schedules.py::scrub_terminal_backlog_metadata`.
- **No schema change, no migration, no new service.**
- **Deferred sibling (not in this change)**: callback/pull-path chat-session persistence (the other #1444 carve-out) is deferred to the pull single-applier work (#1081) — it must land WITH the FAILED-exclusion already shipped here.

---

## 30. CLI Tool (CLI-001)

### 30.1 CLI Package
- **Status**: 🚧 In Progress
- **Description**: Python Click CLI (`trinity`) that provides shell-level access to the platform
- **Key Features**: `pip install -e src/cli/`, mirrors core MCP tools as shell commands, JSON and table output
- **Location**: `src/cli/`

### 30.2 CLI Authentication (CLI-002)
- **Status**: ✅ Implemented
- **Description**: Email-based login flow for CLI users
- **Key Features**: `trinity init` (onboarding), `trinity login` (email + code), `trinity logout`, `trinity status`, config stored in `~/.trinity/config.json`
- **API**: `POST /api/access/request` (auto-approve whitelist), reuses `/api/auth/email/request` + `/api/auth/email/verify`

### 30.3 CLI Agent Operations (CLI-003)
- **Status**: ✅ Implemented
- **Description**: Core agent management commands
- **Key Features**: `trinity agents list|get|create|delete|start|stop|rename`, `trinity chat`, `trinity logs`, `trinity health`, `trinity skills`, `trinity schedules`, `trinity tags`

### 30.4 CLI Output Formatting (CLI-004)
- **Status**: ✅ Implemented
- **Description**: `--format json` (default, for scripting) and `--format table` (human-readable via Rich)

### 30.5 CLI Multi-Instance Profiles (CLI-005)
- **Status**: 🚧 In Progress
- **Description**: Named profiles for managing multiple Trinity instances (local, staging, production) from a single CLI installation
- **Key Features**: `trinity profile list|use|remove`, `--profile` global flag, `TRINITY_PROFILE` env var, legacy flat config auto-migration to `default` profile
- **Location**: `src/cli/trinity_cli/config.py`, `src/cli/trinity_cli/commands/profiles.py`

### 30.6 CLI Deploy Command (CLI-006)
- **Status**: ✅ Implemented
- **Description**: Deploy local agent directories to Trinity with `trinity deploy .`
- **Key Features**: Tar+base64 archive, POST to `/api/agents/deploy-local`, `.trinity-remote.yaml` tracking for idempotent redeploys, `--name` override, `--repo` for GitHub-based deploy, `.gitignore`-aware archiving, instance mismatch warning on redeploy
- **Location**: `src/cli/trinity_cli/commands/deploy.py`
- **Tracking file**: `.trinity-remote.yaml` (auto-added to `.gitignore`)

### 30.7 CLI MCP Key Auto-Provisioning (CLI-007)
- **Status**: ✅ Implemented
- **Description**: After `trinity init` or `trinity login`, automatically provision an MCP API key and store it in the profile
- **Key Features**: Calls `POST /api/mcp/keys/ensure-default`, stores `mcp_api_key` in profile, `trinity init` also writes `.mcp.json` with Trinity MCP server config
- **Location**: `src/cli/trinity_cli/commands/auth.py`

### 30.8 Agent Quota Enforcement (QUOTA-001)
- **Status**: ✅ Implemented
- **Description**: Per-role agent creation limits with admin exemption. Configurable per role via Settings UI.
- **Key Features**: Admin users exempt (unlimited), per-role defaults (creator=10, operator=3, user=1), configurable via `GET/PUT /api/settings/agent-quotas`, legacy `max_agents_per_user` fallback, system agents excluded from count, redeploys bypass quota, 429 response includes current/limit counts
- **Location**: `src/backend/services/settings_service.py` (`get_agent_quota_for_role`), `src/backend/services/agent_service/crud.py`, `src/backend/services/agent_service/deploy.py`, `src/backend/routers/settings.py`, `src/frontend/src/views/Settings.vue`

---

## 31. Canary Invariant Harness (CANARY-001)

### 31.1 Continuous Orchestration-Invariant Watcher (CANARY-001 — Phase 1)
- **Implements**: Issue #411 — first three invariants (S-01, E-02, L-03)
- **Description**: Background watcher service that runs deterministic
  orchestration-invariant checks against live platform state every 5
  minutes. Persists violations to a queryable table and classifies
  green→red transitions for an external alert sink. Catches the bug
  class behind PRs #378, #403, #129, #226 — race conditions and
  cross-component state drift that unit tests miss.
- **Architecture**: deterministic Python library (`src/backend/canary/`)
  shared between the watcher service (`services/canary_service.py`) and
  the on-demand admin endpoint (`POST /api/canary/run-cycle`). Library
  reads state but writes nothing; service writes violations and
  classifies transitions.
- **Phase 1 invariants**:
  - **S-01** Slot–row bijection (Redis ZRANGE vs SQL running rows, drain
    sentinels filtered)
  - **E-02** No phantom reversal (terminal executions stay terminal,
    detected via Redis-backed state comparison)
  - **L-03** Delete cascades (no orphan rows referencing removed agents
    in any cross-cutting table; no orphan Redis slot keys)
- **Storage**: `canary_violations` table; observed_state JSON column.
- **Activation**: gated by `CANARY_ENABLED=1` env var; disabled by
  default. Production deployment is staging/dev — the harness watches
  there, not in user-facing prod. `CANARY_ENABLED` and
  `CANARY_SLACK_WEBHOOK_URL` must be forwarded under `backend.environment:`
  in **both** `docker-compose.yml` and `docker-compose.prod.yml` (#1881
  part 1, shipped as #1876): prod compose launches standalone — no base-compose merge and no
  `env_file:` — so the explicit `environment:` list is the only path into
  the container, and the vars were wired into the dev file only. Staging/dev
  runs prod compose, so the harness was un-enableable on exactly the
  deployment it exists for: a documented `.env` lever that silently did
  nothing (#1039/#1056 packaging-gap class), and a silent-green one level
  above H-01 that no invariant can catch, since invariants only run inside
  the thing that isn't running. Pinned by
  `tests/unit/test_canary_env_prod_parity.py`.
- **Single-cycling-worker lease** (#1881 part 2): the FastAPI lifespan
  starts `canary_service` in **every** uvicorn worker (prod runs
  `--workers 2`) and the service held only a per-process `asyncio.Lock`,
  which guards re-entrancy inside one process and says nothing about
  cross-worker exclusion. Enabling the harness therefore meant two full
  cycles per interval — R-01 `docker exec`ing into every running agent
  container twice per 5 min, violations double-persisted (11,942
  `canary_violations` rows in 24h, measured on eu2), and two independent
  writers on every shared marker (`canary:last_cycle_at`,
  `canary:last_cycle_red`, `canary:e02:terminal_seen`,
  `canary:h01:suspect_since`). The two defects had to ship together: the
  compose fix alone converts a dormant bug into a live one. The scheduled
  loop now runs only when it holds the Redis `canary:leader` lease — SET
  NX, TTL `max(3×interval, 900s)`, own-lease-only refresh, best-effort
  release on `stop()` — mirroring `monitoring:leader` (#1464) and
  `opqueue:leader` (#1632). Every worker still runs its loop and re-checks
  each cycle, so leadership fails over when the holder dies with no
  restart; non-leaders log on the **transition** only, never per cycle.
  - **TTL floor**, the one deviation from `interval × 3`: a canary cycle's
    cost is dominated by R-01's `container.exec_run` sweep, which is bounded
    by no timeout and scales with *fleet size*, not with how often we look.
    A shortened interval must not shorten the lease below one sweep, or it
    lapses mid-cycle and leadership flaps — restoring the concurrent
    probing the lease exists to remove. The floor is a no-op at the default
    300s interval.
  - **Fail-open to leader** when Redis is unreachable. The precedents' own
    justification does not transfer — a duplicate canary cycle is *not*
    inert (it re-runs the sweep and double-persists rows) — so it is taken
    on different grounds: this is the one subsystem whose purpose is
    noticing that something went quiet, and a canary that stops is the
    silent-green failure H-01 exists to catch, one level up where nothing
    can see it. Duplicated probes are noisy and visible; silence is not.
    A Redis outage is also already a degraded state the harness announces
    (`sources_unavailable`; H-01 fires unconfirmed on an unreadable
    marker), and failing closed would suppress precisely those paths.
  - Consequently the lease is **best-effort, not mutual exclusion**:
    H-01's `CONFIRMATION_MIN_SECONDS` and R-01's `DWELL_SECONDS`
    elapsed-wall-clock gates stay load-bearing and must not be relaxed to
    "seen in a second cycle" on the strength of it. Both also ride out a
    real-time transient, which is a single-worker property.
  - Knock-on: a leader failover leaves up to ~1200s (TTL + interval) with
    nobody cycling, which exceeds R-01's `_MAX_OBSERVATION_GAP_SECONDS`
    (600) and restarts its dwell. Correct — a crashed leader is a genuine
    observation outage, and restarting is the fail-safe direction.
  - `run_cycle()` is deliberately **not** gated: `POST /api/canary/run-cycle`
    lands on an arbitrary worker, so gating it would make an explicit admin
    request return an empty payload roughly half the time under
    `--workers 2` — structurally identical to a green cycle, the exact
    ambiguity the 409 `"cycle in progress"` contract exists to remove.
  - Guard: `tests/unit/test_1881_canary_leader_lease.py`.
- **Fleet**: `config/canary-fleet.yaml` deploys two synthetic agents
  (`canary-fleet-burst` minute-cron, `canary-fleet-long` 5-min cron) via
  the existing `/api/systems/deploy` endpoint. Without the fleet, the
  watcher reports trivially-green cycles with no signal.
- **Alert sink**: Slack via incoming webhook URL configured by the
  `CANARY_SLACK_WEBHOOK_URL` env var (admin-side, no Settings UI — the
  audience is operators with shell access on staging/dev). Each
  green→red transition fires exactly one webhook POST with a Block Kit
  payload (severity emoji header, rendered violation summary, context
  line with snapshot_time + violation count + "last red Xm ago"
  badge). Unset = silent sink: cycles still run, violations still
  persist to `canary_violations`, only the outbound POST is skipped.
  Continuing-red invariants don't re-post — **except to complete an
  alert that was never delivered** (#1897). The dashboard-notifications
  path (writing `agent_notifications` rows via `db.create_notification`)
  was rejected on the product call.
  **Delivery is an outcome, not an assumption (#1897).**
  `emit_transition` reports `DELIVERED` / `SKIPPED` (no webhook
  configured — deliberately *not* a failure, or every default install
  would arm a retry per transition) / `FAILED`, and only a non-FAILED
  outcome counts the transition in `cumulative_transitions` or lists it
  under `transitions` in the run-cycle response; the undelivered set is
  surfaced beside it as `undelivered_invariant_ids`, so a webhook outage
  cannot make an admin `POST /api/canary/run-cycle` look like a green
  cycle. An undelivered transition is **re-attempted on a later cycle
  while the invariant is still red**, at most once per cycle interval
  (a floor, because `run_cycle()` is deliberately not leader-gated and
  manual polling would otherwise spend the whole window in seconds), for
  up to `MAX_ALERT_PENDING_AGE_SECONDS` (1800s, a module constant per
  the #1644 `MAX_ROWS_PER_SWEEP` precedent) per *contiguous failure run*
  — failures separated by more than 3× the interval start a fresh run,
  so brief flaps cannot consume the window a later long outage needs.
  Past the window a distinct ERROR names the invariant, the elapsed
  seconds and the last webhook error, and `cumulative_alerts_dropped`
  increments; `cumulative_transitions_detected` keeps counting flips so
  no single counter has to mean both "detected" and "delivered".
  **The exact bound, because "up to 1800s" reads tighter than it is:**
  the budget is evaluated AFTER each attempt (so the ERROR quotes the
  elapsed and error of the attempt that actually just failed, not a
  stale one), which means a run ends on the first attempt whose age
  *exceeds* the window rather than the last one inside it — at the
  5-minute default, **8 POSTs spanning 2100s (35 min)** per run, then
  silence. The **dual of the run-decay**, stated so it is not
  rediscovered as a bug: an invariant flapping red→green→red on a
  period longer than 3× the interval never accumulates run age and so
  never reaches a give-up — but it also never *retries* (it is green
  again before the floor opens), so it costs exactly one POST per red
  episode, which is the detection rate and is precisely the pre-#1897
  behaviour. A delivery-layer budget deliberately does not bound its
  own detector.
  The retried payload is always rebuilt from the CURRENT cycle's
  violations, and a pending entry only acts on a cycle where its
  invariant is red, so a retry is never stale content and never fires
  for something that went green. Retry state is **per-invariant**, in
  the Redis hash `canary:alert_pending` (field = invariant id),
  deliberately independent of the cycle-global `canary:last_cycle_at`
  cursor: withholding that cursor retries nothing (the invariant's own
  freshly-inserted row already post-dates it) and silently swallows an
  unrelated red→green→red flip instead. The entry is armed BEFORE the
  POST and `HDEL`'d on success, not armed on failure, because
  `asyncio.CancelledError` is not an `Exception` and `stop()` cancels a
  live cycle — a SIGTERM landing inside the webhook await would
  otherwise lose the alert on every deploy that coincides with a red
  cycle. Everything fails open: an unreadable or unwritable pending
  store degrades to exactly the pre-#1897 behaviour, never worse, and
  the *evidence* is never at risk because it lives in
  `canary_violations` (SQL) rather than in Redis. The alternative of an
  `alert_state` column on `canary_violations` — delivery state in the
  same failure domain as the evidence, queryable via
  `GET /api/canary/violations` — was rejected on scope (a dual-track
  SQLite + Alembic migration for a delivery bug), not on merit. Two
  workers can both retry one pending entry, which costs at most one
  duplicate message; #1881's posture on this same subsystem ("choose the
  duplicate over the silence") decides it. Guard:
  `tests/unit/test_1897_canary_alert_delivery.py` — under `tests/unit/`
  because no CI workflow runs the canary suite itself (#2037).
- **Instance attribution (#1987)**: the payload names the instance that
  fired it — a `[eu2]` prefix on both the Block Kit header and the `text`
  fallback — so instances sharing one webhook (as `dev` and `eu2` do since
  the #1766 soak) stay tellable apart. A webhook carries no sender
  identity, and continuing-red gating makes each alert a one-shot, so
  anything the message omits is not recoverable from a later one.
  `services/instance_identity.py::get_instance_label()` resolves it:
  optional `TRINITY_INSTANCE_NAME` override → first DNS label of
  `FRONTEND_URL`'s host (`https://eu2.abilityai.dev` → `eu2`; an IP
  literal keeps its whole host) → `installation_id[:8]` → unlabelled.
  Deliberately no new *required* var: managed instances already carry
  `FRONTEND_URL`, so attribution improves fleet-wide without an `.env`
  rollout. Every tier degrades instead of raising — an unlabelled alert
  is the prior behaviour, a lost alert is the failure the sink exists to
  prevent. The label is sanitized (ASCII-alnum + hostname punctuation,
  32-char cap) at resolution *and* at the render boundary, so it can
  neither forge Slack markup (`<!channel>`) nor overflow the 150-char
  header cap Slack rejects the whole message on.
- **Determinism**: invariant checks are pure functions
  `check(snapshot) → list[ViolationReport]`. Same snapshot input always
  yields the same output. No LLM reasoning anywhere in the canary path.
- **Phase 2 / 3 (shipped, #882)**: S-02, E-01, E-05, B-01 (Phase 2) and
  S-03, B-02, R-01 (Phase 3). E-06 shipped separately (#1472).
- **Phase 4 (shipped, #1077)**: four pure single-table predicates over
  `schedule_executions`, no new source types. E-03 (completed rows populated —
  `completed_at IS NOT NULL`, `completed_at`-only predicate) and G-03 (clock
  sanity — `started_at ≤ completed_at`, ~1s tolerance, UTC-aware parse) ride a
  shared terminal-row collector (`_collect_terminal_rows`, windowed on
  `started_at`, `LIMIT 5000`). E-04 (queued-row metadata integrity —
  `queued_at NOT NULL` AND `backlog_metadata` non-NULL + JSON-parseable) and
  G-04 (no raw credentials in `backlog_metadata` — secret-prefix regex scan)
  ride the queued-row metadata `_collect_executions` captures, scoped strictly
  to `status='queued'` rows (so #1449's deferred terminal-row NULL-out can't
  false-fire). E-04/G-04 are stacked on #1450's queued-read rework and land
  after it. **Credential safety:** E-04/G-04 violations persist to
  `canary_violations`, so neither ever echoes the raw `backlog_metadata` — E-04
  reports the failed-predicate reason code, G-04 the matched pattern name only.
- **Phase 5 (shipped, #1813)**: **H-01 collector blindness** — the harness's
  first *self*-check, and the reason the `H-` (harness health) id family exists:
  every other invariant means "the system is broken", H-01 means "the observer
  is blind", and an H-01 violation invalidates every other green in that cycle.
  #1540 repointed the SQL-tier collectors onto the configured engine but left
  the failure *shape* untouched — a collector reading an empty or unreachable
  source returns zero rows, which is indistinguishable from a genuinely clean
  fleet, so both report green. H-01 fires when the roster read
  (`_collect_known_agents`) returns zero rows or raises **while an independent,
  non-SQL source proves the fleet is alive**: Docker container presence
  (`docker_agent_names`, read from the container list *before* any `exec_run`,
  since `zombie_counts` is keyed by exec success and thins on a degraded
  container) ∪ Redis slot keys (`orphan_redis_slots`, corroborating only — slot
  keys exist solely while an execution holds a slot). Docker is collected
  **before** the roster read, so it still supplies evidence on the arm where
  that read raises and the collector returns early; Redis needs `known_agents`
  and cannot. Reason codes
  (stable — trinity-enterprise#202 scores on them): `roster_read_failed` /
  `roster_empty_contradicted` (critical) / `roster_empty_unverifiable` (major —
  the evidence source was unreachable, was never read, or **only Redis** had
  anything to say: `orphan_redis_slots` is by definition slot keys whose agent
  is absent from `agent_ownership`, i.e. L-03's leaked-slot state, so treating
  it as a contradiction would page critical over a correct roster plus an
  unrelated leak). `docker_available`/`redis_available` are **tri-state**
  (`None` = the collector never ran) because `sources_unavailable` cannot
  express a skipped collector. **Confirmation on elapsed
  wall-clock** (`CONFIRMATION_MIN_SECONDS`, marker `canary:h01:suspect_since`,
  E-02's cross-cycle-state precedent) so the last-agent delete race — DB row
  gone, container still tearing down — cannot false-fire. Deliberately NOT "a
  second cycle": prod runs `--workers 2` and, when the gate was written,
  `canary_service` held no leader lease, so both loops shared the marker and
  worker B would confirm worker A's sighting seconds later, collapsing the
  gate. The #1881 `canary:leader` lease does not retire the rule — it fails
  open to leader on a Redis outage, restoring concurrent loops over the shared
  marker, and the transient being ridden out is real-time regardless of worker
  count. An unreadable *or unwritable*
  marker fires *unconfirmed* rather than skipping,
  because a guard that cannot self-check must say so; the marker carries a 24h
  TTL refreshed every suspicious cycle, so a `_clear_marker` that silently
  failed cannot leave the gate armed forever. The gate applies to **every**
  arm including `roster_read_failed` — a raised roster read is often a
  momentary DB blip, and paging critical on one is how a safety net gets muted.
  A whole-database outage now reaches the check at all: `_run_cycle_inner`'s
  pre-cycle latest-violation read is fail-open (it previously raised before
  `collect_snapshot` ran, so H-01 never executed), with transition detection
  falling back to `canary:last_cycle_red` so a persistent outage chirps once
  rather than every cycle. Scoped to the roster read
  ONLY: on a live-but-quiet fleet `terminal_rows`/`enabled_schedules`/
  `orphan_refs`/`terminal_exec_statuses` are all legitimately empty, so a
  general "any SQL collector reads zero" rule would false-alarm on every idle
  install. Dual-track by construction (a pure function over the `Snapshot`; it
  issues no SQL). **Residual:** an entirely *stopped* fleet has no containers
  and no slots, so no evidence exists and H-01 can only reach
  `roster_empty_unverifiable`; partial blindness (roster returns 1 of 20) is out
  of scope, since a count comparison would false-fire on create/stop races.
- **Registration**: each new invariant is a new file under
  `src/backend/canary/invariants/` + a registry entry (per the catalog at
  `docs/testing/orchestration-invariant-catalog.md`); the service and API
  surface stay unchanged. **It must also carry all four per-invariant alert
  surfaces in `services/canary_alerts.py`** — `_INVARIANT_NAMES`,
  `_INVARIANT_RUNBOOKS`, and an id branch in each of `_render_message` and
  `_render_forensic` — or its green→red Slack alert degrades to a bare-id
  fallback with no name, evidence, or next step (#1880). Enforced by
  `tests/unit/test_1880_canary_alert_parity.py`, bidirectionally (a stale or
  typo'd id fails too). Source the name from the invariant module's own
  docstring title, **not** the catalog: catalog ids are not registry ids
  (catalog `E-06` is the unimplemented #129 check, while registry `E-06` is
  "no overdue `next_run_at`"), so a catalog-sourced name can confidently
  mislabel a live alert.

### 31.2 Canary Run-State Observability (#2217)
- **Status**: ✅ Implemented (2026-08-16)
- **GitHub Issue**: #2217
- **Problem**: nothing reported whether the harness is running. A disabled
  canary emits zero violations — byte-for-byte identical to a clean fleet. This
  is the **H-01 class one level up, applied to the detector itself**: H-01
  catches a blind collector *while a cycle runs*; it structurally cannot catch
  "no cycle is running at all" (a dead loop emits nothing). The harness had been
  switched OFF on the dev instance on the belief its snapshot reader was
  SQLite-only and would go blind on PostgreSQL — a constraint **retired by #1540**
  (every SQL-tier collector now reads the configured backend through the
  `get_engine()`/`DATABASE_URL` seam; `src/backend/canary/` has zero `sqlite3`
  imports). Re-enabling is a pure ops toggle (`CANARY_ENABLED=1` in the instance
  `.env` + redeploy) — the compose wiring shipped in #1876; this requirement adds
  only the surface that makes the state observable.
- **Primary surface**: `GET /api/canary/status` (admin-only, `require_admin`) →
  `CanaryService.get_run_status()` (Invariant #1 — logic in the service, thin
  router). Response `CanaryStatusResponse` (`models.py`, Invariant #14):
  `enabled` (`CANARY_ENABLED == "1"`), `status`
  (`disabled|healthy|stale|unknown`), `last_cycle_at`,
  `seconds_since_last_cycle` (clamped `max(0, int(age))`), `interval_seconds`,
  `stale_after_seconds`, `alert_sink_configured`, `redis_available`.
- **The contract the surface answers**: it reports the **shared** Redis cursor
  `canary:last_cycle_at` — written at cycle END with `snapshot.snapshot_time`,
  the instant the leader's collection *started*. So it answers "the
  collection-start instant of the last cycle the leader completed", and **lags
  real completion by up to one cycle's duration**. It is NOT "is the loop alive"
  and NOT literally "when a cycle finished". The **shared** cursor is read (never
  the per-worker `self.last_run_at`/`cumulative_cycles`): with #1881's leader
  lease only one worker cycles, so a non-leader answering the request has
  stale/zero in-process counters.
- **`status` derivation** (AC#3 — three states all distinct from
  enabled+fresh+zero-violations):
  - `disabled` — `enabled=False`. Clean, **never** an alarm; Redis is never read
    (`redis_available=None`). Default-OFF is the normal state for most installs.
  - `unknown` — enabled but no readable timestamp (cursor never written yet **or**
    Redis raised **or** unparseable). **Fail-open**, never an alarm.
  - `stale` — enabled, cursor readable, `age > stale_after_seconds`. The incident
    case.
  - `healthy` — enabled, cursor readable, `age ≤ stale_after_seconds`.
- **Staleness threshold** `stale_after_seconds =
  _max_failover_seconds() + _MAX_CYCLE_LEASE_SECONDS` (≈780 + 900 = **1680s** at
  defaults), both terms the service's own constants so the threshold cannot drift
  out of step with the timing it guards. It is **provably above BOTH** the
  leader-failover window (~780s) AND a legitimately-slow-but-healthy cycle: a
  cycle may run up to `_MAX_CYCLE_LEASE_SECONDS` (900s, R-01's `docker exec`
  sweep wedge-yield ceiling) before it is deemed wedged, and because the cursor
  carries the collection-start instant, a healthy leader's observed cursor age
  reaches `interval + cycle_duration` (up to 1200s). Budgeting only
  `_max_failover + interval` (1080s) would false-`stale` a working harness.
- **`alert_sink_configured` is a deliberately separate field** (not folded into
  `status`): liveness and can-it-alert are orthogonal facts, and an
  enabled+cycling canary with no `CANARY_SLACK_WEBHOOK_URL` persists violations
  but **pushes nothing** — a silent-green canary that must not read as an
  unqualified `healthy`. Read on **every** path (including disabled — an operator
  wiring up a canary wants the sink state before flipping it on).
- **Secondary surface**: `canary_enabled` boolean on `GET /api/settings/feature-flags`
  (any authed user, public-safe), beside `mcp_agent_chat_pull_enabled` /
  `redelivery_governor_enabled` — the observability-only flag home. **Boolean
  only**; last-cycle/stale/sink detail stays admin-only on `/status`. Backed by a
  thin public `CanaryService.is_enabled()` wrapping `_is_enabled()` so "is the
  canary enabled" has one source of truth; the handler imports `canary_service`
  **function-locally** (a top-level import would pull the whole `canary` package
  into the settings-router load).
- **A manual `POST /api/canary/run-cycle` also advances the cursor**, so
  `/status` reports last-cycle across scheduled AND on-demand cycles — it is not a
  probe for scheduled-loop liveness specifically.
- **Backend-agnostic since #1540** — safe to keep `CANARY_ENABLED=1` on
  PostgreSQL. Stated in `docs/POSTGRESQL_SETUP.md` and
  `docs/migrations/SQLITE_TO_POSTGRES.md` (AC#4) so the next operator does not
  re-derive the retired SQLite-only constraint from the docs.
- **Deferred (follow-up)**: an **active push** liveness alarm. A pull-only
  `/status` is queryable-if-asked; the bug's narrative ("switched off during an
  incident, silently never switched back on") is a push problem. The canary
  cannot self-emit its own not-running (H-01 recursion), but a different
  always-on process (`cleanup_service` / `src/scheduler/`) could read
  `canary:last_cycle_at` + `_is_enabled()` and push via the existing Slack sink on
  `enabled && stale`. Deferred because it needs its own default-OFF gating so it
  never alarms an install that never opted in — the exact false-alarm risk the
  issue warns against.
- **Location**: `src/backend/services/canary_service.py`
  (`get_run_status`, `is_enabled`, `_read_last_cycle_for_status`),
  `src/backend/routers/canary.py` (`GET /status`),
  `src/backend/models.py` (`CanaryStatusResponse`),
  `src/backend/routers/settings.py` (`canary_enabled` flag).
- **Guard**: `tests/unit/test_2217_canary_status.py` (the `tests/test_canary_*.py`
  root suite runs in no CI workflow, #1880 — new guards go under `tests/unit/`).

---

## 35. Enterprise Edition Architecture (#847)

### 35.1 Open-Core Seam — Private Submodule Integration (#847)
- **Status**: ✅ Implemented (2026-05-21)
- **GitHub Issue**: #847 (design + paid-module catalog tracked privately in `trinity-enterprise`)
- **Description**: A generic extension seam in the public backend for loading
  closed-source modules from a private git submodule at
  `src/backend/enterprise/`. The seam is feature-agnostic — it carries **no
  enumeration of which capabilities are paid**; that catalog and the
  per-module designs live only in the private `trinity-enterprise` repo.
- **Key mechanism (public)**:
  - `EntitlementService` (`src/backend/services/entitlement_service.py`) — a
    registry. `register_module(feature_id)` populates a set; `is_entitled()` /
    `list_entitled_features()` read from it. OSS builds never call
    `register_module` → empty set → deny everything. `TRINITY_OSS_ONLY=1` is a
    hard override (denies even when modules ARE registered).
  - `requires_entitlement(feature_id)` (`src/backend/dependencies.py`) — a
    FastAPI dependency factory mirroring `require_role`; HTTP 403 when not
    entitled.
  - Conditional loader in `src/backend/main.py` —
    `try: from enterprise.backend import register_enterprise; register_enterprise(app) except ImportError: pass`.
    OSS-only builds (no submodule) silently no-op.
  - `/api/settings/feature-flags` exposes `enterprise_features: list[str]` —
    empty in OSS mode, populated when the private submodule is mounted; the OSS
    frontend reads it to decide which gated surfaces to render (same pattern as
    `session_tab_enabled` / `voice_available`).
  - Enterprise Vue components ship in the OSS bundle (no algorithmic IP — the
    moat is the private backend logic); they are gated purely by the
    server-driven `enterprise_features` list.
- **Tunables (env)**: `TRINITY_OSS_ONLY` (`0`/`1`, default `0`) — force
  OSS-only mode regardless of submodule presence.
- **Private (not in this repo)**: the specific module catalog, their routers and
  private schema, the licensing/entitlement enforcement design, and the
  commercial rationale are documented privately in `trinity-enterprise`.

### 35.2 Seam DX — Optional Submodules, Public Install Doc, Edition Surface (#1443)
- **Status**: ✅ Implemented (2026-07-04)
- **GitHub Issue**: #1443 (epic #1258)
- **Description**: Make the open-core seam discoverable and friction-free.
  Both private submodules (`.claude`, `src/backend/enterprise`) are marked
  `update = none` in `.gitmodules`, so a fresh public clone +
  `git submodule update --init --recursive` completes **without credentials**
  (git skips them, exit 0). Mounting is an explicit per-clone opt-in.
- **Opt-in mechanics** (empirically verified): under `update = none`, a plain
  `--init <path>` is *also* skipped, and a one-shot `--init --checkout` copies
  `none` into local config (future plain updates skip again). The durable
  opt-in is config-first: `git config submodule.<path>.update checkout`, then
  `git submodule update --init <path>`. Existing clones initialized while
  `.gitmodules` had `update = checkout` (i.e. `.claude` post-init) carry a
  protective local override; enterprise clones do NOT and need the one-time
  config line (documented in `docs/ENTERPRISE.md`; `deploy-dev.yml` sets it
  and judges init success by the populated marker file, since skip == exit 0).
- **Public install doc**: `docs/ENTERPRISE.md` — generic seam only (mount
  commands, HTTPS-PAT URL override, rebuild, verification via boot line /
  feature-flags / `edition`); guard-compliant per
  `.github/workflows/enterprise-docs-guard.yml`.
- **Edition surface**: `GET /api/version` returns
  `edition: "oss" | "enterprise"` + `enterprise_features: list[str]`, both
  derived from `entitlement_service.list_entitled_features()` (the same
  source as feature-flags — surfaces can't diverge). Semantics: *effective*
  runtime entitlement, not submodule-on-disk; `TRINITY_OSS_ONLY=1` or a
  fully-failed registration → `"oss"`; partial registration → `"enterprise"`
  with the surviving modules listed. Handler imports the service
  function-locally (test-stub compatibility); `_build_version_payload` stays
  stdlib-pure with `edition`/`enterprise_features` threaded as parameters.

---

## 36. Build Info Surface (#926)

### 36.1 Version Chip + Git Commit Detail (#926)
- **Status**: 🚧 In Progress
- **Implements**: Issue #926
- **Description**: Operators need an in-app way to confirm which commit
  is actually deployed. Pre-#926, only the `VERSION` file (semver
  string) plus an optional `BUILD_DATE` env var were exposed via
  `GET /api/version`. Operators had to SSH or `docker inspect` to
  resolve "is my fix deployed?" — a recurring friction point during
  hotfixes and incident response. This surfaces git commit + branch
  metadata baked in at backend image build time.
- **Backend (`GET /api/version`)** — extended payload:
  ```json
  {
    "version": "0.9.0",
    "platform": "trinity",
    "edition": "oss",
    "enterprise_features": [],
    "components": { … },
    "runtimes": ["claude-code", "gemini-cli", "codex"],
    "build_date": "2026-05-25T14:00:00Z",
    "git_commit": "f1ba610fab…full sha…",
    "git_commit_short": "f1ba610f",
    "git_commit_subject": "review(#929): drop dead accessor…",
    "git_commit_timestamp": "2026-05-25T11:45:00+00:00",
    "git_branch": "dev",
    "voice_enabled": false
  }
  ```
  All new fields default to `"unknown"` when the build args are
  absent (local dev / volume-mount workflows). Endpoint stays
  JWT-authenticated (SEC-180).
- **Build wiring**:
  - `docker/backend/Dockerfile` accepts `GIT_COMMIT`,
    `GIT_COMMIT_SUBJECT`, `GIT_COMMIT_TIMESTAMP`, `GIT_BRANCH`,
    `BUILD_DATE` as `ARG`s and re-exports each as `ENV` so the
    runtime reads them via `os.getenv()`.
  - `docker-compose.yml` `backend.build.args` block forwards the
    `${GIT_COMMIT}` etc. shell vars from the environment so
    `docker compose build` picks them up automatically.
  - `scripts/deploy/start.sh` exports the args from the local repo
    before the build: `git rev-parse HEAD`, `git rev-parse --abbrev-ref HEAD`,
    `git log -1 --pretty=%s`, `git log -1 --pretty=%cI`, and
    `date -u +%Y-%m-%dT%H:%M:%SZ`.
- **Frontend**:
  - `NavBar.vue` renders a small muted version chip (e.g. `v0.9.0`).
    Click opens a modal with the full build-info block.
  - `Settings.vue` adds a "Build Info" subsection showing version,
    commit short SHA + full SHA, commit subject + ISO timestamp,
    branch, build date.
  - One-shot fetch on app mount via a `useBuildInfo()` composable
    that caches the response — build metadata never changes at runtime.
- **Out of scope**: per-component version drift (frontend vs
  backend), MCP server version surface (the MCP TypeScript
  package has its own `package.json` version), agent base-image
  commit metadata. Follow-ups if useful.

---
