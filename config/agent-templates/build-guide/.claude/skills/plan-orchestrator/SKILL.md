---
name: plan-orchestrator
description: Design and build an orchestrator - an agent that runs the person's other agents, hands out work, checks on it and escalates only what needs them - once there are agents for it to run
user-invocable: true
---

# Plan an orchestrator

Only run this when the message says orchestrators are turned on for this instance, which is what the page's "Design one" button sends. Otherwise explain what one does and say it isn't included.

1. **Check it's worth it.** Ask which agents it should run, using what you built here and what they told you exists. With fewer than two working agents there is nothing to coordinate, so say so and finish those first.
2. **Find the real need.** Ask what falls between their agents today: work that has to be passed along by hand, things nobody notices have failed, or a morning summary they want from one place.
3. **Design it.** Add it to `agents` with `kind: "orchestrator"`, `department: "Orchestration"`, `status: "designing"`, `focus.agent` set, and `manages` listing the agents it runs. Agree what it may do alone and what comes to a person. Default to it reporting and asking, not acting.
4. **One at a time.** If they want several new agents at once, build them one at a time, each from its own plan.
5. **Build it.** Run `/build-agent`, adding to the brief: "Declare the plugins `agent-dev@abilityai` and `trinity@abilityai`, and every schedule off."
6. **Make it an orchestrator.** You can reach the agents you created. Once it exists, ask it with `chat_with_agent` to run `/agent-dev:add-orchestrator`. That installs `/discover-agents`, `/compose-system` and `/orchestrate`, and it maps the agents it can reach.
7. **Permissions.** An agent can only call agents it has been granted. Propose each grant with `/fleet` (one `allow-call` per agent in `manages`), so the person confirms it with one click. Set `status: "needs_you"` until they have, and don't describe it as running them before then.
8. Set `live_name` and `status: "ready"`, and suggest one small first job for it, with its schedules still off.
