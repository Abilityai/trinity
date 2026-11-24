/**
 * Confirm cards for the assistant panel.
 *
 * The assistant cannot change a setting. It PROPOSES a change; the page turns
 * the proposal into a card, and on Confirm makes the change through the same
 * route the app's own control uses, with the person's session. Then it reads
 * the result back.
 *
 * A proposal is written by a model, so nothing in it is trusted: it is
 * accepted only when its action is in this catalog, it carries exactly the
 * fields that action takes, and every agent it names is one the page itself
 * read from the instance.
 *
 * Some cards are not in the catalog because the panel handles them itself:
 * `build` (a design the person approves), `upgrade` (the subscription),
 * `choice` (a question answered with a click), `feedback` (a note to the
 * makers of Trinity, sent on the person's click) and `credential` (a masked
 * field whose value goes to this instance and never to the assistant), and
 * `local-move` (the steps that bring in agents run on the person's own machine,
 * written by the page: trinity-enterprise#1353).
 */
import { instanceCredentialName } from './firstSession'

const enc = encodeURIComponent
const agentPath = (name) => `/api/agents/${enc(name)}`

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export const CARD_ACTIONS = Object.freeze({
  'schedule-on': {
    fields: ['agent', 'schedule_id'],
    label: (c, f) => `Turn on the schedule "${scheduleName(c, f)}" for ${c.agent}`,
    call: (c) => ['post', `${agentPath(c.agent)}/schedules/${enc(c.schedule_id)}/enable`],
    held: async (http, c) => (await schedule(http, c))?.enabled === true,
  },
  'schedule-off': {
    fields: ['agent', 'schedule_id'],
    label: (c, f) => `Turn off the schedule "${scheduleName(c, f)}" for ${c.agent}`,
    call: (c) => ['post', `${agentPath(c.agent)}/schedules/${enc(c.schedule_id)}/disable`],
    held: async (http, c) => (await schedule(http, c))?.enabled === false,
  },
  'autonomy-on': {
    fields: ['agent'],
    label: (c) => `Switch Autonomy on for ${c.agent}`,
    call: (c) => ['put', `${agentPath(c.agent)}/autonomy`, { enabled: true }],
    held: async (http, c) => (await read(http, `${agentPath(c.agent)}/autonomy`))?.autonomy_enabled === true,
  },
  'autonomy-off': {
    fields: ['agent'],
    label: (c) => `Switch Autonomy off for ${c.agent}`,
    call: (c) => ['put', `${agentPath(c.agent)}/autonomy`, { enabled: false }],
    held: async (http, c) => (await read(http, `${agentPath(c.agent)}/autonomy`))?.autonomy_enabled === false,
  },
  'start-agent': {
    fields: ['agent'],
    label: (c) => `Start ${c.agent}`,
    call: (c) => ['post', `${agentPath(c.agent)}/start`, {}],
    held: async (http, c) => (await read(http, agentPath(c.agent)))?.status === 'running',
  },
  'stop-agent': {
    fields: ['agent'],
    label: (c) => `Stop ${c.agent}`,
    call: (c) => ['post', `${agentPath(c.agent)}/stop`, {}],
    held: async (http, c) => {
      const status = (await read(http, agentPath(c.agent)))?.status
      return Boolean(status) && status !== 'running'
    },
  },
  share: {
    fields: ['agent', 'email'],
    label: (c) => `Share ${c.agent} with ${c.email}`,
    call: (c) => ['post', `${agentPath(c.agent)}/share`, { email: c.email }],
    held: async (http, c) => shareEmails(await read(http, `${agentPath(c.agent)}/shares`)).includes(c.email.toLowerCase()),
  },
  unshare: {
    fields: ['agent', 'email'],
    label: (c) => `Stop sharing ${c.agent} with ${c.email}`,
    call: (c) => ['delete', `${agentPath(c.agent)}/share/${enc(c.email)}`],
    held: async (http, c) => {
      const shares = await read(http, `${agentPath(c.agent)}/shares`)
      return Array.isArray(shares) && !shareEmails(shares).includes(c.email.toLowerCase())
    },
  },
  'allow-call': {
    fields: ['from', 'to'],
    label: (c) => `Let ${c.from} call ${c.to}`,
    call: (c) => ['post', `${agentPath(c.from)}/permissions/${enc(c.to)}`],
    held: async (http, c) => permitted(await read(http, `${agentPath(c.from)}/permissions`)).includes(c.to),
  },
  'stop-call': {
    fields: ['from', 'to'],
    label: (c) => `Stop ${c.from} calling ${c.to}`,
    call: (c) => ['delete', `${agentPath(c.from)}/permissions/${enc(c.to)}`],
    held: async (http, c) => {
      const data = await read(http, `${agentPath(c.from)}/permissions`)
      return data !== null && !permitted(data).includes(c.to)
    },
  },
})

