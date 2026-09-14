# CSO diff audit — abilityai/trinity-enterprise#190 (`feature/190-benchmark-wiring`, private submodule + public branch)

cso v1.1 · mode `--diff` · 2026-09-07 · working tree pre-commit. Scope: the private submodule branch against enterprise `origin/main` (`8627b38`, 8 files) and the public branch against `dev` (`a9ec8728`, 14 files). The public repo is world-readable; nothing below quotes a credential, a share id, or a host path.

## Verdict

**No finding at the 8/10 gate.** The diff adds one outbound request class to the platform — the entitlement-gated benchmark view asking the hosted benchmark service what it knows about this instance — and every property that makes that safe is in the code and pinned by a test: the request leaves only with the kill switch off and consent on, is keyed on the anonymous share id and never the install id, carries no credential, goes to a URL derived from config and never from a caller, is bounded on time and on wire bytes, refuses redirects and compression, and classifies the answer on its body before anything reaches the browser. Two exposures were found by the plan review and closed in this diff before the audit ran (below).

## Attack surface introduced by the diff

- **Changed route** (gate unchanged): `GET /api/enterprise/telemetry/benchmark` — `requires_entitlement("telemetry")` + `require_admin` (rejects agent and connector principals, allow-lists `mcp_scope`). A read, not a grant. Route-level tests prove an agent principal, a non-admin and an unentitled install all get 403 through the real dependency, and that a transport failure still answers 200 with a named status.
- **New outbound integration**: `GET <configured receiver origin>/v1/telemetry-benchmark?sharing_id=…`. Credential-free by contract. Scheme restricted to http/https; the configured share URL's path must end in the documented tail or the read is refused as `url_underivable` (no guessed path on an operator's custom sink). TLS verification default; `follow_redirects=False` (a followed 3xx would replay the id to the redirect's host); per-phase timeout 5 s plus a 6 s wall-clock deadline; `Accept-Encoding: identity` with any `Content-Encoding` refused; 64 KiB ceiling on wire bytes; `BoundedSemaphore(2)`; a per-worker five-minute memo of well-formed answers only.
- **New renderer input**: `FleetBenchmarkCard.vue` renders numbers projected through an allow-list, one validated single-line timestamp in a `title` attribute, and backend-authored `message`/`reason` strings. No `v-html`; nothing from the wire reaches the DOM as free text.
- No new routes, MCP tools, CSP, CI, dependency, Docker or compose changes.

## Findings

None.

## Closed before the audit (found by the plan review, fixed in the diff)

1. **httpx's own INFO line carries the credential-bearing URL.** httpx logs `HTTP Request: GET <url> "…"` at INFO on the `httpx` logger; on this route the URL carries the share id, which is the receiver's credential. The plan review believed the backend left that logger unconfigured; the PR review (#546, I1) showed `logging_config.py::setup_logging()` already pins it to WARNING unconditionally at lifespan, so the line was silent in production all along and the audit's original premise was wrong. Kept as a belt: an import-time pin at the telemetry module with a comment naming the OSS line as the primary, and a test at root INFO asserting the id is absent from every captured record. Learnings entry 2026-09-07 records the scoped-grep mistake.
2. **A body ceiling on decoded bytes** (the 2026-08-05 ledger class): replaced by a wire-byte cap on `iter_raw()` with compression refused and a `Content-Length` early exit, mirroring the OSS template-registry fetch.

## Verification performed

- Enterprise suite: 383 passed / 5 skipped on seeds 106, 12345 and 99999, 66 of them new for this read (gates, non-minting identity, URL derivation, request shape, every receiver status, wrong-typed and absent fields, 3xx/4xx/5xx, compressed and oversize bodies, transport exception classes that embed the URL, the deadline, `busy`, memo semantics, log hygiene, the model's field set, the sync route, and the router's double gate).
- Standing security guards green (66 tests): vendored-copy parity (Invariant #5), #186 enumeration uniformity, #2094 dependency/path-param pairing, #293 admin gate rejects agent keys, ent#435 settings sink guard.
- Enterprise-docs guard pattern run locally over every changed public file: 0 hits, identical to `dev`.
- Diff scan over both working trees: no secret prefixes, private keys, absolute host paths, non-loopback IPs or real emails (only `example.com` test principals). No `verify=False`, `subprocess`, `os.system`, `v-html`, `innerHTML` or `eval` in the changed code.
- Live: eight sink fixtures and the production receiver answered exactly as classified; the sink log shows one query parameter per request; the backend was restored to the production URL afterwards.
- Independent verifier subagents: none spawned, because no finding survived the gate.

## Observations (below the gate)

- **O1 (3/10)** Public requirements §45.1 FR-6 states the gated view's behavioural contract (five statuses; non-minting, fail-open, bounded, briefly memoised). Reviewed against the enterprise-docs standing rule at the plan gate and operator-approved: contract level only; the internals live in the private flow doc; the guard pattern is silent. Not a disclosure finding.
- **O2 (2/10)** The httpx logger pin is process-wide and, as the PR review noted, redundant with the OSS pin at lifespan. Kept deliberately as a belt with both owners named; strictly quieter; tested. Not a finding.
- **O3 (4/10)** Threadpool exposure under a stalled receiver. Hard exclusion #1 (DoS), and bounded regardless. Noted, not reported.

## Trend

First report for this branch. Prior diff report: `cso-diff-2026-09-07-ent541-retention-parity-guard` (different surface; no shared fingerprints). New: 0 · Persistent: 0 · Resolved: 0 · Closed in the diff before the audit: 2.
