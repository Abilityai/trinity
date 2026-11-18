/**
 * Confirm cards for the assistant panel.
 *
 * The assistant never changes a setting with its own key. It PROPOSES a change
 * as a fenced `trinity-card` block in its reply; the page turns that into a
 * card, and on Confirm makes the change through the same route the app's own
 * control uses, with the person's session. Then it reads the result back.
 *
 * The reply is model-written text, so nothing in it is trusted: a proposal is
 * accepted only when its action is in this catalog, it carries exactly the
 * fields that action takes, and every agent it names is one the page itself
 * read from the instance.
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
  // Takes no fields. Moves a registered free instance to Trinity Pro. (On an
  // instance that has not registered, the panel opens the sign-up door
  // instead of running this.)
  upgrade: {
    fields: [],
    label: () => 'Upgrade this instance to Trinity Pro',
    note: 'Lifts the limit on agents Trinity builds for you.',
    call: () => ['post', UPGRADE_URL, {}],
    held: async (http) => (await read(http, STATUS_URL))?.plan === 'pro',
  },
})

export const STATUS_URL = '/api/enterprise/assistant/status'
export const UPGRADE_URL = '/api/enterprise/assistant/upgrade'

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

const BLOCK = /```trinity-card\s*\n([\s\S]*?)```/g
export const MAX_CARDS_PER_REPLY = 2

/**
 * Splits a reply into the words to show and the cards to offer. Every
 * `trinity-card` block leaves the text, valid or not: a refused proposal must
 * not show as raw JSON either.
 */
export function parseReply(text, fleet) {
  const cards = []
  const shown = String(text || '').replace(BLOCK, (_, body) => {
    let raw = null
    try {
      raw = JSON.parse(body)
    } catch {
      raw = null
    }
    const card = validCard(raw, fleet)
    const duplicate = card && cards.some((c) => JSON.stringify(c) === JSON.stringify(card))
    if (card && !duplicate && cards.length < MAX_CARDS_PER_REPLY) cards.push(card)
    return ''
  })
  return { text: shown.replace(/\n{3,}/g, '\n\n').trim(), cards }
}

export function cardLabel(card, fleet) {
  return CARD_ACTIONS[card.action].label(card, fleet)
}

/** The line under a card's title. Every change to an agent runs as the person. */
export function cardNote(card) {
  return CARD_ACTIONS[card.action].note || 'Runs as you, not as the assistant.'
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
