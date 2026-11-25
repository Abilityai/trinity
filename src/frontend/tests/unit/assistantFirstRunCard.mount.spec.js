// @vitest-environment jsdom
/**
 * ent#1355 — the first session's first run, started from the assistant panel.
 * Mounted through the real AssistantWidget and the real store.
 *
 * In the first session the gateway sends the run as one `chat_with_agent`
 * tool card (the trial included). The page titles it in its own words,
 * "Run its first task: <agent>", runs it as the person on their click through
 * `/tools/confirm`, and tells the assistant "Done: Run its first task: <agent>."
 * with what the instance answered (the run's execution id), so it can follow
 * the run. Outside the first session the same card keeps its plain title.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const ALLOWANCE = { plan: 'free', period_hours: 24, messages_left: 20, messages_limit: 25, builds_left: 0, builds_limit: 1, reached: null, resets_at: null }
const RECEIPT = '{"status": "accepted", "execution_id": "exec-1355abc"}'
const calls = []
let turns

vi.mock('@/api', () => {
  const respond = (method, url, body) => {
    calls.push({ method, url, body })
    if (url === '/api/enterprise/assistant/status') {
      return { available: true, contacted: true, reachable: true, plan: 'free', email: null, allowance: null, leads_setup: true,
        trial_terms: { version: 't0', url: '', accepted: 't0' }, profile_known: true }
    }
    if (url === '/api/enterprise/assistant/turn' || url === '/api/enterprise/assistant/turn/continue') {
      return turns.shift() || { status: 'final', conversation_id: 'c1', text: 'It is running.', cards: [], allowance: ALLOWANCE }
    }
    if (url === '/api/enterprise/assistant/tools/confirm') return { ok: true, content: RECEIPT }
    if (url === '/api/agents') return [{ name: 'scout', status: 'running' }]
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
import { FIRST_RESULT_KEY } from '@/utils/firstSession'

const TURN = '/api/enterprise/assistant/turn'
const CONFIRM = '/api/enterprise/assistant/tools/confirm'
const ARGS = { agent_name: 'scout', message: '/add-source https://rosa-bakery.example', parallel: true, async: true }
const RUN = { action: 'tool', tool: 'chat_with_agent', args: ARGS }
const posted = (url) => calls.filter((c) => c.url === url && c.method === 'post')

describe('the first run, from the assistant panel', () => {
  let wrapper

  const offered = async () => {
    turns = [{ status: 'final', conversation_id: 'c1', text: 'Press the button below to start scout.', cards: [RUN], allowance: ALLOWANCE }]
    await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="assistant-input"]').setValue('go on')
    await wrapper.get('[data-testid="assistant-input"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    return wrapper.get('[data-testid="assistant-card"]')
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    calls.length = 0
    turns = []
    wrapper = mount(AssistantWidget, { attachTo: document.body })
  })
  afterEach(() => wrapper.unmount())

  it('is one card titled "Run its first task" that runs on the click and tells the assistant the run id', async () => {
    const card = await offered()
    expect(card.text()).toContain('Run its first task: scout')
    expect(card.text()).toContain('Starts scout on its first task now, as you.')
    expect(card.text()).not.toContain('counts as one of your messages')   // the click is free (Abilityai/trinity-gateway#37)
    expect(posted(CONFIRM)).toEqual([])                     // nothing ran before the click

    await card.findAll('button').find((b) => b.text() === 'Run its first task').trigger('click')
    await flushPromises()

    expect(posted(CONFIRM).map((c) => c.body)).toEqual([{ tool: 'chat_with_agent', args: ARGS }])
    const told = posted(TURN).at(-1).body
    expect(told.message.startsWith('Done: Run its first task: scout.')).toBe(true)
    expect(told.message).toContain('exec-1355abc')
    expect(told.context.first_session).toBe(true)
  })

  it('keeps its plain title once the first session is over', async () => {
    wrapper.unmount()
    localStorage.setItem(FIRST_RESULT_KEY, '1')            // the first result was seen, and scout is theirs
    wrapper = mount(AssistantWidget, { attachTo: document.body })
    const card = await offered()
    expect(card.text()).toContain('Chat with agent: scout')
    expect(card.text()).not.toContain('Run its first task')
  })
})
