---
name: fleet
description: Change the person's fleet settings with them - who an agent can call, who can use it, its schedules, a public chat link, a skill - by proposing one change at a time for them to confirm with one click on the page, or walking them there click by click so they learn where it is
argument-hint: "[what they want changed]"
user-invocable: true
---

# Settings for their agents

You never change a setting with your own key: Trinity only lets a person grant a power, and these changes are theirs to make. So you **propose**, and they confirm with one click on a card the page shows them, or you **show them** where it is set and they change it there.

## What you can see

The page writes `fleet-state.json` into your workspace, read with the person's own session: each of their agents with its `name` and `label`, who it may call (`may_call`), who it is shared with (`shared_with`), its `schedules` (`id`, `name`, `enabled`), whether its Autonomy switch is on (`autonomy`: `true`, `false`, or `null` when the page couldn't read it), how many `public_links` it has, and its `skills`. Read it every time. It is the only list of their agents you can trust here.

An agent's schedules only run while its Autonomy switch is on, and a new agent starts with it off. When a schedule is on but `autonomy` is `false`, say that Autonomy is off, so the schedule won't run, and offer the `schedules` walk: it ends on the Enable autonomy button. A card can't turn Autonomy on.

If it is missing, or older than this conversation, say the page is refreshing it and ask them to try again in a moment. Never guess an agent's name or a schedule's id.

## Propose one change

Work out the one change they mean. If they asked for several, do them one at a time, each after the last is answered. Then write the canvas `fleet` with `set_canvas`: `canvas_id: "fleet"`, `audience: "roster"`, and one block `{ "id": "proposal", "kind": "json", "payload": <the proposal> }`. Give every proposal a new `id` you haven't used.

Exactly one of these, with exactly these fields:

| They want | `action` | Fields |
|---|---|---|
| Let one of their agents call another | `allow-call` | `from`, `to` (agent names) |
| Stop that | `stop-call` | `from`, `to` |
| Share an agent with a person | `share` | `agent`, `email` |
| Stop sharing it | `unshare` | `agent`, `email` |
| Turn a schedule on | `schedule-on` | `agent`, `schedule_id` |
| Turn a schedule off | `schedule-off` | `agent`, `schedule_id` |
| A public chat link for an agent | `public-link` | `agent` |
| Add a skill from the library to an agent | `add-skill` | `agent`, `skill` |

The page writes the card's words itself, from these fields, so don't put a description in the proposal. Then tell them in one sentence that a card has appeared above the box to type in, to Confirm or say Not now, and end your turn.

## Show them where it is set

For anything a card can't do, or when they want to learn where a setting is, propose `{ "id": ..., "action": "show-me", "key": <place>, "agent": <agent name> }` instead. The page gives them a Show me link. The places are `permissions`, `people`, `schedules`, `public-links`, `skills`, `channels` (Slack, Telegram, WhatsApp), `keys` (API keys for the whole instance; no `agent`), and `credentials` (one agent's own keys).

`credentials`, `people`, `permissions` and `schedules` are walks, not jumps, because they teach them the way: each starts on the Dashboard and lights up each thing to click in turn, so next time they can do it without you. The other places open the page with that control lit up.

- `credentials`: the agent, its Settings, Credentials, the box for each key, then Save credentials. Add `"keys"` with the names to fill now, separated by commas, when only some are needed yet: `{ "id": "p7", "action": "show-me", "key": "credentials", "agent": "game-promoter", "keys": "SUPABASE_URL,SUPABASE_KEY" }`. Names only, never a value. Without `keys` it lights up every required key that is missing.
- `people` (sharing an agent with someone): the agent, its Settings, People, then the email box and Add person. They type the email there themselves, so never put an email in it: `{ "id": "p8", "action": "show-me", "key": "people", "agent": "game-promoter" }`.
- `permissions` (letting one agent call another): the agent, its Permissions tab, the agent to tick, then Save Permissions. Add `"to"` with the name of the agent it should call, one of theirs: `{ "id": "p9", "action": "show-me", "key": "permissions", "agent": "game-promoter", "to": "match-reporter" }`. Without `to` it lights the whole list.
- `schedules` (turning a schedule on): the agent, its Schedules tab, that schedule's switch, then Enable autonomy if it is off. Add `"schedule_id"` from `fleet-state.json`: `{ "id": "p10", "action": "show-me", "key": "schedules", "agent": "game-promoter", "schedule_id": "<its id>" }`. Without it, it uses the only schedule, or lights the list.

Use Show me for:

- learning where a setting is, and working through a level (see Levels in `CLAUDE.md`): offer the walk first, and say you can also make the change for them with one click, as a card, if they'd rather;

- anything that needs a secret or a sign-in somewhere else: an agent's own keys (`credentials`), connecting Slack, Telegram or WhatsApp, API keys, GitHub, Claude. Never ask for a key, a token or a password, and never accept one in the chat;
- deleting anything, or anything that can't be undone;
- any change not in the table above.

## After a walk

Tell them in one sentence that a Show me card has appeared above the box to type in, that it opens a new tab and lights up each click, and to come back and say when they're done.

- After `credentials`, check with `get_credential_status` for that agent and tell them which of the names are now set, by name only. If one is still missing, offer the walk again for just that one.
- After the others, read `fleet-state.json` again (the page refreshes it when they come back to this tab) and tell them what is set now. Never say it is done before the file shows it. If it doesn't show yet, say so and offer the walk again.

## What the page tells you

After a card, their next message comes from the page and starts with one of these:

- `Done: <the change>.` It took, and the page has checked. Say so in one sentence, and offer the next setting that makes sense, or Show me for where it is set.
- `Not now: <the change>.` They declined. Leave it, and don't propose it again unless they ask.
- `That didn't work: <the change>. <why>` Trinity refused it, so nothing was changed. Explain the reason in plain words and offer Show me.
- `Not sure it worked: <the change>. <why>` The page couldn't confirm it either way. Don't propose it again yet: offer Show me so they can see whether it is set.

## Explaining features

For how a Trinity feature works, use `ask_trinity` and Trinity's own docs, in words that fit their level from `profile.md`. Explain first, then offer to set it up with them.
