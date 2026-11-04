---
name: plan-agent
description: Agree with the person what one agent is for and what good looks like, and write it as a short plan, which /build-agent then builds with them
argument-hint: "[opportunity]"
user-invocable: true
---

# Plan an agent

You find out what they want. How it's built is worked out by the create-agent wizard in `/build-agent`, with their answers. So don't design skills, integrations, variable names or schedules here, and don't tell them what those will be. Leave `skills`, `connections`, `variables` and `schedules` empty.

If this is their first agent (`fleet-state.json` lists no agent of theirs yet), open with the short paragraph about their levels from the Levels section of your instructions, then carry on.

Add the agent to `agents` with `status: "designing"` and `focus.agent` set, put the plan in plain words in `purpose`, set `stage: "plan"`, and update the world.

First check whether this already exists: the agents named in their first message, `list_templates`, `list_skills`. If it does, say so and suggest extending or reusing it.

The plan covers only this:

1. **Who it's for:** which team, and who will use it.
2. **The job:** what it does, what it starts from, and what a good result looks like.
3. **How you'll know it worked:** the number it should change, if there is one.
4. **Where the information lives:** the systems they already use, as facts. Not how to connect to them.
5. **What it may do alone:** and what a person must approve. Default to asking first for anything that sends, publishes, pays or deletes.

If the audit is done, you already know most of this, so write it from the audit and don't ask again. Otherwise ask for what's missing, one question at a time, at their level from `profile.md`. Ask no more than you need: `/build-agent` asks them the rest, including for examples.

Save it as `plans/<agent id>.md`, which is the record of what was agreed. Read it back in a few plain sentences and ask whether to build it now. On a yes, go to `/build-agent`.
