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
        .map((s) => ({ id: s.id, name: s.name, enabled: Boolean(s.enabled), cron: s.cron_expression || '', timezone: s.timezone || null })),
    }))
}

/** The person's time zone and wall-clock time, so "at 9" means their 9. */
export function localClock(now = new Date()) {
  try {
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone
    if (!timezone) return {}
    const local_time = new Intl.DateTimeFormat('en-GB', {
      timeZone: timezone, weekday: 'short', year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(now)
    return { timezone, local_time }
  } catch {
    return {}                                   // no zone is better than a wrong one
  }
}

/** What goes with a message: the page, each agent's name and status, and the person's clock. */
export function buildContext(route, fleet, clock = localClock()) {
  return {
    page: describePage(route).label,
    fleet: (fleet || []).map((a) => ({ name: a.name, status: a.status })),
    ...clock,
  }
}