const AGENT_FIELDS = ['agent', 'from', 'to']

async function read(http, url) {
  try {
    const { data } = await http.get(url)
    return data ?? null
  } catch {
    return null
  }
}

async function schedule(http, c) {
  const list = await read(http, `${agentPath(c.agent)}/schedules`)
  return Array.isArray(list) ? list.find((s) => s?.id === c.schedule_id) || null : null
}

const shareEmails = (data) =>
  Array.isArray(data) ? data.map((s) => String(s?.shared_with_email || '').toLowerCase()).filter(Boolean) : []

const permitted = (data) =>
  Array.isArray(data?.permitted_agents) ? data.permitted_agents.map((a) => a?.name).filter(Boolean) : []

function scheduleName(card, fleet) {
  const agent = (fleet || []).find((a) => a.name === card.agent)
  return agent?.schedules?.find((s) => s.id === card.schedule_id)?.name || card.schedule_id
}

/**
 * Whether one proposal may become a card. `fleet` is the page's own reading of
 * the instance: [{ name, schedules: [{ id, name }] }].
 */
export function validCard(raw, fleet) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const spec = Object.hasOwn(CARD_ACTIONS, raw.action) ? CARD_ACTIONS[raw.action] : null
  if (!spec) return null

  const keys = Object.keys(raw).filter((k) => k !== 'action').sort()
  if (keys.join() !== [...spec.fields].sort().join()) return null
  if (!spec.fields.every((f) => typeof raw[f] === 'string' && raw[f].trim() && raw[f].length <= 200)) return null

  const names = new Set((fleet || []).map((a) => a.name))
  for (const field of AGENT_FIELDS) {
    if (spec.fields.includes(field) && !names.has(raw[field])) return null
  }
  if (spec.fields.includes('from') && raw.from === raw.to) return null
  if (spec.fields.includes('email') && !EMAIL.test(raw.email)) return null
  if (spec.fields.includes('schedule_id')) {
    const agent = (fleet || []).find((a) => a.name === raw.agent)
    if (!agent?.schedules?.some((s) => s.id === raw.schedule_id)) return null
  }

  const card = { action: raw.action }
  for (const f of spec.fields) card[f] = raw[f]
  return card
}

export const MAX_CARDS_PER_REPLY = 3
const DESIGN_ID = /^[0-9a-f-]{36}$/

/** A design to build: an id and what the person is shown of it. */
export function validBuild(raw) {
  if (!raw || raw.action !== 'build' || typeof raw.design_id !== 'string' || !DESIGN_ID.test(raw.design_id)) return null
  const d = raw.design
  if (!d || typeof d !== 'object' || !Array.isArray(d.agents) || d.agents.length === 0) return null
  const text = (v, max) => String(v ?? '').slice(0, max)
  const list = (v, max) => (Array.isArray(v) ? v.slice(0, 12).map((x) => text(x, max)) : [])
  return {
    action: 'build',
    design_id: raw.design_id,
    design: {
      title: text(d.title, 120) || 'New agents',
      summary: text(d.summary, 1200),
      to_do_after: list(d.to_do_after, 300),
      agents: d.agents.slice(0, 20).map((a) => ({
        name: text(a?.name, 60),
        job: text(a?.job, 400),
        credentials: list(a?.credentials, 80),
        schedule: a?.schedule ? { name: text(a.schedule.name, 120), cron: text(a.schedule.cron, 60) } : null,
        calls: list(a?.calls, 60),
      })),
    },
  }
}

