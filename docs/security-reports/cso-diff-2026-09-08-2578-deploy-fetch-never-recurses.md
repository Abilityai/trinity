# CSO diff audit — abilityai/trinity#2578 (`feature/2578-no-recurse-submodules`)

**Date**: 2026-09-08 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `818e7f5f` on `dev` · **Diff**: 5 tracked files +~130 / −12 plus 1 new test file (working tree, pre-commit). Surface touched: one GitHub Actions workflow (`.github/workflows/deploy-dev.yml`), two docs (`docs/ENTERPRISE.md`, `docs/memory/requirements/infrastructure.md`), the learnings ledger, the test registry, and `tests/unit/test_2578_deploy_fetch_never_recurses.py`. No backend, frontend, agent, Docker, compose, dependency or `.env` change.

## Verdict
**No findings at the gate.** The diff removes an implicit network operation from the dev deploy (the superproject fetch no longer recurses into a submodule over its stored SSH URL), makes a stale enterprise tree a failed deploy instead of a warned one, and adds a hermetic test that cannot reach a real remote. Four candidates were examined; one was refuted by experiment, three fall under the hard exclusions. Observations recorded below the gate.

## Attack surface introduced by the diff
- **Endpoints, WebSocket channels, MCP tools, Docker permissions, network edges**: none added or changed.
- **CI/CD** (`deploy-dev.yml`): the `=== Pull ===` block's `git fetch`, `git checkout` and `git pull` gain `--no-recurse-submodules`; the submodule block raises `ENT_STALE=1` and runs the repo-controlled `scripts/ci/classify-submodule-failure.sh` when the checked-out enterprise tree does not match the recorded gitlink; the registration section prints a `::error::` for that state and exits 1 after the existing registration checks; the auto-filed incident body gains a stage bullet. No new `${{ … }}` expression, no new secret reference, no new action, `permissions: contents: read` unchanged, third-party actions still SHA-pinned (unchanged lines).
- **Trust-boundary effect**: the deploy's fetch previously attempted an SSH connection from the VM to GitHub as a side effect of a pointer bump, with whatever host and key material the VM had; it no longer does. The only submodule transport left is the existing per-command PAT rewrite, unchanged.
- **Test**: real `git` in `tmp_path` with every inherited `GIT_*` variable dropped, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`, a fake `ssh` that records its argv and refuses, and file transport for everything else. It asserts the ssh dial log is empty after the workflow's block, so a regression that made the test reach a real remote would fail rather than pass quietly.

## Findings
| # | Sev | Conf | Status | Category | Finding | Phase | File:Line |
|---|-----|------|--------|----------|---------|-------|-----------|
| — | — | — | — | — | none at the gate | — | — |

## Verification performed
- **Secrets (P2, diff)**: no commits on the branch yet, so the working diff was scanned instead of `git log -p`. Known-prefix scan over every added line (AWS `AKIA`, OpenAI/Anthropic `sk-`, GitHub `ghp_`/`gho_`/`github_pat_`, Slack `xox*`, private-key headers, `x-access-token:<token>@`) — 0 matches. Internal-identifier scan (IPv4, emails, `/Users/`, `/home/`, internal domains, Tailscale ranges) — only `test@example.com`, `github.com` and the `${PAT}` placeholder the docs page already uses. No tracked `.env`. The enterprise-disclosure guard's PCRE pattern run locally (Python `re`) over every changed doc, the workflow and the test — 0 hits; the guard workflow still greps `docs/`, `CLAUDE.md` and the five seam files.
- **CI/CD (P4)**: the two new `::error::`/`::warning::` strings interpolate only `${HAVE}`/`${WANT}`, which are `git rev-parse` outputs (40-hex or the literal `none`), so no attacker-controlled text reaches a workflow-command annotation. The classifier call mirrors the existing not-mounted branch (`|| true`, exit 0 by contract). The new `exit 1` path lands in the existing `notify-failure` job, which already holds the only `issues: write` grant.
- **Credential redaction (P5/P9 A09)** — the one credential-adjacent line: the stale branch's classifier can echo the last 20 lines of the captured `git submodule update` output in its `unknown` class. Experiment: `git -c "url.https://x-access-token:<value>@127.0.0.1:1/.insteadOf=git@github.com:" ls-remote …` and the `clone` form both fail with `fatal: unable to access 'https://127.0.0.1:1/…'` — git strips the credential from the URL it prints (0 occurrences of the value in either output). The same captured output has been printed in full, unconditionally, by the existing `sed 's/^/ENTERPRISE| /' "$ENT_LOG"` line since #2246, and GitHub masks registered secret values in job logs. REFUTED as a finding.
- **Injection (P9 A03)**: no SQL, `subprocess` with user input, `eval`, `v-html` or `innerHTML` in added lines; the test's `subprocess.run` calls use argument lists (no shell) except the deliberate `bash -e <file>` over the workflow's own extracted block, whose content is repo text.
- **Integrity (P9 A08)**: the enterprise tree is still brought to the recorded gitlink by `git submodule update` (content-addressed checkout of the exact commit); the change adds no path that could substitute a different tree. A tree that does not match the gitlink now fails the run instead of deploying under a warning.
- **Supply chain (P3)**: no dependency, lockfile, Dockerfile or action-version change in the diff.
- **LLM / MCP / webhooks / skills (P6, P7, P8)**: not touched; the `.claude` pointer difference in the working tree is not part of the change and is never staged.
- **STRIDE (P10, deploy pipeline)**: Spoofing — unchanged (SSH key + Tailscale secrets). Tampering — the superproject is fast-forwarded only; the submodule is checked out at the recorded commit or the run fails. Repudiation — the stale cause is now classified and printed next to the evidence; the incident body names the stage. Information disclosure — one fewer transport attempt; redaction verified. Denial of service — a stale tree now fails the dev deploy deliberately (intended, out of scope per hard exclusion 1). Elevation — none.
- **Data classification (P11)**: RESTRICTED material in this workflow (`DEV_SSH_KEY`, `TAILSCALE_AUTH_KEY`, `ENT_SUBMODULE_PAT`) is referenced only through unchanged `${{ secrets.* }}` lines; the test uses synthetic identities and local repositories only.
- **Independent verification**: no finding survived the gate, so no verifier subagent was needed.

## Observations (below the gate, not findings)
- A superproject commit that removes the gitlink while the VM still holds the tree now fails the deploy (`WANT=none`); the error text names the remedy (`git submodule deinit -f …`, then `DEPLOY_ALLOW_OSS_ONLY=true` for a host meant to run OSS-only). Intended.
- The `.dirty` suffix on the built VERSION when the tree is stale is a truthful side effect of the existing `git diff --quiet HEAD` check, now named in the error text and the docs.
- Acceptance criterion 3 of #2578 (a real pointer bump deploying green on its own run) is observable only after merge; no bump is pending.

## Hardening shipped in this diff
- The dev deploy's fetch, checkout and pull can no longer dial a submodule remote as a side effect of a pointer bump.
- A stale enterprise tree is a failed deploy with a classified cause, not a green run with a warning.
- The static guard scans every `git fetch`/`git pull`/`git checkout` in the deploy script, so a later fetch cannot reintroduce the class silently; an 11-way mutation meta-test proves the guard goes red on the pre-fix text.
