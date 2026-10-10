# Feature: Admin Password Change

> trinity-enterprise#709 · Requirement: [auth.md §2.10](../requirements/auth.md) · OSS core (not entitlement-gated)

## Overview

The signed-in admin changes their own password from the browser — no `.env` edit, no backend restart. A stepped dialog re-authenticates the user (current password, then a second factor only when the 2FA gate requires one), takes the new password under the first-run rules, signs out every other session, and keeps the current tab signed in on a fresh token.

## Entry Points

- **UI**: Settings → General → **Account** → *Change password* (`AccountPasswordPanel.vue`); Mobile Admin → System → **Account** (`MobileAdmin.vue`). Both open `components/settings/ChangePasswordDialog.vue`.
- **API**: `POST /api/users/me/password/verify`, `PUT /api/users/me/password` (`routers/users.py`).

## Frontend Layer

`ChangePasswordDialog.vue` — `BaseModal` (Esc, focus trap, backdrop click disabled so a stray click cannot discard typing), a body with a reserved minimum height so steps do not jump.

| Step | Shown when | Primary action |
|------|-----------|----------------|
| `current` | always | *Continue* → `store.verifyCurrent` |
| `second` | verify said `second_factor_required` | *Continue* (the code is checked on submit) |
| `new` | after the above | *Change password* — disabled until all `utils/passwordRules.js` hints are met, confirm matches, and new ≠ current |
| `done` | success | "Password updated" + whether other sessions were signed out |

Refusals map back to the step and field they are about (`current_password_incorrect` → step 1 field; `second_factor_invalid`/`required` → step 2 field; `password_*` → step 3); anything else shows in an `InlineError`. The step counter reads "Step n of 2" without 2FA and "of 3" with it. Fields are cleared on close and after success.

`stores/accountPassword.js` — calls through `api.js`; on success writes `data.access_token` to `localStorage['token']` and calls `authStore.adoptStoredSession()`, so this tab (and sibling tabs, via the storage event) continue on the new session.

## Backend Layer

`routers/users.py`

1. `Depends(require_interactive)` — JWT sessions only; every MCP key scope and event-loopback tokens are refused (403). Then `assert_admin` (Invariant #8).
2. `_reauthenticate`: `routers/auth.check_login_rate_limit(ip, account=username)` (429 → `too_many_attempts`); `password_change_service.load_account` (409 `no_password` for an email-code-only account); verify the current password — on failure `record_login_attempt(success=False)`, audit `password_change_failed`, 400 `current_password_incorrect`.
3. verify endpoint only: `password_change_service.second_factor_state` → `{second_factor_required, second_factor_enrollment_required}`.
4. change endpoint: `check_second_factor` (via `mfa_gate.step_up_decision` / `mfa_gate.verify_code`; a wrong code is counted and audited) → `check_new_password` (mismatch / ASVS rules / unchanged) → `apply_password_change` (bcrypt hash via `db.update_user_password`; for `admin_username()` also `system_settings['admin_password_source'] = 'ui'`) → `record_login_attempt(success=True)` → mint a fresh token → `dependencies.revoke_user_sessions(username, keep_jti=<fresh jti>)` → audit `password_changed`.

Refusals are `{detail: {code, message, errors?}}` with 400/409/429/503 — never 401, which the frontend treats as "session ended".

## Sessions

`create_access_token` adds `iat`. `revoke_user_sessions` writes `auth:sessions_revoked_before:{username}` = `{cutoff}:{keep_jti}` with the access-token TTL. `is_user_session_revoked` (in `get_current_user`, `decode_token`, `/api/auth/validate`) rejects `iat <= cutoff` (or no `iat`) unless `jti == keep_jti`. Fail-open on Redis (#187 posture). Event-loopback tokens (EVT-001, `sub` = the admin, no `iat`, 5-minute single-route) are not sessions and are exempt. Without the exemption, event dispatch would be rejected for the cutoff's whole TTL after an admin password change.

## Boot reconciliation

`database._ensure_admin_user` (SQLite) and `_ensure_admin_user_engine` (PostgreSQL) both ask `utils/admin_identity.env_may_resync_admin_password(stored_hash, admin_password_source)`. With the `ui` marker and a usable hash, `ADMIN_PASSWORD` no longer overwrites the stored hash (a boot note says the env value is ignored); it still seeds a missing or empty hash. No marker → unchanged behaviour. The generic `PUT`/`DELETE /api/settings/{key}` refuse `admin_password_source` (422), so the marker can't be set or cleared there. Clearing it plus a restart would revert a rotation to the `.env` value.

## Second factor (mfa_gate seam)

`MfaProvider` gains an **optional** `verify_code(user, code) -> bool`. `step_up_decision` returns `required = enrolled or policy-required`. Both raise `MfaUnavailable` on provider errors or a provider without `verify_code`, which the endpoint turns into 503 `second_factor_unavailable` — step-up fails closed. Required-but-not-enrolled → 409 `second_factor_enrollment_required`. No provider (OSS) → the step is skipped.

## Audit

`AuditEventType.AUTHENTICATION`: `password_changed` (`details.second_factor`, `details.other_sessions_signed_out`) and `password_change_failed` (`details.reason`). Actor, IP, request id; never a password value.

## Tests

- `tests/unit/test_ent709_admin_password_change.py`
- `tests/unit/test_2996_human_only_routes.py` (both routes classified `interactive`)
- `src/frontend/tests/unit/changePasswordDialog.spec.js` (mounted, jsdom)
