/**
 * Settings changes in the chat. Pure.
 *
 * The guide can't read most agents with its own key, and it never changes a setting
 * with it. So the page writes the person's fleet into the guide's workspace
 * (`fleet-state.json`), the guide proposes one change on its `fleet` canvas, and the
 * page turns a proposal into a card only when it is exactly a change this table knows,
 * on the person's own agents. The card's words come from here, built from the change
 * itself: free text from the guide is never what Confirm says it does.
 */
import { BUILD_GUIDE_NAME, isBuildCrew } from './portalBuild'
import { walkKeys } from '@/components/onboarding/showMeWalk'

export const FLEET_CANVAS_ID = 'fleet'
export const FLEET_PROPOSAL_BLOCK = 'proposal'
export const FLEET_STATE_FILE = 'fleet-state.json'
const MAX_AGENTS = 20

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const SKILL_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/i

const label = (fleet, name) => fleet.agents.find((a) => a.name === name)?.label || name
const owns = (fleet, name) => fleet.agents.some((a) => a.name === name)
const schedule = (fleet, agent, id) => fleet.agents.find((a) => a.name === agent)?.schedules.find((s) => s.id === id)

// Each known change: the fields it needs, any further check, the card's words, and
// where Show me takes the person for it. A change that turns something on also has
// `walk`: what "See where this is set" walks them to (`showMeWalk.js`). One that turns
// something off shows the spot instead, since the walk teaches turning it on; so does a
// share, whose walk ends on adding the person, which Trinity refuses a second time.
export const KNOWN_CHANGES = Object.freeze({
  'allow-call': Object.freeze({
    fields: ['from', 'to'], mine: ['from', 'to'], showMe: 'permissions',
    valid: (p) => p.from !== p.to,
    words: (p, f) => `Let ${label(f, p.from)} call ${label(f, p.to)}`,
    walk: (p) => ({ to: p.to }),
  }),
  'stop-call': Object.freeze({
    fields: ['from', 'to'], mine: ['from', 'to'], showMe: 'permissions',
    valid: (p) => p.from !== p.to,
    words: (p, f) => `Stop ${label(f, p.from)} calling ${label(f, p.to)}`,
  }),
  share: Object.freeze({
    fields: ['agent', 'email'], mine: ['agent'], showMe: 'people',
    valid: (p) => EMAIL_RE.test(p.email),
    words: (p, f) => `Share ${label(f, p.agent)} with ${p.email}`,
  }),
  unshare: Object.freeze({
    fields: ['agent', 'email'], mine: ['agent'], showMe: 'people',
    valid: (p) => EMAIL_RE.test(p.email),
    words: (p, f) => `Stop sharing ${label(f, p.agent)} with ${p.email}`,
  }),
  'schedule-on': Object.freeze({
    fields: ['agent', 'schedule_id'], mine: ['agent'], showMe: 'schedules',
    valid: (p, f) => Boolean(schedule(f, p.agent, p.schedule_id)),
    words: (p, f) => `Turn on ${schedule(f, p.agent, p.schedule_id).name} for ${label(f, p.agent)}`,
    walk: (p) => ({ schedule: p.schedule_id }),
  }),
  'schedule-off': Object.freeze({
    fields: ['agent', 'schedule_id'], mine: ['agent'], showMe: 'schedules',
    valid: (p, f) => Boolean(schedule(f, p.agent, p.schedule_id)),
    words: (p, f) => `Turn off ${schedule(f, p.agent, p.schedule_id).name} for ${label(f, p.agent)}`,
  }),
  'public-link': Object.freeze({
    fields: ['agent'], mine: ['agent'], showMe: 'public-links',
    valid: () => true,
    words: (p, f) => `Create a public chat link for ${label(f, p.agent)}`,
  }),
  'add-skill': Object.freeze({
    fields: ['agent', 'skill'], mine: ['agent'], showMe: 'skills',
    valid: (p) => SKILL_RE.test(p.skill),
    words: (p, f) => `Add the ${p.skill} skill to ${label(f, p.agent)}`,
  }),
})

// Where Show me can go, and what each place is called on a card. `keys` is for the
// whole instance; every other place is on one agent's page. `credentials`, `people`,
// `permissions` and `schedules` are walks from the Dashboard (`showMeWalk.js`), not a
// jump to the spot.
export const SHOW_ME_KEYS = Object.freeze(['permissions', 'people', 'schedules', 'public-links', 'skills', 'channels', 'keys', 'credentials'])
const SHOW_ME_THING = Object.freeze({
  permissions: 'permissions', people: 'sharing', schedules: 'schedules',
  'public-links': 'public links', skills: 'skills', channels: 'channels',
})

const CANT = `${BUILD_GUIDE_NAME} suggested a change this page can't make.`
const NOT_YOURS = "That agent isn't one of yours."

const names = (list) => (Array.isArray(list) ? list.filter((n) => typeof n === 'string') : [])

