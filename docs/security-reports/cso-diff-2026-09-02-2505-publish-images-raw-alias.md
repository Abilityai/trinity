# CSO Audit — 2026-09-02 (diff mode) — PR #2505 publish-images `{{raw}}` alias + VERSION 0.9.5-rc2

**Mode**: `--diff` (daily gate, 8/10) · **Branch**: `fix/publish-images-prerelease-v-alias` vs merge-base `0f4d54db` (dev tip) · **cso v1.1**

## Scope

3 changed files, +41/−4. Executable surface of the diff: one tag-derivation rule in `.github/workflows/publish-images.yml` (`type=semver,pattern=v{{version}}` → `pattern={{raw}}`) plus a 15-line comment block, the `VERSION` file (`0.9.5-rc1` → `0.9.5-rc2`), and a string-assertion test file (excluded by rule 8).

## Attack-surface delta (Phase 1)

- **0** new endpoints, WS channels, schema, dependencies, credential paths.
- **1** existing workflow modified, in its tag list only. Permissions unchanged (`contents: read`, `packages: write`). No new secrets.

## Findings

**None at the 8/10 gate.** (Zero findings ⇒ no independent-verifier pass and no remediation roadmap — nothing survived to verify.)

## Security-relevant observations (not findings)

1. **`{{raw}}` adds no channel**: it emits the git ref name as the image tag; only a semver-parsable `v*` tag reaches it, it cannot produce `latest`, and every version rule stays gated on `github.event_name == 'push'`.
2. **Pre-existing, unchanged, write-access-gated**: the `latest` guard tests "starts with `v` and has no hyphen", not "parses as semver" — a non-semver tag push such as `vnext` would move `latest` while the semver lines emit nothing. Pushing tags needs repo write access, so this is a robustness hardening candidate for a follow-up, out of this PR's scope.
3. **Supply chain**: the four `docker/*` actions are SHA-pinned and each pin was resolved against GitHub — every SHA matches the version its comment claims (setup-buildx 4.3.0, login 4.6.0, metadata 6.2.0, build-push 7.3.0). `actions/checkout@v7` is the repo-wide first-party convention (28 uses), filtered under hard-exclusion 5.
4. **No script-injection sink**: the only `${{ }}` inside a `run:` body is `${{ matrix.image }}` in an `::error` string; matrix values are workflow-static. `inputs.ref` reaches the shell only via `env:`. `VERSION` is newline-stripped before entering `$GITHUB_OUTPUT` and reaches build args via `env:` only.
5. **Context**: no release-tag image (`0.9.0` / `v0.9.0`) exists on GHCR; `v0.9.5` will be this workflow's first live release run.

## Mechanical scans (Phase 2/4, diff-scoped)

- Secret patterns in the two branch commits: **none**. `.env` not tracked. Enterprise-disclosure guard scope (docs / open-core seam files) untouched.
- `pull_request_target`: **none**. Third-party actions: **all SHA-pinned, pins verified**.

## STRIDE delta (CI publish pipeline)

Spoofing / Repudiation / Information Disclosure / DoS / Elevation: unchanged. Tampering/Integrity: slightly improved — the pre-release tag set is now complete (`v0.9.5-rcN` published) without adding any mutable pointer.

## Trend

Prior diff report (`cso-diff-2026-09-01-2468-headless-tool-audit`): 0 findings. This report: **0 findings**. Direction: flat at zero.

---
*AI-assisted scan (read-only, diff-scoped); not a substitute for a professional audit.*
