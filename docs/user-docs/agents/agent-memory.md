# Agent Memory

What an agent carries from one conversation or run to the next, kept by Trinity outside the agent's container so one person's details never reach anyone else. There are two kinds: **person memory** (notes about each person the agent talks to) and **retrieval memory** (facts captured from every run and recalled when they are relevant). Both are separate from a chat's own [working memory](agent-session.md), which lasts only for that conversation.

## Concepts

- **Person memory** — One record per agent and person: the agent's **notes** about that person, which it writes itself, and a **summary** of recent conversations, which Trinity rebuilds every few messages. It is put in front of the agent on every turn that serves that person — a Workspace chat, a signed-in public link, a one-to-one Slack, Telegram or WhatsApp conversation, and a scheduled brief addressed to them.
- **Retrieval memory** — Short facts Trinity extracts after each run (what people told the agent, what was decided, what it tried and how it ended), stored in this instance's own database, and handed back to the agent when they match the request in front of it. **Off by default.**
- **Scope** — Whose memory a turn reads and writes. Every person has their own scope, and nothing crosses between scopes. The agent's owner shares a scope with the agent's headless runs (schedules, loops, agent-to-agent calls), so a scheduled run can recall what the owner said in a chat, and the other way round. Group conversations, anonymous visitors, rooms and voice calls read and write no memory.
- **Fact** — One line of retrieval memory, with when it was last revised and what kind of run it came from, such as *(2026-10-21, scheduled run)*.

## How It Works

### Person memory

The agent writes its notes with the `write_user_memory` tool, during a turn that serves one person. It never names the person: Trinity reads who the turn is for from the run itself. A write replaces the whole notes text, so the agent writes the complete updated version each time. The summary is written by Trinity in the background and never by the agent.

Both writers are screened, because memory is written from conversations with anyone and read back into every later prompt for that person:

- A note that tries to plant instructions for the agent — prompt markers, "always do X" directives, tool directives, links inside instructions — is refused with the rule it tripped and the line it found, and nothing is stored. Preferences, open questions and links stated as facts are stored normally.
- A summary line that trips the same rules is dropped, and the rest of the summary is kept.
- When memory enters the prompt it is framed as information *about* the person, never as instructions, so stored text cannot pass itself off as part of the platform's own prompt.

On a Claude Code agent, a turn that serves anyone other than the owner also runs with Claude Code's own auto-memory switched off, so a "remember this" from one person is not saved into a file every other person's turns would read.

**What the person sees.** In the [Workspace](../sharing-and-access/workspace.md), the agent's **Info** tab has a **What it remembers about you** section: the current notes and a **Changes** list of every write that changed them — in a conversation, or by a scheduled run (named) — with the lines added and removed. **Undo** puts the notes back to how they were before a change, latest first. A change the agent's owner undid reads *Undone by an operator of this agent*.

**What the owner sees.** Open the agent's **Settings** tab, pick **Guardrails**, and scroll to **Memory → Person memory**: everyone the agent keeps memory about, with when it last changed. Open a person to see:

| Section | Shows |
|---------|-------|
| **Notes** | What the agent wrote down about them |
| **Summary** | The rolling conversation summary. It is rebuilt from the latest messages every few turns, so an undone summary can come back while that conversation is still recent |
| **History** | Every change to the notes and the summary, newest first, and where it came from (*In a conversation*, *Scheduled run · name*, *Conversation summary*). **Undo** reverts the latest change of each |
| **Facts** | The retrieval-memory facts kept from their conversations, 20 at a time — each with a **History** of what happened to it and **Forget**. A search box (**Search their facts**) finds what the agent would be reminded of in a conversation with this person |

The owner's own row points to the agent's shared memory instead, because the owner's conversations feed the agent scope. Only the agent's owner or an admin opens this view, and only as a person (a browser session or their own key).

### Retrieval memory

**Turning it on (admin).** Open **Settings → General → Retrieval memory** and switch on **Remember across runs**. The status line says what is happening: *Currently off*, *On, but unavailable — reason*, or *On. N facts stored · last captured …*. Capture starts from the next completed run; nothing earlier is backfilled. While it is on, the embedding model adds about 600 MB of RAM to each backend worker.

On the same panel, **Backend model calls run on** chooses which credential extracts the facts (the same credential also writes conversation summaries, chat titles and the compatibility checks):

| Option | Behaviour |
|--------|-----------|
| **The agent's subscription when it has one, else the platform key** (default) | An agent on a Claude subscription extracts on its own subscription; every other agent on the platform Anthropic key. A subscription that is rate-limited steps aside to the key when there is one |
| **Subscriptions only** | Only agents on a subscription are covered; nothing is captured for the rest |
| **Platform API key only** | Every call uses the platform key; with no key, nothing is captured |

Facts never leave the instance except for the extraction call itself, and credential values are removed from the text before it is sent.

