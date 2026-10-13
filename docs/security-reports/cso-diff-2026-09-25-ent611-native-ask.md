# CSO Diff Audit — 2026-09-25 — abilityai/trinity-enterprise#611 PR B (agents raise asks natively)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/ent611-native-ask` → `feature/ent611-ask-object` (stacked on PR A, #3023) · **Skill**: cso v1.1 · **Audited**: the branch diff against merge-base `dfcb7c07b`, plus the remediation this audit produced.

## Architecture (Phase 0)

An agent now raises an ask through ONE platform call instead of appending to its `~/.trinity/operator-queue.json`:

- `POST /api/agents/{name}/operator-queue` (`agent_router`) and the MCP tool `ask_operator`, both **self-acting**: the agent comes from the key, never from a parameter;
- `ask_service.raise_ask`, the create entry point of PR A's sink. In order: static validation (named 422s) → replay of the first receipt → a 15-minute deadline floor → the own-expired-predecessor link and the `reask_requires_link` guard → role addressing (an optional provider, else `primary` → the owner, `operator` → no person) → the #1632 rate buckets → an atomic per-agent create (replay, depth cap and insert under one lock: PostgreSQL `pg_advisory_xact_lock`, SQLite `BEGIN IMMEDIATE`) → one `raised` audit row and a thin `operator_queue_new` trigger;
- native rows (`channel = mcp`) sit outside the file contract: the poller skips a file entry re-using a native id, and the write-backs and sweeps never touch one;
- the file poller creates through an outcome-reporting accessor and logs the file channel's deprecation.

Trust boundaries crossed: an agent key (agent-authored content, possibly prompt-injected) → a durable row a person reads and answers; a role → a person the platform resolves.

## Attack surface (Phase 1, diff-scoped)

| Surface | Change |
|---|---|
| Endpoints | **1 new**, authenticated, agent-callable write: `POST /api/agents/{name}/operator-queue`. Reads unchanged apart from this audit's fix. |
| MCP tools | **1 new**: `ask_operator`, self-acting, no agent parameter, policy row `none`. |
| WebSocket | No new type. `operator_queue_new` is reused as a thin `{id, agent_name}` trigger on the native path. |
| Background | The poller's reconcile loop: a foreign-id skip, an outcome-reporting create, a once-per-agent deprecation log. |
| Prompt | The Operator Communication section teaches `ask_operator` by its bare name; the file is the fallback. |
| Frontend | Re-ask badges on the Operations cards (attribute bindings only). |
| Migrations | None: PR A added the twelve columns. |
| Docker, compose, dependencies, CI, vendored policy files, skills | Unchanged. |

## Findings (Phases 2–12)

**None.** One candidate was verified on the live stack, then refuted as a new disclosure by a fresh-context verifier; the hardening it prompted stays in the branch.

### Demoted by verification — a native ask's addressee was readable by the agent's own key

**The mechanism (real).** `raise_ask` resolves the `primary` role to the owner's email and stores it in both `resolved_to` and `addressed_to_email`. The receipt names only the role, and `routers/operator_queue.py::_for_principal` withheld `resolved_to` from non-person principals, but not `addressed_to_email`. A live probe confirmed it: a native `primary` ask raised with a real agent key, then listed with the same key, carried `addressed_to_email`.

**Why it is not a finding.** The agent's own key already reads its owner's email in every case: `GET /api/users/me` builds the principal from the key owner's `users` row and returns its email, and nothing fences that route for agent keys. Execution listings carry `source_user_email` too. No new information reached the agent.

**Hardening kept.** `_for_principal` now withholds `addressed_to_email` from non-person principals on native rows (`channel` not NULL/`file`), matching `resolved_to`. This also closes a latent path: once an assignment provider answers `people_for`, a role can resolve to a person OTHER than the owner, whose email the agent could not otherwise read. A file entry's addressee is the agent's own input and stays (PR A's registered residual). Pinned by `TestRaiseRoute::test_a_native_asks_addressee_is_withheld_from_machine_keys` on the item, list and per-agent routes (red before the change on all three) and re-checked live.

## Fixed during the review that preceded this audit

- **Repudiation (A09) — the `raised` audit row was recorded as the owner acting in person.** The row passed the key's resolved owner as `actor_user`, and the audit resolver ranks a user above an agent. It now follows the ent#614 rule: no `actor_user`, `actor_agent_name` set, the presented key and the owner's email carried explicitly; the route test asserts the real resolver's answer, `("agent", <agent>, None)`.
- **The atomic depth cap was proven on SQLite only, by a test that could not lose the race.** A new `requires_postgres` module holds each create inside its critical section; removing either backend's lock turns that backend red on every run.

## Non-security defect found and fixed

The published MCP schema told models and schema-enforcing clients that `context` and `proposal` accept no keys: fastmcp passes every tool's schema through xsschema's `strictJsonSchema`, which stamps `additionalProperties: false` on each object-typed property, a `z.record` included. The fields are now declared object-or-null (an `anyOf` the walker leaves alone), pinned over `listTools` on the real transport, and verified on the live server. The same quirk affects existing tools with record parameters (for example `emit_event.payload`); that is outside this diff.

## Phase notes

- **P2 — secrets:** no key patterns, public IPs or real emails in `dfcb7c07b..HEAD`; the enterprise-docs guard found 0 hits over the 123 added doc lines, with a positive control.
- **P3–P5, P8:** no dependency, CI, Docker, compose, vendored-policy or skill changes.
- **P6 — self-boundaries:** `get_self_acting_agent` refuses a sibling agent's key, a user key and a system key naming another agent with one uniform 403 **before** any lookup (`scope == "agent" and agent == name`, or the system key only as `trinity-system`); ephemeral agents' keys are refused by the auth-entry allowlist, which does not list the new route. No new backend→agent calls.
- **P7 — AI:** agent-authored ask text reaches people only through DOMPurify-sanitised markdown (H-005) or bound attributes; it enters no other principal's prompt. Cost bounds on the native path: the per-agent and fleet rate buckets, the atomic depth cap, and the 15-minute deadline floor on the expiry wake. The MCP tool description is static text.
- **P9 — OWASP:** A01 — the replay, the predecessor link and `reask_requires_link` read only the calling agent's own rows; `to` is a role, so an agent cannot address an arbitrary person; the agent cannot author the Workspace thread or any platform column. A03 — SQLAlchemy Core with bound parameters; the advisory-lock key is an integer derived from sha256. A04 — static validation runs before the replay, time-dependent checks after it. A08 — `extra="forbid"`, byte-measured limits. A10 — no outbound requests.
- **P10 — STRIDE (the create path):** Spoofing — identity from the key. Tampering — platform columns only from the call. Repudiation — one agent-attributed `raised` row with the key. Information disclosure — no new disclosure (see the demoted candidate). DoS — rate buckets and the atomic cap. Elevation — an agent still cannot end an ask (PR A's person-only rule).
- **P11 — data:** ask content (agent-authored, CONFIDENTIAL) in `operator_queue`; the resolved person's email (PII) in `resolved_to` / `addressed_to_email`, withheld from machine principals on native rows, and in the operator-only audit row as the join back to the key's owner.

## Trend

Against PR A's report (`cso-diff-2026-09-25-ent611-ask-endings`): its F1 (a system key answering through the Workspace route) stays fixed; the registered expiry-wake self-trigger debt gains its native bound (the 15-minute floor). New: none (one candidate demoted by verification; its hardening kept).
