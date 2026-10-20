# Feature: Agent Settings home

> trinity#966 decision · trinity-enterprise#887 (rail, General, restart banner, incl. #353 timeout) · #890 (Guardrails) · #888 (Storage) · #891 (People) · #950 (header contents)

## Overview

The agent page is a working strip (Overview · Tasks · Chat · … · Files · Git) plus one configuration home: the **Settings** tab, pinned at the end of the strip outside the overflow menu. Inside it a left rail lists panes, each answering one question; the pane is in the URL.

## User story

As an agent owner I open one place to change what an agent is, what it may do, who works with it and what it keeps, and see in one bar every change that waits for a restart.

## Entry points

- **UI**: Agent Detail → **Settings** (last tab) → rail
- **URL**: `/agents/:name?tab=settings&section=general|guardrails|people|channels|integrations|storage`
- **Legacy URLs**: `?tab=info|guardrails|credentials|folders|access` resolve to their pane

## Frontend layer

| Piece | Role |
|---|---|
| `utils/agentTabs.js` | `buildTabs` drops Info/Credentials/Folders/Access; Settings is last; `TAB_ALIASES` maps the old ids to `settings` |
| `utils/agentSettings.js` | `SETTINGS_SECTIONS` (rail order, question, search keywords), `settingsSections({isSystem})`, `resolveSettingsSection(query)`, `LEGACY_TAB_SECTIONS`, `filterSections` |
| `views/AgentDetail.vue` | Renders two `OverflowTabs` (working strip + pinned Settings), slots the panes into `AgentSettingsHome`, owns the restart flow (`restartForPendingChanges`), reports memory/CPU and funding restarts |
| `components/agent-settings/AgentSettingsHome.vue` | Rail + "Find a setting" + phone-width select; `router.push` on pane change (Back steps panes) |
| `components/agent-settings/SettingsRestartBanner.vue` | The one restart banner; reads `stores/agentRestart.js`; same footprint when nothing waits |
| `components/agent-settings/SettingsSection.vue`, `SettingRow.vue` | Section heading + card; label/hint left, value/verb right, inline editor below |
| `GeneralPane.vue` | Identity (label, id rename, avatar, template description, tags), Model and funding, Execution (timeout #353, capacity, read-only, memory/CPU), Template (InfoPanel in a dialog), Danger zone |
| `GuardrailsPane.vue` | Guardrail rules (GuardrailsPanel embedded), self-change permissions (ent#756), gated-skills count → Skills tab, autonomy read-only, cross-model validation (entitled), reliability |
| `PeoplePane.vue` | `AccessPanel` embedded, unchanged |
| `StoragePane.vue` + `GitHubAuthRow.vue` | Repository (bound repo, BindRepoPanel, GitHub token, git sync), Credentials, Shared folders, File sharing |
| `ChannelsPane.vue` / `IntegrationsPane.vue` | Voice / Expose via MCP + Trinity access key, plus rows that open the tabs still holding the rest |
| ~~`components/AgentLiveStrip.vue`~~ | Deleted by trinity-enterprise#955 — live CPU/memory and the 7-day spend row render in the header again (rows 2 and 3 below) |
| `components/GitPanel.vue` | Gains Pull/Push (driven by AgentDetail's `useGitSync`); loses binding and token config |

## The header (trinity-enterprise#950)

The header is the one surface shown on every tab, so it carries what an operator checks before touching an agent, and nothing that has a home elsewhere.

| Piece | Shows | Data |
|---|---|---|
| `AgentHeader.vue` | Four rows since trinity-enterprise#955 (the pre-#3798 shape, almost whole). **1** Avatar (owner hover: generate / change prompt / next emotion → `open-avatar-modal` / `cycle-emotion`), name with the owner's pencil editing the display label in place (editor inside the `h1`, label never slug, Enter/blur save, Esc cancels, empty clears → `set-label`), slug, description, status / circuit / runtime / ghost / system / shared badges and the funding badge with an overlaid subscription `<select>` (→ `change-subscription`); right: Brain door, the activity + capacity indicator (`Working` / `Idle` badge, `CapacityMeter`, `active/max` — the dashboard's slot map, refreshed on the header's 15 s tick), context ring, Start/Stop. **2** Autonomy + Read-only toggles, tag chips, live CPU / MEM sparklines (no uptime, no gear). **3** 7-day spend row. **4** GitHub line when a repo is connected — no Pull / Push. The id rename stays in Settings only | `agent`, `hasAvatarPrompt`, `authStatus`, `subscriptions`, `agentStats`, `cpuHistory`, `memoryHistory`, `resourceLimits`, `tokenStats`, `gitStatus` |
| `AgentHeaderContext.vue` | Context-window ring + percent, running agents only; "—" until there is a reading (a 0 or `offline` probe is no reading) | `stores/network.js::contextStats[name]`, kept fresh by `composables/useAgentContextStats.js` (15 s, visibility-gated, no stacked requests); started/stopped in `AgentDetail`'s `startAllPolling` / `stopAllPolling` and status watcher because the page is KeepAlive-cached |
| `AgentHeaderMeta.vue` | One reserved-height line: the bound repo (linked via `utils/gitRemote.js::repoFromRemote`), branch, "N changed" (files) and "N ahead" (unpushed commits) as separate badges, "N behind", or "in sync" — below `sm` the branch hides and the counts show as ±N / ↑N / ↓N (words in the tooltip), and the counts wrap under the repo name rather than crush it; then two tag chips, a counted "+N" and the tag editor (`BaseModal` around `TagsEditor`, owners only) | `useGitSync().gitStatus` (shown only when `git_enabled` and an origin exists; cleared on agent switch and on stop), `agentTags` / `allTags`, the same `updateTags` / `addTag` / `removeTag` handlers Settings › General uses |

Removed from the header by #950: the **Workspace** and **Talk** doors. The Workspace is in the nav and the voice call is the Workspace composer's Call control (ent#547). Pull / Push / Refresh stay on the Git tab.

## Restart flow

1. A row learns a restart is due — from the backend (`restart_required` on file sharing / folders / api-key setting; memory/CPU override ≠ container label) or from a save that needs one (guardrails, GitHub token).
2. It calls `agentRestart.report(agent, key, label, true)`.
3. The banner lists the labels and offers **Restart now** (running) or **Start now** (stopped).
4. AgentDetail stops (waiting for a real stop, #1126) and starts the agent. The list is settled by the agent's **status edge** (stopped → running on the same agent), not by the button: `agentRestart.restarted(agent)` clears the list and bumps the epoch, and rows watching the epoch re-read the backend and re-report anything still out of step. So a manual stop/start from the header settles it too, and a start that fails (the lifecycle composable reports and returns normally) leaves every entry listed — nothing applied, nothing is cleared. Pinned by `tests/unit/agentSettingsRestart.mount.spec.js`.

## Backend

No backend change. Endpoints used: `GET/PUT /api/agents/{name}/timeout`, `/capacity`, `/resources`, `/read-only`, `/model`, `/guardrails`, `/skill-gates`, `/git/config`, `/github-pat`, `POST /git/reset-to-main-preserve-state`, plus the existing panels' own.

## Tests

- `tests/unit/agentSettingsHome.spec.js` — strip contents, alias resolution, pane visibility, search, restart registry
- `tests/unit/agentSettingsHome.mount.spec.js` — rail navigation through the URL, banner, timeout validation / save / #929 conflict
- `tests/unit/fileSharingPanelAudience.spec.js` — file sharing reports to the banner

## Not yet

- Channels (ent#340) and Integrations (step 3) panes in full; the Sharing, Permissions, A2A and Payments tabs remain
- The autonomy dial (ent#641, PR #2984) — Guardrails shows the switch read-only until it merges
- One shared, editable description (ent#584) — General shows the template's
