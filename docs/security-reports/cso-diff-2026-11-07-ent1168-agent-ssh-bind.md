# CSO Diff Audit — 2026-11-07 — agent SSH listener posture (trinity-enterprise#1168)

**Mode**: `--diff` (branch `feature/1168-agent-ssh-bind` vs merge-base `9d280314` on `dev`), daily gate (8/10), cso v1.2.3
**Scope**: 33 files — `services/agent_ssh_binding.py` (new), `services/ssh_service.py`, `routers/agent_ssh.py`, the three agent-container create sites (`crud.py`, `lifecycle.py`, `system_agent_service.py`), `main.py` lifespan, three compose files, `.env.example`, `start.sh`, `docker/base-image/{Dockerfile,sshd_trinity.conf}`, `mcp-server/src/types.ts`, tests, docs
**Docker**: available — image-internal checks ran live against `trinity-agent-base:latest`

## Phase 0 — what this change is

A hardening/configuration change to how agent containers publish and run their SSH listener:

- the `22/tcp` publication gains a host interface (`AGENT_SSH_BIND_ADDRESS`, loopback by default) through one helper on all three create sites, with an AST guard;
- the grant endpoint advertises the interface Docker actually published (`HostConfig.PortBindings`), not the detected host alone;
- containers are created with `ENABLE_SSH=false`; the grant starts sshd on demand (root exec) and a boot sweep stops idle listeners where `ssh_access_enabled` is off;
- the base image locks `developer`'s password and asserts the effective sshd policy at build (`sshd -T`).

Quicklist rows with diff overlap: **Claude Code / Node CLIs in the base image** — unchanged pins, not touched by this diff (`not reachable (unchanged)`). **runc / Docker host** — the diff adds `docker exec` calls (two static root execs); the deploy docs' host floor is unchanged (`not reachable (unchanged)`). All other rows: no overlap.

## Phase 1 — attack surface delta

| Surface | Before | After |
|---|---|---|
| Published agent ports | `0.0.0.0:2222–2262` on every agent | `127.0.0.1:<port>` on new/recreated agents (operator-configurable); legacy containers unchanged until recreate |
| Pre-auth sshd processes | one per agent, from boot, regardless of `ssh_access_enabled` | none until a grant; idle ones stopped at boot when the setting is off |
| Endpoints | — | none added; `POST /api/agents/{name}/ssh-access` (admin-only) gains `warnings[]` + `connection.bind_address` |
| Root execs into agents | `passwd -l` (legacy cleanup) | + `SSHD_START_CMD`, `SSHD_STOP_CMD` — both static strings, no caller data |
| Env knobs | — | `AGENT_SSH_BIND_ADDRESS` (backend only; IP literal or fallback) |
| Image | `developer:developer`; `sed`-hardened sshd | locked password; drop-in + build-time `sshd -T` assertion |

Agent-context ingress: none added (the `warnings` go to the admin caller, never into an agent prompt).

## Phases run and verdicts

**P2 Secrets** (diff): `git diff` scanned for key prefixes, emails, private IPs — clean (test fixtures use documentation/RFC1918 placeholders only). No `.env` tracked. Enterprise-docs guard pattern over the added doc lines: no hits.

**P3 Supply chain**: no dependency, lockfile or pin changes in the diff — not applicable; no scanner needed (recorded, not "clean").

**P4 CI/CD**: no workflow changes.