/** A build in the making: an id to ask about, and what it is called. */
export function validJob(raw) {
  if (!raw || raw.action !== 'job' || typeof raw.job_id !== 'string' || !DESIGN_ID.test(raw.job_id)) return null
  return { action: 'job', job_id: raw.job_id, title: String(raw.title ?? '').slice(0, 120) || 'New agents' }
}

const RUN_ID = /^[A-Za-z0-9_-]{6,80}$/

/** A run on one of this instance's agents that the page follows until it ends. */
export function validRun(raw, fleet) {
  if (!raw || raw.action !== 'run' || typeof raw.execution_id !== 'string' || !RUN_ID.test(raw.execution_id)) return null
  if (!(fleet || []).some((a) => a.name === raw.agent)) return null
  return { action: 'run', agent: raw.agent, execution_id: raw.execution_id, title: String(raw.title ?? '').slice(0, 120) || 'A run' }
}

/** A question the person answers with a click: its words and two to five answers. */
export function validChoice(raw) {
  if (!raw || raw.action !== 'choice' || typeof raw.question !== 'string' || !raw.question.trim()) return null
  const options = (Array.isArray(raw.options) ? raw.options : [])
    .filter((o) => typeof o === 'string' && o.trim()).map((o) => o.trim().slice(0, 80)).slice(0, 5)
  if (options.length < 2) return null
  return { action: 'choice', question: raw.question.trim().slice(0, 200), options }
}

const FEEDBACK_KINDS = Object.freeze({ bug: 'Bug report', idea: 'Idea', feedback: 'Feedback' })

/** A note to the makers of Trinity. The person sees all of it before it goes. */
export function validFeedback(raw) {
  if (!raw || raw.action !== 'feedback' || !Object.hasOwn(FEEDBACK_KINDS, raw.kind)) return null
  if (typeof raw.summary !== 'string' || !raw.summary.trim()) return null
  return {
    action: 'feedback', kind: raw.kind, summary: raw.summary.trim().slice(0, 200),
    details: String(raw.details ?? '').trim().slice(0, 4000),
  }
}

export const feedbackKind = (card) => FEEDBACK_KINDS[card.kind] || 'Feedback'

// Credentials the instance itself holds; the instance and Trinity's side hold the same list.
export const INSTANCE_CREDENTIALS = Object.freeze(['ANTHROPIC_API_KEY', 'CLAUDE_SUBSCRIPTION'])
const CREDENTIAL_NAME = /^[A-Z][A-Z0-9_]{1,63}$/

/**
 * A masked field for one credential, by name, for one of this instance's
 * agents (or for the instance). The card carries no value and never will: the
 * person types it into the field and it goes to this instance only.
 */
export function validCredential(raw, fleet) {
  if (!raw || raw.action !== 'credential' || typeof raw.name !== 'string' || !CREDENTIAL_NAME.test(raw.name)) return null
  if (Object.keys(raw).some((k) => !['action', 'name', 'why', 'agent'].includes(k))) return null
  if (raw.agent !== undefined) {
    if (!(fleet || []).some((a) => a.name === raw.agent)) return null
  } else if (!INSTANCE_CREDENTIALS.includes(raw.name)) {
    return null
  }
  // ent#1354: this instance's Claude card is held as the subscription credential,
  // which takes the sign-in code, a subscription token or an API key.
  const name = raw.agent === undefined ? instanceCredentialName({ name: raw.name }) : raw.name
  const card = { action: 'credential', name, why: String(raw.why ?? '').trim().slice(0, 300) }
  if (raw.agent !== undefined) card.agent = raw.agent
  return card
}

/** Where a credential is stored, in words: `the agent scout` or `this instance`. */
export const credentialWhere = (card) => (card.agent ? `the agent ${card.agent}` : 'this instance')

/** The only thing the assistant is told about a credential field: provided or not. */
export function credentialLine(card, provided, { working = null } = {}) {
  const line = `${provided ? 'Provided' : 'Not provided'}: ${card.name} for ${credentialWhere(card)}.`
  // ent#1355: the instance checked its own Claude key after the save; a rejection is said, the value never.
  return provided && working === false ? `${line} Anthropic did not accept it, so agents cannot run on it yet.` : line
}

