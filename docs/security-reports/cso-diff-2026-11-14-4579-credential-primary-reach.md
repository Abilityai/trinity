# CSO Diff Audit — 2026-11-14 — a credential ask reaches a person who can open its field (#4579)

**Mode**: `--diff` (branch `feature/4579-credential-primary-reach` vs merge-base `b046b7af4` on `dev`), daily gate (8/10), cso v1.2.3
**Scope**: 16 files — `services/role_addressing.py`, `services/ask_service.py`, the MCP `ask_for_credential` description and its test, one new unit test file, `tests/registry.json`, eight doc files, one learnings fragment, this report
**Docker**: available; no image-internal check has overlap with this diff

## Phase 0 — what this change is

A `credential` ask is answered only through the Workspace secret field, and that field opens for the ask's one reader only when the agent is on their Workspace roster (shared with them, or their own). The raise picked the reader with no such check, so a seated primary the agent was not shared with became the only reader of an ask nobody could fill.

The change: the reader of a credential ask is a chain with the field's reach checked at each step (`role_addressing.resolve_credential_reader`): the agent's primary person, else its owner, else the raise is refused `422 role_cannot_open_agent` and nothing is filed. `readdress_role` re-addresses a pending credential row by the same rule.

Stack CVE quicklist: no row has overlap (no pin, image, lockfile, workflow or compose change).

## Phase 1 — attack surface delta

| Surface | Before | After |
|---|---|---|
| Endpoints, MCP tools, WebSocket channels | — | none added or changed |
| `POST /api/agents/{name}/operator-queue`, `type: credential` | filed for the seated primary, or the owner when nobody is seated, whatever their reach | filed for the first of {primary, owner} who has the agent on their Workspace roster and an active account; `422 role_cannot_open_agent` when neither does |
| Who may be recorded as a secret's reader | the seated primary, or the owner | the same two people, never anyone else; a deactivated account is never recorded |
| Who may fill the field | the recorded reader, on the roster | unchanged (`_owned_ask` is not touched) |
| Agent-context ingress | — | none added; one clause added to a platform-written tool description |
| Log | — | one WARNING per refused raise and one INFO per hand-over to the owner: the agent's name, never a person |

## Phases run and verdicts

**P2 Secrets** (diff): scanned the added lines for key prefixes, addresses, private IPs and host paths — clean (the tests use `example.com`). Enterprise-docs guard pattern over the added lines: 0 hits. No value is handled by the changed code: the secret's path (`credential_sink`) is untouched.

**P3 Supply chain / P4 CI/CD / P5 Infrastructure / P6 Webhooks**: no overlap with the diff (recorded as not applicable, not as clean).

