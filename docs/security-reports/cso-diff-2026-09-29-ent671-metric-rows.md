# /cso --diff — trinity-enterprise#671 metric-point rows in Settings → Retention (2026-09-29)

**Scope:** `feature/671-metrics-retention-rows` vs `dev` (`e87163125`), plus the managed retention endpoint change that lands with it in its own PR. Daily gate: 8/10.
**Result:** 0 findings (0 critical, 0 high, 0 medium, 0 low).

## Attack surface added
- **No new endpoint.** `GET /api/settings/retention` gains a `quotas` block: `metrics_daily_point_cap` → `{value, source}`, an integer plus `db-row` / `env` / `code-default`. It sits behind the handler's existing first statement, `assert_admin(current_user)` (`routers/settings/retention.py:229`). The value is operator config, not a secret.
- **The managed retention endpoint accepts two more fields.** `PUT /api/enterprise/retention/config` was already entitlement-gated at the router and `require_admin` per route. It now also takes `metrics_retention_days` (0–3650) and `metrics_daily_point_cap` (0–10 000 000), both bounded integers. It rejects an unknown key with a 422, where it used to drop it silently, and it now writes a platform audit row. Its gates are unchanged, and `require_admin` rejects agent-scoped keys, which a test proves through the real router.
- **Deploy config.** The three backend compose files forward `METRICS_RETENTION_DAYS`, `METRICS_DAILY_POINT_CAP` and `INTER_AGENT_MAX_CHAIN_DEPTH` with an empty default, and `.env.example` leaves them commented. The effective values do not change, since the code defaults are the same numbers. Only the reported source changes.
- **Frontend.** Two rows on an admin-only tab, an `env` badge with a static `title`, and an `InlineError` that renders the server's message through `{{ }}` interpolation. No `v-html`.

## Phases
- **Secrets:** 0 key patterns in either repo's diff. The only credential-shaped strings are test placeholders (`redis://u:p@localhost:6379`, `ci-not-a-real-password`), which satisfy `config.py`'s import-time check and connect nowhere. The `.env.example` edits comment out integers.
- **Enterprise docs guard:** 0 hits across the public tree. The public docs describe the seam only ("the entitlement-gated managed retention endpoint").
- **Dependencies, CI and Docker:** unchanged. `starlette.concurrency.run_in_threadpool` ships with FastAPI.
- **Redis ACL and the two-network model:** untouched.
- **Injection:**
  - No raw SQL. The private module's writes go through SQLAlchemy Core upserts.
  - Keys come from fixed tuples, and values are `str(int)`.
  - The write sink keeps its credential-shaped-key guard (`assert_plaintext_write_allowed`).
- **Broken access control:** the read is admin-only and the write is admin-only plus entitled. Both match the OSS write path for the same keys (`PUT /api/settings/ops/config`, `assert_admin`). The UI makes an env-sourced row read-only; an admin can still override env through either API, which is the documented precedence and not a privilege change.
- **Logging and repudiation:** improved. The managed path used to log only a line, so shrinking a window, or lifting the quota to unlimited, left no audit row. It now records `configuration` / `ops_settings_change`, in the same detail shape as the OSS path, with effective (post-clamp) values.
- **Information disclosure:** the new read field carries an integer and a tier name. The audit details carry setting names and values, with no secrets.

## Mutations
All of these were checked from scratch copies and restored byte-identical.
- Audit call removed from the managed PUT: the router-level audit test goes red.
- `extra="forbid"` removed: the unknown-key tests go red.
- Env lock removed from the view: the e2e env-state test goes red.
- Compose default restored, or the `.env.example` line uncommented: the forwarding guard goes red.

## Appendix (below the gate)
- **Accepted:** the managed PUT's audit write is best-effort, swallowed and logged, matching the vault module's precedent: an audit failure never fails a change already written. The OSS `/ops/config` path returns 500 after writing instead. Neither loses the write; they differ only in how loudly an audit-store fault surfaces.
- **Accepted:** `ADMIN_GATE_SCOPES` admits an admin-owned `user`-scoped MCP key on both write paths (#2323, by design). This is unchanged by the diff.
