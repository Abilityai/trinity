# CSO diff audit — 2026-10-07 — trinity-enterprise#753 (the per-agent skill gate map)

**Mode**: `--diff`, daily (8/10 gate) · **Skill**: cso v1.2.3 · **Base**: `dev` @ `a51549772` · **Branch**: `feature/753-skill-gate-map` (working tree, pre-commit) · **Counts as full run**: no

## Scope

| Area | Change |
|---|---|
| Storage | new `agent_skill_gates` (both tracks: SQLite `agent_skill_gates`, Alembic `0093`), `db/skill_gates.py`, CASCADE `AgentRef` |
| Read seam | `skill_gate_service.list_skill_gates` reads the table; a failed read raises (→ 503 `gate_unavailable`); `marker_lock` / `sync_marker_locked` / `write_marker` split out of `sync_gate_marker` |
| Writes | `services/skill_gate_map_service.py`: set / clear / reconcile / person-only drop, under the per-agent marker lock |
| REST | `GET/PUT/DELETE /api/agents/{agent_name}/skill-gates[/{skill_name}]` (`routers/skill_gate.py` `agent_router`) |
| Auth | `require_person_or_capability` (copied verbatim from trinity#3236), `get_skill_gate_readable_agent_by_name` |
| Assignment paths | `routers/skills.py` `_sync_gates` on six routes; `lifecycle.inject_assigned_skills`; `skills_sync_service._reinject_agent` |
| Metadata | `approval:` frontmatter key (closed set `{recommended}`) in `skill_packaging.extract_contract` |
| MCP | `list_skill_gates`, `set_skill_gate`, `clear_skill_gate`; `approval` mapped in `list_skills`; `TOOL_ACCESS_POLICY` rows |

No dependency, CI, Docker, compose, base-image or frontend change.

## Phase 0 — model and quicklist

The gate map is configuration that tightens what an agent may run. Its integrity matters in one direction: anything that removes or loosens a gate is the sensitive write. The trust boundaries are (a) who may write a gate (a person who owns the agent or is an admin; a `skills.manage` holder on an owner-owned agent, never itself), (b) who may read it, and (c) the assignment paths that move library defaults. Quicklist: no pinned component touched. Starlette BadHost row: the new fences read `request.path_params` (`_path_agent`) and the principal, never `request.url.path`; `request.scope["path"]` is read for the audit row only.

## Phase 1 — attack surface delta

- New endpoints: 3 (census `AGENT_CALLABLE`, reasons recorded). New MCP tools: 3 (operator scopes only; connector keys never see them).
- New exec: the marker force-write — constant argv (`marker_command(True)`), path positional; no name reaches argv.
- Agent-context ingress: `list_skill_gates` returns skill names (validated by `SKILL_NAME_RE`), approver kind, origin, deadline, `set_by`, `set_by_agent`, `set_at` — no free text. See finding 1 for `set_by`.

## Phases 2–11

- **P2 secrets**: added lines scanned for known prefixes and real-looking emails — none. Enterprise-docs guard PATTERN over added doc lines — no hit; wording that named an enterprise-only consumer was reworded to the generic seam during /review.
- **P3/P4/P5/P6**: not applicable (no dependency, workflow, Docker or webhook change).
- **P7 agentic**:
  - ASI03 — writes refuse an agent on itself (`self_person_only`), bound a holder to agents its owner owns (owner equality, 404), and refuse the system key, connector, ops, portal_delegate and a missing scope (`assert_person`, sentinel). An agent's unassign keeps explicit gates; a deferred package removal keeps every gate on the skill; the library can only tighten. The census guard, `test_186`, `test_293`, `test_1310_*` are green.
  - ASI10 — ghosts cannot hold gates (409 after the access check); `_EPHEMERAL_ALLOWED_ROUTES` unchanged.
  - ASI01/ASI06 — no new third-party text into a prompt; `approval:` is a closed enum, never rendered.
  - LLM02 — finding 1.
- **P8 agent-shipped**: no shipped file changed. Invisible-Unicode grep over every changed file: none.
- **P9 OWASP**: A01 — enumeration-safe (principal-only 403 before any lookup; sibling and missing agent answer byte-identical 404s, tested). A05 — SQLAlchemy Core binds only; no exec argument from input. A10 — failure directions stated: the read raises (closed); the reconcile never raises and inserts nothing on an unreadable library (stated, retried at the next start/sync/assignment — the library is admin-configured, an agent cannot make it unreadable); container-state unreadable → the exec is tried (bounded); `via_for` uses a sentinel for a missing scope.
- **P10 STRIDE (gate map)**: Spoofing — writer identity from the authenticated principal only. Tampering — every write under `lock_agent_rows` + the marker lock; insert only while the agent is live. Repudiation — audited `CONFIGURATION` rows with `via`/`trigger`, only on change. Information disclosure — finding 1. DoS — no cap on gates per agent (owner-written, self-inflicted; deferred by the user). EoP — covered under ASI03.
- **P11 data**: gate rows INTERNAL configuration; `set_by` CONFIDENTIAL (a username is an email for email-login users).

## Findings

| # | Sev | Conf | Status | Evidence | Category | Finding | Phase | File:Line |
|---|---|---|---|---|---|---|---|---|
| 1 | LOW | 8/10 | VERIFIED (fixed in-branch) | SUPPORTED | info-disclosure | The gate list returns `set_by` — a username, which for email-login users is their email — to agent keys, so an agent can learn a non-owner admin's email when that admin set or cleared a gate on it, or assigned it a recommended skill | P7 (LLM02) | `services/skill_gate_map_service.py` `_public`, `routers/skill_gate.py` `list_agent_skill_gates` |

**Finding 1.**
- **What is exposed:** `_public` copies `set_by` into every gate, and the GET route and `list_skill_gates` pass it through unchanged. An agent key may read its own gates, and a `skills.manage` holder may read an owner-owned agent's.
- **How an agent sees it:** an admin who signs in by email sets a gate on another user's agent. That agent's key calls `list_skill_gates`, and the admin's email enters its context.
- **The verifier's refinement (PARTIAL):** a gate the agent's own key wrote names the agent's owner, which `GET /api/agents/{self}` already returns. Start, sync and sweep writes record `system`. Only a non-owner writer is new.
- **Fix:** withhold `set_by` from any non-person principal on the read and write responses. This follows the #715 queue projection. Done in the branch, with a test.
- **Verifier:** independent agent.

## Hypotheses

None (daily mode).

## Coverage gaps

- No Docker pass: diff mode, no image change.
- The PostgreSQL track was reviewed by reading the code. The Alembic revision is guarded by `has_table` and chained on `0092`. The `pg-migrations` CI job runs a real `upgrade head`; it was not run locally.

## Trend

New finding class for this surface; no prior report covers `agent_skill_gates`.
