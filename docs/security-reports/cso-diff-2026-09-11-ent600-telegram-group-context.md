# CSO diff audit — abilityai/trinity-enterprise#600 (`feature/600-telegram-group-context`)

**Date**: 2026-09-11 · **Mode**: `--diff` (daily gate 8/10) · **cso v1.1** · **Base**: merge-base `682fce30` on `dev` · **Diff**: 31 public files (+~760 / −85) incl. 1 new service module, 1 new Alembic revision, 1 new unit test file (working tree, pre-commit). Surface: no endpoint added; one owner-only PUT gains a human-only boolean arm; one accessor-tier GET gains three derived fields; one webhook-driven code path (observe) added before the turn pipeline; one new outbound HTTPS call (Telegram `getMe`); one prompt surface widened (group history block).

## Verdict
**No findings at the gate.** Every candidate was traced end-to-end and refuted or is a hard exclusion. Two items were hardened on the branch before commit rather than reported (a token-bearing URL that an httpx error message could have carried into a debug log; a dead constant).

## Attack surface introduced by the diff
- **Endpoints**: none added. `PUT /api/agents/{name}/telegram/groups/{id}` accepts `context_enabled`; the arm calls `reject_agent_principal` exactly like ent#265's `allow_proactive` (grant-vs-use: recording a room's conversation is the owner's decision, never an agent's). `GET …/telegram/groups` (`AuthorizedAgentByName`, uniform 404) adds `context_enabled`, `context_status` (enum of four static strings), `context_hint` (static server text), `last_untagged_seen_at` (timestamp) — the same tenant-data tier as the `chat_title`/`welcome_text` already on that route.
- **Webhook path**: the Telegram transport's command branch is now gated on `observe_only` (`if normalized and not normalized.metadata.get("observe_only")`), so an un-tagged `/reset`/`/help`/`/login` in a mention-mode group stays inert — the pre-change behaviour, previously implied by `parse_message` returning `None`. Webhook authentication (URL secret + `X-Telegram-Bot-Api-Secret-Token`) is untouched.
- **New DB writes reachable from the webhook without a turn**: `_record_observed_message` — one `public_chat_messages` insert (SQLAlchemy Core, bound values), one `telegram_group_configs.last_untagged_seen_at` update, a cadenced prune whose subquery binds `session_id`/`keep`. Reached only for updates the transport already authenticated; gated by the per-group toggle and `group_auth_mode=any_verified` (a locked group records nothing).
- **Outbound**: `fetch_can_read_all_group_messages` → `https://api.telegram.org/bot{token}/getMe`, fixed host, `timeout=10`, called from three token-in-hand sites (connect, Verify, bot-added event). The event-handler call is wrapped and logs the exception **type only** — an httpx error string can carry the request URL, and that URL embeds the bot token.
- **Prompt surface**: group turns gain a delimited block of stored group messages, and a `[Replying to X: "…"]` line. Both are third-party content in the **user-message position** (FP rule 11). Defence in depth anyway: speaker labels are whitespace-collapsed, bracket-stripped and clamped to 64 chars; content is collapsed to one line and clamped to 500; the two block delimiters are stripped from content; assistant lines carry a `[agent]` prefix a user label cannot spell (brackets stripped). Bounded: ≤40 lines × 500 chars.
- **Schema**: three additive nullable/defaulted columns on both tracks (`db/migrations.py::telegram_group_context`, Alembic `0059`), guarded idempotent; single Alembic head verified.

## Findings
| # | Sev | Conf | Status | Category | Finding | Phase | File:Line |
|---|-----|------|--------|----------|---------|-------|-----------|
| — | — | — | — | — | none at the gate | — | — |

## Verification performed
- **Secrets / enterprise disclosure (P2)**: known-prefix, internal-host and email scan over every added line in tracked and new files — none. No `enterprise_*` token, no paid-module name; the docs added describe the OSS Telegram adapter only.
- **Access control (P6 / P9-A01)**: the only write arm added is human-only (`reject_agent_principal` when `config.context_enabled is not None`, `routers/telegram.py`); ownership of the group config row is still asserted against `db.get_telegram_groups_for_agent(agent_name)` before the update. No new agent-name path → no new enumeration split. Standing guards `test_186_enumeration_uniformity`, `test_agent_auth_header_guard`, `test_2094_dependency_path_param_pairing` run green on this tree.
- **Injection (P9-A03)**: no `text()` added; `get_recent_messages(since=)` appends a bound `>=` condition; `prune_session` uses `not_in(select … limit)` with bound values; `touch_group_untagged_seen`/`set_can_read_all_group_messages` are `update().values()`. No subprocess, no `v-html`, no `innerHTML` in the Vue diff (BaseBadge + text interpolation only).
- **Sibling-key boundaries (P6)**: no endpoint accepting an agent's own key was added or changed.
- **LLM (P7)**: producers of the new prompt block are group members (already producers on tagged turns and in `all`/`observe` modes); consumer is the same agent. The forgery vector both independent reviewers raised (a `first_name` equal to the agent name, or a body containing the close delimiter) is closed by construction and pinned by `test_forged_labels_and_delimiters_are_neutralised`. No system-prompt write; no tool-schema write. Cost: observation spends no model call; a group turn's prompt grows by a bounded block.
- **Logging (P9-A09)**: the token never reaches a log; the getMe refresh logs the exception class only; observed-message failures log a message without content.
- **Data at rest (P11)**: group chatter now persists in `mention` mode too (it already did in `all`/`observe`), plaintext in `public_chat_messages` like every public-chat turn — INTERNAL/CONFIDENTIAL class, agent-delete cascade unchanged, per-group opt-out exposed to the owner, per-session cap 500 rows. Documented in user docs.
- **Supply chain (P3), CI/CD (P4), infrastructure (P5), skills (P8)**: no dependency, lockfile, workflow, Dockerfile, compose, network or skill file in the diff.

## Appendix (≤4/10, non-blocking)
1. **`/reset@bot` by any member clears the group's shared context** — availability only (hard exclusion #1); documented in user docs; a bare `/reset` is inert in mention mode. Admin-only reset is a product choice left open.
2. **Speaker labels are Telegram display names** — a member can name themselves "Admin"; the label is attribution text under the same trust as the message body, which #903 already accepted for Slack.
3. **`last_update_id` read-then-write dedup across two uvicorn workers** can drop a lower update id that arrives after a higher one — pre-existing (`telegram_webhook.py` `handle_webhook`), volume-sensitive, not a security class; filed as a public reliability bug alongside this PR.

*AI-assisted scan, not a professional audit.*
