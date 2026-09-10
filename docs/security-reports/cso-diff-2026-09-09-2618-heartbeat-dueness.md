# CSO diff audit — abilityai/trinity#2618 (`feature/2618-heartbeat-last-shared`)

**Date**: 2026-09-09 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `bce0bbe1` on `dev` · **Diff**: 13 tracked files +258 / −55 plus 1 new test file (working tree, pre-commit). Surface: one background loop, one lifespan hook, two sentences of consent copy, tests and docs.

## Verdict
**No findings at the gate.** The diff changes *when* an already-consented, already-gated aggregate is sent — dueness now comes from the persisted last-share stamp instead of process age — and ships more hardening than it touches: the acknowledgement/stamp-write split that would otherwise have produced duplicate sends under the new release path, a per-claim lock, a persisted retry cap, and a shutdown hook that never orphans the cross-worker marker.

## Attack surface introduced by the diff
- **Endpoints, WebSocket channels, MCP tools, Docker permissions, network edges, dependencies, workflows**: none added or changed.
- **Background loop** (`services/telemetry_sharing_service.py`): the heartbeat wakes every 10 minutes (+ ≤10 min jitter, sleep-first) and sends when `telemetry_sharing_last_shared_at` is empty, unparseable, in the future, or older than the interval. Both egress gates (`TELEMETRY_SHARING_ENABLED` / `DO_NOT_TRACK`, stored consent) run before any stamp read or Redis touch; a settings read failure reads as consent-off. The Redis tick marker (`telemetry_share:tick`, `SET NX EX` half the interval under the `backend` ACL user — no `@dangerous` command) is a fresh `SingleFlightLock` per claim, released only when the receiver did not acknowledge; `share_now` reports an acknowledged 2xx as True even if the local stamp write raises; after five consecutive failures attempts fall to one per half-interval, measured from the persisted send log.
- **Lifespan** (`main.py`): `telemetry_sharing_service.stop()` in the shutdown block, mirroring every other loop; cancellation mid-send takes the tick's release path.
- **Frontend**: two `receiverCopy` sentences ("retried daily" → "retried automatically"), rendered through Vue interpolation, no `v-html`.
- **Trust-boundary effect**: none. The recipient (`TELEMETRY_SHARING_URL`), the document (the schema-v2 anonymized aggregate keyed on `sharing_id`, never `installation_id`) and the two consent gates are unchanged.

## Findings
| # | Sev | Conf | Status | Category | Finding | Phase | File:Line |
|---|-----|------|--------|----------|---------|-------|-----------|
| — | — | — | — | — | none at the gate | — | — |

## Verification performed
- **Secrets (P2, diff)**: no commits on the branch yet, so the working diff plus the new test file were scanned instead of `git log -p`. Known-prefix scan over every added line (AWS `AKIA`, OpenAI/Anthropic `sk-`, GitHub `ghp_`/`gho_`/`github_pat_`, Slack `xox*`, PEM headers, `password=`/`secret=` literals): none. No emails, private IPs or absolute host paths in added lines; no tracked `.env`; the enterprise-docs guard's seam file `main.py` gains a shutdown hook only, with no paid-module tokens.
- **Supply chain (P3)**: no dependency, lockfile, Dockerfile or action change. A `pnpm-lock.yaml` created by the host vitest run was deleted and never staged.
- **CI/CD (P4)**: no workflow files in the diff.
- **Infrastructure (P5)**: the marker's commands (`SET NX EX`, `GET`, `DEL`, `EXPIRE`) sit inside the `backend` ACL user's allowed set (`-@dangerous` only); no compose or network change; agents still cannot reach Redis (#589), so the marker has no attacker-controlled writer.
- **Integrations (P6)**: the outbound POST (httpx, 10s timeout, no credential, `TELEMETRY_SHARING_URL` from config) is byte-for-byte unchanged. The one new WARNING ("delivered, but the last-shared stamp could not be persisted") carries the exception class only; the loop guard's `logger.exception` sits over callees that all swallow their own errors and none of which holds the URL or a credential in scope.
- **LLM / AI (P7)**: the payload builder, validator and preview are untouched; no prompt, MCP description or HTML sink in the diff.
- **OWASP (P9)**: A01 — no routes; A03 — no SQL, `subprocess`, `eval`, `v-html` or `innerHTML` in added lines (the test's stubs are in-process); A09 — new log lines carry `every %sh, checked every %sm`, a failure count constant, or an exception class; A10 — the URL is config-only and unchanged.
- **STRIDE (P10, the heartbeat component)**: Spoofing — none (no inbound path); Tampering — the stamp and send log are self-written settings, parsed defensively (unparseable ⇒ due, never a raise); Repudiation — every attempt is in `recent_sends` with status or exception class; Information disclosure — unchanged payload, no new log content; DoS — attempt frequency is bounded by the persisted cap (see exclusions); Elevation — none.
- **Data classification (P11)**: unchanged — the aggregate is anonymized counts keyed on the share id (CONFIDENTIAL in transit only by TLS to the configured receiver); the local send log keeps the last five payloads behind the admin-only status route.

## Filter statistics
4 candidates · 0 reported · 0 refuted by a verifier (none needed) · 4 excluded by rules:
1. **Rule 1 (DoS / rate limiting)** — attempt frequency during a receiver outage or on an air-gapped consented install rises from one per day to at most one per wake for the first five failures, then one per half-interval; the diff adds the cap itself, and content, gates and recipient are unchanged.
2. **Rule 5 (hardening without a concrete vulnerability)** — the documented reset `DELETE /api/settings/telemetry_sharing_last_shared_at` (admin-gated, human-only) now yields one heartbeat within 10–20 minutes; consent still gates; documented in the feature flow and FR-7.
3. **Rule 6 (race not concretely exploitable)** — `release_if_owned` is GET-then-DELETE on the caller's own token (the pre-existing primitive); the only writers are trusted backend workers.
4. **Checked, clean** — the `_loop` guard's traceback path holds no credential or URL.

## Trend
Branch-scoped diff report; the three prior diff reports (#2578, ent#541, ent#190 — the last over this same module's benchmark read) all closed with no findings. No fingerprint overlap.

## Remediation roadmap
None required.
