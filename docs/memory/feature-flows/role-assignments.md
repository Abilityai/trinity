# Feature Flow: Role Assignments — who fills which role, and which agent serves which human (trinity-enterprise#500)

> **Status**: OSS half shipping here — the seam
> (`services/assignment_provider.py`), four `ExecutionContext` fields, the
> `get_agent_assignments` MCP tool, a reserved operator-queue id prefix, and the
> `cascade_rename` fix the feature made reachable. **Inert on its own**: with no
> registered provider the seam returns `None`, every field stays `None`, and the
> prompt block renders byte-identically to a build without any of this. The
> record itself — the table, the write endpoints, the provider — is owned by a
> module in the private submodule and is not documented here (public docs
> describe the generic open-core seam only).

## Problem

Nothing in Trinity expresses a **working relationship** between a person and an
agent:

- `agent_ownership.owner_id` is creator / infrastructure — who provisioned the
  container, not who it works for.
- `agent_sharing` is a flat `(agent_name, shared_with_email)` access grant whose
  `allow_proactive` is a consent bit. It is `UNIQUE` per pair, so it
  structurally cannot carry a second *kind* for the same person.
- `agent_tags` is `(agent_name, tag)` — no user dimension at all.
- The Workspace roster is a flat union of the above.

So an agent built to serve one particular human has no way to know who that
human is, what business role they fill, or who else has a stake in its work. It
learns its collaborators (other agents) and the caller's own address, and
nothing about the people around it.

## Goal

Record **user × agent × role × kind**, with at most one `primary` per agent, and
tell the agent about it in the one place every invocation already reads: the
`## Execution Context` block.

