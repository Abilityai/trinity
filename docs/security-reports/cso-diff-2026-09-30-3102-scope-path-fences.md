# CSO diff audit — #3102 scope fences read the routed path

- **Mode**: `--diff`, daily (8/10 gate). Scope: branch `feature/3102-scope-path-fences` vs `dev`.
- **Phases run**: 0, 1, 2 (diff), 9 (A01, A05), 12–14. Phases 3–8 have no changed surface in this diff (no dependency, CI, Docker, webhook, LLM or skill change).

## Result
No findings at or above the gate. 0 critical, 0 high, 0 medium.

## Verified (fresh-context adversarial verifier, every item HOLDS)
- The five fences in `dependencies.get_current_user` compare `request.scope["path"]`, which is the path the router matched when `root_path` is empty. Nothing sets `root_path` or mounts a sub-app. Adding one makes the fences refuse, never admit.
- `%2F` and other percent-encodings decode identically for the router and the fences. `raw_path` is never read.
- `HostHeaderGuard` and Starlette both take the first `host` header, so duplicate Host headers cannot split them.
- Absolute-form targets, a missing Host, non-latin-1 bytes, IPv6 literals and ports pass the guard with `url.path == scope["path"]`.
- The guard is the last `add_middleware` call in `main.py`, so it is outermost. WebSocket handshakes with a crafted Host close with 1008 (uvicorn answers 403). Lifespan passes through.
- Diff secrets scan: clean.

## Out of scope (separate follow-ups)
- Absolute URLs echoed from the raw Host (`routers/a2a.py:100`, `routers/paid.py:167`, `routers/schedules.py`, `routers/settings/mcp_url.py:120`): a plain-letter Host is still reflected. Host-injection class.
- `--forwarded-allow-ips=*` in `docker/backend/Dockerfile` trusts `X-Forwarded-*` from any peer, including agents on `backend:8000`.
- Private submodule: two audit-label `request.url.path` reads, no fences. The guard covers them at runtime.
