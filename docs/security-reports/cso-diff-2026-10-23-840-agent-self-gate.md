# CSO diff audit — 2026-10-23 — trinity-enterprise#840 agent-key self-gate

**Scope:** `feature/840-agent-self-gate` vs `dev` (`66a860094`), daily mode (8/10 gate), cso v1.2.3.

**Result:** 0 findings at or above the gate. No hypotheses. 2 coverage gaps open; a third (the runtime route census) closed after the audit.

**After the audit (2026-10-26, review round):** merging `dev` added a 19th converted route, the Nevermined delegation read, which keeps its uniform 404 through `refusal=`, and a seventh MCP description (`get_agent_spending`). The hand-copy ratchet now also reads an agent name inside `or` / `and` / `… if … else …`. The counts below are as audited at `66a860094`.

## What changed on the attack surface
- **No new endpoint.** 18 existing routes now refuse a sibling agent's key through one helper, `dependencies.assert_own_agent`, called as the handler's first statement. Each keeps the status and message it had before.
  - The routes: reminders ×3, seat decisions ×2, user memory, metric points, reports, canvas writes and deletes ×4, canon roles, objectives, shared files, voice reply, and the outbound A2A call and poll.
- **User memory.** The self-gate now runs before the access check. An agent key naming an agent outside its reach gets the self message instead of "Not authorized". Both are 403, and the refusal now depends only on the principal, so the in-reach/out-of-reach difference is gone.
- **Outbound A2A.** The kill switch is a route dependency, solved after the access dependency and before body validation. With the switch off, a schema-invalid or missing body answers 404 (was 422). Unparseable JSON is still FastAPI's 422.
- **Census.** The route census recognises the self-gate AND requires an `AGENT_CALLABLE_SELF` entry with a use-not-grant reason. The gate alone never classifies a route; the reviewed reason is what keeps a self-scoped grant out, since a listed entry passes every check.
- **Hand-copy ratchet.** `test_1310_auth_wiring.py` fails any hand-written copy of the gate under `src/backend/`.
- **MCP.** Six tool descriptions changed: `write_user_memory`, `set_reminder`, `list_reminders`, `cancel_reminder`, `send_voice_reply` and `get_a2a_task`. Each now says the `agent_name` parameter is for a user-scoped key, and that an agent key may only name itself. These are static strings.

## Checks run (diff-scoped)
| Check | Result |
|---|---|
| Behaviour preservation, per converted handler | Independent reviewer's AST dump, base vs head: the gate is the first statement in all 18, and messages are byte-identical. A census-driven test calls every `AGENT_CALLABLE_SELF` handler with a sibling key (21 cases), putting the caller's own name in every other argument so a gate that compares the wrong parameter fails |
| A false positive in self-gate recognition (an unfenced route classified self) | Refused when: the principal is not `get_current_user`/`CurrentUser`; the agent argument is not the `name`/`agent_name` named in every literal path; both agent parameters are in the path; the route is also registered with `add_api_route`; the import is aliased or the name is redefined; the call is not first, is nested, or uses keywords. Each case is a planted self-test, mutation-proven |
| Hand-copy ratchet coverage | Spellings covered: attribute, `getattr`, `agent_principal_name` and their aliases, path-parameter aliases, `!=` / `is not` / `not in`, a negated `==`, and `== … else: refuse`. Refusals covered: any `raise` or a returned 401/403/404. Principals covered: annotated `User`/`CurrentUser` in any form, a `Depends` default, or an unannotated parameter named like a principal. 19 spellings found and 9 benign shapes spared; live tree clean (allowlist: `aauth_person.assert_own_key`, stricter) |
| Identity guards (ASI03) | `test_293` (admin gate rejects agent keys), `test_2996` (census), `test_1310`, `test_186` (enumeration uniformity), `test_ent629` (reach), `test_ent614` (attribution), `test_2094` (dependency/path pairing): 806 passed together with `test_736` and `test_ent840` |
| Failure direction (A10) | `assert_own_agent` states it: it refuses an agent key naming another agent and is a no-op for every other principal, as the copies were. The a2a switch dependency fails closed (404); its docstring states the order |
| Secrets / PII / IPs in the diff | None (one `owner@example.com` test placeholder) |
| Invisible Unicode in changed files (42, MCP descriptions included) | None |
| Enterprise-docs guard over the changed public docs | Clean. The workflow's PCRE pattern was run through Python `re`, because the host `grep` cannot parse it and reports a false clean |
| Tests | 18 + 9 call-site mutants killed. 72-file affected subset fails identically on base and head (21 pre-existing host/subset failures), with 69 more passing on head. MCP suite 973/973, `tsc --noEmit` clean |

## Coverage gaps
1. **Runtime route census (rule 7): closed after the audit.** This host's venv cannot `import main` (it lacks `opentelemetry.exporter`). `test_2996_route_census_runtime.py` then passed (3/3) in the `/verify-local` venv against the real app: every stored `METHOD /path` matches, both new tables included. `/verify-local --skip-agent` passed, with integration at 69 passed, 13 gated skips and the 2 known false-fails deselected.
2. **No live probe on a running stack.** The operator asked for no live tests against localhost in this session. `/verify-local --skip-agent` runs the integration suite on a sibling stack.
3. **No dependency scanner run.** The diff changes no pin, lockfile, image install, workflow, Dockerfile or compose file (Phases 3–5 had nothing in scope).

## Trend
Prior full runs (2026-09-29, 2026-09-30) carry no finding on the agent-key self-gate class; their ASI03 rows rest on the #2996 census, which until now had no vocabulary for self-fencing. Next full run: record the class as code-recognised + ratcheted.

## Stated residual limits (by design, not findings)
- **System key and loopback.** A `system`-scoped key and the event loopback carry no agent name, so they pass every self-gate while resolving to the owner's role. This is unchanged by the diff; per-route rulings are trinity-enterprise#719.
- **The absent attribute.** The helper's predicate (`agent_principal_name`, shared with `agent_may_reach`) reads a principal with no `agent_name` attribute as "not an agent key". This is unreachable on these routes: the census pins the gate's principal to `get_current_user` / `CurrentUser`, and `models.User` always declares the field.
- **The ratchet's one stated limit.** A comparison against a parameter not named `name` / `agent_name` (or a local alias of one) is not matched. Widening it would flag legitimate header checks (`x_source_agent != current_user.agent_name`).
- **Reads left open on purpose.** Some reads still admit an agent inside its reach: canvas list/get/context, report list, the metrics cross-read (ent#727's edge rule).
- **`share_agent_file`.** Listed as today's behaviour, a use per FILES-001. Whether minting a public link should be person-only, like the canvas share (ent#554), is open in ent#719's sharing cluster.
