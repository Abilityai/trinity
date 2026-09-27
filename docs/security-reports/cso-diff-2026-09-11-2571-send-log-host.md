# CSO diff audit — abilityai/trinity#2571 (`feature/2571-send-log-host`)

**Date**: 2026-09-11 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `682fce30` on `dev` · **Diff**: 15 public files (+~430 / −45), no new test file (two extended), working tree staged.

## Verdict
**No findings at the gate.** Three candidates were traced and refuted or filed as ≤4/10 appendix items. The diff adds no endpoint, no outbound call, no log line, no prompt surface, no dependency, no workflow, no container or network change.

## Attack surface introduced by the diff
- **Endpoints**: none added or changed. `GET /api/settings/telemetry-sharing` keeps `assert_admin` + `reject_agent_principal` untouched; its document gains four fields (`last_shared_host`, `receiver_host`, `configured_host`, `receiver_mismatch`) and a scrubbed `share_url`.
- **Stored values**: each send-log entry gains `host` and the 2xx stamp gains `telemetry_sharing_last_shared_host` — both the ORIGIN of the configured URL (`scheme://host[:port]`, lower-cased) after `strip_url_credentials`, with a second `rsplit("@")` belt and the query/fragment dropped, so a credential cannot reach the store even if the strip is bypassed. The reducer is total (every URL shape, an unparseable value → `None`), proven through the real send path by a test.
- **Writers**: the service only. The generic `PUT /api/settings/{key}` still refuses the whole `telemetry_sharing_` prefix (`routers/settings.py`), the generic DELETE stays admin-gated and open for it by design.
- **Rendering**: Vue interpolation only (no `v-html`); the row text and both sentences are pure functions in `telemetryConsent.js`.
- **Logging**: no new log line; the existing "shared" INFO line stays status/class only. `httpx`/`httpcore` are pinned to WARNING at lifespan (`logging_config.py`), so the library never echoes the configured URL.

## Findings
| # | Sev | Conf | Status | Category | Finding | Phase | File:Line |
|---|-----|------|--------|----------|---------|-------|-----------|
| — | — | — | — | — | none at the gate | — | — |

## Verification performed
- **Secrets / enterprise disclosure (P2)**: word-anchored known-prefix scan, credential-assignment scan, email/IP/host-path scan over every added line — none outside synthetic test fixtures on `example` hosts. The enterprise-docs guard regex (the live CI pattern) applied via Python to every added public-doc line — no hits.
- **Access control (P9-A01)**: no route added; the status read keeps its human-only gate; the new key inherits the family's PUT refusal. `test_186_enumeration_uniformity`, `test_2094_dependency_path_param_pairing`, `test_293_admin_gate_rejects_agent_keys` green.
- **Injection (P9-A03)**: no SQL, subprocess or template path added; `db.set_setting` is the existing parameterised sink; `test_ent435_settings_sink_guard` green (the key is not credential-shaped and is written by its home module).
- **Credential safety (P2/P9-A02)**: userinfo removed before storage and before the status; query dropped from the display URL; the stored origin is compared and displayed only.
- **SSRF (P9-A10)**: the reducer makes no request; the POST target is unchanged operator config.
- **LLM (P7)**: no prompt surface; the panel renders through interpolation.
- **Standing guards on this tree**: the five guard files above — 67 passed.
- **Supply chain (P3), CI/CD (P4), infrastructure (P5), skills (P8)**: nothing of that class in the diff.

## Appendix (≤4/10, non-blocking)
1. **`share_url` keeps its path** (conf 4) — a token embedded as a path segment of an operator-configured URL would still ride the admin-only status; pre-existing, narrowed by this diff, no longer rendered.
2. **The recorded origin can name an internal host** (conf 3) — shown to admins by design; carries no credential.
3. **The generic DELETE covers the new key** (conf 2) — the family's documented reset path; deleting it alone degrades honestly to "an unknown receiver".

*AI-assisted scan, not a professional audit.*
