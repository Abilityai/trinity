# CSO diff audit — #3868 agents may write their own `.env.example`

- Mode: `--diff`, daily (8/10 gate). Base `origin/dev`. 6 files changed plus `tests/unit/test_3868_env_example_writable.py`.
- Result: **0 findings** (0 SUPPORTED, 1 hypothesis).

## Change under audit

`guardrails-baseline.json` gains `path_allow: [".env.example"]`. In `file-guardrail.py`, a basename on that list skips the slash-free `path_deny` patterns (`.env`, `.env.*`, `.mcp.json`, `.credentials.enc`, `.claude.json`). Absolute patterns, operator `extra_path_deny` and symlinks still deny. The platform prompt names the exception beside `.env.*`.

## Checks

- **Who can widen the exception:** `write-runtime-config.py:20` overrides only `max_turns_*`, `execution_timeout_sec`, `extra_bash_deny`, `extra_path_deny`, `disallowed_tools`. `path_allow` comes from the root-owned image baseline only. Env and the agent cannot add names.
- **Failure direction:** a missing or empty `path_allow` keeps today's deny (`config.get("path_allow", []) or []`). An exception inside the hook exits 2 through `run_hook` (fail closed). Both pinned by tests.
- **Exact name only:** `basename in allow` is string equality. `.env.example.bak`, `.env.local`, `.env.production` stay denied (tested). `.ENV.EXAMPLE` and `.env.example/x` were never denied (case-sensitive fnmatch; children match by their own basename), so the allow opens nothing there.
- **Protected directories:** `/home/developer/.ssh/.env.example`, `/opt/trinity/.env.example`, `/etc/claude-code/.env.example` stay denied by the absolute patterns (tested).
- **Symlinks:** a `.env.example` that is a symlink is denied (`os.path.islink`, mutation-tested). A symlinked parent directory or a hardlink to `.env` passes, but the guardrail is accident prevention: Bash is not path-gated and can write `.env` directly, and the prompt says so. No boundary is crossed that was not already open.
- **Prompt text:** static string built at import from two constants; no agent- or user-authored input.
- **Diff hygiene:** no credential prefixes, no invisible Unicode (tag block, zero-width, bidi) in the added lines.

## Hypothesis (not counted)

An agent can type a real value into `.env.example`, and git sync pushes it. Bash and the template author could already do this, so the change adds no route. `K-004` soft-checks placeholder values; git sync has no credential scan.

## Coverage gaps

- Docker pass not run: no Dockerfile, dependency or compose change. The hook was run as the shipped file under pytest, and `guard002-smoke.py` gains matching rows for the image build.
- No independent verifier: no findings to verify.
