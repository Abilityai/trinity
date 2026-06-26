# Feature Flow: Docker Socket GID Detection (#1131)

> **Status**: Shipped 2026-06-26. Deploy/runtime integrity guard — no UI, no
> API, no DB. Closes the #874 non-root regression where the backend silently
> showed **"No agents"** on macOS Docker Desktop / Colima / Rancher / rootless.
> Sibling of [backend-image-packaging.md](backend-image-packaging.md) — both are
> non-root-container (#874) deploy guards with hermetic CI + a regression test.

## Overview

The backend runs as **UID 1000** (non-root, Invariant #17) but must still reach
`/var/run/docker.sock` to enumerate agent containers (Invariant #11 — Docker is
the source of truth). It joins the socket's owning group via compose's
`group_add: ["${DOCKER_GID:-999}"]`. The GID that must be joined is **whatever a
container sees on the mounted socket**, and that varies by runtime — so
`scripts/deploy/start.sh` auto-detects it into `DOCKER_GID` before `compose up`.

## Problem

`#874` assumed Docker Desktop **ignores** `group_add`, so `ensure_docker_gid()`
returned early on non-Linux and left `DOCKER_GID` at the Debian default `999`.
Docker Desktop / Colima / Rancher / rootless actually present the socket
**root-group-owned (GID 0)** inside the container, so UID 1000 + group 999 is
**denied**. `list_all_agents_fast()` swallowed the resulting `PermissionError`
and returned `[]`, so `GET /api/agents` returned an empty list and the Agents
page silently showed **"No agents"** with nothing in the logs (#1131, P1).

The old `getent group docker` detection was also wrong here: it reads the
**host** docker group, which on Docker Desktop / rootless differs from the
in-container socket GID.

## Solution

Two independent changes:

### 1. Probe the in-container socket GID (`scripts/deploy/start.sh`)

`_probe_docker_gid()` (`scripts/deploy/start.sh:161`) runs a throwaway container
against the very socket compose mounts and reads the GID it sees:

```bash
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
    alpine stat -c '%g' /var/run/docker.sock 2>/dev/null | tr -dc '0-9'
```

`ensure_docker_gid()` (`scripts/deploy/start.sh:170`) precedence:

1. **Explicit override wins** — any `DOCKER_GID=<digits>` in `.env` (incl. `0`)
   short-circuits before any probe (`grep -qE '^DOCKER_GID=[0-9]+'`).
2. **Probe (PRIMARY)** — correct on every runtime: Docker Desktop / Colima /
   rootless → `0`; native Linux daemon → the host docker-group GID (bind mount
   preserves it, so the probe returns the same value `getent` would).
3. **`getent` (offline Linux fallback only)** — used only when the probe can't
   run (daemon down, alpine unpullable, SELinux denies the bind) **and**
   `uname -s` is `Linux`.
4. **Warn + leave blank** — non-Linux with no probe → `WARNING` advising
   `DOCKER_GID=<gid>` (Docker Desktop: `0`); compose falls back to `999`.

The detected value is written back to `.env` (in-place replace of a blank
`DOCKER_GID=` line, else appended). The daemon dependency is free: the
base-image check and `compose up` below already require a live daemon.

### 2. Make the swallowed denial observable (`src/backend/services/docker_service.py`)

`list_all_agents_fast()` (`src/backend/services/docker_service.py:136`) converts
the swallowed-exception `print()` to a **throttled `logger.warning()`**
(`docker_service.py:214`) so a denied socket reaches Vector instead of vanishing.
Throttled at most once per 60 s (`_SOCKET_WARN_THROTTLE_S`,
`docker_service.py:21`) because this is a per-poll hot path (5 s heartbeat-watch
+ operator-queue loops, 30 s monitoring loop, many request handlers) and the
denial is persistent until the operator fixes `.env` — an unthrottled warn would
flood the log stream (~24+/min indefinitely). Module-level monotonic timestamp;
the GIL makes the read-modify-write atomic enough that a rare duplicate warn
under a race is harmless. Still degrades to `[]` (never raises).

## Entry Points

- **Deploy**: `./scripts/deploy/start.sh` → `ensure_docker_gid` (`start.sh:199`)
- **Runtime**: `list_all_agents_fast()` on every fleet poll (heartbeat / monitoring
  / operator-queue loops and agent-list request handlers)
- No UI, no API endpoint, no DB table.

## Backend Layer

- `scripts/deploy/start.sh:161` — `_probe_docker_gid()`
- `scripts/deploy/start.sh:170` — `ensure_docker_gid()` (precedence above)
- `src/backend/services/docker_service.py:136` — `list_all_agents_fast()` (throttled WARN)
- `docker-compose.yml:190` / `docker-compose.prod.yml` — `group_add: ["${DOCKER_GID:-999}"]`
  (the `:-999` Debian/Ubuntu fallback applies only when `.env` carries no value)

## Side Effects

- Writes `DOCKER_GID=<n>` back to `.env` on first detection (idempotent — the
  explicit-override branch short-circuits subsequent runs).
- Emits a throttled `WARNING` to the structured (Vector-captured) log stream when
  the socket is unreachable at runtime.

## Security Considerations

- **Non-root invariant (#17) preserved** — UID stays 1000; only socket-group
  membership changes. Joining GID 0 (root **group**, not root user) is the
  required and standard mechanism for Docker Desktop's root-group-owned socket —
  **not** a privilege escalation (cleared in the diff CSO report,
  `docs/security-reports/cso-2026-06-25-1131-diff.md`, 0 findings).
- **No injection** in the `sed` write-back: `${detected}` is numeric-only on both
  source paths (`tr -dc '0-9'` on the probe; `cut -d: -f3` numeric GID field).

## Testing

- **`tests/deploy/test_docker_gid.sh`** — hermetic regression lock. Extracts the
  **real** `_probe_docker_gid` + `ensure_docker_gid` from `start.sh` (no copy →
  no drift) and drives them with shimmed `docker`/`getent`/`uname`. Covers:
  probe-wins-over-getent (the #1131 fix), getent offline fallback, Docker Desktop
  → `0`, explicit override (incl. `0`) respected with no probe, warn-on-no-detection,
  in-place replace, append. No daemon, ~1 s.
- **`tests/unit/test_docker_service_list_fast.py`** — locks the runtime contract:
  socket error → `[]` **+ WARNING**; no client → `[]` silently; persistent denial
  warns **once per window** then resumes after it elapses.
- **CI**: new `verify-docker-gid-detection` job in
  `.github/workflows/container-security.yml:227` runs the bash test. It is the
  **only** guard for the probe branch — `verify-non-root` runs on a Linux runner
  where the socket is already `docker`-group-owned, so it never exercises the
  Docker Desktop / rootless probe path. `tests/deploy/**` added to the workflow
  path filter (`container-security.yml:43`).

**Status**: ✅ Both suites pass locally and in CI.

## Related Flows

- [backend-image-packaging.md](backend-image-packaging.md) — sibling non-root
  (#874) deploy integrity guard (source→image packaging gap, #1033)
- [async-docker-operations.md](async-docker-operations.md) — async wrappers over
  the same Docker SDK surface
- [container-capabilities.md](container-capabilities.md) — non-root container
  capability baseline
