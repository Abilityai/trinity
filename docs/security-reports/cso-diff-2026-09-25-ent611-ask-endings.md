# CSO Diff Audit — 2026-09-25 — abilityai/trinity-enterprise#611 PR A (how an ask ends)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/ent611-ask-object` → `dev` · **Skill**: cso v1.1 · **Audited**: the branch diff against merge-base `cec2a64be` (uncommitted tree), plus the one remediation this audit produced.

## Architecture (Phase 0)

Every way an ask ends now goes through one sink, `services/ask_service.py`:

- the operator answer
- the Workspace answer
- single cancel
- bulk cancel
- the poller's expiry

The sink runs four steps in order:

1. **Write.** A compare-and-set UPDATE flips `status` and writes the endings ledger (`disposition`, `disposed_at`, `disposed_by ∈ {person, timeout}`, `disposed_by_email`, `disposition_reason`, `batch_id`). A writer that loses the race writes nothing.
2. **Audit.** One audit row per transition, carrying ids and enums only.
3. **Broadcast.** One thin `/ws` trigger.
4. **Observe.** The registered observers receive only the rows this call won. The default observer is the ent#329 wake, extended to cancels and expiries (trigger `operator_ending`).

The diff adds four new trust surfaces:

- a person-only gate on the operator ending routes (`reject_non_person_principal`, an allowlist over `mcp_scope`);
- an agent's self-readback, `GET /api/agents/{name}/operator-queue/{request_id}`, behind the identity-first `get_self_acting_agent` (MCP `get_my_ask`);
- an "Ended asks" line in every composed Execution Context;
- a coarse ending on the Workspace projection.

## Attack surface (Phase 1, diff-scoped)

