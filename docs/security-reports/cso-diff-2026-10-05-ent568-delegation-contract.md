# CSO Diff Audit — 2026-10-05 — abilityai/trinity-enterprise#568 (the delegation contract)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/568-delegation-contract` → `dev` · **Skill**: cso v1.2.3 · **Audited**: the uncommitted worktree diff against `09f9d0868` plus untracked files (25 files). Not a full run.

## Architecture (Phase 0)

The change teaches one text — the delegation contract — wherever a caller reads: the platform prompt's §Agent Collaboration (spliced from `DELEGATION_CONTRACT`), the `chat_with_agent` and dynamic `chat_with_<agent>` descriptions (verbatim, from `src/mcp-server/src/delegation_contract.ts`), and a rule sentence in `fan_out` / `send_message`. Every receipt the MCP server writes now ends with one "do not re-send: read the outcome with `get_execution_result(…)`" line, and `runAgentChat` rewrites the backend's REST-only "Poll GET …" line on async `accepted` / `queued` receipts. No dispatch behaviour changes; only text does.

Trust boundaries crossed: none new. The contract is platform-authored; receipt messages echo the caller's own `agent_name` and a backend `execution_id` back to the same caller.

## Stack CVE quicklist

Not re-evaluated: no pin, lockfile, Dockerfile, compose file or dependency is in the diff, and the diff adds no `request.url.path` read.

## Attack surface (Phase 1, diff-scoped)

| Surface | Change |
|---|---|
| Endpoints | None new. |
| MCP tools | None new, no new parameters, visibility unchanged. Description text and receipt `message` strings only. |
| Agent-context ingress | None new. Platform-authored prompt text. |
| Globally advertised text | Dynamic `chat_with_<agent>` descriptions append a platform constant to the backend's name-only line (#846) — no per-agent metadata added. |
| CI | `mcp-server-test.yml` boot smoke also imports `dist/tools/dynamic-agents.js`; no interpolation, trigger, permission or action change. |

## Findings

None.

| # | Sev | Conf | Status | Evidence | Category | Finding |
|---|---|---|---|---|---|---|
| — | — | — | — | — | — | No candidate survived the 8/10 gate. |

## Considered and dismissed

1. **Receipt-shaped JSON inside a reply.** A receipt is the tool's own answer, not text inside a reply; a target can already lie in its reply, and `get_execution_result` is gated to self ∪ permitted. No boundary crossed.
2. **"Not found → re-send it word for word" under idempotency fail-open.** Needs two simultaneous failures (a missed ledger row and `begin()` failing open); worst case is one extra run of the same work. Accepted residual — the same reason §37.2 keeps the MCP server itself from probing this way.
3. **"The outcome will be sent to you" for a system-scoped gate requester.** Pre-existing in ent#751's MCP message; an honesty defect, not a boundary. Filed as #3233.

## Checks run

- Secret-prefix scan over every changed and untracked file — none (the TS test's `trinity_mcp_test_agent_key` is a fixture placeholder already used by `access-wiring.test.ts`).
- Enterprise-docs guard pattern replayed with Python `re` — 0 hits.
- Invisible / bidi / tag-block Unicode and injection-string scan (Phase 8) — none.
- Workflow change reviewed by hand against the zizmor set.

## Coverage gaps

- Not a full run: Docker image checks, quicklist re-resolution and Phase 3 scanners not run (no dependency or image in the diff; the local stack was not rebuilt by instruction).
- zizmor not run; the one workflow line was reviewed by hand.

## Totals

Critical 0 · High 0 · Medium 0 · Low 0. Trend: diff run — n/a.
