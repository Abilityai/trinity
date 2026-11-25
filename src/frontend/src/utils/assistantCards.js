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
 * written by the page: trinity-enterprise#1353), and `use-claude` (switch the
 * named existing agents onto this instance's Claude credential, only on the
 * person's yes: trinity-enterprise#1354).
 */

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
  // trinity-enterprise#1391: each run of this schedule is mailed to the person.
  // No address in the card or the request: the instance uses the caller's own.
  'email-results': {
    fields: ['agent', 'schedule_id'],
    label: (c, f) => `Email each result of "${scheduleName(c, f)}" to you`,
    note: 'Sent to your own email address, starting with its latest result now. To stop, choose "Stop emailing" on the schedule in the Schedules tab.',
    call: (c) => ['post', `${agentPath(c.agent)}/schedules/${enc(c.schedule_id)}/email-me`, { enabled: true }],
    held: async (http, c) => (await schedule(http, c))?.email_results_to_me === true,
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
  // ent#1354: the card keeps the name the assistant asked under. This instance
  // takes both Claude names as one card (a subscription token from
  // `claude setup-token` or an API key, tested before it is saved), and the
  // "Provided:" line must name what the assistant asked for, or it asks again.
  const card = { action: 'credential', name: raw.name, why: String(raw.why ?? '').trim().slice(0, 300) }
  if (raw.agent !== undefined) card.agent = raw.agent
  return card
}

/** This instance's own Claude credential (either name the assistant asks under). */
export const claudeCard = (card) => card?.action === 'credential' && !card.agent && INSTANCE_CREDENTIALS.includes(card.name)

/** Where a credential is stored, in words: `the agent scout` or `this instance`. */
export const credentialWhere = (card) => (card.agent ? `the agent ${card.agent}` : 'this instance')

/** The only thing the assistant is told about a credential field: provided or not. */
export function credentialLine(card, provided, { working = null, kind = null, notUsing, moving, connected, hidden = 0 } = {}) {
  const line = `${provided ? 'Provided' : 'Not provided'}: ${card.name} for ${credentialWhere(card)}.`
  // ent#1355: the instance checked its own Claude key after the save; a rejection is said, the value never.
  if (provided && working === false) return `${line} Anthropic did not accept it, so agents cannot run on it yet.`
  if (!provided || !Object.hasOwn(CLAUDE_KINDS, kind || '')) return line
  // ent#1354: what the save reported, and nothing it did not: how many agents it
  // connected, which are already moving onto it, which are not on it.
  const facts = saveFacts({ connected, moving })
  let said = `${line} Saved as ${kind}.`
  if (facts.connected) said += ` It connected ${agentsWord(facts.connected)} that had no Claude credential.`
  if (facts.moving.length) said += ` Already moving onto it (do not ask about these): ${facts.moving.join(', ')}.`
  if (notUsing === null) {
    return `${said} Whether ${facts.switchedNone ? '' : 'other '}existing agents use it could not be checked.`
  }
  // ent#1390: `hidden` agents were left out of `notUsing`, so a claim about
  // every agent holds only for the ones the person can see.
  const seen = hidden > 0
  const words = notUsingWords(notUsing)
  if (words) {
    const who = seen ? 'Existing agents the person can see that are not on it' : 'Existing agents not on it'
    return `${said} ${who}${facts.switchedNone ? ' (nothing was switched)' : ''}: ${words}.`
  }
  if (facts.moving.length || facts.connected) return said
  return `${said} Every existing agent${seen ? ' the person can see' : ''} is on it.`
}

const agentsWord = (n) => (n === 1 ? '1 agent' : `${n} agents`)

/** What a save reported about agents it moved: a count it connected, and names already moving onto it. */
export function saveFacts({ connected, moving } = {}) {
  const n = Number.isInteger(connected) && connected > 0 ? connected : 0
  const names = Array.isArray(moving) ? moving.filter((m) => typeof m === 'string') : []
  return { connected: n, moving: names, switchedNone: n === 0 && names.length === 0 }
}

