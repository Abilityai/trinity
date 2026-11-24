// @vitest-environment jsdom
/**
 * ent#1354 — connecting Claude in the assistant panel with a sign-in link.
 * Mounted through the real AssistantWidget and the real store.
 *
 * When the assistant asks for this instance's Claude credential, the card
 * shows three steps and a sign-in link the INSTANCE made for this person
 * (`POST /api/subscriptions/claude-sign-in`), with the API key as the
 * alternative in the same field. What is pasted goes once to this instance
 * under CLAUDE_SUBSCRIPTION, never into a turn; a refused paste gets a fresh
 * link and sends the assistant nothing.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const VALUE = 'the-code-from-claude#the-state-1354'
let links
let linkFails
const ALLOWANCE = { plan: 'free', period_hours: 24, messages_left: 24, messages_limit: 25, builds_left: 1, builds_limit: 1, reached: null, resets_at: null }
const calls = []
let turns
let credentialSet
let refuse
let refuseState

vi.mock('@/api', () => {
  const respond = (method, url, body, config) => {
    calls.push({ method, url, body, params: config?.params })
    if (url === '/api/enterprise/assistant/status') {
      return { available: true, contacted: true, reachable: true, plan: 'free', email: null, allowance: null, trial_terms: { version: 't0', url: '', accepted: 't0' }, profile_known: true }
    }
    if (url === '/api/enterprise/assistant/turn' || url === '/api/enterprise/assistant/turn/continue') {
      return turns.shift() || { status: 'final', conversation_id: 'c1', text: 'Thanks.', cards: [], allowance: ALLOWANCE }
    }
    if (url === '/api/enterprise/assistant/credential' && method === 'get') {
      if (refuseState) throw { response: { status: refuseState.status, data: { detail: refuseState.detail } } }
      return { agent: config?.params?.agent ?? null, name: config?.params?.name, set: credentialSet, use: '' }
    }
    if (url === '/api/enterprise/assistant/credential' && method === 'post') {
      if (refuse) {
        const r = refuse
        refuse = null
        throw { response: { status: r.status, data: { detail: r.detail } } }
      }
      return { provided: true, agent: body.agent, name: body.name, replaced: credentialSet === true }
    }
    if (url === '/api/subscriptions/claude-sign-in' && method === 'post') {
      if (linkFails) throw { response: { status: 503, data: { detail: { code: 'store_unavailable', message: 'x' } } } }
      links += 1
      return { url: `https://claude.com/cai/oauth/authorize?state=link-${links}`, expires_in: 900 }
    }
    if (url === '/api/agents') return [{ name: 'scout', status: 'running' }]
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
const CRED = '/api/enterprise/assistant/credential'
const ASK = { action: 'credential', name: 'ANTHROPIC_API_KEY', why: 'Your first agent needs Claude to run.' }
const final = (text, cards = []) => ({ status: 'final', conversation_id: 'c1', text, cards, allowance: ALLOWANCE })
const posted = (url) => calls.filter((c) => c.url === url && c.method === 'post')

describe('connecting Claude with a sign-in link', () => {
  let wrapper

  const openAndAsk = async (card = ASK) => {
    turns = [final('Your agent is ready to run once Claude is connected.', [card])]
    await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="assistant-input"]').setValue('build it')
    await wrapper.get('[data-testid="assistant-input"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
  }
  const paste = async (value) => {
    await wrapper.get('[data-testid="assistant-credential-input"]').setValue(value)
    await wrapper.get('[data-testid="assistant-credential"]').trigger('submit')
    await flushPromises()
  }
  const link = () => wrapper.get('[data-testid="assistant-credential-sign-in"]')

  beforeEach(() => {
    calls.length = 0
    turns = []
    credentialSet = false
    refuse = null
    refuseState = null
    links = 0
    linkFails = false
    localStorage.clear()
    sessionStorage.clear()
    setActivePinia(createPinia())
    wrapper = mount(AssistantWidget, {
      attachTo: document.body,
      global: { stubs: {
        ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' },
        HelpChatWidget: { template: '<div />' },
      } },
    })
  })

  afterEach(() => wrapper.unmount())

  it('shows the three steps with a sign-in link the instance made, and the API key as the alternative', async () => {
    await openAndAsk()
    const steps = wrapper.get('[data-testid="assistant-credential-steps"]').findAll('li')
    expect(steps).toHaveLength(3)
    expect(steps[0].text()).toMatch(/sign in/i)
    expect(steps[1].text()).toMatch(/copy the code/i)
    expect(steps[2].text()).toMatch(/paste it/i)
    expect(posted('/api/subscriptions/claude-sign-in')).toHaveLength(1)
    expect(link().attributes('href')).toBe('https://claude.com/cai/oauth/authorize?state=link-1')
    expect(link().attributes('target')).toBe('_blank')
    expect(wrapper.get('[data-testid="assistant-credential-alternative"]').text()).toMatch(/API key/)
    expect(wrapper.get('[data-testid="assistant-credential-input"]').attributes('type')).toBe('password')
    // the instance is asked about the subscription credential, not the API key
    const state = calls.find((c) => c.url === CRED && c.method === 'get')
    expect(state.params.name).toBe('CLAUDE_SUBSCRIPTION')
  })

  it('posts the paste once to this instance as CLAUDE_SUBSCRIPTION and tells the assistant only that it was provided', async () => {
    await openAndAsk()
    await paste(VALUE)
    const stored = posted(CRED)
    expect(stored).toHaveLength(1)
    expect(stored[0].body).toEqual({ agent: null, name: 'CLAUDE_SUBSCRIPTION', value: VALUE })
    const told = posted(TURN).slice(1)
    expect(told).toHaveLength(1)
    expect(JSON.stringify(told)).not.toContain(VALUE)
    expect(JSON.stringify(told)).toContain('Provided: CLAUDE_SUBSCRIPTION')
    expect(JSON.stringify(sessionStorage)).not.toContain(VALUE)
    expect(JSON.stringify(localStorage)).not.toContain(VALUE)
  })

  it('a refused paste is named, gets a fresh link, and sends the assistant nothing', async () => {
    await openAndAsk()
    refuse = { status: 422, detail: { code: 'instance_refused', message: 'That is not the whole code. Open the sign-in link again for a fresh code.' } }
    await paste('half-a-code')
    expect(wrapper.get('[data-testid="assistant-credential-error"]').text()).toMatch(/not the whole code/)
    expect(link().attributes('href')).toBe('https://claude.com/cai/oauth/authorize?state=link-2')
    expect(posted(TURN)).toHaveLength(1)
    expect(wrapper.find('[data-testid="assistant-credential-input"]').exists()).toBe(true)
  })

  it('when the instance cannot make a link, the field still takes an API key', async () => {
    linkFails = true
    await openAndAsk()
    expect(wrapper.find('[data-testid="assistant-credential-sign-in"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="assistant-credential-alternative"]').text()).toMatch(/API key/)
    expect(wrapper.find('[data-testid="assistant-credential-input"]').exists()).toBe(true)
  })

  it("an agent's own ANTHROPIC_API_KEY stays an agent credential, with no sign-in", async () => {
    await openAndAsk({ action: 'credential', name: 'ANTHROPIC_API_KEY', why: 'Scout calls Claude itself.', agent: 'scout' })
    expect(wrapper.find('[data-testid="assistant-credential-steps"]').exists()).toBe(false)
    expect(posted('/api/subscriptions/claude-sign-in')).toHaveLength(0)
    expect(calls.find((c) => c.url === CRED && c.method === 'get').params.name).toBe('ANTHROPIC_API_KEY')
  })
})
