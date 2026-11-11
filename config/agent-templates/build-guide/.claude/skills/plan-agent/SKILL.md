---
name: plan-agent
description: Agree with the person what one agent is for and what good looks like, and write it as a short plan, which /build-agent then builds with them
argument-hint: "[opportunity]"
user-invocable: true
---

# Plan an agent

You find out what they want. How it's built is worked out by the create-agent wizard in `/build-agent`, with their answers. So don't design skills, integrations, variable names or schedules here, and don't tell them what those will be. Leave `skills`, `connections`, `variables` and `schedules` empty.

If this is their first agent (`fleet-state.json` lists no agent of theirs yet), write the world first (the next paragraph) and only then open with the short paragraph about their levels from the Levels section of your instructions: the page lights their levels up when it sees the build start, and your words say it is doing so now.

Add the agent to `agents` with `status: "designing"` and `focus.agent` set, put the plan in plain words in `purpose`, set `stage: "plan"`, and update the world.

First check whether this already exists: the agents named in their first message, `list_templates`, `list_skills`. If it does, say so and suggest extending or reusing it.

The plan covers only this:

1. **Who it's for:** which team, and who will use it.
2. **The job:** what it does, what it starts from, and what a good result looks like.
3. **How you'll know it worked:** the number it should change, if there is one.
4. **Where the information lives:** the systems they already use, as facts. Not how to connect to them.
5. **What it may do alone:** and what a person must approve. Default to asking first for anything that sends, publishes, pays or deletes.
6. **What it will need later, as facts:** which keys the job needs (names only), and whether it should run on a schedule. For a first agent these are built as Levels 3 and 5, not at creation, so note them here, don't ask for anything, and when you read the plan back say they are asked for at that level, not now.

If the audit is done, you already know most of this, so write it from the audit and don't ask again. If they gave you a brief and asked to skip the audit, or said they're testing, write it from the brief and ask nothing: fill a gap with the plainest default (keys set in Trinity later, input given by them each time, drafts for anything that sends) and say in one line which defaults you took. Otherwise ask for what's missing, one question at a time, at their level from `profile.md`. Ask no more than you need: `/build-agent` asks them the rest, including for examples.

Save it as `plans/<agent id>.md`, which is the record of what was agreed. Read it back in a few plain sentences and ask whether to build it now.

For a first agent, read it back as the steps you'll take together, one level each, so they can see what comes when and what is needed from them at each: Level 1, you create <name> now, with no keys and no jobs yet, and nothing needed from them; Level 2, its first playbook; Level 3, the keys it needs, named here and asked for then, in Trinity, never now; Level 5, its schedule, if it has one. Then the Level 1 question, "Shall I create <name> now? It won't do a job yet"; on the fast path it comes in the same turn as the plan. For example:

> Here's how we'll go, one level at a time:
> 1. **Level 1, now.** I create Game Filler. No keys, no jobs yet, nothing needed from you.
> 2. **Level 2.** Its first playbook: finding the players who can play on a date and at a place.
> 3. **Level 3.** The keys it needs. It will need your Supabase key and WhatsApp Business access. I'll ask for those at that step, and you'll add them in Trinity, not here.
> 4. **Level 5.** A schedule, if you want it to run on its own.
>
> Shall I create Game Filler now? It won't do a job yet.

On a yes, go to `/build-agent`.
