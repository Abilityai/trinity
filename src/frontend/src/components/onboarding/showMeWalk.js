// Show me as a walk. Show me doesn't jump to the spot: it starts on the Dashboard and
// lights up each thing to click, in order, so the person learns the way there. Four walks:
// an agent's keys (`credentials`), sharing it with someone (`people`), letting it call
// another agent (`permissions`) and turning a schedule on (`schedules`). Kept apart from
// the component so a test can read every step without a browser.
//
// Each stop is marked on its page: `data-walk-agent="<name>"` on the agent's name in every
// Dashboard view, `data-walk="<id>"` on the other controls, `data-tab="<id>"` on the agent
// page's tabs, and one attribute per row the walk picks from (`data-walk-credential`,
// `data-walk-permission`, `data-walk-schedule`). A step is either done from the address
// (the agent's page, its tab, its section: `at`), by the person pressing Next, or when
// something appears or goes away (`until`). A step with `expand` is replaced by the steps
// for the rows the page shows. A tab the page has moved under More has an `alt`: More.

import { BUILD_GUIDE_NAME } from '@/components/portal/builder/portalBuild'

export const SHOW_ME_WALKS = Object.freeze(['credentials', 'people', 'permissions', 'schedules'])

// Trinity's agent names are Docker container names; key names are what an .env can hold.
// Both go into a selector, so nothing else gets through.
const AGENT_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/
const KEY_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/
// Trinity's schedule ids are url-safe tokens.
const SCHEDULE_ID = /^[A-Za-z0-9_-]{1,64}$/
const MAX_KEYS = 10
const QUERY = Object.freeze({ walk: 'walk', agent: 'walk_agent', keys: 'walk_keys', to: 'walk_to', schedule: 'walk_schedule' })

const isAgentName = (name) => typeof name === 'string' && AGENT_NAME.test(name) && !name.includes('..')
const isScheduleId = (id) => typeof id === 'string' && SCHEDULE_ID.test(id)
const agentPath = (agent) => `/agents/${encodeURIComponent(agent)}`
const tabPath = (agent, tab, section) => `${agentPath(agent)}?tab=${tab}${section ? `&section=${section}` : ''}`
const credentialsPath = (agent) => `${agentPath(agent)}?tab=settings&section=credentials`
// The credentials list marks itself ready once it has read the agent, so the walk knows
// whether it is stopped and which keys it has before choosing what to light up.
const CHECKLIST_READY = '[data-walk="credentials-checklist"][data-walk-ready="true"]'

/** The key names to light up, from a list or a string Atlas wrote: valid, once each, at most ten. */
export function walkKeys(raw) {
  const list = Array.isArray(raw) ? raw : typeof raw === 'string' ? raw.split(/[\s,]+/) : []
  const out = []
  for (const name of list) {
    if (typeof name === 'string' && KEY_NAME.test(name) && !out.includes(name)) out.push(name)
    if (out.length === MAX_KEYS) break
  }
  return out
}

// What a walk carries besides the agent, each only where it means something: key names
// for credentials, the agent to call for permissions, the schedule for schedules. Never an
// email or a value.
function walkExtras(key, agent, { keys, to, schedule } = {}) {
  const out = {}
  const names = key === 'credentials' ? walkKeys(keys) : []
  if (names.length) out.keys = names
  if (key === 'permissions' && isAgentName(to) && to !== agent) out.to = to
  if (key === 'schedules' && isScheduleId(schedule)) out.schedule = schedule
  return out
}

/**
 * Where a walk starts: the Dashboard, carrying the agent and what the walk lights up.
 * `extra` is the key names (a list or a string, for credentials) or `{ keys, to, schedule }`.
 */
export function walkLink(key, agent, extra = []) {
  if (!SHOW_ME_WALKS.includes(key) || !isAgentName(agent)) return null
  const given = Array.isArray(extra) || typeof extra === 'string' ? { keys: extra } : (extra || {})
  const { keys, to, schedule } = walkExtras(key, agent, given)
  const query = new URLSearchParams({ [QUERY.walk]: key, [QUERY.agent]: agent })
  if (keys) query.set(QUERY.keys, keys.join(','))
  if (to) query.set(QUERY.to, to)
  if (schedule) query.set(QUERY.schedule, schedule)
  return `/?${query}`
}

/** The walk an address asks for, or null. */
export function walkFromQuery(query) {
  const key = query?.[QUERY.walk]
  const agent = query?.[QUERY.agent]
  if (typeof key !== 'string' || !SHOW_ME_WALKS.includes(key) || !isAgentName(agent)) return null
  const extras = walkExtras(key, agent, { keys: query[QUERY.keys], to: query[QUERY.to], schedule: query[QUERY.schedule] })
  return { key, agent, keys: extras.keys || [], ...(extras.to ? { to: extras.to } : {}), ...(extras.schedule ? { schedule: extras.schedule } : {}) }
}

