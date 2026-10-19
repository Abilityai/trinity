/**
 * The agent Settings home (trinity-enterprise#887, trinity#966): which panes
 * the rail shows, and which pane a URL lands on.
 *
 * A PURE module for the same reason `agentTabs.js` is one (#2918): the rail's
 * gate and the deep-link resolver are predicates that must be executed by their
 * spec, not matched out of an SFC's text.
 *
 * The decision on trinity#966 names six panes, each answering one question.
 * A pane that has not landed yet is simply not in this list (step-by-step
 * delivery), and a pane whose rows are all gated away for this viewer is
 * hidden rather than shown blank.
 */

/**
 * Every pane, in rail order. `keywords` feed the rail's "Find a setting" box:
 * the words a person would type for a control that lives in the pane, so a
 * search for "timeout" finds General even though the word is not in its name.
 */
export const SETTINGS_SECTIONS = Object.freeze([
  {
    id: 'general',
    label: 'General',
    question: 'What this agent is',
    keywords: [
      'name', 'display name', 'label', 'id', 'rename', 'avatar', 'description', 'tags',
      'runtime', 'model', 'funding', 'subscription', 'api key', 'timeout', 'execution',
      'capacity', 'parallel', 'read-only', 'read only', 'memory', 'cpu', 'resources',
      'template', 'technical details', 'reset', 'delete', 'danger',
    ],
  },
  {
    id: 'guardrails',
    label: 'Guardrails',
    question: 'What it may do',
    keywords: [
      'guardrails', 'max turns', 'rules', 'permissions', 'change itself', 'self-change',
      'skills', 'approval', 'autonomy', 'unprompted', 'cross-model', 'validation',
      'circuit breaker', 'reliability',
    ],
  },
  {
    id: 'people',
    label: 'People',
    question: 'Who works with it',
    keywords: [
      'people', 'access', 'operators', 'share', 'primary', 'approver', 'collaborator',
      'viewer', 'role', 'invite', 'seat', 'members',
    ],
  },
  {
    id: 'channels',
    label: 'Channels',
    question: 'Where outsiders reach it',
    keywords: ['channels', 'voice', 'slack', 'telegram', 'whatsapp', 'public', 'sharing'],
  },
  {
    id: 'integrations',
    label: 'Integrations',
    question: 'What calls it, what it calls',
    keywords: [
      'integrations', 'mcp', 'expose', 'access key', 'key', 'client', 'permissions',
      'agent to agent', 'a2a', 'payments',
    ],
  },
  {
    id: 'storage',
    label: 'Storage',
    question: 'What it keeps and shares',
    keywords: [
      'storage', 'repository', 'repo', 'git', 'github', 'token', 'pat', 'auto-sync',
      'sync', 'freeze', 'credentials', 'secrets', 'env', 'folders', 'shared folders',
      'file sharing', 'files', 'download links',
    ],
  },
])

export const DEFAULT_SECTION = 'general'

/**
 * Where an old `?tab=` value lands inside Settings. Its counterpart is
 * `TAB_ALIASES` in `agentTabs.js`, which maps the same ids to the `settings`
 * tab; this decides the pane. Nothing bookmarked breaks.
 */
export const LEGACY_TAB_SECTIONS = Object.freeze({
  info: 'general',
  guardrails: 'guardrails',
  credentials: 'storage',
  folders: 'storage',
  access: 'people',
})

/**
 * The panes this viewer gets, in rail order.
 *
 * The system agent has no people, channels or storage of its own to manage
 * (its tabs for those were never shown), so it gets General, Guardrails and
 * Integrations — the migration-map ruling on trinity#966.
 */
export function settingsSections({ isSystem = false } = {}) {
  const systemPanes = ['general', 'guardrails', 'integrations']
  return SETTINGS_SECTIONS.filter((s) => !isSystem || systemPanes.includes(s.id))
}

/**
 * The pane a URL names: an explicit `section`, else the pane a legacy `tab`
 * aliases to, else General. A value naming no visible pane falls back to the
 * first visible one rather than to a blank pane.
 */
export function resolveSettingsSection(query = {}, sections = SETTINGS_SECTIONS) {
  const ids = sections.map((s) => s.id)
  const fallback = ids.includes(DEFAULT_SECTION) ? DEFAULT_SECTION : ids[0] || null
  const requested = query.section || LEGACY_TAB_SECTIONS[query.tab] || null
  return requested && ids.includes(requested) ? requested : fallback
}

/** Filter the rail by the "Find a setting" box. An empty query keeps every pane. */
export function filterSections(sections, query) {
  const q = String(query || '').trim().toLowerCase()
  if (!q) return sections
  return sections.filter((s) =>
    s.label.toLowerCase().includes(q)
    || s.question.toLowerCase().includes(q)
    || s.keywords.some((k) => k.includes(q))
  )
}
