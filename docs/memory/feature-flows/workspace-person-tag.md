# Workspace — tag a person into a conversation (trinity-enterprise#631)

## Overview

In a 1:1 chat or a room, a person types `@` and a colleague's name; the colleague gets
**one item in their Workspace Inbox** — kind *Unread*, never *Action* — saying who tagged
them, in which conversation and which message. A tag is a **pointer, not a seat**: it
changes no membership and grants no access. Opening the item shows the tagged message with a
little around it only to someone who could already see the conversation; anyone else is told
they can't, and who can let them in. The tagger sees, under their own message, that the tag
landed and whether it was read. Delivery is in-app only.

The item is one row on the queue-item ledger the Inbox already reads (`operator_queue`,
ent#610): no new store, no schema change.

Requirement: `docs/memory/requirements/core-agent.md` §5.42. Architecture:
`docs/memory/architecture/workspace.md` → Person tags. Parent epic: ent#630 (the seat is a
separate, incubating decision).

## User Story

As someone working with an agent, I want to name a colleague in the conversation so they
know about the brief that came back or the decision that was made — without re-typing it
somewhere else, and without giving them access they do not already have.

## Entry Points

- **UI (tagger):** the composer's `@` typeahead in a room (`PortalRoom.vue`) or a 1:1 chat
  (`PortalConversation.vue`) — "Agents and people".
- **UI (tagged person):** the Workspace Inbox (`/workspace/inbox`), Unread and All tabs.
- **API:**
  - `GET /api/rooms/{room_id}/people?q=` · `GET /api/enterprise/client-portal/agents/{name}/people?q=` — the picker
  - `POST /api/rooms/{room_id}/messages` `{content, tags?}` · `POST …/client-portal/agents/{name}/chat[/stream]` `{…, tags?}`
  - `GET /api/rooms/{room_id}` → `own_tags`, per-message `tags` · `GET …/agents/{name}/history` → `tags` on own rows
  - `GET /api/enterprise/client-portal/mentions` · `GET …/mentions/{id}` · `POST …/mentions/{id}/read`

## Frontend Layer

```
composer textarea  @bo
  detectTypeaheadTrigger → {kind:'@', query:'bo'}
  usePeoplePicker.lookup('bo')        debounced 150 ms; nothing on an empty query
    store.fetchRoomPeople(roomId,'bo') | store.fetchChatPeople(agent,'bo')
      403 → picker.unavailable (agents-only for the session; an external client)
  rows = agents (filterAgentCandidates) + peopleRows(people)   heading "Agents and people"
  no agent AND server answered [] for 'bo' → noOneCalled('bo', agents)   ← the named refusal
  Tab / Enter-on-selection / click
    person → picker.pick(person); insert personToken = "@Bob Baker"
send()
  tags = tagsInText(text, picked)     only people whose "@Name" is still in the text, ≤10
  room: store.postRoomMessage(roomId, text, {tags})
  1:1 : deliver(text, {tags}) → startPortalChat / sendPortalChat({…, tags})
        (an @agent escalation carries tags into the new room's first post)
  refused (422 unknown_person / 403) → text handed back, InlineError names the reason

transcript
  PortalTagMarks under the sender's own message:
    room: tagsFor(m, room.own_tags)   (whole-room map, refreshed every poll)
    1:1 : message.tags (history)
    "Bob Baker · in their Inbox" | "· read" | "· not delivered"

Inbox (tagged person)
  Portal.vue asks tick (20 s) → store.fetchMentions()
  portalInbox.unreadItems(threads, previews, mentions)  mention:<id> rows, New
  portalInbox.allItems(…, mentions)                      read ones too
  inboxCounts(threads, openAsks, mentions).came          +1 per unread tag (pinned row, Unread tab)
  open row → PortalInboxPane → PortalMentionCard
    store.openMention(id) → skeleton | LoadFailed | card
      can_see  → context list (tagged one marked) + "Open the room"
      !can_see → "You're not in this room … X runs this room and can let you in"
    emits rendered → PortalInbox.readNow → store.markMentionRead(id) (once)
    row kept in place, drawn Read (stableRows ghost)
```

## Backend Layer

