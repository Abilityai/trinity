// @vitest-environment jsdom
/**
 * The assistant panel, mounted. Nothing is sent until the person sends a
 * message; a turn is followed step by step; a proposed change or a design
 * becomes a card the person confirms; and when the allowance runs out the
 * person signs in, accepts the terms themselves and subscribes.
 */
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const DESIGN_ID = '8f8f7d58-8429-4c25-be28-1e6e6bfc82dc'
const ALLOWANCE = { plan: 'free', period_hours: 24, messages_left: 24, messages_limit: 25, builds_left: 1, builds_limit: 1, reached: null, resets_at: null }
const calls = []
let status
let turns        // answers to /turn and /turn/continue, in order
let scheduleOn
let verifyFails
let jobs         // answers to the job's status, in order

vi.mock('@/api', () => {
  const respond = (method, url, body) => {
    calls.push({ method, url, body })
    if (url === '/api/enterprise/assistant/status') return { ...status }
    if (url === '/api/enterprise/assistant/turn' || url === '/api/enterprise/assistant/turn/continue') {
      return turns.shift() || { status: 'final', conversation_id: 'c1', text: 'Done.', cards: [], allowance: ALLOWANCE }
    }
    if (url === `/api/enterprise/assistant/designs/${DESIGN_ID}/build`) {
      return { agents: [{ name: 'shop-desk', created: true, files: 11, schedule: { id: 's9', name: 'Morning' }, problem: '' }] }
    }
    if (url === `/api/enterprise/assistant/jobs/${DESIGN_ID}`) return jobs.shift() || { status: 'running', steps_done: 0, steps: 3, doing: 'Working' }
    if (url === '/api/enterprise/assistant/tools/confirm') return { ok: true, content: 'Agent scout deleted.' }
    if (url === '/api/enterprise/assistant/account/start') return { email: body.email, dev_code: '424242', terms: { version: 't1', url: '' } }
    if (url === '/api/enterprise/assistant/account/verify') {
      if (verifyFails) throw { response: { status: 400, data: { detail: { code: 'wrong_code', message: "That code didn't match." } } } }
      return { plan: 'free', email: body.email, terms_accepted: 't1' }
    }
    if (url === '/api/enterprise/assistant/subscription/start') return { plan: 'pro', billing: 'unbilled', checkout_url: null, email: 'ada@example.com' }
    if (url === '/api/agents') return [{ name: 'trinity-system', status: 'running', is_system: true }, { name: 'issue-digest', status: 'running' }]
    if (url === '/api/agents/autonomy-status') return { 'issue-digest': { autonomy_enabled: false } }
    if (url === '/api/ops/schedules') return { schedules: [{ id: 's1', agent_name: 'issue-digest', name: 'Mondays', enabled: scheduleOn }] }
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

const final = (text, cards = [], allowance = ALLOWANCE) => ({ status: 'final', conversation_id: 'c1', text, cards, allowance })
const sent = (url) => calls.filter((c) => c.url === url)
const TURN = '/api/enterprise/assistant/turn'

describe('assistant panel', () => {
  let wrapper

  const openPanel = async () => {
    await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
  }
  const say = async (text) => {
    await wrapper.get('[data-testid="assistant-input"]').setValue(text)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
  }
  const button = (words) => wrapper.findAll('button').find((b) => b.text() === words)

  beforeEach(() => {
    calls.length = 0
    turns = []
    scheduleOn = false
    verifyFails = false
    jobs = []
    status = { available: true, contacted: false, reachable: null, plan: null, email: null, allowance: null }
    setActivePinia(createPinia())
    wrapper = mount(AssistantWidget, {
      attachTo: document.body,
      global: { stubs: {
        ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' },
        HelpChatWidget: { props: ['embedded'], template: '<div data-testid="ask-tab">ask</div>' },
      } },
    })
  })

  afterEach(() => wrapper.unmount())

  it('opens ready to use, says where messages go, and has sent nothing', async () => {
    await openPanel()
    expect(wrapper.get('[data-testid="assistant-welcome"]').text()).toContain("there's nothing to set up")
    expect(wrapper.get('[data-testid="assistant-disclosure"]').text()).toContain('sent to Trinity')
    expect(wrapper.find('[data-testid="assistant-email"]').exists()).toBe(false)
    expect(calls.filter((c) => c.method !== 'get')).toEqual([])
  })

  it('keeps Ask one tab away', async () => {
    await openPanel()
    expect(wrapper.get('[data-testid="ask-tab"]').isVisible()).toBe(false)
    await wrapper.findAll('[role="radio"]').find((b) => b.text() === 'Ask the docs').trigger('click')
    expect(wrapper.get('[data-testid="ask-tab"]').isVisible()).toBe(true)
    expect(wrapper.find('[data-testid="assistant-input"]').exists()).toBe(false)
  })

  it('sends the message with the page and the agents, and follows a turn step by step', async () => {
    turns = [{ status: 'working', conversation_id: 'c1', activity: ['Looking at your agents'], cards: [], allowance: ALLOWANCE },
      final('You have one agent, and it is running.')]
    await openPanel()
    await say('how are my agents?')
    const first = sent(TURN)[0].body
    expect(first).toMatchObject({ message: 'how are my agents?', counted: true, conversation_id: null,
      context: { page: 'Dashboard', fleet: [{ name: 'issue-digest', status: 'running' }] } })
    expect(sent(`${TURN}/continue`)[0].body).toEqual({ conversation_id: 'c1' })
    expect(wrapper.findAll('.bubble').map((b) => b.text())).toEqual(['how are my agents?', 'You have one agent, and it is running.'])
    expect(wrapper.get('[data-testid="assistant-plan"]').text()).toBe('Free · 24 messages left today')
  })

  it('carries the conversation on in the next message', async () => {
    turns = [final('Hello.'), final('Yes.')]
    await openPanel()
    await say('hi')
    await say('still there?')
    expect(sent(TURN)[1].body.conversation_id).toBe('c1')
  })

  it('makes a confirmed change itself, reads it back, and tells the assistant without counting it', async () => {
    turns = [final('I can turn that on.', [{ action: 'schedule-on', agent: 'issue-digest', schedule_id: 's1' }]), final('It is on.')]
    await openPanel()
    await say('turn on the Monday schedule')
    expect(wrapper.get('[data-testid="assistant-card"]').text()).toContain('Turn on the schedule "Mondays" for issue-digest')
    await button('Confirm').trigger('click')
    await flushPromises()
    expect(sent('/api/agents/issue-digest/schedules/s1/enable')).toHaveLength(1)
    const outcome = sent(TURN)[1].body
    expect(outcome.message).toBe('Done: Turn on the schedule "Mondays" for issue-digest.')
    expect(outcome.counted).toBe(false)
    expect(wrapper.get('[data-testid="assistant-card"]').text()).toContain('Done.')
  })

  it('shows nothing for a change it was not allowed to propose', async () => {
    turns = [final('Deleting it.', [{ action: 'delete-agent', agent: 'issue-digest' }, { action: 'start-agent', agent: 'someone-else' }])]
    await openPanel()
    await say('delete it')
    expect(wrapper.find('[data-testid="assistant-card"]').exists()).toBe(false)
  })

  it('shows a design in plain words and builds it only when Build is pressed', async () => {
    const design = { title: 'Front desk', summary: 'Reads your inbox each morning.', to_do_after: [],
      agents: [{ name: 'shop-desk', job: 'Reads the inbox and drafts replies.', credentials: ['MAILBOX_READ_CREDENTIAL'], schedule: { name: 'Morning', cron: '0 8 * * 1-5' }, calls: [] }] }
    turns = [final('Here it is.', [{ action: 'build', design_id: DESIGN_ID, design }]), final('It is built.')]
    await openPanel()
    await say('build me an inbox agent')
    const card = wrapper.get('[data-testid="assistant-card"]').text()
    expect(card).toContain('Reads the inbox and drafts replies.')
    expect(card).toContain("You'll add: MAILBOX_READ_CREDENTIAL")
    expect(card).toContain('added switched off')
    expect(sent(`/api/enterprise/assistant/designs/${DESIGN_ID}/build`)).toHaveLength(0)
    await button('Build').trigger('click')
    await flushPromises()
    expect(sent(`/api/enterprise/assistant/designs/${DESIGN_ID}/build`)).toHaveLength(1)
    expect(sent(TURN)[1].body.message).toBe('Done: Build "Front desk". Built shop-desk (11 files, schedule "Morning" added switched off, id s9).')
  })

  it('runs a change that cannot be undone only on the click, showing exactly what will run', async () => {
    turns = [final('This deletes scout and its files.', [{ action: 'tool', tool: 'delete_agent', args: { name: 'scout' } }]), final('Gone.')]
    await openPanel()
    await say('delete scout')
    const card = wrapper.get('[data-testid="assistant-card"]').text()
    expect(card).toContain('Cannot be undone')
    expect(card).toContain('Delete agent: scout')
    expect(sent('/api/enterprise/assistant/tools/confirm')).toHaveLength(0)
    await button('Confirm').trigger('click')
    await flushPromises()
    expect(sent('/api/enterprise/assistant/tools/confirm')[0].body).toEqual({ tool: 'delete_agent', args: { name: 'scout' } })
    expect(sent(TURN)[1].body.message).toBe('Done: Delete agent: scout. Agent scout deleted.')
  })

  it('follows a build in the making and turns it into its Build card when it is done', async () => {
    vi.useFakeTimers()
    const design = { title: 'Review summary', summary: 'Summarises reviews.', to_do_after: [],
      agents: [{ name: 'velofix-reviews', job: 'Summarise the reviews.', credentials: [], schedule: null, calls: [] }] }
    jobs = [{ status: 'running', steps_done: 1, steps: 3, doing: 'Writing the playbook summarise' },
      { status: 'done', steps_done: 3, steps: 3, card: { action: 'build', design_id: DESIGN_ID, design } }]
    turns = [final('It is being made.', [{ action: 'job', job_id: DESIGN_ID, title: 'Review summary' }]), final('It is ready.')]
    await openPanel()
    await say('make me a review summariser')
    expect(wrapper.get('[data-testid="assistant-card"]').text()).toContain('Being made')
    await vi.advanceTimersByTimeAsync(4100)
    await flushPromises()
    expect(wrapper.get('[data-testid="assistant-job"]').text()).toContain('Writing the playbook summarise (step 2 of 3)')
    await vi.advanceTimersByTimeAsync(4100)
    await flushPromises()
    const card = wrapper.get('[data-testid="assistant-card"]').text()
    expect(card).toContain('Ready to build')
    expect(card).toContain('velofix-reviews')
    expect(button('Build')).toBeTruthy()
    expect(sent(TURN)[1].body).toMatchObject({ counted: false })
    expect(sent(TURN)[1].body.message).toContain('passed the standard')
    vi.useRealTimers()
  })

  it('tells the assistant when the person says not now, and makes no change', async () => {
    turns = [final('Start it?', [{ action: 'start-agent', agent: 'issue-digest' }]), final('Fine.')]
    await openPanel()
    await say('start it')
    await button('Not now').trigger('click')
    await flushPromises()
    expect(sent('/api/agents/issue-digest/start')).toHaveLength(0)
    expect(sent(TURN)[1].body.message).toBe('Not now: Start issue-digest.')
  })

  it('at the limit, signs the person in, takes their own tick on the terms, and subscribes', async () => {
    turns = [{ status: 'limit', conversation_id: 'c1', text: '', cards: [],
      allowance: { ...ALLOWANCE, messages_left: 0, reached: 'messages', resets_at: new Date(Date.now() + 5 * 3600000).toISOString() } }]
    await openPanel()
    await say('one more thing')
    expect(wrapper.get('[data-testid="assistant-limit"]').text()).toContain('in about 5 hours')
    await wrapper.get('[data-testid="assistant-upgrade"]').trigger('click')
    await wrapper.get('[data-testid="assistant-email"]').setValue('ada@example.com')
    await wrapper.get('[data-testid="assistant-account"] form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[data-testid="assistant-dev-code"]').text()).toContain('424242')

    await wrapper.get('[data-testid="assistant-code"]').setValue('424242')
    await wrapper.get('[data-testid="assistant-account"] form').trigger('submit')
    await flushPromises()
    expect(sent('/api/enterprise/assistant/account/verify')).toHaveLength(0)   // the terms were not ticked
    expect(wrapper.get('[data-testid="assistant-account"]').text()).toContain('Accept the terms to continue.')

    await wrapper.get('[data-testid="assistant-terms"]').setValue(true)
    await wrapper.get('[data-testid="assistant-account"] form').trigger('submit')
    await flushPromises()
    expect(sent('/api/enterprise/assistant/account/verify')[0].body).toEqual({ email: 'ada@example.com', code: '424242', accept_terms: true, terms_version: 't1' })

    turns = [final('You are on Trinity Pro now.', [], { ...ALLOWANCE, plan: 'pro', messages_left: 600 })]
    await wrapper.get('[data-testid="assistant-subscribe"]').trigger('click')
    await flushPromises()
    expect(sent('/api/enterprise/assistant/subscription/start')).toHaveLength(1)
    expect(sent(TURN)[1].body).toMatchObject({ message: 'Done: Subscribe to Trinity Pro.', counted: false })
    expect(wrapper.find('[data-testid="assistant-account"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="assistant-limit"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="assistant-plan"]').text()).toBe('Trinity Pro')
  })

  it('says so when the code is wrong and stays on the code step', async () => {
    verifyFails = true
    turns = [final('Subscribe?', [{ action: 'upgrade' }])]
    await openPanel()
    await say('I want more')
    await button('Continue').trigger('click')
    await wrapper.get('[data-testid="assistant-email"]').setValue('ada@example.com')
    await wrapper.get('[data-testid="assistant-account"] form').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-testid="assistant-code"]').setValue('000000')
    await wrapper.get('[data-testid="assistant-terms"]').setValue(true)
    await wrapper.get('[data-testid="assistant-account"] form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[data-testid="assistant-account"]').text()).toContain("That code didn't match.")
    expect(wrapper.find('[data-testid="assistant-code"]').exists()).toBe(true)
  })

  it('refuses a malformed email before asking Trinity for anything', async () => {
    turns = [final('Subscribe?', [{ action: 'upgrade' }])]
    await openPanel()
    await say('I want more')
    await button('Continue').trigger('click')
    await wrapper.get('[data-testid="assistant-email"]').setValue('not-an-email')
    await wrapper.get('[data-testid="assistant-account"] form').trigger('submit')
    await flushPromises()
    expect(sent('/api/enterprise/assistant/account/start')).toHaveLength(0)
    expect(wrapper.get('[data-testid="assistant-account"]').text()).toContain('full email address')
  })
})
