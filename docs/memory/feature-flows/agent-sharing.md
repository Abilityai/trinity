# Feature: Agent Sharing

## Overview
Collaboration feature enabling agent owners to share agents with team members via email. Supports three access levels: Owner (full control), Shared (limited access), and Admin (full control over all agents). The Sharing tab now includes Team Sharing, Channel Access Policy, Pending Access Requests, Slack/Telegram channel bindings, and Public Links in a unified interface.

> **Cross-channel allow-list (Issue #311)**: As of 2026-04-12, `agent_sharing` is the **unified cross-channel allow-list**, not a web-only construct. The same email shared on this page admits that user across Telegram, Slack, and web public links whenever the agent's access policy requires a verified email. See [unified-channel-access-control.md](unified-channel-access-control.md) for the gate semantics and channel adapter details.

## User Story
As an agent owner, I want to share my agents with team members so that they can use the agents from any channel (web, Slack, Telegram) without having full ownership permissions.

## Entry Points
- **UI**: `src/frontend/src/views/AgentDetail.vue:429-432` - Sharing tab (owners only, hidden for system agents)
- **API**: `POST /api/agents/{name}/share` - Share agent
- **API**: `DELETE /api/agents/{name}/share/{email}` - Remove share
- **API**: `GET /api/agents/{name}/shares` - List shares
- **API**: `GET /api/agents/{name}/access-policy` - Get channel access policy (Issue #311)
- **API**: `PUT /api/agents/{name}/access-policy` - Update channel access policy
- **API**: `GET /api/agents/{name}/access-requests?status=pending` - List pending access requests
- **API**: `POST /api/agents/{name}/access-requests/{id}/decide` - Approve/deny request

---

## Frontend Layer

### SharingPanel.vue (`src/frontend/src/components/SharingPanel.vue`)

The sharing UI is implemented as a dedicated component with multiple stacked sections.

**Component Structure** (~370 lines total):
- Lines 3-74: **Channel Access Policy** section (Issue #311) — `require_email`, `open_access` checkboxes + Pending Access Requests list with Approve/Deny buttons
- Lines 78-152: **Team Sharing** section (header, form, shared users list with proactive toggle — the unified allow-list)
- Lines 157-158: Embedded `SlackChannelPanel`
- Lines 163-164: Embedded `TelegramChannelPanel`
- Lines 169-170: Embedded `PublicLinksPanel`
- Lines 181-183: Imports for the embedded channel panels

**Team Sharing Section** (lines 78-152):
```vue
<div>
  <h3 class="text-lg font-medium ...">Team Sharing</h3>
  <!-- Share form (lines 86-105) -->
  <!-- Shared users list with proactive toggle (lines 116-152) -->
</div>
```

Each shared user row includes:
- User avatar and email
- **Proactive toggle** (Issue #376) — enables `allow_proactive` flag for proactive messaging
- Remove button

**Component Props** (lines 185-194):
```javascript
const props = defineProps({
  agentName: { type: String, required: true },
  shares: { type: Array, default: () => [] }
})
```

**Channel Access Policy & Access Requests (Issue #311)** — wired via direct axios calls in `<script setup>` (no composable):
```javascript
// State (lines 226-230)
const policy = ref({ require_email: false, open_access: false })
const policyLoading = ref(false)
const pendingRequests = ref([])
const decisionLoading = ref(null)

// loadPolicy        — GET  /api/agents/{name}/access-policy
// updatePolicy      — PUT  /api/agents/{name}/access-policy   (merges partial change)
// loadAccessRequests — GET  /api/agents/{name}/access-requests?status=pending
// decideRequest      — POST /api/agents/{name}/access-requests/{id}/decide  { approve }
// formatRequestedAt  — local date formatting helper

// Refresh both on agent change (lines 305-308)
watch(() => props.agentName, async (name) => {
  if (!name) return
  await Promise.all([loadPolicy(), loadAccessRequests()])
}, { immediate: true })
```

After `decideRequest(req, true)` succeeds, `loadAccessRequests()` is re-run and `loadAgent()` is emitted so the Team Sharing list reflects the newly-added email.

### Composable (`src/frontend/src/composables/useAgentSharing.js`)

Encapsulates sharing logic with reactive state management.

**shareWithUser** (lines 13-41):
```javascript
const shareWithUser = async () => {
  const result = await agentsStore.shareAgent(agentRef.value.name, shareEmail.value.trim())
  shareMessage.value = { type: 'success', text: `Agent shared with ${shareEmail.value.trim()}` }
  await loadAgent()
}
```

**removeShare** (lines 43-59):
```javascript
const removeShare = async (email) => {
  await agentsStore.unshareAgent(agentRef.value.name, email)
  showNotification(`Sharing removed for ${email}`, 'success')
  await loadAgent()
}
```

### State Management (`src/frontend/src/stores/agents.js:310-332`)
```javascript
// Agent Sharing Actions
async shareAgent(name, email) {
  const authStore = useAuthStore()
  const response = await axios.post(`/api/agents/${name}/share`,
    { email },
    { headers: authStore.authHeader }
  )
  return response.data
}

async unshareAgent(name, email) {
  const authStore = useAuthStore()
  await axios.delete(`/api/agents/${name}/share/${encodeURIComponent(email)}`, {
    headers: authStore.authHeader
  })
}

async getAgentShares(name) {
  const authStore = useAuthStore()
  const response = await axios.get(`/api/agents/${name}/shares`, {
    headers: authStore.authHeader
  })
  return response.data
}
```

### Tab Visibility (`src/frontend/src/views/AgentDetail.vue:506-509`)

The Sharing tab is only shown to users who can share and hidden for system agents:
```javascript
if (agent.value?.can_share && !isSystem) {
  tabs.push({ id: 'sharing', label: 'Sharing' })
  tabs.push({ id: 'permissions', label: 'Permissions' })
}
```

> **Note (2026-02-18)**: The "Public Links" tab was consolidated into the "Sharing" tab. SharingPanel.vue now renders PublicLinksPanel at the bottom of the panel, separated by a divider.

---

## Backend Layer

### Endpoints (`src/backend/routers/sharing.py`)

All endpoints are gated by `OwnedAgentByName` (owner or admin).

| Line | Endpoint | Method | Purpose |
|------|----------|--------|---------|
| 53-94 | `/api/agents/{agent_name}/share` | POST | Share agent with email |
| 97-119 | `/api/agents/{agent_name}/share/{email}` | DELETE | Remove share |
| 122-133 | `/api/agents/{agent_name}/shares` | GET | List shares |
| 140-146 | `/api/agents/{agent_name}/access-policy` | GET | Get `{require_email, open_access}` (Issue #311) |
| 149-157 | `/api/agents/{agent_name}/access-policy` | PUT | Update policy (delegates to `db.set_access_policy`) |
| 160-168 | `/api/agents/{agent_name}/access-requests` | GET | List requests, defaults `status=pending` |
| 171-221 | `/api/agents/{agent_name}/access-requests/{request_id}/decide` | POST | `{approve: bool}` — on approve, idempotently inserts into `agent_sharing` via `db.share_agent`, auto-whitelists email when email auth is enabled, and broadcasts `agent_shared` over WebSocket |

### Authorization via Dependencies (`src/backend/dependencies.py:258-285`)

The sharing router uses `OwnedAgentByName` dependency for authorization:
```python
def get_owned_agent_by_name(
    agent_name: str = Path(...),
    current_user: User = Depends(get_current_user)
) -> str:
    """Validates user owns or can share an agent."""
    if not db.get_agent_owner(agent_name):
        raise HTTPException(404, "Agent not found")
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(403, "Owner access required")
    return agent_name

OwnedAgentByName = Annotated[str, Depends(get_owned_agent_by_name)]
```

### Share Agent (`routers/sharing.py:23-64`)
```python
@router.post("/{agent_name}/share", response_model=AgentShare)
async def share_agent_endpoint(
    agent_name: OwnedAgentByName,  # Authorization via dependency
    share_request: AgentShareRequest,
    request: Request,
    current_user: CurrentUser
):
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Prevent self-sharing
    current_user_data = db.get_user_by_username(current_user.username)
    current_user_email = (current_user_data.get("email") or "") if current_user_data else ""
    if current_user_email and current_user_email.lower() == share_request.email.lower():
        raise HTTPException(status_code=400, detail="Cannot share an agent with yourself")

    share = db.share_agent(agent_name, current_user.username, share_request.email)
    if not share:
        raise HTTPException(status_code=409, detail=f"Agent is already shared with {share_request.email}")

    # Auto-add email to whitelist if email auth is enabled (Phase 12.4)
    from config import EMAIL_AUTH_ENABLED
    email_auth_setting = db.get_setting_value("email_auth_enabled", str(EMAIL_AUTH_ENABLED).lower())
    if email_auth_setting.lower() == "true":
        try:
            db.add_to_whitelist(share_request.email, current_user.username, source="agent_sharing", default_role="user")  # #314: chat-only grant
        except Exception:
            pass  # Already whitelisted or error - continue anyway

    if manager:
        await manager.broadcast(json.dumps({
            "event": "agent_shared",
            "data": {"name": agent_name, "shared_with": share_request.email}
        }))

    return share
```

### Remove Share (`routers/sharing.py:67-89`)
```python
@router.delete("/{agent_name}/share/{email}")
async def unshare_agent_endpoint(
    agent_name: OwnedAgentByName,
    email: str,
    request: Request,
    current_user: CurrentUser
):
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    success = db.unshare_agent(agent_name, current_user.username, email)
    if not success:
        raise HTTPException(status_code=404, detail=f"No sharing found for {email}")

    if manager:
        await manager.broadcast(json.dumps({
            "event": "agent_unshared",
            "data": {"name": agent_name, "removed_user": email}
        }))

    return {"message": f"Sharing removed for {email}"}
```

### Access Control in Get Agent (`routers/agents.py:187-201`)

Access levels are computed when fetching a single agent:
```python
owner = db.get_agent_owner(agent_name)
agent_dict["owner"] = owner["owner_username"] if owner else None
agent_dict["is_owner"] = owner and owner["owner_username"] == current_user.username
agent_dict["is_shared"] = not agent_dict["is_owner"] and not is_admin and \
                           db.is_agent_shared_with_user(agent_name, current_user.username)
agent_dict["is_system"] = owner.get("is_system", False) if owner else False
agent_dict["can_share"] = db.can_user_share_agent(current_user.username, agent_name)
agent_dict["can_delete"] = db.can_user_delete_agent(current_user.username, agent_name)

if agent_dict["can_share"]:
    shares = db.get_agent_shares(agent_name)
    agent_dict["shares"] = [s.dict() for s in shares]
```

### Agent List Access Control (`services/agent_service/helpers.py:83-153`)

Uses optimized batch query to avoid N+1 problem:
```python
def get_accessible_agents(current_user: User) -> list:
    # Single batch query for ALL agent metadata
    all_metadata = db.get_all_agent_metadata(user_email)

    for agent in all_agents:
        metadata = all_metadata.get(agent_name)
        is_owner = owner_username == current_user.username
        is_shared = bool(metadata.get("is_shared_with_user"))

        # Skip if no access (not admin, not owner, not shared)
        if not (is_admin or is_owner or is_shared):
            continue

        agent_dict["is_owner"] = is_owner
        agent_dict["is_shared"] = is_shared and not is_owner and not is_admin
```

---

## Database Layer (`src/backend/db/agents.py`)

Database operations are in the `AgentOperations` class.

### Schema
```sql
CREATE TABLE IF NOT EXISTS agent_sharing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    shared_with_email TEXT NOT NULL,
    shared_by_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    allow_proactive INTEGER DEFAULT 0,    -- proactive-toggle (Issue #376)
    UNIQUE(agent_name, shared_with_email),
    FOREIGN KEY (shared_by_id) REFERENCES users(id)
)
```

### Operations (`db/agent_settings/sharing.py` — `SharingMixin`, composed into `AgentOperations`)

| Method | Line | Purpose |
|--------|------|---------|
| `share_agent()` | 30-73 | Create share record |
| `unshare_agent()` | 75-88 | Remove share record |
| `get_agent_shares()` | 90-102 | List shares for agent |
| `get_shared_agents()` | 104-119 | Get agents shared with user |
| `is_agent_shared_with_email()` | 121-131 | Direct email lookup, no user record needed (Issue #311) |
| `email_has_agent_access()` | 133-148 | Composite gate: owner / admin / `agent_sharing` (used by channel router gate, Issue #311) |
| `is_agent_shared_with_user()` | 150-166 | Access check by username |
| `can_user_share_agent()` | 168-178 | Authorization check |
| `delete_agent_shares()` | 180-186 | Cascade delete shares |

Both `is_agent_shared_with_email` and `email_has_agent_access` are exposed on the `db` facade.

Access-policy storage lives on `agent_ownership` via `AccessPolicyMixin` (`db/agent_settings/access_policy.py`); access requests live in their own table via `db/access_requests.py`.

### Share Agent (`db/agent_settings/sharing.py`)

`share_agent` now normalizes the email (`.strip().lower()`) and, within the same transaction as the `agent_sharing` insert, deletes any stale pending `access_requests` row for the same `(agent_name, email)` so the owner's Pending list reflects reality after a manual Team Share add (#446). Approved/denied rows are left untouched as audit trail.

```python
def share_agent(self, agent_name, owner_username, share_with_email) -> Optional[AgentShare]:
    owner = self._user_ops.get_user_by_username(owner_username)
    if not owner:
        return None
    if not self.can_user_share_agent(owner_username, agent_name):
        return None

    normalized_email = (share_with_email or "").strip().lower()
    if not normalized_email:
        return None

    owner_email = (owner.get("email") or "").strip().lower()
    if owner_email and owner_email == normalized_email:
        return None  # self-share

    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO agent_sharing (agent_name, shared_with_email, shared_by_id, created_at) VALUES (?, ?, ?, ?)",
                (agent_name, normalized_email, owner["id"], now),
            )
            # #446: clear any stale pending access_request for same (agent, email)
            cursor.execute(
                "DELETE FROM access_requests WHERE agent_name = ? AND email = ? AND status = 'pending'",
                (agent_name, normalized_email),
            )
            conn.commit()
            return AgentShare(...)
        except sqlite3.IntegrityError:
            return None  # already shared
```

### Cascade Delete (`db/agents.py:117-128`)
When an agent is deleted, sharing records AND pending access requests are removed via `delete_agent_ownership()`:
```python
def delete_agent_ownership(self, agent_name: str) -> bool:
    """Remove agent ownership record, all sharing records, and pending access requests."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # Delete sharing records first (cascade)
        cursor.execute("DELETE FROM agent_sharing WHERE agent_name = ?", (agent_name,))
        # Delete access requests (issue #311)
        cursor.execute("DELETE FROM access_requests WHERE agent_name = ?", (agent_name,))
        # Delete ownership record
        cursor.execute("DELETE FROM agent_ownership WHERE agent_name = ?", (agent_name,))
```

---

## Access Levels

| Level | View | Start/Stop | Delete | Share | Git Pull | Git Sync/Init |
|-------|------|------------|--------|-------|----------|---------------|
| Owner | Yes | Yes | Yes | Yes | Yes | Yes |
| Shared | Yes | Yes | No | No | Yes | No |
| Admin | Yes | Yes | Yes (non-system) | Yes | Yes | Yes |

> **Note (2026-01-30)**: Git Pull was changed from Owner-only to Authorized (owner/shared/admin). See [github-sync.md](github-sync.md) for details.

---

## Side Effects

### WebSocket Broadcasts
| Event | Payload |
|-------|---------|
| `agent_shared` | `{name, shared_with}` |
| `agent_unshared` | `{name, removed_user}` |

### Auto-Whitelist (Phase 12.4)
When email auth is enabled, shared emails are automatically added to the whitelist (`routers/sharing.py:44-56`):
```python
if email_auth_setting.lower() == "true":
    try:
        db.add_to_whitelist(share_request.email, current_user.username, source="agent_sharing", default_role="user")  # #314: chat-only grant
    except Exception:
        pass  # Already whitelisted or error - continue anyway
```

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | "Agent not found" |
| Not authorized to share | 403 | "Owner access required" |
| Share not found | 404 | "No sharing found for {email}" |
| Already shared | 409 | "Agent is already shared with {email}" |
| Self-sharing | 400 | "Cannot share an agent with yourself" |

---

## Security Considerations

1. **Email-Based Sharing**: Shares with users who haven't registered yet (future-proofing)
2. **Owner-Only Sharing**: Only owners and admins can share (`OwnedAgentByName` dependency)
3. **Cascade Delete**: Shares removed when agent deleted
4. **Access Validation**: Every endpoint validates via `OwnedAgentByName` dependency
5. **Auto-Whitelist**: When email auth is enabled, shared emails are auto-added to whitelist
6. **System Agent Protection**: System agents cannot be shared (tab hidden in UI)

---

## Testing

### Manual Testing
```bash
# Share an agent
curl -X POST http://localhost:8000/api/agents/my-agent/share \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email": "colleague@example.com"}'

# List shares
curl http://localhost:8000/api/agents/my-agent/shares \
  -H "Authorization: Bearer $TOKEN"

# Remove share
curl -X DELETE http://localhost:8000/api/agents/my-agent/share/colleague@example.com \
  -H "Authorization: Bearer $TOKEN"

# Verify shared user can see agent
curl http://localhost:8000/api/agents \
  -H "Authorization: Bearer $SHARED_USER_TOKEN"
```

---

## Status
Working - Agent sharing fully functional with email-based collaboration

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-04-22 | **#446 — team-share gate hardening + Sharing UX**. `share_agent` now deletes stale pending `access_requests` rows atomically with the share insert (manual Team Share now clears the owner's Pending list). Email normalization (`.strip().lower()`) added in `share_agent`, `is_agent_shared_with_email`, and `email_has_agent_access` as defense-in-depth. `SharingPanel.vue` restructured to distinguish **Identity Proof** (`require_email`) from **Authorization** (Team Sharing allow-list + approval queue); dead-end warning shown when `require_email=true && !open_access && shares.length===0`. New unit test file: `tests/test_team_share_gate_unit.py`. Canonical gate semantics continue to live in [unified-channel-access-control.md](unified-channel-access-control.md). |
| 2026-04-12 | **Issue #311 — unified cross-channel access control**: `agent_sharing` is now the cross-channel allow-list (web + Telegram + Slack). Added 4 endpoints to `routers/sharing.py` for access policy (`require_email`, `open_access`) and pending access requests (approve/deny). `SharingPanel.vue` gained a Channel Access Policy section + Pending Access Requests list (direct axios, no composable). New `SharingMixin` helpers: `is_agent_shared_with_email`, `email_has_agent_access`. `delete_agent_ownership` now also cascades `access_requests`. Canonical primitive lives in [unified-channel-access-control.md](unified-channel-access-control.md). |
| 2026-02-18 | **Public Links tab consolidated**: Public Links tab removed from AgentDetail.vue. SharingPanel.vue now includes PublicLinksPanel as embedded component (lines 79-83, 92). Updated tab visibility line numbers (506-509). Single "Sharing" tab now contains both Team Sharing and Public Links sections. |
| 2026-01-30 | **Git Pull permission update**: Added Git Pull and Git Sync/Init columns to Access Levels table. Shared users can now pull from GitHub (was owner-only). |
| 2026-01-23 | **Full verification**: Updated to use SharingPanel.vue component (not inline in AgentDetail.vue). Updated line numbers for routers/sharing.py (23-64, 67-89, 92-103). Added useAgentSharing.js composable documentation. Updated db/agents.py line numbers for sharing methods. Added OwnedAgentByName dependency documentation from dependencies.py. Documented tab visibility logic at AgentDetail.vue:428-432. Updated helpers.py reference for batch metadata query. |
| 2025-12-30 | Flow verification: Updated line numbers for routers/sharing.py. Updated database layer to reference db/agents.py. Added auto-whitelist feature note. |

---

## See Also

- **[access-tab.md](access-tab.md)** — The Agent Detail **Access** tab: a read-only roster of Trinity *operators* (platform users) resolved from this same `agent_sharing` allow-list (active operator vs pending invite). Add/remove there reuse the `/share` + `/share/{email}` endpoints documented here (trinity-enterprise#17).
- **[unified-channel-access-control.md](unified-channel-access-control.md)** — Canonical reference for the cross-channel gate, `email_has_agent_access` semantics, and how Telegram/Slack/web public links resolve a verified email and consult `agent_sharing`. This flow only documents the sharing UX and API surface; gate implementation lives there.

## Related Flows

- **Upstream**: Authentication (user identity)
- **Downstream**: Public Agent Links (embedded in same tab via PublicLinksPanel), Telegram Integration, Slack Integration (all consume the unified allow-list)
- **Related**: Agent Lifecycle (delete cascades shares + access requests), MCP Orchestration (agent-to-agent access control), Email Authentication (auto-whitelist)