/**
 * The line the Claude field shows after a save (ent#1354): exactly what the
 * save reported. "Nothing was switched" only when it connected no agent and
 * none is moving onto it. `fallback` is the plain saved text.
 */
export function savedReportText({ connected, notUsing, moving } = {}, fallback) {
  const facts = saveFacts({ connected, moving })
  const rows = Array.isArray(notUsing) ? notUsing : []
  if (rows.length === 0 && facts.moving.length === 0) return fallback(connected)
  const parts = [facts.connected
    ? `Connected ${agentsWord(facts.connected)} that had no Claude credential; running ones restart in the background.`
    : 'Saved.']
  if (facts.moving.length) {
    const them = facts.moving.length === 1 ? 'it restarts' : 'they restart'
    parts.push(`Moving onto it as ${them}: ${facts.moving.join(', ')}.`)
  }
  if (rows.length) {
    const them = rows.length === 1 ? 'it' : 'them'
    parts.push(`Not on it yet: ${rows.map((r) => r.name).join(', ')}.`)
    parts.push(facts.switchedNone
      ? `Nothing was switched; the assistant asks you before it moves ${them}.`
      : `The assistant asks you before it moves ${them}.`)
  }
  return parts.join(' ')
}

// ---------------------------------------------------------------------------
// trinity-enterprise#1354 (ruled 2026-11-24, evening): existing agents and the
// Claude credential. Saving a credential through the card switches no agent.
// The instance reports which existing agents are not on it and whether each
// can run as it is; the assistant shows one `use-claude` card naming them, and
// only the person's yes switches exactly those agents.
// ---------------------------------------------------------------------------

export const CLAUDE_KINDS = Object.freeze({ subscription: 'this subscription', api_key: 'this API key' })
export const USE_CLAUDE_MAX_AGENTS = 20

/** One `use-claude` proposal: a kind and the agents, each one the page itself read. */
export function validUseClaude(raw, fleet) {
  if (!raw || raw.action !== 'use-claude' || !Object.hasOwn(CLAUDE_KINDS, raw.kind)) return null
  if (Object.keys(raw).some((k) => !['action', 'kind', 'agents'].includes(k))) return null
  if (!Array.isArray(raw.agents) || raw.agents.length === 0 || raw.agents.length > USE_CLAUDE_MAX_AGENTS) return null
  const names = new Set((fleet || []).map((a) => a.name))
  if (!raw.agents.every((a) => typeof a === 'string' && names.has(a))) return null
  return { action: 'use-claude', kind: raw.kind, agents: [...new Set(raw.agents)] }
}

/** What the instance said about its existing agents, in the words the assistant is given: names, never a value. */
export function notUsingWords(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return ''
  const access = { true: 'runs as it is', false: 'cannot run as it is' }
  return rows.slice(0, USE_CLAUDE_MAX_AGENTS)
    .map((r) => `${r.name} (${access[String(r.model_access)] || 'cannot tell whether it runs'})`).join(', ')
}

/**
 * The person's yes on a `use-claude` card: the instance switches exactly those
 * agents, as the person, and answers which now use it. The line the assistant
 * is told names them.
 */
export async function useClaude(http, card, label) {
  try {
    const { data } = await http.post('/api/enterprise/assistant/credential/use', { kind: card.kind, agents: card.agents })
    const now = Array.isArray(data?.now_using) ? data.now_using : []
    const missed = Array.isArray(data?.not_switched) ? data.not_switched : []
    const onIt = now.length ? `Now on it: ${now.join(', ')}.` : 'None of them is on it yet.'
    if (!missed.length) return { kind: 'done', reason: '', line: `${outcomeLine('done', label)} ${onIt}` }
    const reason = `${onIt} Not switched: ${missed.map((m) => `${m.name} (${m.reason})`).join('; ')}`
    return { kind: now.length ? 'unsure' : 'refused', reason, line: outcomeLine(now.length ? 'unsure' : 'refused', label, reason) }
  } catch (error) {
    const f = failure(error)
    return { kind: f.refused ? 'refused' : 'unsure', reason: f.reason, line: outcomeLine(f.refused ? 'refused' : 'unsure', label, f.reason) }
  }
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
    else if (item?.action === 'use-claude') card = validUseClaude(item, fleet)
    else if (item?.action === 'upgrade' && Object.keys(item).length === 1) card = { action: 'upgrade' }
    else if (item?.action === 'local-move' && Object.keys(item).length === 1) card = { action: 'local-move' }
    else card = validCard(item, fleet)
    const duplicate = card && cards.some((c) => JSON.stringify(c) === JSON.stringify(card))
    if (card && !duplicate && cards.length < MAX_CARDS_PER_REPLY) cards.push(card)
  }
  return cards
}

