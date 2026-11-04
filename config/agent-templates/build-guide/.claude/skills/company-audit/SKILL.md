---
name: company-audit
description: The AI audit - a conversation with one person at a company to find where agents would help most, where the data lives and in what format, recommend their first agent, and write context.md with the company's terminology
user-invocable: true
---

# AI audit

Read `PLAYBOOK.md` in this skill's folder first, and follow its stages in order. They are what makes this an audit rather than a chat. Set `stage: "audit"` and `audit.status: "in_progress"`, and update the world after each answer so the person watches the picture form. Stop whenever they want; what you have is kept and they can come back.

## Talk at their level

From `profile.md`, as the playbook sets out. Never name the frameworks to a `newcomer`.

## What's already here

The first message lists the agents already on this instance, and you can ask about any since. Existing agents are part of the picture: don't suggest what one already does. If they run agents elsewhere (n8n, LangChain, scripts) and want those assessed, offer `/agent-dev:agent-fleet-analysis`.

## The company research

It's probably still running beside this chat, so don't wait for it. As soon as `company-research.md` appears, tell them in two or three sentences what it found, ask what's wrong, and put it in `company` and `departments`. Then ask only for what it didn't answer. Use its "where agents might help" ideas as things to test with them, not as findings.

## Recording it

- Add each department to `departments` as you hear it, but only ask about departments this person can speak for. Note which others need someone else, and say so.
- Each candidate goes in its department's `opportunities`, with `why` (including the numbers from stage 4 and the fit from stage 6), `effort` and `value`. Exactly one is `recommended: true`.
- Each data source goes in `data_sources`, with `access` set to what you actually know.

## Write context.md

Write what any agent at this company needs to know into `context.md` in your workspace, and put the same full text in `context_md` on the world:

- What the company does, in their words, using the research where they agreed with it
- Terminology: the words they use and what they mean, especially words that mean something different here than elsewhere
- Teams and who does what, by role rather than by name
- Systems and what each is the source of truth for
- Rules agents must follow: what must never be sent or changed without a person

Point them at the Context tab and ask what's wrong. Correct it until they agree. Don't write personal details about individuals.

## Close

Set `audit.status: "done"`, `profile_complete: true`, `stage: "plan"`, and write `audit.summary` in two or three sentences, including how you'll know the first agent worked. Update `profile.md` with the recommendation and its target number.

Give the recommendation from stage 9 of the playbook. If they agree, go to `/plan-agent`, which already knows what you found. If they want a different one, take theirs. If they stopped early, say what you didn't cover.
