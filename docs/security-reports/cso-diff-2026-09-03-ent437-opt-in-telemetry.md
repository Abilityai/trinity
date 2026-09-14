# CSO diff audit — abilityai/trinity-enterprise#437 PR1 (`feature/437-opt-in-telemetry`)

**Date**: 2026-09-03 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `00b6f025` on `dev` · **Diff**: 23 files, +2362/−303

## Verdict
**No findings at the daily gate.** Three INFO observations recorded below; one fixture placeholder fixed during the audit.

## Attack surface introduced by the diff
- **New route** `POST /api/settings/telemetry-sharing/ask/dismiss` — `assert_admin` + `reject_agent_principal`, audit `telemetry_sharing_ask_dismissed`, idempotent, no body.
- **Changed** `GET /api/settings/telemetry-sharing` — `?preview=` query bool; response carries the share id, the dismissal/first-value/backfill markers and the last 5 send attempts; gains `reject_agent_principal`; the aggregate builds off the event loop.
- **Changed** `GET /api/settings/feature-flags` — three new booleans for any authenticated user.
- **Five settings keys**, all under the `telemetry_sharing_` prefix the generic `PUT` refuses; the generic `DELETE` stays admin-gated and open as the reset path.
- **Outbound**: same operator-configured intake URL; payload schema v2, validated fail-closed before send; no credential; 10s timeout; TLS verify default.
- **Redis**: `telemetry_share:tick` (`SET NX EX` via `SingleFlightLock`, backend ACL user, TTL-held, fail-open).
- No Docker, compose, CI, dependency, WebSocket or skill changes.

## Verification performed
- Secrets: known-prefix scan over the working diff — one match, a test placeholder (`sk-…`) in `test_ent437_telemetry_consent.py`, replaced with a neutral marker so `secret-scan.yml` cannot trip on it. No `.env`, workflow or config credential touched.
- Enterprise-docs guard: the workflow's PATTERN run over the five changed public docs — 0 hits.
- Auth: both telemetry routes human-only; flags booleans only; enumeration uniformity untouched (no agent-scoped path).
- SQL: bound parameters only in the two new readers.
- Identity: `installation_id` banned by the validator and never read by the builder; `sharing_id` logged as an 8-char prefix; release version, not commit SHA, on the wire.
- Rendering: no `v-html`; payloads and the share URL render as text.
- Suites: 34 + 10 backend, 27 frontend, neighbouring flag suites, all green.

## Observations (below the gate)
| # | Sev | Conf | Observation |
|---|-----|------|-------------|
| O1 | INFO | 4/10 | `telemetry_sharing_hard_disabled` tells any authenticated user whether the telemetry kill switch is set. A config boolean, no PII; it is what lets the card stay hidden with no admin round-trip. |
| O2 | INFO | 3/10 | `telemetry_sharing_first_value` tells any authenticated user the install has completed an autonomous run. Nothing exploitable beyond what execution reads already show. |
| O3 | INFO | — | Fixture used an `sk-` shaped placeholder; replaced. Public-repo rule: zero secret-shaped literals, even fake ones. |

## STRIDE (diff scope)
- **Backend API**: spoofing — admin JWT + human-only gates; tampering — write-once claims for identity/marker rows, prefix-guarded PUT; repudiation — both mutations audited (bool + timestamp only); disclosure — see O1/O2; DoS — excluded; elevation — none (no new privilege tier).
- **Frontend**: the card renders only for a verified admin; per-browser snooze in localStorage is a nudge, not a gate.
- **Outbound**: fail-closed schema validation bounds what can leave; the receiver is operator config, never user input.

## Data classification (diff)
- `sharing_id` — INTERNAL, anonymous key; never beside `installation_id`.
- `telemetry_sharing_recent_sends` — INTERNAL copies of server-built payloads (counts/enums only).
- `telemetry_sharing_dismissed_at` / `_first_value_at` / `_backfill_delivered_at` — INTERNAL timestamps.

_Trend_: first diff audit for this branch; not comparable to the prior diff report (#2505)._