| Surface | Change |
|---|---|
| Endpoints | **1 new** (the self-readback). Respond, cancel and bulk-cancel are person-gated. Cancel takes an optional `reason` (≤ 500 chars), and bulk-cancel returns `batch_id`. |
| Workspace asks | The list gains `include_ended` (a 7-day window). The answer route is person-gated (this audit's fix). |
| MCP tools | **1 new**: `get_my_ask`, self-acting, no agent parameter. |
| WebSocket | +1 thin event, `operator_queue_cancelled`. `operator_queue_responded` is thinned to `{id, agent_name}` (before, it carried the answer text and the responder's email). The sink's bulk `operator_queue_cleared` drops `cleared_by`. |
| Background | Expiry goes through the sink. There is a new wake dispatch for cancels and expiries. |
| Migrations | 1 pair: 12 nullable TEXT columns, both tracks, Alembic `0075` on `0074` (a single head). |
| Docker, compose, dependencies, CI, vendored policy files, skills | Unchanged. |

## Findings (Phases 2–12)

**None open.** This audit found one defect, verified it independently, and fixed it in the same branch.

### F1 — MEDIUM — a system-scoped key could end an ask through the Workspace answer route, recorded as a person

**How it worked.** `client_portal/portal_auth.py::get_portal_principal` rejects agent-scoped keys (#2198), but it deliberately keeps a `scope='system'` key's platform breadth. `client_portal/asks/router.py::answer_ask` checked only the addressee and the roster. It then ended the ask through `ask_service.answer`, which writes `disposed_by='person'` and the key owner's email.

**Exploit.** Preconditions:

- the admin user has an email;
- an ask is addressed to that email.

A prompt-injected `trinity-system`, holding its injected system key, can then answer that ask — an approval included — and the ledger and audit record a person's decision. The operator routes had closed this door in the same PR (P1: "agent- and system-scoped keys → 403").

**Fix.** `PortalPrincipal` gains `is_person`:

- `True` for a portal session;
- `is_person_principal(user)` on the platform path.

The answer route refuses a non-person principal with the shared 403 `person_required` detail, before it reads the row. The Workspace's read breadth for system keys (#2198) is unchanged.

**Tests.** `TestWorkspaceAnswerIsPersonOnly` in `tests/unit/test_ent611_ask_endings.py` covers:

- the route refusal, with no write, no audit and no observer;
- an unknown id reading the same;
- a person still answering;
- person-ness per principal kind: JWT, user key, system key, portal session.

**Mutations.**

- The route refusal removed → 1 test red.
- The dependency no longer computing `is_person` → 1 test red.

Both files were restored byte-identically.

**Verification.** A fresh-context verifier confirmed the chain at 8/10.

### Demoted by verification — the expiry wake is an uncapped agent-driven self-trigger

With the wake on, an agent that keeps filing asks whose deadline has already passed wakes itself about once per poll cycle, with no human involved:

- ingest honours any deadline;
- each ask gets a fresh idempotency key;
- there is no minimum lifetime and no per-agent cap.

The independent verifier rated this **PARTIAL**: the mechanism is real, but it is not materially worse than self-scheduling that already exists. An agent key can create its own schedules with no minimum cron interval and no per-agent cap. `max_parallel_tasks` bounds both paths, and the owner must opt in. It falls below the daily gate, so it is **registered as debt** (`debt:2026-09-25-ent611-expiry-wake-uncapped-self-trigger`), covering both paths, to be revisited with PR B's native deadline floor.

### Out of scope

One pre-existing item outside this diff was referred to the private tracker.

## Key clearances

- **Person gate.** An allowlist (`mcp_scope ∈ {None, "user"}`, no agent, connector or portal-delegate identity). It fails closed on a missing attribute, and runs before any row read on respond, cancel and bulk-cancel. Each arm is tested with its own principal.
- **Self-readback.**
  - The identity check comes first, as one uniform 403 for every name that isn't the caller's; only then the ordinary uniform 404.
  - The projection is an allowlist, with no person emails.
  - A sibling agent's key is refused.
  - The system key reads only as `trinity-system`.
- **Provenance.** `channel` and `raised_by` come only from keyword arguments, never from the agent's file entry. A hostile-entry test forges every new column and reads NULL.
- **Disclosure.**
  - Machine keys never receive the new person fields (`disposed_by_email`, `resolved_to`) on get or list. The pre-existing `responded_by_email` and `addressed_to_email` are a registered residual.
  - The Workspace sees a coarse `ended_by ∈ {you, operator, timeout}` and never an email or the cancel reason.
  - Audit rows record `has_reason`, never the reason text.
  - The Execution Context line carries request ids, dispositions and times only.
- **Prompt surfaces.**
  - The operator's cancel reason reaches the agent framed as data, under an explicit header. Its author is a person with access to the agent, who can already chat with it.
  - Titles and request ids are the agent's own text returning to itself. Request ids are limited to `[A-Za-z0-9._:-]`.
- **Guards green (203 tests):**
  - enumeration uniformity (#186);
  - path-param pairing (#2094);
  - the admin gate (#293);
  - auth wiring (#1310);
  - `/ws` agent scope (ent#467);
  - the backend→agent auth header;
  - vendored-copy parity;
  - the settings sink (ent#435);
  - centralized models;
  - operator-alert emitters (#1677).
- **Secrets (P2).** 0 hits in 4,456 added lines. The enterprise-docs guard pattern has 0 hits over added public doc lines, with a positive control.

## Mutation battery (fix-reverting)

| Source | Mutations | Result |
|---|---|---|
| This audit (F1) | 2 | 2 red |
| The PR's own battery, security-relevant | 5 | all red |

The PR's own battery covers:

- the answer compare-and-set loser reaching the wake;
- bulk cancel handing observers the requested rows;
- the wake dropping the platform-alarm skip;
- `get_my_ask` using a fixed agent;
- the ent#467 allowlist entry removed.

Every file was restored byte-identically.

## Accepted and stated (not findings)

- **Pre-existing person emails to machine keys.** `responded_by_email` and `addressed_to_email` on the queue reads are registered as debt, not changed here.
- **The wake is not durable.** A crash between the ending commit and the wake spawn loses that wake. The agent still reads the ending back.
- **The expiry self-trigger.** Registered as debt, as above.

## STRIDE (diff-scoped)

- **Spoofing.** The actor comes from the authenticated principal, never a body. The readback's identity comes from the key. Since F1, the Workspace answer requires a person.
- **Tampering.** The ledger is written in the same compare-and-set as the status flip. File entries cannot set provenance or ending fields.
- **Repudiation.** `answered` / `cancelled` (+ `has_reason`) / `bulk_cancel` (+ `batch_id` and the ids actually cancelled) / `expired` (system), plus the `operator_resume_dispatch` receipts.
- **Information disclosure.** Thin triggers (a net reduction from the old respond broadcast), the allowlisted readback, the coarse Workspace projection, and new person fields withheld from machine keys.
- **Denial of service.** Bulk cancel is capped at 500 ids. Expiry handles at most 500 rows per cycle. The wake's container read runs off the event loop.
- **Elevation.** F1 is closed. There is no new admin surface.

## Data classification (diff-scoped)

| Data | Class | Where it goes |
|---|---|---|
| `disposed_by_email` | CONFIDENTIAL (PII) | Operator surfaces only. Withheld from machine keys on get and list; absent from the readback, `/ws` and the Workspace. |
| `disposition_reason` | CONFIDENTIAL | Operator surfaces, and the filing agent (framed as data, and via the readback). Never a Workspace client; never an audit row. |
| `disposition`, `disposed_at`, `disposed_by`, `batch_id` | INTERNAL | Coarse in the Workspace. |
| `raised_by`, `channel`, `to_role` | INTERNAL | — |
| `resolved_to`, `proposal` | CONFIDENTIAL | Written by PR B. `resolved_to` is already withheld from machine keys. |

## Trend

- **Prior:** `cso-diff-2026-09-24-ent689-readiness-gate` (0 findings).
- **This diff:** 0 open, 1 found and fixed (F1), 1 demoted and registered.
- **Direction:** stable.
