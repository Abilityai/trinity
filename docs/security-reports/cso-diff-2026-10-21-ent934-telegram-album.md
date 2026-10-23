# CSO Diff Audit — ent#934 Telegram albums arrive as one agent turn

- **Date**: 2026-10-21 · **Mode**: `--diff` (daily gate 8/10) · **Branch**: `feature/ent934-telegram-album` vs `origin/dev` (merge-base `cd1548c1c`)
- **Scope**: `src/backend/adapters/transports/inbound_coalescer.py` (new), `src/backend/adapters/transports/telegram_webhook.py`, `src/backend/adapters/telegram_adapter.py`, `src/backend/db/telegram_channels.py`, `tests/unit/test_ent934_telegram_album.py`, four docs and one learnings fragment

## Verdict: 0 findings.

Album items sharing a `media_group_id` are buffered in Redis and reach the pipeline as one merged update; webhook dedup moves from a `<= last_update_id` watermark to a per-update `SET NX` claim; a mention in a caption now counts as a mention.

## Checks run (diff mode; phases 0, 1, 12–14 + diff-relevant rows)

| Check | Result |
|---|---|
| Attack-surface delta | No new routes, dependencies, CI or Docker changes. The only inbound path is the existing `POST /api/telegram/webhook/{webhook_secret}` |
| Webhook auth order | Unchanged and first: binding lookup by `webhook_secret`, then the `X-Telegram-Bot-Api-Secret-Token` compare, both before the body is parsed. Dedup and buffering run only after both pass, so an unauthenticated caller cannot reach the new Redis code |
| Dedup keys | `trinity:telegram:update:{binding_id}:{update_id}`; `binding_id` comes from the DB row, `update_id` from an authenticated Telegram body. Per-bot namespace, so one bot's ids cannot suppress another's. Redis-down fallback is the previous watermark (no regression); its write is a parameterised SQLAlchemy `WHERE last_update_id < :id` |
| Buffer keys and bounds | `trinity:telegram:album:{binding_id}:{chat_id}:{media_group_id}`, all Telegram-supplied after auth. Bounded: 10 items per album (force flush), 64 KB of JSON per item, 50 open albums per bot, 10 min TTL. Past any bound the item takes today's one-turn path — degradation, not denial or loss |
| Redis ACL (two-network / ACL model) | Commands used: EXISTS, PEXPIRE, DEL, RPUSH, LRANGE, INCR, GET, SET NX, ZADD, ZREM, ZSCORE, ZCARD, ZRANGEBYSCORE, WATCH/MULTI/EXEC/UNWATCH. All within the `backend` user's `+@keyspace +@list +@write +@read +@sortedset +@transaction` grant in `docker-compose.prod.yml`, `docker-compose.hosted.yml` and `docker-compose.yml`; none is `@dangerous`. Backend is the only writer; agents stay off the platform network |
| Data at rest in Redis | Album item JSON (user text, Telegram `file_id`s, sender ids) for ~1.5 s typically, 10 min at most. A `file_id` is useless without the bot token. Same Redis that already holds pending-login emails for this adapter |
| Deserialisation | `json.loads` of JSON this process wrote; no pickle, no eval |
| Prompt surface (LLM) | Caption and document names enter the user-message text, as they did for single photos (FP rule #11). Nothing reaches a system prompt or tool schema |
| Trigger rules | Caption mentions now trigger in mention-mode groups (an intended behaviour fix). The trigger decision is still made once in `parse_message`; the from-bot filter runs before buffering, so bot loops cannot be buffered into a turn |
| Logging | Logs carry group ids, item counts and agent names — no message text, file ids or tokens |
| Secrets in diff | None |
| Hidden Unicode in changed files | None |
| Enterprise-docs guard | The diff's docs add no paid-module tokens |
| Tests | `tests/unit/test_ent934_telegram_album.py` 35/35 green; reverting the dedup turns `test_out_of_order_updates_are_both_processed` red, reverting caption mentions turns two caption tests red |

## Hypotheses

None (daily mode).

## Coverage gaps

Full-repo phases not run in diff mode: history secrets, dependency scan, CI/CD, image internals.
