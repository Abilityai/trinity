# CSO diff audit: email-code sign-in policy (abilityai/trinity-enterprise#849)

Date: 2026-10-09 · Mode: daily, `--diff` vs `origin/dev` · Open findings: 0

## Findings (both fixed in this branch)

| # | Sev | Conf | Evidence | Finding | File |
|---|---|---|---|---|---|
| 1 | MEDIUM | 9/10 | SUPPORTED | Slack `require_email` emailed code did not consult the login policy | `src/backend/adapters/slack_adapter.py` |
| 2 | MEDIUM | 9/10 | SUPPORTED | Public-link per-address 429 counted only addresses that were sent a code, so the 4th request told a member from an outsider | `src/backend/routers/public.py` |

1. With the policy off, a member's inbox could still complete the Slack verification step. Impact is bounded: the access policy reads the Slack profile email, so the code grants no sharing or owner privilege. Fix: same member rule and detached send as Telegram/WhatsApp; code drawn from `secrets`; the caller guard now also covers `send_verification_code`.
2. Fix: while codes are refused, every request is counted per address before the detached send.

## Accepted

- A provider or member-lookup error fails open. Owner decision: availability over lockout.
- Sessions, MCP keys and channel bindings issued before the toggle stay valid; tracked separately.

## Pre-existing, unrelated

- `POST /api/access/request` returns `already_registered`, which reveals whitelist membership when self-signup is enabled.

## Checked

Secrets grep on added lines, enterprise-docs guard (new seam file listed), `/token` untouched, every emailed-code mint/redeem/send caller (AST guard), response bodies and timing on each refusal path, fresh-context adversarial verifier, mutation per fix.

Coverage gaps: none for the diff scope.
