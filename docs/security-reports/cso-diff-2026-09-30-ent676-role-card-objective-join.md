# CSO Diff Audit — 2026-09-30 — abilityai/trinity-enterprise#676 (the role card reads the objective ↔ metric join)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/676-role-card-objective-join` → `dev` · **Skill**: cso v1.2 · **Audited**: the working-tree diff against merge-base `863240f32`.

## Architecture (Phase 0)

The Workspace role card (`GET /api/enterprise/client-portal/agents/{name}/role`) stops computing its own objective ↔ metric join and calls the platform's one join (`services/objective_join_service.read_objective_join`) in process.

- `client_portal/role_card.py`: the local join, the 30-day metric staleness rule and the read of the agent server's `/api/metrics` are removed. Four pure functions build what a client sees: `portal_objective`, `portal_metric`, `objectives_error`, `finding_codes`. Each PICKS fields; none copies a row.
- `services/objectives_read_budget.py` (new): the `agent_objectives_read:{name}` key, limit and window, spelled once. `enforce` raises 429 (the operator route), `admit` returns a bool (the portal route).
- `client_portal/router.py::portal_agent_role`: after the roster gate, a per-viewer check (`portal_role_objectives:{email}:{name}`, 20/min) and then the shared budget decide `objectives_admitted`, which `build_role_card` takes as a required argument.
- `PortalAgentRole.vue`: renders the projection; text interpolation only.

