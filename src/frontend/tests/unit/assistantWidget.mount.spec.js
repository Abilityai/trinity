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
let runs = []    // answers to a run's status, in order

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
    if (url === '/api/enterprise/assistant/runs/issue-digest/exec-12345') return runs.shift() || { status: 'running', ended: false, ok: false, error: '' }
    if (url === '/api/enterprise/assistant/tools/confirm') return { ok: true, content: 'Agent scout deleted.' }
    if (url === '/api/enterprise/assistant/terms/trial') return { version: 't0', url: '', text: '# Trial terms\n\nYour messages are sent to Ability AI.' }
    if (url === '/api/enterprise/assistant/terms/accept') return { plan: 'free', email: null, allowance: ALLOWANCE, trial_terms: { version: 't0', url: '', accepted: 't0' } }
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
    runs = []
    status = { available: true, contacted: true, reachable: true, plan: 'free', email: null, allowance: null, trial_terms: { version: 't0', url: '', accepted: 't0' } }
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

  it('opens ready to use and has sent nothing', async () => {
    await openPanel()
    expect(wrapper.get('[data-testid="assistant-welcome"]').text()).toContain("there's nothing to set up")
    expect(wrapper.find('[data-testid="assistant-email"]').exists()).toBe(false)
    expect(calls.filter((c) => c.method !== 'get')).toEqual([])
  })

  it('takes several lines: Enter sends, Shift+Enter does not', async () => {
    await openPanel()
    const box = wrapper.get('[data-testid="assistant-input"]')
    expect(box.element.tagName).toBe('TEXTAREA')
    await box.setValue('first line')
    await box.trigger('keydown', { key: 'Enter', shiftKey: true })
    await flushPromises()
    expect(sent(TURN)).toEqual([])
    await box.setValue('first line\nsecond line')
    await box.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(sent(TURN)[0].body.message).toBe('first line\nsecond line')
    expect(box.element.value).toBe('')
  })

  describe('on an instance where nobody has agreed yet', () => {
    beforeEach(async () => {
      status = { available: true, contacted: false, reachable: null, plan: null, email: null, allowance: null, trial_terms: { version: null, url: '', accepted: null } }
      const { useAssistantStore } = await import('@/stores/assistant')
      await useAssistantStore().loadStatus()
    })

    it('says where the data goes and sends nothing until the person agrees; one click then sends what they wrote', async () => {
      await openPanel()
      const box = wrapper.get('[data-testid="assistant-terms-first"]')
      expect(box.text()).toContain('sent to Ability AI')
      // the terms are one click away, and reading them agrees to nothing
      await wrapper.get('[data-testid="assistant-terms-link"]').trigger('click')
      await flushPromises()
      expect(wrapper.get('[data-testid="assistant-terms-text"]').text()).toContain('Your messages are sent to Ability AI.')
      expect(calls.filter((c) => c.method !== 'get')).toEqual([])
      expect(wrapper.find('[data-testid="assistant-email"]').exists()).toBe(false)
      await say('how are my agents?')
      expect(calls.filter((c) => c.method !== 'get')).toEqual([])
      expect(wrapper.get('[data-testid="assistant-waiting"]').text()).toContain('how are my agents?')
      expect(button('Agree and send')).toBeTruthy()

      await wrapper.get('[data-testid="assistant-agree"]').trigger('click')
      await flushPromises()
      expect(sent('/api/enterprise/assistant/terms/accept')[0].body).toEqual({ accept: true })
      expect(sent(TURN)[0].body.message).toBe('how are my agents?')
      expect(wrapper.find('[data-testid="assistant-terms-first"]').exists()).toBe(false)
    })

    it('agreeing first is one click, and then messages just go', async () => {
      await openPanel()
      expect(button('Agree and start')).toBeTruthy()
      await wrapper.get('[data-testid="assistant-agree"]').trigger('click')
      await flushPromises()
      expect(sent(TURN)).toEqual([])
      await say('hello')
      expect(sent(TURN)[0].body.message).toBe('hello')
    })
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

  it('follows a run on an agent, and brings the assistant back with the result when it ends', async () => {
    vi.useFakeTimers()
    runs = [{ status: 'running', ended: false, ok: false, error: '' }, { status: 'success', ended: true, ok: true, error: '' }]
    turns = [final("It's running. I'll come back with the result.", [{ action: 'run', agent: 'issue-digest', execution_id: 'exec-12345', title: 'Weekly digest' }]),
      final('The digest found three new issues.')]
    await openPanel()
    await say('run the digest now')
    const card = wrapper.get('[data-testid="assistant-card"]')
    expect(card.text()).toContain('Running')
    expect(card.text()).toContain('Weekly digest on issue-digest')
    expect(card.find('[role="progressbar"]').exists()).toBe(true)
    await vi.advanceTimersByTimeAsync(1600)
    await flushPromises()
    expect(sent(TURN)).toHaveLength(1)                      // still going: the assistant is not disturbed
    await vi.advanceTimersByTimeAsync(1600)
    await flushPromises()
    expect(sent(TURN)[1].body).toMatchObject({ counted: false })
    expect(sent(TURN)[1].body.message).toContain('The run "Weekly digest" on issue-digest finished (execution exec-12345)')
    expect(wrapper.findAll('.bubble').map((b) => b.text()).at(-1)).toBe('The digest found three new issues.')
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
    vi.useRealTimers()
  })

  it('does not follow a run on an agent this instance does not have', async () => {
    turns = [final('Running.', [{ action: 'run', agent: 'someone-elses', execution_id: 'exec-12345', title: 'X' }])]
    await openPanel()
    await say('run it')
    expect(wrapper.find('[data-testid="assistant-card"]').exists()).toBe(false)
  })

  it('shows each step of a long turn as it happens, with what it said it would do', async () => {
    let release
    const held = new Promise((resolve) => { release = resolve })
    turns = [{ status: 'working', conversation_id: 'c1', text: "I'll check every agent's schedules.", activity: ['Looking at your agents'], cards: [], allowance: ALLOWANCE },
      { status: 'working', conversation_id: 'c1', activity: ['List agent schedules: issue-digest'], cards: [], allowance: ALLOWANCE }]
    const api = (await import('@/api')).default
    const post = api.post.getMockImplementation()
    let n = 0
    api.post.mockImplementation(async (url, body) => {
      if (url === `${TURN}/continue` && ++n === 2) { await held; return { data: final('Two schedules.') } }
      return post(url, body)
    })
    await openPanel()
    await say('what runs on a schedule?')
    const working = wrapper.get('[data-testid="assistant-working"]')
    expect(working.text()).toContain("I'll check every agent's schedules.")
    expect(working.findAll('li').map((li) => li.text())).toEqual(['Looking at your agents', 'List agent schedules: issue-digest'])
    release()
    await flushPromises()
    api.post.mockImplementation(post)
    expect(wrapper.find('[data-testid="assistant-working"]').exists()).toBe(false)
    expect(wrapper.findAll('.bubble').map((b) => b.text()).at(-1)).toBe('Two schedules.')
  })

  it('says on the launcher that an answer arrived while the panel was closed', async () => {
    vi.useFakeTimers()
    runs = [{ status: 'failed', ended: true, ok: false, error: 'Credit balance is too low' }]
    turns = [final('Running.', [{ action: 'run', agent: 'issue-digest', execution_id: 'exec-12345', title: 'Weekly digest' }]),
      final('It failed: the Claude account is out of credit.')]
    await openPanel()
    await say('run the digest now')
    await wrapper.get('[aria-label="Close"], [aria-label="Close the assistant"], [data-testid="assistant-close"]').trigger('click')
    expect(wrapper.get('[data-testid="assistant-launcher-busy"]').exists()).toBe(true)
    await vi.advanceTimersByTimeAsync(1600)
    await flushPromises()
    expect(sent(TURN)[1].body.message).toContain('failed (execution exec-12345). Credit balance is too low')
    expect(wrapper.get('[data-testid="assistant-launcher-unread"]').text()).toBe('1')
    await openPanel()
    expect(wrapper.find('[data-testid="assistant-launcher-unread"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="assistant-card"]').text()).toContain('Credit balance is too low')
    vi.useRealTimers()
  })

  it('offers things to do for this fleet; one that needs finishing goes into the box, not out', async () => {
    await openPanel()
    const groups = wrapper.findAll('[data-testid="assistant-starters"]')
    expect(groups.map((g) => g.find('p').text())).toEqual(['Check', 'Do', 'Build'])
    await button('Ask an agent…').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="assistant-input"]').element.value).toBe('Ask issue-digest ')
    expect(sent(TURN)).toEqual([])
    await button('What runs on a schedule?').trigger('click')
    await flushPromises()
    expect(sent(TURN)[0].body.message).toBe('What runs on a schedule?')
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
    // What the limit stopped is asked again, so subscribing answers their question and not just itself.
    expect(sent(TURN)[1].body).toMatchObject({ counted: false })
    expect(sent(TURN)[1].body.message).toBe('Done: Subscribe to Trinity Pro. Now answer what they asked just before, which the limit stopped: "one more thing"')
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
