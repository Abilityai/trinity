/**
 * Which tabs the agent detail page renders, and in what order (#2153, ent#479).
 *
 * A PURE builder, so "which tabs exist" has exactly one definition. `?tab=`
 * used to resolve against a hand-maintained `DEEP_LINK_TABS` list that omitted
 * a2a, loops, playbooks, access and nevermined — real tabs whose deep links
 * silently landed on Overview. Two sources of truth for the same fact, and
 * only one of them was updated when a tab was added. Taking the flags as
 * arguments lets the deep-link resolver ask the same function for the SUPERSET
 * (every flag permissive) without a second list, so a tab added below is
 * deep-linkable the moment it appears.
 *
 * ent#479 lifted it out of `AgentDetail.vue`. It was tested by extracting the
 * function's TEXT from the SFC and `new Function`-ing it — a real execution,
 * but one that existed only because the spec could not import an SFC. A tab
 * gate is the kind of predicate #2918 says must be executed as shipped, so the
 * shipped thing is an importable module now and the spec imports it.
 */

/**
 * Legacy `?tab=` ids that moved or were renamed, so old deep links keep
 * working (#1108). trinity-enterprise#754: the Playbooks tab merged into
 * Skills — an old `?tab=playbooks` link lands on the tab that holds those
 * skills now. Exported so the resolver's data is executed by its spec rather
 * than matched out of the SFC's text.
 */
//
// trinity-enterprise#887/#888/#890/#891 (trinity#966): the tabs whose
// configuration moved into the Settings home alias to `settings`; the pane
// each one lands on is `LEGACY_TAB_SECTIONS` in `utils/agentSettings.js`.
export const TAB_ALIASES = Object.freeze({
  guardrails: 'settings',
  playbooks: 'skills',
  info: 'settings',
  credentials: 'settings',
  folders: 'settings',
  access: 'settings',
})

export function buildTabs({
  isSystem = false, hasDashboardFlag = false, hasDeclaredMetrics = false,
  brainOrbVisible = false, canShare = false, a2aVisible = false, gitSync = false,
} = {}) {

  // Primary tabs - most frequently used. Overview leads (#1107).
  const tabs = [
    { id: 'overview', label: 'Overview' },
    { id: 'tasks', label: 'Tasks' },
    { id: 'chat', label: 'Chat' }
  ]

  // #1112 collapsed the Session tab into the Chat tab above; ent#358 retired
  // the surface entirely — continuous conversation is the Workspace's job now.
  // The Chat tab keeps stateless per-turn chat and links across.

  // Dashboard tab — EITHER a `dashboard.yaml` or declared metrics (ent#479).
  //
  // Gating on `dashboard.yaml` alone hid the tab from exactly the agents
  // ent#439's "declared metrics are the default tiles" targets: an agent that
  // declares `metrics:` in template.yaml and records points has numbers to
  // show and no dashboard file to show them in. Both flags come from ONE
  // DB-only probe (`GET /api/agent-dashboard/{name}/exists`), so neither half
  // needs the agent to be running.
  //
  // trinity-enterprise#1006: an owner sees it with neither, because the
  // Dashboard tab is where metrics are declared from the UI — hiding it until
  // one exists would hide the only door to the first one.
  if (hasDashboardFlag || hasDeclaredMetrics || (canShare && !isSystem)) {
    tabs.push({ id: 'dashboard', label: 'Dashboard' })
  }

  // Brain Orb tab (#58) — platform flag AND per-agent capability. Selecting it
  // navigates to the dedicated /agents/:name/brain route (handled by a watcher).
  if (brainOrbVisible) {
    tabs.push({ id: 'brain', label: 'Brain' })
  }

  tabs.push(
    { id: 'reports', label: 'Reports' },  // #918 agent-published reports
    // ent#438 — the canvas is Reports' living sibling: same agent output,
    // one surface kept current instead of a record that accumulates. Next
    // to it deliberately, so the choice is visible where it is made.
    { id: 'canvas', label: 'Canvas' },
    { id: 'schedules', label: 'Schedules' },
    { id: 'loops', label: 'Loops' },
    // trinity-enterprise#754: ONE Skills tab — the agent's own skills and the
    // library's, with Run and Requires approval — in the slot the Playbooks
    // tab held, visible to everyone with access (the system agent included).
    // The management controls inside it are role-gated, not the tab.
    { id: 'skills', label: 'Skills' },
    // trinity-enterprise#888: Credentials is a section of Settings › Storage.
    { id: 'nevermined', label: 'Payments' }
  )

  // Access control tabs - hide for system agent (system agent has full access).
  // trinity-enterprise#891: the Access tab is Settings › People now.
  if (canShare && !isSystem) {
    tabs.push({ id: 'sharing', label: 'Sharing' })
    tabs.push({ id: 'permissions', label: 'Permissions' })
  }

  // A2A tab (trinity-enterprise#158) — owner-only, non-system, and only when the
  // enterprise A2A module is entitled (never a blank tab in OSS/unentitled).
  if (a2aVisible && canShare && !isSystem) {
    tabs.push({ id: 'a2a', label: 'A2A' })
  }

  // Git and Files tabs together
  if (gitSync) {
    tabs.push({ id: 'git', label: 'Git' })
  }
  // DEPRECATED: Terminal tab hidden for all users (candidate for removal)
  // tabs.push({ id: 'terminal', label: 'Terminal' })
  tabs.push({ id: 'files', label: 'Files' })

  // Settings — the one configuration home, last on the strip
  // (trinity-enterprise#887, trinity#966). Owner-only, as the Settings tab has
  // been since #1108. The system agent gets it too: the panes it may see are
  // decided by `settingsSections()` in `utils/agentSettings.js` (General and
  // Guardrails). Folders (→ Storage) and Info (→ General) are panes now.
  if (canShare) {
    tabs.push({ id: 'settings', label: 'Settings' })
  }

  return tabs
}
