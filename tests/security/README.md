# Security integration tests

Live-stack integration tests for Trinity security boundaries (Redis network isolation, ACLs, …). Most tests in this directory require:

- The platform stack running locally (`./scripts/deploy/start.sh`)
- The `docker` Python package installed in the test venv (module-level `pytest.importorskip("docker")` skips when absent)
- Real Redis credentials available — either pre-exported in the shell or sourced from the project-root `.env` by `conftest.py`:
  - `REDIS_PASSWORD` (admin user)
  - `REDIS_BACKEND_PASSWORD` (backend ACL user, see [Network Topology in architecture.md](../../docs/memory/architecture.md#network-topology-589))

Tests that depend on a specific credential also gate it locally via a per-test fixture (e.g. `backend_password` in `test_redis_network_isolation.py`) — those tests skip with a clear message when the env var is missing, rather than crashing with `KeyError` (Issue #764).

Run only this directory:

```bash
cd tests
.venv/bin/python -m pytest security/ -m integration -v
```
