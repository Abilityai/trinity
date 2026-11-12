# Workspace Projects

A **project** is the record of a piece of sustained work: its goal, the people on it, the agents that may work on it, its tasks and a shared log — linked to the chats and rooms where the work actually happens. You keep talking to agents in ordinary Workspace chats; a project is what ties those chats together and keeps what came out of them.

Projects appear on instances licensed for them. On an instance without the entitlement there is no **Projects** link in the sidebar, no Projects tab in the rail, and no project controls in a chat header; everything else in the [Workspace](workspace.md) works as described there. The agent-side skills still run — see [Platform mode](../abilities/agent-dev-plugin.md) on the agent-dev plugin page for what they do with and without the platform.

## Concepts

- **Project** — A name, a goal, a state (**Not started**, **Running**, **Stopped**, **Done**), a mode (**Track**, **Propose**, **Auto**), a tracker link, members, agents, tasks and a log. Opened from **Projects** in the sidebar or from the badge in a linked chat's header.
- **Member** — A person at your company on the project. The **creator** is the first member and the only one who changes the project's name, goal, steward, members and visibility, starts and stops it, and sets its mode.
- **Agent on the project** — An agent that may work on it. Adding an agent you do not own asks its owner first; until they approve, the agent is listed as *Waiting for owner* and is not offered.
- **Linked chat** — A Workspace chat (or room) that belongs to the project. Every turn in it tells the agent which project it is in.
- **Log** — The project's shared, append-only record: decisions, deliverables, task changes, blockers, hand-offs and notes, written by members and by agents.
- **Task** — A unit of work with an owner or an agent, a priority, an objective and a definition of done. The owner is the project's creator or one of its members (or, on a project an agent stewards, a canon role written `role:<id>`); Trinity refuses anyone else when an owner is set or changed, from the page, the API or an agent. Owners written before this rule, or by a folder **Import**, are kept as they are, and the project page warns when one reaches nobody. A new task starts as *to do*; its status moves through *to do*, *active*, *blocked*, *needs a decision*, *paused*, *awaiting verification* and *done*. A task can be a **subtask** of another, can **wait on** other tasks, and can be **for a person** (see [Subtasks, waiting and tasks for people](#subtasks-waiting-and-tasks-for-people)).
- **Steward** — The person or agent who owns the project's health, verifies finished tasks, starts tasks' runs when the mode lets it, and gets the digest of what needs attention. The creator stewards a project unless they name someone else.
- **Mode** — Whether agents may start work on the project: **Track** (a person starts every run), **Propose** (the steward proposes runs, a person gives each one its go) or **Auto** (the steward starts runs). Only a person sets it. See [Starting and stopping a project](#starting-and-stopping-a-project).
- **Health** — The steward's read of the project — **On track**, **At risk** or **Off track** — with a line of explanation, and a note when it is overdue.
- **Guest** — An outside client invited to the project. They see their own chats and what was shared with them, nothing else.

## How It Works

### Creating a project

From the **Projects** list, **New project** asks for a name and a goal, who the steward is (you, someone else at the company, or an agent you can use), an optional tracker link, visibility, and the agents that may work on it. The goal is written in markdown, so bold, lists and links render on the project page. **Import** brings in a project an agent already manages as a folder — one import per folder; after that the platform is the project's home.

From a chat, the header's **Project** button offers two ways in: **Add this chat to a project** the chat's agent is already on, or **Make this chat a new project** — the same form, with this chat linked and its agent added as you save. A room can be added to a project but not turned into one. The pinned **Main** chat never belongs to a project; start a new chat for it.

An agent can also start a project for its owner from its own `/project-init` skill, once an admin has granted the agent the **Start projects** permission — see [Platform mode](../abilities/agent-dev-plugin.md). The owner is the creator and first member; nobody else gains access.

### The project page

The page opens on **Overview**, with **Tasks**, **Log** and **Files & reports** as tabs. The header carries the name, the state (with who changed it and when), visibility, the mode and the goal; the creator gets **Start project** or **Stop project**, **Mark done**, the mode selector, **Edit** and **Archive**, and every member gets **Members & access**. Everyone else sees the mode as a badge.

The Overview is two columns — a wide main column and a narrow side column — that keep their places however much each card holds:

| Column | Cards |
|--------|-------|
| **Main** | **Continue** (below) · **Where it stands** — tasks by status and what needs attention, each line opening Tasks · **Waiting for your go** — in Propose and Auto, the runs the steward proposed · **Work on it** — every agent on the project and what it offers you · **Metrics** — numbers the agents record that members chose to show · **My chats on this project** |
| **Side** | **Steward** (below) · **Health** · **Needs you** — your own open asks on this project, answered in place · **Members** · **Agents** and the rooms you are in |

On a phone the cards form one column in that order.

### Continuing work

Work on a project happens in chats. **Continue**, at the top of the Overview, is the way back in:

- **Continue with *agent*** reopens your newest chat on the project with that agent.
- **Start with *agent*** — when you have no chat on the project yet — starts a new chat with that agent and links it to the project before it opens.
- The other agents you can chat with are one click away beside it; **Work on it** below lists every agent on the project, including the ones you cannot use yet and why (its owner has not approved it, it is not on your list, or the project is archived). When none of the project's agents is available to you yet, the card says so and points at **Work on it**.

Under **Continue**, a one-time line says how a project moves — chats linked to it build it up, agents record decisions, deliverables and blockers in the **Log**, and the steward checks on it. **Got it** dismisses it for this browser.

Chats are private. **My chats on this project** lists only yours; other members' chats appear as a count. Rooms you are in are linked from the side column.

A linked chat shows the project's name as a badge in its header, with **Wrap up** and **Detach** beside it. Detaching removes the link and nothing else.

### What the agent does with the project

Every turn in a linked chat carries one line that names the project and tells the agent to read it first: the goal, the recent log and the open tasks. From there the agent works as it would in any chat — and records outcomes as it goes. Each decision, deliverable, task change, blocker or hand-off becomes one entry in the project's **Log**, written through the agent's project tools, and the agent keeps the task list current: it creates tasks, adds notes, and moves a task it has finished to **Awaiting verification** with its evidence.

**Wrap up**, in a linked chat's header, sends the agent a fixed request to do that for the whole conversation — one log entry per meaningful outcome, tasks updated to match, discussion skipped — and then tell you what it recorded. The request is visible in the chat, and the entries are the agent's own.

### Tasks and the verification rule

Tasks are grouped **Open**, **Awaiting verification** and **Done**. Members add tasks with **New task** and change them by opening one; agents do the same through their tools.

An agent cannot mark a task done. When it finishes, it moves the task to **Awaiting verification** and leaves its evidence in the task's log. A person — or the steward, when the steward is an agent — confirms it: open the task to read the evidence, then **Confirm** on its row or set the status to **Done**. A done task can only be reopened, and only by a person. Task **priority** is set by people only.

Every row and every open task names **who created it** (a person, or an agent marked *(agent)*) and **who it is for**: the person who owns it and the agent doing it.

### Subtasks, waiting and tasks for people

- **Subtasks.** Set **Subtask of** to another task's id (like `T-001`) to put a task under it. Subtasks go one level deep: a subtask can't have subtasks, and a task that has subtasks can't become one. The parent's row shows its progress, e.g. *2/5 subtasks*. **Finishing every subtask does not finish the parent** — mark the parent done yourself. Clear **Subtask of** to make it a top-level task again.
- **Waiting on other tasks.** **Waits on** lists the tasks that must be done first, by id (`T-002, T-004`). While any of them is open, the row says *Waiting on T-002*. Waiting is worked out from those tasks; it is not the **Blocked** status, which you set by hand for something outside the project. A link that would make a circle is refused, naming it — for example *That link would make a circle: T-004 → T-007 → T-004.* A task and its own subtasks don't wait on each other, and a task can wait on at most 20 tasks.
- **What's next.** Agents can ask for the tasks that can be worked now: *to do* or *active*, waiting on nothing open, and not a parent with open subtasks, by priority.
- **Tasks for people.** Turn on **A person does this task** to make a task a person's: it needs an owner on the project and no agent. It is never started as an agent run — the task offers no **Run this task** — and only a person marks it done. When the last task it waits on is done, its owner is told, once each time it becomes ready. As an agent, only the project's steward can give a task to a person; any other agent asks with `propose_project_changes`.

### Held tasks and the two-rejection rule

- **Needs my go.** Turn it on in a task to hold the task for a person: when the steward starts it, in any mode, the run waits for a person's go instead. Any member who can work on tasks sets or clears it; an agent can't. A held task says **Needs a go** on its row.
- **Two rejections.** When the steward sends finished work back from **Awaiting verification** a second time, the task moves to **Needs a decision** and waits for a person. The task says it was rejected twice and gives the steward's last reason; an agent steward must give one. While it waits, no agent moves it or starts it. A person's decision — any status change — clears it and starts the count again. Only the steward's send-backs count.
- **The steward never verifies its own work.** It can't mark done a task whose run it was the agent of, or whose done claim it wrote; a person or another verifier checks it.

### The steward

The steward owns the project's health and the verification of finished work, and is the one place a project's "what needs attention" is collected:

- **Health** — the steward (or the creator) sets **On track**, **At risk** or **Off track** with a short line; the Overview marks it **Update due** when the last one is more than two weeks old.
- **The digest** — what needs the steward on each project they steward: tasks to verify, blocked or undecided tasks, tasks untouched for a week, open asks, a health update due, a project gone quiet. For a person it is the **I steward** tab on the Projects list; for an agent it is the `get_steward_digest` tool.

The **Steward** card on the Overview shows who it is, the project's health as a badge, and the state behind that. With nobody named it reads *The creator stewards it until someone is named.* For an agent steward: the last sweep (the last time it read its digest), its next scheduled run when it has one, the project's health and whether an update is due. It says **No sweep recorded yet** and **No sweep scheduled** plainly when that is the case, and **Scheduled** without a time when the schedule belongs to an agent you cannot read. For a person steward: the project's health and what is waiting on them, with **Open I steward** when the steward is you.

**Being on a project does not give you its steward agent.** To chat with an agent steward, or to get its questions, you need access to that agent itself, the same access its owner gives on the agent's **Channels** settings. When you don't have it, the Steward card says **You can't use {agent} yet**, and **Ask for access** sends the agent's owner an access request. The card then reads **Access requested** until they decide. The agent's owner sees, on the same card, which members can't use it, marked **Asked** where they have asked, and gives access with **Give access**. If the owner declines, the card says so and **Ask again** sends a new request. Only members get this: a guest, or someone viewing a company-visible project they are not on, sees no access block.

**An agent steward gets the project-management skills.** When you pick an agent as steward, in **New project** or later in **Edit project**, Trinity also puts it on the project and gives it the library's project-management skills, the ones it uses to verify finished tasks, record health and write the log. The line under **Steward** says what will happen before you save: the agent gets the skills, it already has them, only its owner or an admin can give them (you can still make it the steward), or the skills aren't available on this Trinity yet. The project is created in every case, and the **Steward** card keeps saying whether the agent has the skills. Changing the steward later leaves the old one's skills alone. A person steward works too; the card then says what having no steward agent means.

**An agent steward needs its own schedule.** Trinity does not run the steward; the platform only answers its questions. The agent-dev plugin's `/project-steward` skill is the sweep: on each run it reads the digest, verifies pending claims, dispatches labeled work, escalates stalls and open loops as asks, records the project's health with `set_project_health`, and writes one log entry per outcome. Give the steward agent a schedule for it (the skill ships a default weekday one) — without it, nothing moves between your chats, and the Steward card says so. The skills are described on the [agent-dev plugin page](../abilities/agent-dev-plugin.md).

### Starting and stopping a project

A goal and a steward do not start a project: a person does. A new project begins **Not started**.

| State | What it means for agents |
|-------|--------------------------|
| **Not started** | No task's run starts. The steward may write the plan (tasks, notes, the log) before you press **Start project**; other agents' turns with no person in them can't write to it. |
| **Running** | Runs start as the mode allows. |
| **Stopped** | A person pressed **Stop project**. Nothing new starts — no task's run, no approval of a proposed run — and pending proposals are withdrawn. Runs already under way finish; **Stop project** asks first and names them. A banner on the project says it is stopped. |
| **Done** | Like stopped. **Reopen project** starts it again. |
| **Archived** | Read-only. **Restore** brings it back stopped, so it never restarts work by itself. |

The same rule holds for an agent working on its own: a turn with no person in it (a schedule, a reminder) writes to a project only while it is running, except a run finishing its own task and the steward's plan before the start. A turn a person is in is that person's act and is not refused.

**The mode** decides who starts a task's run while the project is running. Only a person with edit rights sets it; a steward change, a create, an import or an agent never changes it.

- **Track** (the default) — a person starts every run from the task. The steward keeps the record but starts nothing.
- **Propose** — the steward proposes a run on an agent it names. The proposal waits for a person's go: it reaches the task's person as an approval in their **Inbox** (when the platform's system agent is running), and it is listed under **Waiting for your go** on the Overview. Tick the runs, then **Approve selected** or **Decline** (with an optional note). Each one is decided on its own, and the card says what happened to each: **Started**, or why not.
- **Auto** — the steward starts runs itself, except on a task held for a person's go. The platform never picks the task or the agent; the steward names both.

Lowering the mode to Track withdraws pending proposals; going from Auto to Propose keeps them. Runs already under way finish either way. A run belongs to a person: the one in the steward's conversation, the person who approved it, or — in a turn with no person — the person who turned Auto on. If that person can no longer start that agent on the project, the run is refused rather than handed to someone else.

**The steward's own schedule is not a platform wake.** An agent steward's schedules, reminders and loops are its own and span all its projects. On a project that isn't running, such a turn can't start a run and can't write to the project (apart from the steward's plan before the start). Trinity itself does not wake the steward for a project that isn't running.

### How a project moves forward

1. **Open it.** The Overview says where it stands, what needs attention, and who stewards it.
2. **Continue with an agent.** The chat opens linked to the project; the agent reads the goal, the log and the open tasks before it answers.
3. **The agent records what it does.** Decisions, deliverables and blockers land in the **Log**; finished work moves to **Awaiting verification**. **Wrap up** when a conversation ends.
4. **The steward checks on it.** A person confirms tasks from the Tasks tab and sets health from the Overview; an agent steward does both on its schedule.
5. **Everyone sees the result.** The Log is the shared record; **Where it stands** rolls the tasks up; the Projects list shows each project's health and attention count.

### Running a task from the project

Open a task and use **Run this task** to start its work from the project itself:

- **Pick the agent.** You're offered the agents working on the project that you can use. An agent on the project that you can't use yet is named, with what to do (ask its owner for access). With none at all, add one in **Members & access** first.
- **Preview what it gets.** This is the exact first message the agent will receive: the task's objective and definition of done, the context marked for agents (it reads each item itself), and the last few log entries.
- **Start** opens a chat with that agent, tied to the task. The first message is sent once, even if you press Start twice. Starting again with the same agent reopens the same chat, and the task lists **Chat for this task** so you can get back to it. The run uses the agent's own Claude subscription or API key, and its cost shows on the turn when it's done.

Only members and the creator can start a run. Picking the agent also sets the task's **Agent**, and a *to do* task moves to *active*. A task for a person can't be started.

On a project that is not started, stopped or done, **Run this task** says why in place of **Start**; the task's chats stay open. A task with a run under way names it — **Run R-… on {agent}: Running** — and **Start** is refused with a run already under way. A refused **Start** always says why, by name.

### Files, reports and decisions

**Files & reports** gathers material on the project: a file, a report or a recorded decision you can already see from one of the project's agents, added with **Add from…**. Markdown files render in place. The creator can share a file or report with the project's guests.

### Context for agents

A brief often needs more room than a task's fields: a call transcript, your view, the background. Put it on the project instead of preparing it somewhere else. On **Files & reports**, **Write a brief** opens a markdown editor. **Upload** takes a `.md` or `.txt` file and opens it in the same editor, so you can check it and mark it before it's saved. A task's detail has the same two buttons, for a brief about that task only. No agent is needed: the project stores the brief itself.

- **Agents on this project get this** marks a brief, or a linked text file, for the project's agents. Everyone who can read the project can read what you mark, and the page tells you so.
- An agent sees the marked items when it reads the project and reads a brief with `read_project_context`, 200 KB at a time. A longer brief is never cut silently: the agent is told how much it got and reads on from there. **Preview** shows exactly what one read returns.
- A brief can be up to 512 KB, and a project's briefs up to 5 MB in total. Anything larger is refused with a message saying so, never shortened.
- Members write briefs. Whoever added a brief, or the creator, can edit or remove it. Anyone who can open the project can read them. Guests never see them.

### Members, agents and visibility

**Members & access** is where the creator adds people by their work email, adds agents, and invites guests.

- **Visibility** is **Members only** (the default) or **Company** — every platform user at your company can open a Company project and work on it with its agents, but only members write its tasks and log; only members can open a Members-only one.
- **Adding an agent** you own takes effect at once. Adding someone else's agent raises an ask to its owner in their Inbox; the agent is listed as *Waiting for owner* until they approve, and as *Owner declined* if they refuse.
- **Guests** — invited outside clients — see the project's name and goal, their own chats on it, and the files and reports shared with them. They never see the log, the tasks, the members or other people's chats, and their chats carry no project context to the agent.
- **Where you stand.** Under the goal, the project page says your role and what it lets you do: the creator can change everything; a member can add and change tasks, write in the log and add files; a company viewer can open the project and chat with its agents, but only members change its tasks, log and files. If you aren't a member, one line in place of **New task**, the log entry form and **Add from…** says who can add you. On an archived project the line says nothing changes until the creator restores it. The **Awaiting verification** group says who confirms a finished task: a member, or the steward agent by name when it is active on the project.
- **Ask to join.** Someone who can open a company project but isn't on it gets **Ask to join** under the role line. The project's creator gets one decision in their **Inbox**. **Approve** adds them as a member, and **Decline** sends back the reason the creator gives. While a request waits, the page says who it waits on, and asking again doesn't send a second one. Guests are invited and never see this.
- **Ask to add a room's agent.** In a room linked to the project, a member who can't add an agent gets **Ask to add** in the room's agents list. The creator decides in their Inbox. If they approve and the agent is someone else's, its owner is asked next, as always.
- **Archiving** keeps the project readable and its chats yours, but nothing new can be linked and agents stop receiving its context. **Restore** undoes it.

## For Agents

Agents reach a project through the MCP tools the platform exposes to them — `list_projects`, `get_project`, `list_project_tasks`, `get_project_log`, `read_project_context` (a brief people marked for agents, 200 KB a call), `create_project_task` (with `parent`, `blocked_by` and `for_person`), `update_project_task` (`parent` — `null` clears it — `blocked_by_add`, `blocked_by_remove`, `for_person`), `add_project_task_note`, `add_project_log_entry`, `link_to_project`, `propose_project_changes` (any agent on the project, with the project-management permission), and for a steward agent `get_steward_digest`, `set_project_health` and `start_project_task`. `list_project_tasks` with `view: "next"` returns the tasks that can be worked now; an agent never sees a task for a person there. Each task carries `creator`, `assigned_to`, `for_person`, `parent`, `subtasks`, `blocked_by`, `waiting_for` and `unblocked`. Projects carry `state` and `mode`; tasks carry `needs_go` and `needs_decision`. An agent sees only the projects it is active on, and only in a turn whose audience is internal: a chat or room with no outside client in it.

`start_project_task` (`project_id`, `task_id`, `agent`): As a project's steward, start one task's run on an agent you name. The platform never picks the task or the agent. What happens depends on the project's mode, which only a person sets: in Track a person starts every run, so this is refused (`mode_track`); in Propose, and on a task held for a person's go in any mode, it becomes a proposal the task's person approves (state `proposed`); in Auto the run starts (state `started`). The run is the person's: the one in this conversation, or, in a turn with no person, the person who turned Auto on (refused as `auto_origin_unavailable` when they can no longer start that agent here). Other refusals: the project is not running (`project_not_started`, `project_stopped`, `project_done`); `only_the_steward_starts`; the task is done, awaiting its check, for a person (`task_is_for_a_person`), needs a decision, blocked or waiting on another task; the agent is not on the project; `agent_consent_predates_runs`; a run is already live (`task_run_live`) or proposed. A refusal never asks you to widen a permission. A run ends one of two ways: the worker moves the task to pending-verification with its evidence, or raises an ask with `context.run_id`. In a turn with no person, an agent may move a task to active or pending-verification only as that task's run. The agent-dev plugin's `project-*` skills wrap these tools; see the [agent-dev plugin page](../abilities/agent-dev-plugin.md).

## Limitations

- **Entitlement.** Projects appear only on licensed instances; an external client never sees the Projects list.
- **Chats are private.** A project links chats; it does not share them. Other members' chats are a count.
- **One steward.** A project has one steward, a person or an agent; the creator stewards it until someone else is named.
- **Agents do not finish tasks, set priority, or change the project itself.** Done, priority, the project's state and mode, members and visibility are a person's. The steward verifies finished work, never its own.
- **An agent can request the people part; its primary contact approves it.** With the project-management permission, any agent on the project can request members to add or remove (work emails; to add, also a canon role written `role:<id>` — the people who hold it when you approve), other agents to add or remove, and an owner for each task. The request goes to the agent's **primary contact** — the person assigned on the agent's **Access** tab, or its owner when nobody is assigned — as one decision in their **Inbox**, showing the whole change, not to the project's creator. **Approve** applies exactly that, once, as you; anything you could not do on the project yourself is listed as not applied, by name, and the log records who requested it and who approved it. **Decline** applies nothing, and the reason you give is what the agent reads next. It can also ask for a new steward or a change to who can open the project. Only the project's creator can apply member, agent, steward and visibility changes, and a member can apply task owners, so a primary contact who could apply none of a request is not asked: the agent is told why and nothing is raised. One request waits at a time, and the **Steward** card says who it is waiting on. If the agent leaves the project before you answer, its request is withdrawn and nothing from it is applied. Adding an agent someone else owns still asks that agent's owner.
- **The platform does not run the steward.** An agent steward moves a project only through its own schedule.
- **A stopped project doesn't silence an open task chat.** Stopping refuses new runs, but a message you send in a task's existing chat still reaches its agent; the stopped banner says so.
- **Import is one-way.** A folder project imported from an agent is managed on the platform from then on; nothing syncs back to the folder.

## See Also

- [Workspace](workspace.md) — the chat app projects live in: the sidebar, chats as tabs, the Inbox
- [Approvals](../automation/approvals.md) — the asks an agent steward raises, and the owner-consent ask when an agent is added
- [Shared Sessions (Rooms)](../collaboration/rooms.md) — rooms that can be linked to a project
- [agent-dev plugin](../abilities/agent-dev-plugin.md) — `/project-init`, `/project-task`, `/project-steward`, `/project-status` and the rest of the project skills
- [Skills and Playbooks](../automation/skills-and-playbooks.md) — assigning the project skills to a deployed agent from the Library
