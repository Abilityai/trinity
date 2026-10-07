# CSO diff audit — 2026-10-06 — trinity-enterprise#792 (file-route path normalisation)

**Mode**: `--diff`, daily (8/10 gate) · **Skill**: cso v1.2.3 · **Base**: `dev` @ `fa50b3888` · **Branch**: `feature/792-double-slash-path`

## Scope

| File | Change |
|---|---|
| `src/backend/services/agent_service/files.py` | `_normalize_user_path` collapses any run of leading slashes; new `_deny_anchor` / `_DENY_ANCHORS` / `_is_user_deletable_path`; `DELETE /files` applies the deny list (and refuses a directory holding a protected path); PUT / mkdir / DELETE forward the normalised path to the agent |
| `docker/base-image/hooks/file-guardrail.py`, `docker/base-image/hooks/read-only-guard.py` | the agent-side `_normalise` collapses leading slashes too (added after the PR review, see Phase 12) |
| `tests/unit/test_files_protected_paths.py`, `tests/unit/test_ent596_skill_manager.py`, `tests/unit/test_ent792_file_guardrail_hook.py`, `tests/unit/test_read_only_guard.py`, `tests/test_files_guardrail_bypass.py`, `tests/registry.json` | tests |
| `docs/memory/requirements/content-files.md`, `docs/memory/learnings/2026-10-06-*.md` | docs |

No dependency, CI, Docker, compose, frontend, schema or MCP change. The base-image change is two hook functions; no file is added, so the image's COPY set is unchanged.

## Phase 0 — model and quicklist

The three backend file-write routes check a user-supplied path lexically, then proxy it to the agent server, which acts on `Path(path).resolve()`. The trust boundary is the backend check: any key that passes `can_user_access_agent` reaches it, including an agent-scoped key acting on a same-owner sibling. The diff changes what the check reads and what is forwarded. It changes no principal or dependency.

Quicklist: no row's component is touched by the diff (no pin changed). Starlette BadHost row: the file routes gate on no URL path; `enforce_agent_capability` reads `request.scope["path"]` for the audit row only. Not re-assessed beyond that in diff mode.

## Phase 1 — attack surface delta

- New endpoints: 0. Auth dependencies changed: 0.
- Narrowed: `DELETE /api/agents/{name}/files` now refuses deny-listed paths and directories holding them; all three write routes refuse any number of leading slashes on path-anchored patterns and on the `skills.manage` fence.
- Agent-context ingress: unchanged.

## Phases 2–8

- **P2 secrets**: added lines scanned for known prefixes — none. No credential is logged (the refusal logs agent, path, username).
- **P3–P6**: no overlap with the diff (no dependency, workflow, Docker or webhook change).
- **P7**: ASI02/ASI03 strengthened. The ent#596 fence and the deny list now agree with the agent server's resolution for every symlink-free path, proven by a property against an independent segment-stack oracle. The `/proc/self/root` and `/proc/self/cwd` doors on DELETE are closed by the existing `/proc/*` row, now applied to DELETE.
- **P8**: no agent-shipped surface changed. Invisible-Unicode scan of changed files: none.

## Phase 9 — OWASP 2025 on the diff

- **A01 (file-write path policy completeness)**: the deny list now covers DELETE, ancestor-aware for path-anchored patterns. Basename-pattern protection on DELETE is name-only (a directory that merely contains a `.env` is deletable), stated in the docstring and the requirement. The agent server's by-name block applies on its side too.
- **A05**: no SQL, subprocess or template sink touched.
- **A09**: the new refusal is logged at WARNING like the PUT and mkdir refusals; `JsonFormatter` encodes CR/LF.
- **A10**: `_is_user_deletable_path` is a pure predicate with no exception path. It fails closed: an empty path is refused, and `/` holds every anchor. The failure direction was unstated in the first draft; the docstring now states it (applied after the audit pass).

## Phase 10 — STRIDE (backend file routes)

Tampering and elevation of privilege are reduced. An agent key without `skills.manage` can no longer write `.claude/skills/**` through a `//` spelling, and no caller can write `.ssh/*` or `.claude/settings*.json` that way or delete them at all. Spoofing, repudiation and disclosure are unchanged; refusals keep the existing audit (`capability_refused`) and log lines.

## Phase 11 — data classification touched

RESTRICTED: `.ssh/*`, `.aws/*`, `.gcp/*`, `.credentials.enc`, `.env*`. CONFIDENTIAL with code-execution impact: `.claude/settings*.json` (hooks) and `.claude/skills/**`. The RESTRICTED set and `.claude/settings*.json` are now refused on DELETE as well as write. `.claude/skills/**` stays deletable by humans and by agents holding `skills.manage`; the ent#596 fence refuses everyone else.

## Phase 12 — findings

**Correction (PR review, same day): one finding the first pass missed, fixed in this PR.** The variant grep was cut at fifteen lines, and the agent-side copy of the policy fell below the cut. `docker/base-image/hooks/file-guardrail.py` and `read-only-guard.py` normalised `file_path` with the same `normpath` call. In the shipped image, `//home/developer/.ssh/authorized_keys` and `//home/developer/.claude/settings.json` exited 0 (allowed) where the one-slash form exited 2. Both `_normalise` functions now collapse leading slashes. `tests/unit/test_ent792_file_guardrail_hook.py` runs the shipped hook's `main()` against the shipped baseline; `test_read_only_guard.py` has `//` rows. Reverting either fix turns its rows red, and the fixed hooks, mounted into `trinity-agent-base`, deny the `//` forms in the real image. Whether Claude Code canonicalises `file_path` before PreToolUse was not checked; the fix does not depend on it.

Variant analysis, rerun in full:
- `canvas_blocks.py` fails closed on `//`;
- `agent_shared_files_service.validate_publish_path` refuses a leading `/`;
- the agent server checks containment after `resolve()`;
- the MCP server has no path classifier;
- the agent-side hooks are fixed above;
- every other `normpath` call in the agent image joins a platform-built path (`pull_worker`, `result_callback`, `retained_results`).

No other finding at or above the 8/10 gate.

Active verification:
- Mutation battery: six backend mutations and two hook mutations, each red.
- In-container probe (`trinity-agent-base`, `--network none`): `//…` and `/proc/self/{root,cwd}/…` resolve into `/home/developer`; no other symlink in the image resolves there.

Out-of-scope observations from the plan review were filed in the private tracker, not described here.

## Hypotheses

None (daily mode).

## Coverage gaps

None for the scoped phases. P3–P6 did not apply, because the diff has no overlap with them. No independent verifier ran, because no finding survived to verify.

## Trend

First diff report for this area. It closes trinity-enterprise#792 at the root cause: the shared normaliser, plus the forwarded string.