/** The address without the walk, so a reload doesn't start it again. */
export function withoutWalk(query = {}) {
  const rest = { ...query }
  for (const name of Object.values(QUERY)) delete rest[name]
  return rest
}

// The first stop of every walk: the agent, on the Dashboard.
const agentStep = (agent, name) => ({
  id: 'agent',
  target: `[data-walk-agent="${agent}"]`,
  title: 'Your agents live here',
  body: `This is the Dashboard: every agent you have is on it. Click ${name} to open it.`,
  fallback: { label: `Open ${name}`, to: agentPath(agent) },
})
const backToGuide = `Go back to your chat with ${BUILD_GUIDE_NAME} and tell it you're done.`

// A tab on the agent's page. When it doesn't fit, the page shows it under More.
const tabStep = (agent, tab, label, body) => ({
  id: 'tab',
  target: `[data-testid="agent-tabs"] [data-tab="${tab}"]`,
  alt: { target: '[data-testid="agent-tabs"] [data-overflow-trigger]', body: `It's under More. Click More, then ${label}.` },
  at: { tab },
  title: label,
  body,
  fallback: { label: `Open ${label}`, to: tabPath(agent, tab) },
})

/** The steps of the credentials walk. The `keys` step is filled in once the page shows the rows. */
export function credentialsWalk({ agent, label }) {
  const name = label || agent
  return [
    agentStep(agent, name),
    {
      id: 'settings',
      target: '[data-walk="settings-tab"]',
      title: 'Its settings',
      body: `How ${name} is set up lives under Settings, at the end of these tabs. Click it.`,
      fallback: { label: 'Open its Settings', to: `${agentPath(agent)}?tab=settings` },
    },
    {
      id: 'credentials',
      target: '[data-testid="settings-rail"] [data-section="credentials"]',
      title: 'Credentials',
      body: `The keys and addresses ${name} uses to reach your tools. They stay inside this agent. Click Credentials.`,
      fallback: { label: 'Open Credentials', to: credentialsPath(agent) },
    },
    {
      id: 'stopped',
      target: '[data-testid="credentials-stopped"]',
      title: `Start ${name} first`,
      body: 'Trinity only saves keys into a running agent. Start it at the top of this page and the boxes below open.',
      optional: true,
      until: 'gone',
      waitFor: CHECKLIST_READY,
    },
    { id: 'keys', expand: true, waitFor: CHECKLIST_READY, target: null, title: 'One moment', body: `Checking what ${name} needs.` },
    {
      id: 'finished',
      target: null,
      title: "That's it",
      body: `${name} has its keys. Go back to your chat with ${BUILD_GUIDE_NAME} and tell it you're done. It checks they're set, without ever seeing them.`,
    },
  ]
}

/** Sharing the agent with someone: Settings, People, then the email box and Add person. */
export function peopleWalk({ agent, label }) {
  const name = label || agent
  return [
    agentStep(agent, name),
    {
      id: 'settings',
      target: '[data-walk="settings-tab"]',
      at: { tab: 'settings' },
      title: 'Its settings',
      body: `Who can use ${name} is set under Settings, at the end of these tabs. Click it.`,
      fallback: { label: 'Open its Settings', to: tabPath(agent, 'settings') },
    },
    {
      id: 'people',
      target: '[data-testid="settings-rail"] [data-section="people"]',
      at: { tab: 'settings', section: 'people' },
      title: 'People',
      body: `The people ${name} is shared with. Click People.`,
      fallback: { label: 'Open People', to: tabPath(agent, 'settings', 'people') },
    },
    {
      id: 'add',
      target: '[data-walk="people-form"]',
      until: '[data-walk="people-added"]',
      title: 'Their email',
      body: `Type the email of the person who should use ${name}, then click Add person.`,
      fallback: { label: 'Open People', to: tabPath(agent, 'settings', 'people') },
    },
    {
      id: 'finished',
      target: null,
      end: true,
      title: "That's it",
      body: `${name} is shared with them. They find it in their Workspace when they sign in with that email. ${backToGuide}`,
    },
  ]
}

/** Letting the agent call another: Permissions, the agent to tick, then Save. */
export function permissionsWalk({ agent, label, to, toLabel }) {
  const name = label || agent
  const other = to ? toLabel || to : ''
  return [
    agentStep(agent, name),
    tabStep(agent, 'permissions', 'Permissions', `Which agents ${name} may call is set under Permissions. Click it.`),
    {
      id: 'rows',
      expand: 'permissions',
      waitFor: '[data-walk="permissions-list"], [data-walk="permissions-none"]',
      target: null,
      title: 'One moment',
      body: `Checking which agents ${name} may call.`,
      fallback: { label: 'Open Permissions', to: tabPath(agent, 'permissions') },
    },
    {
      id: 'finished',
      target: null,
      end: true,
      title: "That's it",
      body: `${name} may now call ${other || 'the agents you ticked'}. ${backToGuide}`,
    },
  ]
}

