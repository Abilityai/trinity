# /cso --diff — #3825 password change (2026-10-20)

cso v1.1, daily mode, branch `feature/3825-password-change` vs `dev`.

## Findings

| # | Sev | Conf | Status | Finding | File |
|---|-----|------|--------|---------|------|
| 1 | MEDIUM | 9/10 | VERIFIED → FIXED | MCP API keys survive a password change | `src/backend/services/password_service.py` |

**1. MCP API keys survive a password change.** The change signs out every other JWT session (per-user cutoff in the JWT branch of resolve_platform_user_unfloored) but the MCP-key branch never consults the cutoff and the change neither revokes nor lists the account's keys.

- *Exploit*: Sign in with a leaked admin password; POST /api/mcp/keys (require_interactive, default scope user; ADMIN_GATE_SCOPES admits user) mints a key; the admin changes the password; the key keeps admin API access until revoked by hand.
- *Impact*: The change does not evict an attacker who already minted a key, which is the threat #3825 exists for (#3799).
- *Resolution (this diff)*: Opt-in revoke_api_keys on PUT /api/users/me/password revokes the caller's user/ops/portal_delegate keys; the response and panel report revoked/active counts. Tested incl. agent-key exclusion and a scope-filter mutation.
- *Recommendation*: Surface the account's person-minted keys (user/ops/portal_delegate scopes, not agent/system/connector) in the change response and the panel, with an opt-in to revoke them in the same change.

## Checked and refuted
- Fingerprint readable through other settings readers: refuted: routers/settings/ops.py and agent_defaults.py read only their own known keys; generic GET/PUT/DELETE reserve it
- Event-loopback JWT reaching PUT /me/password: refuted: fenced at the auth door to POST /api/agents/{subscriber}/task
- Enterprise 2FA/SSO claims lost on the fresh token: refuted: both mint data={'sub'} only
- Voice socket accepting a signed-out token: found in /review, fixed in this diff (jti + cutoff check, tested)

## Coverage gaps
- Session cutoff and change limiter fail open when Redis is down (same posture as #187; the response reports `other_sessions_signed_out`).

Supply chain: no dependency or lockfile changes. Secrets scan of the diff: clean.
