# CSO Diff Audit — trinity-enterprise#862 PR1: AAuth flag split + Settings surface

- **Date**: 2026-10-13 · **Mode**: `--diff` (daily gate 8/10) · **Branch**: `feature/ent862-aauth-graduate` vs `origin/dev` (merge-base `984f8f4c8`, working tree audited pre-commit)
- **Scope**: `src/backend/services/aauth/{config,settings,whoami}.py`, `routers/settings/{aauth,generic,flags,__init__}.py`, `routers/aauth_person.py`, `models.py`, `src/mcp-server/src/{client.ts,tools/a2a.ts,tools/aauth.ts}`, `src/frontend/src/{components/settings/AAuthPanel.vue,components/settings/A2AEndpointsPanel.vue,stores/a2aFederation.js,utils/aauthSettings.js,views/Settings.vue}`, three `docker-compose*.yml` + `scripts/dev/aauth_prototype/{compose.b.yml,demo.sh}`, `.env.example`, docs, tests

## Verdict: 1 finding (MEDIUM, pre-existing, remediated in-branch after the audit). No new door opened.

The diff adds one grant surface and closes a generic door around it. The grant surface — `PUT /api/settings/aauth` — is gated the way Invariant #8 asks of a grant (`require_interactive` + `assert_admin`: no MCP key of any scope), writes only the fields sent, validates the issuer by name, and audits `{from, to}`. The generic `PUT/DELETE /api/settings/{key}` now refuses the three keys that route owns. The finding is that it stopped at three.

```
SECURITY FINDINGS
═════════════════
#  Sev   Conf   Status    Evidence   Category               Finding                                                    Phase  File:Line
── ────  ────   ──────    ────────   ────────               ───────                                                    ─────  ─────────
1  MED   9/10   VERIFIED  SUPPORTED  settings-generic-door  aauth_person_server / aauth_resource_allowlist still       P9     routers/settings/generic.py:183, :628
                                                            writable via the admin-only generic catch-all
```

