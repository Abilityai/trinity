# CSO Security Audit — Issue #474 Follow-up (Layer 2 + sibling-collapse)

**Mode**: `--diff`
**Scope**: `origin/dev..HEAD` on `AndriiPasternak31/issue-474-plan`
**Date**: 2026-05-17
**Base**: 2026-05-13 report (`cso-2026-05-13-474-diff.md`) covered the
initial pipe-drop reclassification. This follow-up covers the four commits
added since: per-base_url drop-grace (`c0599c20`), monitoring split
(`7831a811`), `TimeoutException` broadening + `/health` timeout liveness
(`7af831f3`), and regression coverage (`58c2ec49`).

## Files in scope

| File | Δ |
|------|---|
| `src/backend/services/agent_client.py` | +201 / -69 |
| `src/backend/services/monitoring_service.py` | +152 / -? |
| `docker/base-image/agent_server/services/gemini_runtime.py` | +12 |
| `docker/base-image/agent_server/services/headless_executor.py` | +19 |
| `docker-compose.sibling.yml` | +79 (new) |
| `tests/**` (5 new + 2 updated) | +1155 |
| `docs/memory/feature-flows/*.md` (3 files) | +13 |
| `docs/security-reports/cso-2026-05-13-474-diff.md` | +147 (new) |

## Summary

| Category          | CRITICAL | HIGH | MEDIUM | LOW | INFO |
|-------------------|---------:|-----:|-------:|----:|-----:|
| Secrets           | 0 | 0 | 0 | 0 | 0 |
| Dependencies      | 0 | 0 | 0 | 0 | 0 |
| Auth Boundaries   | 0 | 0 | 0 | 0 | 0 |
| Injection         | 0 | 0 | 0 | 0 | 0 |
| Platform Patterns | 0 | 0 | 0 | 0 | 1 |
| Configuration     | 0 | 0 | 0 | 0 | 1 |

### Findings

**CRITICAL / HIGH / MEDIUM**: None.

**INFO-1 — Stable user-facing error strings (positive change).**
`check_network_health()` now returns fixed strings (`"Connection refused"`,
`"HTTP timeout"`, `"HTTP transport error on /health: <ClassName>"`,
`"Connection dropped: <ClassName>"`) instead of the prior
`f"{type(e).__name__}: {e}"[:200]` template. This narrows the information-
disclosure surface (raw `httpx` / OSError messages no longer reach the
fleet-health UI) while keeping full classname + message in the debug log
for triage. Net security posture improves.

**INFO-2 — `docker-compose.sibling.yml` review.**
- Loopback-only bind: `127.0.0.1:6390:6379` — Redis is not exposed off the
  developer host.
- Both `REDIS_PASSWORD` and `REDIS_BACKEND_PASSWORD` are mandatory via
  `${VAR:?must be set}` — compose refuses to render without them.
- ACL parity with production: `default` user has `+@all`, `backend` user
  has the curated category list with `-@dangerous` (no `FLUSHALL`,
  `CONFIG`, `SHUTDOWN`, `DEBUG`, `MIGRATE`, `REPLICAOF`, `MONITOR`),
  matching the production stack's network-topology contract. The sibling
  file is dev/test-only and does not ship to production; safe.
- Healthcheck uses `REDISCLI_AUTH` env var (no password on argv) — same
  pattern as the production compose.

### Other checks

- **Secrets scan** (`grep -rE 'sk-|ghp_|AKIA|trinity_mcp_[a-z0-9]{20,}|password\s*=\s*"..."'` against diff): clean.
- **No new `.env*` files** added.
- **No new dependencies** — `requirements*.txt`, `package.json`,
  `pyproject.toml` unchanged. `uv.lock` is a 3-line stub already present.
- **No new HTTP routes** — diff contains no `@router.*` or `@app.*`
  decorators. Auth-boundary surface unchanged.
- **No injection vectors introduced** — no new `subprocess` /
  `os.system` / `eval` / SQL string-interpolation / user-controlled-URL
  callsites. The drop-grace path runs on internal docker DNS base URLs
  (`http://agent-<name>:8000`) constructed by `AgentClient.__init__`
  from validated agent names, not from external input.
- **Concurrency safety**: `_recent_drops: Dict[str, float]` and
  `_client_pool: Dict[str, httpx.AsyncClient]` are process-local. The
  pool-eviction handler uses an `is` identity check
  (`if evicted is client:`) before `aclose()`, so concurrent siblings
  cannot double-close someone else's client. Cleanup of fresh-during-grace
  clients runs in `finally`, exception-suppressed (`except Exception: pass`)
  — idiomatic for socket teardown and not a security concern.
- **Resource bounds**: `_recent_drops` grows with unique agent base_urls
  and is pruned on read past the 2 s window — bounded by fleet size, not
  by request volume. No DoS amplification.
- **Exception swallowing**: two `except Exception: pass` blocks (lines
  ~789 and ~821 of `agent_client.py`) are scoped to `client.aclose()`
  cleanup only; they cannot mask request-path errors.

### Recommendation

**CLEAR.** No CRITICAL, HIGH, or MEDIUM findings. The follow-up commits
narrow rather than widen the security surface — stable error strings
reduce information disclosure, the sibling compose file follows the
production ACL contract and binds to loopback only, and the new
drop-grace path is process-local, bounded, and cleanly closes its
non-pooled clients on every exit. Safe to merge.
