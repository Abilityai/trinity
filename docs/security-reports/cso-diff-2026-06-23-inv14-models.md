# CSO Security Audit

**Mode**: diff
**Scope**: `AndriiPasternak31/autoplan-issue-654` vs `origin/dev` (#654 INV-14 model centralization, PR #1308)
**Date**: 2026-06-23
**Diff size**: 37 files, +1548 / −1092

## Summary

| Category | CRITICAL | HIGH | MEDIUM | LOW |
|----------|----------|------|--------|-----|
| Secrets | 0 | 0 | 0 | 0 |
| Dependencies | 0 | 0 | 0 | 0 |
| Auth Boundaries | 0 | 0 | 0 | 0 |
| Injection | 0 | 0 | 0 | 0 |
| Platform Patterns | 0 | 0 | 0 | 0 |
| Configuration | 0 | 0 | 0 | 0 |

## Nature of the change

Pure refactor enforcing Architectural Invariant #14: ~97 Pydantic request/response
models are relocated out of 32 router modules into the centralized `src/backend/models.py`.
No business logic, route handler, auth dependency, or persistence code is added.
A new AST static guard (`tests/unit/test_models_centralized.py`) forbids `class X(BaseModel)`
under `routers/` going forward, with a single documented allowlist entry
(`canary.py::RunCycleRequest`, justified by a class-definition-time dependency inversion).

## Verification performed

1. **Secrets** — no hardcoded credentials, tokens, or API keys introduced
   (pattern scan on added lines clean). No `.env`/manifest files in scope.
2. **Dependencies** — no `requirements*.txt` / `package.json` / `pyproject` changes;
   no new packages, no version bumps → no new CVE surface.
3. **Auth boundaries** — **zero** `Depends(...)`, `require_admin`, `require_role`,
   `AuthorizedAgent`, `OwnedAgentByName`, or `get_current_user` lines added or removed
   across all 32 router diffs. Route handler signatures (and their guards) are untouched.
4. **Injection / boundary validation** — every moved `@field_validator` was confirmed
   **byte-identical** in `models.py` (fan-out task-id regex `^[a-zA-Z0-9_-]{1,64}$`,
   task/concurrency/timeout bounds, `policy` whitelist, loop `stop_signal` normalization).
   The constants backing those validators moved with **identical values**
   (`TASK_ID_RE`, `MAX_TASKS=50`, `MAX_CONCURRENCY=10`, `MAX_RUNS_LIMIT=100`,
   `MAX_MESSAGE_LEN=100_000`, `MAX_STOP_SIGNAL_LEN=200`, `MAX_DELAY_SECONDS=3600`,
   `MAX_TIMEOUT_PER_RUN=7200`, `CONTEXT_MAX_CHARS=4000`) and are no longer
   duplicated in the routers (no divergent definition can drift).
5. **Security-relevant defaults** — permission/access fields (`require_email`,
   `open_access`, `read_only`, `group_auth_mode="none"`, `validation_enabled=False`,
   `welcome_enabled=False`, `allow_proactive`) appear identically on both `+`/`-` sides
   of the diff — moves, not flips. No default weakened.
6. **Import integrity** — each router now imports its models from the centralized
   `models` module; the prompt-injection-surface cap `CONTEXT_MAX_CHARS` is correctly
   imported by `webhooks.py` rather than dropped.

The PR additionally carries a byte-identical OpenAPI proof (covers field constraints,
required-ness, and defaults); this audit independently verified the custom-validator
logic that OpenAPI does not fully capture.

## Findings

#### CRITICAL
None

#### HIGH
None

#### MEDIUM / LOW
None

## Recommendation

**CLEAR** — no security impact. The change is a mechanical, behavior-preserving
relocation of model definitions; input validation and authorization surfaces are
provably unchanged, and a new static guard prevents regression of the invariant.
