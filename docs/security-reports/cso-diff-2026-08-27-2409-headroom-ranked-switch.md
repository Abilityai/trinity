# CSO Diff Audit — trinity#2409 headroom-ranked subscription auto-switch

- **Date**: 2026-08-27 · **Mode**: `--diff` (daily, ≥8/10 confidence gate) · **Branch**: `feature/2409-headroom-ranked-switch` (working tree vs merge-base `908f8713`, pre-commit)
- **Scope**:  21 files changed, 674 insertions(+), 170 deletions(-) — backend `services/subscription_headroom_service.py` (shared gate + MGET reader + pure ranker), `services/subscription_auto_switch.py` (service-layer selector, `asyncio.to_thread`, `destination_headroom` on the switch), `services/subscription_service.py` (new-agent selector), `db/subscriptions.py` (two filter-only listings replace two first-match selectors), `database.py` facade, `agent_service/crud.py` call site, `services/subscription_headroom_alerts.py` (constant re-export + docstring); 1 new test file, 6 adapted tests, `tests/registry.json`; 6 docs
- **Phases**: 0–14. Phases 3 (deps), 4 (CI/CD), 5 (infra/Docker), 8 (skills) are **N/A** — no requirements/lockfile, no `.github/workflows/`, no Dockerfile/compose, no `src/mcp-server/`, no `src/frontend/`, no `.claude/skills/` change. Phase 6 reduces to the one new integration surface: a Redis `MGET` on the existing breaker client.

## Findings

**None at the ≥8 confidence gate.**

No new endpoint, WebSocket, upload, background job, dependency, schema or migration. The diff changes **which subscription an already-authorised, already-triggered auto-switch lands on** and **what the switch records about why**. Every new input to that decision is platform-written (the provider snapshot the backend's own probe stores in Redis), every new output is a percentage, a tier word or a reset instant, and every failure path degrades to the pre-#2409 behaviour with a WARNING.

## Clean categories (what was checked)

| Axis | Verdict | Basis |
|---|---|---|
| Attack surface delta | CLEAN | zero new routes / WS / uploads / jobs. New code paths are reached only from the existing failure hook (`task_execution_service` / `chat_execution_service` → `handle_subscription_failure`, unchanged trigger + `auto_switch_subscriptions` gate) and the existing creation path (`crud._apply_subscription_env`, `is_claude_runtime`-gated as before) |
| Injection (A03) | CLEAN | db listings are SQLAlchemy Core with bound params (`db/subscriptions.py::_list_unfailed_subscriptions`: `.where(subscription_credentials.c.id != exclude_id)`); Redis keys are `_SNAPSHOT_KEY.format(sid=…)` over DB-sourced uuids passed as an `MGET` argument list (no command string); no `subprocess`/`eval`/`open` in added lines (scan: 0); no frontend change (no new `v-html` sink — the notification text already embedded admin-set subscription names before this diff, and the added clause is `:.0f`-formatted numbers) |
| Trust boundary of the new input | CLEAN / unchanged | the ranker trusts `subscription:headroom:{id}`, written only by `_store_snapshot` from the backend's own HTTPS probe (`_probe`, httpx default TLS verification) under the `backend` Redis ACL user (`docker-compose.yml:41`); agents sit on `trinity-agent-network` and cannot reach `redis:6379` (#589). Steering a switch would require write access to the platform Redis — the same boundary the LIMIT badge, both circuit breakers and the capacity slots already rest on. `_to_model` + the gate re-validate shape (pydantic) and a malformed snapshot blinds only its own candidate |
| Authorization / enumeration (A01, #186) | CLEAN | no agent-scoped handler touched; `tests/unit/test_186_enumeration_uniformity.py` unaffected. The selector runs under the pre-existing per-agent switch lock (`agent_switch_lock`) — `asyncio.to_thread` moves the blocking reads off the loop while the lock is held, so the read→decide→assign window stays serialised |
| Credential exposure (A02/A09) | CLEAN | secret-prefix / email / IP / host-path scan over every added line (source, tests, docs): 0. `get_subscription_token` is used for truthiness only (`subscription_service.py::select_subscription_for_new_agent`, as `get_least_used_subscription` did); `describe_reading` emits tiers, percentages, reset instants and an age — never a token; the fail-open WARNINGs log `type(e).__name__` + the Redis/import exception text, the shape `get_breaker_redis` already logs |
| Logging / audit (A09) | IMPROVED | the pick is now recorded with its reason on the activity `details`, the notification `metadata` and the result (`destination_headroom`), and an inert ranker (Redis down, poisoned import, ambient refresh off) logs a WARNING instead of silently falling back — the learnings-2026-08-12 "silent policy flip" class is closed by construction and pinned by `test_a_poisoned_headroom_module_fails_open_and_is_logged` beside a positive real-import proof |
| Fail-open direction | CLEAN | every failure of the ranking half yields the db's own load-balance order — exactly the pre-#2409 pick — never a blocked switch; the ONE new deny path (a fresh provider refusal is dropped) can only make auto-switch *decline* to move an agent onto a subscription the provider is refusing, and its freshness is bounded by the same `FRESHNESS_SECONDS` the LIMIT badge trusts |
| Availability class (excluded by rule) | noted | one `MGET` per selection, 1 s socket-bounded, off the event loop; N = registered subscriptions. No unbounded growth |
| Enterprise-docs-guard (ent#45) | CLEAN | the workflow's own `PATTERN` (Python `re`, lookahead-capable) over every ADDED line of the six touched docs: 0 hits. The docs describe OSS-core behaviour only |
| Vendored parity (Invariant #5) / agent-key self-boundaries / backend→agent auth / webhooks / channels / CI / Docker / deps / skills / MCP descriptions / voice-token lock | N/A | untouched by this diff |

## Below the gate (recorded, not findings)

| Item | Confidence | Disposition |
|---|---|---|
| `destination_headroom` (utilization %, reset instants, subscription names) rides the activity `details`, which the unfiltered `/ws` broadcasts (`agent_activity`). | 2/10 | Not a finding: the same activity already carried both subscription names; the added values are INTERNAL-class numbers with no cross-tenant delta. |
| A poisoned or unreachable Redis makes the ranker inert (today's behaviour) rather than failing the switch. | 2/10 | By design (AC #4); loud, not silent. |
| `_parse_utilization` reads a fraction > 1.0 as an already-percent value — an overage subscription would rank BEST. | n/a (pre-existing) | Filed **#2419** during `/autoplan`; out of this diff's scope. |

## Verification

No finding survived the confidence gate, so the independent adversarial pass was vacuous by construction; each *clean* verdict above is traced to a quoted line or a scan with a stated result. Behavioural proof that the change is real and bounded: the new suite fails **80/81** on the unmodified source; full `tests/unit` **12,718 passed** (1 pre-existing failure in the private submodule, `test_1920`, untouched by this diff); 36 API integration tests green against the live backend; a live switch on the local instance chose the 18%/9% subscription over the 0-agent 88%/60% one, and a pure-read run with every survivor provider-refused declined to switch and logged why.