Trust boundaries touched: agent-authored files and recorded values → a Workspace viewer (who may be an external, portal-token client); three principals (viewer, operator, the agent's own key through MCP `get_objectives`) → one rate bucket.

**Stack CVE quicklist**: not re-assessed. No Dockerfile, lockfile, requirements file, compose service or image pin is in the diff (`.env.example` changes one comment). This is a statement of scope, not a verdict of `patched`; the daily full audit owns those rows.

## Attack surface (Phase 1, diff-scoped)

| Surface | Change |
|---|---|
| Endpoints | None new. One existing portal read changes its response shape and gains two non-raising limiter checks. One existing operator read calls the same limiter through a new leaf (same key, limit and window). |
| Response fields to a portal client | Removed: `direction`, `by`, `value`, `as_of`. Added per metric: `type`, `unit`, `actual`, `last_point_at`, `freshness`, `gap.status`, `finding.code`. Added per card: `objectives_error`, `finding_codes`. |
| Agent-context ingress | None. Nothing in the diff lands in an agent prompt. |
| Agent-authored text reaching a human | Objective `statement`, metric names, a recorded text value. All were already on the card; the recorded value is now capped at 64 characters (it was unbounded from `metrics.json`). |
| MCP tools, WebSocket, agent server, migrations, dependencies, CI, Docker/compose, vendored policy files, skills | Unchanged. |

## Findings (Phases 2–12)

**None at the 8/10 gate.** Every phase was run against the diff:

- **Phase 2, secrets:** added lines carry no known secret prefix, no private IP, and no email outside `@example.com`. One regex hit (`re_…`) is the substring of `metric_store_unavailable`. The enterprise-disclosure pattern from `.github/workflows/enterprise-docs-guard.yml` has zero hits over `docs/`, `CLAUDE.md` and the two seam files.
- **Phases 3–5, supply chain / CI / infrastructure:** no dependency, lockfile, workflow, Dockerfile, compose service or vendored-copy file is touched. No scanner was needed; none was run.
- **Phase 6, integrations:** no webhook, internal route, agent-identity endpoint or backend→agent call site is added. The join's agent-door reads go through the `AgentClient` the card already held.
- **Phase 7, agentic:** ASI09 (human-agent trust) is the only row with overlap. Agent-authored text is rendered through Vue interpolation; there is no `v-html` in the component. No other row overlaps.
- **Phase 8, agent-shipped surfaces:** none touched. No invisible or bidi characters in any changed file.
- **Phase 9, A01:** gate order is unchanged and pinned: `get_portal_principal` → `_require_roster` (uniform 404) → limiter keys built on the validated name (`test_an_off_roster_name_is_a_404_before_any_limiter_key_is_minted`). The projection is an allowlist in code and a second allowlist in `response_model`; both key sets are pinned as literals. **A06:** the read was unlimited before this change and now has a per-viewer and a per-agent bound. **A10:** see below.
- **Phase 10, STRIDE (the role-card read):** information disclosure reduced (operator remediation text, objective file paths, `owner: role:<id>`, deltas and `by` dates do not cross; an unbounded text value is now bounded). Denial of service: see candidate 1. No change to spoofing, tampering, repudiation or elevation.
- **Phase 11, data:** objective statements and metric values are the agent owner's business data, CONFIDENTIAL to the agent's roster. Nothing new leaves that audience.

### Failure directions (A10), stated

| Guard | Direction | Where it is stated |
|---|---|---|
| The two limiter checks | Fail OPEN on a Redis outage through the limiter's bounded in-process fallback (the platform doctrine for rate limits) | `services/rate_limiter.py` module docstring |
| A refused budget | Degrades: the card answers without objectives and says why; never a 429 for the card | `portal_agent_role` docstring, `objectives_read_budget.py` |
| `objectives_admitted` | Required keyword, no default, so a new caller cannot reach the fan-out unbounded by omission | `build_role_card` docstring |
| The join raising | Fail-soft and NAMED (`objectives_error: objectives_unreadable`), logged with a traceback. It guards a data read, not an auth, signature or capability decision; the import sits outside the `try` so an ImportError stays loud | `build_role_card` |

### Candidates considered and not reported

1. **Roster members can spend the shared bucket.** Three viewers of one agent, each at their own 20/min cap, can hold the agent's 60/min budget empty and so refuse the operator route and the agent's own `get_objectives` (each gets a 429 with `Retry-After`). Rate-limit exhaustion is hard exclusion 1; before this change the portal read had no bound at all and reached the container directly. The reverse direction, which would have been a real loss (an agent hiding its owner's readiness control by polling), was designed out: the card never answers 429.
2. **`role.path` reaches portal clients.** The card has sent and rendered the role file's path (`<canon>/roles/<id>.yaml`) since #2927. It predates this diff and is ent#527's stated design (the card names the file because the file is what gets edited). The diff's "paths stay on the operator door" rule is about objective files; the wording in the docs says so.
3. **`finding_codes` names file-level failures that may concern another role's file.** Codes only, by the owner's recorded ruling at claim time; no path, id or sentence crosses (pinned by test).
4. **`objectives_rate_limited` tells a viewer that someone is reading this agent's objectives heavily.** No identity, count or timing beyond "right now". Below the gate.
5. **The viewer's email is part of a limiter key.** The existing pattern of every limiter in this router; the key lives 61 seconds.

## Active verification (Phase 12)

- **Guards that ran green on this tree** (full `tests/unit`, 19,541 passed): `test_186_enumeration_uniformity`, `test_293_admin_gate_rejects_agent_keys`, `test_1310_auth_wiring`, `test_2996_human_only_routes`, `test_models_centralized`, `test_ent435_settings_sink_guard`. The runtime route census (`test_2996_route_census_runtime`) could not run on this host: `import main` needs `opentelemetry.exporter`, which the local venv lacks. **Coverage gap**, closed by CI and by `/verify-local`.
- **The controls were challenged, not just observed.** Ten call-site mutations of `role_card.py` and `router.py`, each restored from a scratch copy and each turning a named test red, including: the portal route ignoring the budget, the portal route spelling its own key, the shared bucket spent before the viewer cap, the finding sentence copied into the projection, and the whole join row copied to the client.
- **Independent verifier:** not spawned — no finding survived the gate to verify. The plan was reviewed before implementation by two independent agents, whose findings (the whole-card 429, the silent empty on a failed read) changed the design.

## Coverage gaps

- The runtime route census on this host (above).
- Stack CVE quicklist rows: out of this diff's scope, not assessed here.
- No live probe. The change is exercised on the real in-process limiter and the real join in unit tests; the operator's localhost eyeball precedes the PR.

## Trend

First audit of this surface since `cso-diff` runs began covering the Workspace role card. No prior finding to close; no new finding.
