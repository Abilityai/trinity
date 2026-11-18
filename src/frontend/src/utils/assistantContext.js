/**
 * What the assistant is told with every message.
 *
 * The task endpoint is stateless, so the page carries the conversation, and it
 * adds what the person is looking at and the state of their fleet — all read
 * with the person's own session. The assistant's own key sees none of this.
 */

export const MAX_HISTORY_TURNS = 12
const MAX_TURN_CHARS = 1500

const PAGE_NAMES = Object.freeze({
  '/': 'Dashboard',
  '/agents': 'Agents list',
  '/templates': 'Templates',
  '/operations': 'Operations',
  '/settings': 'Settings',
  '/sessions': 'Sessions',
  '/library': 'Library',
})

/** A one-line description of where the person is. `route` is a vue-router route. */
export function describePage(route) {
  const path = route?.path || '/'
  const agent = route?.params?.name
  if (agent && path.startsWith('/agents/')) {
    const tab = route.query?.tab
    return { label: `Agent page for ${agent}${tab ? `, ${tab} tab` : ''}`, agent: String(agent) }
  }
  const known = PAGE_NAMES[path] || PAGE_NAMES[`/${path.split('/')[1] || ''}`]
  return { label: known || `Page ${path}`, agent: null }
}

/** The fleet as the page read it, joined into one list the cards can validate against. */
export function buildFleet(agents, autonomyByName, schedules, selfName) {
  return (agents || [])
    .filter((a) => a?.name && a.name !== selfName && !a.is_system)
    .map((a) => ({
      name: a.name,
      status: a.status || 'unknown',
      autonomy: autonomyByName?.[a.name]?.autonomy_enabled ?? null,
      schedules: (schedules || [])
        .filter((s) => s?.agent_name === a.name)
        .map((s) => ({ id: s.id, name: s.name, enabled: Boolean(s.enabled), cron: s.cron_expression || '' })),
    }))
}

function fleetLines(fleet) {
  if (!fleet.length) return ['(no agents yet)']
  return fleet.map((a) => {
    const autonomy = a.autonomy === null ? 'autonomy unknown' : a.autonomy ? 'autonomy on' : 'autonomy off'
    const schedules = a.schedules.length
      ? a.schedules
          .map((s) => `${JSON.stringify(s.name)} id=${s.id} ${s.enabled ? 'on' : 'off'}${s.cron ? ` cron=${JSON.stringify(s.cron)}` : ''}`)
          .join('; ')
      : 'no schedules'
    return `- ${a.name}: ${a.status}, ${autonomy}, schedules: ${schedules}`
  })
}

const clip = (text) => {
  const s = String(text || '')
  return s.length > MAX_TURN_CHARS ? `${s.slice(0, MAX_TURN_CHARS)}…` : s
}

/**
 * The full prompt for one turn. `history` is the turns BEFORE this message:
 * [{ role: 'user' | 'assistant', content }].
 */
export function buildPrompt({ page, fleet, history, message }) {
  const parts = ['Page: ' + page.label, '', 'Fleet:', ...fleetLines(fleet || [])]
  const turns = (history || []).filter((m) => m.role === 'user' || m.role === 'assistant').slice(-MAX_HISTORY_TURNS)
  if (turns.length) {
    parts.push('', 'Conversation so far:')
    for (const m of turns) parts.push(`${m.role === 'user' ? 'Person' : 'You'}: ${clip(m.content)}`)
  }
  parts.push('', 'Current message:', String(message || ''))
  return parts.join('\n')
}
