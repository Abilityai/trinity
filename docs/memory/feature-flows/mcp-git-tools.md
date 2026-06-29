# Feature: MCP Git Tools + Request-ID Audit Correlation (#905)

## Overview

Exposes the per-agent git surface (`github-sync.md`) as **direct, deterministic
MCP tools** so an orchestrator can run status / sync / log / pull / sync-state /
reset **without** round-tripping through `chat_with_agent` and burning an LLM
turn. Conflicts stay LLM-mediated: a 409 surfaces the conflict type verbatim and
points the caller back to `chat_with_agent` to resolve.

The same change closes an **audit-correlation gap**: an MCP tool call and the
backend operation it triggers used to land as two **unjoinable** audit rows. Each
git tool now mints a per-call `requestId`, stamps it on its own `mcp_operation`
row **and** forwards it as `X-Request-ID` on the proxied backend call; the backend
adopts the header onto the resulting `git_operation` row, so
`GET /api/audit-log?request_id=<id>` returns **both rows end-to-end**.

Two previously-dropped fields on the `mcp_operation` row are also recovered for
**every** tool (not just git): `target_id` (the agent the tool acted on, resolved
from params) and `request_id`.

## User Story

As an orchestrator (head agent / external Claude Code), I want to drive an
agent's git lifecycle through deterministic MCP tools and later trace a single
git action across **both** the MCP and backend audit logs, so I can automate
sync/recovery without an LLM turn and investigate exactly what a tool did.

## Entry Points