const TOOL_NAME = /^[a-z0-9_]{2,80}$/

/**
 * A change that cannot be undone, by tool name and arguments. The page shows
 * exactly these, in its own words: nothing the model wrote is displayed as the
 * description of what will run. The instance decides whether the tool is one
 * that takes a confirmation at all.
 */
export function validTool(raw) {
  if (!raw || raw.action !== 'tool' || typeof raw.tool !== 'string' || !TOOL_NAME.test(raw.tool)) return null
  const args = raw.args
  if (!args || typeof args !== 'object' || Array.isArray(args)) return null
  let size = 0
  try {
    size = JSON.stringify(args).length
  } catch {
    return null
  }
  if (size > 4000 || Object.keys(raw).some((k) => !['action', 'tool', 'args'].includes(k))) return null
  return { action: 'tool', tool: raw.tool, args: JSON.parse(JSON.stringify(args)) }
}

// A change the instance asks the person to confirm is not always one that
// cannot be taken back: only reading runs without a click. These are the ones
// that cannot (the instance holds the same list).
const IRREVERSIBLE = /^(delete_|remove_|teardown_|revoke_|clear_|reset_|withdraw_|close_)|^(rename_agent|deploy_system|apply_system|restart_system|unassign_skill_set)$/
export const irreversibleTool = (card) => card?.action === 'tool' && IRREVERSIBLE.test(card.tool || '')

/**
 * A link Trinity's side supplied, if it is a plain web address; '' otherwise.
 * Nothing else (a `javascript:` or `data:` address) is ever put in an href or opened.
 */
export function webLink(raw) {
  try {
    const url = new URL(String(raw || ''))
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : ''
  } catch {
    return ''
  }
}

/** `delete_agent` as words, and each argument as the person would read it. */
export function toolWords(card) {
  const words = card.tool.replace(/_/g, ' ')
  const details = Object.entries(card.args).map(([key, value]) => ({
    key: key.replace(/_/g, ' '),
    value: typeof value === 'string' ? value : JSON.stringify(value),
  }))
  return { title: words.charAt(0).toUpperCase() + words.slice(1), details }
}

/**
 * The cards of one reply. `raw` is what the gateway sent; every entry is
 * checked here and anything that does not pass is dropped, silently.
 */
export function acceptCards(raw, fleet) {
  const cards = []
  for (const item of Array.isArray(raw) ? raw : []) {
    let card = null
    if (item?.action === 'build') card = validBuild(item)
    else if (item?.action === 'tool') card = validTool(item)
    else if (item?.action === 'job') card = validJob(item)
    else if (item?.action === 'run') card = validRun(item, fleet)
    else if (item?.action === 'choice') card = validChoice(item)
    else if (item?.action === 'feedback') card = validFeedback(item)
    else if (item?.action === 'credential') card = validCredential(item, fleet)
    else if (item?.action === 'upgrade' && Object.keys(item).length === 1) card = { action: 'upgrade' }
    else if (item?.action === 'local-move' && Object.keys(item).length === 1) card = { action: 'local-move' }
    else card = validCard(item, fleet)
    const duplicate = card && cards.some((c) => JSON.stringify(c) === JSON.stringify(card))
    if (card && !duplicate && cards.length < MAX_CARDS_PER_REPLY) cards.push(card)
  }
  return cards
}

export function cardLabel(card, fleet) {
  if (card.action === 'job') return `Making "${card.title}"`
  if (card.action === 'run') return `${card.title} on ${card.agent}`
  if (card.action === 'choice') return card.question
  if (card.action === 'feedback') return `Send ${feedbackKind(card).toLowerCase()}: ${card.summary}`
  if (card.action === 'build') return `Build "${card.design.title}"`
  if (card.action === 'upgrade') return 'Subscribe to Trinity Pro'
  if (card.action === 'local-move') return 'Bring in the agents you run in Claude Code'
  if (card.action === 'credential') return `${card.name} for ${card.agent || 'this instance'}`
  if (card.action === 'tool') {
    const { title, details } = toolWords(card)
    const first = details.find((d) => ['name', 'agent name', 'agent'].includes(d.key)) || details[0]
    return first ? `${title}: ${first.value.slice(0, 80)}` : title
  }
  return CARD_ACTIONS[card.action].label(card, fleet)
}