/** Turning a schedule on: Schedules, then its switch and Autonomy (`scheduleSteps`). */
export function schedulesWalk({ agent, label }) {
  const name = label || agent
  return [
    agentStep(agent, name),
    tabStep(agent, 'schedules', 'Schedules', `When ${name} works on its own is set under Schedules. Click it.`),
    {
      id: 'rows',
      expand: 'schedules',
      waitFor: '[data-tour="showme-schedules"]',
      target: null,
      title: 'One moment',
      body: `Checking ${name}'s schedules.`,
      fallback: { label: 'Open Schedules', to: tabPath(agent, 'schedules') },
    },
  ]
}

/** The steps of a walk read from an address (`walkFromQuery`), for the agent's names. */
export function walkSteps(walk, who) {
  if (walk?.key === 'people') return peopleWalk(who)
  if (walk?.key === 'permissions') return permissionsWalk({ ...who, to: walk.to })
  if (walk?.key === 'schedules') return schedulesWalk(who)
  return credentialsWalk(who)
}

/**
 * The steps for the agents it may call, from the rows the page shows (`{ name, checked }`):
 * the one named, to tick then Save; already ticked, say so; none named, the whole list.
 */
export function permissionSteps(rows, to, { agent, label, toLabel }) {
  const name = label || agent
  const list = Array.isArray(rows) ? rows : []
  const fallback = { label: 'Open Permissions', to: tabPath(agent, 'permissions') }
  const save = {
    id: 'save',
    target: '[data-walk="permissions-save"]',
    until: '[data-walk="permissions-saved"]',
    title: 'Save them',
    body: `Click Save Permissions. ${name} can call it from its next job.`,
    // Save is greyed out until something changes, so the walk would wait for ever.
    idle: {
      when: '[data-walk="permissions-save"][data-walk-idle="true"]',
      title: 'Nothing to save',
      body: "Nothing has changed here yet. Tick an agent first, or press Done if it's already how you want it.",
    },
    fallback,
  }
  if (!list.length) {
    return [{
      id: 'none',
      target: '[data-walk="permissions-none"]',
      end: true,
      title: 'No other agent yet',
      body: `${name} can only call your other agents, and you don't have another one yet. Ask ${BUILD_GUIDE_NAME} to build the next one with you.`,
    }]
  }
  const row = to ? list.find((r) => r.name === to) : null
  if (!row) {
    return [
      { id: 'pick', target: '[data-walk="permissions-list"]', next: true, title: 'Tick the ones it may call', body: `Tick each agent ${name} may call.`, fallback },
      save,
    ]
  }
  const other = toLabel || to
  if (String(row.checked) === 'true') {
    return [{ id: 'already', target: `[data-walk-permission="${to}"]`, next: true, title: 'Already allowed', body: `${name} may already call ${other}.`, fallback }]
  }
  return [
    {
      id: 'tick',
      target: `[data-walk-permission="${to}"]`,
      until: `[data-walk-permission="${to}"][data-walk-checked="true"]`,
      title: `Tick ${other}`,
      body: `Tick ${other}, so ${name} may call it.`,
      fallback,
    },
    save,
  ]
}

/**
 * The steps for the schedule to turn on, from the rows the page shows (`{ id, enabled, name }`):
 * the one named (or, when none was named, the only one), its switch; already on, say so;
 * otherwise the whole list. Then Enable autonomy while it is off, and a last word that
 * claims only what the walk saw.
 */
