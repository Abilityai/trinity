# CSO Security Audit

**Mode**: diff (daily gate, 8/10 confidence) · **Skill**: cso v1.1
**Scope**: `feature/1880-canary-alert-names` vs `origin/dev` @ `39f29c64` (#1880 — canary alert-surface parity)
**Date**: 2026-07-30
**Diff size**: 7 files, +997 / −18 — 1 source (`services/canary_alerts.py`), 2 new test files, 4 docs

## Summary

| Category | CRITICAL | HIGH | MEDIUM | LOW |
|----------|----------|------|--------|-----|
| Secrets | 0 | 0 | 0 | 0 |
| Dependencies | 0 | 0 | 0 | 0 |
| Auth Boundaries | 0 | 0 | 0 | 0 |
| Injection | 0 | 0 | 0 | 0 |
| Information Disclosure | 0 | 0 | **1** | 0 |
| Platform Patterns | 0 | 0 | 0 | 0 |
| Configuration | 0 | 0 | 0 | 0 |

## Nature of the change

Adds per-invariant Slack alert metadata (name, runbook, summary branch, forensic branch) for five
canary invariants — E-03, E-04, E-06, G-03, G-04 — that previously fell through to a bare-id
fallback, plus a CI-enforced parity guard. **No new endpoint, auth dependency, WebSocket channel,
subprocess, file IO, or outbound network call.** Exactly one trust boundary moves data: the
outbound Slack webhook payload.

## Attack surface delta

| Surface | Delta |
|---------|-------|
| Public / authenticated / admin endpoints | none |
| WebSocket channels | none |
| File upload / write paths | none |
| Subprocess / exec | none |
| External integrations | **existing Slack egress — content widened** |
| Background jobs | none (canary watcher unchanged) |
| Dependencies | none — new imports are stdlib (`ast`, `json`, `pathlib`, `typing`) + `pytest` |

---

## F1 — MEDIUM · VERIFIED · 9/10 — Canary alerts now disclose credential-incident location to a non-admin audience

**Phase**: 11 (Data Classification) / 7 (LLM & AI) · **Category**: Information Disclosure
**File**: `src/backend/services/canary_alerts.py` (G-04 branches in `_render_message` / `_render_forensic`)

### Description

Before this change all five invariants rendered a count-only fallback:

```
🚨 canary G-04 G-04 (critical): G-04 fired 1 violation(s).
```

After, a G-04 alert carries which agent and which execution row hold a credential-shaped value,
which credential family it matched, and — via the runbook — that the value is stored plaintext and
survives until the row is deleted.

The secret **value** is never transmitted; that property is upheld (see Verified-Safe below) and
now regression-pinned. The disclosure is the *pointer*, not the secret.

### Exploit scenario

1. An observer has access to the Slack channel behind `CANARY_SLACK_WEBHOOK_URL`. Channel
   membership is governed by Slack, and has **no relationship to Trinity's admin role** — the
   webhook URL is a single env var an operator sets.
2. G-04 fires. The observer learns: agent `X`, execution `Y`, pattern family `github_pat`, and that
   the value is plaintext and still present.
3. The content also lands in the Slack `text` fallback, which populates mobile push notifications
   and any Slack-connected integration — not only the rendered blocks.
4. The observer cannot read the value from Slack, but now knows precisely where a live credential
   sits and roughly how long it will persist. Before #1880 the same reconnaissance required an
   admin query against `canary_violations`.

### Impact

Risk transfer, not a new leak: from *"admins can query where a credential leaked"* to *"the canary
Slack channel is told where a credential leaked."* Bounded — no secret value, no user content, no
PII. The four other invariants disclose only agent names, execution/schedule ids, statuses, and
timestamps (INTERNAL classification).

Leading the runbook with **rotate first** is the correct containment call and is pinned by
`tests/unit/test_1880_canary_alert_parity.py::test_g04_runbook_never_sends_anyone_to_the_raw_blob`.

### Recommendation — APPLIED IN-DIFF (documentation control)

`docs/memory/architecture.md` (canary Alert sink) now states the operational requirement: point
`CANARY_SLACK_WEBHOOK_URL` at a **restricted channel**, and states exactly what a rendered alert
carries. No code change — the disclosure is intended behaviour and is the feature's whole point;
what was missing was the deployment constraint being written down.

---

## Verified-safe (claims tested adversarially, not assumed)

Both were handed to an independent fresh-context verifier instructed to **refute** them, which
exercised the real code with planted sentinels rather than reasoning about it.

### No credential material can reach the Slack payload — UPHELD 9/10

- All 15 `ViolationReport(...)` constructions read. No rendered field is `backlog_metadata` or any
  secret-bearing column. `reason` (E-04) is one of three module constants; `matched_pattern`
  (G-04) one of ten hardcoded literals, with a `break` at first match so even the match count is
  withheld.
- Stronger than claimed: **no `signal_query` carries a secret either**, so even a future accidental
  render of that field would not leak the blob.
- `_build_slack_payload` consumes only `severity`, `invariant_id`, the two renderers' output, the
  static runbook, `snapshot_time`, and `persisted_ids`. `emit_transition` never echoes the webhook
  URL.
- L-03's `sample_refs.row_id` is not a vector — composite-PK tables get a synthetic
  `f"{table}-row"` literal, so user-controlled values (e.g. `agent_tags.tag`) cannot surface.
- Empirically: sentinels planted in `observed_state["backlog_metadata"]` for both E-04 and G-04
  reached neither `text` nor any block. Regression-pinned by
  `test_scrubbed_invariants_render_only_whitelisted_keys` and
  `test_unrendered_invariant_never_dumps_its_observed_state`.

### Slack mrkdwn / markup injection is impossible — UPHELD 8/10

- JSON break-out is structurally impossible: `slack_service` builds a dict and lets httpx
  serialize. A name containing a forged block fragment produced an escaped string, not a new block.
- Eight agent-name write paths traced, all sanitized: main create (sanitize precedes
  `register_agent_owner`), local deploy (`skip_name_sanitization=True` but sanitized earlier),
  system-manifest deploy (manifest validation enforces `^[a-z0-9][a-z0-9-]*[a-z0-9]$` first),
  rename (**stricter** — drops `.`), ephemeral ghost (server hex suffix applied after sanitize),
  fork-to-own (never touches the name), system agent (constant), MCP `create_agent` (routes through
  the sanitizing endpoint).
- Every other rendered field is server-generated: ids via `secrets.token_urlsafe(16)`, timestamps
  via `utc_now_iso()`/`.isoformat()`, `status` constrained by the collector's `WHERE` clause,
  numerics via `int()`/`round()`.
- `_` is permitted by the sanitizer but is cosmetic only: no link (`<>|` stripped), no mention
  (`@<` stripped), no code fence (backtick stripped), and the header block is `plain_text`, where
  mrkdwn is not parsed at all.

**Confidence is 8 not 9 because the guarantee is transitive across ~8 write paths, with zero
escaping at the render boundary.** See Note N1.

---

## Notes below the finding bar

**N1 — render boundary performed no escaping (defense-in-depth). FIXED IN-DIFF.** The injection
claim rested entirely on sanitize-at-write. Verified empirically at audit time: given a
hypothetical unsanitized name, `<https://…|CLICK>` rendered as a live link and `<!channel>` as a
channel-wide mention inside the section block. There is precedent for platform code writing an
`agent_name` the sanitizer could never produce — `services/retention_guard.py` deliberately uses a
leading `_` for its alarm sentinel *because* sanitization strips it. That row is excluded from
L-03's orphan scan, so it could not reach an alert, and it is a developer constant, not attacker
input. It therefore fell under hard exclusion #5 (hardening without a concrete vulnerability) and
was **not** a finding — but it was raised because a scrub at the render boundary converts a
transitive guarantee into a structural one, and this diff grew the render sites from 10 to 15.

**Resolution.** `_mrkdwn_safe()` now sits at the render boundary and is applied to **every** string
field interpolated from `observed_state` — 56 call sites across both renderers, replacing the two
inconsistent `?`-defaulting idioms (`get(k, "?")` and `get(k) or "?"`) with one. It escapes `&`,
`<`, `>` (Slack's own documented set — the three that make a link or mention parse at all) and
collapses control characters so a value cannot forge an extra bullet line. `*`, `_`, `~`, and
backtick are deliberately left alone: Slack defines no escape for them, they can only produce
cosmetic emphasis, and mangling them would corrupt legitimate values. Numeric counters are not
wrapped — `str(int)` is metacharacter-free by construction.

Covered by `test_hostile_identity_field_cannot_forge_slack_markup` (6 hostile payloads × 4
invariants) and `test_mrkdwn_safe_contract`. Mutation-verified: removing the escape line fails 17
tests. The claim-2 confidence rationale above ("transitive across ~8 write paths, zero escaping at
the render boundary") no longer applies — the property is now local and structural.

**N2 — behaviour change wider than the diff's framing.** The nine pre-existing `_render_message`
branches were changed from `.get("agent_name")` to `.get("agent_name") or "?"`. Previously a NULL
`agent_name` raised `TypeError` inside `sorted()`, was swallowed by `canary_service`, and the alert
was **silently dropped** while the green→red cursor still advanced. Those alerts now send. Intended
and reviewed, but it affects all fifteen invariants, not only the five the issue names.

**N3 — `_format_duration` over-claimed its own guarantee.** Caught `(TypeError, ValueError)` while
`int(float("inf"))` raises `OverflowError`, contradicting a docstring promising the helper can never
be why an alert fails to send. Unreachable today (both producers `int()` at source). **Fixed
in-diff.**

## Coverage gaps

- `src/backend/enterprise` is not checked out in this clone (submodule `update = none`), so
  enterprise agent-creation paths were not audited for unsanitized `agent_name` writes. Relevant
  only to N1, which is already below the finding bar.
- Slack's own rendering was not exercised against a live workspace; Block Kit limits were asserted
  against documented caps (header ≤150, section ≤3000, ≤50 blocks) at 5000 violations × 500 agents.

## Clean phases

| Phase | Checked | Result |
|-------|---------|--------|
| 2 Secrets | Diff scanned for `sk-`/`ghp_`/`gho_`/`ghs_`/`ghu_`/`github_pat_`/`xoxb-`/`xoxp-`/`AKIA`/`AIza`/`sk_live_`; no new log sites; enterprise-disclosure guard pattern run against all 5 changed source+doc files | clean |
| 3 Supply chain | No `requirements.txt` / `package.json` delta; all new imports stdlib + `pytest` | clean |
| 4 CI/CD | No workflow files changed | n/a |
| 5 Infrastructure | No Docker/compose changes; `canary_alerts.py` is not vendored into the agent image → Invariant #5 parity n/a | n/a |
| 6 Webhooks | No inbound webhook, internal endpoint, or agent-scoped-key boundary touched | n/a |
| 7 LLM/AI | Canary is deterministic by design — no LLM anywhere on this path; no `v-html`/frontend surface | clean |
| 8 Skills | No `.claude/skills/` changes in the diff | n/a |
| 9 OWASP A01 | No new routes; no auth dependency changed; no enumeration surface | n/a |
| 9 OWASP A02 | No crypto, no key handling, no JWT | n/a |
| 9 OWASP A03 | Injection analysed in depth — see Verified-safe. No SQL, no command, no template | clean |
| 9 OWASP A04–A10 | No auth flow, config, deserialization, or URL-from-user-input surface in the diff | n/a |
| 10 STRIDE | Spoofing/Repudiation/EoP: no identity or authz surface. Tampering: only via `agent_name`, sanitized. DoS: payload size bounded and tested. Information Disclosure: **F1** | 1 finding |

## Data classification (payload contents)

| Data | Class | Handling |
|------|-------|----------|
| Credential value in `backlog_metadata` | RESTRICTED | **Never leaves the DB** — scrubbed at the invariant; verified unreachable from the render layer |
| `matched_pattern` (family name only) | INTERNAL | Sent to Slack — F1 |
| Agent name, execution id, schedule id | INTERNAL | Sent to Slack — F1 |
| Status, ISO timestamps, counters | INTERNAL | Sent to Slack |
| Webhook URL (the credential) | RESTRICTED | Read from env, never logged or echoed |

## Trend

| | |
|---|---|
| Prior diff-mode reports | `cso-diff-2026-07-01`, `cso-diff-2026-07-09`, `cso-diff-2026-07-17-inv8-auth` |
| Resolved | — |
| Persistent | — |
| New | F1 |
| Direction | Not comparable. Every prior `cso-diff` audited a different branch and scope, so fingerprints do not overlap; F1 is new because no prior report covered the canary alert path. |

## Verdict

**No CRITICAL or HIGH findings. Safe to merge.** One MEDIUM information-disclosure finding, already
remediated in-diff by a documentation control (the restricted-channel deployment note). The
change's central security property — that a credential-detection alert cannot itself leak the
credential — was tested adversarially and holds, and is now pinned by regression tests rather than
resting on reviewer attention.
