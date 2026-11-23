// @vitest-environment jsdom
/**
 * ent#1358 — a credential the assistant asks for, typed into a masked field in
 * the panel. Mounted through the real AssistantWidget and the real store.
 *
 * The value goes once to this instance (`POST /api/enterprise/assistant/credential`)
 * and nowhere else: never into a turn sent to the assistant, never into the
 * conversation the panel keeps in this browser, never into the field after Save.
 * The assistant is told only "Provided" or "Not provided".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const VALUE = 'ntn_the-value-the-person-typed-1358'
const ALLOWANCE = { plan: 'free', period_hours: 24, messages_left: 24, messages_limit: 25, builds_left: 1, builds_limit: 1, reached: null, resets_at: null }
const calls = []
let turns
let credentialSet
let refuse

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
vi.mock('vue-router', () => ({ useRoute: () => ({ path: '/', params: {}, query: {} }) }))
vi.mock('@/stores/agents', () => ({ useAgentsStore: () => ({ fetchAgents: async () => {} }) }))

import AssistantWidget from '@/components/assistant/AssistantWidget.vue'

const TURN = '/api/enterprise/assistant/turn'
const CRED = '/api/enterprise/assistant/credential'
const ASK = { action: 'credential', name: 'NOTION_API_KEY', why: 'Scout reads your Notion pages.', agent: 'scout' }
const final = (text, cards = []) => ({ status: 'final', conversation_id: 'c1', text, cards, allowance: ALLOWANCE })
const posted = (url) => calls.filter((c) => c.url === url && c.method === 'post')

describe('assistant credential field', () => {
  let wrapper

  const openAndAsk = async (card = ASK) => {
    turns = [final('Scout needs your Notion key.', [card])]
    await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="assistant-input"]').setValue('connect notion to scout')
    await wrapper.get('[data-testid="assistant-input"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
  }
  const type = async (value) => {
    await wrapper.get('[data-testid="assistant-credential-input"]').setValue(value)
  }
  const save = async () => {
    await wrapper.get('[data-testid="assistant-credential"]').trigger('submit')
    await flushPromises()
  }

  beforeEach(() => {
    calls.length = 0
    turns = []
    credentialSet = false
    refuse = null
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

  it('shows a masked field that says the assistant cannot see the value and what it is for', async () => {
    await openAndAsk()
    const field = wrapper.get('[data-testid="assistant-credential"]')
    expect(wrapper.get('[data-testid="assistant-credential-input"]').attributes('type')).toBe('password')
    const card = wrapper.get('[data-testid="assistant-card"]').text()
    expect(card).toContain('NOTION_API_KEY for scout')
    expect(card).toContain('The assistant cannot see what you type')
    expect(card).toContain('stored on the agent scout as NOTION_API_KEY')
    expect(field.text()).toContain('Scout reads your Notion pages.')
  })

  it('stores the value on this instance, empties the field, and tells the assistant only "Provided"', async () => {
    await openAndAsk()
    await type(VALUE)
    await save()
    expect(posted(CRED).map((c) => c.body)).toEqual([{ agent: 'scout', name: 'NOTION_API_KEY', value: VALUE }])
    const turnsSent = posted(TURN)
    expect(turnsSent.at(-1).body.message).toBe('Provided: NOTION_API_KEY for the agent scout.')
    expect(turnsSent.at(-1).body.counted).toBe(false)
    // The value is in no turn the assistant was sent, in no message kept here, and not in the field.
    expect(JSON.stringify(turnsSent)).not.toContain(VALUE)
    const { useAssistantStore } = await import('@/stores/assistant')
    expect(JSON.stringify(useAssistantStore().messages)).not.toContain(VALUE)
    expect(JSON.stringify({ ...localStorage })).not.toContain(VALUE)
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(VALUE)
    expect(wrapper.find('[data-testid="assistant-credential-input"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="assistant-credential-outcome"]').text()).toBe('NOTION_API_KEY provided to the agent scout.')
  })

  it('cancel stores nothing and tells the assistant "Not provided"', async () => {
    await openAndAsk()
    await type(VALUE)
    await wrapper.get('[data-testid="assistant-credential-cancel"]').trigger('click')
    await flushPromises()
    expect(posted(CRED)).toEqual([])
    expect(posted(TURN).at(-1).body.message).toBe('Not provided: NOTION_API_KEY for the agent scout.')
    expect(JSON.stringify(calls)).not.toContain(VALUE)
    expect(wrapper.get('[data-testid="assistant-credential-outcome"]').text()).toContain('not provided')
  })

  it('a refused value is named in the field, the card stays open, and the assistant is told nothing', async () => {
    await openAndAsk()
    refuse = { status: 422, detail: { code: 'invalid_format', message: 'That does not look like NOTION_API_KEY. Nothing was stored.' } }
    const before = posted(TURN).length
    await type('wrong')
    await save()
    expect(wrapper.get('[data-testid="assistant-credential-error"]').text()).toContain('does not look like NOTION_API_KEY')
    expect(wrapper.get('[data-testid="assistant-credential-input"]').element.value).toBe('')
    expect(posted(TURN).length).toBe(before)
    // Fixing it is a second try in the same field.
    await type(VALUE)
    await save()
    expect(posted(TURN).at(-1).body.message).toBe('Provided: NOTION_API_KEY for the agent scout.')
  })

  it('says it replaces a value already set, without showing it, and the outcome says replaced', async () => {
    credentialSet = true
    await openAndAsk()
    expect(calls.find((c) => c.url === CRED && c.method === 'get').params).toEqual({ name: 'NOTION_API_KEY', agent: 'scout' })
    expect(wrapper.get('[data-testid="assistant-credential-replaces"]').text())
      .toBe('NOTION_API_KEY is already set on the agent scout. Saving replaces it; the current value is never shown.')
    await type(VALUE)
    await save()
    expect(wrapper.get('[data-testid="assistant-credential-outcome"]').text()).toBe('NOTION_API_KEY replaced on the agent scout.')
  })

  it("the instance's own Claude key is asked for with no agent", async () => {
    await openAndAsk({ action: 'credential', name: 'ANTHROPIC_API_KEY', why: 'Your agents run on Claude.' })
    expect(wrapper.get('[data-testid="assistant-card"]').text()).toContain('stored on this instance as ANTHROPIC_API_KEY')
    await type('sk-ant-api03-example')
    await save()
    expect(posted(CRED)[0].body).toEqual({ agent: null, name: 'ANTHROPIC_API_KEY', value: 'sk-ant-api03-example' })
    expect(posted(TURN).at(-1).body.message).toBe('Provided: ANTHROPIC_API_KEY for this instance.')
  })

  it('a card that carries anything but a name, a reason and a known agent is not shown', async () => {
    await openAndAsk({ ...ASK, value: VALUE })
    expect(wrapper.find('[data-testid="assistant-credential"]').exists()).toBe(false)
  })
})
