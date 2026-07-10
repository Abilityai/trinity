# Trinity Tests

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
| `run-full.sh` | Yes | Everything (slow tests included) | ~45+ min |

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
