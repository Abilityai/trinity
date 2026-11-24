// @vitest-environment jsdom
/**
 * ent#1354 (ruled 2026-11-24) — connecting Claude in the assistant panel with a
 * token the person makes in a terminal. Mounted through the real
 * AssistantWidget and the real store.
 *
 * When the assistant asks for this instance's Claude credential, the card
 * shows three steps — open a terminal on your own computer, run
 * `claude setup-token`, paste the token it prints here — with the API key as
 * the alternative in the same field. The page makes no sign-in link and asks
 * the instance for none. What is pasted goes once to this instance under the
 * name the assistant asked for, never into a turn; a refused paste is named
 * in the instance's words and sends the assistant nothing.
 *
 * The walk on build 8d7ca11e5 (2026-11-24): the gateway asks under
 * ANTHROPIC_API_KEY. The panel renamed a fresh card to CLAUDE_SUBSCRIPTION but
 * not one kept from an earlier conversation, so two names were in play and the
 * assistant heard "Provided:" for a name it never asked for. Now a card keeps
 * its name, a kept card behaves like a fresh one, and the instance takes both
 * names as one Claude card (enterprise test_1354_gateway_key_step.py).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const VALUE = 'sk-ant-oat01-' + 't'.repeat(40)
const ALLOWANCE = { plan: 'free', period_hours: 24, messages_left: 24, messages_limit: 25, builds_left: 1, builds_limit: 1, reached: null, resets_at: null }
const calls = []
let turns
let credentialSet
let credentialCurrent
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
      return { agent: config?.params?.agent ?? null, name: config?.params?.name, set: credentialSet, current: credentialCurrent, use: '' }
    }
    if (url === '/api/enterprise/assistant/credential' && method === 'post') {
      if (refuse) {
        const r = refuse
        refuse = null
        throw { response: { status: r.status, data: { detail: r.detail } } }
      }
      return { provided: true, agent: body.agent, name: body.name, replaced: credentialSet === true }
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

describe('connecting Claude with a token made in a terminal', () => {
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

  beforeEach(() => {
    calls.length = 0
    turns = []
    credentialSet = false
    credentialCurrent = null
    refuse = null
    refuseState = null
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

  it('shows three terminal steps naming claude setup-token, the API key as the alternative, and no sign-in link', async () => {
    await openAndAsk()
    const steps = wrapper.get('[data-testid="assistant-credential-steps"]').findAll('li')
    expect(steps).toHaveLength(3)
    expect(steps[0].text()).toMatch(/open a terminal on your own computer/i)
    expect(steps[1].text()).toMatch(/^Run claude setup-token/)
    expect(wrapper.get('[data-testid="assistant-credential-command"]').text()).toBe('claude setup-token')
    expect(steps[2].text()).toMatch(/paste the token it prints here/i)
    expect(wrapper.get('[data-testid="assistant-credential-alternative"]').text()).toMatch(/API key/)
    expect(wrapper.get('[data-testid="assistant-credential-input"]').attributes('type')).toBe('password')
    expect(wrapper.get('[data-testid="assistant-credential-input"]').attributes('placeholder')).toBe('Paste the token (or an API key)')
    // no link out to a sign-in, and no request for one
    expect(wrapper.get('[data-testid="assistant-credential-steps"]').find('a').exists()).toBe(false)
    expect(calls.some((c) => c.url.includes('sign-in'))).toBe(false)
    // the instance is asked about the name the assistant asked for
    const state = calls.find((c) => c.url === CRED && c.method === 'get')
    expect(state.params.name).toBe('ANTHROPIC_API_KEY')
  })

  it.each(['ANTHROPIC_API_KEY', 'CLAUDE_SUBSCRIPTION'])('posts the paste once to this instance under %s, the name asked for, and tells the assistant only that it was provided', async (name) => {
    await openAndAsk({ ...ASK, name })
    expect(wrapper.get('[data-testid="assistant-credential-steps"]').findAll('li')).toHaveLength(3)
    await paste(VALUE)
    const stored = posted(CRED)
    expect(stored).toHaveLength(1)
    expect(stored[0].body).toEqual({ agent: null, name, value: VALUE })
    const told = posted(TURN).slice(1)
    expect(told).toHaveLength(1)
    expect(JSON.stringify(told)).not.toContain(VALUE)
    expect(told[0].body.message).toBe(`Provided: ${name} for this instance.`)
    expect(JSON.stringify(sessionStorage)).not.toContain(VALUE)
    expect(JSON.stringify(localStorage)).not.toContain(VALUE)
  })

  it.each(['ANTHROPIC_API_KEY', 'CLAUDE_SUBSCRIPTION'])('on an instance holding only an API key, the %s card says so and never that a subscription is set', async (name) => {
    // The walk (2026-11-24 14:50Z): "CLAUDE_SUBSCRIPTION is already set on this
    // instance" before any subscription existed; only a bad API key did.
    credentialSet = true
    credentialCurrent = 'api_key'
    await openAndAsk({ ...ASK, name })
    const note = wrapper.get('[data-testid="assistant-credential-replaces"]').text()
    expect(note).toMatch(/^This instance already has an Anthropic API key\./)
    expect(note).not.toMatch(/subscription is|is already set/)
  })

  it('names a subscription the instance does hold', async () => {
    credentialSet = true
    credentialCurrent = 'subscription'
    await openAndAsk({ ...ASK, name: 'CLAUDE_SUBSCRIPTION' })
    expect(wrapper.get('[data-testid="assistant-credential-replaces"]').text()).toMatch(/^This instance already has a Claude subscription\./)
  })

  it('a pasted sign-in code is refused in plain words and sends the assistant nothing', async () => {
    await openAndAsk()
    refuse = { status: 422, detail: { code: 'token_expected', message: 'That looks like a sign-in code, not a token. Run claude setup-token in a terminal on your own computer and paste the token it prints (it starts with sk-ant-oat01-), or paste an Anthropic API key (sk-ant-api…). Nothing was stored.' } }
    await paste('a-code-from-a-page#its-state')
    expect(wrapper.get('[data-testid="assistant-credential-error"]').text()).toMatch(/sign-in code, not a token/)
    expect(posted(TURN)).toHaveLength(1)
    expect(wrapper.find('[data-testid="assistant-credential-input"]').exists()).toBe(true)
    expect(JSON.stringify(calls.filter((c) => c.url === TURN))).not.toContain('its-state')
  })

  it('a Claude card kept from an earlier conversation shows the same steps and posts the same way', async () => {
    wrapper.unmount()
    localStorage.setItem('trinity-assistant-conversation', JSON.stringify({
      at: Date.now(), conversationId: 'c0', since: null,
      messages: [{ id: 'm1', role: 'assistant', content: 'Connect Claude below.',
        cards: [{ id: 'k1', card: { action: 'credential', name: 'ANTHROPIC_API_KEY', why: 'Your agent runs on Claude.' }, state: 'open' }] }],
    }))
    setActivePinia(createPinia())
    wrapper = mount(AssistantWidget, {
      attachTo: document.body,
      global: { stubs: {
        ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' },
        HelpChatWidget: { template: '<div />' },
      } },
    })
    await flushPromises()
    await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="assistant-credential-steps"]').findAll('li')).toHaveLength(3)
    await paste(VALUE)
    expect(posted(CRED).map((c) => c.body)).toEqual([{ agent: null, name: 'ANTHROPIC_API_KEY', value: VALUE }])
    expect(posted(TURN).at(-1).body.message).toBe('Provided: ANTHROPIC_API_KEY for this instance.')
  })

  it("an agent's own ANTHROPIC_API_KEY stays an agent credential, with no token steps", async () => {
    await openAndAsk({ action: 'credential', name: 'ANTHROPIC_API_KEY', why: 'Scout calls Claude itself.', agent: 'scout' })
    expect(wrapper.find('[data-testid="assistant-credential-steps"]').exists()).toBe(false)
    expect(calls.find((c) => c.url === CRED && c.method === 'get').params.name).toBe('ANTHROPIC_API_KEY')
  })
})