/** The line under a card's title. Every change to an agent runs as the person. */
export function cardNote(card) {
  if (card.action === 'job') return "Each agent is made with the standard tools and checked. This takes a few minutes. Keep talking or close this panel: I'll come back when they're ready."
  if (card.action === 'run') return "Running on the agent. Keep talking or close this panel: I'll come back with the result."
  if (card.action === 'choice') return ''
  if (card.action === 'local-move') return 'You run these on your own machine. Nothing here changes until your agents arrive.'
  if (card.action === 'feedback') return 'Goes to the people who make Trinity, with the page you are on and your Trinity version. Nothing else is attached.'
  if (card.action === 'build') return 'Creates these agents on this instance, as you. Nothing is switched on.'
  if (card.action === 'tool') return irreversibleTool(card) ? 'This cannot be undone. It runs as you.' : 'Changes this instance. It runs as you.'
  if (card.action === 'credential') {
    return `The assistant cannot see what you type. It is stored on ${credentialWhere(card)} as ${card.name}, and the assistant is only told whether it was provided.`
  }
  if (card.action === 'upgrade') return "A larger allowance and bigger teams. We email a code to this instance's registered address to confirm."
  return 'Runs as you, not as the assistant.'
}

// A 4xx is Trinity refusing: nothing changed. No answer, or a 5xx, may have
// been applied anyway, so the page says it isn't sure rather than that it failed.
function failure(error) {
  const status = error?.response?.status
  if (!status) return { ok: false, refused: false, reason: "Couldn't reach Trinity, so it may not have been made." }
  if (status >= 500) return { ok: false, refused: false, reason: `Trinity had a problem (${status}), so it may not have been made.` }
  const detail = error.response.data?.detail
  const words = typeof detail === 'string' ? detail : detail?.message
  return { ok: false, refused: true, reason: words || `Trinity refused it (${status}).` }
}

/**
 * Makes one confirmed change with the person's session, then reads it back.
 * Returns the outcome kind the assistant is told about.
 */
export async function runCard(http, card, fleet) {
  if (!validCard(card, fleet)) {
    return { kind: 'refused', reason: "This page can't make that change." }
  }
  const spec = CARD_ACTIONS[card.action]
  const [method, url, body] = spec.call(card)
  try {
    await http[method](url, body)
  } catch (error) {
    const f = failure(error)
    return { kind: f.refused ? 'refused' : 'unsure', reason: f.reason }
  }
  const held = await spec.held(http, card)
  return held
    ? { kind: 'done' }
    : { kind: 'unsure', reason: "The page couldn't confirm it took." }
}

/** The line the page sends the assistant after a card is answered. */
export function outcomeLine(kind, label, reason) {
  if (kind === 'done') return `Done: ${label}.`
  if (kind === 'declined') return `Not now: ${label}.`
  if (kind === 'refused') return `That didn't work: ${label}. ${reason || ''}`.trim()
  return `Not sure it worked: ${label}. ${reason || ''}`.trim()
}

/** What a finished build is told to the assistant as: what exists now, and what does not. */
export function buildOutcome(card, report) {
  const label = cardLabel(card)
  const rows = Array.isArray(report?.agents) ? report.agents : []
  const made = rows.filter((r) => r.created && !r.problem)
  const troubled = rows.filter((r) => r.problem)
  if (!rows.length) return { kind: 'unsure', line: outcomeLine('unsure', label, 'The instance reported nothing.'), reason: 'The instance reported nothing.' }
  const parts = made.map((r) => `${r.name} (${r.files} files${r.schedule ? `, schedule "${r.schedule.name}" added switched off, id ${r.schedule.id}` : ''})`)
  const problems = troubled.map((r) => `${r.name}: ${r.problem}`).join('; ')
  if (!troubled.length) return { kind: 'done', line: `${outcomeLine('done', label)} Built ${parts.join(', ')}.`, reason: '' }
  const kind = made.length ? 'unsure' : 'refused'
  const reason = made.length ? `Built ${parts.join(', ')}. Not finished: ${problems}.` : problems
  return { kind, line: outcomeLine(kind, label, reason), reason }
}
