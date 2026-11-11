---
name: build-agent
description: Build the agent you planned with the person, yourself, in this chat - the create-agent wizard headlessly, one batch of questions and one approval asked here, the agent created with every schedule off, and a first agent one level at a time (the agent with one question, then each playbook, its keys, its schedule) - and keep their world updated until it really exists
argument-hint: "[agent id]"
user-invocable: true
metadata:
  version: "0.1.0"
  adapted_from: "Ability AI's Agent Builder /build-agent 0.2.0 (template 0.2.1, 3 Nov 2026): no hosted Architect, no --fleet, questions asked in this chat"
---

# Build the agent

You build the agent yourself, with Ability AI's **create-agent** wizard (`/create-agent:custom`, headless). The person is in this chat the whole time, so everything you need from them you ask here. Whatever you create belongs to them, because they own you.

## First agent: one step at a time

When this is their first agent (`fleet-state.json` lists no agent of theirs yet), the build follows their levels, one level per step, so they see what each part is and why. Each step is one ask and one yes. Never start a step before they say yes, never do two steps in one turn, and end your turn after each step so the page can tick the level and tell you. Run Steps 0 to 4 below with these changes:

**Level 1: the agent exists.** Ask one thing only: "Level 1 is just asking me to create it. Shall I create <name> now? It won't do a job yet." On yes, create it yourself, in that same turn, **without the wizard** and without asking anything more. The wizard is for later agents: it asks its own questions and raises cards in the Inbox, and an answer to a card never reaches you here, so a first build through it stalls. Write three files in `~/builds/<slug>/`:

- `template.yaml`: `name: <slug>`, `display_name: <Name>`, `description:` one line, `resources:` with `cpu: "1"` and `memory: "2g"`, and, when the plan lists keys, `credentials:` with their names under `env_file` and a `credential_setup:` entry per key with its `name`, a `title`, a `description` of what it is and where to get it, and `required: true`. `credential_setup:` goes at the top level, beside `credentials:`, never inside it: inside, Trinity doesn't see `required: true`. Names only, never a value: Trinity then lists them as needed, so the page keeps Level 3 for them. Nothing else: no `schedules:`, no `plugins:`. The keys part looks exactly like this, with the plan's names:

```yaml
credentials:
  env_file:
    - SUPABASE_URL
    - SUPABASE_KEY
credential_setup:
  - name: SUPABASE_URL
    title: Supabase project URL
    description: The address of your Supabase project, under Project Settings, API.
    required: true
  - name: SUPABASE_KEY
    title: Supabase key
    description: The service key, under Project Settings, API, in your Supabase project.
    required: true
```

- `CLAUDE.md`: who it is and who it's for; the job in plain words, from the plan; the company (the text of `context.md`, or what you know); how to work with it (ask it in its chat; its playbooks arrive one at a time, each started with `/<name>`); and its rules (it asks before anything that sends, publishes, pays or deletes). No `.claude/skills/` yet.
- `README.md`: two lines on what it is.

The plan's playbooks are written now too, but outside the agent, one file each in `~/builds/<slug>.held/<name>/SKILL.md`, so Level 2 can add them. Its keys are named in `template.yaml` and nothing more: no values, and none asked for. No connections, no channels, no schedules: the schedule it will need stays in the plan. A question asking for a key, a token or a password is never passed on at any step.

Then create it on Trinity with `scripts/deploy-agent.sh ~/builds/<slug>`, from bash: it packs the folder with its manifest and posts it to Trinity itself, and prints Trinity's answer. Never paste an archive into a tool call (`deploy_local_agent`): its bytes get altered on the way, and Trinity refuses it as drift. Check it, don't assume: `get_agent_info(<slug>)` returns it; if it doesn't, nothing was created, so say so and run the script once more. Set its `live_name` and `status: "ready"` and its planned skills to `status: "planned"`, write the world, and tell them in two sentences that <name> now exists on their Trinity and can't do a job yet: next it gets its first playbook. End your turn there. The page tells you when Level 1 is reached.

