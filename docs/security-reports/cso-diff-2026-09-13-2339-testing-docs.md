# CSO diff audit — abilityai/trinity#2339 (`feature/2339-testing-docs-strategy`)

`cso v1.1` · mode `--diff` · confidence gate 8/10 · 2026-09-13

## Verdict

**Clean.** The diff adds no attack surface: one new strategy document, sixteen renames (fifteen dated testing documents into `docs/archive/testing/`, one feature test record into `docs/planning/`), pointer edits in eleven live documents, two new test modules, three docstring-or-string hunks in existing code, two `harness:` paths in the journey catalog, and three text-inserted test-registry entries. No endpoint, dependency, workflow, container, webhook or prompt surface changes.

## Attack surface introduced by the diff

| Category | Count |
|---|---|
| New / changed endpoints | 0 |
| New DB reads / Redis keys | 0 |
| New or widened prompt surfaces | 0 |
| Dependency / workflow / Docker / compose changes | 0 |
| New test files | 2 (`tests/unit/_markdown_links.py`, `tests/unit/test_2339_testing_docs_consolidated.py`) — read git-tracked files as bytes through one fixed-argv `git ls-files`, import nothing from `src/`, execute nothing |
| Code hunks | 3 documentation-path strings (`services/pull_pilot.py` log text, `canary/invariants/e01_*` docstring, `test_1081` docstring) + one removed parametrize entry in `test_2008` |
| Doc files | 1 new, 11 edited, 16 renamed |
| Private submodule | `.claude` moved in the working tree only (three pointer lines in `skills/tidy`, `skills/read-docs`, `skills/ui-sweep`, pushed to trinity-dev `main`); not staged in this PR |

## Findings

None in scope.

## Verification performed

- **Secrets (P2):** 0 secret-shaped strings (`sk-`, `ghp_`, `gho_`, `github_pat_`, `xox*`, `AKIA`, private-key headers, `password=`/`secret=` literals, private-range IPs) in the added lines; no commits on the branch yet, so the working-tree diff against merge-base `2c5cfe0e3` was scanned. The three regex hits were `+@pytest.mark.parametrize` lines (the diff's `+` is a valid local part) — the known false positive.
- **Enterprise-disclosure guard (P2, trinity-enterprise#45):** fifteen documents leave the guard's live scope by moving under `docs/archive/`, so the exact PCRE from `.github/workflows/enterprise-docs-guard.yml` was replayed over every moved file (0 hits) and over every new or changed live document, including `docs/testing/STRATEGY.md` (0 hits). The one hit in the tree (`README.md:780`) is a pre-existing line outside both the diff and the guard's scope.
- **P3–P7:** not applicable — no dependency, CI, Dockerfile/compose, webhook, integration or LLM-surface files changed (stated, not skipped). The `pull_pilot.py` hunk changes a documentation path inside an existing warning string; no new data reaches the log line.
- **P8 (skill supply chain):** the three edited private skill files were scanned as executable prompt code: 0 network calls, 0 credential-env reads, 0 injection phrases in the added lines.
- **P9–P11:** tests and docs are hard-excluded (#8, #12); the code hunks are strings and docstrings. STRIDE over the new guard: it reads tracked files and cannot tamper with, disclose, or escalate anything; its only process is `git ls-files` with a fixed argument vector, no shell, `cwd` pinned to the repo root.
- **P12:** eight candidates screened; six cleared in-context; two pre-existing hygiene items in renamed files were fixed in-flight (appendix). No candidate reached the 8/10 gate, so no fresh-context verifier was needed.

## Appendix (non-blocking)

- **A1 — PRE-EXISTING, LOW, fixed here.** `docs/archive/testing/302-settings-tabbed-layout-manual-test-plan.md` carried a developer's home-directory path in a shell example (public since 2026-06-01; the #2091 class `/validate-pr` greps for). Replaced with a placeholder path; no technical content changed.
- **A2 — PRE-EXISTING, LOW, fixed here.** `docs/archive/testing/TESTING_GUIDE.md` carried a company-domain test address in a login example (public since 2025-12-10). Replaced with `user@example.com` per the CLAUDE.md placeholder rule.
- **Trend:** flat against the 2026-09-12 diff audit (#2337). That audit's appendix A1 is tracked privately as abilityai/trinity-enterprise#614 and is untouched by this branch.
