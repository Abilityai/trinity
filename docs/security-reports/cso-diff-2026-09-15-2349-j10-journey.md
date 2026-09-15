# CSO diff audit — abilityai/trinity#2349 (`feature/2349-j10-agent-calls-agent`)

`cso v1.1` · mode `--diff` · confidence gate 8/10 · 2026-09-15

## Verdict

**Clean for what the diff introduces.** The branch adds one journey harness and its test-tier primitives, two catalog records, a registry entry, an invariant-catalog correction and documentation. No endpoint, schema, Redis key, prompt surface, dependency, workflow or container config changed. The harness handles one RESTRICTED value at run time — the caller agent's own MCP key, read from its container so the test can cross the real permission gate — and that value is confined to one helper and one header dict, with every repr, failure message and poll observation verified to exclude it.

**Four pre-existing product findings were surfaced by the harness.** None is introduced by the diff; all four are tracked and three are carried as strict xfails. They are listed in the appendix, the highest of them a HIGH-severity permission-gate gap in `run_agent_loop`.

## Attack surface introduced by the diff

| Category | Count |
|---|---|
| New / changed endpoints | 0 |
| New DB reads / Redis keys | 0 |
| New or widened prompt surfaces | 0 (the harness sends fixed strings in the user-message position) |
| Dependency / workflow / Docker / compose changes | 0 |
| New test files | 1 (`tests/journeys/test_j10_agent_calls_agent_journey.py`) |
| Code hunks | test tier only: `tests/journeys/conftest.py` (agent-key reader, MCP JSON-RPC session, edge/executions/activities helpers, two-agent fixture), `tests/journeys/catalog.yaml`, `tests/registry.json` |
| Doc files | 7 edited (JOURNEYS.md regenerated; IA-03 corrected; four feature-flow Testing pointers; two learnings entries) |
| Private submodule | `.claude` moved in the working tree only (DEBT_INBOX.md, pushed to trinity-dev main as 81ee365); not staged |

## Findings

None in scope.

## Verification performed

- **Secrets (P2):** the working-tree diff and the new file scanned for `AKIA`, `sk-`, `ghp_`/`gho_`/`github_pat_`, `xox*`, `trinity_mcp_` and `password=`/`token=`/`secret=` literals: 0. `.env` untracked and ignored. Every run log this session produced was grepped for the agent-key prefix: 0 hits. Enterprise-disclosure guard PCRE replayed over the edited docs and the new test: 0 hits.
- **P3–P6:** not applicable — no dependency, CI, Dockerfile/compose, webhook or integration files changed (stated, not skipped). `docker` and `httpx`, which the harness uses, were already declared in `tests/requirements-test.txt`.
- **P7 (LLM):** the harness's messages sit in the user-message position and it renders nothing. The MCP tool boundary it exercises is the product's, and the two gaps it found there are appendix A1 and A3.
- **P8:** no skill files in the diff.
- **P9–P11:** tests and docs are hard-excluded (#8, #12); the one real surface — a test reading an agent's key through the Docker socket — was traced end to end: lookup by the platform's `trinity.agent-name` label, extraction of exactly one variable from an env blob that also carries the provider key and PATs, no return of the blob, no logging, reprs that omit headers, and a failure path that names the exception type only. STRIDE over the harness: it can only act as an agent the tier itself created, on the host that runs the stack.
- **P12:** nine candidates screened, all cleared in-context (detail in the JSON). Independent verification of the appendix items was performed earlier in the same session by two fresh-context review subagents against the code, and then reproduced live with `pytest --runxfail`; no further verifier was spawned for them.

## Appendix (pre-existing, tracked — not introduced by this diff)

- **A1 — HIGH, VERIFIED live.** `run_agent_loop` honours a caller-supplied `agent_name` and never calls `checkAgentAccess`; the backend loop route is owner-equivalent for agent keys and stamps `source_agent_name` from the key. A holder of agent A's key can start a bounded loop on any same-owner sibling with no permission edge, with the rows attributed to A. Same class as the per-endpoint admin gate that took five occurrences (#1890). Tracked as abilityai/trinity-enterprise#628; strict xfail in the harness.
- **A2 — MEDIUM, VERIFIED live.** `agent_permissions` is enforced only at the MCP layer; an agent key used raw against the backend admits any same-owner sibling on `/chat`, `/task`, `/fan-out`, `/loops` and inbound A2A (architecture.md Invariant #8). Ruling requested as abilityai/trinity-enterprise#629; no public reproducer by the plan-gate decision.
- **A3 — MEDIUM, VERIFIED live.** A refused agent-to-agent call is audited as `success: true` because `withAudit` decides by throw/no-throw and the deny path returns a string. Tracked as abilityai/trinity#2807; strict xfail in the harness.
- **A4 — MEDIUM, VERIFIED by code reading.** No chain-depth guard on agent-to-agent chat chains; the bound is per-hop timeouts and capacity parks whose overflow queues rather than refuses. Tracked as abilityai/trinity#2806; skeleton strict xfail in the harness.
- **Trend:** flat against the 2026-09-13 diff audit (#2339) — 0 findings introduced in nine consecutive diff audits. A1 belongs to the same "gate applied per call site" family as the ent#614 header-trust item in the 2026-09-12 appendix.

## Remediation roadmap

| Item | Disposition |
|---|---|
| A1 loop-gate gap | Defer → tracked privately (ent#628); a one-line fix plus an audit of sibling agent-targeting tools; the strict xfail flips when it lands |
| A2 backend owner-equivalence | Defer → ruling requested (ent#629) |
| A3 denial audited as success | Defer → tracked (#2807); the strict xfail flips when it lands |
| A4 no depth guard | Defer → tracked (#2806) with the flip condition written in the issue |