**Scope boundary.** Business roles live in the team's canon repository
(`roles/<id>.md`, cloned into the agent's workspace). Trinity records **who
fills them today**. This is not a role store, and the backend cannot see canon at
write time — so a `role_id` naming a role that does not exist is not a 422, it is
a drift verdict later.

## The seam — `src/backend/services/assignment_provider.py`

`mfa_gate` / `a2a_gate` in shape: a `typing.Protocol`, a module-level
`_provider`, `register_provider` / `get_provider` / `clear_provider`, and one
resolve function.

```python
provider.assignment_for(agent_name: str, triggered_by: str | None) -> dict | None
```

answering

```python
{
    "primary_user_display": str | None,   # a display NAME, never an email
    "role_id":              str | None,
    "stakeholders":         list[str] | None,
    "proactive_consent":    bool | None,
}
```

Four properties are load-bearing rather than stylistic.

**It is SYNC, deliberately.** `compose_system_prompt` is a plain `def` called
from async request handlers. There are already two blocking engine reads on that
path (`_resolve_collaborators`, `_resolve_platform_url`), which is precedent for
one more *read* and not licence for HTTP. A provider is expected to answer from
memory; SQLite runs `NullPool` with a 30 s connect timeout, so a `connect()` from
the event loop under write-lock contention can stall a whole worker.

**The seam owns the failure handling, not the provider.**
`compose_system_prompt` has no exception handler of its own, and the `replace`
block it fills is unguarded. All three of its callers lose the execution-context
block if this raises; one of them (`pull_coordination_service`) falls back to
`caller_prompt` alone and loses the platform prompt entirely. So the `try` lives
here, exactly as `mfa_gate.gate_login` catches around `provider.gate_decision`.

**Shape validation sits beside the `try`, because `try`/`except` cannot see the
defect it catches.** A provider returning a `str` where a list was promised does
not raise — it iterates into single characters and renders
`Stakeholders: A, .,  , S, m, i, t, h`. Wrong, not absent, and therefore
invisible. `a2a_gate.py` carries the same check for the same reason. A malformed
field is dropped and the rest of the answer is kept; a malformed *answer*
degrades to `None`; both log a WARNING.

**Unknown keys are dropped.** The answer is projected onto a fixed key tuple, so
a provider cannot add a field that reaches the prompt without a change here.

Every degraded case — no provider, no assignment, a raise, a bad shape —
resolves to the same `None`, because the renderer's contract is already "omit
any field that is None or empty" and no caller could act on the difference.

## The rendered block

Two lines join `## Execution Context` between `Execution ID` and
`Collaborators`:

```
- **Primary human**: A. Smith (role: head-of-ops) — proactive contact NOT yet permitted; do not message them unprompted
- **Stakeholders**: approver: B. Jones, viewer: C. Okafor
```

- The primary line **requires a display name**. A role id with nobody in it is
  not a *primary human*, and rendering the role alone would state a relationship
  that does not exist.
- **Consent is stated, never implied.** An assignment records who fills a role;
  permission to message them lives on `agent_sharing.allow_proactive`, a
  different table with a different owner. A bare name would read as permission,
  so the qualifier is rendered in both directions and omitted only when consent
  is genuinely unresolved (`None`).
- Bounds are per field (120 / 64 / 140) rather than the generic 80, which
  truncates a real name mid-word; the stakeholder list is capped like
  collaborators (20, then `… (N more)`).
- Every value goes through `_sanitize_field`.

## Audience — the outside surfaces see none of it

`compose_system_prompt` is the **unified** path. An anonymous public-link chat,
an x402 paid turn and a Workspace client turn all reach it exactly as a staff
chat does. The assignment record names a real person, so it would be the **first
third-party identity** in the auto-filled block — `collaborators` are agent
names, `source_user_email` is the caller's own address. A visitor asking "who is
your primary human?" would be answered.

Two mechanisms close that, and both are required:

1. **The rendered field is a display name.** The answer contract has no
   email-shaped key, so an address cannot ride in even by a provider's mistake.
2. **`triggered_by` is forwarded to the provider**, which suppresses on an
   outside audience. It is an **ALLOW-list**, not a denylist naming `public` and
   `paid`: a denylist is open at the top and a surface added tomorrow would
   disclose by default. A `None` label — audience unknown — is suppressed too.

One fact the second mechanism rests on, and it is asserted over the source
rather than assumed: **a Workspace/portal turn is labelled `public`**, not a
third label. `test_ent500_public_turn_no_pii.py` fails if `routers/public.py`,
`routers/paid.py` or `client_portal/service.py` ever labels a turn anything
else, so a new outside-facing surface forces the audience decision instead of
inheriting disclosure.

## MCP — `get_agent_assignments`

The third surface (Invariant #13): `src/mcp-server/src/tools/assignments.ts` +
`client.getAgentAssignments` + one `toolGroups` entry under `operatorOnly`.

**Read-only by construction.** Creating an assignment is a GRANT, and the write
path refuses every non-interactive principal, so a write tool could only ever
fail. A source-level test asserts no `create_*` / `set_*` / `delete_*` tool
appears in the module.

`operatorOnly` is the allow-list `{user, agent, system}` — **`agent` is in it**,
which is precisely why the backend self-scopes an agent principal to its own
roster. Advertisement is not authorization, and an agent-scoped MCP key resolves
to its *owner carrying the owner's role*; on a default admin-owned install, an
un-self-scoped read would hand any agent the whole fleet's roster (the
ent#293 / #1890 class, moved one layer onto a read).

Degradation is deliberately **narrower** than `credential_vault.ts`. That module
branches on the detail SHAPE (a `{code, message}` dict vs a plain string) because
its backend raises coded refusals. This route raises none: its failures are the
entitlement 403 (plain-string detail) and a **uniform 404** covering both "route
absent" and "no such agent / no access" (Invariant #8 enumeration safety). So the
tool merges those in its message rather than claiming a distinction it does not
have.

## Reserved alert prefix

`ROLE_DRIFT_ALERT_PREFIX = "role-drift-"` joins
`operator_queue_service._RESERVED_ID_PREFIXES`. The role file lives in the
agent's **own writable workspace**, so an unreserved prefix would let the agent
pre-create the id of the alert about its own configuration and have
`create_item`'s `on_conflict_do_nothing` swallow the real one — the #1631
suppression attack aimed at the alarm most likely to notice tampering.

It is a **named public constant**, which is the deliberate deviation from the
house convention. The convention puts the constant in the *emitter's* module
(`BASE_IMAGE_STALE_ALERT_PREFIX` in `system_agent_service.py`); here the emitter
is in a different repository, so a literal on each side would be two strings with
no compiler, no test and no review connecting them.

## `cascade_rename` — an existing bug this feature made reachable

`register_agent_owned_table` is how a registered module joins the OSS agent
cascade, and its docstring promised the "delete + rename paths sweep/re-key" the
table. Only delete did: `cascade_delete` iterated `EXTRA_AGENT_REFS`,
`cascade_rename` iterated `AGENT_REFS` alone.

The consequence is not a stale row, it is a **cross-wire**. After a rename the
registered rows keep the OLD name, so the renamed agent silently loses them — and
a new agent later taking the freed name inherits them. For a table recording
which human an agent serves, that is one agent inheriting another agent's people,
and those people then flow into its system prompt and its roster read. The
codebase names this exact class one function away (`delete_reports_to_refs`:
*"a dangling ref would silently re-attach to an unrelated agent that reuses the
name"*).

Fixed here, in the public repo, as an edition-agnostic OSS fix: `cascade_rename`
sweeps `EXTRA_AGENT_REFS` with the same bound-parameter `text()` UPDATE the
delete path uses, and the docstring now says why both halves matter.

## Failure Semantics

| Failure | Result |
|---|---|
| No provider registered (core build) | `None` — block renders as before |
| Provider raises | `None` + WARNING; the FULL block, platform prompt included, still renders |
| Provider answers a non-mapping | `None` + WARNING |
| Provider answers a malformed field | that field dropped, the rest kept, + WARNING |
| Outside audience | `None` (the provider's own gate) |
| `include_execution_context=False` | the whole block is absent, provider never called |

## Testing

- `tests/unit/test_ent500_assignment_provider.py` — the three degrade paths,
  last-wins registration, `clear_provider`, unknown-key dropping, and a static
  assert that the seam file is in `enterprise-docs-guard.yml`'s hardcoded
  `SEAM_FILES` **and** in both `paths:` filters.
- `tests/unit/test_ent500_execution_context_fields.py` — rendering, ordering,
  bounds, the resolve-exactly-once property, the pre-filled-caller `replace`
  guard, and a raising provider still yielding the full block.
- `tests/unit/test_ent500_public_turn_no_pii.py` — audience suppression in both
  directions, fail-closed on an unknown label, no email-shaped key, and the
  outside-facing routers' trigger labels.
- `tests/unit/test_ent500_cascade_rename.py` — verified to FAIL on the pre-fix
  `cascade_rename`, using a table deliberately absent from the OSS MetaData
  (a test against an OSS table passes on the broken code).
- `tests/unit/test_ent500_role_drift_prefix.py` — the reservation and the
  cross-repo export contract.
- `src/mcp-server/src/tools/assignments.test.ts` + a block in
  `tool-visibility.test.ts`.

## Related Flows
- [execution-context-injection.md](execution-context-injection.md) — the block these lines join
- [system-wide-trinity-prompt.md](system-wide-trinity-prompt.md) — the parent prompt feature
- [mcp-connector.md](mcp-connector.md) — the scope tiers `operatorOnly` sits in