**Level 2: its first playbook.** Say in one sentence what a playbook is: one job the agent does the same way every time, started with `/<name>`. Propose the most useful one first, saying in plain words what it will do for them. On their yes, set it `status: "building"` and write the world, then ask the agent with `chat_with_agent` to save it: "Save this as `.claude/skills/<name>/SKILL.md`, exactly as written, and change nothing else:" followed by the whole file from `~/builds/<slug>.held/`. When it confirms, set it `status: "done"`, write the world, and delete it from `~/builds/<slug>.held/`. If its keys aren't set yet, don't suggest trying it: it can't reach their tools until they are, so say it will do the job once its keys are in, at Level 3, next. Otherwise tell them to try `/<name>` in <name>'s chat. End your turn. The others follow the same way, one at a time, with an offer to add the rest together once the first is in. A playbook that needs a key says so at its top; the person adds its value at Level 3, never at Level 2.

**Every playbook that uses a key says where the key is.** Write this line into the playbook, with its key names: "Needs the keys <NAMES>. They are environment variables in your own container (Trinity writes them into your `~/.env`): read one as `$NAME` in a command, never with `fetch_credential` or any other Trinity credential tool, which look in a different store and will say the key is missing." Put the same line in the agent's own `CLAUDE.md`.

**Look at the data before a playbook relies on it.** Once the agent's keys pass the test, and before any later playbook for a service its map doesn't cover, have the agent take one read-only look and save `data-map.md`, with the exact message in your `CLAUDE.md` ("Look at the data before a playbook relies on it"). Write playbooks from that map's real names, and start each one that reads data with: "Read `~/data-map.md` first and use its names. If what you find no longer matches the map, do the job from what you find, then correct the map." A first agent's playbook is written before its keys exist: correct it from the map right after the test passes, and say you did.

**Level 3: its keys.** Its keys were named when it was created. Check with `get_credential_status` which it still needs. If a playbook uses one that isn't named, ask the agent with `chat_with_agent` to add it to its own `template.yaml`, changing nothing else, as Level 1 names them. Then say which keys it needs and, before any walk, where to find its value for each. Look it up first, every time: menus change, and what you remember is often out of date. Use `WebSearch` to find the service's own help page for that key (its own site only, such as supabase.com/docs or developers.facebook.com, never a blog or a forum), read it with `WebFetch`, and take the steps from that page. Then write one short block per key, each in its own paragraph: what the key is in one plain sentence, the steps to reach it as two to four numbered steps in the page's own menu names, and the page as a link they can click. If you can't find the service's own page, say so: don't invent a menu name. If someone else at their company holds a key, say who to ask. They copy the values there and type them in Trinity, never in this chat. For example: "Game Filler needs three keys. Here is where to find each. **Supabase URL and secret key**: 1. Open your project in Supabase. 2. Go to Settings, then API Keys. 3. Copy the project URL, and the secret key (it starts with sb_secret_). Guide: [Supabase: API keys](https://supabase.com/docs/guides/api/api-keys). **WhatsApp access token**: 1. Open your app at developers.facebook.com/apps. 2. Open WhatsApp, then API Setup. 3. Click Generate access token. That one is temporary: the guide shows how to make a permanent one. Guide: [WhatsApp Cloud API: get started](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started). Keep those pages open in another tab. Shall I walk you through it?" End with the offer of the walk in plain words, exactly: "Shall I walk you through it?" (never name `/fleet` or any other tool of yours to them). On their yes, propose Show me `credentials` for it through `/fleet`, naming those keys, so the page walks them to where each one goes. When they come back and say it's done, check with `get_credential_status` that every key is set, then test the connections before anything else. Ask <name> with `chat_with_agent`, sending exactly this and nothing else: "Test your connections, changing nothing: for each key you have, make one read-only request to its service (for example read one row, or fetch your own account details). Reply with one line per service: worked, or failed with the status code and the error message. Never print a key's value. End with one last line, exactly `CONNECTIONS: OK` if every service worked, or `CONNECTIONS: FAILED` if any did not." The page reads that run itself and ticks Level 3 only when its reply ends with `CONNECTIONS: OK`: nothing you write on the world and nothing you say can tick it. If the reply ends with `CONNECTIONS: OK`, tell them, in one sentence, that each connection answered. Never say the connections work, that <name> is connected, or that Level 3 is done without that reply in front of you: keys that are set are not keys that work, and made-up values are set too. If one failed, say which one and what the error usually means in plain words (401 or 403: the key is wrong, or lacks permission for this; 404: the address or the project is wrong; a timeout: the address can't be reached), give that key's block again, looked up fresh, with the guide's link and its steps (never a bare list of key names), and offer the walk again: "Shall I walk you through it?". Test again when they come back. If the values are placeholders (they say so, or the reply shows it), say in one sentence that Connected stays open until real keys pass, give each key's block with its guide so they can get the real ones, and ask whether to carry on with the next level meanwhile; only on their yes, write `keys_later: true` on the world, so the page shows Connected as "Later" (not a tick) and points to the next level. Don't quote `CONNECTIONS: OK` or `CONNECTIONS: FAILED` to them: those lines are for the page. When it needs no key, Level 3 is still a step: say in one sentence that <name> needs no key, so there is nothing to connect, and ask whether to move on. End your turn. On their yes, write `keys_not_needed: true` on the world (the page then shows Level 3 as not needed) and go on to Level 4.

