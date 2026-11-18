/**
 * What the assistant is told about where the person is.
 *
 * Read with the person's own session and sent with each message: the page
 * they are on and the agents they have. The page also keeps this reading to
 * check every change the assistant proposes against it.
 */

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
export function buildFleet(agents, autonomyByName, schedules) {
  return (agents || [])
    .filter((a) => a?.name && !a.is_system)
    .map((a) => ({
      name: a.name,
      status: a.status || 'unknown',
      autonomy: autonomyByName?.[a.name]?.autonomy_enabled ?? null,
      schedules: (schedules || [])
        .filter((s) => s?.agent_name === a.name)
        .map((s) => ({ id: s.id, name: s.name, enabled: Boolean(s.enabled), cron: s.cron_expression || '' })),
    }))
}

/** What goes with a message: the page, and each agent's name and status. */
export function buildContext(route, fleet) {
  return {
    page: describePage(route).label,
    fleet: (fleet || []).map((a) => ({ name: a.name, status: a.status })),
  }
}
