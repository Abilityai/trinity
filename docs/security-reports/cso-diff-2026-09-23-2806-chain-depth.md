# CSO Diff Audit — 2026-09-23 — #2806 (inter-agent chain-depth guard)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `AndriiPasternak31/issue-2806` → `dev` · **Skill**: cso v1.1 · **Audited**: commits `6be15f93a..5bc957d47` against merge-base `c3902756c`.

## Architecture (Phase 0)
The change adds a guard, it does not open a surface. `schedule_executions.chain_depth` (nullable INTEGER, both migration tracks) is stamped on the child row of an agent-principal call as `1 + MAX(chain_depth)` over the calling agent's `running` rows. `dispatch_admission_service.enforce_inter_agent_depth` is called from `/chat` admission, `/task` dispatch and the `/fan-out` router. A hop past `inter_agent_max_chain_depth` (default 8, 1–32) gets a named 403 before any idempotency claim, slot or row. The MCP client returns that 403 as a structured non-retryable result.

## Attack surface (Phase 1, diff-scoped)
- New endpoints: 0 · New tools: 0 · WebSocket: 0 new event types · Docker images/CI/dependencies: unchanged · Migrations: 1 (nullable INTEGER; SQLite + Alembic `0072`)
- New config: `INTER_AGENT_MAX_CHAIN_DEPTH` (not a secret; in all four env files) + ops key `inter_agent_max_chain_depth`, writable only through the existing admin-gated ops-config route, validated 1–32 and clamped on use
- Changed response surface: a new 403 body `{error, depth, max_depth, caller, target, message}` on `/chat`, `/task`, `/fan-out`
- Docs changed: area files, requirements, feature flows, test catalog. Enterprise-docs guard pattern replayed over the added lines: 0 hits

## Findings (Phases 2–12)
**None at or above the 8/10 gate.**

What was checked, and the line that clears it:
- **Guard keyed on the principal, not a header** — `_chain_caller` reads `current_user.agent_name`, or `trinity-system` for `mcp_scope == "system"` (`dispatch_admission_service.py`). `X-Source-Agent` and `parent_execution_id` are never read, so omitting either cannot strip the guard (tested: `test_2806_task_strip_case…`, `test_2806_agent_bearer_alone_is_refused_on_every_route`).
- **No enumeration oracle (Invariant #8)** — the 403 is raised after the `AuthorizedAgent` path dependency's uniform 404. Tested: `test_2806_the_uniform_404_wins_over_the_depth_403`. The body names only the caller (itself) and a target it is already authorized for.
- **No dangling idempotency claim (Invariant #18)** — the guard runs before `begin`. There is a behavioral test (no `in_flight` claim left) plus an AST order guard over all three entry points.
- **SQL** — `get_max_running_chain_depth` is SQLAlchemy Core with bound parameters. The migrations interpolate constants only.
- **Config tampering** — the floor of 1 means no stored value can refuse every agent call. A non-integer row falls back to 8 and never 500s the dispatch path (tested).
- **Recording** — the audit row and FAILED `agent_collaboration` activity carry agent names, depth and key id/name, and no secrets. The `/ws` activity broadcast has the same shape as existing collaboration activities (`chat_execution_service.py:217-218` already sends `source_agent`/`target_agent`).
- **MCP client** — `parseDepthRefusal` matches only a 403 whose body carries the code. Every other 403 (access denial, SELF-EXEC-001) still throws. Parsing is try/catch-wrapped on the error path.

## Accepted and stated (not findings)
- **This is a reliability bound, not a security boundary.** Documented residuals: a user-scoped key held by an agent counts as a root; an agent-key call with no running row counts as depth 1 (#2392 is the exact-parent fix). Loops, schedule triggers and events start fresh roots: filed as **#2973**.
- **Over-refusal is contagious (availability, excluded by rule 1).** An agent permitted to call Y can drive Y's running row to the max, for example by self-tasking up to depth 7 and then calling Y. While that row runs, every outgoing agent call Y makes is refused, including calls from a human's concurrent turn. This is limited to agents that already hold a permission edge, and it ends when the row finishes (`execution_timeout_seconds`). It is documented in `architecture/execution.md`.
- **Two sync DB reads on the event loop per agent call** (settings + one indexed aggregate). This matches the existing `admit_chat_request` reads. Excluded as DoS by rule 1.

## STRIDE (agent-to-agent dispatch, diff-scoped)
- **Spoofing**: depth derives from the authenticated key. **Tampering**: the caller cannot lower `MAX` over its own running rows. **Repudiation**: each refusal writes an audit row plus an activity. **Information disclosure**: none, because it runs after the uniform 404. **DoS**: see the over-refusal note. **EoP**: none; the guard only removes capability.

## Data classification (diff-scoped)
- `chain_depth`: INTERNAL (an integer)
- the refusal audit row: INTERNAL (agent names, key id/name, depth)

## Trend
Prior: `cso-diff-2026-09-21-ent549-file-audience` (0 open). This diff: **0 open**. Direction: stable. No independent fresh-context verifier ran, because no finding survived to need one.

> Not a substitute for a professional security audit. This is an AI-assisted first pass between professional audits.
