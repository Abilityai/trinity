/**
 * Agent name resolution (ent#181).
 *
 * An agent has two names and they are not interchangeable:
 *
 *  - `name` — the SLUG. The identity. Routes, Docker container + volume names
 *    and their immutable labels, MCP keys, A2A cards and every `agent_name`
 *    column key on it. Never render a decision off anything else.
 *  - `display_label` — a human-facing label an owner can change freely. NULL
 *    means "no label", not "empty name".
 *
 * One helper rather than `agent.display_label || agent.name` at each call site,
 * because a name resolved one way in the header and another in a list shows one
 * agent under two names with no way to tell which is real (requirements §1.3.1
 * FR-3). If the fallback ever changes, it changes here.
 */

/**
 * What a human should see for this agent: the label if it has one, else the slug.
 * @param {{name?: string, display_label?: string|null}|string|null|undefined} agent
 * @returns {string}
 */
export function agentDisplayName(agent) {
  if (!agent) return ''
  if (typeof agent === 'string') return agent
  const label = typeof agent.display_label === 'string' ? agent.display_label.trim() : ''
  return label || agent.name || ''
}

/**
 * True when the agent is rendered under a label, i.e. the slug is NOT what the
 * user sees. Surfaces use this to decide whether the slug needs showing too —
 * it is what URLs and keys use, so hiding it entirely trades one confusion for
 * another (§1.3.1 FR-4).
 * @param {{name?: string, display_label?: string|null}|null|undefined} agent
 * @returns {boolean}
 */
export function hasDistinctLabel(agent) {
  if (!agent || typeof agent === 'string') return false
  const label = typeof agent.display_label === 'string' ? agent.display_label.trim() : ''
  return Boolean(label) && label !== agent.name
}

/**
 * The two names a surface renders when it shows both: `{ primary, secondary }`.
 *
 *  - `primary` — what a human reads: the label if there is one, else the slug.
 *  - `secondary` — the slug, and ONLY when a distinct label would otherwise
 *    hide it; `null` when the primary already IS the slug, so a surface can
 *    render it without reserving a phantom second line (§1.3.1 FR-4).
 *
 * Composed from the two helpers above rather than re-deriving the rule, so
 * "which name wins" has exactly one definition (§1.3.1 FR-3). Call sites use
 * this instead of an inline `display_label || name` chain; a chain in one
 * component and this helper in another is how one agent ends up under two
 * names, which is the confusion the label was supposed to remove.
 *
 * `secondary` is either `null` or exactly `agent.name` — never the label. It is
 * the string a reader pastes into a URL, a `docker` command or an MCP key
 * lookup, so a presentation name appearing there would be worse than showing
 * nothing.
 *
 * Accepts the same inputs as `agentDisplayName`: an agent object, a bare slug
 * string (legacy callers → `{ primary: str, secondary: null }`), or
 * null/undefined (→ `{ primary: '', secondary: null }`).
 *
 * @param {{name?: string, display_label?: string|null}|string|null|undefined} agent
 * @returns {{primary: string, secondary: string|null}}
 */
export function agentNameParts(agent) {
  return {
    primary: agentDisplayName(agent),
    secondary: hasDistinctLabel(agent) ? agent.name : null,
  }
}

/**
 * Tooltip text for a name that may be truncated: shows the label AND the slug
 * when they differ, so the identity is always one hover away (§1.3.1 FR-4).
 *
 * This is a BELT, not the slug's home. It used to be the only place the slug
 * appeared on the dense surfaces (list row, grid tile), which is the shortcut
 * #2358 reverses — a `title` is invisible on touch, unreachable by keyboard and
 * impossible to copy from. Those surfaces now render the slug as real text via
 * `agentNameParts`; the tooltip still earns its place for a long label that
 * ellipsizes.
 * @param {{name?: string, display_label?: string|null}|null|undefined} agent
 * @returns {string}
 */
export function agentNameTooltip(agent) {
  const shown = agentDisplayName(agent)
  if (!hasDistinctLabel(agent)) return shown
  return `${shown} · ${agent.name}`
}

/**
 * Label for an agent in a `<select>`/picker `<option>` (#1642). An `<option>`
 * renders a single text line and can't carry a tooltip or a second field, so
 * disambiguation goes inline: `Display name (slug)` when a distinct label is
 * set, else the bare slug. The `<option>` **value** stays the slug — this is
 * the display text only. Pickers are the surface class where "which agent is
 * this?" matters most, hence the slug rides along here rather than being
 * dropped (§1.3.1 FR-4, the #964 render rule).
 * @param {{name?: string, display_label?: string|null}|string|null|undefined} agent
 * @returns {string}
 */
export function agentOptionLabel(agent) {
  const shown = agentDisplayName(agent)
  if (!hasDistinctLabel(agent)) return shown
  return `${shown} (${agent.name})`
}
