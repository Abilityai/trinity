# CSO Diff Audit — 2026-09-30 — #3105 (Bash guardrail denies sudo routes to the guardrail registration)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/3105-sudo-guardrail-deny` → `dev` · **Skill**: cso v1.1 · **Audited**: working tree against `origin/dev` (`729423f9b`).

## Architecture (Phase 0)

- `docker/base-image/hooks/guardrails-baseline.json`: two `bash_deny` regexes. `bash-guardrail.py` runs them with `re.search` over the full Bash command before execution. The command is agent-authored, and the agent is the principal the hook constrains.
- `docker/base-image/agent_server/routers/info.py`: agent `/health` gains `guardrails_registration`, an enum from `os.stat`/`os.access` on `/etc/claude-code/managed-settings.json`.
- Docs and one unit test.

Trust boundary: agent-authored Bash command → guardrail hook (same container, same UID for the command; hook runs under Claude Code with a timeout).

## Attack surface (Phase 1, diff-scoped)

| Surface | Change |
|---|---|
| Bash PreToolUse hook | Two new deny patterns. |
| Agent `/health` (unauthenticated, agent network) | One enum field. |
| Endpoints, MCP tools, WebSocket, migrations, dependencies, CI, Docker/compose, vendored policy files, skills | Unchanged. |

## Findings

None open. One finding was fixed on the branch during the audit.

### Fixed during audit — regex backtracking outlasts the hook (HIGH, 8/10, VERIFIED)

- **Where**: `guardrails-baseline.json`, the root-shell pattern and the sudo-path pattern.
- **Cause**: sudo option tokens had two ways to match (`-u` as an argument-taking flag or a bare flag, and `-u` could take the next `-u` as its argument), so matching `sudo -u -u … -u true; sudo -i` was exponential in the number of `-u` tokens: 1.58s at 30 tokens, growing about 1.6× per token. The sudo-path pattern was quadratic: 19s on 40,000 repeated `sudo ` words.
- **Exploit**: the agent sends `sudo` + ~45 × `-u ` + `true; sudo -i`. The hook runs past its timeout. A timed-out PreToolUse hook does not return exit 2, so the command runs and the root-shell deny is skipped.
- **Fix**: each option token has one match path (`-[ugCDhprtUT]` takes an argument, the bare-flag branch excludes those letters). The path pattern is anchored at `\A` with a `sudo` lookahead. Crafted inputs above now run in ≤0.02s. `test_patterns_do_not_backtrack` pins it.
- **Assumes**: Claude Code treats a hook timeout as a non-blocking error (the command proceeds). Not observed in a live container.

### Candidates considered and not reported

- `/health` discloses to agent-network peers whether this agent's registration is intact. Enum only, no path or content; below gate.
- Regex evasion (variables, `base64`, script files, `sudo -e`/`sudoedit`). Documented as a speed bump in the issue and the docs; the control is defence in depth.
- A `sudo` rewrite that keeps root:root `0444` reads `ok` on `/health`. Documented limit.

Phases 2–8 and 11: no secrets, dependencies, workflows, Dockerfiles, webhooks, prompts or skills in the diff.
