# Feature: 4-Tier Role Model

## Overview
Expands the user role system from 2 tiers (admin/user) to 4 tiers: admin > creator > operator > user. Adds server-side role enforcement via a `require_role()` dependency factory, new user management API endpoints, and a Settings UI section for admins to change user roles.

**Implementation Status**: Complete (ROLE-001, GitHub Issue #143)

## Revision History
| Date | Changes |
|------|---------|
| 2026-03-20 | Initial documentation |

## User Story
As a platform admin, I want to assign fine-grained roles to users so that I can control who can create agents, who can only operate existing agents, and who can only access public links.

As a whitelisted user signing up via email, I get the `creator` role by default so that I can immediately create and manage agents without requiring additional admin action.

---

## Role Table

| Role | Entry Path | Can Create Agents | Intent |
|------|-----------|-------------------|--------|
| admin | Password login | Yes | Full platform control |
| creator | Whitelisted email signup | Yes | Create and manage own agents |
| operator | Whitelisted email, role manually set | No | Run existing agents only |
| user | Public links | No | Public link interactions only |

**Hierarchy** (lowest to highest): `user` < `operator` < `creator` < `admin`

---

## Entry Points
- **UI**: `src/frontend/src/views/Settings.vue:796-866` - User Management section with role dropdowns
- **API**: `GET /api/users` - List all users (admin-only)
- **API**: `PUT /api/users/{username}/role` - Change a user's role (admin-only)

---

## Architecture: Role Enforcement Flow

```
┌──────────────────────────────────────────────────────────────────┐
│                    4-Tier Role Model                              │
└──────────────────────────────────────────────────────────────────┘

  Role Hierarchy (index in ROLE_HIERARCHY list)
┌──────────────────────────────────────────────────┐
│  dependencies.py                                 │
│  ROLE_HIERARCHY = ["user", "operator",           │
│                    "creator", "admin"]           │
│                                                  │
│  index 0: user    — public link access only      │
│  index 1: operator — run agents, no create       │
│  index 2: creator  — create + manage agents      │
│  index 3: admin    — full platform control       │
└──────────────────────────────────────────────────┘

  Request arrives at protected endpoint
┌────────────────────────────────┐
│  @router.post("/api/agents")   │
│  Depends(require_role(         │
│    "creator"))                 │
└───────────┬────────────────────┘
            │
            v
┌────────────────────────────────────────────────┐
│  require_role("creator") factory               │
│  1. Calls get_current_user (JWT / MCP key)     │
│  2. Looks up user_level = index of user.role   │
│  3. Looks up min_level = index of "creator"    │
│  4. If user_level < min_level: raise 403       │
│  5. Otherwise: return current_user             │
└────────────────────────────────────────────────┘

  New email user signs up
┌────────────────────────────────────────────────┐
│  db/email_auth.py:get_or_create_email_user()   │
│  INSERT INTO users (role) VALUES ("creator")   │
│  (default changed from "user" to "creator")    │
└────────────────────────────────────────────────┘

  Admin changes a user's role via Settings UI
┌───────────────────┐    PUT /api/users/{username}/role
│  Settings.vue     │ ──────────────────────────────────>
│  Role <select>    │    { "role": "operator" }
│  @change handler  │
└───────────────────┘
                         ┌────────────────────────────────────┐
                         │  routers/users.py                  │
                         │  1. require_admin dependency       │
                         │  2. Block self-role-change         │
                         │  3. Validate role string           │
                         │  4. db.update_user_role(username,  │
                         │       role)                        │
                         │  5. Return {username, role}        │
                         └──────────────┬─────────────────────┘
                                        │
                                        v
                         ┌────────────────────────────────────┐
                         │  db/users.py:update_user_role()    │
                         │  UPDATE users SET role = ?         │
                         │  WHERE username = ?                │
                         └────────────────────────────────────┘
```

---

## Data Flow

### 1. Role Check on Protected Endpoint

**File**: `src/backend/dependencies.py:173-198`

```python
# Role hierarchy definition (line 174)
ROLE_HIERARCHY = ["user", "operator", "creator", "admin"]

# Factory function (lines 177-198)
def require_role(min_role: str):
    def _require_role(current_user: User = Depends(get_current_user)) -> User:
        user_level = ROLE_HIERARCHY.index(current_user.role) if current_user.role in ROLE_HIERARCHY else -1
        min_level = ROLE_HIERARCHY.index(min_role) if min_role in ROLE_HIERARCHY else len(ROLE_HIERARCHY)
        if user_level < min_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{min_role}' or above required"
            )
        return current_user
    return _require_role
```

**Key behavior**: A user with role `admin` (index 3) passes a `require_role("creator")` (index 2) check because `3 >= 2`. Roles unknown to `ROLE_HIERARCHY` receive index `-1` and fail all role checks.

### 2. Agent Creation Gated at Creator Level

**File**: `src/backend/routers/agents.py:308, 314-317`

```python
# create_agent_endpoint — line 308
async def create_agent_endpoint(config: AgentConfig, request: Request,
    current_user: User = Depends(require_role("creator"))):

# deploy_local_agent — lines 314-317
async def deploy_local_agent(
    ...
    current_user: User = Depends(require_role("creator"))
):
```

### 3. New Email Users Default to `creator`

**File**: `src/backend/db/email_auth.py:246-248`

```python
cursor.execute("""
    INSERT INTO users (username, email, role, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?)
""", (username, email.lower(), "creator", now, now))
```

Previously this inserted `"user"`. Changed to `"creator"` so whitelisted users can immediately create agents without admin intervention.

### 4. User Management API

**File**: `src/backend/routers/users.py`

**GET /api/users** (lines 22-43) — list all users, admin-only:
```python
@router.get("")
async def list_users(current_user: User = Depends(require_admin)):
    users = db.list_users()
    return [
        {
            "id": u["id"],
            "username": u["username"],
            "email": u.get("email"),
            "role": u["role"],
            "name": u.get("name"),
            "picture": u.get("picture"),
            "created_at": u.get("created_at"),
            "last_login": u.get("last_login"),
        }
        for u in users
    ]
```

**PUT /api/users/{username}/role** (lines 46-77) — change role, admin-only:
```python
@router.put("/{username}/role")
async def update_user_role(username: str, body: UserRoleUpdate,
        current_user: User = Depends(require_admin)):
    if username == current_user.username:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: ...")
    updated = db.update_user_role(username, body.role)
    if not updated:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    return {"username": updated["username"], "role": updated["role"]}
```

### 5. Database Update

**File**: `src/backend/db/users.py:225-238`

```python
def update_user_role(self, username: str, role: str) -> Optional[Dict]:
    """Update a user's role. Returns updated user or None if not found."""
    valid_roles = {"admin", "creator", "operator", "user"}
    if role not in valid_roles:
        raise ValueError(f"Invalid role '{role}'. Must be one of: ...")
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users SET role = ?, updated_at = ? WHERE username = ?
        """, (role, datetime.utcnow().isoformat(), username))
        conn.commit()
        if cursor.rowcount == 0:
            return None
    return self.get_user_by_username(username)
```

`database.py:290-291` delegates directly to this method:
```python
def update_user_role(self, username: str, role: str):
    return self._user_ops.update_user_role(username, role)
```

---

## Frontend Layer

### Settings.vue — User Management Section

**File**: `src/frontend/src/views/Settings.vue`

**Template** (lines 796-866): Renders a table of all users. Each row shows username, email, current role, and last login. A `<select>` dropdown allows the admin to pick a new role. The current user's own row shows a static badge instead of a dropdown (prevents self-role-change in the UI).

```vue
<!-- Role select for other users (line 843-853) -->
<select
  v-if="u.username !== currentUsername"
  :value="u.role"
  @change="updateUserRole(u.username, $event.target.value)"
>
  <option value="admin">admin</option>
  <option value="creator">creator</option>
  <option value="operator">operator</option>
  <option value="user">user</option>
</select>

<!-- Static badge for self (line 854-856) -->
<span v-else>{{ u.role }} (you)</span>
```

**State** (lines 1246-1254):
```javascript
const usersList = ref([])
const loadingUsers = ref(false)
const currentUsername = computed(() => {
  const u = authStore.user
  // admin login stores email as `${username}@localhost`
  if (u?.email?.endsWith('@localhost')) return u.email.replace('@localhost', '')
  return u?.email || u?.name || null
})
```

**Methods** (lines 1686-1710):
```javascript
async function loadUsers() {
  loadingUsers.value = true
  try {
    const response = await axios.get('/api/users', {
      headers: authStore.authHeader
    })
    usersList.value = response.data || []
  } catch (e) {
    console.error('Failed to load users:', e)
  } finally {
    loadingUsers.value = false
  }
}

async function updateUserRole(username, role) {
  try {
    await axios.put(`/api/users/${encodeURIComponent(username)}/role`, { role }, {
      headers: authStore.authHeader
    })
    await loadUsers()
  } catch (e) {
    alert(e.response?.data?.detail || 'Failed to update role')
    await loadUsers() // refresh to reset select to original value
  }
}
```

`loadUsers()` is called on Settings page mount (line 2103).

---

## Key Functions

| File | Function/Symbol | Line | Purpose |
|------|----------------|------|---------|
| `src/backend/dependencies.py` | `ROLE_HIERARCHY` | 174 | Ordered list defining role precedence |
| `src/backend/dependencies.py` | `require_role(min_role)` | 177 | Dependency factory for role-gated endpoints |
| `src/backend/dependencies.py` | `require_admin` | 158 | Unchanged; still enforces role == "admin" exactly |
| `src/backend/routers/users.py` | `list_users` | 22 | GET /api/users — returns all users (admin-only) |
| `src/backend/routers/users.py` | `update_user_role` | 46 | PUT /api/users/{username}/role (admin-only) |
| `src/backend/db/users.py` | `update_user_role` | 225 | SQL UPDATE on users.role column |
| `src/backend/database.py` | `update_user_role` | 290 | Delegation shim to db/users.py |
| `src/backend/db/email_auth.py` | `get_or_create_email_user` | 226 | New email users now default to `creator` |
| `src/backend/routers/agents.py` | `create_agent_endpoint` | 308 | Now uses `require_role("creator")` |
| `src/backend/routers/agents.py` | `deploy_local_agent` | 314 | Now uses `require_role("creator")` |
| `src/frontend/src/views/Settings.vue` | User Management section | 796 | Admin UI to view and change roles |
| `src/frontend/src/views/Settings.vue` | `loadUsers()` | 1686 | Fetches user list from API |
| `src/frontend/src/views/Settings.vue` | `updateUserRole()` | 1700 | Calls PUT role endpoint |
| `src/frontend/src/views/Settings.vue` | `currentUsername` | 1249 | Computed; used to prevent self-change dropdown |

---

## API Reference

### GET /api/users

Admin-only. Returns all platform users without password hashes.

**Response:**
```json
[
  {
    "id": 1,
    "username": "admin",
    "email": null,
    "role": "admin",
    "name": null,
    "picture": null,
    "created_at": "2025-12-01T10:00:00",
    "last_login": "2026-03-20T08:00:00"
  },
  {
    "id": 2,
    "username": "user@example.com",
    "email": "user@example.com",
    "role": "creator",
    "name": null,
    "picture": null,
    "created_at": "2026-01-15T12:00:00",
    "last_login": "2026-03-19T09:30:00"
  }
]
```

### PUT /api/users/{username}/role

Admin-only. Changes the role of the specified user.

**Request Body:**
```json
{ "role": "operator" }
```

**Response:**
```json
{
  "username": "user@example.com",
  "role": "operator"
}
```

---

## Side Effects

- No WebSocket broadcasts on role change.
- No audit log entries specific to role changes (standard request logging applies via Vector).
- Role change takes effect on the user's **next authenticated request** — active JWT tokens are not invalidated, but since the JWT only stores `username` (not role), each request re-fetches the user from the DB. Role enforcement is therefore immediate for all new requests.

---

## Error Handling

| Error Case | HTTP Status | Message | Location |
|------------|-------------|---------|----------|
| Non-admin calls GET /api/users | 403 | `Admin access required` | `require_admin` dependency |
| Non-admin calls PUT role | 403 | `Admin access required` | `require_admin` dependency |
| Admin tries to change own role | 400 | `Cannot change your own role` | `routers/users.py:58` |
| Invalid role value | 400 | `Invalid role. Must be one of: admin, creator, operator, user` | `routers/users.py:61-64` |
| Target user not found | 404 | `User '{username}' not found` | `routers/users.py:72` |
| Operator tries to create agent | 403 | `Role 'creator' or above required` | `require_role` factory |
| Unknown role in hierarchy check | (treated as index -1) | Fails all `require_role` checks | `dependencies.py:190` |

---

## Database Notes

- No schema migration is needed. The `role` column already exists on the `users` table with `DEFAULT 'user'`.
- Only `db/email_auth.py` changed its hardcoded default from `"user"` to `"creator"` for new email signups.
- Existing users created before ROLE-001 retain their stored role (typically `"user"`). An admin can promote them via the Settings UI or API.
- Public-link JWT sessions have no corresponding DB user with an elevated role; they continue to receive effective `user`-level access.

---

## Testing

### Prerequisites
- Backend running
- Admin credentials available (`POST /api/token` with `username=admin`)
- At least one non-admin user exists (sign up via email or create via DB)

### Test Steps

#### 1. Verify role hierarchy: operator cannot create agents

**Action:**
```bash
# Login as a user with role=operator and try to create an agent
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d 'username=operator@example.com&password=...' | jq -r .access_token)

curl -s -X POST http://localhost:8000/api/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "test", "template": "github:Org/repo"}'
```

**Expected:** HTTP 403 `Role 'creator' or above required`

#### 2. Admin promotes operator to creator via API

**Action:**
```bash
ADMIN_TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d 'username=admin&password=YOUR_PASSWORD' | jq -r .access_token)

curl -s -X PUT http://localhost:8000/api/users/operator@example.com/role \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"role": "creator"}'
```

**Expected:**
```json
{"username": "operator@example.com", "role": "creator"}
```

#### 3. Creator can now create agents

**Action:** Repeat step 1 with the same token (JWT fetches fresh role from DB on each request).

**Expected:** HTTP 200 agent created successfully.

#### 4. Admin cannot change own role via API

**Action:**
```bash
curl -s -X PUT http://localhost:8000/api/users/admin/role \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"role": "user"}'
```

**Expected:** HTTP 400 `Cannot change your own role`

#### 5. New email signup defaults to creator

**Action:** Sign up a new whitelisted email via `/api/auth/email/request` + `/api/auth/email/verify`.

**Verify:**
```bash
curl -s http://localhost:8000/api/users \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  | jq '.[] | select(.email == "newuser@example.com") | .role'
```

**Expected:** `"creator"`

#### 6. Settings UI: role dropdown

**Action:**
1. Login as admin, navigate to Settings (`/settings`).
2. Scroll to "User Management" section.
3. Find a non-admin user row.
4. Change the role dropdown to `operator`.

**Expected:**
- Dropdown fires `updateUserRole()` on change.
- Table refreshes with updated role.
- Own row shows static badge `admin (you)` instead of dropdown.

---

## Related Flows
- [email-authentication.md](email-authentication.md) — Email signup now defaults new users to `creator` role
- [admin-login.md](admin-login.md) — Admin password login; `require_admin` dependency unchanged
- [agent-lifecycle.md](agent-lifecycle.md) — `create_agent_endpoint` now gated by `require_role("creator")`
- [platform-settings.md](platform-settings.md) — Settings page where User Management section lives