/**
 * ent#1355: in the first session the gateway sends an agent's first run as one
 * `chat_with_agent` tool card. The page words it as that, in its own words.
 */
export function firstRunCard(card, firstSession) {
  return Boolean(firstSession) && card?.action === 'tool' && card.tool === 'chat_with_agent'
    && typeof card.args?.agent_name === 'string' && card.args.agent_name !== ''
}

// ---------------------------------------------------------------------------
// trinity-enterprise#1390: a tool card says in one plain sentence what will
// happen, and its button names the action. The tool's parameters are kept
// behind the card's Details toggle, never in its body.
// ---------------------------------------------------------------------------

const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
const pad = (v) => String(v).padStart(2, '0')
const and = (words) => (words.length < 2 ? words.join('') : `${words.slice(0, -1).join(', ')} and ${words.at(-1)}`)

/**
 * When a 5-field cron expression runs, in words: `every Monday at 09:00 (Europe/London)`.
 * Words only when they are exactly true; anything else is the expression itself.
 * `timezone` undefined is the create default (UTC); `null` means the zone is not
 * known, so none is named.
 */
export function cronWords(expr, timezone) {
  const raw = String(expr ?? '').trim()
  const tz = typeof timezone === 'string' && timezone.trim() ? timezone.trim() : (timezone === null ? '' : 'UTC')
  const zone = tz ? ` (${tz})` : ''
  const fallback = `on the schedule ${raw}${zone}`
  const f = raw.split(/\s+/)
  if (f.length !== 5) return fallback
  const [m, h, dom, mon, dow] = f
  const num = (v, max) => /^\d{1,2}$/.test(v) && Number(v) <= max
  // `*/N` is an even gap only when N divides the range: `*/45` runs at :00 and :45.
  const every = (v, range) => {
    const n = Number(v.match(/^\*\/(\d{1,2})$/)?.[1])
    return n > 0 && range % n === 0 ? n : 0
  }
  if (dom === '*' && mon === '*' && dow === '*') {
    if (h === '*' && (m === '*' || every(m, 60) === 1)) return 'every minute'
    if (h === '*' && every(m, 60)) return `every ${every(m, 60)} minutes`
    if (h === '*' && m === '0') return 'every hour'
    if (h === '*' && num(m, 59)) return `every hour at ${Number(m)} ${Number(m) === 1 ? 'minute' : 'minutes'} past`
    if (m === '0' && every(h, 24) === 1) return 'every hour'
    if (m === '0' && every(h, 24)) return `every ${every(h, 24)} hours`
  }
  if (!num(m, 59) || !num(h, 23) || mon !== '*') return fallback
  const at = `at ${pad(h)}:${pad(m)}${zone}`
  if (dom === '*' && dow === '*') return `every day ${at}`
  if (dom === '*' && dow === '1-5') return `every weekday ${at}`
  if (dom === '*' && /^[0-7](,[0-7])*$/.test(dow)) return `every ${and([...new Set(dow.split(',').map((d) => DAYS[Number(d)]))])} ${at}`
  // Only a day every month has: day 29-31 is skipped in shorter months.
  if (num(dom, 28) && Number(dom) > 0 && dow === '*') return `on day ${Number(dom)} of every month ${at}`
  return fallback
}

