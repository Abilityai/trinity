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

---
