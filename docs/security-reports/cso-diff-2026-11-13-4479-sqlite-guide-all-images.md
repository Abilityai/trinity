# CSO diff review — #4479 SQLite → PostgreSQL guide and `start.sh` refusal name every platform image

- **Date:** 2026-11-13
- **Mode:** daily, `--diff` (8/10 confidence gate)
- **Scope:** `docs/migrations/SQLITE_TO_POSTGRES.md`, `scripts/deploy/start.sh` (one `echo` in the `ensure_database` refusal), `tests/unit/test_3159_hosted_postgres.py`
- **Result:** no findings

## What changed

Wording only. The refusal message and guide step 3 name all four source-built services
(`backend frontend mcp-server scheduler`) instead of the backend alone, and the guide's rollback
sentence adds the image step. No code path, endpoint, schema, dependency, workflow or compose file changes.

## Checks run

| Check | Result |
|---|---|
| Secret prefixes, addresses, emails, hostnames in added lines | none |
| Invisible / bidi Unicode in the three files | none |
| Private-module tokens in added lines (enterprise-docs guard class) | none |
| Shell: new `echo` adds no new expansion (same `${COMPOSE_FILES[*]:-}` as the line it replaces) | no new injection surface |
| Service names exist in `docker-compose.yml` and `docker-compose.prod.yml` | all four present |
| `bash -n scripts/deploy/start.sh` | passes |
| `tests/unit/test_3159_hosted_postgres.py` | 31 passed (2 new, red before the change) |

## Security relevance of the fix

The defect let a source-built install run a new backend beside the previous release's scheduler and MCP
server while reporting healthy. Security fixes shipped in those two services would silently not apply
after an upgrade done as instructed. The change closes that for operators who follow the text.

## Findings

None.

## Hypotheses

None (daily mode).

## Coverage gaps

- Phases outside the diff (dependencies, CI/CD, infrastructure, agentic, OWASP, STRIDE) were not run: no
  file in their scope changed.
- No Docker pass and no independent verifier: nothing to verify.
- The full unit suite was not run locally (host Python is older than the repo pin); only the changed test
  file ran. CI is the authority for the rest.
- `shellcheck` is not installed on the host.
