// @vitest-environment jsdom
/**
 * trinity-enterprise#1406 — a choice that never reached the assistant can be
 * pressed again. Mounted through the real AssistantWidget and the real store.
 *
 * Seen on the sign-up walk of 2026-11-27 (dev, build a864be8): the three
 * proposed-agent buttons were pressed, the network dropped, "Couldn't reach the
 * assistant" showed, and every button stayed disabled, also after a reload.
 * Only typing the button's text went on.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const ALLOWANCE = { plan: 'free', period_hours: 24, messages_left: 20, messages_limit: 25, builds_left: 0, builds_limit: 1, reached: null, resets_at: null }
const calls = []
let turns

const failure = (props) => Object.assign(new Error('failed'), props)
const NETWORK = () => failure({ code: 'ERR_NETWORK' })
const TIMEOUT = () => failure({ code: 'ECONNABORTED' })
const BAD_GATEWAY = () => failure({ response: { status: 502, data: '<html>502 Bad Gateway</html>' } })
const INTERRUPTED = () => failure({ response: { status: 502, data: { detail: { code: 'gateway_interrupted', message: "Trinity didn't finish answering. Send that again." } } } })
const SERVER_ERROR = () => failure({ response: { status: 500, data: { detail: 'Internal Server Error' } } })

vi.mock('@/api', () => {
  const respond = (method, url, body) => {
    calls.push({ method, url, body })
    if (url === '/api/enterprise/assistant/status') {
      return { available: true, contacted: true, reachable: true, plan: 'free', email: null, allowance: null, leads_setup: true,
        trial_terms: { version: 't0', url: '', accepted: 't0' }, profile_known: true }
    }
    if (url === '/api/onboarding/first-run') return { first_run: false, seeded_agents: [], own_agent_count: 1 }
    if (url === '/api/enterprise/assistant/turn' || url === '/api/enterprise/assistant/turn/continue') {
      const next = turns.shift() || { status: 'final', conversation_id: 'c1', text: 'Thanks.', cards: [], allowance: ALLOWANCE }
      if (typeof next === 'function') throw next()
      return next
    }
    if (url === '/api/agents') return [{ name: 'mine', status: 'running' }]
    if (url === '/api/agents/autonomy-status') return {}
    if (url === '/api/ops/schedules') return { schedules: [] }
    return {}
  }
  const client = {
    get: vi.fn(async (url) => ({ data: respond('get', url) })),
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
const OPTIONS = ['Inbox helper', 'Research scout', 'Weekly writer']
const final = (text, cards = []) => ({ status: 'final', conversation_id: 'c1', text, cards, allowance: ALLOWANCE })
const CHOICE = { action: 'choice', question: 'Which one first?', options: OPTIONS }
const sentTurns = () => calls.filter((c) => c.url === TURN && c.method === 'post').map((c) => c.body.message)

describe('a choice that failed to send can be pressed again (ent#1406)', () => {
  let wrapper

  const open = () => mount(AssistantWidget, {
    attachTo: document.body,
    global: { stubs: { HelpChatWidget: { template: '<div />' } } },
  })
  const offerChoice = async () => {
    turns = [final('Here are three.', [CHOICE])]
    await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="assistant-input"]').setValue('what should I build?')
    await wrapper.get('[data-testid="assistant-input"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
  }
  const buttons = () => wrapper.get('[data-testid="assistant-choice"]').findAll('button')
  const press = async (label) => {
    await buttons().find((b) => b.text() === label).trigger('click')
    await flushPromises()
  }
  const enabled = () => buttons().filter((b) => b.attributes('disabled') === undefined).map((b) => b.text())
  const reload = async () => {
    wrapper.unmount()
    setActivePinia(createPinia())
    wrapper = open()
    await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    sessionStorage.clear()
    calls.length = 0
    turns = []
    wrapper = open()
  })
  afterEach(() => wrapper.unmount())

  it('the network drops: the buttons are usable again, and one click sends the choice', async () => {
    await offerChoice()
    turns = [NETWORK]
    await press('Research scout')

    expect(enabled()).toEqual(OPTIONS)
    expect(wrapper.text()).toContain("your choice wasn't sent")
    expect(wrapper.text()).toContain('press it again')

    turns = [final('Building it.')]
    await press('Research scout')
    expect(sentTurns().slice(-2)).toEqual(['Research scout', 'Research scout'])
    expect(wrapper.text()).toContain('Building it.')
    expect(enabled()).toEqual([])                      // now it reached: it cannot go twice
    expect(wrapper.text()).not.toContain("your choice wasn't sent")
  })

  it('the choice that failed is not left showing as said', async () => {
    await offerChoice()
    turns = [NETWORK]
    await press('Research scout')
    const store = (await import('@/stores/assistant')).useAssistantStore()
    expect(store.messages.filter((m) => m.role === 'user').map((m) => m.content)).toEqual(['what should I build?'])
  })

  it('a proxy that could not reach Trinity (502) is the same: it never arrived', async () => {
    await offerChoice()
    turns = [BAD_GATEWAY]
    await press('Inbox helper')
    expect(enabled()).toEqual(OPTIONS)
  })

  it("Trinity's own 502 (gateway_interrupted) may have taken the turn: closed, with Trinity's words", async () => {
    await offerChoice()
    turns = [INTERRUPTED]
    await press('Inbox helper')
    expect(enabled()).toEqual([])
    expect(wrapper.text()).toContain("Trinity didn't finish answering")
    expect(wrapper.text()).not.toContain("your choice wasn't sent")
  })

  it('a 500 is an answer from Trinity: the choice stays closed', async () => {
    await offerChoice()
    turns = [SERVER_ERROR]
    await press('Inbox helper')
    expect(enabled()).toEqual([])
    expect(wrapper.text()).not.toContain("your choice wasn't sent")
  })

  it('after a reload the buttons are still usable, and one click sends', async () => {
    await offerChoice()
    turns = [NETWORK]
    await press('Weekly writer')
    await reload()

    expect(enabled()).toEqual(OPTIONS)
    turns = [final('On it.')]
    await press('Weekly writer')
    expect(sentTurns().at(-1)).toBe('Weekly writer')
    expect(enabled()).toEqual([])
  })

  it('a choice that reached the assistant (the answer timed out) still cannot be sent twice', async () => {
    await offerChoice()
    turns = [TIMEOUT]
    await press('Research scout')
    expect(enabled()).toEqual([])
    await reload()
    expect(enabled()).toEqual([])
  })

  it('a choice that reached the assistant and then failed mid-turn cannot be sent twice', async () => {
    await offerChoice()
    turns = [{ status: 'working', conversation_id: 'c1', activity: ['Reading'] }, NETWORK]
    await press('Research scout')
    expect(enabled()).toEqual([])
    expect(sentTurns().filter((m) => m === 'Research scout')).toHaveLength(1)
  })
})
