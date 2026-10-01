# CSO Diff Audit — 2026-09-23 — abilityai/trinity#2915 (the operator-queue file sync tells the truth)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/2915-queue-sync-honest` → `dev` · **Skill**: cso v1.1 · **Audited**: the branch diff at f59c5d32d plus the one uncommitted remediation this audit produced.

## Architecture (Phase 0)
The change keeps the operator-queue file contract (`~/.trinity/operator-queue.json` ⇄ `operator_queue`) and makes the poller honest about it: every file entry is reconciled against its row through a pure content fingerprint, the outcome lands on the row as a closed-vocabulary sync/delivery state, the write-back delivers only into a matching still-pending entry after a pre-write re-read with a compare-and-swap, the respond routes refuse a diverged item until the human acknowledges it, and the accountability transitions are audited. Trust boundary unchanged: the agent-authored file enters at `_sync_agent`; everything it can influence downstream is a field name, a folded token, a hash, or the presence of one key.

## Attack surface (Phase 1, diff-scoped)
- New endpoints: 0 · New tools: 0 (one optional param on `respond_to_operator_queue`) · WebSocket: +1 fleet-level thin trigger (empty payload, allow-listed with reason) · Docker/compose: unchanged · Dependencies: unchanged · CI: unchanged · Migrations: 1 (7 nullable TEXT columns, both tracks, Alembic re-chained to `0072` on `0071_seat_decisions`, single head verified)
- New agent-controlled inputs: the entry `status` (folded), the four content fields (compared, never re-stored), the `platform` block (presence of `aging_since` only), `if_match` on the agent server (hash compare, 412), and the entry `type` (pre-existing unbounded column — its NEW audit copy is the one finding, fixed)
- Changed response surface: list/get carry the seven columns + `aging`/`aged_since`; the list carries two access-scoped counts; 409 `item_diverged` on both respond entry points; the portal projection stays a named allowlist (coarse `sync` + `aging`, never the reason or a poller timestamp); one admin-gated, validated ops setting

## Findings (Phases 2–12)
**None open.** One defect found by this audit and fixed in the same branch, independently verified:

1. **LOW — the new `ingested` audit row copied the entry's unbounded `type` into `audit_log.details`.** `_clamp_ingested_item` bounds title/question/context/options/priority/created_at and never `type`; `create_item` stores it with no belt; `platform_audit_service.log` has no cap; the table is append-only, hash-chained and un-prunable for a year. Exploit: an id-shaped entry with a ~2 MB `type` → ingested in 5 s → chain-committed, undeletable. Impact is log-content injection + storage amplification (no auth or control-flow effect; the audit view renders details as text; no in-repo LLM reads audit rows), hence LOW. Fixed: `details["type"] = _fold_agent_status(…)` (`^[a-z_]{1,32}$` or `other`), pinned by `test_the_ingested_audit_row_carries_no_agent_text`. Residual (pre-existing, registered): the `operator_queue.type` column itself stays unbounded at ingest and sink.

Three pre-existing integrity defects were closed by the branch itself: an unparseable queue file was read as empty and then overwritten with the reconstructed responses alone (every pending entry lost on a torn write); expiry never ran while no agent was running; a pending entry re-using a terminal row's id was re-admitted every cycle (a phantom admit against the depth cap).

Key clearances: `sync_detail`/`delivery_detail` are closed vocabularies enforced by regex (`_detail`, `_fold_agent_status`, `_read_failure_detail`) and never `str(e)` or `response.text`; audit `details` carry ids and enums only; the 409 discloses nothing the same principal cannot already GET; the aging bound is unreachable through the generic settings PUT (422) and admin-gated on ops/config; `count_flags` is scoped by the caller's accessible set; the `operator_queue_sync` broadcast has no payload (ent#467 guard green); no `v-html` added; backend→agent calls still ride `AgentClient` (auth-header guard green); vendored-copy parity, enumeration uniformity, admin-gate and settings-sink guards all green (218 tests).

## Mutation battery (fix-reverting)
7 mutations from scratch copies, 7 red, all restored byte-identically: divergence detection disabled (3 red), Docker-unreadable as "no agents" (1), invalid JSON read as empty and written back (1, after the test was strengthened to pin the first guard), fingerprint gate removed (1), hysteresis removed (1), `if_match` never refuses (2), the 409 block removed (1).

## Accepted and stated (not findings)
- **Upgrade-time one-off**: pre-existing cancelled/expired rows whose entries are gone each record `undelivered:entry_missing` once (≤200 per agent per cycle) — the header count is honest about history rather than silent.
- **`closed_by_filer` rows still count toward the #1632 depth cap** until an operator cancels them — registered; trinity-enterprise#619 owns withdrawal semantics.
- **An agent may pre-fill `platform.aging_since`** in its own file — that only suppresses the receipt write; aging is computed from the row's `created_at`.

## STRIDE (operator-queue sync, diff-scoped)
- **Spoofing**: the row's sync/delivery state is written only by the leader-locked poller; nothing an agent writes is copied into it. **Tampering**: an agent rewrite is detected (`changed`) and never applied; the write-back refuses to deliver into it; the agent server's write is atomic + compare-and-swap. **Repudiation**: `ingested · diverged · reconciled · written_back · undeliverable` audit rows, transition-only. **Information disclosure**: the portal sees a coarse enum + a bool; reasons and poller timestamps stay operator-side; the WS trigger carries nothing. **DoS**: bounded per-agent index read, one re-read per rare write, hysteresis and transition-only audit (a fleet restart writes zero rows). **Elevation**: none — no new endpoint, no new principal path; the ops setting is admin-only.

## Data classification (diff-scoped)
- `sync_state` / `sync_detail` / `delivery_state` / `delivery_detail` — INTERNAL — closed enums; rendered to operators and (coarsely) to the addressed client
- `sync_updated_at` / `last_confirmed_at` / `delivery_updated_at` — INTERNAL — poller timestamps; withheld from the portal projection
- audit `details` — INTERNAL — ids and enums only (F1 closed the one exception)
- `platform.aging_since` in the agent's file — INTERNAL-to-the-agent — a timestamp, never authorization

## Trend
Prior: `cso-diff-2026-09-21-ent549-file-audience` (0 findings). This diff: **0 open**, 1 found-and-fixed during the audit. Direction: stable.