export function scheduleSteps(rows, schedule, { agent, label }) {
  const name = label || agent
  const list = Array.isArray(rows) ? rows : []
  const fallback = { label: 'Open Schedules', to: tabPath(agent, 'schedules') }
  if (!list.length) {
    return [{
      id: 'none',
      target: '[data-tour="showme-schedules"]',
      end: true,
      title: 'No schedule yet',
      body: `${name} has no schedule yet. Ask ${BUILD_GUIDE_NAME} to plan one with you.`,
    }]
  }
  const autonomy = {
    id: 'autonomy',
    target: '[data-walk="autonomy-enable"]',
    optional: true,
    until: 'gone',
    waitFor: '[data-walk-autonomy]',
    title: 'Turn on Autonomy',
    body: `Autonomy is ${name}'s switch for working on its own. While it's off, none of its schedules run. Click Enable autonomy.`,
    fallback,
  }
  const last = (body) => ({ id: 'finished', target: null, end: true, title: "That's it", body: `${body} ${backToGuide}` })
  // A schedule named that isn't there any more is never swapped for another one.
  const row = list.find((r) => r.id === schedule) || (!schedule && list.length === 1 ? list[0] : null)
  if (!row || !isScheduleId(row.id)) {
    return [
      {
        id: 'pick',
        target: '[data-tour="showme-schedules"]',
        next: true,
        title: 'Pick one',
        body: 'Each schedule has its own switch, on the right of its row. Click the switch of the one to turn on, then Next.',
        fallback,
      },
      autonomy,
      last(`Any of ${name}'s schedules that are on run at the time shown on their row, while Autonomy is on.`),
    ]
  }
  const called = row.name || 'This schedule'
  const done = last(`${name} now runs ${called} on its own, at the next time shown on its row.`)
  if (String(row.enabled) === 'true') {
    return [{ id: 'already', target: `[data-walk-schedule="${row.id}"]`, next: true, title: 'Already on', body: `${called} is already on.`, fallback }, autonomy, done]
  }
  return [
    {
      id: 'switch',
      target: `[data-walk-schedule="${row.id}"] [data-walk="schedule-toggle"]`,
      until: `[data-walk-schedule="${row.id}"][data-walk-enabled="true"]`,
      title: `Turn on ${called}`,
      body: 'Click its switch, on the right of its row. The row then says Active.',
      fallback,
    },
    autonomy,
    done,
  ]
}

/** Whether the address already shows a step as done, so the walk moves on by itself. */
export function stepDone(step, route, agent) {
  const onAgent = route?.path === agentPath(agent)
  const query = route?.query || {}
  if (step?.id === 'agent') return onAgent
  if (step?.at) return onAgent && query.tab === step.at.tab && (!step.at.section || query.section === step.at.section)
  if (step?.id === 'settings') return onAgent && (query.tab === 'settings' || query.tab === 'credentials')
  if (step?.id === 'credentials') return onAgent && (query.section === 'credentials' || (query.tab === 'credentials' && !query.section))
  return false
}

/**
 * Which rows to light up, from the rows the page shows (`{ name, status, required }`):
 * the keys Atlas named that aren't set; else every missing required key; else any missing
 * one. No rows to match means the paste box instead.
 */
export function pickRows(rows, wanted = []) {
  const list = Array.isArray(rows) ? rows : []
  const asked = walkKeys(wanted)
  const candidates = asked.length ? list.filter((r) => asked.includes(r.name)) : list
  if (!candidates.length) return { mode: 'paste', names: asked }
  const missing = candidates.filter((r) => r.status !== 'set')
  const required = missing.filter((r) => String(r.required) === 'true')
  const names = (asked.length || !required.length ? missing : required).map((r) => r.name)
  return names.length ? { mode: 'rows', names } : { mode: 'all-set', names: [] }
}

/** The steps for the keys themselves, from `pickRows`. */
export function keySteps({ mode, names = [] }, { agent, label }) {
  const name = label || agent
  const fallback = { label: 'Open Credentials', to: credentialsPath(agent) }
  if (mode === 'all-set') {
    return [{
      id: 'all-set',
      target: '[data-walk="credentials-checklist"]',
      title: 'Already set',
      body: `Everything ${name} needs here is already set.`,
      next: true,
      fallback,
    }]
  }
  if (mode === 'paste') {
    return [
      {
        id: 'paste',
        target: '[data-walk="quick-inject"]',
        title: 'Paste them here',
        body: names.length
          ? `Put each one on its own line, as NAME=value: ${names.join(', ')}. Never paste them in a chat.`
          : 'Put each one on its own line, as NAME=value. Never paste them in a chat.',
        next: true,
        fallback,
      },
      {
        id: 'save',
        target: '[data-walk="quick-inject-save"]',
        title: 'Save them',
        body: `Click Inject. They go into ${name} only, and it uses them from its next job.`,
        until: '[data-walk="credentials-saved"]',
        fallback,
      },
    ]
  }
  return [
    ...names.map((key, i) => ({
      id: `key-${key}`,
      target: `[data-walk-credential="${key}"]`,
      title: key,
      body: i === 0
        ? 'Paste its value in this box. The lines above it say what it is and where to get it. Never paste it in a chat.'
        : 'Then this one, the same way.',
      next: true,
      fallback,
    })),
    {
      id: 'save',
      target: '[data-walk="credentials-save"]',
      title: 'Save them',
      body: `Click Save credentials. They go into ${name} only, and it uses them from its next job.`,
      until: '[data-walk="credentials-saved"]',
      fallback,
    },
  ]
}
