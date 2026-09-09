# CSO diff audit — abilityai/trinity-enterprise#545 (`feature/545-funnel-non-minting-read`)

**Date**: 2026-09-09 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `3fa11329` on `dev`; enterprise submodule branch of the same name off private `main` `fd25b47` · **Diff**: public 7 tracked files +170 / −14 plus 2 new files (a pure decision module and its spec); enterprise 6 tracked files +36 / −13 plus 1 new test file (working trees, pre-commit). Surface: one read accessor added beside its writer, the writer's mint semantic, one response field made nullable, one footer.

## Verdict
**No findings at the gate.** The diff removes a durable write from a GET and ships more hardening than it touches: the install id is minted through a PRIMARY-KEY-enforced claim instead of a last-write-wins upsert, the enterprise suite proves the read path at the write sink and statically, and the footer never claims "not minted" for a value that is merely absent.

## Attack surface introduced by the diff
- **Endpoints**: none added. `GET /api/enterprise/telemetry/funnel` keeps both gates (`requires_entitlement("telemetry")` on the router, `require_admin` on the route — agent and connector principals rejected, #1890 / #2323) and its `installation_id` becomes `Optional[str]`: `null` until a writer has minted it. The durable write the GET used to perform is gone.
- **Writers**: `operator_intake_service.get_or_create_installation_id` claims the id write-once (`insert_setting_if_absent`) after a SELECT-miss and reads the winner's value back; its three callers (the consent POST, the product-event emit, the canary label) are unchanged.
- **Readers**: `get_installation_id` — one settings read, no write on a miss, a read failure propagates rather than masquerading as "not minted".
- **Frontend**: the footer renders `installIdFooter(installation_id)` through Vue interpolation (no `v-html`): `Install <id>` / "No install id yet — one is minted when this instance opts in to security & product updates (Settings → General)" / "Install id unavailable".
- **WebSocket channels, MCP tools, Docker permissions, network edges, dependencies, workflows, background loops**: none added or changed.
- **Trust-boundary effect**: none. The id stays local (it leaves the box only inside the operator-consented intake POST, unchanged), and its creation surface shrinks by one path.

## Findings
| # | Sev | Conf | Status | Category | Finding | Phase | File:Line |
|---|-----|------|--------|----------|---------|-------|-----------|
| — | — | — | — | — | none at the gate | — | — |

## Verification performed
- **Secrets (P2, diff)**: no commits on either branch yet, so both working diffs plus the three new files were scanned instead of `git log -p`. Known-prefix scan over every added line (AWS `AKIA`, OpenAI/Anthropic `sk-`, GitHub `ghp_`/`gho_`/`github_pat_`, Slack `xox*`, `password=`/`secret=`/`token=` literals): none. The enterprise test's `REDIS_URL`/`SECRET_KEY` are the import-time placeholders the enterprise CI workflow itself uses. No tracked `.env`. **Enterprise-disclosure guard (trinity-enterprise#45)**: the guard's own pattern matches none of the 24 added public-doc lines; neither seam file (`main.py`, `entitlement_service.py`) is touched; the funnel view has been a public, generic-seam fact in requirements §45 FR-4 since ent#184.
- **Supply chain (P3)**: no dependency, lockfile, Dockerfile or action change.
- **CI/CD (P4)**: no workflow files in the diff.
- **Infrastructure (P5)**: no compose, network, Redis or Docker change; the accessor's new primitive is the existing `insert_setting_if_absent` (#2380) on the OSS settings table.
- **Integrations (P6)**: no outbound call added or changed; the funnel is local-only; the benchmark read (ent#190) is untouched except that its fixture's sentinel now also covers the write sink.
- **LLM / AI (P7)**: no prompt, tool description, `v-html` or `innerHTML` in the diff; the enterprise telemetry router has no MCP tool.
- **Skill supply chain (P8)**: no skill files changed (the trinity-dev commit on this branch's sprint is a debt-inbox markdown entry, not a skill).
- **OWASP (P9)**: A01 — no new route; the funnel route's gates are unchanged and its agent-principal rejection is now pinned by a test; no agent-scoped path, so no enumeration split. A03 — no raw SQL (`sqlalchemy.insert`/`select` with bound values in the test; the facade elsewhere), no `subprocess`, `eval` or `v-html`. A09 — no new log line. A10 — no URL construction.
- **STRIDE (P10, the funnel read + the accessor)**: Spoofing — none (admin-only read; the write sink is the OSS facade). Tampering — the stored id is admin-writable through the pre-existing generic PUT/DELETE only; the claim cannot overwrite. Repudiation — unchanged (the intake POST is audited at consent; the GET writes nothing to repudiate). Information disclosure — an admin now learns "no id minted" for their own install; nothing new leaves the box or reaches a log. DoS — none (one SELECT per GET, strictly fewer writes). Elevation — none.
- **Data classification (P11)**: `installation_id` stays INTERNAL on the box (anonymous per-install id) and CONFIDENTIAL-linked only inside the consented intake POST, where it rides beside the operator's email; the OSS sharing validator bans it from the anonymized aggregate (unchanged). The change reduces where it is created, not where it goes.

## Filter statistics
6 candidates · 0 reported · 0 refuted by a verifier (none needed) · 5 excluded by rules · 1 checked clean:
1. **Rule 5 (framework-aware, no exploit)** — an admin-written string in the setting renders in the footer through interpolation only, on an admin-only surface.
2. **Checked, clean** — `insert_setting_if_absent` skips `set_setting`'s sink guards by design (it cannot overwrite); the key is the fixed `installation_id` literal.
3. **Rule 5 / out of diff scope** — the generic admin `DELETE /api/settings/installation_id` re-keys future product events; pre-existing and unchanged.
4. **Rule 6 (not concretely exploitable)** — the read-back after the claim can be empty only if an admin DELETE lands inside the claim window; every caller tolerates it.
5. **Rule 8 / placeholder FP** — the test's `redis://u:p@localhost` and `ci-not-a-real-secret`.
6. **Checked, clean (ent#45 guard)** — zero pattern hits over the added public-doc lines; no seam file touched.

## Trend
Branch-scoped diff report; the four prior diff reports (#2618, #2578, ent#190 — the last over this same module's benchmark read, which deferred exactly this item — and ent#541) all closed with no findings. No fingerprint overlap.

## Remediation roadmap
None required.
