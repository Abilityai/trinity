# /cso --diff — feature/4813-remove-scheduler-events (abilityai/trinity#4813)

Date 2026-11-27 · cso 1.2.3 · daily gate · diff against `dev` at `ea04de240` · not a full run.

Scope: the scheduler's Redis event publisher and its `PUBLISH_EVENTS` setting are removed (`src/scheduler/service.py`, `src/scheduler/config.py`, four compose files); the `/ws/events` docstring and the live docs stop describing a relay that never existed; the Executions list store refreshes on the activity a run really sends (`src/frontend/src/stores/executions.js`).

## Findings

None.

## Checked and clean

- **Surface removed, none added.** The scheduler no longer sends on Redis pub/sub. The removed payloads carried schedule names and a free-text `error` string to any Redis client able to subscribe; nothing does that now. No endpoint, auth dependency, input path, credential path or MCP tool is added or changed.
- **Guard against its return.** `tests/unit/test_4813_scheduler_publishes_nothing.py` drives every former publish path on a real service with a recording Redis client, and scans `src/scheduler/` for a `publish` or `xadd` call, so a later signal cannot bypass the backend's event bus (Invariant #10) unnoticed.
- **Redis and networks.** ACL users, the two-network layout and the compose hardening are untouched: the compose change is one removed environment line in each of four files. The hosted/prod parity guards are green.
- **Frontend.** The store treats a socket event as a trigger only and refetches through the access-controlled `GET /api/executions`; no event payload is rendered, no `v-html` is added. The per-client socket filter is unchanged. Socket-driven refetches are paced to one per 2 s per open tab (each also reads the stats aggregate), so a busy fleet cannot drive the read rate, and no model call is involved.
- **Failure direction.** The removed `try/except` wrapped only the publish. No guard is added or loosened. The dispatch-exception branch keeps its conditional FAILED write; the row-gone case has a test.
- **Secrets:** no known-prefix, e-mail, private-address or URL hit in the added lines; no `.env` tracked.
- **Hidden characters:** none in the added lines.
- **Enterprise-docs guard:** no hit on added doc lines or on the `main.py` seam file.
- **Dependencies, CI workflows, Dockerfiles, agent-shipped files:** no file of those kinds changed.
- **Unit tier with CI's flags:** 43,931 passed. Every red id is accounted for under coverage gaps; none is in a file this branch touches.

## Coverage gaps

- No Docker pass. The diff changes no image or dependency, but it does remove a variable from the compose files, and the stack was not booted on the audit host. CI's stack-boot job is the first real render of those files.
- 231 unit ids are red on the audit host. 227 fail identically on the merge-base when the same 25 files are run there with the same flags: missing Python packages, a `sed` dialect difference and local version pins that differ from the image. The other 4 are signal-timing tests in the marketplace release file; they failed only under the parallel full run and pass alone on this branch. The guards that import the assembled app (the runtime route census, the ops-write sweep) are among the 227, so CI is the first real import of `main.py` with the docstring change.
- The optional private module is not mounted in this worktree. A text search of it for the removed event names, channel and setting found no reference.
- No dependency scanner ran, because no dependency changed.
- No independent verifier ran, because there is no finding to verify. Two independent reviewers read the plan before the code was written; their findings are folded into the change.
