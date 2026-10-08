# Feature: Skills Tab — own and shared skills, Run, Requires approval (trinity-enterprise#754)

## Overview

One **Skills** tab on the agent page shows everything the agent can do: its **own skills** (the agent's `.claude/skills/`, live, or the last-known list when it is stopped) and its **shared skills** (platform library assignments and sets). Each skill is one fixed-size card with Run / Edit & Run; the owner or an admin also gets a **Requires approval** toggle and an approver-kind picker (the ent#753 gate map). It replaced the Playbooks tab (PLAYBOOK-001, retired flow [playbooks-tab.md](playbooks-tab.md)) and the library-only Skills tab (§22.2's surface); `PlaybooksPanel.vue` and `SkillsPanel.vue` are deleted.

Requirement: `docs/memory/requirements/skills.md` §22.1 (the Shared section is §22.2). All code is OSS; the `approver` kind appears only on an install with a registered assignments provider (`skill_gate_map_service.approver_kinds()`).

## User Story

As an agent owner, I want to see, run and gate every skill my agent has — its own and the library's — in one place, even while the agent is stopped, so that I never have to guess which tab holds a skill or whether running it needs someone's approval.

## Entry Points

- **UI tab**: `src/frontend/src/utils/agentTabs.js:75` — `{ id: 'skills', label: 'Skills' }` in Playbooks' former slot, for **every** viewer with access (the system agent included). Rendered at `src/frontend/src/views/AgentDetail.vue:270-280` (`<SkillsTab>`).
- **Deep links**: `?tab=skills`; the legacy `?tab=playbooks` resolves here through `TAB_ALIASES` (`agentTabs.js:27`) → `resolveDeepLinkTab` (`AgentDetail.vue:442`), applied by `applyDeepLinkRouting` in both `onMounted` (`:1419`) and `onActivated` (`:1469`). Also reached from the Library page's "Assigned to" chips (`/agents/{name}?tab=skills`) and the Overview tab's "N skills" button (`components/OverviewPanel.vue:532`).
- **API** (no new routes):

| Purpose | Route |
|---|---|
| Own list (live or last-known) | `GET /api/agents/{name}/playbooks?last_known=true` |
| Shared list | `GET /api/skills/library/status`, `GET /api/agents/{name}/skills`, `GET /api/skills/library`, `GET /api/agents/{name}/skill-sets?probe=true`, `GET /api/skills/library/sets` |
| Approval | `GET /api/agents/{name}/skill-gates[?probe=true]`, `PUT` / `DELETE /api/agents/{name}/skill-gates/{skill}` |
| Run | `POST /api/agents/{name}/task` `{message: "/<name>", async_mode: true}` |
| Assignment verbs | `PUT /api/agents/{name}/skills`, `POST /api/agents/{name}/skills/inject`, `POST` / `DELETE /api/agents/{name}/skill-sets/{set}` ([skill-assignment.md](skill-assignment.md)) |
| Self-approved marker | `GET /api/agents/{name}/executions[/{id}]`, `GET /api/enterprise/client-portal/agents/{agent}/history` |

## Frontend Layer

### Components

| File | Role |
|---|---|
| `components/skills/SkillsTab.vue` | The tab: filter, in-agent enforcement line, Own section, Shared section, the three dialogs |
| `components/skills/SkillCard.vue` | One card with fixed-height slots; renders decisions, emits verbs |
| `components/skills/SkillAssignModal.vue` | "Assign skills": the library picker, moved out of the deleted `SkillsPanel.vue` |
| `components/skills/SkillDetailsModal.vue` | A Shared card's full story: the #2914 conflict + "Unassign library skill", every delivery warning, the injection error, the superseded text |
| `components/skills/AgentSkillSets.vue` | Unchanged; shown inside the "Manage sets" `BaseModal` |
| `components/skills/ExecutionGateMarker.vue` | "Ran without approval" — `badge` (list row) or `line` (detail, Workspace turn) |
| `utils/skillCards.js` | Every rule, as a pure module (`buildSkillCards`, `modeChip`, `gateKeyFor`, `kindLabel`, …) |
| `components/base/BaseBadge.vue:22-27,60-64` | Gains `size: 'md' \| 'sm'`; `sm` (11px) is the card's mode chip |

#### Layout (`SkillsTab.vue`)

