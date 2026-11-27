// @vitest-environment jsdom
/**
 * trinity-enterprise#1416: after Connect Claude in the first session, the Chief
 * of Staff is offered on the same card as the Company Brain (the ent#1408
 * behaviour, extended to both). Mounted on the ent#1408 harness: the real
 * AssistantWidget and stores, the instance answering through a mocked api.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const TOKEN = 'sk-ant-oat01-' + 't'.repeat(40)
const ALLOWANCE = { plan: 'free', period_hours: 24, messages_left: 24, messages_limit: 25, builds_left: 0, builds_limit: 1, reached: null, resets_at: null }
const OWN = 'scout'
const BRAIN = 'cornelius'
const CHIEF = 'corbin'
const EXAMPLES = ['acme-sage', 'acme-scout', CHIEF, BRAIN]
const calls = []
let turns
let held
let off          // per kind, the agents not on it (credential._report: not_using)
let leads        // the assistant leads this session (status.leads_setup)
let brain        // what the first-run read names as the brain, or undefined (an older instance)
let present      // the agents /api/agents lists besides trinity-system
let core         // what the first-run read names as core agents (ent#1416)

vi.mock('@/api', () => {
  const respond = (method, url, body, config) => {
    calls.push({ method, url, body, params: config?.params })
    if (url === '/api/enterprise/assistant/status') {
      return { available: true, contacted: true, reachable: true, plan: 'free', email: null, allowance: null,
        trial_terms: { version: 't0', url: '', accepted: 't0' }, profile_known: true, ...(leads ? { leads_setup: true } : {}) }
    }
    if (url === '/api/onboarding/first-run') {
      return { first_run: false, seeded_agents: EXAMPLES, own_agent_count: 1, core_agents: core, ...(brain !== undefined ? { brain_agent: brain } : {}) }
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
      return [{ name: 'trinity-system', status: 'running', is_system: true }, ...present.map((name) => ({ name, status: 'running' }))]
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

describe('the Chief of Staff goes onto the Claude connected in the first session, with the Brain (ent#1416)', () => {
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
    core = [CHIEF, BRAIN]
    present = [OWN, ...EXAMPLES]
    off = { subscription: [OWN, CHIEF, BRAIN, 'acme-scout'], api_key: [OWN, CHIEF, BRAIN, 'acme-scout'] }
    localStorage.clear()
    sessionStorage.clear()
    open()
  })
  afterEach(() => wrapper.unmount())

  it('the card names both with their own agent and says why', async () => {
    await connectThenOffer([OWN])
    expect(useCard().text()).toContain(`Use this subscription on these agents: ${OWN}, ${CHIEF}, ${BRAIN}`)
    expect(useCard().text()).toContain(`${CHIEF} and ${BRAIN} are the chief of staff and the company brain this instance came with`)
    expect(useCard().text()).not.toContain('acme-scout')
    await button(useCard(), 'Yes, switch them').trigger('click')
    await flushPromises()
    expect(posted(USE).map((c) => c.body)).toEqual([{ kind: 'subscription', agents: [OWN, CHIEF, BRAIN] }])
  })

  it('the assistant is told the Chief of Staff is not on it either', async () => {
    await connectThenOffer([OWN])
    expect(providedLine()).toContain(CHIEF)
    expect(providedLine()).toContain(BRAIN)
    expect(providedLine()).not.toContain('acme-scout')
  })

  it('the Chief of Staff already on it: only the brain is added, and said', async () => {
    off = { subscription: [OWN, BRAIN], api_key: [OWN, BRAIN] }
    await connectThenOffer([OWN])
    expect(useCard().text()).toContain(`Use this subscription on these agents: ${OWN}, ${BRAIN}`)
    expect(useCard().text()).not.toContain(CHIEF)
    expect(useCard().text()).toContain(`${BRAIN} is the company brain this instance came with`)
  })

  it('a Chief of Staff the instance does not have is never put on the card', async () => {
    present = [OWN, 'acme-sage', 'acme-scout', BRAIN]
    off = { subscription: [OWN, BRAIN], api_key: [OWN, BRAIN] }
    await connectThenOffer([OWN])
    expect(useCard().text()).toContain(`Use this subscription on these agents: ${OWN}, ${BRAIN}`)
    expect(useCard().text()).not.toContain(CHIEF)
  })
})
