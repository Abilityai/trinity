# Feature: Execution Context Injection (#171)

## Overview
Every agent invocation receives a dynamic `## Execution Context` block in its
system prompt so it can self-calibrate behavior — knowing its mode (chat vs
headless task), trigger source, model, timeout budget, own name, permitted
collaborators, schedule metadata, and current timestamp. Implemented 2026-04-14
as an extension of the Trinity Prompt pipeline
([system-wide-trinity-prompt.md](system-wide-trinity-prompt.md)).

## Problem
Agents ran blind to operational metadata: they didn't know whether they were
in an interactive chat (where clarifying questions are fine) or an autonomous
task (where they should execute to completion), didn't know their timeout
budget, and didn't know who triggered them. This made it impossible for agents
to calibrate reasoning depth, plan work within a budget, or adjust behavior for
scheduled vs human-initiated runs.

## User Story
As an agent operator, I want the platform to tell each agent exactly which
mode and budget it is running in, so agents can behave correctly without
per-agent prompt engineering.

## Entry Points
- **Backend — chat (interactive)**: `src/backend/routers/chat.py` `/api/agents/{name}/chat`
- **Backend — task / schedule / mcp / agent / fan-out / paid / public**: `src/backend/services/task_execution_service.py` `execute_task()`
- **Backend — scheduler plumb-through**: `src/backend/routers/internal.py` `/api/internal/execute-task` (`InternalTaskExecutionRequest`)

## Frontend Layer

None — backend-only feature. The execution context block is assembled server-side and delivered to the agent container via the existing `system_prompt` field on `/api/chat` and `/api/task`. No UI surface, no user-visible controls beyond the operator kill-switch (which reuses the existing Settings page via the `trinity_execution_context_enabled` key).

## Context Block Format

```
## Execution Context

- **Mode**: chat | task
- **Triggered by**: schedule (source agent: 'orchestrator-1', user: 'alice@example.com')
- **Schedule**: 'daily-report' (cron: 0 9 * * *, next: 2026-04-15T09:00:00Z)
- **Attempt**: 1
- **Model**: claude-sonnet-4-6
- **Timeout**: 900s — plan to finish well within this budget
- **Agent**: oracle-1
- **Execution ID**: exec-8f21
- **Primary human**: A. Smith (role: head-of-ops) — proactive contact NOT yet permitted; do not message them unprompted
- **Stakeholders**: approver: B. Jones, viewer: C. Okafor
- **Collaborators**: researcher-1, writer-1
- **Timestamp**: 2026-04-14T09:00:00Z
- **Platform**: https://your-domain.com

Autonomous execution. Do not ask clarifying questions — execute to completion
and return your results. Plan your work to finish well within the timeout budget.
```

