# Feature: Per-Client Channel Access Controls (trinity-enterprise#21)

## Overview
From the client roster in Settings › Channels, an agent's owner can block, unblock, revoke or approve one external channel client (Telegram, WhatsApp) on that agent, instead of only changing the agent-wide access policy. Requirement: `requirements/public-access.md` §46.2.

## User Story
As an agent owner, I want to stop one person on Telegram or WhatsApp from reaching my agent, or withdraw the access I gave them, without changing who else can reach it.

## Entry Points
- **UI**: Settings › Channels → Who has talked to it → Open roster (`agent-settings/channels/ClientRoster.vue`) → Block / Unblock / Revoke / Approve per row. An approval there re-reads the pending list in Who can chat, and a decision in Who can chat re-reads the roster (`ChannelsPane.vue`).
- **API**: `POST /api/agents/{name}/clients/block` (`{channel, client_id, reason?}`), `…/clients/unblock`, `…/clients/revoke`; approve is the existing `POST /api/agents/{name}/access-requests/{id}/decide`

## Flow

```
ClientRoster.vue ─→ routers/sharing.py (OwnedAgentByName + require_person, audit `authorization`)
                    └─→ services/client_roster_service.py
                          get_client_roster: chat-link rows (Telegram, WhatsApp)
                            + rows with no chat link: a block whose links are gone (bot
                              unbind), a pending access request's requester (ent#737
                              `requester_channel_id`) — held at the gate, so never linked
                            → access: blocked > granted (share) > pending (email or
                              channel id) > none
                          block / unblock / revoke → 404 unless the client is on the roster
                    └─→ db/channel_client_blocks.py (UNIQUE agent, channel, client_id)
```

### Enforcement — every way in, keyed on the channel-native sender id
| Path | Where | Behaviour for a blocked sender |
|---|---|---|
| A turn (DM or group, any policy) | `adapters/message_router.py::_handle_message_inner`, right after the agent resolves | Dropped: no rate-limit charge, typing, reply, auth prompt or access request |
| An observed (un-tagged) group message | `message_router._record_observed_message` | Not recorded into the group's history |
| A transport command (`/login`, `/reset@bot`, …) | `transports/telegram_webhook.py::_process_update`, `transports/twilio_webhook.py::_process_update` | Dropped before the adapter answers it: no reply, no code email, no session reset |
| A proactive message / access-grant notification | `services/proactive_message_service.py::_refuse_blocked` (Telegram, WhatsApp delivery) | `ChannelDeliveryError("recipient_blocked…")`; `auto` tries the next channel (a block is per channel) |

## Database
`channel_client_blocks` — `id`, `agent_name`, `channel`, `client_id`, `blocked_at`, `blocked_by_id`, `blocked_by_email`, `reason`; UNIQUE `(agent_name, channel, client_id)`. SQLite `channel_client_blocks` migration + Alembic `0116_channel_client_blocks`. Registered in the agent cleanup registry (re-keyed on rename, removed on delete).

## Limits
- A group-only sender who never filed an access request has no roster row, so there is nothing to block them from; a chat link is written only for a DM that passed the gate (#1533), deliberately.
- Under open access, revoke removes the share but the policy still admits the client; block is the control that always holds.

## Testing
- `tests/unit/test_ent21_channel_client_controls.py` — roster states, block/unblock/revoke against a real DB; the parsed Telegram `sender_id` equals the roster `client_id`; transport commands from a blocked sender (Telegram, WhatsApp); a block survives a bot unbind and stays unblockable; a pending requester with no chat link is listed and blockable; proactive delivery refused.
- `tests/unit/test_message_router_access_gate.py` — the turn gate (DM, group) and the observed-message path.
- `src/frontend/tests/unit/clientRosterControls.mount.spec.js` — the roster controls.
