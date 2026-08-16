# Requirements — Authentication & Authorization

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 2. Authentication & Authorization

### 2.1 Email-Based Authentication
- **Status**: ✅ Implemented (2025-12-26, security-hardened 2026-03-26)
- **Description**: Passwordless email login with 6-digit verification codes
- **Key Features**: 2-step verification, admin-managed whitelist, auto-whitelist on agent sharing, rate limiting (IP-based + per-email OTP lockout after 5 failures)
- **Security**: OTP brute-force prevented by dual rate limits — `login_attempts:{ip}` (shared with admin login) and `otp_attempts:{email}` (max 5 failures → 10-min lockout). Both `POST /api/auth/email/verify` and `POST /api/public/verify/confirm` are protected. (pentest 3.1.5 / #176)
- **Enumeration-safety (#186)**: `POST /api/auth/email/request` returns an **identical body + status** regardless of whitelist membership (`{"success": true, "message": "If your email is registered, you'll receive a code shortly"}` — no distinct message, no `expires_in_seconds`). The rate-limit path returns the **same generic 200** (never a 429 differential; suppression is WARN-logged server-side), and the verification email is dispatched **fire-and-forget** so the whitelisted path's latency matches the non-whitelisted/rate-limited paths — closing the body, status, and timing membership oracles (pentest 3.3.3).
- **Flow**: `docs/memory/feature-flows/email-authentication.md`

### 2.2 Admin Password Login
- **Status**: ✅ Implemented
- **Description**: Password-based fallback for admin user
- **Key Features**: Bcrypt hashing, first-time setup wizard

### 2.3 Session Persistence
- **Status**: ✅ Implemented
- **Description**: User profile survives page refresh via localStorage JWT

### 2.4 Agent Sharing
- **Status**: ✅ Implemented
- **Description**: Share agents with team members
- **Key Features**: Share via email, access levels (Owner/Shared/Admin), sharing tab for owners

### 2.5 User Role Model (ROLE-001)
- **Status**: ✅ Implemented (2026-03-20)
- **Requirement ID**: ROLE-001
- **GitHub Issue**: #143
- **Description**: 4-tier role hierarchy (admin > creator > operator > user) with server-side enforcement via `require_role()` dependency factory
- **Key Features**:
  - Role hierarchy: `user` < `operator` < `creator` < `admin`
  - `require_role(min_role)` FastAPI dependency factory for endpoint protection
  - Agent creation restricted to `creator`+ role
  - Admin-only user management: `GET /api/users`, `PUT /api/users/{username}/role`
  - New email users default to `creator` role
  - Settings UI "User Management" section with role dropdowns
- **Database**: `role` column on `users` table (default `"user"`)
- **Flow**: `docs/memory/feature-flows/role-model.md`

### 2.6 Auth0 OAuth
- **Status**: ❌ Removed (2026-01-01)
- **Reason**: Auth0 SDK caused blank pages on HTTP LAN access. Email auth is simpler and works everywhere.

### 2.7 Shared Imperative Auth-Guard Family (INV-8, #1310)
- **Status**: ✅ Implemented (2026-07-17)
- **GitHub Issue**: #1310 (split from #654's architecture-invariant drift report; carved off from INV-14/#1308 **because it is security-sensitive**)
- **Requirement**: Architectural Invariant #8 ("Auth Pattern") — duplicated inline auth wiring (`db.can_user_access_agent` / `db.can_user_share_agent` → 403, and inline `role != "admin"` → 403) collapses behind shared helpers. Threshold: **≤5 bespoke in-scope non-exception sites**.
- **Description**: `dependencies.py` already carried the **path-dependency** factories (`AuthorizedAgent` / `OwnedAgent[ByName]`, uniform-404, #186) and the imperative fences (`_enforce_connector_scope`, `reject_agent_principal`). #1310 adds a small **imperative-guard family** — callable from any router body, for the sites where the agent name is *derived from a resolved resource* or the check is *composite* (a session/notification/execution row must be looked up before the agent name is known), where a path-dependency can't reach.

  | Helper | Wraps | Raises | Replaces |
  |---|---|---|---|
  | `assert_admin(user, *, detail="Admin access required")` | `_reject_connector_principal` + `role != "admin"` | 403 | inline admin checks + 4 router-local `require_admin` dupes |
  | `assert_agent_access(user, agent_name, *, detail="Access denied")` | `_enforce_connector_scope(owner_op=False)` + `db.can_user_access_agent` | 403 | inline `can_user_access_agent` → 403 |
  | `assert_agent_owner(user, agent_name, *, detail=…)` | `_enforce_connector_scope(owner_op=True)` + `db.can_user_share_agent` | 403 | inline `can_user_share_agent` → 403. **NOT delete-authorization** (see below) |
  | `assert_owns_or_admin(user, owner_id, *, detail="Not authorized")` | `user.id != owner_id AND role != "admin"` | 403 | strict-self-**or-admin** session gates (voice/chat) |
  | `assert_owns(user, owner_id, *, detail=…)` | `user.id != owner_id` (id-only, **no admin bypass**) | 403 | strict-self session gate (`public.py` public-link session detail) |

- **Preserve-403 (not 404).** All five raise **403** — access-first inline handlers are already *self-uniform* per INV-8 (they check access before any existence lookup, so there is no 404-then-403 enumeration oracle). The platform precedent is `schedules.py` `create_schedule` (#1445: "Access-check FIRST … no 404-vs-403 name-enumeration oracle"). The clean `{agent_name}`-path sites *could* have adopted the uniform-404 path-dependencies, but were consciously kept 403 to minimize frontend blast-radius and hold **one** imperative convention.
- **Imperative vs path-dependency (when to use which).** Agent name **in the path** → prefer the path-dependency (`AuthorizedAgent[ByName]`/`OwnedAgent[ByName]`, uniform-404). Agent name **derived from a resolved resource** (notification/session/subscription/execution row) or a **composite** gate (owner-or-initiator, resource-404-then-access) → use the imperative helper. Both fences run `_enforce_connector_scope` first, so the two conventions enforce the connector boundary identically.
- **`assert_agent_owner` ≠ delete authorization.** It wraps `can_user_share_agent` (owner-or-admin) but does **NOT** carry the `is_system` guard that `can_user_delete_agent` (`db/agents.py`) does. A delete path must keep using the delete predicate — never reuse `assert_agent_owner` for delete.
- **Documented exceptions (permanent, not deferred).** `nevermined._require_read_access`/`_require_write_access` and `reports.get_report` stay on their own **intentional-404** helpers (#186 designs, enumeration-safe by construction). `sessions._session_or_404` stays a **compound uniform-404** (existence + user + agent binding in one `if`) — migrating it to `assert_owns` would regress 404→403 **and** leak session-id existence, so it is left as its own already-consolidated helper. `internal.py` is the INV-8 no-auth exception.
- **`slack.py` retired (#1710).** The 11 inline-auth sites #1310 deferred were migrated onto `assert_agent_access` (3 read) / `assert_agent_owner` (8 owner) — behavior-preserving (same 403 + `detail`; site #10's ent#223 human-only `reject_agent_principal` kept). No `# noqa: inv8` marker remains in the tree; a re-introduced inline gate now trips the static guard unless it carries a freshly-added, individually-reviewed marker.
- **Static guard**: `tests/unit/test_1310_auth_wiring.py` — a precise AST matcher forbids `db.can_user_access_agent(` / `db.can_user_share_agent(` Call-nodes whose negation guards a `raise HTTPException`, and inline `role != "admin"` deny-`If`s, in `routers/` (per-function allowlist; `# noqa: inv8` line-exemption retained for a future, individually-reviewed exception — none in-tree after #1710). Filter/capability/allow-branch sites (assignments, `role == "admin"` selection, WS-`close(4003)` dict handlers) are **not** flagged.
- **Behavioral proof**: `tests/unit/test_1310_auth_consolidation.py` (real-DB `db_harness`) locks status + detail + admitted-principal set per migrated site; `test_186_enumeration_uniformity.py` extended for the five helpers.
- **Flow**: `docs/memory/feature-flows/role-model.md`

---
