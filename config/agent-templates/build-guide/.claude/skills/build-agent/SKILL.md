---
name: build-agent
description: Build the agent you planned with the person, yourself, in this chat - the create-agent wizard headlessly, one batch of questions and one approval asked here, the agent created with every schedule off, and a first agent one step at a time (the agent, then each playbook) - and keep their world updated until it really exists
argument-hint: "[agent id]"
user-invocable: true
metadata:
  version: "0.1.0"
  adapted_from: "Ability AI's Agent Builder /build-agent 0.2.0 (template 0.2.1, 3 Nov 2026): no hosted Architect, no --fleet, questions asked in this chat"
---

# Build the agent

You build the agent yourself, with Ability AI's **create-agent** wizard (`/create-agent:custom`, headless). The person is in this chat the whole time, so everything you need from them you ask here. Whatever you create belongs to them, because they own you.

## First agent: one step at a time

When this is their first agent (`fleet-state.json` lists no agent of theirs yet), build it in steps that follow their levels, so they see what each part is and why. Run Steps 1 to 4 as below, with these changes:

1. **The agent first, without its jobs.** Before the agent is created (before you pack it, or before the wizard pushes it), move each playbook from the plan out of the agent folder into `~/builds/<slug>.held/<name>/SKILL.md`, keeping the wizard's standard ones in place. Create it and check it (Step 4, point 1). Set its planned skills to `status: "planned"`, its `live_name` and `status: "ready"`, and write the world. Then tell them in two sentences that <agent> now exists on their Trinity, and that it can't do a job yet: next it gets its first playbook. End your turn there. The page then tells you Level 1 is reached.
2. **Its first playbook.** Say in one sentence what a playbook is: one job the agent does the same way every time, started with `/<name>`. Propose the most useful one first, saying in plain words what it will do for them. On their yes, set it `status: "building"` and write the world, then ask the agent with `chat_with_agent` to save it: "Save this as `.claude/skills/<name>/SKILL.md`, exactly as written, and change nothing else:" followed by the whole file from `~/builds/<slug>.held/`. When it confirms, set it `status: "done"`, write the world, delete it from `~/builds/<slug>.held/`, and tell them to try `/<name>` in <agent>'s chat. End your turn. The page tells you when Level 2 is reached.
3. **The others, one at a time.** Propose the next one the same way, saying what it adds, and offer to add the rest together if they'd rather move on.
4. **Then its keys, and later its schedules,** as in Step 4, point 4. They are Levels 3 and 5.

Never start a step before they say yes, and never add a playbook to it in the same turn you create it: the page has to see the agent once without them. While `~/builds/<slug>.held/` still has playbooks, these steps aren't finished: carry on with the next one when they come back to it.

If the agent was created with its playbooks anyway (the wizard created it before you could set them aside), say so plainly, set them `done`, and carry on from its keys.

For any later agent, build it all at once, as below, unless they ask to go step by step.

## Step 0: Resume or start

Take `<slug>` from the agent id (kebab-case, at most 30 characters). If `~/builds/.build-<slug>.json` exists, this is a resumed build (the wizard was waiting on their answers or their approval): read it and go straight to **Step 3**.

Otherwise write `~/builds/.build-<slug>.json` = `{"plan": "plans/<agent id>.md", "started_at": "<now>"}`, set the agent's `status: "building"`, `focus.agent` and `stage: "build"` on the `build` canvas, and write the whole document.

## Step 1: Compose the wizard's brief

Write it to `~/builds/<slug>.brief.md`. It knows nothing about this person or company except what you put in it:

- **The agent:** the plan from `plans/<agent id>.md`: who it's for, the job, what a good result looks like, the target, where the information lives, and what it may do alone.
- **The company:** the text of `context.md`, what's relevant from `company-research.md`, or what you know if neither is written yet. The new agent's instructions should say where it works and use the company's words.
- **Why this agent:** the audit's finding behind it and what it should save them. If the audit was skipped, say it's a test agent and keep it small.
- One line at the end: `Requested as: <the plan's one-sentence purpose>`.

## Step 2: How you talk to them while it builds

From `profile.md`. For a `newcomer` or `practitioner`: every question and approval in plain language, saying what it means for them, with no file paths, tool names or words like scaffold, repo or deploy. For a `builder`: direct and technical.

## Step 3: Run the wizard

Invoke `/create-agent:custom` with the brief (Skill tool). The wizard stops twice, for **one batch of questions** about real gaps in the brief and for **one approval** before it creates anything. Both times, ask the person **in this chat**: the questions together in one message, then the approval in one message that says what will be created and that its schedules start off. Never use `ask_operator`, and never ask more than those two times: small choices take sensible defaults. End your turn after asking; their answer resumes the build here.

Every schedule is created `enabled: false`. A person turns them on once they trust the agent.

**Packing (no-token path).** Without a `GITHUB_TOKEN` credential, the agent is uploaded rather than given a repository: build the archive with `scripts/pack-agent.sh <agent folder>` (it writes the `.trinity-manifest.json` the platform checks and prints the base64) and create it with `deploy_local_agent`. Say plainly that it has no repository of its own.

## Step 4: Close

When the wizard reports the agent created:

1. Check, don't assume: `get_agent_info(<name>)` returns it, and `list_agent_schedules(<name>)` shows every schedule `enabled: false`.
2. Set the agent's `live_name` to its real name, `status: "ready"`, and fill in what was built (`skills`, `connections`, `variables`, `schedules`), then write the world. The page checks `live_name` against the person's real agents.
3. Delete `~/builds/.build-<slug>.json`.
4. Tell them what exists now, and the one thing to do next: try it, add the values for its keys in Trinity, and turn on any schedules once they trust it. If it needs keys, don't describe where they go: say who to ask or where to find each value, then offer to show them where to put it, which is a `credentials` Show me in `/fleet` naming the keys it needs now. Then offer: "Want me to set who it can work with, or who can use it?", which is `/fleet`.
5. When they've tried it, set `status: "live"` and `stage: "live"`.

## Errors

| Situation | Do |
|---|---|
| `create-agent` plugin missing | Say the wizard isn't installed on you yet and that restarting you installs it; set `status: "failed"` |
| Creating the agent is refused for permission | Say an administrator needs to turn on "Manage other agents" for you, under your Settings, Guardrails, Permissions; keep the build file so it resumes |
| They decline the approval | Keep the work in `~/builds/<slug>`, say where it is, delete the build file, set `status: "designing"` |
| `deploy_local_agent` refused (`MANIFEST_DRIFT`, size) | Pack once more from a fresh copy; refused again, say what Trinity said, in plain words |
| Anything else fails twice | Set `status: "failed"`, say what happened, and offer to bring in the team, as your rules set out |

Don't report success on anything you haven't confirmed.
