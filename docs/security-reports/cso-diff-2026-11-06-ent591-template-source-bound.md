# /cso --diff — trinity-enterprise#591 template sources for agent-created agents

**Date:** 2026-11-06. **Mode:** diff, daily gate (8/10). **Scope:** the working tree of `feature/591-template-source-bound` against `origin/dev` `8b59fdbb1`.
- **Policy:** `services/template_source_policy.py` (new): entry grammar, matcher, stored-row reader, refusal, enforcement, admin read.
- **Call sites:** `routers/agents.py` (`POST /api/agents` before the idempotency claim; `POST /deploy-local` before extraction), `services/agent_service/crud.py::create_agent_internal` (choke point, before the ghost pre-gates), `services/system_service.py::deploy_manifest` (whole-deploy refusal + dry-run prediction), `routers/templates.py` (`allowed_for_agents`).
- **Admin surface:** `GET/PUT /api/settings/agent-creation-allowlist` (`routers/settings/templates.py`), the generic `PUT/DELETE /api/settings/{key}` block, `AgentCreationAllowlistUpdate`.
- **Shared:** `platform_audit_service.principal_actor` (extracted from `dependencies._audit_capability`); `db.list_agent_created_agent_names`.
- **MCP:** description text only (`create_agent`, `deploy_local_agent`, `list_templates`, `deploy_system`).
- **Frontend:** `AgentCreationAllowlistPanel.vue` on Settings → Agents.

## Architecture delta

The diff narrows what agent keys can do; it widens nothing for them.
- **New write surface:** one admin setting. `PUT` is the grant gate (`assert_interactive_admin`: admin AND signed-in session), so neither an agent key (which carries its owner's role) nor an admin's own MCP `user` key can write it; the generic catch-all refuses the key on both verbs.
- **New read surface:** the admin `GET` (`assert_admin`, which rejects agent principals) returns the list plus the templates live agent-created agents use, fleet-wide; an admin already sees every agent. Agent keys learn the list only from a refusal body and the `allowed_for_agents` flag on `GET /api/templates` (ruled 2026-11-06).
- **Principal rule:** bounded unless `mcp_scope ∈ {None, "user", "system"}`, with an absent attribute bounded (sentinel, never `getattr(..., None)`). The same allowlist as `ADMIN_GATE_SCOPES`, pinned equal by test.

## Findings

None at the confidence gate. 0 SUPPORTED.

## Controls challenged

| Control | Evidence |
|---|---|
| Every creation path refuses before any side effect | Mutation battery: deleting each call line turns its test red — router create (2), deploy-local (1), crud choke point (4: ghost before its spawn-rate token, fork / copy / clone before any GitHub read), deploy write (1), deploy preview (1), template annotation (1). `test_the_choke_point_refuses_a_ghost_before_its_spawn_token` asserts no rate-limiter call, no container, no minted key |
| No other creation or ghost-spawn path | Callers of `create_agent_internal`: the router facade, deploy-local, `system_service` (deploy + apply), the Cornelius seeder (bare admin `User`, unbounded). `EphemeralConfig` is constructed nowhere else; MCP reaches creation only through `POST /api/agents`. Apply is `require_person` (pinned by `test_apply_stays_person_only`) |
| No post-create source change an agent can drive | `git/bind-to-own-repo` is `require_interactive` + `reject_agent_principal`; `git/initialize` is `require_person`; no branch-switch route exists; `reset-to-main-preserve-state` takes `instructions.manage`. Catalog / registry ids and `github_repo` derive from the same string (`template_service._build_template`), so an id cannot alias another repo |
| Uploaded code cannot pass as bundled | A `local:` entry matches only when the name resolves under `_LOCAL_TEMPLATE_ROOTS[0]` (the read-only curated root); the deploy-local store, which any creator can rewrite, never matches. Parametrised in `test_the_matcher` |
| Ref pin judged on what the create really uses | `effective_ref`: the template's `@ref`, else `source_branch`, else `main`; `fork_to_own` and a copy of the default branch are unknown and match only unpinned entries. One-way parity with crud's `_parse_github_ref` over tricky inputs; the mirrored regexes are pinned equal to crud's |
| Admin gates | `test_only_a_signed_in_admin_writes_the_list` (admin `user` key, agent key carrying admin, `system` key, signed-in creator: all 403, nothing stored); `test_an_agent_key_cannot_read_the_admin_view`; generic `PUT`/`DELETE` 422. Census + guards green: 287 tests across ent435 / 293 / 2996 / 1310 / 186 / ent614 |
| Fail closed, visibly | A non-list row bounds agents to no source; a list with bad entries keeps the valid ones; both set `stored_invalid`, and the panel's badge reads `bounded`, never the count (`test_a_damaged_row_fails_closed_and_says_so`, the mount spec, and a mutation of the badge predicate). An unreadable bundled catalog admits nothing. People never read the row, so a DB fault cannot affect them |
| Nothing attacker-authored is echoed | Refusal body, audit row and log carry only the canonical source (`source_of`), or none for an unrecognised string; `test_the_refusal_names_the_canonical_source_and_the_list` sends a newline-injected template. The audit target is the sanitised slug (the ent#164 rule; mutation-checked). Admin entries are regex-limited (no spaces or newlines) before they reach an agent's context |
| Attribution (Tandem R29) | `template_source_refused` rows name the agent as actor with its key id, the owner as email (`test_the_refusal_row_names_the_agent_and_a_sanitised_target`); exactly one row per refused request through each real call site |
| Settings sink (ent#435) | The writer goes through `db.set_setting`, which runs `assert_plaintext_write_allowed`; the key is not credential-shaped; `test_ent435_settings_sink_guard.py` green |
| Frontend output | 0 `v-html` added; entries, sources and counts are interpolated text |
| Public-repo disclosure (ent#45) | The docs-guard pattern over the added public doc and seam lines: 0 hits |
| Secrets and concealed Unicode | 0 credential-shaped strings and 0 tag-block / zero-width / bidi characters in the added lines |

## Hypotheses

- **A repo entry, pinned or not, is only as strong as who can push to it.** (Corrected by the PR #4311 review: a pin is a name, which push access can move or shadow with a same-named branch; the working-branch and copy-of-`main` mismatches it also found are fixed.) An agent built from a listed repo can push a working branch and spawn from `@thatbranch`; an owner entry admits any repo the agent's token can create under that owner. Stated as the limit in requirements/auth.md §2.9a and the panel hint; the ref pin is the control an admin opts into.
- **The bound is create-time, not containment.** A parent can reshape a child afterwards through `instructions.manage` / `skills.manage` file writes, or by chatting to a ghost that has a shell. Stated in §2.9a.
- **`trinity-system` is unbounded.** Its key is `system` scope, which the ruling leaves outside the bound, as `ADMIN_GATE_SCOPES` leaves it outside the capability gates.

## Coverage gaps

- **Image-internal Docker pass not run** (diff mode; the operator asked that the local stack not be rebuilt). No Docker, compose or base-image file changed.
- **`import main` on this host fails** (`opentelemetry.exporter` missing from the host venv), so `test_2996_route_census_runtime.py` and `test_ent873_audit_client_ip.py` did not run here; `/verify-local` covers both in the real image.
- **Phases with no diff overlap were skipped:** 3 (no dependency changes), 4 (no workflow changes), 5 (no Docker or compose changes), 6 (no webhook or integration code), 8 (beyond the Unicode scan, no shipped skill, template or hook changed).
- **No live PostgreSQL:** the new query is SQLAlchemy Core with bound values, exercised on SQLite only.
