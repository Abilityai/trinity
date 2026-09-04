# CSO Audit — 2026-09-01 (diff mode) — #2468 headless tool-family deny + counterweight

**Mode**: `--diff` (daily gate, 8/10) · **Branch**: `feature/2468-headless-tool-audit` vs merge-base `efc6d7db` (dev tip) · **cso v1.1**

## Scope

10 changed files, +719/−6. Executable surface of the diff: a data/comment change in a stdlib-only agent-server module (`_runtime_config.py` — deny tuple 1→11, new `PLATFORM_KEPT_TOOLS` + `AUDIT_CLI_VERSION`), static prompt text (`platform_prompt_service.py`), one operator-side dev script (`scripts/dev/audit_headless_tools.py`), one test file, docs.

## Attack-surface delta (Phase 1)

- **−11 agent-visible harness tools** on every Claude spawn (both spawn sites, single builder — the deny only ever removes capability).
- **+1 static always-tier prompt section** — no interpolation, no user input, no injection channel.
- **+1 operator dev script** — docker-exec caller, list-form subprocess args, `shlex.quote` on every composed value, bounded timeouts, no credential handling.
- **0** new endpoints, WS channels, schema, dependencies, CI workflows, credential paths.

## Findings

**None at the 8/10 gate.** (Zero findings ⇒ no independent-verifier pass and no remediation roadmap — nothing survived to verify.)

## Security-relevant observations (not findings)

1. **Containment improvement**: denying `SendMessage`+`ListAgents` closes an *unaudited cross-execution channel* — concurrent executions of one agent share a container HOME (up to `max_parallel_tasks`, possibly different callers' turns), and these tools advertise discovery + messaging of "other local Claude sessions on this machine", outside Trinity's permissioned `chat_with_agent` path. The channel's exploitability was never measured (plausible mechanism, conservative deny); recorded in the audit prose.
2. **Trust-model residual (unchanged)**: `scripts/dev/audit_headless_tools.py` reads the container's own CLI output — a compromised agent can render its own probe CLEAN. Equal trust to an agent reporting its own state (the #2467 forged-stream class); the script is an operator dev tool, not a control plane.
3. **Drift residual (mitigated in-diff)**: `--disallowedTools` treats an unknown name as a silent no-op, so a CLI rename re-offers a denied tool's false promise. Mitigations shipped: `AUDIT_CLI_VERSION` as data, the exact-cover test snapshot (a version bump forces a re-measure), the probe script; CI enforcement registered in the debt inbox (needs a credentialed agent CI lacks).
4. **Mixed-fleet honesty**: the prompt's family paragraph is phrased as a capability fact ("dies with this process"), never a policy fact, so the backend-deployed prompt does not assert a deny the pre-rebuild fleet image doesn't yet enforce.
5. **FP cleared**: the `sk-` secret-scan pattern vs `task-notification` in audit comments — analytically (`\b`-anchored rules cannot match between two word characters) and empirically (PR #2472's identical strings passed the gitleaks job).

## Mechanical scans (Phase 2/5/6, diff-scoped)

- Secret patterns in added lines: **none** (`sk-`/`ghp_`/`gho_`/`github_pat_`/`xox*`/`AKIA`/`whsec_`/PEM headers).
- TLS-off / `shell=True` / `os.system`: **none**.
- Internal URLs / IPs / emails: **none**.
- Vendored-parity (Invariant #5): `_runtime_config.py` is **not** a vendored pair (agent-only; the backend never imports it — tests load it by path) — no parity obligation created.
- No `.env`, compose, Dockerfile, workflow, or dependency changes.

## STRIDE delta (agent container)

Tampering ↓ (fewer tools), Information Disclosure ↓ (cross-execution channel closed), Elevation of Privilege ↓ (RemoteTrigger's external control plane removed from agent reach); Spoofing/Repudiation/DoS unchanged.

## Trend

Prior diff report (`cso-diff-2026-09-01-2467-turn-integrity`): 0 findings. This report: **0 findings**. Direction: flat at zero across the sibling-issue pair.

---
*AI-assisted scan (read-only, diff-scoped); not a substitute for a professional audit.*
