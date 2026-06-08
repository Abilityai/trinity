# CSO Security Audit — Diff Mode (vulns & secrets focus)

**Mode**: `--diff`
**Branch**: `AndriiPasternak31/issue-874` vs `origin/dev`
**Date**: 2026-05-17
**Scope**: 10 modified files — all infrastructure (Dockerfiles, compose, CI, nginx, env example, architecture docs)
**Focus**: vulnerabilities & secrets

---

## Summary

| Category          | CRITICAL | HIGH | MEDIUM | LOW | INFO |
|-------------------|----------|------|--------|-----|------|
| Secrets           | 0        | 0    | 0      | 0   | 0 *(I-01 resolved)* |
| Dependencies      | 0        | 0    | 0      | 0   | 0    |
| Auth Boundaries   | 0        | 0    | 0      | 0   | 0    |
| Injection         | 0        | 0    | 0      | 0   | 0    |
| Platform Patterns | 0        | 0    | 0      | 1   | 0 *(I-02 resolved)* |
| Configuration     | 0        | 0    | 0      | 0   | 0    |

**This diff is itself a hardening change (issue #874 — non-root containers).** It moves backend / scheduler / mcp-server / prod-frontend from root to non-root UIDs, drops `NET_BIND_SERVICE` + `CHOWN` + `SETGID` + `SETUID` from the prod frontend, and adds a CI regression guard. No new attack surface introduced.

---

## Findings

### CRITICAL
None.

### HIGH
None.

### MEDIUM
None.

### LOW

**L-01 — Backend `docker.sock` membership remains root-equivalent on the host.**
- Backend now joins `${DOCKER_GID:-999}` to retain socket access after the root → UID 1000 switch (`docker-compose.yml:91-96`, `docker-compose.prod.yml:78-83`).
- The mount is `:ro` (limits attacker's ability to mutate socket metadata) but a read-only socket still permits `docker exec` / container create — i.e. container-escape-to-host is unchanged from baseline.
- Net effect of this PR is **strictly better**: previous setup was root + `docker.sock`. Now it's UID 1000 + `cap_drop: ALL` + `no-new-privileges:true` + `docker.sock`. Seccomp and capability drops now actually apply.
- Not a regression. Tracked here only because the docker-group access pattern is the dominant residual risk in the backend container.

### INFORMATIONAL

**I-01 — CI fallback password `CiTestPassword!1` hardcoded in workflow** — *RESOLVED 2026-05-17*.
- All three references (`.github/workflows/frontend-e2e.yml` previously lines 46/70/111) replaced with a new "Generate CI admin password" step that:
  - Honours an explicit `E2E_ADMIN_PASSWORD` secret when set.
  - Otherwise generates a per-run value `$(openssl rand -base64 24 | tr -d '/+=\n')Aa1!` — the `Aa1!` suffix guarantees OWASP ASVS 2.1 character-class coverage, the `tr -d` strips base64 chars that would corrupt form-encoded curl bodies.
  - Masks the value via `::add-mask::` and propagates it to subsequent steps via `GITHUB_ENV`, removing the inline `env: ADMIN_PASSWORD: ...` block from three downstream steps.
- Net result: the repo no longer publishes a default admin credential, even one only reachable from inside the runner sandbox.

**I-02 — `verify-non-root` CI step does not cover the prod frontend image** — *RESOLVED 2026-05-17*.
- Added new step `Verify prod frontend image UID` that builds the prod image out-of-band (`docker build -f docker/frontend/Dockerfile.prod -t trinity-frontend:prod-check ./src/frontend` — same context as `docker-compose.prod.yml`) and asserts `docker run --rm --entrypoint id trinity-frontend:prod-check -u == 101`.
- The e2e workflow still boots the dev compose (Vite-dev image) for Playwright; the prod image build is independent and adds ~1 min to CI.
- Architecture invariant #17 updated to document both CI guards.

---

## Secrets Scan (in-diff)

| Pattern | Result |
|---------|--------|
| `sk-*`, `ghp_*`, `AKIA*`, `xoxb-*` API key prefixes | Only matches in `tests/unit/` fixtures (pre-existing). No matches in any diffed file. |
| Generic `password|secret|token|api_key = "..."` | Only the CI fallback noted in I-01. |
| `.env` (vs `.env.example`) added | None. Only `.env.example` modified. |

---

## Vulnerability Surface (in-diff)

| Vector | Result |
|--------|--------|
| Dependency manifests | Not modified. |
| New HTTP routes | None. |
| Shell / command injection | CI `curl ... -d "username=admin&password=${ADMIN_PASSWORD}"` interpolates the env-supplied password into a form body. With the current value (`CiTestPassword!1`) the form encoding is intact; an `&` or `=` in a future `E2E_ADMIN_PASSWORD` secret would corrupt the form but is an operational defect, not a security one. |
| SQL injection | No SQL touched. |
| Path traversal / SSRF | No file or HTTP-client code touched. |
| Webhook signature handling | Not touched. |
| Container security regression | Net improvement — see Hardening Wins. |

---

## Hardening Wins (introduced by this diff)

1. **Backend**: root → `trinity:1000`. `cap_drop: ALL` + `no-new-privileges:true` now meaningful (`docker/backend/Dockerfile:68-78`).
2. **Scheduler**: root → `trinity:1000`, same DB-write UID as backend so the shared `/data` SQLite WAL stays coherent (`docker/scheduler/Dockerfile:35-44`).
3. **MCP server**: root → built-in `node:1000` (`src/mcp-server/Dockerfile:21-24`).
4. **Prod frontend**: `nginx:alpine` → `nginxinc/nginx-unprivileged:alpine` (UID 101). Drops `CAP_NET_BIND_SERVICE`, `CAP_CHOWN`, `CAP_SETGID`, `CAP_SETUID` (`docker/frontend/Dockerfile.prod:27-52`, `docker-compose.prod.yml:106-121`).
5. **CI regression guard**: `verify-non-root` step asserts UIDs and confirms `group_add` correctly wires Docker-socket access on Linux by exercising `/api/agents` (`.github/workflows/frontend-e2e.yml:67-99`).
6. **Architecture invariant #17** records the rule so future Dockerfiles failing it are caught at review (`docs/memory/architecture.md:824`).

---

## Recommendation

**CLEAR — merge.**

This branch is a defense-in-depth improvement with no introduced criticals or highs. Both informational items have been resolved in-PR; only L-01 (docker.sock residual risk) remains, and that is an accepted architectural constraint of the Trinity orchestrator.

### Suggested follow-up (separate PR — out of scope for #874)

- **L-01 mitigation via docker-socket-proxy**: introduce a `tecnativa/docker-socket-proxy`-style sidecar on `trinity-platform-network` that exposes a restricted subset of the Docker API (`CONTAINERS=1`, `IMAGES=1`, `EXEC=1`, `POST=0` for the methods backend doesn't need) and point backend at the proxy instead of mounting `/var/run/docker.sock` directly. This is a meaningful architectural change (new service, new compose entry, updated `docker_service.py` URL, end-to-end testing) and should ship as its own issue.
