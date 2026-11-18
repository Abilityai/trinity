// @vitest-environment jsdom
/**
 * The assistant panel, mounted: opening it provisions the agent, a message
 * carries the page and fleet, a proposed change becomes a card, and Confirm
 * makes the change with the page's own client before the assistant is told.
 */
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const calls = []
let reply = ''
let scheduleOn = false
// What the instance reports about itself; each test starts registered on the free plan.
const FREE = { agent_name: 'trinity-assistant', exists: true, running: true, mode: 'gateway', gateway_configured: true,
  registered: true, email: 'ada@example.com', gateway_reachable: true, plan: 'free', agents_built: 0, agent_limit: 2,
  limit_reached: false, model_auth: 'passthrough', gateway_mock: true }
let status = { ...FREE }
let verifyFails = false

vi.mock('@/api', () => {
  const respond = (method, url, body) => {
    calls.push({ method, url, body })
    if (url === '/api/enterprise/assistant/status') return { ...status }
    if (url === '/api/enterprise/assistant/provision') return { agent_name: 'trinity-assistant', created: false, persona_installed: true }
    if (url === '/api/enterprise/assistant/catalog') return { templates: [{ id: 'issue-digest', kind: 'template', name: 'Weekly issue digest', summary: 'Mondays.', added: '2026-11-10' }], playbooks: [] }
    if (url === '/api/enterprise/assistant/register') return { status: 'code_sent', email: body.email, dev_code: '424242' }
    if (url === '/api/enterprise/assistant/verify') {
      if (verifyFails) throw { response: { status: 400, data: { detail: { code: 'code_wrong', message: "That code doesn't match." } } } }
      status = { ...FREE }
      return { ...status }
    }
    if (url === '/api/enterprise/assistant/mode') { status = { ...FREE, mode: body.mode, registered: false, plan: null, agent_limit: null }; return { ...status } }
    if (url === '/api/enterprise/assistant/upgrade') { status = { ...status, plan: 'pro', agent_limit: null, limit_reached: false }; return { ...status } }
    if (url === '/api/agents') return [{ name: 'trinity-assistant', status: 'running' }, { name: 'issue-digest', status: 'running' }]
    if (url === '/api/agents/autonomy-status') return { 'issue-digest': { autonomy_enabled: false } }
    if (url === '/api/ops/schedules') return { schedules: [{ id: 's1', agent_name: 'issue-digest', name: 'Mondays', enabled: scheduleOn }] }
    if (url === '/api/agents/trinity-assistant/task') return { execution_id: 'e1' }
    if (url === '/api/agents/trinity-assistant/executions/e1') return { status: 'success', response: reply }
    if (url === '/api/agents/issue-digest/schedules/s1/enable') { scheduleOn = true; return {} }
    if (url === '/api/agents/issue-digest/schedules') return [{ id: 's1', enabled: scheduleOn }]
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
vi.mock('vue-router', () => ({ useRoute: () => ({ path: '/', params: {}, query: {} }) }))
vi.mock('@/stores/agents', () => ({ useAgentsStore: () => ({ fetchAgents: async () => {} }) }))

import AssistantWidget from '@/components/assistant/AssistantWidget.vue'

const settle = async () => {
  for (let i = 0; i < 6; i++) {
    await vi.advanceTimersByTimeAsync(2100)
    await flushPromises()
  }
}

describe('assistant panel', () => {
  let wrapper

  beforeEach(() => {
    calls.length = 0
    scheduleOn = false
    status = { ...FREE }
    verifyFails = false
    vi.useFakeTimers()
    setActivePinia(createPinia())
    wrapper = mount(AssistantWidget, { attachTo: document.body, global: { stubs: { ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' } } } })
  })

  afterEach(() => {
    wrapper.unmount()
    vi.useRealTimers()
  })

  const open = async () => {
    await wrapper.find('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
  }
  const say = async (text) => {
    await wrapper.find('[data-testid="assistant-input"]').setValue(text)
    await wrapper.find('form').trigger('submit')
    await settle()
  }

  it('provisions on open and says what it can see', async () => {
    expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(false)
    await open()
    expect(calls.map((c) => c.url).slice(0, 2)).toEqual(['/api/enterprise/assistant/status', '/api/enterprise/assistant/provision'])
    expect(wrapper.find('[data-testid="assistant-context"]').text()).toBe('Sees what you see: Dashboard · 1 agent')
    expect(wrapper.find('[data-testid="assistant-plan"]').text()).toBe('Free plan · 0 of 2 agents built')
    expect(wrapper.find('[data-testid="assistant-recipes"]').text()).toContain('Weekly issue digest')
  })

  it('sends the page, the fleet and the message, and shows the reply without the card block', async () => {
    reply = 'Schedule added.\n```trinity-card\n{"action":"schedule-on","agent":"issue-digest","schedule_id":"s1"}\n```'
    await open()
    await say('turn on the schedule')

    const task = calls.find((c) => c.url === '/api/agents/trinity-assistant/task')
    expect(task.body.message).toContain('Page: Dashboard')
    expect(task.body.message).toContain('- issue-digest: running, autonomy off, schedules: "Mondays" id=s1 off')
    expect(task.body.message.endsWith('Current message:\nturn on the schedule')).toBe(true)

    const bubbles = wrapper.findAll('.bubble')
    expect(bubbles.map((b) => b.attributes('data-role'))).toEqual(['user', 'assistant'])
    expect(bubbles[1].text()).toBe('Schedule added.')
    expect(wrapper.find('[data-testid="assistant-card"]').text()).toContain('Turn on the schedule "Mondays" for issue-digest')
  })

  it('makes nothing change until Confirm, then changes it once and tells the assistant', async () => {
    reply = '```trinity-card\n{"action":"schedule-on","agent":"issue-digest","schedule_id":"s1"}\n```'
    await open()
    await say('turn on the schedule')
    const enable = () => calls.filter((c) => c.url === '/api/agents/issue-digest/schedules/s1/enable')
    expect(enable()).toHaveLength(0)

    reply = 'It is on.'
    await wrapper.findAll('[data-testid="assistant-card"] button')[0].trigger('click')
    await settle()

    expect(enable()).toHaveLength(1)
    expect(wrapper.find('[data-testid="assistant-card"]').text()).toContain('Done.')
    expect(wrapper.find('[data-testid="assistant-card"]').findAll('button')).toHaveLength(0)
    const tasks = calls.filter((c) => c.url === '/api/agents/trinity-assistant/task')
    expect(tasks[1].body.message.endsWith('Current message:\nDone: Turn on the schedule "Mondays" for issue-digest.')).toBe(true)
    expect(wrapper.find('[data-testid="assistant-note"]').text()).toBe('Done: Turn on the schedule "Mondays" for issue-digest.')
  })

  it('Not now changes nothing and says so to the assistant', async () => {
    reply = '```trinity-card\n{"action":"schedule-on","agent":"issue-digest","schedule_id":"s1"}\n```'
    await open()
    await say('turn on the schedule')
    reply = 'Left as is.'
    await wrapper.findAll('[data-testid="assistant-card"] button')[1].trigger('click')
    await settle()

    expect(calls.some((c) => c.url.endsWith('/enable'))).toBe(false)
    const tasks = calls.filter((c) => c.url === '/api/agents/trinity-assistant/task')
    expect(tasks[1].body.message.endsWith('Current message:\nNot now: Turn on the schedule "Mondays" for issue-digest.')).toBe(true)
  })

  it('a proposal for an agent the page did not read never becomes a card', async () => {
    reply = 'Sure.\n```trinity-card\n{"action":"stop-agent","agent":"trinity-system"}\n```'
    await open()
    await say('stop the system agent')
    expect(wrapper.find('[data-testid="assistant-card"]').exists()).toBe(false)
    expect(wrapper.findAll('.bubble')[1].text()).toBe('Sure.')
  })

  describe('at the door', () => {
    beforeEach(() => {
      status = { ...FREE, mode: null, registered: false, email: null, plan: null, agents_built: null, agent_limit: null }
    })

    it('asks for an email first and provisions nothing until a route is chosen', async () => {
      await open()
      expect(wrapper.find('[data-testid="assistant-door"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="assistant-input"]').exists()).toBe(false)
      expect(calls.some((c) => c.url.endsWith('/provision'))).toBe(false)
    })

    it('refuses a bad email in the page, with an example, before calling anything', async () => {
      await open()
      await wrapper.find('[data-testid="assistant-email"]').setValue('not-an-email')
      await wrapper.find('form').trigger('submit')
      await flushPromises()
      expect(wrapper.find('[data-testid="assistant-door"]').text()).toContain('you@example.com')
      expect(calls.some((c) => c.url.endsWith('/register'))).toBe(false)
    })

    it('registers by email and code, then opens the conversation on the free plan', async () => {
      await open()
      await wrapper.find('[data-testid="assistant-email"]').setValue('Ada@Example.com')
      await wrapper.find('form').trigger('submit')
      await flushPromises()
      expect(calls.find((c) => c.url.endsWith('/register')).body).toEqual({ email: 'ada@example.com' })
      expect(wrapper.find('[data-testid="assistant-dev-code"]').text()).toContain('424242')

      await wrapper.find('[data-testid="assistant-code"]').setValue('424242')
      await wrapper.find('form').trigger('submit')
      await settle()
      expect(calls.find((c) => c.url.endsWith('/verify')).body).toEqual({ email: 'ada@example.com', code: '424242' })
      expect(calls.some((c) => c.url.endsWith('/provision'))).toBe(true)
      expect(wrapper.find('[data-testid="assistant-door"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="assistant-plan"]').text()).toBe('Free plan · 0 of 2 agents built')
    })

    it('keeps the door open and says why when the code is wrong', async () => {
      verifyFails = true
      await open()
      await wrapper.find('[data-testid="assistant-email"]').setValue('ada@example.com')
      await wrapper.find('form').trigger('submit')
      await flushPromises()
      await wrapper.find('[data-testid="assistant-code"]').setValue('000000')
      await wrapper.find('form').trigger('submit')
      await flushPromises()
      expect(wrapper.find('[data-testid="assistant-door"]').text()).toContain("That code doesn't match.")
      expect(calls.some((c) => c.url.endsWith('/provision'))).toBe(false)
    })

    it('the other door is the basic helper, with no registration', async () => {
      await open()
      await wrapper.find('[data-testid="assistant-own-claude"]').trigger('click')
      await settle()
      expect(calls.find((c) => c.url.endsWith('/mode')).body).toEqual({ mode: 'local' })
      expect(calls.some((c) => c.url.endsWith('/register'))).toBe(false)
      expect(wrapper.find('[data-testid="assistant-plan"]').text()).toBe('Basic helper · your own Claude')
      expect(wrapper.find('[data-testid="assistant-recipes"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="assistant-start-pro"]').exists()).toBe(true)
    })
  })

  describe('at the limit', () => {
    beforeEach(() => {
      status = { ...FREE, agents_built: 2, limit_reached: true }
    })

    it('says the allowance is used, keeps the conversation open, and upgrades on one click', async () => {
      reply = 'Welcome to Pro.'
      await open()
      const banner = wrapper.find('[data-testid="assistant-limit"]')
      expect(banner.text()).toContain('2 of 2 agents built')
      expect(banner.text()).toContain('no payment is taken')
      expect(wrapper.find('[data-testid="assistant-input"]').exists()).toBe(true)

      await banner.find('button').trigger('click')
      await settle()
      expect(calls.filter((c) => c.url.endsWith('/upgrade'))).toHaveLength(1)
      expect(wrapper.find('[data-testid="assistant-limit"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="assistant-plan"]').text()).toBe('Trinity Pro')
      const told = calls.filter((c) => c.url === '/api/agents/trinity-assistant/task').pop()
      expect(told.body.message.endsWith('Current message:\nDone: Upgrade this instance to Trinity Pro.')).toBe(true)
    })

    it('an upgrade card from the assistant runs the same upgrade', async () => {
      reply = 'The free allowance is used up.\n```trinity-card\n{"action": "upgrade"}\n```'
      await open()
      await say('build me another agent')
      const card = wrapper.find('[data-testid="assistant-card"]')
      expect(card.text()).toContain('Upgrade this instance to Trinity Pro')
      expect(card.text()).toContain('Mock checkout: no payment is taken.')
      // the banner offering the same upgrade steps aside while the card is open
      expect(wrapper.find('[data-testid="assistant-limit"]').exists()).toBe(false)
      reply = 'You are on Pro.'
      await card.findAll('button')[0].trigger('click')
      await settle()
      expect(calls.filter((c) => c.url.endsWith('/upgrade'))).toHaveLength(1)
      expect(wrapper.find('[data-testid="assistant-card"]').text()).toContain('Done.')
    })
  })
})
