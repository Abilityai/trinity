# CSO diff audit — abilityai/trinity-enterprise#1279

**Date**: 2026-11-25 · **Mode**: `--diff`, daily (8/10 gate) · **Base**: `feature/ent1278-agent-token-fetch`

Scope: backend GitHub call sites through the repo-token provider seam (`services/github_token_provider.py`), the create path (`agent_service/crud.py`), the env writer and recreate matcher (`lifecycle.py`, `helpers.py`), the token route and `/git/initialize` (`routers/git.py`), fork/bind messages (`fork_to_own.py`), the skills-library sweep (`skill_service.py`). The private provider module changed too; it is described here only through the public seam.

## Findings

None at or above the 8/10 gate.

## Checks run

| Check | Result |
|---|---|
| Secrets in the diff (`AKIA`, `sk-`, `ghp_`, `github_pat_`, `xox`, `re_`) | no hits |
| Enterprise-disclosure guard pattern over changed docs and seam files | no hits |
| Credential in argv or URL userinfo in new subprocess / exec call sites | none added; git auth stays on `git_auth_env` (environment header) |
| Token values in new log lines or error details | none; new 503/400 details carry the repo name and a code only |
| Installation token baked into a container env | refused at the single env writer (`_apply_github_env`, `ghs_` guard) and by the broker gate; pinned by tests and mutation |
| Installation token persisted | the persist site keys on `fork`/`per_user`; tier `app` never matches |
| Platform PAT fallback for a covered repo | create path, template read, skills sweep and template welcome all stop on a failed mint; pinned by tests and mutation |
| Write credential to a pull-only agent | token route requests `contents: read` unless `source_mode` is false; an absent attribute reads as pull-only (least privilege) |
| Working branch on a shared repo through the app | default path pull-only; explicit `source_mode=false` refused with 400 `GITHUB_APP_SHARED_REPO` |
| Rebuild-from-nothing handing the platform PAT to a working-branch agent | closed where a provider is configured (unknown coverage keeps the broker, uncovered gets no PAT) |
| Guard tests: `test_ent435_settings_sink_guard`, `test_292_cmdline_credential_leak`, `test_2996_human_only_routes`, `test_1310_auth_wiring`, `test_293_admin_gate_rejects_agent_keys`, `test_186_enumeration_uniformity`, `test_agent_auth_header_guard`, `test_credential_sanitizer_backend` | 275 passed |
| A10 failure direction of new guards | stated in code: `is_configured` fails closed (an erroring check counts as configured); `_apply_app_kind_default` treats a failed grant lookup as pull-only; `_create_time_token` fails closed with 503 |
| New routes | none; the existing token route and `/git/initialize` keep their auth (`assert_own_agent` agent-only; `OwnedAgentByName` + `require_person`) |

## Design notes (not findings)

- The provider's reach is shared by every creator, as the platform PAT's is. A creator can read any repo the installation covers and, when the repo is writable and unbound, get a working branch on it. The platform PAT already allows the same with an explicit `source_mode=false`. The template-source allowlist still applies.
- Provider caches are now also touched from worker threads (`token_for_repo_blocking`). A race costs one extra mint; no token is shared across fingerprints.

## Hypotheses

None (daily mode).

## Coverage gaps

- Phase 3 (dependencies): no dependency or lockfile changed in the diff; no scanner run.
- Phase 4 (CI): no workflow changed.
- Docker-image checks: not applicable to this diff; no image, compose or base-image file changed.
- Full public unit suite: delegated to CI.
- Live GitHub behaviour (installation lookup fields, permission intersection against a real read-only installation): not exercised; unit tests mock the GitHub responses.
