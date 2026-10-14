# CSO diff audit — 2026-10-08 — trinity-enterprise#754 (the merged Skills tab)

**Mode**: `--diff`, daily (8/10 gate) · **Skill**: cso v1.2.3 · **Base**: `dev` @ `51b01c786` · **Branch**: `feature/754-skills-tab-merge` (local; audited at `7ab943948`, finding fixed in `852fcc4fd`) · **Counts as full run**: no

## Scope

| Area | Change |
|---|---|
| Agent server | `GET /api/skills` reports per skill `source` (the platform's `.trinity-skill.json` marker present or not), `dir` and `approval` (closed set `recommended`). Agent-controlled and display-only: none of them decides access or gating, and the platform's gate map stays the authority. |
| Listing proxy | new `services/agent_skills_listing.py`, the one proxy behind `GET /api/agents/{name}/playbooks`, the public link and the connector. A Redis last-known copy `agent:skills_list:{name}` (no TTL, 256 KB cap, cleared on teardown and on create) is served on `?last_known=true`. |
| Gate map read | `GET /api/agents/{name}/skill-gates` gains `approvers[{kind, reachable, viewer_fills}]`, and `?probe=true` → `hook`, honoured for a person who may manage the agent's skills only. |
| Executions | the list and detail gain `gate_self_approved` / `gate_self_approved_by_viewer`. Both are booleans, for people only, derived from ent#752's `self_approved` records. |
| Workspace | the history read and the synchronous chat reply carry the same pair, people only. |
| Library | `GET /api/skills/library` names each skill's `approval` (declared on `SkillInfo` since ent#753, never passed). |
| Frontend | the merged tab (`components/skills/*`, `stores/skills.js`, `stores/skillGates.js`, `utils/skillCards.js`); the marker in Tasks, on the execution page and in the Workspace. |

No dependency, CI, Docker, compose or base-image Dockerfile change. No new route, MCP tool, credential or migration.

## Phase 0 — model and quicklist

The tab reads three things:
- **The agent's own listing.** The agent authors it; it is display only.
- **The gate map.** It is server-authoritative.
- **Execution history.**

The trust boundaries are:
- who may trigger the in-agent `/health` probe: a person who may manage the agent's skills;
- what an anonymous public-link holder sees: a fixed field list;
- what a machine key learns about approvers: nothing. `viewer_fills` is a boolean that is false for machine keys, and the self-approved marker is for people only;
- the Redis copy: backend-only, since agents cannot reach Redis (#589).

**Quicklist:** no pinned component is touched; every row reads "not touched by diff". For the Starlette BadHost row: no new fence reads `request.url.path`.

## Phase 1 — attack surface delta

- **Endpoints:**
  - new endpoints: 0;
  - changed reads: 8 (the agent page's list, the public link, the connector read, the gate map, the executions list and detail, the Workspace history and chat reply, the library list);
  - new query params: `last_known` and `probe`, both FastAPI bools.
- **New storage:** one Redis keyspace, `agent:skills_list:`, registered in `CLEARED_KEYSPACES` (the #1560 parity test is green).
- **New outbound call:** one `/health` GET per manager tab load. It has a 3 s timeout, goes through `services/agent_auth.agent_httpx_client`, and does no breaker bookkeeping.
- **Agent-context ingress:** none new. Run sends `/<name>` of the agent's own or assigned skill to that same agent.
- **Agent-authored text shown to people:** skill name, description, argument hint and mode chip. All are rendered by interpolation; no `v-html` was added.

## Phases 2–11

- **P2 secrets:**
  - Added lines scanned for known prefixes, PEM blocks, JWTs, credential assignments, private IPs and non-placeholder emails: none. The only email hits are the `Ana@Example.com` casefolding fixtures.
  - The enterprise-docs guard PATTERN over added doc lines: 0 hits.
  - `test_ent435_settings_sink_guard.py` and `test_292_cmdline_credential_leak.py` are green, with no new settings writer and no new exec.
- **P3 / P4:** not applicable: no manifest, lockfile or workflow change.
- **P5 infra:** only the Redis keyspace applies here.
  - Each key is per agent, backend-only, at most 256 KB per agent, and shape-checked on read.
  - It is cleared on delete, rename, purge and failed create.
  - It is also cleared on create, so a read in flight during a delete cannot hand a recycled name its predecessor's list.
- **P6:** backend→agent calls go through `services/agent_auth.py` (`test_agent_auth_header_guard.py` is green). No webhook, channel, A2A or agent-identity endpoint is touched.
- **P7 agentic:**
  - **ASI03:** `?probe` is honoured only when `is_person_principal` and `can_manage_agent_skills` both hold (`routers/skill_gate.py:116`). The manage check alone admits a system-scoped key and an agent key holding `skills.manage`; both are now tested as refused. `viewer_fills` reuses `enforce`'s own two functions, and a parity test checks they agree. The route census, `test_186`, `test_293` and `test_1310_*` are green.
  - **LLM02 / person data:** beside `source_user_email`, the self-approved marker says who fills an approver kind. The operator ruled it people-only in the review round, so machine keys read both flags false. This is the `set_by` rule (#715) behind the ent#753 report's finding 1.
  - **ASI09:** skill text the agent authored renders as text. The hook warning maps a closed enum to fixed sentences.
  - **ASI01 / ASI06:** no new prompt ingress; no memory writer touched.
- **P8 agent-shipped:** no shipped file changed. An invisible-Unicode grep over every added line found nothing.
- **P9 OWASP:**
  - **A01:** every changed read keeps its existing dependency (`AuthorizedAgentByName`, `AuthorizedAgent`, the portal principal plus roster, `get_skill_gate_readable_agent_by_name`). The public link keeps to `PUBLIC_SKILL_FIELDS` (`agent_skills_listing.py:59-62`), so `source`, `dir` and `approval` never reach an anonymous visitor.
  - **A05:** `get_self_approved_runs` is SQLAlchemy Core with bound parameters, chunked at 500 ids per statement.
  - **A10:** each new guard states its failure direction:
    - the cache write and read fail open (to the live answer and the original error, respectively);
    - probe failures read `unknown`, and are logged when unexpected;
    - the flags read never raises (it returns no flags);
    - the probe gate fails closed (`hook: null`).

    Finding 1 was an A10 error-text leak.
- **P10 STRIDE (changed components):**
  - **Spoofing:** no new identity input.
  - **Tampering:** an agent can write only its own cached listing, which is the same trust as its live listing.
  - **Repudiation:** gate writes keep their audit rows.
  - **Information disclosure:** finding 1 (fixed) and the people-only ruling.
  - **DoS:** the probe is manager-only with a 3 s timeout, and the IN-list is chunked.
  - **Elevation of privilege:** no new write surface.
- **P11 data:**
  - The cached listing is INTERNAL: agent-authored skill metadata its viewers already see.
  - The self-approved flags are CONFIDENTIAL-derived (who approves): booleans, for people only.
  - The hook state is INTERNAL: a closed enum.

## Findings

| # | Sev | Conf | Status | Evidence | Category | Finding | Phase | File |
|---|---|---|---|---|---|---|---|---|
| 1 | LOW | 9/10 | VERIFIED | SUPPORTED | error-text-disclosure | A Docker daemon fault during the listing's container reload was echoed in the 500 of `/playbooks` and of the anonymous public link: the daemon URL, its API version, the full container id and the daemon's message | P9 (A10) | `src/backend/services/agent_skills_listing.py` (`fetch_live`), `src/backend/routers/public.py` (`get_public_playbooks`) |

**Exploit scenario.** A public-link holder requests `GET /api/public/playbooks/{token}`; they are anonymous apart from the token. Meanwhile the Docker daemon is degraded: it returns a 500 on the container inspect, or the socket drops on the second inspect, milliseconds after the first. The route's catch-all then returns `500 {"detail": "Failed to fetch playbooks: 500 Server Error for http+docker://localhost/v1.<n>/containers/<full id>/json: …"}`. The caller cannot cause the fault, and what leaks is environment detail of little use. It is still a regression: at the base, the public route never reloaded the container, and the agent page reloaded outside its `try`, which gave a plain 500.

**Verifier.** An independent agent, given only the claim, the invariant and the false-positive rules, returned CONFIRMED (LOW). Nothing on the way out rewrites the detail (exception handlers, middleware, nginx), and docker-py builds the message from the request URL, which carries the full id.

**Resolution.** Fixed in-branch with tests in `852fcc4fd`. Any reload failure other than NotFound now maps to `unreachable` (503, "Could not read the agent's state", with the cause logged). The public link's catch-all answers a fixed sentence. Mutation-proven: each change reverted turns its test red.

## Hypotheses

None (daily mode).

## Coverage gaps

- **No Docker pass.** This is diff mode with no image, compose or dependency change, and the operator's rule keeps Docker untouched this run. The agent-server change (`docker/base-image/agent_server/routers/skills.py`) is exercised by the standalone agent-server suite, not in a built image. A full `/verify-local` (with the agent stage) is pending the operator's go.
- **PostgreSQL** was not exercised locally. There is no schema change, and the chunked read is plain Core.
- **Live end to end** (real Redis, real containers) was not run; it is also pending `/verify-local`.

## Trend

**Prior report on this surface:** `cso-diff-2026-10-07-ent753-skill-gate-map`. Its finding 1 (`set_by` reaching agent keys) stays fixed: `_for_principal` is still applied at `routers/skill_gate.py:112`.

**Recurrence of that class:** person data a machine key can infer came back in this branch's review, as the self-approved marker beside `source_user_email`. The people-only ruling closed it before this audit (`c78eb6631`).

**New:** finding 1, fixed.

**Direction:** flat. One LOW, fixed in-branch.

## Remediation

Nothing open.