**Level 4: in control.** Walk them to sharing it with someone (Show me `people`) or to letting it call another of their agents (Show me `permissions`), through `/fleet`.

**Level 5: on a schedule.** Create the schedule from the plan with `create_agent_schedule`, off, and say what it would do and when. Then walk them to turning it on (Show me `schedules`, naming it) through `/fleet`; the walk ends on Autonomy. The page ticks Level 5 once the schedule and Autonomy are both on: they don't wait for the first run.

**When its steps are done.** Once its schedule is on (the page says `Level reached: Level 5`, or they tell you they turned it on), its first build is finished. Reply in three short parts, in this order. First, what <name> now does by itself, and when. Second, how to try it now, as a route they can follow: click **<name>** under Agents, on the left, then type `/<playbook>` in its chat and press Enter, naming its real playbook; if a key still holds a test value, say it needs the real value first. Third, what comes after, with no push: say there is nothing more they have to do now, and that when they are ready there are two ways to go further. One is more jobs for <name>: an agent is like an employee, so it holds many playbooks, not one; name one more job from their plan or audit it could take on. The other is another agent, only when the job is one they would give to a different person. Say the later levels are about agents working as a team and wait for them. Don't name what Level 6 needs, and don't end by asking to plan the next agent. Never say "you're done", and never say they have reached all their levels: there are eight levels and the page counts them. For example: "Game Filler now checks for games every morning at 8:00 and drafts the invitations for you to approve. Try it now: click **game-filler** under Agents, on the left, then type `/draft-invitations` in its chat and press Enter. That's your first agent built, and there is nothing more you have to do now. When you're ready, there are two ways to go further, and I'm here for either. You can give Game Filler more jobs: an agent is like an employee, so it can hold many playbooks, for example one that chases players who haven't replied. Or you can build another agent, when the job is one you'd give to a different person. The later levels are about agents working as a team, and they'll be there whenever you get to them."

Never add a playbook to it in the same turn you create it: the page has to see the agent once without them. While `~/builds/<slug>.held/` still has playbooks, the steps aren't finished: carry on with the next one when they come back to it. A build left waiting on wizard questions (`~/builds/<slug>.pending.md`) for a first agent doesn't wait: settle them as above and ask the Level 1 question. If the agent was created with its playbooks or keys anyway, say so plainly, set them `done`, and carry on from the next level.

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