**P7 Agentic**:
- **ASI03 identity and privilege.** No grant is added. The set of people who can be recorded as a credential ask's reader does not grow: it was {seated primary, owner} and stays so. The new case is the owner being asked while a primary is seated. The owner already administers the agent's credentials (the Credentials section and the agent's `.env`), and a credential ask asks its reader to PROVIDE a value, it shows them none. The agent cannot steer the choice: seats, shares and ownership are human-only writes.
- **ASI09 human-agent trust.** An owner may now see a masked field for an agent whose primary is someone else. The card is the existing one, rendered as before. The refusal's remedy asks for a share; it does not tell the agent to ask someone else for the secret.
- **ASI08 cascading failures.** A refused raise is counted by the per-agent create limiter before the resolver runs (`_rate_allowed` precedes the addressing arm in `raise_ask`), so the new refusal, its lookups and its log line are bounded.
- **LLM02 disclosure.** The refusal and both log lines name roles and the agent, never a person, an address or a count (tests assert no address in the message, the detail or the log). One code covers "off the roster" and "deactivated", so the agent cannot tell which. It learns one fact about its own roster: nobody who could be asked can open it.
- **ASI01 / LLM01.** No third-party text reaches a prompt through this change.

**P8 Agent-shipped content**: the one changed agent-loaded text is the description of `ask_for_credential` (1,820 of 2,048 characters). Invisible-Unicode scan over all changed files: none. No injection string, URL or network call in the added lines.

**P9 OWASP 2025**:
- **A01**: no route change; the route's principal check (`get_self_acting_agent`) and the field's door (`_owned_ask`) are untouched. The answer refusal (`credential_answer_via_field`) is unchanged.
- **A05**: no SQL, no shell, no template.
- **A09**: a refusal files nothing, so it is logged at WARNING; a hand-over to the owner is logged at INFO.
- **A10 failure direction**: `on_workspace_roster` states it — an unreadable lookup is "not on the roster" (**closed**: the ask is handed on or refused by name, never filed for a reader nobody checked), and the deactivation check it calls denies on error. Pinned by a test that breaks the roster read, and by a mutation that makes it fail open (red).

**P10 STRIDE** (delta): *Elevation* — none: the candidate readers are unchanged and the more privileged of the two is the fallback. *Denial of service* — an owner can still block a credential by keeping the agent from its primary; that now reaches the owner or returns a refusal the agent can act on, where it used to produce a stuck row. *Information disclosure* — analysed under LLM02. *Tampering* — a credential row's reader is still written by the platform only. *Repudiation* — unchanged: the fill is audited by name as before.

**P11 Data classification**: no new stored data.

## Findings

None at or above the 8/10 gate.

Candidates considered and dropped:
- **The owner types the secret in place of a seated primary.** A deliberate product decision, and not a privilege change: the owner already controls the agent's credentials.
- **A re-addressed row that nobody can open keeps its previous reader.** When the primary seat moves and neither the new primary nor the owner can open the field, the pending row stays with the person it was filed for, who can still fill it while the agent is on their roster. Low and below the gate: `readdress_role` has no production caller in this tree or in the private module's pinned copy, the previous reader was the legitimate reader at the raise, and the open issue #4189 already tracks "a former primary can still fill the field". Recorded so the first caller of `readdress_role` decides whether such a row should be ended instead.
- **The refusal as an oracle.** One bit about the agent's own roster, no person, the same as the `approver` path since #4501.
- **A transient lookup error on the primary hands the ask to the owner.** Fails toward the more privileged legitimate reader; availability only.
- **Reach is checked only at the raise**, and a credential ask has no deadline, so a later unshare, deactivation or ownership transfer recreates the stuck state. Availability only, already recorded as debt in the learnings ledger.

## Hypotheses

None in daily mode.

## Coverage gaps

- The route-census runtime guards could not run on the audit machine (the full app does not import in its virtualenv). The diff adds no route; CI runs them.
- The fixed code was not run on a live stack. The defect was reproduced on a live `v1.0.5-rc2` stack; the fix is shown by unit tests over a real database, the real sink, the real door and the real route.
- The private enterprise tree is not mounted in this worktree. Its pinned copy was searched for callers of the changed functions: its `raise_ask` callers are gate raises that carry their own audience and do not take the changed arm, and nothing calls `readdress_role`.
- Independent verifier: not applicable, no finding reached the gate; the candidates above were challenged in sequence by the auditor.

## Guard tests

`test_293_admin_gate_rejects_agent_keys`, `test_1310_auth_wiring`, `test_186_enumeration_uniformity`, `test_ent614_source_agent_attribution`, `test_292_cmdline_credential_leak`, `test_ent435_settings_sink_guard`, `test_ent1053_approver_answer_fence`, `test_ent1042_role_answer_fence`, `test_ent1007_ask_to_canon_role`, `test_ent816_shared_asks`, `test_ent1095_readdress_role`, `test_4501_approver_reach`, `test_ent1258_suspended_workspace_door`, the six `test_ent1066_credential_*` / `test_ent1109_*` files and the three `test_ent1158_*` files, `test_4579_credential_primary_reach`: 746 passed.

## Trend

Two earlier diff reports cover this surface (`cso-diff-2026-11-04-ent1095-readdress-primary-asks`, `cso-diff-2026-11-12-4501-approver-reach`): no findings then, none now.
