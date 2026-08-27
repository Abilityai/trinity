# Feature: Workspace ratings — one click, on the thing being judged

> **Status**: ✅ Implemented (2026-08-24)
> **Issue**: abilityai/trinity-enterprise#366
> **Requirement**: `docs/memory/requirements/core-agent.md` §5.15
> **Related**: [agent-evaluations.md](agent-evaluations.md) (ent#206 — the referee surface and its write fence), [workspace-deliverables.md](workspace-deliverables.md) (ent#365 — the card this attaches to), `docs/memory/requirements/core-agent.md` §5.14

## Overview

Feedback used to require knowing a skill existed and choosing to stop and invoke
it. That catches the conscientious minority and misses everyone else —
disproportionately the unhappy ones.

Now: thumbs on an agent message, **Useful / Not what I needed** on a deliverable,
and a comment box that opens on a negative. One click, attached to the specific
thing being judged.

## Why this is a platform primitive and not a skill

A capture-feedback skill runs **inside the agent**, so it can summarise
charitably, omit, or fail silently. That is the same reasoning that made
agent-authored reports unusable as the evaluation surface and produced
`agent_evaluations` with a write fence (ent#206): **the graded agent must never
write its own grade**. A user rating is the one score that must not pass through
the thing being scored.

So ent#366 **amends** that fence rather than working around it: a Workspace
principal becomes a legitimate writer, filed under `evaluator = workspace:<email>`.
The rated agent still has no write path.

The rich capture stays a skill — reading back a session and writing prose into
canon is judgment work over material only the agent holds.

## The gates

```
POST /api/enterprise/client-portal/agents/{agent}/ratings
  ├─ roster scope            (off-roster → 404)
  ├─ target_kind ∈ {message, deliverable}   (else 422)
  ├─ rating ∈ {up, down}                    (else 422)
  └─ target visible TO THIS READER          (else 404, uniform)
        message      → row belongs to this agent AND this client,
                       and is the AGENT's message
        deliverable  → ent#365's audience gate (`get_report_for_client`)
```

Two of those deserve their reasons written down:

- **Ids prove nothing.** Message ids and report ids are global. A route that
  trusted one would let anyone rate — and *comment on* — a conversation they have
  never seen. The check is against the reader, never against the agent.
- **You cannot rate your own message.** Only the agent's messages are rateable;
  allowing a user's own would put their self-rating into the agent's tally.
- The refusal is the **same 404** for a target that does not exist and one that
  is not yours, so this is not an existence oracle (Invariant #8).

## One rating per person per thing

```sql
CREATE UNIQUE INDEX idx_agent_evaluations_rating_target
  ON agent_evaluations(evaluator, target_kind, target_id)
  WHERE target_id IS NOT NULL;
```

A second thumb is a **correction**, not a second vote — which is what makes a raw
tally count *people* rather than clicks. The index carries the rule so two rapid
clicks cannot race into two rows; the service's update-then-insert reflects it.
Partial on `target_id IS NOT NULL`, so however many graded-run rows a Tier-0 pass
writes (all with no target), none of them are affected.

## What the rated agent may read — the grooming decision

The issue left this open. Decided: **tallies yes, the words never.**

`_redact_for_agent_principal` strips `comment` for an agent-scoped caller and
sets `comment_withheld: true`, so a reader can tell *"no comment"* from *"not
yours to read"*. Quality, target and evaluator all survive, so the agent can
count its own ratings and no operator surface loses anything.

Two reasons, and the second is the harder one:

1. A score the agent can read is a loop it may start optimising for — the issue's
   own concern.
2. The comment is untrusted free text written by someone who is, by construction,
   annoyed. Handing it verbatim to the agent being criticised is a
   prompt-injection path **into** it.

## The comment, and degrading without the skill

The rating and its comment are durable **before** anything is dispatched. Then:

| Agent has `capture-feedback` | What happens | What the person is told |
|---|---|---|
| yes | a background turn runs the skill with the words **fenced as data** (the `routers/webhooks.py` framing), as its own execution — never a message in the client's thread | "Thanks — passed on to the agent." |
| no | nothing more | "Thanks — recorded for the team." |

Both are true statements. The second is the AC #6 case, and it is why the
acknowledgement is derived from the server's answer rather than assumed.

The dispatch is a **background task**: it is a whole agent turn, and a person
clicking *not what I needed* should not wait on the agent that just disappointed
them. `dispatched` therefore means "handed off" — the turn's own outcome is
observable as an ordinary execution row.

## The tally

Raw counts on the agent page, never a percentage. One thumbs-down out of one
rating renders as "100% negative" — a number that looks like evidence and is not.
Both figures show, so the denominator (the honest part) is on screen. An
unreadable tally reports `unavailable` rather than a zero it did not measure.

Explicitly **not NPS**: a promoter percentage from a handful of users has the
same problem, and the free text was the valuable part anyway.

## Files

| File | Role |
|------|------|
| `db/schema.py`, `db/tables.py`, `db/migrations.py`, `migrations/versions/0047_workspace_ratings.py` | Four nullable columns + the partial UNIQUE, on both tracks |
| `db/evaluations.py` | `upsert_workspace_rating` (the correction semantics), `workspace_rating_tally`, `list_workspace_ratings_for_targets` |
| `client_portal/service.py` | Target visibility, the write path, the fenced prompt, the background dispatch |
| `client_portal/router.py` | `POST …/ratings`, rate-limited per (client, agent) |
| `routers/evaluations.py` | The read amendment — tallies to the agent, words to operators |
| `components/portal/PortalRating.vue` | The control + the comment box |
| `components/portal/PortalConversation.vue`, `PortalDeliverables.vue`, `PortalAgentPage.vue` | The three places it appears |
| `tests/unit/test_ent366_workspace_ratings.py`, `src/frontend/tests/unit/portalRatings.spec.js` | The rules |

## Known limits

- A reply composed locally during a live turn has no row id yet, so it becomes
  rateable on the next load rather than immediately.
- There is no un-rate: clicking the rating you already gave is a no-op, because
  clearing locally would show a state the server does not have.
- The tally is fleet-wide per agent, not per-reader — "how did this land with
  people" is the question it answers.
