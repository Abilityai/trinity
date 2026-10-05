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
  | `assert_person(user)` / `Depends(require_person)` | `is_person_principal` (JWT or the person's own `user` key) + no `vouched_source_agent` | 403 `HUMAN_ONLY_DETAIL` | owner-tier grant routes (§2.8, #2996) |
  | `Depends(require_interactive)` | `reject_non_interactive_principal` (JWT only) + no `vouched_source_agent` | 403 | credential / sign-in identity mint, bind, rotate (§2.8) |

- **Preserve-403 (not 404).** All five raise **403** — access-first inline handlers are already *self-uniform* per INV-8 (they check access before any existence lookup, so there is no 404-then-403 enumeration oracle). The platform precedent is `schedules.py` `create_schedule` (#1445: "Access-check FIRST … no 404-vs-403 name-enumeration oracle"). The clean `{agent_name}`-path sites *could* have adopted the uniform-404 path-dependencies, but were consciously kept 403 to minimize frontend blast-radius and hold **one** imperative convention.
- **Imperative vs path-dependency (when to use which).** Agent name **in the path** → prefer the path-dependency (`AuthorizedAgent[ByName]`/`OwnedAgent[ByName]`, uniform-404). Agent name **derived from a resolved resource** (notification/session/subscription/execution row) or a **composite** gate (owner-or-initiator, resource-404-then-access) → use the imperative helper. Both fences run `_enforce_connector_scope` first, so the two conventions enforce the connector boundary identically.
- **`assert_agent_owner` ≠ delete authorization.** It wraps `can_user_share_agent` (owner-or-admin) but does **NOT** carry the `is_system` guard that `can_user_delete_agent` (`db/agents.py`) does. A delete path must keep using the delete predicate — never reuse `assert_agent_owner` for delete.
- **Documented exceptions (permanent, not deferred).** `nevermined._require_read_access`/`_require_write_access` and `reports.get_report` stay on their own **intentional-404** helpers (#186 designs, enumeration-safe by construction). `sessions._session_or_404` stays a **compound uniform-404** (existence + user + agent binding in one `if`) — migrating it to `assert_owns` would regress 404→403 **and** leak session-id existence, so it is left as its own already-consolidated helper. `internal.py` is the INV-8 no-auth exception.
- **`slack.py` retired (#1710).** The 11 inline-auth sites #1310 deferred were migrated onto `assert_agent_access` (3 read) / `assert_agent_owner` (8 owner) — behavior-preserving (same 403 + `detail`; site #10's ent#223 human-only `reject_agent_principal` kept). No `# noqa: inv8` marker remains in the tree; a re-introduced inline gate now trips the static guard unless it carries a freshly-added, individually-reviewed marker.
- **Static guard**: `tests/unit/test_1310_auth_wiring.py` — a precise AST matcher forbids `db.can_user_access_agent(` / `db.can_user_share_agent(` Call-nodes whose negation guards a `raise HTTPException`, and inline `role != "admin"` deny-`If`s, in `routers/` (per-function allowlist; `# noqa: inv8` line-exemption retained for a future, individually-reviewed exception — none in-tree after #1710). Filter/capability/allow-branch sites (assignments, `role == "admin"` selection, WS-`close(4003)` dict handlers) are **not** flagged.
- **Behavioral proof**: `tests/unit/test_1310_auth_consolidation.py` (real-DB `db_harness`) locks status + detail + admitted-principal set per migrated site; `test_186_enumeration_uniformity.py` extended for the five helpers.
- **Flow**: `docs/memory/feature-flows/role-model.md`

### 2.8 Human-Only Grant Surfaces (INV-8, #2996 / trinity-enterprise#711)
- **Status**: ✅ Implemented (2026-09-28)
- **GitHub Issues**: #2996 (autonomy toggle), trinity-enterprise#711 (sign-in email rebind)
- **Problem**: `get_current_user` resolves an agent-scoped or system-scoped MCP key to its **owner, carrying the owner's role**. #1890 made the admin gates refuse agent keys; the **owner** tier (`can_user_share_agent`, `assert_agent_owner`, `OwnedAgentByName`) and the self-service routes (`get_current_user` alone) kept admitting them. The owner gates cannot simply refuse agent keys the way the admin gates do, because agents legitimately use owner-gated routes (MCP schedule tools, file writes). So the rule is applied per route, and a route census makes the next route choose a side.
- **Two principal rules** — both **allowlists** over `User.mcp_scope`, fail-closed on a principal without the attribute (sentinel, never `getattr(..., None)`), and both refuse a principal carrying `vouched_source_agent` (the event-loopback JWT, `mcp_scope=None`):

  | Rule | Admits | Refuses | Primitive |
  |---|---|---|---|
  | **PERSON** | JWT session; the person's own `user`-scoped key | agent, system, connector, portal_delegate, ops, any future scope, a missing `mcp_scope`, a `user` principal carrying an agent/connector/delegate identity | `assert_person` / `Depends(require_person)` (predicate `is_person_principal`, ent#611) |
  | **INTERACTIVE** | JWT session only | every key | `Depends(require_interactive)` (predicate `is_interactive_principal`, #1854) |

  They are **credential-class** rules, not proof of human origin: a `user` key is the person's delegated credential, and a JWT counts as interactive only because non-human JWTs are fenced at `get_current_user`.
- **Assignment rule**: a route that **grants or changes what an agent may do** takes PERSON; a route that **mints, binds or rotates a credential or sign-in identity** takes INTERACTIVE — a minter must be stricter than the principal class it produces, or the PERSON rule is circular.

  | Route | Rule |
  |---|---|
  | `PUT /api/agents/{name}/autonomy` (#2996 — decides whether **unattended cron** fires; a manual `trigger_schedule` is not gated by autonomy) | PERSON |
  | `PUT /api/agents/{name}/{api-key-setting, capabilities, capacity}` | PERSON |
  | `PUT /api/agents/{name}/{read-only, resources, timeout, public-channel-model, guardrails}` | PERSON **or** an agent holding `agents.manage` (§2.9) |
  | `PUT /api/users/me/email` (ent#711) | INTERACTIVE |
  | `PUT` / `DELETE /api/users/me/github-pat` (a credential future agent creations inherit, ent#162) | INTERACTIVE |
  | `POST /api/mcp/keys` (every scope), `POST /api/mcp/keys/ensure-default` | INTERACTIVE (security.md §20.10) |
  | `GET /api/mcp/keys` (trinity-enterprise#712 — the key inventory; no machine consumer) | INTERACTIVE |

- **Refusal**: 403. PERSON routes return `HUMAN_ONLY_DETAIL` (`{"code": "person_required", ...}`, the same machine code as ent#611's ask endings, whose own detail is unchanged); INTERACTIVE routes return `reject_non_interactive_principal`'s existing detail. The refusal depends only on the principal, never on whether the addressed agent exists, so answering 403 before a path's 404 is not an enumeration oracle (#186).
- **Still working**: the owner's `user` key and ops tooling on a `user` key keep the PERSON routes; the UI and CLI use JWTs everywhere; heartbeat, the result callback, reports and notifications do not use these gates; `trinity-system` keeps `require_admin` (#2323) and has no caller of the routes above.
- **Route census (the inheritance mechanism)**: `tests/unit/test_2996_human_only_routes.py` walks every `*.py` under `src/backend/` (`rglob`, `enterprise/` excluded — the private tree carries its own), every HTTP method (GETs included), both `@<router>.<verb>(...)` and `add_api_route(...)`. Each route must resolve to exactly one policy class: `person` / `interactive` (the Depends form, or the imperative call as the handler's **first** statement on its principal argument, with the name imported from `dependencies`), `admin_tier` (`require_admin` / `assert_admin`, counted and printed), `admin_widened` (an `allow_scopes` opt-in, listed per route), `portal` (`get_portal_principal`), `agent_callable` / `own_auth` / `delegated` (listed per route with the `METHOD /path` pinned and a reason), or `unclassified_at_freeze` — the frozen baseline `tests/unit/fixtures/human_only_route_baseline.json`. The baseline is **shrink-only**: exact count literal, stale entries fail, an entry that becomes gated must be removed. WebSocket routes are pinned as a literal set. `tests/unit/test_2996_route_census_runtime.py` imports the real app in a subprocess (fails, never skips) and checks every registered route maps to a census key and every stored `METHOD /path` matches.
- **Not closed here**: the census does not classify the routes it freezes as `unclassified_at_freeze` — the baseline is a to-do marker, not a judgement (`tests/unit/fixtures/README.md`). Each entry leaves it through a per-route ruling (tracked in trinity-enterprise#719), and the baseline only lets it shrink.
- **Tests**: `tests/unit/test_human_only_principals.py` (primitives), `tests/unit/test_mcp_key_creation_requires_session.py`, `tests/unit/test_2996_human_only_routes.py`, `tests/unit/test_2996_route_census_runtime.py`, `tests/unit/test_2996_owner_config_person_only.py`, `tests/unit/test_2996_self_service_session_only.py`, `tests/unit/test_2996_agent_schedule_tools_unchanged.py` (the agent's own schedule tools stay agent-callable).
- **Flows**: `feature-flows/autonomy-mode.md`, `feature-flows/email-authentication.md`, `feature-flows/mcp-api-keys.md`

### 2.9 Self-Change Grants (trinity-enterprise#164)
- **Status**: ✅ Implemented (2026-10-05)
- **GitHub Issues**: trinity-enterprise#164 (re-scoped 2026-10-02: grants, not per-action approval); the Settings toggles are trinity-enterprise#756
- **Problem**: an agent's key resolves to its owner (Invariant #8), so the owner gates let any agent reshape any agent of the same owner — its instructions, schedules, configuration, or the fleet itself. ent#596 closed that for skills with one capability grant; this extends the same seam to the rest of the agent's shape.
- **Rule**: four capabilities in a closed set (`db/capability_grants.CAPABILITIES`). Humans (JWT, `user` key, `system`) are never fenced; an agent principal without the grant is **refused** — no pause, no approval-and-resume. The 403 names the missing permission (`*_management_not_permitted`) and tells the agent to raise an ask with `ask_class: permission-request`; answering that ask does **not** grant anything — an admin grants it in the agent's Settings. Every refusal is audited (`capability_refused`).

  | Capability | Covers | Exempt |
  |---|---|---|
  | `skills.manage` (ent#596) | skill writes, skill sets, `.claude/skills/**` via the file routes | — |
  | `schedules.manage` | create / update / delete / enable / disable a schedule, and its webhook + webhook secret | the agent's **own** schedules (#2996 — its own use, bounded by autonomy); "trigger now" |
  | `instructions.manage` | `CLAUDE.md`, `AGENTS.md` and `.claude/**` except skills, via the file routes (write, create-folder, delete — a delete of an ancestor counts); `git/reset-to-main-preserve-state` | — |
  | `agents.manage` | create a durable agent, delete, `deploy-local`, `systems/deploy`, the chat model, and the five reconfigure writes in §2.8 | spawning / discarding an **ephemeral** agent (ent#69 governs ghosts) |

- **Not grantable**: autonomy, api-key-setting, capabilities, capacity (PERSON, §2.8) and rename (ent#69 Part 2, human-only). A calibrating companion (owner readiness stamp, ent#663) cannot be granted `instructions.manage` (422 `calibrating_agent`); ephemeral and system agents cannot be granted anything.
- **Grant surface**: `GET /api/agents/{name}/capability-grants` (owner-level: all four, held or not, with who/when — an agent may read its own); `PUT /api/agents/{name}/capability-grants/{capability}` `{granted}` — admin **and interactive**, idempotent, audited (`capability_grant` / `capability_revoke`). The ent#596 `PUT /skill-manager` route is kept.
- **Upgrade effect**: no agent holds `schedules.manage` / `instructions.manage` / `agents.manage` after deploy. An agent that orchestrates others (creates durable agents, writes another agent's schedules or instructions, reconfigures) is refused until an admin grants it.
- **Known limit**: the fence is on the platform's routes. An agent can still edit files inside its **own** container with its own tools; `instructions.manage` governs the platform write path, not the agent's shell.
- **Tests**: `tests/unit/test_ent164_self_change_grants.py` (fences read off FastAPI's dependant graph, behaviour per class, ghost and own-schedule exemptions, calibrating refusal), `tests/unit/test_2996_owner_config_person_only.py` (real keys: an ungranted agent gets the named refusal, a granted one changes the stored value, the grant does not open the PERSON writes; the grant route), `tests/unit/test_2996_human_only_routes.py` (census class `person_or_grant`).
- **Flow**: `feature-flows/self-change-grants.md`

---