const str = (v, max = 80) => (typeof v === 'string' ? v.slice(0, max) : '')
function storedSchedule(args, fleet) {
  const agent = (fleet || []).find((a) => a.name === args.agent_name)
  return agent?.schedules?.find((s) => s.id === args.schedule_id) || null
}
function scheduleTitle(args, fleet) {
  const name = storedSchedule(args, fleet)?.name
  return name ? `“${str(name, 120)}”` : ''
}
// An update sends `timezone` only when it changes it; otherwise the schedule keeps
// its stored zone, which is named when the page read it and left out when not.
function updateZone(args, fleet) {
  if (typeof args.timezone === 'string' && args.timezone.trim()) return args.timezone
  const stored = storedSchedule(args, fleet)?.timezone
  return typeof stored === 'string' && stored.trim() ? stored : null
}
// Where a schedule's results go, when they go beyond the run record. On an update a
// blank address clears delivery, the same as null.
function delivery(args, agent, { update = false } = {}) {
  if (typeof args.deliver_to_workspace_email === 'string' && args.deliver_to_workspace_email.trim()) {
    return ` Each run's results are delivered to ${str(args.deliver_to_workspace_email.trim(), 120)}'s Workspace chat with ${agent}.`
  }
  const cleared = args.deliver_to_workspace_email === null || (update && typeof args.deliver_to_workspace_email === 'string')
  return cleared ? ' Its results are no longer delivered to a Workspace chat.' : ''
}

// Tools the page has its own words for. `sentence` is what happens; `button` names it.
const TOOL_COPY = Object.freeze({
  chat_with_agent: (a, { firstSession }) => (firstSession
    ? { sentence: `Starts ${str(a.agent_name)} on its first task now, as you.`, button: 'Run its first task' }
    : { sentence: `Gives ${str(a.agent_name)} this task to run now, as you.`, button: 'Run the task' }),
  create_agent_schedule: (a) => ({
    sentence: `${str(a.agent_name)} will run “${str(a.name, 120)}” ${cronWords(a.cron_expression, a.timezone)}.`
      + delivery(a, str(a.agent_name))
      + (a.enabled === false ? ' It is added switched off.' : ''),
    button: a.enabled === false ? 'Add it switched off' : 'Switch it on',
  }),
  toggle_agent_schedule: (a, { fleet }) => {
    const which = scheduleTitle(a, fleet)
    const its = `${str(a.agent_name)}'s schedule${which ? ` ${which}` : ''}`
    return a.enabled === false
      ? { sentence: `Switches off ${its}: it does not run until it is switched on again.`, button: 'Switch it off' }
      : { sentence: `Switches on ${its}: it runs at its times from now on.`, button: 'Switch it on' }
  },
  update_agent_schedule: (a, { fleet }) => {
    const which = scheduleTitle(a, fleet)
    const zone = typeof a.timezone === 'string' && a.timezone.trim() ? str(a.timezone.trim()) : ''
    const when = typeof a.cron_expression === 'string'
      ? `: it will run ${cronWords(a.cron_expression, updateZone(a, fleet))}`
      : (zone ? `: its times are now read in ${zone}` : '')
    const state = a.enabled === true ? ', and switches it on' : a.enabled === false ? ', and switches it off' : ''
    const sentence = `Changes ${str(a.agent_name)}'s schedule${which ? ` ${which}` : ''}${when}${state}.${delivery(a, str(a.agent_name), { update: true })}`
    return { sentence, button: 'Change it' }
  },
  trigger_agent_schedule: (a, { fleet }) => {
    const which = scheduleTitle(a, fleet)
    return { sentence: `${str(a.agent_name)} runs its schedule${which ? ` ${which}` : ''} once, now, as you.`, button: 'Run it now' }
  },
  start_agent: (a) => ({ sentence: `Starts ${str(a.name)}.`, button: 'Start it' }),
  stop_agent: (a) => ({ sentence: `Stops ${str(a.name)}. It can be started again.`, button: 'Stop it' }),
  delete_agent: (a) => ({ sentence: `Deletes ${str(a.name)} from this instance. This cannot be undone.`, button: 'Delete it' }),
})