Invoke `/create-agent:custom` with the brief (Skill tool). The wizard stops twice, for **one batch of questions** about real gaps in the brief and for **one approval** before it creates anything. Both times, ask the person **in this chat, in your own words**: the questions together in one message, then the approval in one message that says what will be created and that its schedules start off. Never call `ask_operator` or `AskUserQuestion`, even where the wizard's steps say to: those raise a card in the Inbox, and an answer to that card never reaches you here, so the build stalls with the person waiting. Never ask more than those two times: small choices take sensible defaults. A question asking for a key, a token or a password is never passed on: answer it yourself with "set in Trinity later". End your turn after asking; their answer resumes the build here (Step 0).

Every schedule is created `enabled: false`. A person turns them on once they trust the agent.

**Then create it on Trinity.** The wizard only writes a folder, `~/builds/<slug>`. Nothing exists on Trinity yet, whatever its closing message says: "created", "exists locally", "try it locally first" and "deploy with /trinity:onboard" are about that folder. Never pass them on and never follow them. In the same turn, without asking anything more:

- **No-token path (the usual one).** Without a `GITHUB_TOKEN` credential, the agent is uploaded rather than given a repository: run `scripts/deploy-agent.sh ~/builds/<slug>` from bash. It writes the `.trinity-manifest.json` the platform checks, packs the folder and posts it to Trinity itself, and prints Trinity's answer. Never paste an archive into `deploy_local_agent`: the bytes get altered on the way. Say plainly, later, that it has no repository of its own.
- **With a `GITHUB_TOKEN`**, the wizard pushes the repository, and you create the agent from it.

## Step 4: Close

An agent is created when Trinity has it, not when a folder exists. So, once `deploy-agent.sh` (or the create from the repository) has answered:

1. Check, don't assume: `get_agent_info(<name>)` returns it, and `list_agent_schedules(<name>)` shows every schedule `enabled: false`. If it doesn't return it, nothing was created: say so, and go back to Step 3.
2. Set the agent's `live_name` to its real name, `status: "ready"`, and fill in what was built (`skills`, `connections`, `variables`, `schedules`), then write the world. The page checks `live_name` against the person's real agents.
3. Delete `~/builds/.build-<slug>.json`.
4. Tell them what exists now, and the one thing to do next: try it, add the values for its keys in Trinity, and turn on any schedules once they trust it. If it needs keys, don't describe where they go: say who to ask or where to find each value, then offer to show them where to put it, which is a `credentials` Show me in `/fleet` naming the keys it needs now. Then offer: "Want me to set who it can work with, or who can use it?", which is `/fleet`.
5. When they've tried it, set `status: "live"` and `stage: "live"`.

## Errors

| Situation | Do |
|---|---|
| `create-agent` plugin missing | Say the wizard isn't installed on you yet and that restarting you installs it; set `status: "failed"` |
| A card appeared in the Inbox (the wizard asked or sought approval through `ask_operator`) | Never wait on it: nothing comes back from it. Say in the chat, in your words, what it asked, and end your turn; on their answer resume from Step 0 and settle it yourself. For a first agent, drop the wizard and create it as Level 1 says |
| Creating the agent is refused for permission | Say an administrator needs to turn on "Manage other agents" for you, under your Settings, Guardrails, Permissions; keep the build file so it resumes |
| They decline the approval | Keep the work in `~/builds/<slug>`, say where it is, delete the build file, set `status: "designing"` |
| `deploy-agent.sh` refused (`MANIFEST_DRIFT`, size) | Run it once more; refused again, say what Trinity said, in plain words. Never retype or paste the archive yourself |
| The wizard ends with "exists locally", "try it locally" or "/trinity:onboard" | That's the folder, not an agent. Pack and create it on Trinity (Step 3), then check it (Step 4). Never tell them it was created before `get_agent_info` returns it |
| Anything else fails twice | Set `status: "failed"`, say what happened, and offer to bring in the team, as your rules set out |

Don't report success on anything you haven't confirmed.
