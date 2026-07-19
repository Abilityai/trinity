# CSO Security Audit

**Mode**: diff
**Scope**: `AndriiPasternak31/issue-1310` vs `origin/dev` (#1310 INV-8 auth-dependency consolidation)
**Date**: 2026-07-17
**Diff size (source)**: 23 files, +240 / −226 (21 routers + `dependencies.py` + `services/agent_service/helpers.py`)

## Summary

| Category | CRITICAL | HIGH | MEDIUM | LOW |
|----------|----------|------|--------|-----|
| Secrets | 0 | 0 | 0 | 0 |
| Dependencies | 0 | 0 | 0 | 0 |
| Auth Boundaries | 0 | 0 | 0 | 0 |
| Injection | 0 | 0 | 0 | 0 |
| Platform Patterns | 0 | 0 | 0 | 0 |
| Configuration | 0 | 0 | 0 | 0 |

## Nature of the change

Behavior-preserving refactor enforcing Architectural Invariant #8 ("Auth Pattern"). Duplicated
**inline** router-level auth wiring is consolidated behind five leaf helpers added to
`dependencies.py`:

| Helper | Wraps | Raises |
|---|---|---|
| `assert_admin` | `_reject_connector_principal` + `role != "admin"` | 403 |
| `assert_agent_access` | `_enforce_connector_scope` + `can_user_access_agent` | 403 |
| `assert_agent_owner` | `_enforce_connector_scope(owner_op=True)` + `can_user_share_agent` | 403 |
| `assert_owns_or_admin` | `id != owner AND role != "admin"` | 403 |
| `assert_owns` | `id != owner` (**no admin bypass**) | 403 |

~30 router sites migrate onto these helpers. No new endpoint, DB table, DB accessor, or import
edge is introduced (`dependencies.py` already imports `db`; the helpers are leaf functions). The
`_narrow_to_agent` fleet-filter helper is relocated from `routers/executions.py` to
`services/agent_service/helpers.py::narrow_to_agent`, removing a router→router import edge.

## Auth-boundary analysis (the security-relevant axis)

Both DB predicates short-circuit `role == "admin" → True`, so every migrated `can_user_*` site is
**semantically identical** to the helper that replaces it. The audit verified two axes, not just
the 403↔404 status axis:

1. **Status axis (401/403/404/role)** — every migrated site preserves its exact status **and**
   its exact `detail` string (each helper threads a per-site `detail=`). No inline **403** was
   converted to the dependency-family **404**: access-first inline handlers are already
   self-uniform per INV-8 (§ `schedules.py` #1445 precedent), so preserving 403 is correct and
   introduces no enumeration oracle. Proven by `tests/unit/test_1310_auth_consolidation.py`
   (real-DB `db_harness`, negative-per-site assertions).

2. **Admitted-principal-set axis (same 403, wider caller set)** — the subtler risk. Verified:
   - **`public.py::get_public_link_session_detail`** is **strict-self, no admin bypass**
     ("owners cannot see other users' sessions"). It maps to the new `assert_owns` (id-only), NOT
     `assert_owns_or_admin` — mapping it to the admin-bypass variant would have flipped 403→200
     for an admin reading another user's public-link session. A dedicated characterization case
     (`test_public_link_session_denies_admin_non_owner`) locks admin-non-owner → **403**.
   - The four Shape-F session gates (`voice_stop`, `get_voice_panel`, chat get/close-session)
     legitimately carry the admin bypass and map to `assert_owns_or_admin`; the admin-admitted
     path is asserted preserved.
   - Every Shape-F/strict-self owner line's **anti-IDOR binding** (`session.agent_name != name` /
     `preview.agent_name != name` → 403, the cross-agent-session-read defense #600) is preserved
     verbatim above the migrated owner line.
   - The `agent_files.share` **anti-exfil sibling** (an agent-scoped key may not share a
     *sibling* agent's files under a shared owner) is preserved verbatim; only the owner line was
     migrated.

3. **Connector-fence parity (defense-in-depth, non-regression)** — the new agent-name helpers run
   `_enforce_connector_scope` first, matching the path-dependencies' second fence (ent#46). This
   is a **tightening**, never a loosening: connector-scoped keys are already fenced to their two
   allowlisted routes in `get_current_user`, so no migrated endpoint is reachable by a connector
   principal at runtime; the fence is redundant belt-and-suspenders. `assert_admin` likewise
   rejects connector principals (parity with the existing `require_admin`).

## Documented exceptions (kept inline; enumeration-safe by construction)

- `reports.get_report`, `nevermined._require_read_access`/`_require_write_access` — intentional
  **404** designs (#186), allowlisted in the static guard.
- `sessions._session_or_404` — a **compound uniform-404** (existence + user + agent binding). NOT
  migrated: mapping it to `assert_owns` would regress 404→403 **and** leak session-id existence.
  (Deviation from the plan's site list, which mislabeled it a `public.py` sibling; the correct,
  behavior-preserving decision is to leave it.)
- `slack.py` (10 inline sites) — deferred follow-up (channel-adapter owner coordination); each
  line carries a `# noqa: inv8` marker so **new** inline auth added there still trips the guard.
- `internal.py` — the INV-8 no-auth exception (agent→backend), unchanged.

## Durable control (where the risk reduction lives)

The extraction is behavior-preserving → the posture is observably identical to today. The durable
reduction is the **static AST guard** `tests/unit/test_1310_auth_wiring.py`: it forbids, in
`routers/*.py`, a `db.can_user_*_agent()` call whose negation guards a `raise HTTPException`, and
an inline `role != "admin"` deny-`If` — the exact shapes that would re-introduce a 404-then-403
oracle or an omitted connector fence. Precise AST matcher, self-tested against planted violations
AND the benign shapes (assignments, `role == "admin"` allow/filter branches, WS `close(4003)`
dict handlers), with a per-`(file, function)` allowlist and the `# noqa: inv8` line-exemption.

## Verification performed

1. **Secrets** — no credentials/tokens/keys added (added-line pattern scan clean); no
   `.env`/manifest files in scope.
2. **Dependencies** — no `requirements*` / `package.json` / `pyproject` changes → no new CVE
   surface.
3. **Auth boundaries** — the analysis above; every consolidated boundary enforces the same
   verdict + admitted-principal set as before. No `get_current_user`, `require_admin`,
   `require_role`, `AuthorizedAgent`, or `OwnedAgent*` binding was weakened.
4. **Injection / path** — no SQL, no path construction, no template rendering added; the
   relocated `narrow_to_agent` is byte-identical logic.
5. **Tests** — `tests/unit/test_1310_auth_consolidation.py` (39, behavioral incl. Part B helper
   truth tables) + `test_1310_auth_wiring.py` (12, static guard + self-tests) +
   `test_186_enumeration_uniformity.py` (29, extended for the helpers) green (80 together); the
   full unit tier green modulo pre-existing flakes unrelated to this diff.

## Verdict

**PASS** — no new bypass, no widening, no enumeration oracle. One isolated widening risk
(`public.py`) and one connector-scope divergence were identified in review and fixed in-plan
(`assert_owns` / `_enforce_connector_scope`), each with a dedicated RED→GREEN characterization
case. No CRITICAL/HIGH/MEDIUM/LOW findings.
