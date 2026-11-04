/**
 * What a Confirm card does. Each change calls the same backend route as the main app's
 * own control, with the person's own session (`http` sends their platform token), once.
 * Then the page reads it back to check it took, and keeps the guide's fleet file fresh.
 * Nothing here ever runs with an agent's key.
 */
import { FLEET_STATE_FILE, KNOWN_CHANGES, fleetStateFor } from './fleetChanges'

const enc = encodeURIComponent
const agentPath = (name) => `/api/agents/${enc(name)}`

const CALLS = Object.freeze({
  'allow-call': (c) => ['post', `${agentPath(c.from)}/permissions/${enc(c.to)}`],
  'stop-call': (c) => ['delete', `${agentPath(c.from)}/permissions/${enc(c.to)}`],
  share: (c) => ['post', `${agentPath(c.agent)}/share`, { email: c.email }],
  unshare: (c) => ['delete', `${agentPath(c.agent)}/share/${enc(c.email)}`],
  'schedule-on': (c) => ['post', `${agentPath(c.agent)}/schedules/${enc(c.schedule_id)}/enable`],
  'schedule-off': (c) => ['post', `${agentPath(c.agent)}/schedules/${enc(c.schedule_id)}/disable`],
  'public-link': (c) => ['post', `${agentPath(c.agent)}/public-links`, { name: 'Public chat', link_type: 'chat' }],
  'add-skill': (c) => ['post', `${agentPath(c.agent)}/skills/${enc(c.skill)}`],
})

// A 4xx is Trinity refusing: nothing was changed. No answer, or a 5xx, may have been
// made anyway, so the page says it isn't sure rather than that nothing changed.
function failure(error) {
  const status = error?.response?.status
  if (!status) return { ok: false, refused: false, reason: "Couldn't reach Trinity, so it may not have been made." }
  if (status >= 500) return { ok: false, refused: false, reason: `Trinity had a problem (${status}), so it may not have been made.` }
  const detail = error.response.data?.detail
  return { ok: false, refused: true, reason: typeof detail === 'string' && detail ? detail : `Trinity refused it (${status}).` }
}

/** Makes one confirmed change. Anything but a known confirm card is refused here too. */
export async function runChange(http, card) {
  if (card?.kind !== 'confirm' || !KNOWN_CHANGES[card.action] || !CALLS[card.action]) {
    return { ok: false, refused: true, reason: "This page can't make that change." }
  }
  const [method, url, body] = CALLS[card.action](card)
  try {
    await http[method](url, body)
    return { ok: true }
  } catch (error) {
    return failure(error)
  }
}

async function read(http, url, fallback) {
  try {
    const { data } = await http.get(url)
    return data ?? fallback
  } catch {
    return fallback
  }
}

const permittedNames = (data) => (Array.isArray(data?.permitted_agents) ? data.permitted_agents.map((a) => a?.name).filter(Boolean) : [])
const shareEmails = (data) => (Array.isArray(data) ? data.map((s) => String(s?.shared_with_email || '').toLowerCase()).filter(Boolean) : [])
const skillNames = (data) => (Array.isArray(data) ? data.map((s) => s?.skill_name).filter(Boolean) : [])

/** Whether the change now holds on the instance, read with the person's session. */
export async function changeHeld(http, card) {
  switch (card?.action) {
    case 'allow-call': return permittedNames(await read(http, `${agentPath(card.from)}/permissions`, {})).includes(card.to)
    case 'stop-call': return !permittedNames(await read(http, `${agentPath(card.from)}/permissions`, {})).includes(card.to)
    case 'share': return shareEmails(await read(http, `${agentPath(card.agent)}/shares`, [])).includes(String(card.email).toLowerCase())
    case 'unshare': return !shareEmails(await read(http, `${agentPath(card.agent)}/shares`, [])).includes(String(card.email).toLowerCase())
    case 'schedule-on':
    case 'schedule-off': {
      const list = await read(http, `${agentPath(card.agent)}/schedules`, [])
      const found = Array.isArray(list) ? list.find((s) => s?.id === card.schedule_id) : null
      return Boolean(found) && found.enabled === (card.action === 'schedule-on')
    }
    case 'public-link': {
      const links = await read(http, `${agentPath(card.agent)}/public-links`, [])
      return Array.isArray(links) && links.length > 0
    }
    case 'add-skill': return skillNames(await read(http, `${agentPath(card.agent)}/skills`, [])).includes(card.skill)
    default: return false
  }
}

/** The person's own agents as the guide sees them, read with the person's session. */
export async function readFleet(http, owned = []) {
  const agents = (Array.isArray(owned) ? owned : []).slice(0, 20)
  const parts = { permissions: {}, shares: {}, schedules: {}, autonomy: {}, links: {}, skills: {} }
  await Promise.all(agents.map(async (agent) => {
    const base = agentPath(agent.name)
    const [permissions, shares, schedules, autonomy, links, skills] = await Promise.all([
      read(http, `${base}/permissions`, {}),
      read(http, `${base}/shares`, []),
      read(http, `${base}/schedules`, []),
      read(http, `${base}/autonomy`, {}),
      read(http, `${base}/public-links`, []),
      read(http, `${base}/skills`, []),
    ])
    parts.permissions[agent.name] = permittedNames(permissions)
    parts.shares[agent.name] = shareEmails(shares)
    parts.schedules[agent.name] = Array.isArray(schedules) ? schedules : []
    if (typeof autonomy?.autonomy_enabled === 'boolean') parts.autonomy[agent.name] = autonomy.autonomy_enabled
    parts.links[agent.name] = Array.isArray(links) ? links : []
    parts.skills[agent.name] = skillNames(skills)
  }))
  return fleetStateFor({ owned: agents, ...parts })
}

// Coming back to the Build tab rewrites the fleet file the guide reads: on every return for
// ten minutes after a Show me walk was opened (the change is made in that other tab), and
// otherwise at most every five minutes, since each read is several routes per agent.
// (The page also skips a return while the last one is still reading.)
export const FLEET_BACK_MS = 300_000
export const FLEET_WALK_MS = 600_000
export function fleetRefreshDue({ now, readAt = 0, walkOpenedAt = 0 }) {
  if (walkOpenedAt && now - walkOpenedAt < FLEET_WALK_MS) return true
  return now - readAt >= FLEET_BACK_MS
}

/** Into the person's own guide, through the ordinary files route. */
export async function writeFleetFile(http, guideName, fleet) {
  await http.put(`${agentPath(guideName)}/files?path=${enc(FLEET_STATE_FILE)}`, { content: JSON.stringify(fleet, null, 2) })
}

/** The line the guide is told after a card: it reacts to these four openings. */
export function outcomeMessage(card, outcome) {
  if (outcome === 'done') return `Done: ${card.words}.`
  if (outcome === 'declined') return `Not now: ${card.words}.`
  if (outcome?.refused === false) return `Not sure it worked: ${card.words}. ${outcome.reason || ''}`.trim()
  return `That didn't work: ${card.words}. ${outcome?.reason || ''}`.trim()
}