| Type | Location | Description |
|------|----------|-------------|
| **MCP tool** | `get_git_status` | Live status — branch, remote, last commit, changed/untracked files, sync_status. Read-only. |
| **MCP tool** | `git_sync` | Stage + commit + push to working branch. Owner-only. `strategy: normal\|pull_first\|force_push`. |
| **MCP tool** | `get_git_log` | Recent commits (limit clamped 1–100, default 10). Read-only. |
| **MCP tool** | `git_pull` | Pull from GitHub. `strategy: clean\|stash_reapply\|force_reset`. Owner + shared accessors. |
| **MCP tool** | `get_git_sync_state` | Persisted `agent_sync_state` row (#389) — last status, consecutive failures, ahead/behind. Read-only. |
| **MCP tool** | `reset_to_main_preserve_state` | ⚠️ DESTRUCTIVE recovery (#384) — adopt `origin/main`, force-with-lease. Owner-only. |
| **API** (admin) | `GET /api/audit-log?request_id=<id>` | Join an MCP `mcp_operation` row to the backend `git_operation` row it triggered. |

The six tools proxy the existing backend git endpoints under
`/api/agents/{name}/git/*` — no new backend route was added; the MCP surface and
the request-id correlation are the only new behaviour.

---

## Architecture Flow

```
┌──────────────────────────────────────────────────────────────┐
│ Orchestrator (head agent / Claude Code)                      │
│  calls git_sync(agent_name="acme", strategy="normal")        │
└───────────────────────────┬──────────────────────────────────┘
                            │ MCP (Streamable HTTP, Bearer key)
                            ▼
┌──────────────────────────────────────────────────────────────┐
│ MCP server  tools/git.ts                                     │
│  1. checkAgentAccess()  — {self} ∪ permitted gate (scope=agent)│
│  2. requestId = randomUUID()                                 │
│  3. context.requestId = requestId   ← stamped BEFORE await    │
│  4. client.gitSync(name, body, requestId)                    │
└───────────────────────────┬──────────────────────────────────┘
              ┌─────────────┴───────────────┐
              ▼                             ▼
┌───────────────────────────┐   ┌──────────────────────────────┐
│ client.ts request()       │   │ audit.ts withAudit() wrapper  │
│  header X-Request-ID = rid │   │  reads context.requestId      │
│  POST /api/agents/acme/    │   │  + resolveTargetId(params)    │
│       git/sync             │   │  → mcp_operation row          │
└───────────────┬───────────┘   │    {target_id, request_id}    │
                ▼               └──────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│ Backend routers/git.py                                       │
│  add_request_id middleware adopts incoming X-Request-ID       │
│  → request.state.request_id                                  │
│  _audit_git(...) → git_operation row {request_id} (success    │
│                    AND failure/409/500 paths)                 │
└──────────────────────────────────────────────────────────────┘

           Both rows now share request_id  →
           GET /api/audit-log?request_id=<id> returns the pair
```

---

## Implementation

### MCP server (TypeScript)

- **`src/mcp-server/src/tools/git.ts`** (new) — `createGitTools(client, requireApiKey)` returns the six tools.
  - **`run()`** shared runner: gates the target agent (`checkAgentAccess`), mints `requestId = randomUUID()`, **stamps it on `context.requestId` BEFORE the await** (so the `mcp_operation` row carries it even when the call throws), runs the proxied call, and normalizes errors. A 409 `ApiError` is returned as a structured conflict object (`{error:"conflict", status:409, conflict_type, conflict_class, detail, hint}`) with a hint back to `chat_with_agent`; everything else → `{error}`.
  - **`checkAgentAccess()`** mirrors `operator_queue.ts` / `executions.ts`: `system` → allow; `user` → allow (backend already owner-scopes); `agent` → `{self}` or a target in `getPermittedAgents(caller)`. This gate lives HERE because the backend resolves an agent-scoped key to its **owner** and does NOT apply `agent_permissions` (architecture §5).
  - **`get_git_log`** clamps `limit` to 1–100 a second time in `execute` (defense-in-depth — the agent server shells `git log -${limit}`).
- **`src/mcp-server/src/client.ts`**
  - **`ApiError`** (new, exported) — typed error thrown by `request()` on non-2xx. Carries `.status`, `.conflictType`, `.conflictClass` (from `X-Conflict-Type`/`X-Conflict-Class`). `.message` keeps the historical `API error (<status>): <body>` shape so existing callers/tests still match; `instanceof Error` holds.
  - **`request()`** gains an optional `requestId` param → sets the `X-Request-ID` header; threaded through the reauth-retry recursion.
  - Six proxy methods: `getGitStatus`, `gitSync`, `getGitLog`, `gitPull`, `getGitSyncState`, `resetToMainPreserveState` — each takes `requestId?` last.
- **`src/mcp-server/src/audit.ts`**
  - **`ToolCallContext`** (new, exported) — `{session?: McpAuthContext, requestId?: string}` replaces the old inline `{session?}` context type in `withAudit`. A tool stamps `requestId` here; the wrapper reads it **after** `execute` (so a tool that mints it mid-call is captured).
  - **`resolveTargetId(params)`** (new, exported) — returns `params.agent_name ?? params.name` when a string, else `undefined`. Defensive so non-agent tools log no target.
  - `logToolCall(...)` gains `targetId?` + `requestId?`, setting `target_type:"agent"`/`target_id`/`request_id` on the `mcp_operation` entry — both fields were previously dropped ("we don't have params here").
- **`src/mcp-server/src/server.ts`** — registers `createGitTools(client, requireApiKey)` in the tool group list.

### Backend (Python)

- **`src/backend/routers/git.py`**
  - **`_audit_git(*, action, request, current_user, agent_name, success, details)`** (new helper) — emits one `GIT_OPERATION` row, forwarding `request.state.request_id`. Best-effort (the audit service swallows its own errors).
  - **Symmetric auditing**: `sync_to_github`, `pull_from_github`, and `reset_to_main_preserve_state` now call `_audit_git` on **every** exit path — success AND the business-failure paths (409 conflict / 400 / 500). Previously only the success path logged, so a mutating/destructive op that hit a conflict left no audit trace. `reset_to_main_preserve_state` gained a `request: Request` param to do this.
- **`src/backend/db/audit.py`** — `_filter_conditions` / `get_audit_entries` / `count_audit_entries` gain a `request_id` filter (`audit_log.c.request_id == request_id`).
- **`src/backend/routers/audit_log.py`** — `GET /api/audit-log` gains a `request_id` query param threaded into the filter dict.
- **`src/backend/routers/internal.py`** — `POST /api/internal/audit` passes `request.request_id` through to the service (the MCP fire-and-forget write path).
- **`src/backend/models.py`** — `InternalAuditRequest` gains `request_id: Optional[str]` so the MCP `mcp_operation` write carries the correlation id.

---

## Access Control Summary

| Tool | Backend dependency | Agent-scoped key (non-owner) |
|------|--------------------|------------------------------|
| `get_git_status` / `get_git_log` / `get_git_sync_state` | `AuthorizedAgentByName` | read (self ∪ permitted) |
| `git_pull` | shared-accessor allowed | pull allowed (self ∪ permitted) |
| `git_sync` | `OwnedAgentByName` | **denied** (owner-only) — read+pull only |
| `reset_to_main_preserve_state` | `OwnedAgentByName` | **denied** (owner-only) |

The `{self} ∪ permitted` MCP-layer gate (in `git.ts`) is what restricts an
agent-scoped key from reaching siblings; the backend's `OwnedAgentByName` on the
mutating endpoints is the second layer that downgrades a shared key to read+pull.

---

## Testing

- **`src/mcp-server/src/tools/git.test.ts`** (new) — tool behaviour: requestId minting + stamping on `context.requestId`, X-Request-ID forwarding, 409 → structured conflict object with `chat_with_agent` hint, agent-to-agent gate (self / permitted / denied), `get_git_log` limit clamping, owner-only surface.
- **`src/mcp-server/src/audit.test.ts`** (new) — `resolveTargetId` (`agent_name` / `name` / neither), `withAudit` passes `targetId` + `context.requestId` through to the `mcp_operation` entry.
- **`tests/test_audit_log_unit.py`** (updated) — `request_id` filter on `get_audit_entries` / `count_audit_entries`; `InternalAuditRequest.request_id` round-trips through `POST /api/internal/audit`; the `_audit_git` symmetric success/failure rows for sync/pull/reset.

---

## Related Flows

- [github-sync.md](github-sync.md) — the underlying git endpoints these tools proxy (source/working-branch modes, sync/pull strategies, reset-preserve-state).
- [git-sync-health.md](git-sync-health.md) — the `agent_sync_state` row `get_git_sync_state` reads.
- [audit-trail.md](audit-trail.md) — the audit subsystem; this flow adds the `request_id` correlation + recovers `target_id`/`request_id` on the `mcp_operation` row.
- [mcp-orchestration.md](mcp-orchestration.md) — the MCP server surface these six tools join.
- [persistent-state-allowlist.md](persistent-state-allowlist.md) — the `.trinity/persistent-state.yaml` allowlist `reset_to_main_preserve_state` snapshots.
