# CSO diff audit — trinity-enterprise#787 (2026-10-06)

`cso v1.2.3` · `--diff` daily (8/10 gate) · branch `feature/ent787-guardrail-exec-form` vs merge-base `84b586b81` · diff run, not a full run.

## Result

**0 findings** (critical 0, high 0, medium 0, low 0).

## Checks run

| Check | Result |
|---|---|
| Secret prefixes in diff + untracked files | none |
| Concealed Unicode in hooks + new tests | none |
| Root exec argv for the read-only config | constant script; the config JSON reaches the shell as positional data (`printf '%s' "$3"`); executed in tests with quote, backtick, `$()` and format-string payloads |
| GUARD-002 hooks vs agent-controlled shell prefix, `PYTHONPATH`, user site | closed: exec form, `env -i`, `-I -S`; verified in a built image with a hostile workspace, with the old invocation as control |
| `CLAUDE_CODE_SHELL_PREFIX` from `.env` / settings `env` | closed: `PROTECTED_KEYS` + policy `env` pin; verified in a built image |
| Read-only config writable without `sudo` | closed while the root file exists (root:root 0444, root-owned dir; agent `rm` refused in a built image) |
| Invariant #17 | unchanged |
| Failure direction (A10) | stated in the readers: root file decides when present; home copy only while it is missing |

## Considered and dismissed

- Home-copy fallback while the root file is missing: owner-accepted for this PR, registered as debt.
- An agent using passwordless `sudo`: stated limit, not a boundary.
- Hook timeout (30 s) lets the tool run on timeout: CLI behaviour; hooks finish well under 1 s.
- The shipped smoke script deletes the root config if run as root: needs `sudo`.

## Coverage gaps

- A real PreToolUse Bash deny through a logged-in Claude Code CLI is not exercised here (needs model auth); covered by `/verify-local`.
