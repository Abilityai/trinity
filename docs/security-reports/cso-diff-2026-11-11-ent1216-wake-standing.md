# CSO Diff Audit — 2026-11-11 — whose turn a delegation wake is (trinity-enterprise#1216)

**Mode**: `--diff` (branch `feature/1216-wake-turn-reads` vs merge-base `af465288a` on `dev`), daily gate (8/10), cso v1.2.3
**Scope**: 7 files — `services/caller_wake_service.py`, two unit test files, `tests/registry.json`, `architecture/security.md`, one feature flow, this report
**Docker**: available; no image-internal check has overlap with this diff

## Phase 0 — what this change is

A wake turn (`caller_wake`) is dispatched on a delegating agent when a run it delegated ends. It carried nobody, so `turn_actor` rated it a visitor's turn and the ledger rule (`acting_turn_user_id`) hid every execution row from the agent's own key while it ran.

The change: the wake carries the callee row's `source_user_id` **only when that person rates `owner` on the delegating agent** (`_standing_person`, rated with the real `turn_actor` over a row with no source agent). Every other wake carries nobody, exactly as before. Also: the wake notice marks a cut of the callee's words with two lengths, and the wake audit row gains `standing` (`owner` / `none`).

Stack CVE quicklist: no row has overlap (no pin, image, lockfile, workflow or compose change).

## Phase 1 — attack surface delta

| Surface | Before | After |
|---|---|---|
| Endpoints, MCP tools, WebSocket channels | — | none added or changed |
| Wake turn's tier | `share/visitor` for every wake | `owner` when the delegation ran with an owner-tier person; unchanged otherwise |
| Agent key's execution reads during a wake | no rows | full view during an owner-standing wake; unchanged otherwise |
| Agent-context ingress | the callee's words, framed and capped at 2,000 characters | same text, plus a platform-written cut marker (two integers) |
| Audit | ids and enums | + `standing` enum |

## Phases run and verdicts

**P2 Secrets** (diff): scanned added lines for key prefixes, emails and private IPs — clean (the test fixtures use `example.com` and a synthetic key-shaped string that already existed in the file). Enterprise-docs guard pattern over the added doc lines: 0 hits.

**P3 Supply chain / P4 CI/CD / P5 Infrastructure / P6 Webhooks / P8 Agent-shipped content**: no overlap with the diff (recorded as not applicable, not as clean). Invisible-Unicode scan over every changed file: none.

**P7 Agentic**:
- **ASI03 identity and privilege.** The one grant in this diff is owner standing for a wake. It is given only when the callee row's person rates `owner` on the delegator, and that person is written at dispatch by `turn_tier_service.turn_origin_user_id`: the agent owner's id when the delegating turn was the owner's own, the shared person when the turn acted for one, nobody for a visitor. So the wake is an owner turn only where the delegation itself already ran with the owner's identity. Challenged:
  - A shared person's turn cannot dispatch as the owner: the turn header comes from the process environment and wins over any model-typed id (`src/mcp-server/src/types.ts`, "wins over an agent-supplied execution_id").
  - The synthetic row carries no `source_agent_name`, so the delegation rule (`_delegation_runs_as_owner`) cannot lift a non-owner.
  - A deleted user, a person with no access and a rating that raises all carry nobody (tests).
  - A person JWT cannot make a row wake-eligible: `source_agent_name` is principal-derived (`resolve_source_agent`).
- **ASI01 prompt injection.** No new ingress. The callee's words now reach an owner-tier turn where they reached a visitor-tier one. They are another agent's text, framed ("Its own words on how it ended") and capped; a synchronous `chat_with_agent` already returns the same text into the owner's running turn. The cut marker is platform-written and holds two integers.
- **ASI08 cascading failures.** The idempotency window, the hourly budget and the reserved-trigger recursion break are unchanged lines.
- **LLM02 disclosure.** The audit gains an enum, never a user id, name or email (test).

**P9 OWASP 2025**:
- **A01**: no route change. The read fence itself (`acting_turn_user_id`, `viewer_row_scope`) is unchanged; only what the wake row says changes.
- **A05**: no SQL, no shell, no template.
- **A09**: the wake's standing is now on its audit row.
- **A10 failure direction**: `_standing_person` states it in its docstring — anything unreadable carries nobody (**closed**: the wake is the visitor-rated turn it was), and it never raises on the billed terminal path.

**P10 STRIDE** (delta): *Elevation* — owner standing for a wake of an owner-identity delegation, analysed above. *Information disclosure* — an owner-standing wake reads executions the agent's key could already read outside a wake. *Repudiation* — improved (`standing` audited). Others: no change.

**P11 Data classification**: no new stored data. The wake row's `source_user_id` is an existing INTERNAL column.

## Findings

None at or above the 8/10 gate.

## Hypotheses

None in daily mode.

## Coverage gaps

- The live run covered the owner's delegation only (a local stack with the patched service: the wake row carried the owner, rated `owner`, and the agent's key read the callee's result while the wake ran). The wakes that stay unchanged are covered by unit tests on a real database (`tests/unit/test_ent1216_wake_turn_reads.py`), not by a live run.
- The private enterprise tree was not audited for consumers of a wake row's `source_user_id` beyond a grep for the trigger name (no hits).
- Pre-existing behaviour of the wakes this diff leaves unchanged, and of the other platform-dispatched turns with nobody on the row, is out of this diff's scope and tracked separately.

## Guard tests run

`test_293_admin_gate_rejects_agent_keys`, `test_1310_auth_wiring`, `test_186_enumeration_uniformity`, `test_ent614_source_agent_attribution`, `test_292_cmdline_credential_leak`, `test_ent926_agent_key_turn_scope`, `test_ent1148_caller_reads_stay_person`, `test_ent1148_fleet_reads_stay_person`, `test_ent1148_delegation_tier`, `test_ent875_viewer_tier`: 768 passed.

Mutations: removing the hand-off of the person turns 5 of the new tests red; carrying every person turns 4 red.

## Trend

No prior report covers this surface. Totals: 0 findings.