- **Header** (`:16-41`): title, a filter box (`:24-30`) that narrows both sections by name or description (`matches()`, `:316-320`), and the in-agent enforcement line (`:35-40`, below).
- **Own skills** (`:44-87`): count of own cards (`ownCount`, `:324`), a meta line (`ownBanner`, `:347-361`), then by `viewState` (`ownView`, `:326-335`): skeleton / `LoadFailed` (retry → `store.loadAgentList()`) / empty text (`ownEmptyText`, `:337-345`) / a grid of own cards plus kept-gate cards.
  - meta line, live: `From .claude/skills, ~/.claude/skills` (the agent's `skill_paths`);
  - meta line, last-known (warning tone): "The agent is stopped. Start the agent to run. Showing its skills as of <relative time>." or "The agent isn't answering. Showing its skills as of …"; the absolute time is on hover (`Listed <date>`).
- **Shared skills** (`:90-226`):
  - head: count (`store.assigned.length`); for the owner or an admin (not on the system agent), on a configured library: **Assign skills** (opens `SkillAssignModal`), **Manage sets** (opens `AgentSkillSets` in a `BaseModal`), **Sync now** (disabled while stopped; primary-styled while a save did not deliver — `pendingSync`, #2703; `store.inject()`);
  - set chips: up to 3 (`MAX_SET_CHIPS`) + "+N more"; purple when complete, warning with a dot when partial, unresolved or missing credentials (`setVariant` / `setTitle`), plus "Some sets need credentials" when any set lacks them; a failed sets read shows "Couldn't read this agent's sets" with a Retry (`store.loadSets()`) instead of an absent line; a click opens Manage sets;
  - the shared line (`:153-163`): the last verb's outcome (save / unassign / sync error, plus the ent#672 deprecation note), else "Last sync <time>" or "Not synced from this screen yet: skills are also copied in when the agent starts.";
  - body: skeleton / `LoadFailed` / the named empty states by `store.emptyReason` (`library_unconfigured` with an admin link to `/settings?tab=agents`, `library_empty`, `none_assigned` worded by role) / "No shared skill matches the filter." / the grid.

#### Exactly one section per skill (`utils/skillCards.js::buildSkillCards`, `:136-282`)

```
Shared  = one card per assignment row (store.assigned), joined to its library entry
          and to the live/last-known entry with the same name or dir          (:165-235)
Own     = every listed skill s that Shared does not render                    (:238-269)
            assigned  = a row exists for s.name or skillDir(s)
            own_copy  = s.name/dir is a #2914 conflict, or s.source == "agent"
            assigned and not own_copy → skipped (Shared renders it)
            s.source == "platform" and not assigned → "from library" badge (a leftover)
Kept    = gates matching no card, only once a list is known (live|last_known) (:272-279)
```

- A #2914 **name conflict** renders in both sections, as before: the Own card is the agent's copy, which runs; the Shared card keeps the assignment with a "name conflict" badge, Run disabled, and a note that opens the details dialog.
- An **old agent image** (no `source` / `dir` / `approval`): `skillDir()` (`:62-67`) derives the directory from `path`; an assigned name renders only in Shared, an unassigned one in Own; no leftover badge, no own-skill recommendation.
- An **orphaned assignment** (no longer in the library) is a Shared card with the note "No longer in the library".
- **Gate matching** (`findGate`, `:157-162`) checks the name, then the dir, casefolded. The key a write uses is `gateKeyFor({name, dir})` (`:71-75`): the name if it matches `SKILL_NAME_RE` (`:25`, the backend's `skill_packaging.SKILL_NAME_RE`), else the dir, lower-cased. With neither, the toggle is disabled with the reason (`cantCarry`, `:284`).

#### The card (`SkillCard.vue`)

Every slot has a fixed height, so content never changes a card's size; long text clips with the full text on hover or in the details dialog.

| Slot | Lines | Content |
|---|---|---|
| Name | `:17-24` | `/name` (+ the library version, short SHA) |
| Badge area | `:29-59` | One line on Own, two on Shared. The author's mode chip first (`BaseBadge size="sm"`), then platform facts: `via <set>`, `deprecated`, `name conflict` or the last sync's `synced` / `up to date` / `partial` / `failed`, `from library`, `not user-invocable` |
| Description + hint | `:63-78` | Two lines + the argument hint. While a verb's error stands, an `InlineError` replaces them (dismissible) |
| Note line (Shared) | `:81-98` | First match of: conflict → injection error → one delivery warning → "N delivery warnings" → "Superseded by …" → "No longer in the library" → "Not in the agent yet: sync now, or start it again". A note with detail is a button that opens `SkillDetailsModal` |
| Gate line | `:101-113` | Shown to everyone (below) |
| Verbs | `:116-155` | **Run**, **Edit & Run**, **Unassign** (Shared, owner/admin; disabled for a set-only member, with the reason); on a kept-gate card only **Clear gate** |
| Approval row | `:158-187` | Owner/admin, not a ghost, not the system agent: `BaseToggle` "Requires approval" + `BaseSelect` approver kind; on a kept-gate card, "Gate set <date>" |

**Mode chip** (`MODE_CHIPS` / `modeChip`, `:32-43`) — the author's `automation:` frontmatter, a declaration nothing enforces:

| `automation` | Chip | Variant |
|---|---|---|
| `autonomous` | runs unattended (loop icon) | `autonomous` |
| `gated` | asks mid-run (pause icon) | `info` |
| `manual` | start by hand (person icon) | `neutral` |
| anything else | the raw value | `neutral` |

The raw value is on hover (`automation: <value>`). "Approval" and "gated" belong only to the enforced gate.

#### Run (`SkillsTab.vue::onRun`, `:428-451` → `stores/skills.js::runSkill`, `:210-219`)

```javascript
const response = await api.post(`/api/agents/${agentName.value}/task`, {
  message: `/${skillName}`,
  async_mode: true,   // the shared client times out at 30 s; a run is accepted, not awaited
})
```

| Answer | What the page does |
|---|---|
| 202 `pending_approval` (`utils/skillGate.js::pendingApprovalMessage`) | Info toast with the server's message for 8 s (`PENDING_NOTICE_TOAST`); no navigation — nothing ran (trinity#3274) |
| Named gate refusal (`isGateRefusal`: `X-Trinity-Error-Code` + `detail.status === 'refused'`) | Error toast (`apiErrorMessage`), kept until dismissed (`composables/useNotification.js:39`) |
| Any other error | `InlineError` on the card: `Could not run /<name>` or the server's message |
| Accepted `{status: "accepted", execution_id}` | `emit('run-with-instructions', '__NAVIGATE_TASKS__:<id>')` → `AgentDetail.vue:1568-1588` opens Tasks with `?execution=<id>` |

**Edit & Run** (`onEditRun`, `:434-436`) emits `/<name> `, which prefills the Tasks input. It needs only `user_invocable`.

Run enablement (`runVerdict`, `skillCards.js:103-113`), checked in order:

| Condition | Run | Hover text |
|---|---|---|
| Agent not running | off | Start the agent to run |
| List not live (last-known) | off | The agent isn't answering right now |
| Shared card whose name the agent's own skill shadows | off | The agent's own skill of this name runs: use it from Own skills |
| Not in the live list | off | Not in the agent yet: sync now, or start it again |
| `user_invocable: false` | off | This skill is not user-invocable |
| Gated | on | Asks the approver first: nothing runs until they say yes |

The last-known list never drives Run.

#### Gate line and approval row

Gate line (`gateLineFor`, `skillCards.js:115-129`). It names a **kind**, never a person: the map is readable by every viewer and by agent keys.

| State | Icon | Line |
|---|---|---|
| Gated, `approvers[kind].viewer_fills` | lock | Needs approval: you approve this |
| Gated, the kind reaches nobody (`reachable: false`; with no `approvers` row, the gate's own `approver_reachable`) | warning | Needs approval from <kind>: nobody fills it yet |
| Gated | lock | Needs approval from <kind> (`primary` → "the primary contact", `approver` → "an approver") |
| Not gated, `approval: recommended`, and the approval row is shown | warning | Author recommends approval: not gated |

`recommended` reads the library entry's `approval` (Shared) or the agent server's `approval` field (both sections).

Approval row (`SkillCard.vue:158-187`, `:217-242`):
- the picker lists `approvers` (fallback: `primary`); a kind nobody fills is listed as `(nobody yet)` and cannot be selected; a gate whose kind the install no longer offers is appended, disabled;
- toggle on → `set-gate` with the shown kind: the gate's own, else the first reachable kind, else `primary` — never a bodiless PUT;
- toggle off → `clear-gate`; changing the kind of a gated skill → `set-gate` with the new kind;
- `SkillsTab.onSetGate` / `onClearGate` (`:457-465`) → `stores/skillGates.js::setGate` / `clearGate`.

**Kept gates** (ent#753: sticky until cleared): a gate whose skill is not in the listed skills renders as a dashed card under Own: "Not in this agent's skills list: gate kept", description "This gate stays until someone clears it.", **Clear gate** for the owner or an admin (it may guard a `.claude/commands/` entry). They render only once a list is known.

#### In-agent enforcement line (`SkillsTab.vue:381-392`)

Shown where the approval row is (owner or admin, not a ghost, not the system agent) and the agent has at least one gate (`enforcementSlot`). The slot is reserved as soon as gates exist, so the background probe swaps text in place.

| `hook` | Line |
|---|---|
| `ok`, `unknown`, `null` | nothing |
| `unsupported_runtime` | This agent's runtime can't enforce gates inside the agent; requests that name a gated skill still wait for approval. |
| `predates` | This agent's image predates the in-agent gate check; recreate the agent to enforce gates inside it. |
| `missing` / `not_root_owned` / `writable` | The in-agent gate check isn't intact on this agent (<value>); recreate the agent to restore it. |

#### Self-approved marker (`ExecutionGateMarker.vue`)

A run that went through without approval because its requester is the approver (ent#752's `self_approved` record):
- **Tasks row**: `TasksPanel.vue:244-247`, a `locked` badge "ran without approval" after the compacted chip, from `task.gate_self_approved` / `gate_self_approved_by_viewer` (list read `TasksPanel.vue:713`);
- **Execution page**: `views/ExecutionDetail.vue:179,205-210`, a line in the Execution Origin card (the card also shows when the run has no other origin data);
- **Workspace turn**: `components/portal/PortalConversation.vue:468-473`, a line under the agent bubble, from `assistantRow` (`portalUtils.js:2104-2122`). History rows map through it (`PortalConversation.vue:1501-1510`). A just-finished turn gets the flag from the reply poll (`replyFields`, `portalUtils.js:2207-2220` → `PortalConversation.vue:2155-2165`), as does a reattached turn (`PortalConversation.vue:1618-1619`).

Copy: "Ran without approval: you are the approver." for the requester, else "Ran without approval: its approver started it.".

### State Management

**`stores/skills.js`** (agent-scoped, #235) gains the agent's own listing (`:80-93`):
- `agentList`, `agentListPaths`, `agentListAt` (`captured_at`), `agentListReason` (`stopped` | `unreachable`), `agentListError`, `agentListLoaded`;
- `agentListState`: `idle | live | last_known | none | failed`;
- `loadAgentList(name)` (`:173-201`) has a sequence guard plus an agent check. A 404 / 503 / 504 is `none`, a known state (stopped or unreachable with no copy kept). Any other failure is `failed` only before the first success: a failed refresh keeps what is on screen;
- `runSkill(name)` (`:210-219`) returns `{held}` (a 202) or `{executionId}`, and throws on any error, a gate refusal included.

**`stores/skillGates.js`** (new, agent-scoped):
- holds `gates`, `approvers`, `approverKinds`, `hook`, and per-skill `busy` / `errors` keyed by the lower-cased gate key;
- `load(name, {probe})` (`:100-104`) reads the map, then probes in the background, so a slow agent never holds the toggles back;
- `write()` (`:110-128`) re-reads the map for the agent it wrote to, and drops the answer if the page moved to another agent (KeepAlive);
- the PUT's `warnings` (e.g. `approver_unassigned`) need no state of their own: the map is re-read after every write, and its `reachable: false` is what the card's "nobody fills it yet" says.

**Refetch of the own list** (`SkillsTab.vue:505-531`):
- agent switch → full `loadAll()`;
- status change → `loadAgentList()` plus, for the approval-row viewer, a re-probe;
- the `agent_skills_changed` tick (`store.changedAt[agent]`, #2703);
- the Manage sets dialog closing;
- after save / unassign / sync (`onSaved`, `onUnassign`, `onSync`).

`onUnmounted` (`:533-536`) clears both stores. Switching tab unmounts the tab; leaving the KeepAlive'd page does not.

### API Calls

On load (`loadAll`, `SkillsTab.vue:505-510`):

```javascript
store.load(name)                 // GET /api/skills/library/status + GET /api/agents/{n}/skills;
                                 // configured → GET /api/skills/library,
                                 //   GET /api/agents/{n}/skill-sets?probe=true, GET /api/skills/library/sets
store.loadAgentList(name)        // GET /api/agents/{n}/playbooks  params {last_known: true}
gatesStore.load(name, { probe }) // GET /api/agents/{n}/skill-gates; then ?probe=true when probe (approval-row viewer)
```

Writes: `PUT /api/agents/{n}/skill-gates/{key}` `{approver}`, `DELETE /api/agents/{n}/skill-gates/{key}`, `POST /api/agents/{n}/task`, plus the assignment verbs in [skill-assignment.md](skill-assignment.md).

### Loading, failures and agent switches

- **A section draws once every read it is built from has answered, or failed.** `SkillsTab.vue` computes `gatesKnown` (`skillGates.hasLoaded || error`) and `sharedKnown` (`skills.sharedLoaded || error`). Own needs the agent's list, the assignments and the gate map; Shared needs the assignments and the map. `stores/skills.js::sharedLoaded` is per agent: status, assignments, the library list when configured, then the sets read. `emptyReason` stays null until it is set. `buildSkillCards({assignmentsKnown, gatesKnown})` calls nothing "left from the library" and holds every approval toggle until both are known.
- **Failures are named.** Shared shows `LoadFailed` with Retry, carrying the library read's own error. A failed gate map gets a line for everyone (`skills-gates-error`), with Retry. Own's 404/503/504 is the known "none" state; a running agent offers "Check again" (`skills-own-retry`). A failed refresh keeps the list behind the stale banner, with Retry. A 200 that is not a listing is a failure.
- **Agent switch.** The tab outlives a switch. `resetView()` clears the outcome line, card errors, the pending-sync emphasis, the three dialogs, the Unassign confirm and the filter. Run, Unassign and Sync drop an answer for an agent the page has left. Every read and write in `stores/skills.js` checks its answer against the agent it was asked for, and a load also against a newer load. A write's follow-up read targets that agent. The per-agent busy flags reset with the agent, and `saveAssignments` answers `null` when superseded.
- **Unassign** opens a `ConfirmDialog` that says the approval requirement goes with it when the card is gated; focus lands on Cancel. The details dialog's "Unassign library skill" and Manage sets' "Unassign set" carry `data-destructive`, so neither takes initial focus.
- **Gate writes.** A card's `gateKey` is the matched gate's own key when gated (it may be the directory), else `gateKeyFor` (the name, which is what a request types). `SkillCard`'s default kind is computed from the current map; a pick holds the select only until its write settles. Each switch is named "Requires approval for /<name>" (`BaseToggle` lets `aria-label` name a labelled switch).

### Roles, ghosts, the system agent

`SkillsTab` props come from the agent payload: `can_share` (owner or admin, `routers/agents.py:493`), `is_system`, `ephemeral`.

| Viewer / agent | Tab, Run, Edit & Run, gate line | Approval row, hook probe (`showOwnerRow`, `:279`) | Assign / Manage sets / Sync / Unassign / Clear gate (`showManage`, `:281`) |
|---|---|---|---|
| Owner or admin | yes | yes | yes |
| Shared user, other viewer | yes | no | no |
| System agent (any viewer) | yes | no | no |
| Ephemeral (ghost) agent, owner | yes | no (ent#753 refuses gates on ghosts) | yes |

The server enforces the same lines: gate writes need a person who owns the agent or is an admin (or a `skills.manage` holder, never on itself) — [skill-gate.md](skill-gate.md) §6; `probe` is honoured only for a person who may manage the agent's skills; assignment writes go through the ent#596 fence (`get_skill_managed_agent_by_name`).

### Vocabulary (agent surfaces say "skills"; API names unchanged)

`components/chat/ChatInput.vue:25` (`/` menu heading) and `:248` (placeholder); `views/PublicChat.vue:285`; `components/portal/PortalConversation.vue:1872` ("/ for skills"); `components/portal/PortalTypeahead.vue:104` (heading) and `:110` (screen-reader count); `components/portal/portalUtils.js:1032` (empty line); `components/DashboardPanel.vue:149`; `components/ExposedToolsPanel.vue:14` ("Exposed skills"); `components/ConnectorChannelPanel.vue:6`; `components/SharingPanel.vue:210`. `/playbooks`, `exposed_playbooks`, `run_playbook` and `list_playbooks` keep their names.

## Backend Layer

### 1. One proxy of the agent's list — `services/agent_skills_listing.py` (new)

```
GET /api/agents/{n}/playbooks[?last_known]   routers/agent_files.py:79-104   (AuthorizedAgentByName)
GET /api/public/playbooks/{token}            routers/public.py:403-431       (link token)
connector_service.fetch_live_playbooks       services/connector_service.py:108-125
        │
        ▼
fetch_live(agent)                                                       :90-115
  ├─ no container          → SkillsListUnavailable(not_found,   404, "Agent not found")
  ├─ not running           → SkillsListUnavailable(not_running, 503, "Agent is not running. Start the agent to view its skills.")
  ├─ GET http://agent-{n}:8000/api/skills   (agent_httpx_client, 10 s)
  │    timeout             → (unreachable, 504, "Agent is starting up, please try again")
  │    connect error       → (unreachable, 503, "Could not connect to agent")
  │    non-200             → (agent_error, <status>, "Agent returned error: …")
  └─ 200 → remember(agent, body) → body
list_skills(agent, last_known)          (the agent-page route only)     :118-142
  └─ not_running | unreachable, last_known=true, recall() has a copy →
       200 {skills, count, skill_paths, last_known: {captured_at, reason: "stopped" | "unreachable"}}
     anything else → the original error, unchanged
```

- **`remember`** (`:145-169`) keeps only a dict whose `skills` is a list, as `{skills, skill_paths, captured_at}`. A live EMPTY list replaces the copy. A JSON body over `MAX_CACHED_BYTES` (256 KB, `:53`) is not kept AND drops the older copy, which is never served as current. It writes through `redis_breaker_util.get_breaker_redis()` (1 s socket timeouts, `None` when down); a failed write is logged and the live answer still returns.
- **`recall`** (`:172-193`) returns `None` for no key, Redis down, or a malformed value (shape-checked).
- **`forget`** (`:196-204`) never raises.
- **`public_view`** (`:207-216`) keeps each skill to `PUBLIC_SKILL_FIELDS` (`:58-61`), the eight fields the public link carried before #754, so `source` / `dir` / `approval` (and any later field) never reach an anonymous visitor.
- Callers keep their own wording: `/playbooks` maps the exception's status and detail, and any other exception (e.g. a non-JSON body) to 500 `Failed to fetch playbooks: …`. The public route maps `not_found` / `not_running` to 503 "Agent is not running". The connector says "Agent is not running." for `not_running`.
- Not routed: the Workspace roster (`client_portal/service.py:1389`, `_read_skills`) reads the agent directly and keeps only title / description / starter, so it neither writes nor serves the copy.

**Redis key** `agent:skills_list:{name}` (`KEY_PREFIX`, `:49`), no TTL:
- **written** by every successful live read through `fetch_live`: the Skills tab, the chat `/` menu, the Dashboard's update-dashboard check, the exposed-skills picker, the public link, the connector;
- **cleared** by `clear_agent_runtime_state` (`services/agent_runtime_state.py:212-219`; the keyspace is registered in `CLEARED_KEYSPACES`, `:81`, for `test_1560_agent_redis_key_parity`), which runs on delete (`routers/agents.py:827`), rename, both names (`routers/agent_rename.py:205-206`), failed create (`agent_service/crud.py:3148`), ghost teardown (`agent_service/ephemeral.py:171`), retention purge (`cleanup_service.py:1736`) and orphaned-ghost reclaim (`cleanup_service.py:2116`);
- **cleared again on the create path**, beside `clear_agent_breakers` (`agent_service/crud.py:3379-3380`): a live read in flight during a delete could have written it back;
- **never cleared** on stop or start (`clear_agent_breakers`, which runs on every start, does not touch it).

### 2. The gate map read gains two fields — `routers/skill_gate.py:98-118`

Auth is unchanged (`get_skill_gate_readable_agent_by_name`). Models: `SkillGateApproverStatus` / `SkillGateMapResponse` (`models.py:3877-3902`).

- **`approvers: [{kind, reachable, viewer_fills}]`**, one entry per kind in `approver_kinds()` (`skill_gate_map_service.approver_status`, `:154-177`):
  - `reachable`: the kind resolves to at least one person (`skill_gate_service.approver_people`, `skill_gate_service.py:311-319`, which is `_approvers` without the refusal);
  - `viewer_fills`: `requester_from_principal(caller)` (`skill_gate_service.py:658-681`) is a person and its casefolded email is in that list. These are the functions `enforce` uses to decide self-approval, so the card cannot say "you approve this" for a run the gate would hold. An agent key, a connector or the event loopback never fills a kind;
  - booleans only: no name or email.
- **`hook`** with `?probe=true` (`skill_gate_map_service.hook_status`, `:228-258`):
  - honoured only when `is_person_principal(caller)` and `can_manage_agent_skills(caller, agent)` (the #3052 `probe` precedent); anyone else gets `null`;
  - one direct `agent_httpx_client` `GET /health`, 3 s timeout, no circuit-breaker bookkeeping;
  - the agent's `skill_gate_hook` (`agent_server/routers/info.py:135`) when it is one of `ok | missing | not_root_owned | writable | unsupported_runtime`; `predates` for a 200 without the field; `unknown` for no usable answer (stopped, timeout, non-200, not JSON, an unexpected value).
- MCP `list_skill_gates` (`src/mcp-server/src/tools/skills.ts:541-566`) passes the response through and sends no `probe`, so `hook` is `null` there.

### 3. "Ran without approval" flags — derived, no new column

- **Source**: ent#752's `self_approved` row in `skill_gate_requests`, written by `skill_gate_service.record_self_approval` at the `/task`, `/chat` and backstop seams; its UNIQUE `dispatched_execution_id` is the run the agent received.
- **`db.get_self_approved_runs(agent, ids)`** (`db/skill_gate_requests.py:168-182`, facade `database.py:2315`): one `SELECT dispatched_execution_id, requester_key … WHERE agent_name = ? AND state = 'self_approved' AND dispatched_execution_id IN (…)`.
- **`skill_gate_map_service.self_approved_flags(agent, ids, principal)`** (`:180-206`) → `{id: (True, by_viewer)}`. `by_viewer` is `requester_key == "person:" + casefold(email)` for a person principal, so the email never leaves the server. It never raises: a failed read means no flags.
- **Executions**: `routers/schedules.py:891-917` (`get_agent_executions`, one batch read per page) and `:920-938` (`get_execution`) take `current_user` and add `gate_self_approved` / `gate_self_approved_by_viewer` (`ExecutionSummary` `models.py:4121-4122`, `ExecutionResponse` `:4178-4179`). The PERF-001 summary column list is unchanged.
- **Workspace**: `client_portal/router.py:1623-1647` (`portal_history`) → `service.get_history(agent, principal.email, …)` → `annotate_self_approved_turns` (`client_portal/service.py:4587`; `skill_gate_map_service.py:209-225`). Only assistant rows with an `execution_id` (#3166) are looked up, the viewer is the portal session's email, and questions, report rows and pre-#3166 rows read false. `PortalHistoryMessage` gains both fields (`client_portal/models.py:1008-1009`).

### Database Operations

- No new table, column or migration.
- Reads: `skill_gate_requests` (`state = 'self_approved'`, by `dispatched_execution_id`), `agent_skill_gates` (ent#753), `agent_skills` / `agent_skill_sets` (unchanged).
- Writes: only the existing gate and assignment writers ([skill-gate.md](skill-gate.md) §6, [skill-assignment.md](skill-assignment.md)).
- Redis: `agent:skills_list:{name}` (above).

## Agent Layer

`docker/base-image/agent_server/routers/skills.py`, `GET /api/skills` (`:366-415`). `SkillInfo` (`:32-52`) gains three informational fields; the agent can write both the marker and its frontmatter, so none of them decides access or gating.

| Field | Source |
|---|---|
| `source` | `platform` when `.trinity-skill.json` (`PLATFORM_MARKER_FILENAME`, `:25`) is a file in the skill dir, else `agent` (`_source`, `:253-257`) |
| `dir` | The directory name (`:343`); the gate fingerprint resolves a dir first |
| `approval` | `_approval` (`:260-275`): the `trinity:` block first, then the flat key, first non-null wins (the backend `skill_packaging.extract_contract` precedence); kept only if in `APPROVAL_VALUES` (`:29`, `{"recommended"}`); anything else is `null` with the #2850 warn-once skip |

The name-only fallback for an unreadable `SKILL.md` still carries `source` and `dir` (`:359-360`). The two constants are duplicated from `skill_packaging` (`META_FILENAME`, `APPROVAL_VALUES`) because the image cannot import the backend. Existing agents get the fields only after `./scripts/deploy/build-base-image.sh` and a recreate; until then the frontend falls back (`skillDir()` from `path`, no own-skill `approval`).

The scanner (`scan_skills_directory`, `:278-363`) walks one level of `.claude/skills/` and `~/.claude/skills/` (project skills win a duplicate name, sorted by name). **Per-field frontmatter normalization (#2850)**: each field is coerced on its own through `_field` (`:245`), so one bad field drops to `null` and the rest of the record survives; the outer `except` is for an unreadable file only.

| Field | Accepted forms | Result |
|-------|----------------|--------|
| `allowed-tools` | `Read, Bash, Bash(git:*)` (Claude Code's string) or `[Read, Bash]` | The same `List[str]`; the string splits on commas at group depth 0 (`normalize_allowed_tools`, `:188`). A bool, mapping, number or unbalanced group → field skipped. Display metadata only: nothing enforces it |
| `description`, `automation` | String; YAML 1.1 scalars (`2026-01-01`, `42`, `yes`) become their string form | A list or mapping → field skipped |
| `argument-hint` | String; `[file]` / `[a, b]` restored to the bracketed string; `[]` → `null` | A nested collection → field skipped |
| `user-invocable` | Bool, or `"true"` / `"yes"` / `"1"` | Unchanged |
| `approval` | See above | Closed set |

A skipped field is warned once per `(file, field, value)` (`_SKIPPED_FIELD_WARNED`, `:121`), then logged at DEBUG.

## Side Effects

- **Redis**: `SET agent:skills_list:{name}` on each successful live read; `GET` only on the stopped / unreachable path with `last_known`; `DEL` on the lifecycle events above.
- **Agent `/health`**: one probe per tab load (and per status change) by the approval-row viewer.
- **Gate writes**: ent#753's marker ordering and audit rows (`skill_gate_set` / `skill_gate_cleared`, `details.via` from the principal) — [skill-gate.md](skill-gate.md) §6.
- **Run**: an async `/task` execution (Tasks list, execution row). A gated skill raises an approval and nothing runs. A run by the approver is self-approved: the `self_approved` record plus the audit row (ent#752).
- **Assignment verbs**: delivery plus the `agent_skills_changed` thin trigger (#2703) — [skill-injection.md](skill-injection.md).
- **WebSocket**: none added. The tab re-reads its own list on the existing `agent_skills_changed` tick.

## Error Handling

| Case | Answer | What the tab shows |
|---|---|---|
| Agent stopped, copy kept | 200 + `last_known.reason: "stopped"` | Warning banner, Run disabled ("Start the agent to run") |
| Agent stopped, no copy (or Redis down) | 503 "Agent is not running. Start the agent to view its skills." | "Start the agent to see its own skills." — never an empty grid |
| Running but unreachable, copy kept | 504 / 503 → 200 + `reason: "unreachable"` | "The agent isn't answering. Showing its skills as of …", Run disabled |
| Running but unreachable, no copy | 504 / 503 | "The agent isn't answering right now; its own skills show here when it does." |
| No container | 404 "Agent not found" (even with a copy) | Treated as `none` |
| Agent answered non-200 | That status, "Agent returned error: …" — never masked by the copy | `LoadFailed` with retry on first load; a failed refresh keeps the list |
| Body not JSON | 500 "Failed to fetch playbooks: …" | `LoadFailed` |
| Listing over 256 KB | 200 live; not kept, older copy dropped | Stopped later: no list |
| Gate PUT/DELETE refused | 422 `invalid_skill_name` / `invalid_approver` / `invalid_deadline` / `approver_unavailable`; 409 `ephemeral_agent`; 403 `person_required` / `skill_management_not_permitted`; 404 | `InlineError` in that card's description slot |
| Name can't carry a gate | (client-side, `SKILL_NAME_RE`) | Toggle disabled, reason on hover |
| Run held | 202 `pending_approval` | Info toast, 8 s, no navigation |
| Run refused by the gate | Named refusal + `X-Trinity-Error-Code` | Error toast until dismissed |
| Run, other failure | e.g. 429 / 503 | `InlineError` on the card |
| Sync while one runs | 409 | Shared line: "A skill sync is already running for this agent. Try again in a moment." |
| Unassign fails | Server error | `InlineError` on the card |
| Hook probe, no answer | `hook: "unknown"` | Nothing (not "not ok") |
| Self-approved read fails | Flags false (logged) | No marker |
| `record_self_approval` write failed | No record | No marker. Named gap: the in-container hook refuses the skill inside that run anyway |

## Security Considerations

1. **Last-known list**: served only on `?last_known=true` under the live route's own gate (`AuthorizedAgentByName`). It is display-only and never drives Run. The key is backend-only (agents are not on the platform network, #589), size-capped, and cleared on every lifecycle event that frees the name.
2. **Public link**: `public_view` is an allow-list, so new per-skill fields stay off the unauthenticated route by default.
3. **Gate map**: `approvers` carries booleans only; the gate line names a kind. `viewer_fills` reuses the enforce path's functions and is false for every machine principal.
4. **Probe**: honoured only for a person who may manage the agent's skills; one `/health` read, no breaker side effects, so it is not a read amplifier.
5. **Self-approved flags**: the viewer comparison happens on the server; no email is added to any payload (pinned by the response-key tests).
6. **Agent-reported `source` / `dir` / `approval`**: agent-controlled and informational; the platform gate map stays the authority.

## Testing

### Prerequisites

- Services running; a skills library with at least one skill.
- A base image built from this branch and the test agent recreated, for `source` / `dir` / `approval` and the `/health → skill_gate_hook` field (`./scripts/deploy/build-base-image.sh`). Save the running image under a second tag first: a build from an older local `dev` downgrades the agents.
- The owner account has an email (`primary` resolves to it; the default admin often has none); a second account with shared access to the agent.

### Test Steps

1. **Action**: As the owner, open a running agent's Skills tab.
   **Expected**: Own lists the agent's `.claude/skills/` skills ("From .claude/skills, …"), Shared lists the assignments, and each name appears once (a #2914 conflict appears in both, its Shared card's Run disabled).
   **Verify**: `GET …/playbooks?last_known=true` → 200 without `last_known`; Redis `EXISTS agent:skills_list:<agent>` → 1.
2. **Action**: Click **Run** on an ungated skill.
   **Expected**: The Tasks tab opens with the run highlighted.
   **Verify**: The POST body is `{message: "/<name>", async_mode: true}`.
3. **Action**: Click **Edit & Run**.
   **Expected**: The Tasks input reads `/<name> `.
4. **Action**: Turn **Requires approval** on.
   **Expected**: `PUT …/skill-gates/<name>` `{approver: "primary"}`; the gate line reads "Needs approval: you approve this". For an owner with no email: "…: nobody fills it yet", and the kind is listed as "(nobody yet)".
5. **Action**: As the shared user, open the same tab and Run the gated skill.
   **Expected**: The gate line reads "Needs approval from the primary contact", with no approval row and no Assign / Manage sets / Sync / Unassign; Run shows an info toast with the server's notice and stays on the tab.
   **Verify**: A card in Needs Response.
6. **Action**: As the owner, Run the gated skill.
   **Expected**: It runs. Its Tasks row shows the "ran without approval" badge ("…you are the approver." on hover), and the execution page says so in the origin card. The shared user sees "…its approver started it."
7. **Action**: As the owner, send `/<gated-skill> …` in the Workspace.
   **Expected**: The marker line sits under the agent's reply, both as it lands and after a reload.
8. **Action**: Stop the agent.
   **Expected**: Own shows the last-known list with the stopped banner; every Run is disabled.
   **Verify**: `…/playbooks?last_known=true` → 200 with `last_known.reason: "stopped"`; without the param → 503.
9. **Action**: Delete the stopped agent.
   **Expected**: A new agent with the same name never shows the predecessor's list.
   **Verify**: `EXISTS agent:skills_list:<agent>` → 0.
10. **Action**: Set a gate on an agent whose image predates ent#752's hook.
    **Expected**: The owner sees "This agent's image predates the in-agent gate check; …"; the shared user sees no line.
11. **Action**: Remove a gated skill's directory from the agent, then reload.
    **Expected**: A dashed "Not in this agent's skills list: gate kept" card with **Clear gate**.
12. **Action**: Open `/agents/<name>?tab=playbooks`.
    **Expected**: The Skills tab.
13. **Action**: Type in the filter box.
    **Expected**: Both sections narrow by name or description.

### Edge Cases

Covered by unit tests:
- a live empty list replaces the copy;
- a non-listing 200 is never kept;
- Redis down on either path;
- an oversized listing drops the older copy;
- no container answers 404 even with a copy;
- an agent error is never masked;
- an old image (dir from `path`);
- a gate keyed on the dir rather than the frontmatter name;
- a name `SKILL_NAME_RE` rejects;
- an agent switch mid-load and mid-write;
- a machine key never "fills" a kind and is never "you";
- a record for another agent's run does not leak across.

### Test Files

| File | What it covers |
|---|---|
| `tests/unit/test_ent754_skills_list_cache.py` | The real `/playbooks` route with only the container, the agent's answer and Redis faked: live writes, empty replaces, no overwrite on failure or a non-listing, `last_known` stopped / unreachable / no copy / no param, an agent error not masked, 404 with a copy, Redis down on both paths, a malformed copy, the 256 KB cap, the keyspace registered and cleared by `clear_agent_runtime_state` and on create but not on stop, the public link's field strip and 503, the connector read; a container removed mid-read is 404 without Docker's text (public: 503); a connection dropped mid-answer is unreachable; the connector keeps "Agent error: …" and refuses a non-listing; the library listing names `approval` |
| `tests/unit/test_ent754_gate_map_viewer_and_hook.py` | The real `list_agent_skill_gates`: `approvers` per kind (OSS `primary`; both kinds with a provider), casefolded `viewer_fills`, another viewer / non-owner admin / emailless owner / agent key, agreement with `enforce`, no person data; `probe` ignored without manage rights, honoured for owner and admin, one `/health` with a short timeout, each state, `predates`, `unknown`; a system key and an agent key holding `skills.manage` pass the manage check but never get the probe; an unexpected probe failure is `unknown` and logged |
| `tests/unit/test_ent754_execution_self_approved.py` | A record written through the real `record_self_approval`, read through the real list and detail routes: only that run is marked, "you" only for the approver, a machine key sees the fact but is never "you", no email in the payload, no cross-agent leak; a run someone else approved is not marked; a large page is read in statements under every bind limit |
| `tests/unit/test_ent754_workspace_self_approved.py` | The real Workspace history read: only the self-approved reply is marked, the reply poll carries it, the route model keeps both fields and adds no email; through the route, a machine viewer (`is_person` false) sees the fact but is never "you"; the synchronous fallback answers both flags (`test_2580_reply_message_id.py`) |
| `tests/unit/test_ent754_agent_server_skillinfo.py` | The agent server loaded standalone: `source` follows the marker, `dir` reported, the unreadable-file fallback, `approval` closed set with `trinity:` precedence, a bad value keeps the rest, behavioural parity with `skill_packaging.extract_contract`, vendored constants equal |
| `src/frontend/tests/unit/skillCards.spec.js` | The pure rules: exactly one section (conflict in both), Run, gate-line variants, controls by role / ghost / system, Shared badges and the note line, mode chip and labels |
| `src/frontend/tests/unit/skillsStoreAgentSwitch.spec.js` | The skills store across an agent switch: a load (and a failed one), a sync, a save and a set write that answer after the switch are dropped, and a save's follow-up read never re-reads the next agent |
| `src/frontend/tests/unit/skillGatesStore.spec.js` | Load then background probe, no probe unless asked, a failed read, a failed probe is no answer, a stale answer dropped, writes send the kind and re-read, a refused write named on its skill, an agent left mid-write |
| `src/frontend/tests/unit/skillsTab.mount.spec.js` | The tab mounted (only `@/api` mocked): sections and roles, the stopped agent (list and none), Run in async mode and each answer, Edit & Run, the approval toggle and picker, "you approve this", kept gates, the hook warning and no probe for a viewer, the filter; an agent switch carries nothing over; nothing drawn from an unanswered read and every failure named; writes go to the gate the card shows, with a current default; Unassign confirms; dialogs open on the safe action; hook states in words |
| `src/frontend/tests/unit/executionGateMarker.mount.spec.js` | The marker itself, and wired into the Tasks row and the execution page's origin card |
| `src/frontend/tests/unit/portalSelfApprovedTurn.spec.js` | `assistantRow` / `replyFields` carry the flags; the conversation marks only the self-approved reply; `turnGateFlags` reads either spelling (streaming row or the synchronous body) |
| `src/frontend/tests/unit/skillGateSenders.spec.js` | Ported: the SkillsTab Run among the UI senders of a 202 / refusal (trinity#3274) |
| `src/frontend/tests/unit/skillSets.spec.js`, `skillDeprecation.spec.js` | Ported to `SkillsTab` / the dialogs: sets, chips and Manage sets; deprecated badge, superseded line, the conflict's way out in the details dialog |
| `src/frontend/tests/unit/skillAssignDraftSurvivesSync.spec.js` | Ported from `skillsPanelDraftSurvivesSync`: the picker draft survives a sync (`SkillAssignModal`) |
| `src/frontend/tests/unit/agentDetailTabSuperset.spec.js` | `playbooks` is an alias to `skills`; `skills` is offered to a non-owner |
| `src/frontend/tests/unit/agentDetailGateNotice.spec.js` | `<SkillsTab>` receives the page's toast host (`:notify`) |
| `tests/unit/test_1560_agent_redis_key_parity.py` | Existing: every `agent:` key literal is registered (`agent:skills_list:` in `CLEARED_KEYSPACES`) |
| `tests/unit/test_2850_skill_allowed_tools_forms.py` | Existing: the scanner's per-field normalization against `tmp_path` fixtures (string vs list `allowed-tools`, one bad field keeps the rest, bracket hints, YAML 1.1 scalars, warn-once) |

```bash
# backend unit tests (repo root, backend venv)
pytest tests/unit/test_ent754_*.py tests/unit/test_1560_agent_redis_key_parity.py tests/unit/test_2850_skill_allowed_tools_forms.py -q
# frontend
cd src/frontend && npx vitest run tests/unit/skillCards.spec.js tests/unit/skillGatesStore.spec.js \
  tests/unit/skillsTab.mount.spec.js tests/unit/executionGateMarker.mount.spec.js tests/unit/portalSelfApprovedTurn.spec.js
```

### Status

Implemented (2026-10-08).

Known limits:
- the Overview tab's "N skills" button counts library assignments only;
- canon roles in the approver picker are follow-up trinity-enterprise#848.

## Related Flows

- **Upstream**: [skill-gate.md](skill-gate.md) — the gate map (§6), enforcement, the in-container hook (§5), the self-approved record; [skill-assignment.md](skill-assignment.md) — the assignment writes behind the Shared section; [skill-injection.md](skill-injection.md) — delivery, Sync, `agent_skills_changed`.
- **Downstream**: [tasks-tab.md](tasks-tab.md) — where Run lands, and the marker on the row; [execution-detail-page.md](execution-detail-page.md) — the marker in the origin card.
- **Same proxy**: [playbook-autocomplete.md](playbook-autocomplete.md) (chat `/` menu), [public-agent-links.md](public-agent-links.md) (`GET /api/public/playbooks/{token}`), [mcp-connector.md](mcp-connector.md) (`fetch_live_playbooks`).
- **Retired**: [playbooks-tab.md](playbooks-tab.md) — the Playbooks tab, now this tab's Own section.
- **Elsewhere**: [library-page.md](library-page.md) — the fleet browse that links here; [workspace-composer-typeahead.md](workspace-composer-typeahead.md) — the Workspace `/` menu.

## Revision History

| Date | Changes |
|------|---------|
| 2026-10-08 | Created for trinity-enterprise#754: the Playbooks tab and the library-only Skills tab merged into one Skills tab — the cards, Run (`async_mode`), Requires approval, the last-known list (`services/agent_skills_listing.py`, `?last_known=true`), `approvers` / `?probe=true` on the gate map, the self-approved marker (Tasks, execution page, Workspace), agent-server `source` / `dir` / `approval`, and "skills" in UI copy. Absorbs `playbooks-tab.md`. |
| 2026-10-08 | Review round (ent#754): agent-switch guards in the tab and the skills store; sections wait for the reads they depend on and name each failure; gate writes keyed on the gate the card shows; a live default approver; Unassign confirms; dialogs focus the safe action; hook states in words. Backend: a container removed mid-read is a plain 404 (no Docker text, public link included); transport errors are unreachable; the connector keeps its wording; the self-approved read is chunked; a machine Workspace viewer is never "you"; the synchronous Workspace reply carries both flags; `GET /skills/library` names `approval`. |
