# /cso --diff — #2840 B-02 pull arm + B-08 (2026-09-28)

**Scope:** `feature/2840-b02-pull-aware` vs `dev`. Daily gate: 8/10.
**Result:** 0 findings (0 critical, 0 high, 0 medium, 0 low).

## Attack surface added
- **No new endpoint.** `GET /api/internal/next-task` now also writes `agent:pull_poll:{agent_name}` (a unix timestamp, 24h TTL) on every call. The write runs inside `pull_coordination_service.claim_next_task`, which the router reaches only after `_pull_authorized` passes: the internal secret, or the agent's own agent-scoped key bound to that `agent_name` on a pilot. An agent can stamp only its own key.
- **Container env read by the canary.** The Docker collector already lists running agent containers with full attrs. It now reads `Config.Env` for exactly two keys (`TRINITY_PULL_MODE`, `TRINITY_MAX_PARALLEL_TASKS`) and returns `{pull_mode, pool_size}` and nothing else.
- **New canary invariant B-08.** It is read-only. Its `observed_state` holds agent name, pool counts and poll age, and is persisted to `canary_violations` and rendered to Slack.

## Phases
- **Secrets:** no key patterns in the diff. The end-to-end test's fake credential is a `LEAKCANARY-` marker, not a real key.
- **Enterprise docs guard:** 0 hits across the changed files.
- **Dependencies, CI and Docker:** unchanged. `docker/backend/Dockerfile` copies `src/backend/canary/` whole, so the new module ships.
- **Redis ACL:** the `backend` user has `~*` and `+@write` without `@dangerous` (`docker-compose.yml:478-520`). `SET … EX` is permitted, and no new service touches Redis.
- **Injection:** no SQL is added. The Redis key is built from an authenticated `agent_name`.
- **Information disclosure:** the container env carries every injected credential. `_pull_env_of` discards everything except the two pull keys, and its `except` returns `None` without logging the exception (which could quote env text). `TestInvariantB02PullEndToEnd` seeds a credential-shaped value and asserts it is absent from `repr(snapshot)`.
- **Spoofing:** a compromised pilot can keep its own B-08 green by polling. It cannot affect B-02, which reads queue age from SQL and lease state from `schedule_executions`, or any other agent's key.

## Mutations
- Pilot lookup forced to `False`: the end-to-end test turns red.
- Env collector keeping every key: the end-to-end test turns red.

## Appendix (below the gate)
- **Accepted:** `record_worker_poll` makes a synchronous Redis call inside an async route. The handler already makes a synchronous database call, and the Redis client has no socket timeout, so a hung Redis stalls that worker's event loop. This is the existing pattern, not a new class, and DoS is excluded by rule.
