# Trinity Tests

> ⚠️ **This suite mutates the target instance.** The API fixtures **create and
> delete real agents** on whatever `TRINITY_API_URL` points at. **Point it at a
> local dev instance — never staging/production.** Every agent the suite creates
> is named with the `pytest-ephemeral-` prefix and torn down by name; the suite
> **never deletes an agent it did not create**. A leftover-sweep for crashed
> runs is **OFF by default** — enable with `TRINITY_TEST_CLEANUP_SWEEP=1`, and
> even then it only removes `pytest-ephemeral-` agents on a localhost target
> (#1558).

## Quick start

```bash
# From repo root. The local CLI (trinity_cli, used by test_cli_*.py) is installed
# on demand by the run-*.sh wrappers (via tests/setup-env.sh) — it is kept out of
# requirements-test.txt so an editable path doesn't red the repo's "Dependency
# Graph" check on every release.
python -m venv tests/.venv && source tests/.venv/bin/activate
pip install -r tests/requirements-test.txt
bash tests/run-integration.sh   # ~30 sec, 25 tests — verifies env
bash tests/run-core.sh          # ~30 min, full core + unit tier (requires running backend)
```

## Required env vars

The `run-*.sh` scripts source `tests/setup-env.sh` which pulls these from the
project `.env`. To run pytest directly without the shell wrappers, export
them yourself. `tests/conftest.py` also auto-loads `TRINITY_TEST_PASSWORD`,
`REDIS_BACKEND_PASSWORD`, `INTERNAL_API_SECRET`, and `SECRET_KEY` from `.env`
when python-dotenv is installed, so direct `pytest` invocations work too.

| Var | Source | Purpose |
| --- | --- | --- |
| `TRINITY_TEST_PASSWORD` | `.env::ADMIN_PASSWORD` | Aliases ADMIN_PASSWORD so the per-account auth rate limiter (5 fails / 900s at `routers/auth.py:35-46`) doesn't lock out the `admin` account before any test runs. |
| `REDIS_BACKEND_PASSWORD` | `.env::REDIS_BACKEND_PASSWORD` | Required by `tests/security/test_redis_network_isolation.py` for ACL tests. |
| `INTERNAL_API_SECRET` | `.env::INTERNAL_API_SECRET` | Internal-API auth for scheduler / agent-server callbacks. |
| `SECRET_KEY` | `.env::SECRET_KEY` | JWT signing key — must match the running backend. |

## Tiers

| Script | Backend? | What it covers | Wall time |
| --- | --- | --- | --- |
| `run-smoke.sh` | Yes | Marker `smoke` — high-signal API checks | ~2 min |
| `run-integration.sh` | Yes | Marker `integration` — E2E flows + `tests/security/` Redis ACL | ~1 min |
| `run-core.sh` | Yes | `-m "not slow"` for non-unit + unit tier (`-m "not slow"`) in two pytest invocations | ~30 min |
| `run-full.sh` | Yes | Everything, as orchestrated tiers — see below | ~45+ min |

### `run-full.sh` — the honest full run (#2080)

One command, one verdict. It runs each tier as its **own** pytest invocation
and **exits non-zero on any tier failure, collection error, or skip-audit
violation**. All tiers run even after one fails, so a single red tier cannot
hide the state of the rest.

```
tests/run-full.sh                 # everything
tests/run-full.sh --tier unit     # one tier (repeatable)
tests/run-full.sh --no-pg         # skip the disposable-Postgres tier
tests/run-full.sh -k pattern      # extra args pass through to pytest
```

Tiers: `unit`, `integration`, `git-sync`, `security`, `scheduler`,
`agent-server`, `api` (root-level live-backend tests), `standalone`,
`postgres`.

What it does beyond running pytest:

- **Self-heals the venv** — creates it if absent and `pip install -r
  requirements-test.txt` every run, so a newly declared dependency cannot abort
  a tier at collection. It refuses to run when the venv's Python differs from
  the image pin (read from `docker/backend/Dockerfile`, single declaration per
  #1891). `TRINITY_TEST_ALLOW_PY_MISMATCH=1` overrides it loudly, and the
  override is repeated in the final summary so such a run cannot be quoted as
  a clean result.
- **Postgres/Alembic tier** — starts a disposable `postgres:16-alpine`, runs a
  real `alembic upgrade head`, exports both `DATABASE_URL` (engine) and
  `TEST_POSTGRES_URL` (the variable the PG-gated tests actually check), runs
  them, and tears the container down. This is the dual-track migration
  contract (Invariant #3) exercised locally, not only in the `pg-migrations`
  CI job.
- **Provisions a test agent** so agent-dependent tests run instead of skipping
  (`tests/harness/ensure_test_agent.py`; exports `TEST_AGENT_NAME`).
- **Runs the standalone-documented tests** as separate invocations
  (`tests/harness/list_standalone_tests.py` derives the list from the tests'
  own "Run standalone: pytest …" instruction, so it cannot go stale).
- **Skip audit** — after every tier, `tests/harness/audit_skips.py` parses the
  `-rs` output and **fails the run on any skip whose reason is not on a named
  allowlist**. A skip is a test that did not run; it is indistinguishable from
  a pass in every summary line pytest prints, which is how whole tiers came to
  be silently uncovered. Adding an allowlist entry requires writing down why
  the condition is acceptable.
- **Nothing can hang it** — every tier carries `--timeout` and
  `--timeout-method=thread` (`signal` re-enters the interpreter from a handler
  and turned one hung read into a pytest INTERNALERROR).

Logs land in `tests/reports/full-<timestamp>/`, one per tier, plus a per-tier
PASS/FAIL summary table at the end.

## Where does a new test go? (#1895)

The **per-PR** CI unit job runs `cd tests && pytest unit/`
(`backend-unit-test.yml`), and `tests/unit/pytest.ini` seals that island
(`norecursedirs = ..`). So **`tests/unit/` is the ONLY directory a per-PR job
collects.** A `test_*.py` in the `tests/` **root** is run by no per-PR job — only
by the nightly `integration-nightly.yml` sweep and the local `run-*.sh` — so its
coverage is invisible to the merge gate (this is how #1880 shipped: the one file
exercising the canary alert composer, incl. the G-04 credential-leak check, gated
nowhere).

- **No live backend** (pure logic, or a tmp-SQLite DB — no `api_client` /
  `created_agent` / `ws_ticket` / other live fixture, no raw `httpx`/`ws_connect`
  to a running stack) → put it in **`tests/unit/`**.
- **Mixed** (some self-contained cases + some that need a live backend) → **split
  it**: the self-contained half → `tests/unit/`, the live half stays in `tests/`
  root.
- **Needs a live backend** → keep it in the `tests/` root. If it takes no live
  *fixture* parameter (raw `httpx`/`ws_connect`), add a
  `# allow-root-live-test: <reason>` comment so the placement guard exempts it.
- **Async unit tests MUST carry an explicit `@pytest.mark.asyncio`** (on the
  function, its class, or a module-level `pytestmark`). `tests/unit/pytest.ini`
  sets no `asyncio_mode`, so pytest-asyncio runs in **strict** mode: an unmarked
  `async def test_` does not run — it fails outright on the current toolchain, or
  passes vacuously on older pytest-asyncio. Either way it is a silent coverage
  hole.

Both rules are enforced by `tests/lint_root_test_placement.py` (part 1: no
self-contained test in `tests/` root; part 2: no unmarked async under
`tests/unit/`), which runs in the unconditional `lint-sys-modules` CI job (push +
PR, never path-filtered). `tests/lint_root_test_placement_baseline.txt` grandfathers
today's live-in-root set and is ratcheted — never grow it; move the new test
instead.

**Known uncovered surface:** the sibling dirs (`tests/security/`,
`tests/scheduler_tests/`, `tests/git_sync/`, `tests/agent_server/`, …) are also
collected by no per-PR job; this guard is root-scoped and does not close that
(tracked separately — #1958 makes the unit job a *required* check; giving the
sibling suites a per-PR home is its own follow-up).

## Friction recovery

### `429 Too Many Requests` on `/api/token`

The per-account rate limiter at `src/backend/routers/auth.py:35-46` allows 5
failed logins per 15 minutes per account. One bad `TRINITY_TEST_PASSWORD` (or
test code passing the wrong password) trips it and poisons every subsequent
test in the same window. To clear immediately:

```bash
# From project root:
REDIS_PW=$(grep ^REDIS_PASSWORD .env | cut -d= -f2-) && \
  docker compose exec -T redis redis-cli -a "$REDIS_PW" --no-auth-warning \
    DEL login_attempts_acct:admin
```

### Fresh install: `setup_completed=false`

The setup token is printed once to backend stdout at startup. If you missed
it (e.g. running tests against a fresh `./scripts/deploy/start.sh`), bypass
the wizard by setting the flag directly from inside the backend container:

```bash
docker compose exec backend python -c \
  "from database import db; db.set_setting('setup_completed', 'true')"
```

### `ModuleNotFoundError: No module named 'trinity_cli'`

The `run-*.sh` wrappers install the editable CLI on demand (via
`tests/setup-env.sh`). It is kept out of `tests/requirements-test.txt` because
an `-e ./src/cli` line there breaks GitHub's dependency-graph updater. If you
invoke pytest directly (without a wrapper), install it yourself:

```bash
# From repo root:
tests/.venv/bin/pip install -e ./src/cli
```

### Conductor workspaces: backend mounts the *original* repo, not the worktree

When working in a Conductor worktree (e.g.
`/Users/andrii/conductor/workspaces/trinity/<name>`), the running
`trinity-backend` container bind-mounts the *original* repo's
`src/backend/` directory — NOT the worktree's. Editing
`src/backend/routers/foo.py` inside the worktree and running
`docker compose restart backend` will NOT pick up your change. The
fix lands in your worktree's git history (committed there) but doesn't
go live in the running backend until either:
 1. Your branch is merged into the original repo's branch, or
 2. You copy the modified file into the original repo's tree before
    re-running tests (and remember to clean up the overlay before
    pushing the original repo's branch).

For tests-only changes (`tests/**`), this isn't an issue — pytest runs
from the worktree's local Python venv and sees the worktree's files
directly.