### F1 — the two Flow 5–6 keys stayed on the generic door (MEDIUM · 9/10 · VERIFIED · SUPPORTED · OWASP A01:2025 · ASI03)
- **Evidence**: `services/aauth/config.py:43` `SETTINGS_ROUTE_KEYS = frozenset({FLAG_SETTING, PROTOTYPE_FLAG_SETTING, ISSUER_SETTING})`; `routers/settings/generic.py:183` and `:628` gate `update_setting` / `delete_setting` with `assert_admin(current_user)` alone, which admits `ADMIN_GATE_SCOPES = {None, "user", "system"}` (`dependencies.py`). Neither key is credential-shaped for the ent#435 sink guard (`_allowlist`, `_server` match no suffix), so nothing else refuses the write. The branch's own comment on the refusal block — *"an MCP key carrying an admin owner's role would otherwise flip them"* — applies verbatim to the two keys it left out.
- **Exploit scenario**: (1) an attacker holds an admin's user- or system-scoped MCP key (a leaked `trinity-ops-agent` key, a key in a shared `.mcp.json`); (2) `PUT /api/settings/aauth_resource_allowlist {"value": "https://attacker.example"}` passes `assert_admin`; (3) any agent later induced to name that resource (the LLM-supplied-URL path the allowlist exists to fence) sends it a request signed as the agent and carries the person-server ladder to it. Repointing `aauth_person_server` lets an attacker-run person server mediate owner binding — only while the prototype switch is on, which this diff made human-only.
- **Prerequisites that keep it MEDIUM**: an already-compromised admin-role key; a second actor (an agent's own key) to realise egress; and the same key can already register a credentialed outbound A2A endpoint (`PUT /api/settings/a2a-endpoints` is `assert_admin` + `reject_agent_principal`, not interactive) — key-driven egress grants are the existing baseline.
- **Pre-existing**: yes — `origin/dev` has both keys with no catch-all guard; the diff narrowed the door and did not widen it.
- **Verifier**: independent fresh-context agent — CONFIRMED, MEDIUM, "pre-existing; the branch narrows it but does not close it".
- **Remediation (applied in-branch after this audit, per approved plan decision #16 "refuse ALL `aauth_*` keys")**: `PUT /api/settings/aauth` takes `person_server` (strict server-identifier rule, empty clears) and `resource_allowlist` (must parse under `resource_allowlist.parse_allowlist`, empty clears); both keys join `SETTINGS_ROUTE_KEYS`; `demo.sh` moves its two remaining generic writes to the route. Closure evidence: the parametrised generic-door tests in `tests/unit/test_ent862_aauth_settings.py` cover five keys.

## Checks run (diff mode; phases 0, 1, 12–14 + diff-relevant rows)

| Check | Result |
|---|---|
| Attack-surface delta | +2 routes (`GET/PUT /api/settings/aauth`: admin; write interactive-only), +1 MCP parameter (`auth_scheme` on `register_a2a_endpoint`, same human-only backend route), +4 compose env forwards (a boolean, empty default), +2 Settings panels. No new agent-context ingress, WebSocket channel, webhook, or Docker permission |
| Grant-vs-use (ASI03, Invariant #8) | `update_aauth_settings` = `Depends(require_interactive)` + `assert_admin`; tested against agent-, user- and system-scoped keys (403) and a non-admin JWT (403). Guard suites green: `test_293_admin_gate_rejects_agent_keys`, `test_2996_human_only_routes`, `test_1310_auth_wiring`, `test_186_enumeration_uniformity`, `test_2094_dependency_path_param_pairing`, `test_ent435_settings_sink_guard` (144 pass) |
| Flag split (A10 failure direction) | Both switches resolve through `settings_service._resolve_bool_flag` (row wins both ways; unreadable row → env → **off**) and state fail-closed in their docstrings; Flows 5–6 gated once at `aauth_person.require_live` on BOTH switches (4-arm test); internal readers unchanged so one issuer is compared throughout |
| Generic catch-all | Refuses `aauth_enabled`, `aauth_prototype_enabled`, `aauth_issuer` on PUT and DELETE (6 tests) — F1 for the two keys it missed |
| Input validation | Issuer never repaired: `normalize_issuer` refuses http, port, path, mixed case, IP literal → `422 {reason: invalid_issuer}` (5 cases); booleans are `StrictBool` (`1` refused); `extra="forbid"`; `nothing_to_change` → 400 |
| Credential exposure | Nothing new stored; the audit row carries switch/issuer `{from,to}` only; the MCP tool refuses `credentials` together with `auth_scheme: aauth` before any round trip; `A2AEndpointsPanel` hides the credential field for aauth and sends no `credentials` key on a blank bearer update (mounted tests) |
| Secrets in diff | Known-prefix scan over added lines: none; no tracked `.env`; hidden-Unicode grep over changed files: none |
| LLM05 output handling | No `v-html`/`innerHTML` in the new panels; dialog copy interpolates through Vue text binding |
| Enterprise-docs guard | `enterprise-docs-guard.yml` pattern (ugrep `-P`) over added `docs/` + seam lines: no hits |
| Compose wiring | `AAUTH_ENABLED=${AAUTH_ENABLED:-}` in `docker-compose.yml`, `.prod.yml`, `.hosted.yml` (empty default keeps the row the observable source); `compose.b.yml` sets it outright like the prototype flag; YAML-parse test pins all four |
| Writers outside the backend | `scripts/dev/aauth_prototype/demo.sh` moved to the dedicated route for the three blocked keys (found in `/review`); F1 remediation moves the remaining two |

## Hypotheses
(none — daily mode)

## Coverage gaps
Diff mode: secrets archaeology over full history, dependency scanners, CI/CD workflow audit, image internals and the Docker pass were not run — the diff changes no dependency, workflow, or Dockerfile. The frontend panels were exercised by mounted vitest specs, not by a human in a browser in both themes (the design-contract check the PR self-check still owes).