**P5 Infrastructure** (diff overlap: Dockerfile, compose ×3):
- `USER developer` still terminal; the new `passwd -l`, `COPY --chown=root:root` drop-in and the assertion RUN all precede it (pinned by test). No `ARG` secrets, no `.env` copied.
- Compose: one env line per file (`AGENT_SSH_BIND_ADDRESS=${AGENT_SSH_BIND_ADDRESS:-}`), rendered and verified (`test_2280_hosted_compose_parity`, the ent873 knob test, a live `docker compose config`). No network, port or hardening change.
- Live in the base image: `Include` line present, `sshd -t` OK, `sshd -T` shows `passwordauthentication no`, `kbdinteractiveauthentication no`, `permitemptypasswords no`, `permitrootlogin no`, `pubkeyauthentication yes`, `usepam yes` with the drop-in mounted; start command idempotent; zombie-safe start verified (see finding L1's sibling note in the learnings fragment).
- Two-network invariant: untouched (`network='trinity-agent-network'` at all three sites, unchanged lines).

**P7 Agentic** (overlap: ASI03 identity, ASI05 code execution, ASI10 rogue agents):
- ASI03: the only endpoint touched stays `Depends(require_admin)` (rejects agent principals and unknown scopes, #1890/#2323). Guard run: `test_293`, `test_1310_auth_wiring`, `test_186`, `test_2996`, `test_2094`, `test_agent_auth_header_guard`, `test_292` — 213 passed.
- ASI05: the two new root execs are constant argv (`SSHD_START_CMD`, `SSHD_STOP_CMD`); the agent name selects the container via `docker_client.containers.get(f"agent-{name}")`, never a shell. `NOPASSWD:ALL` sudo for `developer` is unchanged and out of scope (startup.sh depends on it; documented).
- ASI10: an agent cannot start its own sshd through this path (admin-only grant); an agent *can* run `sudo /usr/sbin/sshd` itself as before — unchanged, and the published interface (loopback) is now what bounds it.

**P8 Agent-shipped content**: the drop-in is an sshd config, not agent-loaded instructions; invisible-Unicode grep over every changed file: none.

**P9 OWASP 2025**:
- A01: no new route; the grant stays admin-only; `warnings` disclose the detected host and bind IP to an admin who already receives `host`.
- A05: no SQL, no shell interpolation; `ipaddress.ip_address` validates the knob before it reaches Docker as a tuple element.
- A10 failure direction — stated per guard: resolver invalid → **loopback** (closed, one ERROR); specific-IP bind unassignable → **named error, grant/create refused** (closed); `ensure_sshd_running` failure → **500, no key injected** (closed); boot sweep → **fail-open** (a Docker fault or one failing exec is logged; readiness never blocks), including an unreadable `ssh_access_enabled` → listeners left alone (the conservative direction for availability; the posture WARNING still fires).

**P10 STRIDE** (delta only): *Information disclosure* — the create API's new 400 names the configured bind IP (L1). *Elevation* — none: no principal gains a capability. *Spoofing/Tampering* — none: no new credential or identity path. *DoS* — excluded by rule, except: a legacy container's sshd restarting is not possible through this path.

**P11 Data classification**: no new stored data. `AGENT_SSH_BIND_ADDRESS` is INTERNAL (host topology), read from env, logged at INFO.

## Findings

```
#   Sev   Conf   Status     Evidence    Category               Finding                                              Phase  File:Line
──  ────  ────   ──────     ────────    ────────               ───────                                              ─────  ─────────
L1  LOW   9/10   VERIFIED   SUPPORTED   info-disclosure        create API 400 names the configured bind IP          P9/A01 services/agent_ssh_binding.py:describe_ssh_bind_failure
```

**L1 — the bind-failure 400 names the configured interface IP to a creator-tier caller.**
`describe_ssh_bind_failure` returns `"... to AGENT_SSH_BIND_ADDRESS=<ip>: that address is not assigned on the Trinity host right now ..."`, and `crud.py::_create_agent_container` raises it as `HTTPException(400, detail=...)`. `POST /api/agents` is `require_role("creator")`, so a creator (not only an admin) learns the host's VPN/interface IP when a create fails during a VPN outage. Exploit scenario: a creator-tier user times agent creates during a Tailscale outage and reads the tailnet address of the Trinity host from the 400. Impact: disclosure of a private-range interface address to an already-authenticated platform user who can reach the host; no privilege change. Recommendation: **accept** — the value is operator topology the same user can usually infer, the message is the fix path for the operator who sees it, and the condition only arises on a non-default configuration (`AGENT_SSH_BIND_ADDRESS` set to a specific IP). Alternative if the team prefers: keep the IP in the log line and drop it from the HTTP `detail`.

No CRITICAL / HIGH / MEDIUM findings. **Totals (SUPPORTED): 0 CRIT · 0 HIGH · 0 MED · 1 LOW.**

## Hypotheses

- **H1** — a listener started by a grant keeps running after the grant's key expires (until the next backend boot, and only if `ssh_access_enabled` is then off). Pre-auth sshd on loopback (new containers) or every interface (legacy, until recreated) with no authorized key. Not a finding: the key sweep removes the authorized line at `expires_at`, the port is loopback for the containers this change creates, and the legacy exposure is the pre-existing one this change closes on recreate. A "stop sshd when the last key expires" extension of `cleanup_service`'s sweep would close it; noted for the operator, not filed.

## Coverage gaps

- Diff mode: phases 3, 4, 6 had no diff overlap and were not run beyond the overlap check. No full-run claims are made.
- Not a Docker pass: the full image was not rebuilt here (the build-time `sshd -T` assertion is exercised by `build-base-image.sh` / `publish-images.yml`); the drop-in's effect was verified live by mounting it into the current image instead.
- Live off-box probe of a loopback-bound port (`nc` from another host) not performed — the runbook gives the operator step.

## Trend

Prior reports: `cso-2026-09-30` finding 10 (MEDIUM — prod/hosted publish backend 8000, MCP 8080, Vector 8686 and OTel on all interfaces; `DOCKER-USER` firewall ships only with `--provision`; `DEPLOYMENT.md` never mentions `DOCKER-USER`/ufw). Its documentation half is **resolved by this diff** with new evidence: `docs/DEPLOYMENT.md` gains "Firewall: Docker publishes past ufw" recommending `scripts/deploy/docker-firewall.sh` for hand-installed hosts, and the Security Recommendations list names it. Its port-publication half (8000/8686/OTel) is **not** touched here and remains open on that report; this diff closes the sibling exposure that report did not enumerate — the agent SSH range 2222–2262, which the same `0.0.0.0` default published — for new and recreated containers.

## Verification notes

Independent verifier: sequential in-context challenge (single finding, LOW; no subagent spawned). Guard tests quoted above were run at the PR head: 213 passed.