/** What a tool card says will happen, and what its button says. Never the tool's parameters. */
export function toolCopy(card, { firstSession = false, fleet = [] } = {}) {
  const words = Object.hasOwn(TOOL_COPY, card.tool) ? TOOL_COPY[card.tool](card.args || {}, { firstSession: firstRunCard(card, firstSession), fleet }) : null
  if (words) return words
  const { title } = toolWords(card)
  const sentence = `Runs “${title.toLowerCase()}” on this instance, as you.${irreversibleTool(card) ? ' This cannot be undone.' : ''}`
  return { sentence, button: title }
}

/** The name of a card's button, where the card has one of its own (else the panel's word for the action). */
export function cardButton(card, fleet, { firstSession = false } = {}) {
  return card?.action === 'tool' ? toolCopy(card, { firstSession, fleet }).button : ''
}

export function cardLabel(card, fleet, { firstSession = false } = {}) {
  if (firstRunCard(card, firstSession)) return `Run its first task: ${card.args.agent_name.slice(0, 80)}`
  if (card.action === 'job') return `Making "${card.title}"`
  if (card.action === 'run') return `${card.title} on ${card.agent}`
  if (card.action === 'choice') return card.question
  if (card.action === 'feedback') return `Send ${feedbackKind(card).toLowerCase()}: ${card.summary}`
  if (card.action === 'build') return `Build "${card.design.title}"`
  if (card.action === 'upgrade') return 'Subscribe to Trinity Pro'
  if (card.action === 'local-move') return 'Bring in the agents you run in Claude Code'
  if (claudeCard(card)) return 'Connect Claude'
  if (card.action === 'credential') return `${card.name} for ${card.agent || 'this instance'}`
  if (card.action === 'use-claude') return `Use ${CLAUDE_KINDS[card.kind]} on these agents: ${card.agents.join(', ')}`
  if (card.action === 'tool') {
    const { title, details } = toolWords(card)
    const first = details.find((d) => ['name', 'agent name', 'agent'].includes(d.key)) || details[0]
    return first ? `${title}: ${first.value.slice(0, 80)}` : title
  }
  return CARD_ACTIONS[card.action].label(card, fleet)
}

/** The line under a card's title. Every change to an agent runs as the person. */
export function cardNote(card, { firstSession = false, fleet = [] } = {}) {
  if (card.action === 'job') return "Each agent is made with the standard tools and checked. This takes a few minutes. Keep talking or close this panel: I'll come back when they're ready."
  if (card.action === 'run') return "Running on the agent. Keep talking or close this panel: I'll come back with the result."
  if (card.action === 'choice') return ''
  if (card.action === 'local-move') return 'You run these on your own machine. Nothing here changes until your agents arrive.'
  if (card.action === 'feedback') return 'Goes to the people who make Trinity, with the page you are on and your Trinity version. Nothing else is attached.'
  if (card.action === 'build') return 'Creates these agents on this instance, as you. Nothing is switched on.'
  if (card.action === 'tool') return toolCopy(card, { firstSession, fleet }).sentence
  if (claudeCard(card)) {
    return 'The assistant cannot see what you paste. It is saved on this instance as its Claude credential, and the assistant is only told whether it was saved.'
  }
  if (card.action === 'credential') {
    return `The assistant cannot see what you type. It is stored on ${credentialWhere(card)} as ${card.name}, and the assistant is only told whether it was provided.`
  }
  if (card.action === 'use-claude') {
    return 'Switches only these agents, as you. A running one restarts, which takes about a minute. Nothing else changes.'
  }
  if (card.action === 'upgrade') return "A larger allowance and bigger teams. We email a code to this instance's registered address to confirm."
  return CARD_ACTIONS[card.action]?.note || 'Runs as you, not as the assistant.'
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