**Keeping one agent out (owner).** **Settings › Guardrails › Memory** has a per-agent switch. With the platform switch off it says *Platform switch is off* and saves your choice for later; switched off, the agent is *Kept out* and nothing is captured or recalled for it.

**What happens on each run.** After a run ends, Trinity extracts up to 10 one-line facts from the request and the outcome, drops any that trip the same screen as person memory, merges each with any fact that already says the same thing (updating or retiring the old one), and stores the rest in the run's scope. On the next request in that scope, the facts most relevant to it — up to 5 — are added to the agent's prompt under *Retrieved for this request*. Relevance decides whether a fact is recalled; recency only reorders the recalled ones, so a newer wording of a changed fact ranks first.

**Searching it.** Under the per-agent switch, **Search memory** shows what the agent would be reminded of for any question you type, with the date, the kind of run and a relevance percentage for each fact. The agent can search too, with `search_memory`, when the five recalled facts are not enough; it only ever searches the scope of the turn it is in.

**Forgetting.** **Forget** on a fact (in a person's memory view) stops the agent recalling it. Every fact keeps a history — *Kept*, *Rewritten*, *Retired*, *Seen again, unchanged*, *Screened out*, *Pruned*, *Recalled into a prompt*, *Found by a search*, *Forgotten* — so you can see why it exists and when it was used.

## For Agents

| Tool | Description |
|------|-------------|
| `write_user_memory(memory_text)` | Replace the notes about the person the current turn serves (up to 8,000 characters). Refused when the turn serves no single person (a group, a schedule that names no one, an agent call), when the run is not the live turn, while another person's turn is live on the agent (`memory_turn_ambiguous`), or when a line trips the screen (the refusal names the rule and the line). A refused write stores nothing — tell the person the note was not saved |
| `search_memory(query, k?)` | Search retrieval memory for the current turn's scope; up to 50 facts, each with its date, kind of run and relevance. Refused when memory is off (`memory_off`), the embedding model is unavailable (`memory_unavailable`), or the turn serves nobody (`memory_no_subject`) |

The person is never a parameter: Trinity resolves it from the run. An agent's key works only on its own agent, and a person's key is refused on both tools.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/user-memory` | POST | The `write_user_memory` door — the agent's own key only |
| `/api/agents/{name}/person-memory` | GET | Everyone the agent keeps memory about (owner or admin, a person) |
| `/api/agents/{name}/person-memory/person?user_email=` | GET | One person's notes and summary |
| `/api/agents/{name}/person-memory/writes?user_email=` | GET | That person's change history |
| `/api/agents/{name}/person-memory/writes/{id}/undo` | POST | Undo one change |
| `/api/agents/{name}/retrieval-memory` | GET / PUT | This agent's retrieval-memory status / `{opt_out}` (PUT: owner, a person) |
| `/api/agents/{name}/retrieval-memory/facts?user_email=` | GET | One scope's facts (owner, a person) |
| `/api/agents/{name}/retrieval-memory/facts/{id}` | DELETE | Forget a fact |
| `/api/agents/{name}/retrieval-memory/events?fact_id=&execution_id=` | GET | A fact's history, or everything one run did |
| `/api/agents/{name}/retrieval-memory/search?q=&user_email=` | GET | The owner's search over one scope |
| `/api/agents/{name}/retrieval-memory/search` | POST | The `search_memory` door — the agent's own key only |
| `/api/settings/memory` | GET | Platform status (admin) |

The platform switch is the `trinity_memory_enabled` setting. See [Backend API Docs](http://localhost:8000/docs) for full schemas.

## Limitations

- **Retrieval memory is off by default** and captures nothing from before it was switched on.
- **No memory in shared conversations.** Rooms, Telegram and Slack groups, anonymous public-link visitors and voice calls neither read nor write person or retrieval memory.
- **One live turn at a time for writes.** On a shared agent, `write_user_memory` is refused while another person's turn is live, so a note can be lost. Stagger schedules that brief different people at the same time.
- **The screen is a pattern check**, not prompt-injection detection: a paraphrase or another language can get past it. The framing in the prompt is the second line of defence.
- **Subscription-only installs.** With **Subscriptions only**, or with no platform key, agents running Gemini CLI or Codex capture nothing.
- **Dates inside a fact can age.** A fact's date is when it was last revised, not when the event happened; relative words like "next week" in the text do not update.

## See Also

- [Workspace](../sharing-and-access/workspace.md) — where a person sees and undoes what an agent remembers about them
- [Continuous Conversations](agent-session.md) — a chat's working memory, which is separate
- [Public Links](../sharing-and-access/public-links.md) — per-person memory for email-verified visitors
- [Scheduling](../automation/scheduling.md) — scheduled briefs that read and write the person's memory
- [Agent Guardrails](agent-guardrails.md) — the Settings pane that holds the Memory section