```
POST /api/rooms/{id}/messages {content, tags}
  shared_sessions.router.post_message → service.post_message(tags=)
    _require_membership; _enforce_budgets
    tagger = _tagger(current_user)            person_mention_service.tagger_for
       agent key → 403 agents_cannot_tag; external client → 403 tagging_unavailable
    people = resolve_tags(tags, _room_agents(participants), tagger)
       dedupe; >10 → 422 too_many_tags; self → 422 cannot_tag_yourself;
       db.get_taggable_person(agents, email) None → 422 unknown_person {name}
       (BEFORE the newcomer join and the append — a refused tag writes nothing)
    _join_mentioned_newcomers; mentions = resolve_mentions(content, participants)   unchanged
    append_message(id, …, mentions)                       stored mentions = agents only
    _deliver_tags → person_mention_service.deliver(Conversation('room', id, first agent, name))
       per person: db.create_person_mention(request_id = mention-<conv>-<msg>-<email>, …)
         operator_queue INSERT … ON CONFLICT (agent_name, request_id) DO NOTHING
         type 'mention', status 'delivered', addressed_to_email, context.mention
       failure → [{label, state:'failed'}], the post still succeeds (#3210)
    _after_landing (wakes)                                 unchanged
  → {room_id, seq, mentions, woke, tags:[{label, state, read_at}]}

POST …/client-portal/agents/{a}/chat/stream {…, tags}
  router: person_tags = service.resolve_chat_tags(principal, a, tags)
    tagger_for_portal (platform person only) + resolve_tags([a])  → ChatTagError 403/422
  start_portal_turn(…, person_tags) → background portal_chat(…, person_tags)
    user_row_id = _persist_user_turn(…)
    deliver_chat_tags(a, session_id, user_row_id, person_tags)   best-effort

GET /api/enterprise/client-portal/mentions/{id}        (client_portal/mentions/router.py)
  person_mention_service.open_for_reader(Reader(email, is_platform), id)
    _mine: addressee match, else 404 not_found (same as missing)
    room: _room_access — per door, as _require_membership: live workspace_user
          (email) on either door; live user participant (username) or admin
          only when is_platform (never via a portal session, #78)
      yes → shared_sessions.db.get_messages(seq−4, limit 6) → context, message
      no  → can_see false, id null, no content, can_let_you_in = moderators
    chat: can_see false, can_let_you_in = [tagger]
POST …/mentions/{id}/read   person only → db.mark_person_mention_read (CAS delivered→read)
```

## Data

| Store | What | Notes |
|---|---|---|
| `operator_queue` | one row per (conversation, message, person) | `type='mention'`, `status` `delivered`/`read`, `acknowledged_at` = read time, `addressed_to_email`, `context.mention`, `raised_by='person'`, `channel='mention'`, `request_id` prefix `mention-` (reserved, about-a-person) |
| `enterprise_room_messages.mentions` | agents to wake | unchanged — never holds a person |

No schema change; no migration on either track.

## Side Effects

- No WebSocket broadcast: the Workspace has no `/ws` for a portal client; the Inbox reads
  tags on its 20 s asks tick, and the room's own 3 s poll carries the tagger's marks.
- No audit row, no ending observer, no agent wake — the row is never `pending`.
- Logged: `person tag: <tagger> tagged a person in <kind> <id>` (no address).

## Error Handling

| Case | Answer |
|---|---|
| unknown / suspended / no access to the agents | 422 `unknown_person` + `name`, before any write |
| tagging yourself | 422 `cannot_tag_yourself` |
| more than 10 distinct people | 422 `too_many_tags` |
| agent key / non-person key | 403 `agents_cannot_tag` |
| external Workspace client | 403 `tagging_unavailable` (the picker goes agents-only) |
| another person's item, or none | 404 `not_found` (uniform) |
| marking read with a system key | 403 `person_required` |
| row write fails after the message landed | post succeeds; tagger's mark says "not delivered" |
| Inbox tags read fails | last good list kept; `mentionsFailed` |

## Security

- **#78**: the reader projection carries no cost, execution id, addressee or ledger id; the
  admin's every-room visibility rides the platform door only.
- **Invariant #8**: not-yours and not-there are one 404; the send-time refusal does not
  distinguish "no account" from "no access".
- **Agents never tag**: `tagger_for` refuses agent identity before anything else, and the
  `mention-` prefix is platform-reserved on the queue-file seam.
- **Operator door**: `mention` rows are excluded from the Operating Room's list, totals and
  item read, so a sharee of an agent cannot see who tagged whom.
- The room picker route is `DELEGATED` in the #2996 route census (its principal may be a
  Workspace client, so no dependency gate fits); the refusal is `tagger_for`, pinned by
  `test_an_agent_key_cannot_list_people_to_tag`.

## Testing

- `tests/unit/test_ent631_person_mention.py` — real `init_schema` SQLite: one Unread item;
  not in Action; dedupe and re-delivery; too many; unknown name refused by name before any
  write; no-access refused identically; suspended; picker scope and the empty-query bound;
  agent wake list unchanged and stored mentions agent-only; can't-see vs can-see; admin
  bypass platform-door only; no internal facts; uniform 404; chat tag points at the tagger;
  agents and external clients cannot tag; reserved prefix; delivered → read; operator door
  excludes; the chat seam and the Inbox door.
- `src/frontend/tests/unit/portalPersonMention.spec.js` — the pure rules.
- `src/frontend/tests/unit/portalPersonMention.mount.spec.js` — the room picker (offer, Tab,
  send carries the address, deleted name untags, named refusal, refused send keeps the text),
  the tagger's marks, the card (can't see / can see / failed), the Inbox read-once.

## Related

- ent#610 (the Inbox ledger), ent#606 (addressing by role), ent#450 (the picker pattern),
  ent#564 (out-of-app reach — out of scope), ent#630 (the epic; the seat).
- `workspace-inbox.md`, `operating-room.md`.