Fields that don't apply are omitted (chat mode has no timeout; non-scheduled
runs have no schedule block; empty collaborators list is omitted entirely).
The two assignment lines (trinity-enterprise#500) are omitted on any build with
no registered provider, and on every outside-facing turn — see
[role-assignments.md](role-assignments.md).

## Backend Layer

### Service: `services/platform_prompt_service.py`

Single source of truth for system prompt assembly (invariant #15). New surface:

| Symbol | Purpose |
|---|---|
| `ExecutionContext` (dataclass) | Typed per-invocation metadata. All fields optional. |
| `ExecutionContext.derive_mode(triggered_by)` | Maps trigger label → `"chat"` or `"task"`. |
| `build_execution_context(ctx) -> str` | Renders the markdown block. Returns `""` on any internal error so callers can fall back. |
| `compose_system_prompt(execution_context, caller_prompt, include_execution_context=True)` | Single composition entry point. Order: static platform instructions → execution context → caller prompt. |
| `is_execution_context_enabled()` | Operator kill-switch via `trinity_execution_context_enabled` setting (default true). |
| `_sanitize_field(value, max_len)` | Strips control chars, backticks, `##`, `---`; truncates. Applied to every user-controlled string before rendering. |

### Mode derivation

| `triggered_by` value | Mode |
|---|---|
| `chat`, `user`, `public`, `paid` | `chat` (interactive — agent may ask clarifying questions) |
| `schedule`, `mcp`, `agent`, `manual`, `fan_out`, other | `task` (autonomous — agent should execute and return) |

### Wiring

**`routers/chat.py` — interactive UI chat (mode=chat):**
```python
exec_ctx = ExecutionContext(
    agent_name=name,
    mode="chat",
    triggered_by=triggered_by,            # "chat" | "mcp" | "agent"
    source_user_email=current_user.email or current_user.username,
    source_agent_name=x_source_agent,
    source_mcp_key_name=x_mcp_key_name,
    model=request.model,
)
payload["system_prompt"] = compose_system_prompt(
    execution_context=exec_ctx,
    include_execution_context=is_execution_context_enabled(),
)
```

**`services/task_execution_service.py` — all headless execution paths:**
```python
exec_ctx = ExecutionContext(
    agent_name=agent_name,
    mode=ExecutionContext.derive_mode(triggered_by),
    triggered_by=triggered_by,
    source_user_email=source_user_email,
    source_agent_name=source_agent_name,
    source_mcp_key_name=source_mcp_key_name,
    model=model,
    timeout_seconds=timeout_seconds,
    attempt=attempt,
    schedule_name=(schedule_context or {}).get("name"),
    schedule_cron=(schedule_context or {}).get("cron"),
    schedule_next_run=(schedule_context or {}).get("next_run"),
)
effective_system_prompt = compose_system_prompt(
    execution_context=exec_ctx,
    caller_prompt=system_prompt,          # e.g. per-user memory block from public.py
    include_execution_context=is_execution_context_enabled(),
)
```

**`routers/internal.py` — scheduler plumb-through:**
`InternalTaskExecutionRequest` gained optional fields `schedule_name`,
`schedule_cron`, `schedule_next_run`, `attempt`. The dedicated scheduler may
pass them; when absent the schedule block is simply omitted (backwards
compatible — no scheduler update required to ship this).

### Auto-resolved fields

`compose_system_prompt` fills these from the DB / the seam when the caller
leaves them `None`, without mutating the caller's dataclass:

- `collaborators` → `db.get_permitted_agents(agent_name)` (from `agent_permissions` table)
- `platform_url` → `db.get_setting_value("public_chat_url")`
- `primary_user_display` / `role_id` / `stakeholders` / `proactive_consent` →
  `services/assignment_provider.resolve_assignment(agent_name, triggered_by)`
  (trinity-enterprise#500). All four come from ONE provider answer, so they are
  resolved together in a single call rather than per field.

Each lookup degrades to empty / omitted on failure.

**The `replace` guard is part of the contract, not an optimisation.** It reads
`if ctx.collaborators is None or ctx.platform_url is None or needs_assignment`.
Before `needs_assignment` joined it, a caller that pre-filled BOTH older
auto-filled fields skipped the whole block — and the assignment lines never
rendered, for that caller only, with nothing to notice. Any future auto-filled
field must extend this guard, or it inherits the same silent hole.

### Prompt Injection Defense

Schedule names and MCP key names are user-controlled and land verbatim in the
system prompt. Every rendered user-controlled string flows through
`_sanitize_field`, which:

1. Replaces control characters (`\x00–\x1f`, `\x7f`) — including newlines and tabs — with spaces
2. Replaces backticks with single quotes
3. Collapses `##` → `#` and `---` → `-` (neutralizes markdown heading injection)
4. Truncates to a per-field cap (80 chars default, 60 for collaborator names, 40 for timestamps, 200 for platform URL, 120 for a person's display name, 64 for a role id, 140 for a stakeholder entry — the assignment fields get their own bounds because the 80-char default truncates a real name mid-word)

The assignment fields add a second defence that is not about escaping: the rendered identity is a **display name**, and the provider answer contract has no email-shaped key at all. This block reaches anonymous public-link and paid turns, so an address here would be third-party PII in front of an outside audience — a new disclosure class, since `collaborators` are agent names and `source_user_email` is the caller's own address. The audience gate is the other half: `triggered_by` is forwarded to the provider, which suppresses on an outside audience and fails closed on a label it does not recognise.

Covered by unit tests:
- `test_schedule_name_injection_attempt_neutralized`
- `test_mcp_key_name_injection_attempt_neutralized`
- `test_sanitize_field_neutralizes_markdown_injection`

### Failure Semantics

Every call site wraps context building in try/except and falls back to the
existing `get_platform_system_prompt()` alone on failure. Rendering errors
return an empty block. **The context builder never fails a request.**

## Operator Kill-Switch

Setting: `trinity_execution_context_enabled` (default `"true"`). Setting to
`"false"` / `"0"` / `"off"` disables the context block globally without a
redeploy. Lives alongside the existing `trinity_prompt` operator setting.

## Database

No schema changes. Reads from existing:
- `agent_permissions` (collaborators)
- `settings` (`public_chat_url`, `trinity_execution_context_enabled`)

## Side Effects

- Increases the prompt token count of every invocation by ~150–250 tokens (the rendered context block plus mode guidance line). At low invocation rates the cost is negligible; at high rates the operator can disable via the kill-switch.
- Two read-only DB queries per invocation: `agent_permissions` lookup (collaborators) and `settings` lookup (`public_chat_url`). Both are local SQLite, sub-millisecond, indexed.
- No new WebSocket events, no new audit entries, no notifications.

## Error Handling

| Failure | Behavior |
|---|---|
| `build_execution_context` raises any exception | Caught inside the builder; returns `""`; the wrapping caller falls back to the base platform prompt. Logged at `WARNING`. |
| `compose_system_prompt` raises | Caller-side try/except in both `chat.py` and `task_execution_service.py` falls back to `get_platform_system_prompt()` alone. Logged at `WARNING`. |
| `db.get_permitted_agents` fails | `_resolve_collaborators` returns `[]`; collaborators line omitted. Logged at `DEBUG`. |
| `db.get_setting_value("public_chat_url")` fails | `_resolve_platform_url` returns `None`; platform line omitted. Logged at `DEBUG`. |
| Schedule lookup row missing | Schedule fields stay `None`; schedule block omitted entirely. No error. |
| `execution_id` is `None` (interactive chat) | Schedule lookup never runs (gated on `triggered_by == "schedule"`). |
| Adversarial schedule / MCP key name | Sanitizer neutralizes control chars, backticks, and markdown heading markers; truncates to the per-field cap. The string content is preserved but cannot inject structure. |

**Invariant**: the execution context block is best-effort metadata. Building it can never fail an agent invocation.

## Security Considerations

- **No new attack surface**: no new HTTP endpoints, no new auth boundaries. `/api/internal/execute-task` is already gated by `verify_internal_secret`; this PR only adds optional fields to its Pydantic request body.
- **Prompt injection (mitigated)**: schedule names and MCP key names are user-controlled and reach the agent system prompt verbatim. `_sanitize_field` strips `\x00–\x1f` / `\x7f` control characters (including newlines and tabs), replaces backticks with single quotes, collapses `##` → `#` and `---` → `-`, and truncates to per-field caps. Two adversarial unit tests (`test_schedule_name_injection_attempt_neutralized`, `test_mcp_key_name_injection_attempt_neutralized`) verify that crafted names cannot inject markdown structure.
- **No credential exposure**: only metadata (user email, MCP key *name*, agent name, schedule *name*) reaches the prompt. No secret values, no key contents, no `.env` data. The user email is already visible to the agent via existing chat-history mechanisms.
- **Auth pass-through**: the builder is called from already-authenticated paths. It performs no authorization decisions of its own.
- **Operator kill-switch**: setting `trinity_execution_context_enabled=false` (or `0` / `off`) disables the block globally without a redeploy. Useful as a fast incident response if an unforeseen issue surfaces in production.

## Out of Scope (Deferred)

- Context window size in prompt (needs agent-server cooperation)
- Parent execution chain for delegation trees (needs execution-tree query)
- Execution history summary for scheduled tasks
- Scheduler-side plumb-through of `schedule_name`/`cron`/`next_run` (separate PR; DB fallback ships today)

## Testing

`tests/test_platform_prompt_unit.py` — 41 unit tests covering:
- Sanitization (control chars, markdown heading injection, backticks, length cap, None/empty)
- Mode derivation for every trigger label
- Field rendering for chat/task/scheduled/agent/mcp/user triggers
- Collaborators (rendered / empty-omitted / truncated at MAX_COLLABORATORS)
- Prompt injection defense against adversarial schedule and MCP key names
- Builder error fallback (empty string on internal failure)
- `compose_system_prompt` ordering, collaborator auto-fill, kill-switch flag
- Operator kill-switch parsing for truthy/falsy setting values

`tests/unit/test_ent500_execution_context_fields.py` — the assignment fields:
rendering and ordering, the dedicated bounds, the stakeholder cap, the
resolve-exactly-once property, the pre-filled-caller `replace`-guard path, and a
raising provider still yielding the full block (platform prompt included).

`tests/unit/test_ent500_assignment_provider.py` — the seam's three degrade paths
(no provider / raising / malformed shape) and the guard-file membership assert.

`tests/unit/test_ent500_public_turn_no_pii.py` — an outside turn renders no
assignment line, an inside turn still does, an unrecognised label is suppressed,
and the outside-facing routers use only suppressed trigger labels.

Run: `.venv/bin/python -m pytest tests/unit/test_platform_prompt_unit.py tests/unit/test_ent500_*.py -v`

## Related Flows
- [system-wide-trinity-prompt.md](system-wide-trinity-prompt.md) — parent feature (admin-configurable platform instructions)
- [task-execution-service.md](task-execution-service.md) — primary wiring site
- [parallel-headless-execution.md](parallel-headless-execution.md) — headless task path
- [role-assignments.md](role-assignments.md) — where the assignment fields come from (trinity-enterprise#500)
