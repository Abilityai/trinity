// @vitest-environment jsdom
/**
 * trinity-enterprise#1408 — the seeded Cornelius goes onto the Claude credential
 * the person connects in the first session, together with their own agent, in
 * the same card. Mounted through the real AssistantWidget and the real store.
 *
 * Seen live on 2026-11-27 (dev self-serve, build a864be8): the person connected
 * Claude through the assistant's card, the subscription was offered for the
 * agent they built only, and `/first-run` on Cornelius then failed with
 * "Authentication failure: Not logged in". The page had left Cornelius out as
 * one of the examples (ent#1390). The ruling (2026-11-27): Cornelius is part
 * of the product, not an example (epic ent#1407). It goes onto the credential
 * with the person's own agent, said in the same card; the other example agents
 * are left as they are, and the card does not name them.
 *
 * The instance names its company brain on the first-run read (`brain_agent`).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const TOKEN = 'sk-ant-oat01-' + 't'.repeat(40)
const ALLOWANCE = { plan: 'free', period_hours: 24, messages_left: 24, messages_limit: 25, builds_left: 0, builds_limit: 1, reached: null, resets_at: null }
const OWN = 'scout'
const BRAIN = 'cornelius'
const EXAMPLES = ['acme-sage', 'acme-scout', BRAIN]
const calls = []
let turns
let held
let off          // per kind, the agents not on it (credential._report: not_using)
let leads        // the assistant leads this session (status.leads_setup)
let brain        // what the first-run read names as the brain, or undefined (an older instance)

vi.mock('@/api', () => {
  const respond = (method, url, body, config) => {
    calls.push({ method, url, body, params: config?.params })
    if (url === '/api/enterprise/assistant/status') {
      return { available: true, contacted: true, reachable: true, plan: 'free', email: null, allowance: null,
        trial_terms: { version: 't0', url: '', accepted: 't0' }, profile_known: true, ...(leads ? { leads_setup: true } : {}) }
    }
    if (url === '/api/onboarding/first-run') {
      return { first_run: false, seeded_agents: EXAMPLES, own_agent_count: 1, ...(brain !== undefined ? { brain_agent: brain } : {}) }
    }
    if (url === '/api/enterprise/assistant/turn' || url === '/api/enterprise/assistant/turn/continue') {
      return turns.shift() || { status: 'final', conversation_id: 'c1', text: 'Thanks.', cards: [], allowance: ALLOWANCE }
    }
    if (url === '/api/enterprise/assistant/credential' && method === 'get') {
      return { agent: null, name: config?.params?.name, set: held !== null, current: held, use: '' }
    }
    if (url === '/api/enterprise/assistant/credential' && method === 'post') {
      held = 'subscription'
      return { provided: true, agent: null, name: body.name, replaced: false, working: true, connected_agents: 0,
        kind: 'subscription', not_using: off.subscription.map((name) => ({ name, model_access: false })), moving: [] }
    }
    if (url === '/api/enterprise/assistant/credential/agents') {
      const kind = config?.params?.kind
      return { kind, not_using: off[kind].map((name) => ({ name, model_access: false })), moving: [] }
    }
    if (url === '/api/enterprise/assistant/credential/use') {
      // credential.use(): an agent not among those off the kind is refused.
      const extra = body.agents.filter((n) => !off[body.kind].includes(n))
      if (extra.length) {
        throw { response: { status: 422, data: { detail: { code: 'not_listed', message: `Not among the agents off this Claude subscription: ${extra.join(', ')}. Nothing was changed.` } } } }
      }
      off[body.kind] = off[body.kind].filter((n) => !body.agents.includes(n))
      return { kind: body.kind, now_using: body.agents, not_switched: [] }
    }
    if (url === '/api/agents') {
      return [{ name: 'trinity-system', status: 'running', is_system: true }, { name: OWN, status: 'running' },
        ...EXAMPLES.map((name) => ({ name, status: 'running' }))]
    }
    if (url === '/api/agents/autonomy-status') return {}
    if (url === '/api/ops/schedules') return { schedules: [] }
    return {}
  }
  const client = {
    get: vi.fn(async (url, config) => ({ data: respond('get', url, undefined, config) })),
    post: vi.fn(async (url, body) => ({ data: respond('post', url, body) })),
    put: vi.fn(async (url, body) => ({ data: respond('put', url, body) })),
    delete: vi.fn(async (url) => ({ data: respond('delete', url) })),
  }
  return { default: client }
})
vi.mock('vue-router', () => ({ useRoute: () => ({ path: '/', params: {}, query: {} }), useRouter: () => ({ push: async () => {} }) }))
vi.mock('@/stores/agents', () => ({ useAgentsStore: () => ({ fetchAgents: async () => {} }) }))

import AssistantWidget from '@/components/assistant/AssistantWidget.vue'

const TURN = '/api/enterprise/assistant/turn'
const USE = '/api/enterprise/assistant/credential/use'
const ASK = { action: 'credential', name: 'ANTHROPIC_API_KEY', why: 'Your agent runs on Claude, and this connects it.' }
const final = (text, cards = []) => ({ status: 'final', conversation_id: 'c1', text, cards, allowance: ALLOWANCE })
const posted = (url) => calls.filter((c) => c.url === url && c.method === 'post')

describe('the seeded Cornelius goes onto the Claude connected in the first session (ent#1408)', () => {
  let wrapper
  const cards = () => wrapper.findAll('[data-testid="assistant-card"]')
  const useCard = () => cards().find((c) => c.text().includes('on these agents'))
  const button = (card, words) => card.findAll('button').find((b) => b.text() === words)
  const say = async (text) => {
    await wrapper.get('[data-testid="assistant-input"]').setValue(text)
    await wrapper.get('[data-testid="assistant-input"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
  }
  // The first-session script's step 8 then 8a: the Claude card, the paste,
  // then the assistant's switch card with the agents it chose to name.
  const connectThenOffer = async (agents) => {
    turns = [final('Paste the token in the field below.', [ASK]),
      final('Shall I switch them?', [{ action: 'use-claude', kind: 'subscription', agents }])]
    await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
    await say('build it')
    await wrapper.get('[data-testid="assistant-credential-input"]').setValue(TOKEN)
    await wrapper.get('[data-testid="assistant-credential"]').trigger('submit')
    await flushPromises()
  }
  // The line the page sent the assistant after the save ("Provided: ...").
  const providedLine = () => posted(TURN).map((c) => c.body.message).find((m) => m.startsWith('Provided'))
  const open = () => {
    setActivePinia(createPinia())
    wrapper = mount(AssistantWidget, {
      attachTo: document.body,
      global: { stubs: {
        ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' },
        HelpChatWidget: { template: '<div />' },
      } },
    })
  }

  beforeEach(() => {
    calls.length = 0
    turns = []
    held = null
    leads = true
    brain = BRAIN
    off = { subscription: [OWN, BRAIN, 'acme-scout'], api_key: [OWN, BRAIN, 'acme-scout'] }
    localStorage.clear()
    sessionStorage.clear()
    open()
  })
  afterEach(() => wrapper.unmount())

  it('the assistant is told Cornelius is not on it, and never the examples', async () => {
    await connectThenOffer([OWN])
    expect(providedLine()).toContain(BRAIN)
    expect(providedLine()).toContain(OWN)
    expect(providedLine()).not.toContain('acme-scout')
  })

  it('the card names Cornelius with their own agent and says why, even when the assistant named only theirs', async () => {
    await connectThenOffer([OWN])
    expect(useCard().text()).toContain(`Use this subscription on these agents: ${OWN}, ${BRAIN}`)
    expect(useCard().text()).toContain(`${BRAIN} is the company brain this instance came with`)
    expect(useCard().text()).not.toContain('acme-scout')
    await button(useCard(), 'Yes, switch them').trigger('click')
    await flushPromises()
    expect(posted(USE).map((c) => c.body)).toEqual([{ kind: 'subscription', agents: [OWN, BRAIN] }])
    expect(posted(TURN).at(-1).body.message).toBe(`Done: Use this subscription on these agents: ${OWN}, ${BRAIN}. Now on it: ${OWN}, ${BRAIN}.`)
  })

  it('an example the assistant named is left off the card, Cornelius is not', async () => {
    await connectThenOffer([OWN, 'acme-scout'])
    expect(useCard().text()).toContain(`Use this subscription on these agents: ${OWN}, ${BRAIN}`)
    expect(useCard().text()).not.toContain('acme-scout')
  })

  it('Cornelius already on it: the card names only their own agent, with no word about the brain', async () => {
    off = { subscription: [OWN, 'acme-scout'], api_key: [OWN] }
    await connectThenOffer([OWN])
    expect(useCard().text()).toContain(`Use this subscription on these agents: ${OWN}`)
    expect(useCard().text()).not.toContain(BRAIN)
    expect(providedLine()).not.toContain(BRAIN)
  })

  it('an instance that names no brain keeps Cornelius out of sight, as before (ent#1390)', async () => {
    brain = undefined
    wrapper.unmount()
    open()
    await connectThenOffer([OWN])
    expect(useCard().text()).toContain(`Use this subscription on these agents: ${OWN}`)
    expect(useCard().text()).not.toContain(BRAIN)
    expect(providedLine()).not.toContain(BRAIN)
  })

  it('outside the first session the page adds nothing to the assistant\'s card', async () => {
    leads = false
    wrapper.unmount()
    open()
    held = 'subscription'
    turns = [final('scout is not on it.', [{ action: 'use-claude', kind: 'subscription', agents: [OWN] }])]
    await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
    await say('why can scout not run?')
    expect(useCard().text()).toContain(`Use this subscription on these agents: ${OWN}`)
    expect(useCard().text()).not.toContain(BRAIN)
  })
})
