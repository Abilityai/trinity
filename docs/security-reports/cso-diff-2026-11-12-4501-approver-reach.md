# CSO Diff Audit — 2026-11-12 — an ask to the approver reaches only approvers who can open the agent (#4501)

**Mode**: `--diff` (branch `feature/4501-approver-reach` vs merge-base `3e0a512f9` on `dev`), daily gate (8/10), cso v1.2.3
**Scope**: 15 files — `services/role_addressing.py`, `services/ask_service.py`, the MCP `ask_operator` description, one new unit test file, three test fixtures, `tests/registry.json`, six doc sentences, one learnings fragment, this report
**Docker**: available; no image-internal check has overlap with this diff

## Phase 0 — what this change is

An agent's ask addressed `to: approver` was filed for whoever the seat provider named. The answer to such an ask is fenced to the people recorded at the raise (`may_end`), so a seated approver who cannot open the agent became the only person allowed to decide an ask they could never see: nobody could answer it.

The change: the raise records only the seated approvers for whom `role_addressing.can_open_agent` is true (`resolve_approver_for_ask`), and refuses `422 role_cannot_open_agent` when none is. It is the rule an ask to a canon role and a gated-skill approval already apply.

Stack CVE quicklist: no row has overlap (no pin, image, lockfile, workflow or compose change).

## Phase 1 — attack surface delta

| Surface | Before | After |
|---|---|---|
| Endpoints, MCP tools, WebSocket channels | — | none added or changed |
| `POST /api/agents/{name}/operator-queue`, `to: approver` | filed for every seated approver | filed for the seated approvers who can open the agent; `422 role_cannot_open_agent` when none can |
| Who may answer an `approver` ask | the recorded audience | a subset of the same audience, never a superset |
| Agent-context ingress | — | none added; one sentence changed in a platform-written tool description |
| Log | — | one WARNING per refused raise: the role and the agent's name |

## Phases run and verdicts

**P2 Secrets** (diff): scanned added lines for key prefixes, addresses and private IPs — clean (the tests use `example.com`). Enterprise-docs guard pattern over the added doc lines: 0 hits.

**P3 Supply chain / P4 CI/CD / P5 Infrastructure / P6 Webhooks**: no overlap with the diff (recorded as not applicable, not as clean).

**P7 Agentic**:
- **ASI03 identity and privilege.** No grant is added. The recorded audience of an `approver` ask can only shrink: `reachable_people` filters the provider's answer and nothing is added to it. An empty result refuses the raise, so no `approver` row is written with an empty audience, which is the one state `may_end` treats as unfenced (a legacy row). `readdress_role` reaches the same resolver through `_fresh_audience`, which leaves rows untouched on a refusal. Challenged by tests: an unreachable approver recorded beside a reachable one cannot answer (`may_end` False); the refusal writes no row, no audit entry and no broadcast, with a positive control.
- **An admin who is seated as approver and not shared the agent** is recorded (`can_user_access_agent` is true for an admin). That person is a seated approver and decides in Operations; this is not an escalation.
- **ASI09 human-agent trust.** The refusal's remedy is to share the agent with the approver. It does not tell the agent to address `primary` or `operator` instead, which would move a separate approval to the owner. The tool description says the same.
- **ASI08 cascading failures.** A refused raise is counted by the per-agent create limiter before the resolver runs (`_rate_allowed` precedes `_address`), so the new refusal and its log line are bounded.
- **LLM02 disclosure.** The refusal and the log name the role and the agent, never a person, an address or a count (test). The two codes tell the raising agent whether someone is seated who cannot open it; an ask to a canon role already tells it the same about its own roster.

**P8 Agent-shipped content**: the one changed agent-loaded text is the `to` parameter description of `ask_operator`. Invisible-Unicode scan over all 15 changed files: none. No injection string, URL or network call in the added lines.

**P9 OWASP 2025**:
- **A01**: no route change; the route's principal check (`get_self_acting_agent`) is untouched. The fence predicate (`may_end`) is unchanged.
- **A05**: no SQL, no shell, no template.
- **A09**: a refusal files nothing, so it is logged at WARNING.
- **A10 failure direction**: `can_open_agent` states it — an unreadable lookup is "cannot open" (**closed**: the raise is refused by name rather than filed for a reader who may not see it). Pinned by a test that breaks the user lookup.

**P10 STRIDE** (delta): *Elevation* — none: the audience shrinks. *Denial of service* — an owner can still block an approval by not sharing the agent with the approver; before this change that produced a stuck row, now it produces a refusal the agent can act on. *Information disclosure* — analysed under LLM02. *Repudiation* — unchanged.

**P11 Data classification**: no new stored data.

## Findings

None at or above the 8/10 gate.

Candidates considered and dropped: the two refusal codes as a seat oracle (the agent learns one fact about its own roster and no person; same as the canon-role path); reach checked only at the raise, so a later unshare recreates the stuck state (availability only, already recorded as debt in the learnings ledger); log volume from refused raises (bounded by the create limiter).

## Hypotheses

None in daily mode.

## Coverage gaps

- The route-census runtime guards could not run on the audit machine (the full app does not import in its virtualenv). The diff adds no route; CI runs them.
- No live run on a stack: the evidence is unit tests over a real database, the real sink and the real route.
- The private enterprise tree is not mounted in this worktree; its pinned copy was searched for callers of the changed functions (none).

## Guard tests

`test_293_admin_gate_rejects_agent_keys`, `test_1310_auth_wiring`, `test_186_enumeration_uniformity`, `test_ent614_source_agent_attribution`, `test_292_cmdline_credential_leak`, `test_ent1053_approver_answer_fence`, `test_ent1042_role_answer_fence`, `test_ent1007_ask_to_canon_role`, `test_ent816_shared_asks`, `test_ent1095_readdress_role`, `test_4501_approver_reach`: 358 passed.

## Trend

One earlier diff report covers this surface (`cso-diff-2026-11-04-ent1095-readdress-primary-asks`): no findings then, none now.
