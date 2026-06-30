# Feature Flow: Per-Agent Public-Channel Model Override (#894)

## Overview
Lets an agent **owner** choose which Claude model the agent uses for **public-facing**
conversations — the public chat link, Slack/Telegram/WhatsApp channels, and x402 paid
chat — independently of the platform-wide default. The owner's own authenticated chats,
scheduled runs, and agent-to-agent calls are **unaffected** (those keep resolving model
the same way they did before).

Epic: #1079 (Channels & Integrations).

## User Story
As an agent owner, I want my public-facing agent to run on a cheaper model (Haiku) for
cost control, or a more capable one (Opus), without changing the platform default or how
the agent behaves in my own chats.

## Data
- `agent_ownership.public_channel_model TEXT NULL` — the override; **NULL = inherit the
  platform default**. Additive, no backfill. Dual-track migration: SQLite
  `agent_ownership_public_channel_model` (`db/migrations.py`) + Alembic
  `0009_agent_ownership_public_channel_model`; DDL in `db/schema.py`/`db/tables.py`.
- Getter/setter: `db/agents.py:get_public_channel_model` / `set_public_channel_model`
  (facade in `database.py`). The getter **fails closed to None** if the persisted value
  is no longer a valid public-channel model (defense-in-depth, mirrors `voice_name` #28).

## Validation
- `services/settings_service.PUBLIC_CHANNEL_MODELS` (frozenset of current-gen ids) +
  `is_valid_public_channel_model()`. Kept in sync by hand with `ModelSelector.vue`
  presets (no shared Python/Vue registry). A model removed after it was saved degrades to
  the platform default (consistent with #1080 graceful degradation).

## Backend
- `GET /api/agents/{name}/public-channel-model` (any authenticated accessor) → raw
  override, `resolved_model`, `is_overridden`, `platform_default`, `available_models`.
- `PUT /api/agents/{name}/public-channel-model` (owner-only via `can_user_share_agent`) →
  set/clear; 422 on an off-whitelist value; audit `CONFIGURATION:update_public_channel_model`.
- Both in `routers/agent_config.py`; request model `PublicChannelModelUpdate` (`models.py`).

## Resolution (override → platform default → hardcoded fallback)
Each public call site passes `model=db.get_public_channel_model(agent_name)` into
`task_execution_service.execute_task` (None → the existing platform-default fallback at
`execute_task`):
- `routers/public.py` — sync path + `_execute_public_chat_background` (async).
- `adapters/message_router.py` — Slack/Telegram/WhatsApp.
- `routers/paid.py` — x402 paid chat.

Non-public triggers (chat, schedule with its own `agent_schedules.model`, loop, fan-out,
agent-to-agent) never touch this field — their model resolution is unchanged.

## Frontend
`components/SharingPanel.vue` — a "Public chat model" `<select>` (options from the GET's
`available_models`, "" = Use platform default) above the Channels section; loads on mount,
PUTs on change, re-syncs on error.

## Testing
`tests/unit/test_894_public_channel_model.py`: validator (valid/invalid), db round-trip /
clear / invalid-persisted→None / missing-agent no-op (db_harness), and a static guard that
all three public call sites pass the override into `execute_task`.
