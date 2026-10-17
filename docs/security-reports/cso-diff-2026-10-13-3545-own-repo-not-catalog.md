# CSO diff audit: #3545 own repository treated as a catalog template

- Date: 2026-10-13. Mode: daily, diff. Base: `origin/dev@05612cd78`. Skill: cso 1.2.3.
- Scope: `src/backend/services/template_service.py`, `src/backend/services/agent_service/crud.py`, two unit test files.
- Result: 0 findings at the 8/10 gate. This is a partial run (no Docker pass).

## Change

`POST /api/agents` decided the git mode with `catalog_template=bool(gh_template)`. `get_github_template()` builds a template for every `github:` id, so every create with a personal token was pull-only. The create path now asks `is_catalog_github_template(repo_path)`.

## Boundary reviewed

Property: an agent never receives a working (push) branch on a repository its creator does not own.

A grant needs all of:

1. Not fork-to-own, `source_mode` not set explicitly, `kind` agent, not ephemeral.
2. A token of tier `per_user` or `per_agent`.
3. Repository not in the catalog (admin list, else registry, plus bundled defaults; case- and `.git`-insensitive).
4. Token login equals the repository owner (case-insensitive).
5. Push probe returns ok.

Conditions 1, 2, 4 and 5 are unchanged. Condition 3 was always true before the change, so 4 and 5 never ran.

Widened: repositories the creator's own GitHub account owns and can push to.
Tightened: a respelled catalog repository (`Owner/Repo`, `owner/repo.git`) is still the catalog repository.

## Below the gate (hypotheses, confidence 4/10)

| Case | Why it is not a finding |
|---|---|
| Registry unreadable, so a registry template reads as not-catalog | Conditions 4 and 5 still apply. Only the repository's personal-account owner passes, and that account can already push with the same token. Failure direction is open and stated in the docstring. |
| A renamed catalog repository's old name reads as not-catalog | Same bound. Follow-up: compare the canonical `full_name` from the repository API. |

## Phase notes

- Secrets (P2): diff carries no credential, URL with userinfo, or new log line. No new `system_settings` writer.
- Agentic (P7): no auth dependency, route, MCP tool, or agent-context ingress changed. ASI03 reviewed as above.
- OWASP (P9): A01 reviewed as above. A10: the one new guard states its failure direction.
- Invisible-Unicode scan of the diff: no hit.

## Coverage gaps

- Stack CVE quicklist not re-assessed (no dependency, image or workflow in the diff).
- No dependency scanner run (not applicable in diff).
- Auth guard tests (`test_293`, `test_2996`, `test_1310`) not run: the diff changes no auth dependency or route.
- GitHub push behaviour through a repository-rename redirect not tested.
- Docker pass not run.

## Out-of-diff observation

`crud.py`: the "Dynamic GitHub template" branch of `_resolve_github_repo_and_pat` and the copy-intent `else` branch are unreachable, because `get_github_template()` builds a template for every `github:` id.
