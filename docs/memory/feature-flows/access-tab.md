# Feature: Access Tab — Manage Trinity Operators per Agent

## Overview
A read-only **Access** tab on the Agent Detail page that shows the roster of Trinity *operators* (platform users) who have access to a given agent — distinct from external *clients* who merely chat with it through channels. The tab is a typed view layered over the existing `agent_sharing` allow-list: each shared email is resolved against the `users` table, classifying it as an **active** operator (has a Trinity account) or a **pending** invite (allow-listed but no account yet). Add/remove operators reuse the existing `/share` and `/share/{email}` endpoints — this feature introduces **no new write path**.

> **Issue**: trinity-enterprise#17 · **Public PR**: #1317 · **Commit**: `72a7d396`

> **Relationship to Sharing**: This tab is the *operator-facing* slice of the same `agent_sharing` allow-list that powers [agent-sharing.md](agent-sharing.md) (which owns the write surface, channel bindings, access policy, and pending requests). Drawing the operator-vs-client line on the **read path** is what this issue owns; the strict client roster is the Sharing redesign (#18/#20).

## User Story
As an agent owner or admin, I want to see exactly which Trinity users (operators) have access to my agent — and whether each one is an active account or just an allow-listed invite — so that I can manage my human team separately from the external clients who chat through channels.

## Entry Points
- **UI**: `src/frontend/src/views/AgentDetail.vue:737` — the `access` tab is registered (only when `agent.can_share && !isSystem`), pushed *before* the `sharing` tab.
- **UI content**: `src/frontend/src/views/AgentDetail.vue:163-166` — renders `<AccessPanel :agent-name="agent.name" />` when `activeTab === 'access' && agent.can_share`.
- **API**: `GET /api/agents/{name}/access` — operator access roster (read-only).
- **Add/remove (reused)**: `POST /api/agents/{name}/share`, `DELETE /api/agents/{name}/share/{email}` — see [agent-sharing.md](agent-sharing.md).

---

## End-to-End Flow

```
AccessPanel.vue (load)
  → agentsStore.getAgentAccess(name)              stores/agents.js:423-429
    → GET /api/agents/{name}/access               routers/sharing.py:217-231
      → db.get_agent_operator_access(name)        database.py:472-473 (facade)
        → SharingMixin.get_agent_operator_access  db/agent_settings/sharing.py:139-182
          → SELECT agent_sharing LEFT OUTER JOIN users
            ON lower(users.email) = lower(agent_sharing.shared_with_email)
          → resolved row  → status="active"  (username/role/last_active)
          → unresolved    → status="pending" (username/role/last_active = None)
      ← List[AgentOperatorAccess]                  db_models.py:63-75
  ← operators[]  (rendered as roster with Active/Pending + role badges)
```

---

## Frontend Layer

### AccessPanel.vue (NEW — `src/frontend/src/components/AccessPanel.vue`, 169 lines)
- **Header** (lines 4-11): states the purpose — Trinity *operators* with access to this agent; explicitly points external channel *clients* to the **Sharing** tab.
- **Add operator form** (lines 14-34): single email input → `addOperator()` → `agentsStore.shareAgent(agentName, email)` (the reused `/share` write path), then reloads the roster.
- **Roster list** (lines 65-97): one row per operator showing `username || email`, an **Active**/**Pending** status pill (lines 75-79), an optional **role** badge (lines 81-84), and a "last active" timestamp when present. Pending rows render "Invited — no account yet" (line 87).
- **Remove** (lines 91-95): `removeOperator(email)` → `agentsStore.unshareAgent(agentName, email)` (reused `/share/{email}` delete), then reloads.
- **State** (lines 119-129): `load()` calls `agentsStore.getAgentAccess(props.agentName)`; skeleton/error/empty states handled. Reloads on `agentName` change (`watch`, line 168) and on mount (`onMounted(load)`, line 167).

### State Management (`src/frontend/src/stores/agents.js`)
- `getAgentAccess(name)` (lines 423-429) — `GET /api/agents/{name}/access`. The only new store action.
- `shareAgent(name, email)` (line 397) and `unshareAgent(name, email)` (line 406) — **reused unchanged** for add/remove.

### Tab wiring (`src/frontend/src/views/AgentDetail.vue`)
- Import `AccessPanel` (line 311).
- Tab registration (lines 735-740): inside the `agent.can_share && !isSystem` block, `access` is pushed first, then `sharing`, then `permissions`.

### SharingPanel.vue cross-references (`src/frontend/src/components/SharingPanel.vue`)
The Sharing tab now defers operator management to the Access tab in its copy:
- Line 17: "Identity proof alone does *not* grant access — manage operators on the **Access** tab."
- Line 63: dead-end-config warning — "Add operators on the **Access** tab, or enable Open access, to let people chat."

---

## Backend Layer

### Endpoint (`src/backend/routers/sharing.py:217-231`)
```python
@router.get("/{agent_name}/access", response_model=list[AgentOperatorAccess])
async def get_agent_access_endpoint(agent_name: OwnedAgentByName, request: Request):
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")
    return db.get_agent_operator_access(agent_name)
```
- **Auth**: `OwnedAgentByName` (owner or admin) — same gate as every other endpoint in `routers/sharing.py`.
- **Read-only**: no writes, no side effects, no WebSocket broadcast. (Writes happen only via the reused `/share` endpoints, lines 105-200.)
- `AgentOperatorAccess` imported from `database` at line 13.

### Response Model (`src/backend/db_models.py:63-75`)
```python
class AgentOperatorAccess(BaseModel):
    email: str
    username: Optional[str] = None
    role: Optional[str] = None
    last_active: Optional[str] = None  # ISO-Z last_login; None until first login
    status: str                        # 'active' | 'pending'
```
Re-exported through `database.py` (import at line 34) so both the router and the `db` facade share one definition.

### DB Facade (`src/backend/database.py:472-473`)
```python
def get_agent_operator_access(self, agent_name: str):
    return self._agent_ops.get_agent_operator_access(agent_name)
```

### Resolution Query (`src/backend/db/agent_settings/sharing.py:139-182`)
`SharingMixin.get_agent_operator_access(agent_name)` is the heart of the feature:
- `LEFT OUTER JOIN` from `agent_sharing` to `users` on `lower(users.email) == lower(agent_sharing.shared_with_email)` (case-insensitive on both sides), scoped to the agent, ordered `created_at DESC`.
- For each row: `resolved = (username is not None)`.
  - **resolved** → `status="active"`, with `username`, `role`, and `last_active` (= `users.last_login`) surfaced.
  - **unresolved** → `status="pending"`, with `username`/`role`/`last_active` all `None`.
- Returns a list of plain dicts (FastAPI coerces to `AgentOperatorAccess` via the route's `response_model`).

---

## The Operator-vs-Client Distinction (why it lives on the read path)

`agent_sharing` is the single unified cross-channel allow-list (Issue #311 — same email admits a user on web, Slack, and Telegram). It does not itself distinguish "operator" (a Trinity platform user) from "client" (an external person who only chats via a channel). This feature draws that line **at read time** by resolving each allow-list email against `users`:

- An email that **resolves** to a `users` row is a Trinity **operator** — they have an account, a role, and a login history.
- An email that **does not resolve** is a **pending** invite — allow-listed but no account yet (they may currently only be a channel client, or simply haven't logged in).

Because the line is drawn on the read path, **all** grants stay visible on the Access tab for now. A strict client-vs-operator split (routing non-user emails into a dedicated *client* roster, separate from operators) is deferred to the Sharing-side redesign (#18/#20). Keeping the classification in `get_agent_operator_access` — rather than splitting the underlying table — means no schema change and no second write path.

---

## Side Effects
None. `GET /api/agents/{name}/access` is a pure read. The add/remove buttons in `AccessPanel.vue` route through the existing `/share` endpoints, which carry their own side effects (auto-whitelist, `agent_shared`/`agent_unshared` WebSocket broadcasts, SEC-001 audit) — documented in [agent-sharing.md](agent-sharing.md).

## Error Handling
| Error Case | HTTP Status | Source |
|------------|-------------|--------|
| Agent not found (no owner) | 404 | `OwnedAgentByName` dependency |
| Not owner/admin | 403 | `OwnedAgentByName` dependency |
| Agent container missing | 404 | `get_agent_container(agent_name)` check (line 227-229) |

(Add/remove inherit the `/share` error table — 400 self-share, 409 already shared, 404 share not found.)

---

## Testing

Unit tests: `tests/unit/test_ent17_operator_access.py` (runs against the real initialized SQLite engine, so it exercises the actual outer-join read path; `ent17_`-prefixed fixture names avoid cross-test DB collisions).

- `test_operator_access_classifies_active_vs_pending` — shares two emails on one agent: one resolves to a `users` row (creator role, has `last_login`) → asserts `status="active"`, `username`, `role="creator"`, `last_active` populated; the other has no account → asserts `status="pending"` with `username`/`role`/`last_active` all `None`. Also shares a *third* email on a **different** agent and asserts it does **not** leak into the queried agent's roster (agent-scoping).
- `test_operator_access_empty_for_unshared_agent` — an agent with no shares returns `[]`.

### Manual
```bash
# Operator roster (owner/admin token)
curl http://localhost:8000/api/agents/my-agent/access \
  -H "Authorization: Bearer $TOKEN"
# → [{"email":"op@x.com","username":"op@x.com","role":"creator",
#     "last_active":"2026-06-20T09:00:00Z","status":"active"}, ...]

# Add an operator (reuses /share)
curl -X POST http://localhost:8000/api/agents/my-agent/share \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"email":"newop@x.com"}'

# Remove (reuses /share/{email})
curl -X DELETE http://localhost:8000/api/agents/my-agent/share/newop@x.com \
  -H "Authorization: Bearer $TOKEN"
```

---

## Related Flows
- **[agent-sharing.md](agent-sharing.md)** — owns the `agent_sharing` write surface (`/share`, `/share/{email}`, `/shares`), channel access policy, pending access requests, and channel bindings. The Access tab is the operator-facing read view over the same allow-list; add/remove delegate here.
- **[unified-channel-access-control.md](unified-channel-access-control.md)** — the cross-channel gate semantics (`email_has_agent_access`) that make `agent_sharing` admit users across web/Slack/Telegram.

## Status
Working — Access tab ships in PR #1317 (commit `72a7d396`).
