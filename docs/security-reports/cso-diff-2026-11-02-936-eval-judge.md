# /cso --diff — feature/936-eval-judge (2026-11-02)

**Scope:** the agent-behavioral-evaluation epic (abilityai/trinity-enterprise#205) against `origin/dev`, merge-base `0e54bc269`, 175 files. Daily mode, 8/10 gate. Partial by construction (diff run, no Docker pass).

## Result

One finding, **MEDIUM**, fixed on the branch in `f193da467`.

| # | Sev | Conf | Status | Evidence | Category | Finding | File:Line |
|---|---|---|---|---|---|---|---|
| 1 | MED | 9/10 | VERIFIED | SUPPORTED | broken-access-control-share-tier (A01, ASI03) | Evaluation reads gave a share holder every user's runs of a shared agent | `src/backend/routers/evaluations.py:440` |

**1. Share-tier narrowing missing on the evaluation reads.** trinity-enterprise#875 limits a person an agent is only shared with to the executions they started (`viewer_own_rows` / `viewer_row_scope`). The evaluation reads scoped by `accessible_agent_names` alone, and every row this epic writes grades one execution.

- **Exploit:** B, shared agent X by its owner A, calls `GET /api/agents/X/evaluations/indicators/checks_passed/runs`, `/evaluations/failed`, or the MCP `get_evaluation_summary` tool. B gets A's execution ids, schedule ids, delegating agents, reason codes and costs, while `GET /api/agents/X/executions/{id}` 404s the same ids.
- **Fix:** `ReadScope` carries `viewer_own_rows` (the exemption-list form, which fails closed), and every evaluation read applies it:
  - the window, cost and list queries take one SQL condition;
  - the per-execution and by-id reads check who started the run;
  - the chain stops at another user's run with `access`;
  - the stored trend, which can't be split per user, reads empty for a narrowed caller.
- **Test:** `tests/unit/test_936_share_holder_own_runs.py`. It was red on every route before the fix, and the owner reads both runs as the control.

## Checked and clean

- **Routes:** 9 new routes, all GET. Each is gated through `get_authorized_agent_by_name` (uniform 404) or the read scope, and `execution_id` is bound to `agent_name` on the per-execution reads. Static routes are registered before `/{eval_id}` (Invariant #4).
- **Guard tests:** 353 auth guard tests pass, among them the #2996 census, #1890/#2323 admin allowlist, #186 enumeration, ent#629 reach, ent#614 attribution, #918 broadcast, #1159 agent auth, ent#435 settings sink and #292 cmdline leak.
- **Enterprise-docs guard:** the pattern (228 characters, confirmed non-empty) finds 0 hits on added lines of public docs and seam files.
- **Diff hygiene:** no secret prefixes in `git log -p`, no invisible or bidi Unicode, no `v-html` or dynamic links, no SQL string interpolation of request input.
- **MCP:** the new tool is `enforce`-gated on `agent_name`, uses the caller's own key, and has a static description.
- **Read receipts:** the reader comes from the principal and never from a header. Receipts are written only after the access check, and the write is fail-open without changing the response.
- **Model gateway:** failure reasons are static strings, and the secret is held with `repr=False` and never serialised.
- **Judge path** (agent-authored transcript into a model): framing, length caps, credential scrubbing before send, strict output validation and a store-side guard are all verified. Detail is in the private repository's report.

## Coverage gaps

- Phases 3–5 did not run: no dependency, lockfile, Dockerfile, compose or workflow change.
- No Docker pass was run.
- The private submodule range is reported privately (trinity-enterprise#45).
