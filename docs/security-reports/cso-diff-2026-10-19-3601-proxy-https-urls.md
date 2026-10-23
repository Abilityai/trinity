# CSO Diff Audit — #3601 / #2907 webhook and MCP URLs over https behind a proxy

- **Date**: 2026-10-19 · **Mode**: `--diff` (daily gate 8/10) · **Branch**: `feature/3601-proxy-https-urls` vs `origin/dev` (merge-base `e0cabc5af`)
- **Scope**: `src/backend/routers/schedules.py`, `src/backend/routers/settings/mcp_url.py`, `src/frontend/src/components/settings/McpKeysTab.vue`, `src/frontend/src/views/Settings.vue`, `docs/DEPLOYMENT.md`, two feature-flow docs, `tests/unit/test_3601_2907_proxy_scheme_urls.py`

## Verdict: 0 findings.

The diff routes two advertised URLs through the existing `utils/public_url.public_base_url` helper. That helper reads the raw `X-Forwarded-Proto` header and only ever upgrades http to https. uvicorn proxy-header trust, `request.client.host`, `TRUSTED_PROXIES` and every compose `command:` are untouched.

## Checks run (diff mode; phases 0, 1, 12–14 + diff-relevant rows)

| Check | Result |
|---|---|
| Attack-surface delta | None. No new routes, inputs, dependencies, CI or Docker changes |
| Caller-controlled header | `X-Forwarded-Proto` is attacker-settable. Its only effect is the scheme of a URL returned to the same caller. The host comes from `Host`, which that caller already controlled before this diff |
| Persistence of the derived URL | None. The webhook URL is built per response. `resolve_mcp_url` is called in `routers/connector.py` `_status` and the key-regenerate route, both of which return the URL in the response and store nothing. A forged header cannot reach another user's view |
| Downgrade | Not possible. The helper never turns an https request into http (`test_webhook_url_never_downgrades_https`) |
| Client-IP trust (Finding 16, cso-2026-09-30) | Unchanged. No `--proxy-headers` / `--forwarded-allow-ips` added, so per-IP limiters and audit `actor_ip` keep their current trust |
| Frontend | `McpKeysTab.vue` takes the server `default_url` only when it starts with `https://`, and renders it as text in a JSON snippet. No `v-html` |
| Missing host | `_build_webhook_url` returns `None` instead of a relative URL |
| Secrets in diff | None |
| Enterprise-docs guard | The diff's docs add no paid-module tokens |
| A10 failure direction | Upgrade-only is stated in the helper docstring. The new code adds no guard |
| Tests | `tests/unit/test_3601_2907_proxy_scheme_urls.py` 14/14 green (red-first: 8 red before the fix) |

## Hypotheses

None (daily mode).

## Coverage gaps

Diff mode: full-repo phases (secrets archaeology over history, dependency scan, CI/CD, image internals) not run. Out of scope for a logic diff with no dependency, workflow or Docker changes.