/**
 * What goes in `fleet-state.json`: the person's own agents (no guide), with who each
 * may call, who it is shared with, its schedules, whether Autonomy is on (its schedules
 * only run while it is; null when it couldn't be read), public links and skills.
 */
export function fleetStateFor({ owned = [], permissions = {}, shares = {}, schedules = {}, autonomy = {}, links = {}, skills = {} } = {}, now = Date.now) {
  const agents = (Array.isArray(owned) ? owned : [])
    .filter((a) => a?.name && !isBuildCrew(a.name))
    .slice(0, MAX_AGENTS)
    .map((a) => ({
      name: a.name,
      label: a.display_label || a.name,
      may_call: names(permissions[a.name]),
      shared_with: names(shares[a.name]),
      schedules: (Array.isArray(schedules[a.name]) ? schedules[a.name] : [])
        .filter((s) => s && typeof s.id === 'string')
        .map((s) => ({ id: s.id, name: typeof s.name === 'string' && s.name ? s.name : s.id, enabled: s.enabled === true })),
      autonomy: typeof autonomy[a.name] === 'boolean' ? autonomy[a.name] : null,
      public_links: Array.isArray(links[a.name]) ? links[a.name].length : 0,
      skills: names(skills[a.name]),
    }))
  return { updated_at: new Date(now()).toISOString(), agents }
}

/** The guide's proposal on its `fleet` canvas, or null. Only its string fields count. */
export function proposalFrom(canvas) {
  const blocks = Array.isArray(canvas?.blocks) ? canvas.blocks : []
  const raw = blocks.find((b) => b?.id === FLEET_PROPOSAL_BLOCK && b?.kind === 'json')?.payload
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  if (typeof raw.id !== 'string' || !raw.id || raw.id.length > 80 || typeof raw.action !== 'string') return null
  return Object.fromEntries(Object.entries(raw).filter(([, v]) => typeof v === 'string'))
}

/**
 * The card for a proposal: a change to confirm, a Show me, or a refusal saying why.
 * Fields the change doesn't use are ignored, so they never reach the person.
 */
export function cardFor(proposal, fleet) {
  if (!proposal?.id) return null
  const { id, action } = proposal
  const refuse = (reason, showMe) => (showMe ? { kind: 'refused', id, reason, showMe } : { kind: 'refused', id, reason })
  const text = (field) => (typeof proposal[field] === 'string' && proposal[field] ? proposal[field] : '')

  if (action === 'show-me') {
    const key = text('key')
    if (!SHOW_ME_KEYS.includes(key)) return refuse(CANT)
    if (key === 'keys') return { kind: 'show-me', id, key, words: 'Show me where the API keys are set' }
    const agent = text('agent')
    if (!agent || !owns(fleet, agent)) return refuse(NOT_YOURS)
    if (key === 'credentials') {
      return { kind: 'show-me', id, key, agent, keys: walkKeys(text('keys')), words: `Show me how to add ${label(fleet, agent)}'s credentials` }
    }
    if (key === 'people') return { kind: 'show-me', id, key, agent, words: `Show me how to share ${label(fleet, agent)} with someone` }
    if (key === 'permissions') {
      const to = text('to')
      if (!to) return { kind: 'show-me', id, key, agent, words: `Show me how to choose which agents ${label(fleet, agent)} may call` }
      if (!owns(fleet, to)) return refuse(NOT_YOURS)
      if (to === agent) return refuse(CANT)
      return { kind: 'show-me', id, key, agent, to, words: `Show me how to let ${label(fleet, agent)} call ${label(fleet, to)}` }
    }
    if (key === 'schedules') {
      const scheduleId = text('schedule_id')
      if (!scheduleId) return { kind: 'show-me', id, key, agent, words: `Show me how to turn on one of ${label(fleet, agent)}'s schedules` }
      const found = schedule(fleet, agent, scheduleId)
      if (!found) return refuse(CANT)
      return { kind: 'show-me', id, key, agent, schedule: scheduleId, words: `Show me how to turn on ${label(fleet, agent)}'s ${found.name}` }
    }
    return { kind: 'show-me', id, key, agent, words: `Show me where ${label(fleet, agent)}'s ${SHOW_ME_THING[key]} are set` }
  }

  const change = KNOWN_CHANGES[action]
  if (!change) return refuse(CANT)
  if (change.fields.some((field) => !text(field))) return refuse(CANT)
  // Trinity stores an email in lower case, so the card and the check use it that way.
  const picked = Object.fromEntries(change.fields.map((field) => [field, field === 'email' ? text(field).toLowerCase() : text(field)]))
  if (change.mine.some((field) => !owns(fleet, picked[field]))) return refuse(NOT_YOURS, change.showMe)
  if (!change.valid(picked, fleet)) return refuse(CANT)
  return { kind: 'confirm', id, action, ...picked, words: change.words(picked, fleet), showMe: change.showMe, walk: change.walk ? change.walk(picked) : null }
}
