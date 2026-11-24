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
let startFails       // /account/start answers 502
let refuseSubscribe  // /subscription/start answers 409 sign_in_first, and the terms move on
let jobs         // answers to the job's status, in order
let runs = []    // answers to a run's status, in order
let afterAgreeing = null   // the line Trinity opens with once someone has agreed
let checklist = {}       // what the getting-started endpoint answers
let statusReads = []     // answers to /status, in order, before falling back to `status`
let byCode = false       // the module offers the subscribe-by-code routes; off: they answer 404 (an older module)
let subTerms             // what GET /subscription/terms answers
let refusals = {}        // url → [{ status, detail }], thrown in order by the by-code routes
let confirmEmail         // the `email` /subscription/confirm answers: masked or null at the door
let codeVersion          // the terms version /subscription/code names; undefined: the one served
let agentsList = null    // what /api/agents answers when set (ent#1355: an instance with no agent of the person's own)
let credentialAnswer     // what POST /credential answers (ent#1355: the instance's save, count and check)
let agentsWait = null    // when set, /api/agents answers only once this settles (a slow agent list)
let externalAccess       // what GET /api/settings/mcp-external-access answers (ent#1353); a `fails` key throws

vi.mock('@/api', () => {
  const respond = (method, url, body) => {
    calls.push({ method, url, body })
    if (url.startsWith('/api/enterprise/assistant/subscription/') && url !== '/api/enterprise/assistant/subscription/start') {
      if (!byCode) throw { response: { status: 404, data: { detail: 'Not Found' } } }
      const refusal = (refusals[url] || []).shift()
      if (refusal) throw { response: { status: refusal.status, data: { detail: refusal.detail } } }
      if (url.endsWith('/subscription/terms')) return { ...subTerms }
      if (url.endsWith('/subscription/code') || url.endsWith('/subscription/code/resend')) {
        return { email: 'a•••@example.com', expires_in: 900, terms: { version: codeVersion === undefined ? subTerms.version : codeVersion, url: subTerms.url } }
      }
      if (url.endsWith('/subscription/confirm')) return { plan: 'pro', billing: 'unbilled', checkout_url: null, email: confirmEmail }
    }
    if (url === '/api/enterprise/assistant/status') {
      const read = statusReads.length ? statusReads.shift() : { ...status }
      if (read.fails) throw { response: { status: 500, data: { detail: 'The assistant is not answering.' } } }
      return read
    }
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
    if (url === '/api/enterprise/assistant/terms/subscription') return { version: 't1', url: '', text: '# Terms\n\nUsage sharing is turned on.' }
    if (url === '/api/enterprise/assistant/terms/accept') {
      status = { ...status, contacted: true, reachable: true, plan: 'free', allowance: ALLOWANCE, trial_terms: { version: 't0', url: '', accepted: 't0' }, nudge: afterAgreeing }
      return { plan: 'free', email: null, allowance: ALLOWANCE, trial_terms: { version: 't0', url: '', accepted: 't0' } }
    }
    if (url === '/api/enterprise/assistant/credential' && method === 'get') return { set: false }
    if (url === '/api/enterprise/assistant/credential') return credentialAnswer
    if (url === '/api/enterprise/assistant/feedback') return { ok: true }
    if (url === '/api/enterprise/assistant/opened') return { ok: true }
    if (url === '/api/enterprise/onboarding/checklist') return checklist
    if (url === '/api/enterprise/assistant/account/start' && startFails) {
      throw { response: { status: 502, data: { detail: { code: 'mail_failed', message: "The code couldn't be sent right now." } } } }
    }
    if (url === '/api/enterprise/assistant/account/start') return { email: body.email, dev_code: '424242', terms: { version: 't1', url: '' } }
    if (url === '/api/enterprise/assistant/account/verify') {
      if (verifyFails) throw { response: { status: 400, data: { detail: { code: 'wrong_code', message: "That code didn't match." } } } }
      return { plan: 'free', email: body.email, terms_accepted: 't1' }
    }
    if (url === '/api/enterprise/assistant/subscription/start' && refuseSubscribe) {
      refuseSubscribe = false
      status = { ...status, terms_accepted: 't0', terms: { version: 't1', url: '' } }
      throw { response: { status: 409, data: { detail: { code: 'sign_in_first', message: 'Accept the current terms first.' } } } }
    }
    if (url === '/api/enterprise/assistant/subscription/start') return { plan: 'pro', billing: 'unbilled', checkout_url: null, email: 'ada@example.com' }
    if (url === '/api/settings/mcp-external-access') {
      if (externalAccess?.fails) throw { response: { status: 500, data: { detail: 'unreadable' } } }
      return externalAccess
    }
    if (url === '/api/agents' && agentsList) return agentsList
    if (url === '/api/agents') return [{ name: 'trinity-system', status: 'running', is_system: true }, { name: 'issue-digest', status: 'running' }]
    if (url === '/api/agents/autonomy-status') return { 'issue-digest': { autonomy_enabled: false } }
    if (url === '/api/ops/schedules') return { schedules: [{ id: 's1', agent_name: 'issue-digest', name: 'Mondays', enabled: scheduleOn }] }
    if (url === '/api/agents/issue-digest/schedules/s1/enable') { scheduleOn = true; return {} }
    if (url === '/api/agents/issue-digest/schedules') return [{ id: 's1', enabled: scheduleOn }]
    return {}
  }
  const client = {
    get: vi.fn(async (url) => {
      if (url === '/api/agents' && agentsWait) await agentsWait
      return { data: respond('get', url) }
    }),
    post: vi.fn(async (url, body) => ({ data: respond('post', url, body) })),
    put: vi.fn(async (url, body) => ({ data: respond('put', url, body) })),
    delete: vi.fn(async (url) => ({ data: respond('delete', url) })),
  }
  return { default: client }
})
const pushed = []
vi.mock('vue-router', () => ({ useRoute: () => ({ path: '/', params: {}, query: {} }), useRouter: () => ({ push: async (to) => { pushed.push(to) } }) }))
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
    startFails = false
    refuseSubscribe = false
    jobs = []
    runs = []
    afterAgreeing = null
    checklist = {}
    statusReads = []
    byCode = false
    refusals = {}
    confirmEmail = 'a•••@example.com'
    codeVersion = undefined
    agentsList = null
    credentialAnswer = { provided: true, agent: null, name: 'ANTHROPIC_API_KEY', replaced: false, connected_agents: 2, working: true }
    agentsWait = null
    externalAccess = { enabled: false, source: 'default', advertised_url: 'https://trinity.example/mcp' }
    pushed.length = 0
    subTerms = { version: 's1', title: 'Subscription terms', url: '', text: '# Subscription terms\n\nYou pay monthly.' }
    status = { available: true, contacted: true, reachable: true, plan: 'free', email: null, allowance: null, trial_terms: { version: 't0', url: '', accepted: 't0' }, profile_known: true }
    localStorage.clear()
    sessionStorage.clear()
    setActivePinia(createPinia())
    wrapper = mount(AssistantWidget, {
      attachTo: document.body,
      global: { stubs: {
        ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' },
        HelpChatWidget: { props: ['embedded', 'askOnly'], template: '<div data-testid="ask-tab" :data-ask-only="String(askOnly)">ask</div>' },
      } },
    })
  })

  afterEach(() => wrapper.unmount())

  it('opens ready to use and has sent nothing', async () => {
    await openPanel()
    expect(wrapper.get('[data-testid="assistant-welcome"]').text()).toContain('get agents working for you')
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
      expect(box.text()).toContain('turns on anonymous usage sharing')
      expect(box.text()).toContain('turn it off in Settings')
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

    it('once they agree with nothing written yet, it says where to start', async () => {
      afterAgreeing = { key: 'first-agent', text: "You don't have an agent yet. Tell me one task you'd hand off." }
      await openPanel()
      await wrapper.get('[data-testid="assistant-agree"]').trigger('click')
      await flushPromises()
      expect(wrapper.findAll('.bubble').map((b) => b.text())).toEqual(["You don't have an agent yet. Tell me one task you'd hand off."])
      expect(sent(TURN)).toEqual([])
    })
  })

  // trinity-enterprise#1330: the first status after setup can arrive before the
  // instance's account report has landed. While the status says that report is
  // still pending, the terms box is not offered; the status is read again with
  // backoff and only then is the box decided.
  describe('while the account report is still pending', () => {
    const NOT_AGREED = { available: true, contacted: false, reachable: null, plan: null, email: null, allowance: null, trial_terms: { version: 't0', url: '', accepted: null } }
    const PENDING = { ...NOT_AGREED, bind: { state: 'pending' } }
    const AGREED = { ...NOT_AGREED, contacted: true, reachable: true, plan: 'free', trial_terms: { version: 't0', url: '', accepted: 't0' } }
    const box = () => wrapper.find('[data-testid="assistant-terms-first"]')
    const reads = () => sent('/api/enterprise/assistant/status').length
    const load = async () => {
      await flushPromises()
      calls.length = 0      // the panel's own read on mount is not one of these
      const { useAssistantStore } = await import('@/stores/assistant')
      await useAssistantStore().loadStatus()
      await openPanel()
    }
    const signedIn = async (on) => {
      const { useAuthStore } = await import('@/stores/auth')
      useAuthStore().isAuthenticated = on
    }

    beforeEach(async () => {
      vi.useFakeTimers({ toFake: ['setTimeout'] })
      await signedIn(true)
    })
    afterEach(() => { vi.useRealTimers() })

    it('pending, then accepted: the terms box is never shown and nothing is accepted', async () => {
      statusReads = [{ ...PENDING }, { ...AGREED }]
      status = { ...AGREED }
      await load()
      expect(box().exists()).toBe(false)
      expect(wrapper.find('[data-testid="assistant-finishing"]').exists()).toBe(true)
      for (const ms of [500, 1000, 2000, 4000]) {
        await vi.advanceTimersByTimeAsync(ms)
        await flushPromises()
        expect(box().exists()).toBe(false)
      }
      expect(reads()).toBe(2)
      expect(wrapper.find('[data-testid="assistant-finishing"]').exists()).toBe(false)
      expect(sent('/api/enterprise/assistant/terms/accept')).toEqual([])
    })

    it('pending, then not accepted: the box appears after the re-read', async () => {
      statusReads = [{ ...PENDING }, { ...NOT_AGREED }]
      status = { ...NOT_AGREED }
      await load()
      expect(box().exists()).toBe(false)
      await vi.advanceTimersByTimeAsync(500)
      await flushPromises()
      expect(reads()).toBe(2)
      expect(box().exists()).toBe(true)
      expect(sent('/api/enterprise/assistant/terms/accept')).toEqual([])
    })

    it('still pending after every re-read: it gives up and offers the box as before', async () => {
      statusReads = [{ ...PENDING }, { ...PENDING }, { ...PENDING }, { ...PENDING }, { ...PENDING }]
      status = { ...PENDING }
      await load()
      await vi.advanceTimersByTimeAsync(500 + 1000 + 2000)
      await flushPromises()
      expect(box().exists()).toBe(false)
      await vi.advanceTimersByTimeAsync(4000)
      await flushPromises()
      expect(reads()).toBe(5)
      expect(box().exists()).toBe(true)
      await vi.advanceTimersByTimeAsync(60000)
      await flushPromises()
      expect(reads()).toBe(5)
    })

    it('a message sent while it is being read again is held, never sent; no starter is offered until the opening is decided', async () => {
      statusReads = [{ ...PENDING }, { ...PENDING }]
      status = { ...NOT_AGREED }
      await load()
      // trinity-enterprise#1356: what the empty panel offers waits with the opening, so it cannot change under the person
      expect(wrapper.find('[data-testid="assistant-starters"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="assistant-opening-pending"]').attributes('aria-busy')).toBe('true')
      await say('how are my agents?')
      expect(sent(TURN)).toEqual([])
    })

    it('pending, then accepted: a message held meanwhile goes once the read settles', async () => {
      statusReads = [{ ...PENDING }, { ...AGREED }]
      status = { ...AGREED }
      await load()
      await say('how are my agents?')
      expect(sent(TURN)).toEqual([])
      await vi.advanceTimersByTimeAsync(500)
      await flushPromises()
      expect(sent(TURN).map((c) => c.body.message)).toEqual(['how are my agents?'])
      expect(sent('/api/enterprise/assistant/terms/accept')).toEqual([])
    })

    it('pending, then not accepted: the box shows with the message still held', async () => {
      statusReads = [{ ...PENDING }, { ...NOT_AGREED }]
      status = { ...NOT_AGREED }
      await load()
      await say('how are my agents?')
      await vi.advanceTimersByTimeAsync(500)
      await flushPromises()
      expect(box().exists()).toBe(true)
      expect(wrapper.get('[data-testid="assistant-waiting"]').text()).toContain('how are my agents?')
      expect(sent(TURN)).toEqual([])
      expect(sent('/api/enterprise/assistant/terms/accept')).toEqual([])
    })

    it('stops reading again once the person signs out', async () => {
      statusReads = [{ ...PENDING }, { ...PENDING }, { ...PENDING }, { ...PENDING }, { ...PENDING }]
      status = { ...PENDING }
      await load()
      await vi.advanceTimersByTimeAsync(500)
      await flushPromises()
      expect(reads()).toBe(2)
      await signedIn(false)
      await vi.advanceTimersByTimeAsync(60000)
      await flushPromises()
      expect(reads()).toBe(2)
      const { useAssistantStore } = await import('@/stores/assistant')
      expect(useAssistantStore().settling).toBe(false)
    })

    it('stops reading again once the panel is torn down', async () => {
      statusReads = [{ ...PENDING }, { ...PENDING }, { ...PENDING }, { ...PENDING }, { ...PENDING }]
      status = { ...PENDING }
      await load()
      await vi.advanceTimersByTimeAsync(500)
      await flushPromises()
      expect(reads()).toBe(2)
      wrapper.unmount()
      wrapper = { unmount() {} }
      await vi.advanceTimersByTimeAsync(60000)
      await flushPromises()
      expect(reads()).toBe(2)
    })

    it('no pending marker: the box is offered at once and the status is read once', async () => {
      status = { ...NOT_AGREED }
      await load()
      expect(box().exists()).toBe(true)
      await vi.advanceTimersByTimeAsync(10000)
      await flushPromises()
      expect(reads()).toBe(1)
    })
  })

  describe('docked on the left', () => {
    const STUBS = { ChatBubble: true, HelpChatWidget: true }
    const remount = async (matches) => {
      wrapper.unmount()
      window.matchMedia = (query) => ({ matches, media: query, addEventListener() {}, removeEventListener() {} })
      setActivePinia(createPinia())
      wrapper = mount(AssistantWidget, { attachTo: document.body, global: { stubs: STUBS } })
      await flushPromises()
    }
    afterEach(() => { delete window.matchMedia })

    it('is a rail with its button when collapsed, and says which page it is looking at when open', async () => {
      expect(wrapper.find('[data-testid="assistant-rail"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(false)
      await wrapper.get('[data-testid="assistant-expand"]').trigger('click')
      await flushPromises()
      expect(wrapper.get('[data-testid="assistant-panel"]').attributes('role')).toBe('complementary')
      expect(wrapper.find('[data-testid="assistant-rail"]').exists()).toBe(false)
      expect(wrapper.get('[data-testid="assistant-page"]').text()).toBe('Looking at: Dashboard')
      const { useAssistantStore } = await import('@/stores/assistant')
      expect(useAssistantStore().panelOpen).toBe(true)          // App.vue pads the page by this
    })

    it('starts open beside the page where there is room and no choice was made, without taking the keyboard', async () => {
      await remount(true)
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(true)
      expect(document.activeElement?.getAttribute('data-testid')).not.toBe('assistant-input')
      expect(localStorage.getItem('trinity-assistant-open')).toBeNull()        // the page opened it: no choice recorded
    })

    it('keeps the person\'s choice: collapsed stays collapsed, and a small window never opens it over the page', async () => {
      await remount(true)
      await wrapper.get('[aria-label="Close the assistant"]').trigger('click')
      expect(localStorage.getItem('trinity-assistant-open')).toBe('false')
      await remount(true)
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(false)
      await openPanel()
      expect(localStorage.getItem('trinity-assistant-open')).toBe('true')
      await remount(false)                                                     // remembered open, but no room
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="assistant-launcher"]').exists()).toBe(true)
    })

    it('keeps its button in the top-left corner as the logo, open or collapsed, with "Trinity" beside it', async () => {
      expect(wrapper.get('[data-testid="assistant-wordmark"]').text()).toBe('Trinity')
      await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(true)
      const orb = wrapper.get('[data-testid="assistant-launcher"]')            // still there: the same button collapses it
      expect(orb.attributes('aria-expanded')).toBe('true')
      expect(wrapper.get('[data-testid="assistant-panel"] header').text()).toContain('Trinity')
      await orb.trigger('click')
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(false)
      expect(localStorage.getItem('trinity-assistant-open')).toBe('false')
    })

    it('can be made wider or narrower at its edge, within bounds, and keeps the width', async () => {
      await openPanel()
      const edge = wrapper.get('[data-testid="assistant-resize"]')
      expect(edge.attributes('role')).toBe('separator')
      await edge.trigger('keydown', { key: 'ArrowRight' })
      expect(localStorage.getItem('trinity-assistant-width')).toBe('408')
      expect(document.documentElement.style.getPropertyValue('--assistant-w')).toBe('408px')
      for (let i = 0; i < 20; i++) await edge.trigger('keydown', { key: 'ArrowLeft' })
      expect(localStorage.getItem('trinity-assistant-width')).toBe('320')     // never narrower than this
      await edge.trigger('dblclick')
      expect(localStorage.getItem('trinity-assistant-width')).toBe('384')
    })

    it('Option/Alt+Z opens and collapses it from anywhere, the message box included; Enter sends and says so', async () => {
      const press = async (opts = {}, target = document.body) => {
        target.dispatchEvent(new KeyboardEvent('keydown', { key: 'Ω', code: 'KeyZ', altKey: true, bubbles: true, cancelable: true, ...opts }))
        await flushPromises()
      }
      await press({ altKey: false, key: 'z' })                                  // a bare Z is just a letter
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(false)
      await press()
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="assistant-panel"] button[type="submit"]').exists()).toBe(false)
      expect(wrapper.get('[data-testid="assistant-keys"]').text()).toMatch(/^Enter to send · (⌥Z|Alt\+Z) to hide or show$/)
      await press({ metaKey: true })                                           // not with Cmd as well
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(true)
      await press({}, wrapper.get('[data-testid="assistant-input"]').element)   // from inside the box too
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(false)
    })

    it('stays out of the way while setup has the screen, then takes over beside the page', async () => {
      await remount(true)
      const { useFirstRunStore } = await import('@/stores/firstRun')
      const firstRun = useFirstRunStore()
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(true)
      firstRun.overlayOpen = true
      await flushPromises()
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="assistant-peek"]').exists()).toBe(false)
      expect(localStorage.getItem('trinity-assistant-open')).toBeNull()       // not the person's choice
      firstRun.overlayOpen = false
      await flushPromises()
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(true)
    })

    it('Esc collapses it only when it lies over the page', async () => {
      await remount(true)
      await wrapper.get('[data-testid="assistant-panel"]').trigger('keydown', { key: 'Escape' })
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(true)
      await remount(false)
      await openPanel()
      await wrapper.get('[data-testid="assistant-panel"]').trigger('keydown', { key: 'Escape' })
      expect(wrapper.find('[data-testid="assistant-panel"]').exists()).toBe(false)
    })
  })

  describe('speaking first', () => {
    it('says hello beside the launcher on a first visit, and can be waved away', async () => {
      expect(wrapper.get('[data-testid="assistant-peek"]').text()).toContain("I'm your Trinity assistant")
      await wrapper.get('[data-testid="assistant-peek-close"]').trigger('click')
      expect(wrapper.find('[data-testid="assistant-peek"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="assistant-launcher"]').exists()).toBe(true)
      expect(calls.filter((c) => c.method !== 'get')).toEqual([])
    })

    it('says hello again in a new session while nobody here has started, even in a browser that opened it before', async () => {
      wrapper.unmount()
      localStorage.setItem('trinity-assistant-seen', '1')
      status = { available: true, contacted: false, reachable: null, plan: null, trial_terms: { version: null, url: '', accepted: null } }
      setActivePinia(createPinia())
      localStorage.setItem('trinity-assistant-conversation', JSON.stringify({ at: Date.now(), conversationId: 'old',
        messages: [{ id: 'm1', role: 'user', content: 'from before the reset' }] }))
      wrapper = mount(AssistantWidget, { attachTo: document.body, global: { stubs: { ChatBubble: true, HelpChatWidget: true } } })
      await flushPromises()
      const { useAssistantStore: before } = await import('@/stores/assistant')
      expect(before().messages).toEqual([])                       // a conversation kept from before is not shown
      expect(localStorage.getItem('trinity-assistant-conversation')).toBeNull()
      expect(wrapper.find('[data-testid="assistant-peek"]').exists()).toBe(true)
      expect(wrapper.get('[data-testid="assistant-launcher-unread"]').text()).toBe('1')
      await wrapper.get('[data-testid="assistant-peek-close"]').trigger('click')
      const { useAssistantStore } = await import('@/stores/assistant')
      await useAssistantStore().loadStatus()
      expect(wrapper.find('[data-testid="assistant-peek"]').exists()).toBe(false)   // once per session
    })

    it('drops a conversation kept under an earlier Trinity record, whichever of the status and the page arrives first', async () => {
      const kept = (since) => localStorage.setItem('trinity-assistant-conversation', JSON.stringify({ at: Date.now(), conversationId: 'old', since,
        messages: [{ id: 'm1', role: 'user', content: 'from before the reset' }] }))
      const { useAssistantStore } = await import('@/stores/assistant')
      const remount = async (statusFirst) => {
        wrapper.unmount()
        setActivePinia(createPinia())
        if (statusFirst) await useAssistantStore().loadStatus()      // as App.vue does, before the panel exists
        wrapper = mount(AssistantWidget, { attachTo: document.body, global: { stubs: { ChatBubble: true, HelpChatWidget: true } } })
        await flushPromises()
        return useAssistantStore().messages.length
      }
      status = { ...status, since: '2026-11-19T13:06:26+00:00' }
      for (const statusFirst of [true, false]) {
        kept('2026-11-18T09:00:00+00:00')                            // the instance was forgotten and came back
        expect(await remount(statusFirst)).toBe(0)
        expect(localStorage.getItem('trinity-assistant-conversation')).toBeNull()
        kept('2026-11-19T13:06:26+00:00')                            // the same record: it is theirs, keep it
        expect(await remount(statusFirst)).toBe(1)
        status = { ...status, contacted: false, since: undefined }   // Trinity has never heard of this instance
        expect(await remount(statusFirst)).toBe(0)
        status = { ...status, contacted: true, since: '2026-11-19T13:06:26+00:00' }
        localStorage.clear()
      }
      // Trinity out of reach says nothing about what it remembers: nothing is dropped on that.
      kept('2026-11-18T09:00:00+00:00')
      status = { ...status, reachable: false, since: undefined }
      expect(await remount(true)).toBe(1)
    })

    it('tells a new person the agents already there are examples, and aims at one of their own', async () => {
      status = { available: true, contacted: false, reachable: null, plan: null, trial_terms: { version: null, url: '', accepted: null } }
      const { useAssistantStore } = await import('@/stores/assistant')
      const { useFirstRunStore } = await import('@/stores/firstRun')
      useFirstRunStore().seededAgents = ['issue-digest']                // the one agent here came with the install
      await useAssistantStore().loadStatus()
      await wrapper.get('[data-testid="assistant-peek-open"]').trigger('click')
      await flushPromises()
      const said = wrapper.findAll('.bubble').map((b) => b.text())
      expect(said[0]).toContain('an agent of your own')
      expect(said[0]).toContain('came with Trinity as an example')
      expect(said[0]).not.toContain('You have 1 agent')
    })

    it('starts the conversation itself with someone it does not know, before anything is agreed or sent', async () => {
      status = { available: true, contacted: false, reachable: null, plan: null, trial_terms: { version: null, url: '', accepted: null } }
      const { useAssistantStore } = await import('@/stores/assistant')
      await useAssistantStore().loadStatus()
      await wrapper.get('[data-testid="assistant-peek-open"]').trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-testid="assistant-peek"]').exists()).toBe(false)
      const said = wrapper.findAll('.bubble').map((b) => b.text())
      expect(said.length).toBe(1)
      expect(said[0]).toContain('You have 1 agent here')          // the system agent is not counted
      expect(wrapper.find('[data-testid="assistant-welcome"]').exists()).toBe(false)
      const offered = wrapper.findAll('[data-testid="assistant-starters"]')
      expect(offered.length).toBe(1)                               // one question, not a menu
      expect(offered[0].text()).toContain('what describes you best')
      expect(calls.filter((c) => c.method !== 'get')).toEqual([])
      expect(localStorage.getItem('trinity-assistant-conversation')).toBeNull()   // a hello is not a conversation to keep
      // Their click waits for the terms; once they agree it goes, and the assistant is told what it opened with.
      await button('Founder or owner').trigger('click')
      await wrapper.get('[data-testid="assistant-agree"]').trigger('click')
      await flushPromises()
      expect(sent(TURN)[0].body.message).toBe("I'm a founder or business owner.")
      expect(sent(TURN)[0].body.context.said).toContain("I'm Trinity's assistant")
    })

    it('does not open with a hello for someone it already knows, and its own next step replaces the hello', async () => {
      await openPanel()
      expect(wrapper.findAll('.bubble').length).toBe(0)
      expect(wrapper.find('[data-testid="assistant-welcome"]').exists()).toBe(true)
      const { useAssistantStore } = await import('@/stores/assistant')
      const store = useAssistantStore()
      store.status = { ...store.status, profile_known: false }
      store.messages = []
      store.welcome('Hi.')
      store.greet({ key: 'first-run', text: "Your agent hasn't run yet." })
      await flushPromises()
      expect(wrapper.findAll('.bubble').map((b) => b.text())).toEqual(["Your agent hasn't run yet."])
    })

    it('carries the getting-started steps: one quiet line, the list on a click, and the next step done with the assistant', async () => {
      checklist = { completed_count: 2, total_count: 4, complete: false, dismissed: false, items: [
        { key: 'first_agent_created', title: 'Create your first agent', description: 'a', done: true, action_label: 'x', action_route: '/' },
        { key: 'first_chat', title: 'Talk to it', description: 'b', done: true, action_label: 'x', action_route: '/' },
        { key: 'first_schedule_created', title: 'Put it on a schedule', description: 'A schedule makes it run without you.', done: false, action_label: 'x', action_route: '/' },
        { key: 'first_channel_connected', title: 'Reach it where you work', description: 'd', done: false, action_label: 'x', action_route: '/' },
      ] }
      await openPanel()
      const line = wrapper.get('[data-testid="assistant-setup-toggle"]')
      expect(line.attributes('aria-label')).toBe('Getting started: 2 of 4 done. Next: Put it on a schedule')
      expect(wrapper.find('[data-testid="assistant-setup-help"]').exists()).toBe(false)     // closed to begin with
      await line.trigger('click')
      const items = wrapper.findAll('[data-testid^="assistant-setup-item-"]')
      expect(items.map((i) => i.attributes('data-done'))).toEqual(['true', 'true', 'false', 'false'])
      expect(wrapper.findAll('[data-testid="assistant-setup-help"]').length).toBe(1)        // only the next step offers a hand
      expect(items[2].text()).toContain('A schedule makes it run without you.')
      turns = [final('Which agent, and when should it run?')]
      checklist = { ...checklist, completed_count: 3, items: checklist.items.map((i, n) => ({ ...i, done: n < 3 })) }
      await wrapper.get('[data-testid="assistant-setup-help"]').trigger('click')
      await flushPromises()
      const turn = sent(TURN)[0].body
      expect(turn.message).toBe('Help me with this getting-started step: "Put it on a schedule".')
      expect(turn.context.setup).toEqual([
        { title: 'Create your first agent', done: true }, { title: 'Talk to it', done: true },
        { title: 'Put it on a schedule', done: false }, { title: 'Reach it where you work', done: false }])
      // The turn ended: the steps are read again, and the tick the assistant earned shows.
      expect(wrapper.get('[data-testid="assistant-setup-toggle"]').attributes('aria-label')).toBe('Getting started: 3 of 4 done. Next: Reach it where you work')
    })

    it('shows no steps when they are all done, dismissed, or not offered on this build', async () => {
      await openPanel()                                   // the endpoint answers nothing here
      expect(wrapper.find('[data-testid="assistant-setup"]').exists()).toBe(false)
      await say('hello')
      expect(sent(TURN)[0].body.context.setup).toBeUndefined()
    })


    it('lights the launcher on a first visit, and not again once it has been opened', async () => {
      expect(wrapper.get('[data-testid="assistant-launcher-unread"]').text()).toBe('1')
      expect(wrapper.get('[data-testid="assistant-launcher-halo"]').classes()).toContain('eager')
      await openPanel()
      await wrapper.get('[aria-label="Close the assistant"]').trigger('click')
      expect(wrapper.find('[data-testid="assistant-launcher-unread"]').exists()).toBe(false)
      expect(calls.filter((c) => c.method !== 'get')).toEqual([])
    })

    it('shows the next step Trinity gives as a message of its own, with what to do still offered, and tells the assistant with the reply', async () => {
      localStorage.setItem('trinity-assistant-seen', '1')
      status = { ...status, nudge: { key: 'first-run', text: "Your agent hasn't run yet. Want me to give it a first run?" } }
      const { useAssistantStore } = await import('@/stores/assistant')
      const store = useAssistantStore()
      store.unread = 0
      await store.loadStatus()
      await flushPromises()
      expect(wrapper.get('[data-testid="assistant-launcher-unread"]').text()).toBe('1')
      status = { ...status, nudge: null }
      expect(sent('/api/enterprise/assistant/opened')).toEqual([])          // unread: nothing to tell yet
      await openPanel()
      expect(sent('/api/enterprise/assistant/opened').map((c) => c.body)).toEqual([{ key: 'first-run' }])
      await wrapper.get('[aria-label="Close the assistant"]').trigger('click')
      await openPanel()
      expect(sent('/api/enterprise/assistant/opened').length).toBe(1)        // told once
      expect(wrapper.findAll('.bubble').map((b) => b.text())).toEqual(["Your agent hasn't run yet. Want me to give it a first run?"])
      expect(wrapper.findAll('[data-testid="assistant-starters"]').length).toBeGreaterThan(0)
      await say('yes please')
      expect(sent(TURN)[0].body.context.said).toBe("Your agent hasn't run yet. Want me to give it a first run?")
      expect(wrapper.findAll('[data-testid="assistant-starters"]').length).toBe(0)
      await say('and then?')
      expect(sent(TURN)[1].body.context.said).toBeUndefined()
    })

    it('asks who they are with one click until that is known', async () => {
      status = { ...status, profile_known: false }
      const { useAssistantStore } = await import('@/stores/assistant')
      await useAssistantStore().loadStatus()
      await openPanel()
      expect(wrapper.findAll('[data-testid="assistant-starters"]')[0].find('p').text()).toBe('First, what describes you best?')
      await button('Operations').trigger('click')
      await flushPromises()
      expect(sent(TURN)[0].body.message).toBe('I work in operations.')
    })
  })

  describe('questions answered with a click', () => {
    const choice = (question, options) => ({ action: 'choice', question, options })

    it('one question: the click is the reply, and it is not counted', async () => {
      turns = [final('One thing first.', [choice('What starts it?', ['A time', 'Someone asking'])]), final('Good.')]
      await openPanel()
      await say('build me an agent')
      expect(wrapper.get('[data-testid="assistant-choice"]').text()).toContain('What starts it?')
      await button('Someone asking').trigger('click')
      await flushPromises()
      expect(sent(TURN)[1].body).toMatchObject({ message: 'Someone asking', counted: false })
      expect(wrapper.findAll('.bubble[data-role="user"]').at(-1).text()).toBe('Someone asking')
      expect(button('A time').attributes('disabled')).toBeDefined()
    })

    it('several questions: the reply goes once each has an answer, as one message', async () => {
      turns = [final('', [choice('What starts it?', ['A time', 'Someone asking']), choice('How often?', ['Daily', 'Weekly'])]), final('Good.')]
      await openPanel()
      await say('build me an agent')
      await button('A time').trigger('click')
      await flushPromises()
      expect(sent(TURN).length).toBe(1)
      await button('Weekly').trigger('click')
      await flushPromises()
      expect(sent(TURN)[1].body.message).toBe('What starts it? A time\nHow often? Weekly')
    })

    it('writing instead closes the question, and an answer already clicked goes along', async () => {
      turns = [final('', [choice('What starts it?', ['A time', 'Someone asking']), choice('How often?', ['Daily', 'Weekly'])]), final('Good.')]
      await openPanel()
      await say('build me an agent')
      await button('A time').trigger('click')
      await say('every second Tuesday')
      expect(sent(TURN)[1].body).toMatchObject({ message: 'What starts it? A time\nevery second Tuesday', counted: true })
      expect(button('Daily').attributes('disabled')).toBeDefined()
    })

    it('drops a question with fewer than two answers', async () => {
      turns = [final('Hm.', [choice('Sure?', ['Yes'])])]
      await openPanel()
      await say('x')
      expect(wrapper.find('[data-testid="assistant-choice"]').exists()).toBe(false)
    })
  })

  it('sends feedback to the Trinity team only when the person presses Send, exactly as shown', async () => {
    const FEEDBACK = '/api/enterprise/assistant/feedback'
    turns = [final('Here is what I would send.', [{ action: 'feedback', kind: 'bug', summary: 'Schedules page is blank', details: 'Opened it and saw nothing.' }]), final('Sent. Thank you.')]
    await openPanel()
    await say('the schedules page is broken')
    const card = wrapper.get('[data-testid="assistant-card"]')
    expect(card.text()).toContain('For the Trinity team')
    expect(card.text()).toContain('Send bug report: Schedules page is blank')
    expect(wrapper.get('[data-testid="assistant-feedback"]').text()).toBe('Opened it and saw nothing.')
    expect(sent(FEEDBACK)).toEqual([])
    await button('Send').trigger('click')
    await flushPromises()
    expect(sent(FEEDBACK)[0].body).toEqual({ kind: 'bug', summary: 'Schedules page is blank', details: 'Opened it and saw nothing.', page: 'Dashboard' })
    expect(sent(TURN)[1].body).toMatchObject({ message: 'Done: Send bug report: Schedules page is blank.', counted: false })
  })

  it('keeps Ask one tab away', async () => {
    await openPanel()
    expect(wrapper.get('[data-testid="ask-tab"]').isVisible()).toBe(false)
    await wrapper.findAll('[role="radio"]').find((b) => b.text() === 'Ask the docs').trigger('click')
    expect(wrapper.get('[data-testid="ask-tab"]').isVisible()).toBe(true)
    expect(wrapper.get('[data-testid="ask-tab"]').attributes('data-ask-only')).not.toBe('undefined')   // one place to ask, no tabs of its own
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

  it('says when more messages come back once few are left, before the limit is reached', async () => {
    const low = { ...ALLOWANCE, messages_left: 3, resets_at: new Date(Date.now() + 5 * 3600000).toISOString() }
    turns = [final('It is running.', [], low)]
    await openPanel()
    await say('is it running?')
    expect(wrapper.get('[data-testid="assistant-plan"]').text()).toBe('Free · 3 messages left · more in about 5 hours')
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
    expect(wrapper.get('[data-testid="assistant-auto-create"]').element.checked).toBe(false)   // waits for Build by default (ent#1343)
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
    expect(groups.map((g) => g.find('p').text())).toEqual(['Check', 'Do', 'Build', 'Tell the Trinity team'])
    await button('Ask an agent…').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="assistant-input"]').element.value).toBe('Ask issue-digest ')
    expect(sent(TURN)).toEqual([])
    await button('What runs on a schedule?').trigger('click')
    await flushPromises()
    expect(sent(TURN)[0].body.message).toBe('What runs on a schedule?')
  })

  it('a proposed build waits for the click on Build: create-when-ready is not ticked (ent#1343)', async () => {
    vi.useFakeTimers()
    const design = { title: 'Review summary', summary: 'Summarises reviews.', to_do_after: [],
      agents: [{ name: 'velofix-reviews', job: 'Summarise the reviews.', credentials: [], schedule: null, calls: [] }] }
    jobs = [{ status: 'done', steps_done: 3, steps: 3, card: { action: 'build', design_id: DESIGN_ID, design } }]
    turns = [final('It is being made.', [{ action: 'job', job_id: DESIGN_ID, title: 'Review summary' }]), final('It is ready.'), final('velofix-reviews exists now.')]
    await openPanel()
    await say('make me a review summariser')
    expect(wrapper.get('[data-testid="assistant-auto-create"]').element.checked).toBe(false)
    await vi.advanceTimersByTimeAsync(4100)
    await flushPromises()
    expect(sent(`/api/enterprise/assistant/designs/${DESIGN_ID}/build`)).toHaveLength(0)    // nothing built without the click
    expect(sent(TURN)[1].body.message).toContain('waits for their click')
    await button('Build').trigger('click')
    await flushPromises()
    expect(sent(`/api/enterprise/assistant/designs/${DESIGN_ID}/build`)).toHaveLength(1)
    vi.useRealTimers()
  })

  it('creates the agents as soon as a build is ready when the person ticks create-when-ready', async () => {
    vi.useFakeTimers()
    const design = { title: 'Review summary', summary: 'Summarises reviews.', to_do_after: [],
      agents: [{ name: 'velofix-reviews', job: 'Summarise the reviews.', credentials: [], schedule: null, calls: [] }] }
    jobs = [{ status: 'done', steps_done: 3, steps: 3, card: { action: 'build', design_id: DESIGN_ID, design } }]
    turns = [final('It is being made.', [{ action: 'job', job_id: DESIGN_ID, title: 'Review summary' }]), final('shop-desk exists now.')]
    await openPanel()
    await say('make me a review summariser')
    await wrapper.get('[data-testid="assistant-auto-create"]').setValue(true)
    await vi.advanceTimersByTimeAsync(4100)
    await flushPromises()
    expect(sent(`/api/enterprise/assistant/designs/${DESIGN_ID}/build`)).toHaveLength(1)
    expect(sent(TURN)[1].body.message).toContain('Done: Build "Review summary". Built shop-desk')
    vi.useRealTimers()
  })

  it('takes a plain yes as pressing the one Build button that is waiting, and anything longer as a message', async () => {
    const design = { title: 'Shop desk', summary: 'Answers the shop inbox.', to_do_after: [],
      agents: [{ name: 'shop-desk', job: 'Answer the inbox.', credentials: [], schedule: null, calls: [] }] }
    turns = [final('Here is the design.', [{ action: 'build', design_id: DESIGN_ID, design }]), final('Renamed.'), final('It exists.')]
    await openPanel()
    await say('a desk for my shop')
    await say('yes but call it front-desk')
    expect(sent(`/api/enterprise/assistant/designs/${DESIGN_ID}/build`)).toHaveLength(0)
    expect(sent(TURN)[1].body.message).toBe('yes but call it front-desk')
    await say('ok build it')
    expect(sent(`/api/enterprise/assistant/designs/${DESIGN_ID}/build`)).toHaveLength(1)
    expect(sent(TURN)[2].body).toMatchObject({ counted: false })
  })

  it('after a reload, the conversation is back and a run being followed is still followed', async () => {
    vi.useFakeTimers()
    turns = [final("It's running.", [{ action: 'run', agent: 'issue-digest', execution_id: 'exec-12345', title: 'Weekly digest' }]), final('Three new issues.')]
    await openPanel()
    await say('run the digest now')
    await flushPromises()
    wrapper.unmount()                                    // the page goes away mid-run
    const { useAssistantStore } = await import('@/stores/assistant')
    useAssistantStore().messages = []                    // the old page's script dies with it

    setActivePinia(createPinia())
    runs = [{ status: 'success', ended: true, ok: true, error: '' }]
    wrapper = mount(AssistantWidget, { attachTo: document.body, global: { stubs: {
      ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' },
      HelpChatWidget: { template: '<div />' } } } })
    await openPanel()
    expect(wrapper.findAll('.bubble').map((b) => b.text())).toEqual(['run the digest now', "It's running."])
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(true)
    await vi.advanceTimersByTimeAsync(1600)
    await flushPromises()
    expect(sent(TURN).at(-1).body).toMatchObject({ conversation_id: 'c1', counted: false })
    expect(wrapper.findAll('.bubble').map((b) => b.text()).at(-1)).toBe('Three new issues.')
    vi.useRealTimers()
  })

  it('a new conversation forgets the kept one', async () => {
    await openPanel()
    await say('hello')
    expect(localStorage.getItem('trinity-assistant-conversation')).toContain('hello')
    await wrapper.get('[aria-label="New conversation"]').trigger('click')
    expect(localStorage.getItem('trinity-assistant-conversation')).toBeNull()
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

    // the tick says what it turns on, and the terms are one click away
    expect(wrapper.get('[data-testid="assistant-account"]').text()).toContain('turn on anonymous usage sharing')
    await wrapper.get('[data-testid="assistant-full-terms-link"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="assistant-full-terms-text"]').text()).toContain('Usage sharing is turned on.')
    expect(sent('/api/enterprise/assistant/account/verify')).toHaveLength(0)

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

  describe('subscribing with an account already known', () => {
    const NOTICE = 'The terms have changed since you last accepted them.'
    const START = '/api/enterprise/assistant/account/start'
    const SUBSCRIBE = '/api/enterprise/assistant/subscription/start'
    const offerUpgrade = async () => {
      turns = [final('Subscribe?', [{ action: 'upgrade' }])]
      await openPanel()
      await say('I want more')
      await button('Continue').trigger('click')
      await flushPromises()
    }
    const account = () => wrapper.get('[data-testid="assistant-account"]')
    const known = async (fields) => {
      status = { ...status, ...fields }
      const { useAssistantStore } = await import('@/stores/assistant')
      await useAssistantStore().loadStatus()
    }

    it('asks a known account whose terms are out of date to accept the current ones before subscribing', async () => {
      await known({ email: 'ada@example.com', terms_accepted: 't0', terms: { version: 't1', url: '' } })
      turns = [{ status: 'limit', conversation_id: 'c1', text: '', cards: [],
        allowance: { ...ALLOWANCE, messages_left: 0, reached: 'messages', resets_at: null } }]
      await openPanel()
      await say('one more thing')
      await wrapper.get('[data-testid="assistant-upgrade"]').trigger('click')
      await flushPromises()

      expect(sent(START).map((c) => c.body)).toEqual([{ email: 'ada@example.com' }])
      expect(account().text()).toContain(NOTICE)
      expect(account().text()).toContain('version t1')
      expect(wrapper.find('[data-testid="assistant-terms"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="assistant-subscribe"]').exists()).toBe(false)

      await wrapper.get('[data-testid="assistant-code"]').setValue('424242')
      await account().find('form').trigger('submit')
      await flushPromises()
      expect(account().text()).toContain('Accept the terms to continue.')
      expect(sent('/api/enterprise/assistant/account/verify')).toHaveLength(0)

      await wrapper.get('[data-testid="assistant-terms"]').setValue(true)
      await account().find('form').trigger('submit')
      await flushPromises()
      expect(sent('/api/enterprise/assistant/account/verify')[0].body).toMatchObject({ email: 'ada@example.com', accept_terms: true, terms_version: 't1' })

      await wrapper.get('[data-testid="assistant-subscribe"]').trigger('click')
      await flushPromises()
      expect(sent(SUBSCRIBE)).toHaveLength(1)
    })

    it('goes straight to subscribing when the accepted terms are the current ones', async () => {
      await known({ email: 'ada@example.com', terms_accepted: 't1', terms: { version: 't1', url: '' } })
      await offerUpgrade()
      expect(wrapper.find('[data-testid="assistant-subscribe"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="assistant-terms"]').exists()).toBe(false)
      expect(account().text()).not.toContain(NOTICE)
      expect(sent(START)).toHaveLength(0)
    })

    it('asks for an email, and sends nothing, when no account is known', async () => {
      await known({ email: null, terms_accepted: null, terms: { version: 't1', url: '' } })
      await offerUpgrade()
      expect(wrapper.find('[data-testid="assistant-email"]').exists()).toBe(true)
      expect(sent(START)).toHaveLength(0)
    })

    it('turns a refusal over changed terms into the accept step, not a dead end', async () => {
      await known({ email: 'ada@example.com', terms_accepted: 't1', terms: { version: 't1', url: '' } })
      refuseSubscribe = true
      await offerUpgrade()
      await wrapper.get('[data-testid="assistant-subscribe"]').trigger('click')
      await flushPromises()

      expect(sent(SUBSCRIBE)).toHaveLength(1)
      expect(account().text()).not.toContain('Accept the current terms first.')
      expect(account().text()).toContain(NOTICE)
      expect(wrapper.find('[data-testid="assistant-terms"]').exists()).toBe(true)
      expect(sent(START).map((c) => c.body)).toEqual([{ email: 'ada@example.com' }])
    })

    it('keeps the address and says why when the code cannot be sent', async () => {
      await known({ email: 'ada@example.com', terms_accepted: 't0', terms: { version: 't1', url: '' } })
      startFails = true
      await offerUpgrade()

      expect(sent(START)).toHaveLength(1)
      expect(account().text()).toContain("The code couldn't be sent right now.")
      expect(wrapper.get('[data-testid="assistant-email"]').element.value).toBe('ada@example.com')
      expect(wrapper.find('[data-testid="assistant-subscribe"]').exists()).toBe(false)
    })
  })

  // trinity-enterprise#1331: on a module with the by-code routes the code goes to
  // the instance's registered admin. The panel shows the subscription terms and
  // the masked address, asks for no email, and takes the person's own tick.
  describe('subscribing by code', () => {
    const BYCODE = '/api/enterprise/assistant/subscription'
    const account = () => wrapper.get('[data-testid="assistant-account"]')
    const atLimit = async () => {
      turns = [{ status: 'limit', conversation_id: 'c1', text: '', cards: [],
        allowance: { ...ALLOWANCE, messages_left: 0, reached: 'messages', resets_at: null } }]
      await openPanel()
      await say('one more thing')
      await wrapper.get('[data-testid="assistant-upgrade"]').trigger('click')
      await flushPromises()
    }
    const tickAndSend = async () => {
      await wrapper.get('[data-testid="assistant-sub-terms-tick"]').setValue(true)
      await account().get('form').trigger('submit')
      await flushPromises()
    }
    const confirmWith = async (digits) => {
      await wrapper.get('[data-testid="assistant-sub-code"]').setValue(digits)
      await account().get('form').trigger('submit')
      await flushPromises()
    }
    const refuse = (path, status, code, extra = {}) => {
      refusals[`${BYCODE}${path}`] = [{ status, detail: { code, message: `the gateway's words for ${code}`, ...extra } }]
    }

    beforeEach(() => { byCode = true })

    it('shows the terms and the masked address, sends the code on the tick, and subscribes with it', async () => {
      status = { ...status, email: 'ada@example.com' }
      const { useAssistantStore } = await import('@/stores/assistant')
      await useAssistantStore().loadStatus()
      await atLimit()
      expect(sent(`${BYCODE}/terms`)).toHaveLength(1)
      expect(account().text()).toContain('Subscription terms')
      expect(wrapper.get('[data-testid="assistant-sub-terms-text"]').text()).toContain('You pay monthly.')
      expect(wrapper.get('[data-testid="assistant-sub-address"]').text()).toContain('a•••@example.com')
      expect(account().text()).not.toContain('ada@example.com')
      expect(account().find('input[type="email"]').exists()).toBe(false)

      // No tick, nothing sent
      await account().get('form').trigger('submit')
      await flushPromises()
      expect(sent(`${BYCODE}/code`)).toHaveLength(0)
      expect(account().text()).toContain('Tick the subscription terms to continue.')

      await tickAndSend()
      expect(sent(`${BYCODE}/code`)).toHaveLength(1)
      expect(sent(`${BYCODE}/code`)[0].body ?? {}).toEqual({})
      expect(wrapper.get('[data-testid="assistant-sub-sent"]').text()).toContain('a•••@example.com')
      expect(wrapper.get('[data-testid="assistant-sub-sent"]').text()).toContain('15 minutes')

      turns = [final('You are on Trinity Pro now.', [], { ...ALLOWANCE, plan: 'pro', messages_left: 600 })]
      await confirmWith(' 424242 ')
      expect(sent(`${BYCODE}/confirm`).map((c) => c.body)).toEqual([{ code: '424242', accept: true, version: 's1' }])
      expect(sent(`${BYCODE}/start`)).toHaveLength(0)
      expect(sent('/api/enterprise/assistant/account/start')).toHaveLength(0)
      expect(sent(TURN)[1].body.message).toBe('Done: Subscribe to Trinity Pro. Now answer what they asked just before, which the limit stopped: "one more thing"')
      expect(wrapper.find('[data-testid="assistant-account"]').exists()).toBe(false)
      expect(wrapper.get('[data-testid="assistant-plan"]').text()).toBe('Trinity Pro')
    })

    it('says the address is the admin one on record when none is known here', async () => {
      await atLimit()
      expect(wrapper.get('[data-testid="assistant-sub-address"]').text()).toContain("this instance's registered admin")
    })

    it('links the terms when they are published as a page', async () => {
      subTerms = { version: 's2', title: 'Pro terms', url: 'https://terms.example/sub', text: '' }
      await atLimit()
      const link = wrapper.get('[data-testid="assistant-sub-terms-link"]')
      expect(link.attributes('href')).toBe('https://terms.example/sub')
      expect(link.attributes('target')).toBe('_blank')
    })

    it('sends a new code on request', async () => {
      await atLimit()
      await tickAndSend()
      await wrapper.get('[data-testid="assistant-sub-resend"]').trigger('click')
      await flushPromises()
      expect(sent(`${BYCODE}/code/resend`)).toHaveLength(1)
      expect(account().text()).toContain('A new code is on its way')
      expect(wrapper.find('[data-testid="assistant-sub-code"]').exists()).toBe(true)
    })

    it.each([
      ['wrong_code', 400, "That code isn't right."],
      ['code_expired', 400, 'That code has expired.'],
      ['too_many_attempts', 429, 'Too many tries with that code.'],
      ['code_required', 400, 'Enter the code from the email.'],
    ])('a %s refusal is a plain sentence and the code step stays', async (code, http, line) => {
      await atLimit()
      await tickAndSend()
      refuse('/confirm', http, code)
      await confirmWith('111111')
      expect(account().text()).toContain(line)
      expect(account().text()).not.toContain("the gateway's words")
      expect(wrapper.find('[data-testid="assistant-sub-code"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="assistant-sub-resend"]').exists()).toBe(true)
    })

    it('reloads the terms and asks for a new tick when they changed', async () => {
      await atLimit()
      await tickAndSend()
      subTerms = { ...subTerms, version: 's9', text: '# Subscription terms\n\nNew wording.' }
      refuse('/confirm', 409, 'terms_changed')
      await confirmWith('424242')
      expect(sent(`${BYCODE}/terms`)).toHaveLength(2)
      expect(account().text()).toContain('The subscription terms changed')
      expect(wrapper.get('[data-testid="assistant-sub-terms-text"]').text()).toContain('New wording.')
      expect(wrapper.get('[data-testid="assistant-sub-terms-tick"]').element.checked).toBe(false)
      await tickAndSend()
      await confirmWith('424242')
      expect(sent(`${BYCODE}/confirm`).at(-1).body).toEqual({ code: '424242', accept: true, version: 's9' })
    })

    it.each([
      ['a masked', 'a•••@example.com'],
      ['a null', null],
    ])('keeps the address it holds when the confirm answers %s email', async (_, answered) => {
      confirmEmail = answered
      status = { ...status, email: 'ada@example.com' }
      const { useAssistantStore } = await import('@/stores/assistant')
      const store = useAssistantStore()
      await store.loadStatus()
      await atLimit()
      await tickAndSend()
      await confirmWith('424242')
      expect(sent(`${BYCODE}/confirm`)).toHaveLength(1)
      expect(wrapper.find('[data-testid="assistant-account"]').exists()).toBe(false)
      expect(store.status.email).toBe('ada@example.com')
    })

    it.each([
      ['unknown_instance', 401, "Trinity doesn't recognise this instance"],
      ['register_first', 409, 'This instance is not registered yet'],
      ['workspace_only', 403, "Ask this instance's admin to sign in and subscribe."],
      ['subscription_key_refused', 403, 'Subscribing needs a person signed in, not a key.'],
    ])('a %s refusal at confirm is a dead end: no new code, no second press', async (code, http, line) => {
      await atLimit()
      await tickAndSend()
      refuse('/confirm', http, code)
      await confirmWith('424242')
      expect(account().text()).toContain(line)
      expect(account().text()).not.toContain("the gateway's words")
      expect(wrapper.find('[data-testid="assistant-sub-resend"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="assistant-sub-confirm"]').exists()).toBe(false)
      expect(account().text()).not.toMatch(/send a new code/i)
    })

    it.each([
      ['invalid_ip', 'where this request came from'],
      ['invalid_who', 'who accepted the terms'],
    ])('a %s refusal at confirm says so and lets the person press Confirm again', async (code, line) => {
      await atLimit()
      await tickAndSend()
      refuse('/confirm', 422, code)
      await confirmWith('424242')
      expect(account().text()).toContain(line)
      expect(account().text()).not.toContain("the gateway's words")
      expect(wrapper.get('[data-testid="assistant-sub-code"]').text()).not.toMatch(/send a new code/i)
      await confirmWith('424242')
      expect(sent(`${BYCODE}/confirm`)).toHaveLength(2)
    })

    it.each([
      ['gateway_unreachable', 502],
      ['workspace_only', 403],
      ['subscription_key_refused', 403],
    ])('a %s refusal reading the terms offers Try again, which reads them again', async (code, http) => {
      refuse('/terms', http, code)
      await atLimit()
      expect(wrapper.find('[data-testid="assistant-sub-terms-tick"]').exists()).toBe(false)
      await wrapper.get('[data-testid="assistant-sub-terms-retry"]').trigger('click')
      await flushPromises()
      expect(sent(`${BYCODE}/terms`)).toHaveLength(2)
      expect(wrapper.find('[data-testid="assistant-sub-terms-retry"]').exists()).toBe(false)
      expect(wrapper.get('[data-testid="assistant-sub-terms-text"]').text()).toContain('You pay monthly.')
    })

    it('reloads the terms and clears the tick at once when the code names another version', async () => {
      await atLimit()
      codeVersion = 's7'
      subTerms = { ...subTerms, version: 's7', text: '# Subscription terms\n\nMoved on.' }
      await tickAndSend()
      expect(sent(`${BYCODE}/terms`)).toHaveLength(2)
      expect(wrapper.find('[data-testid="assistant-sub-code"]').exists()).toBe(false)
      expect(account().text()).toContain('The subscription terms changed')
      expect(wrapper.get('[data-testid="assistant-sub-terms-text"]').text()).toContain('Moved on.')
      expect(wrapper.get('[data-testid="assistant-sub-terms-tick"]').element.checked).toBe(false)
      await tickAndSend()
      await confirmWith('424242')
      expect(sent(`${BYCODE}/confirm`).at(-1).body).toEqual({ code: '424242', accept: true, version: 's7' })
    })

    it('confirms terms that name no version with an empty version', async () => {
      subTerms = { ...subTerms, version: null }
      await atLimit()
      expect(account().text()).not.toContain('(version')
      await tickAndSend()
      expect(wrapper.find('[data-testid="assistant-sub-code"]').exists()).toBe(true)
      await confirmWith('424242')
      expect(sent(`${BYCODE}/confirm`).map((c) => c.body)).toEqual([{ code: '424242', accept: true, version: '' }])
    })

    it.each([
      ['register_first', 409, 'This instance is not registered yet'],
      ['email_limit', 429, 'Too many codes have been sent'],
      ['code_limit', 429, 'Too many codes have been sent'],
      ['address_limit', 429, 'Too many codes have been sent'],
      ['email_off', 503, "can't send email right now"],
      ['email_failed', 502, "The code couldn't be sent."],
    ])('a %s refusal on sending says what to do and sends nothing more', async (code, http, line) => {
      await atLimit()
      refuse('/code', http, code)
      await tickAndSend()
      expect(account().text()).toContain(line)
      expect(wrapper.find('[data-testid="assistant-sub-code"]').exists()).toBe(false)
      expect(wrapper.get('[data-testid="assistant-sub-terms-tick"]').element.checked).toBe(true)
    })

    it('names the admin when someone else asks to subscribe', async () => {
      refuse('/terms', 403, 'subscription_admin_only', { admin: 'admin' })
      await atLimit()
      expect(account().text()).toContain("Only this instance's admin (admin) can subscribe.")
      expect(wrapper.find('[data-testid="assistant-sub-terms-retry"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="assistant-sub-send"]').exists()).toBe(false)
      expect(sent(`${BYCODE}/code`)).toHaveLength(0)
    })

    it('falls back to signing in by email on a module without the by-code routes', async () => {
      byCode = false
      await atLimit()
      expect(sent(`${BYCODE}/terms`)).toHaveLength(1)
      expect(wrapper.find('[data-testid="assistant-email"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="assistant-sub-terms-tick"]').exists()).toBe(false)
    })

    it('asks for the by-code terms only once after falling back', async () => {
      byCode = false
      await atLimit()
      await button('Cancel').trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-testid="assistant-account"]').exists()).toBe(false)
      await wrapper.get('[data-testid="assistant-upgrade"]').trigger('click')
      await flushPromises()
      expect(sent(`${BYCODE}/terms`)).toHaveLength(1)
      expect(wrapper.find('[data-testid="assistant-email"]').exists()).toBe(true)
    })
  })

  // trinity-enterprise#1355: where the instance says the assistant leads the
  // first session, the panel opens by itself with one question and two answers.
  describe('the first session where the assistant leads', () => {
    const leads = async () => {
      agentsList = [{ name: 'trinity-system', status: 'running', is_system: true }]
      status = { ...status, leads_setup: true, profile_known: false }
      wrapper.unmount()
      setActivePinia(createPinia())
      wrapper = mount(AssistantWidget, {
        attachTo: document.body,
        global: { stubs: {
          ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' },
          HelpChatWidget: { props: ['embedded', 'askOnly'], template: '<div data-testid="ask-tab">ask</div>' },
        } },
      })
      await flushPromises()
      await flushPromises()
    }

    it('opens by itself, even where the screen is narrow, with one question and two answers', async () => {
      await leads()
      expect(wrapper.find('[data-testid="assistant-input"]').exists()).toBe(true)
      const said = wrapper.findAll('.bubble').map((b) => b.text())
      expect(said).toHaveLength(1)
      expect(said[0]).toContain('Where are you starting from?')
      const offered = wrapper.findAll('[data-testid="assistant-starters"]')
      expect(offered).toHaveLength(1)
      expect(offered[0].findAll('button').map((b) => b.text())).toEqual(['I already run agents in Claude Code', 'Start from scratch'])
      expect(calls.filter((c) => c.method !== 'get')).toEqual([])
    })

    it('does not open by itself when the person closed it before', async () => {
      localStorage.setItem('trinity-assistant-open', 'false')
      await leads()
      expect(wrapper.find('[data-testid="assistant-input"]').exists()).toBe(false)
    })

    it('Start from scratch goes to the assistant, which is told the question it answers', async () => {
      await leads()
      await button('Start from scratch').trigger('click')
      await flushPromises()
      expect(sent(TURN)[0].body.message).toBe('Start from scratch')
      expect(sent(TURN)[0].body.context.said).toContain('Where are you starting from?')
      expect(sent(TURN)[0].body.context.first_session).toBe(true)     // the assistant follows its first-session script
    })

    it('a typed answer goes to the assistant the same way', async () => {
      await leads()
      await say('I run a bakery, what can you do?')
      expect(sent(TURN)[0].body.message).toBe('I run a bakery, what can you do?')
      expect(sent(TURN)[0].body.context.said).toContain('Where are you starting from?')
    })

    // trinity-enterprise#1353: the hand-off for agents already run elsewhere.
    it('agents already run elsewhere: the answer goes to the assistant, as the first-session answer it is', async () => {
      await leads()
      turns = [final('Here is how to bring them in.', [{ action: 'local-move' }])]
      await button('I already run agents in Claude Code').trigger('click')
      await flushPromises()
      expect(sent(TURN)).toHaveLength(1)
      expect(sent(TURN)[0].body.message).toBe('I already run agents in Claude Code')
      expect(sent(TURN)[0].body.context.first_session).toBe(true)
      expect(sent(TURN)[0].body.context.said).toContain('Where are you starting from?')
    })

    it('the steps carry this instance\'s address, and the outside-access step while it is off', async () => {
      await leads()
      turns = [final('Here is how to bring them in.', [{ action: 'local-move' }])]
      await button('I already run agents in Claude Code').trigger('click')
      await flushPromises()
      const card = wrapper.get('[data-testid="assistant-local-move"]')
      expect(card.text()).toContain(window.location.origin)
      expect(card.text()).toContain('/trinity:connect')
      expect(card.text()).toContain('/agent-dev:agent-fleet-analysis')
      expect(card.text()).toContain('/agent-dev:agent-fleet-migrate')
      expect(card.find('[data-testid="local-move-access"]').exists()).toBe(true)
      expect(card.find('[data-testid="local-move-access"] a').attributes('href')).toBe('/settings?tab=mcp-keys')
      expect(card.findAll('button').map((b) => b.text())).not.toContain('Confirm')     // nothing to confirm: it changes nothing here
    })

    it('outside access already on: that step is left out', async () => {
      externalAccess = { enabled: true, source: 'settings', advertised_url: 'https://trinity.example/mcp' }
      await leads()
      turns = [final('Here is how.', [{ action: 'local-move' }])]
      await button('I already run agents in Claude Code').trigger('click')
      await flushPromises()
      const card = wrapper.get('[data-testid="assistant-local-move"]')
      expect(card.find('[data-testid="local-move-access"]').exists()).toBe(false)
      expect(card.text()).toContain('/trinity:connect')
    })

    it('outside access unreadable: the steps still show, with the place to look if connecting is refused', async () => {
      externalAccess = { fails: true }
      await leads()
      turns = [final('Here is how.', [{ action: 'local-move' }])]
      await button('I already run agents in Claude Code').trigger('click')
      await flushPromises()
      const card = wrapper.get('[data-testid="assistant-local-move"]')
      expect(card.get('[data-testid="local-move-access"]').text()).toContain('external_mcp_access_off')
      expect(card.text()).toContain('/trinity:connect')
    })

    it('a "no" the assistant kept is not asked again: the opening offers only the other path', async () => {
      status = { ...status, local_agents: 'no' }
      await leads()
      const offered = wrapper.findAll('[data-testid="assistant-starters"] button').map((b) => b.text())
      expect(offered).toEqual(['Start from scratch'])
    })

    it('steps already given: the opening asks whether the agents are here instead of offering the steps again', async () => {
      status = { ...status, local_agents: 'offered' }
      await leads()
      const offered = wrapper.findAll('[data-testid="assistant-starters"] button').map((b) => b.text())
      expect(offered).toEqual(['My agents are here', 'Start from scratch'])
      await button('My agents are here').trigger('click')
      await flushPromises()
      expect(sent(TURN)[0].body.message).toBe('My agents are here')
    })

    it('steps already given: "My agents are here" stays once the moved agents have arrived', async () => {
      status = { ...status, local_agents: 'offered' }
      await leads()
      const { useAssistantStore } = await import('@/stores/assistant')
      useAssistantStore().fleet = [{ name: 'trinity-system', status: 'running', is_system: true }, { name: 'moved-in', status: 'running' }]
      await flushPromises()
      const offered = wrapper.findAll('[data-testid="assistant-starters"] button').map((b) => b.text())
      expect(offered).toContain('My agents are here')
    })

    it('raises no settings step in the first session: no getting-started list, no settings nudge', async () => {
      checklist = { completed_count: 0, total_count: 2, complete: false, dismissed: false, items: [
        { key: 'claude', title: 'Connect Claude in Settings', description: 'a', done: false, action_label: 'x', action_route: '/' },
        { key: 'first_agent_created', title: 'Create your first agent', description: 'b', done: false, action_label: 'x', action_route: '/' },
      ] }
      await leads()
      const { useAssistantStore } = await import('@/stores/assistant')
      useAssistantStore().greet({ key: 'connect', text: 'One thing before your agents can run: connect a model in Settings.' })
      await flushPromises()
      expect(wrapper.text()).not.toContain('Connect Claude in Settings')
      expect(wrapper.text()).not.toContain('connect a model in Settings')
    })

    it('an instance where the assistant does not lead keeps its usual hello', async () => {
      agentsList = [{ name: 'trinity-system', status: 'running', is_system: true }]
      status = { ...status, profile_known: false }
      await openPanel()
      expect(wrapper.text()).not.toContain('Where are you starting from?')
      expect(button('Start from scratch')).toBeUndefined()
      await say('hello')
      expect(sent(TURN)[0].body.context.first_session).toBeUndefined()
    })
  })

  // trinity-enterprise#1355 checkpoint 3: Claude is connected in the chat at the
  // moment the first agent is about to run, and the first result opens by itself.
  describe('the first run', () => {
    const CLAUDE = { action: 'credential', name: 'ANTHROPIC_API_KEY', why: 'Your agent runs on Claude.' }
    const save = async (value) => {
      await wrapper.get('[data-testid="assistant-credential-input"]').setValue(value)
      await wrapper.get('[data-testid="assistant-credential"]').trigger('submit')
      await flushPromises()
    }

    it('connects Claude through the shared credential field; the key goes to this instance only', async () => {
      turns = [final('One thing before it runs: connect Claude.', [CLAUDE]), final('Running it now.')]
      await openPanel()
      await say('run my agent')
      expect(wrapper.findAll('[data-testid="assistant-credential"]')).toHaveLength(1)
      expect(wrapper.get('[data-testid="assistant-credential-input"]').attributes('type')).toBe('password')
      expect(wrapper.get('[data-testid="assistant-credential-help"] a').attributes('href')).toMatch(/^https:\/\/console\.anthropic\.com/)
      await save('sk-ant-api03-SECRETVALUE')
      const stored = calls.filter((c) => c.url === '/api/enterprise/assistant/credential' && c.method === 'post')
      expect(stored.map((c) => c.body)).toEqual([{ agent: null, name: 'ANTHROPIC_API_KEY', value: 'sk-ant-api03-SECRETVALUE' }])
      expect(calls.some((c) => c.url.startsWith('/api/settings/api-keys'))).toBe(false)     // the instance saves and checks it, not the page
      expect(JSON.stringify(sent(TURN))).not.toContain('SECRETVALUE')
      expect(sent(TURN)[1].body).toMatchObject({ counted: false, message: 'Provided: ANTHROPIC_API_KEY for this instance.' })
      expect(wrapper.find('[data-testid="assistant-credential-input"]').exists()).toBe(false)
      expect(wrapper.get('[data-testid="assistant-credential-connected"]').text()).toContain('Connected 2 agents')
      expect(wrapper.find('[data-testid="assistant-credential-check"]').exists()).toBe(false)
    })

    it('a key Anthropic rejects after the save is said on the card and to the assistant, never the key', async () => {
      credentialAnswer = { provided: true, agent: null, name: 'ANTHROPIC_API_KEY', replaced: false, connected_agents: 0,
        working: false, problem: 'Anthropic rejected this key.' }
      turns = [final('Connect Claude first.', [CLAUDE]), final('That key was not accepted.')]
      await openPanel()
      await say('run my agent')
      await save('sk-ant-api03-SECRETVALUE')
      expect(sent(TURN)[1].body.message).toBe(
        'Provided: ANTHROPIC_API_KEY for this instance. Anthropic did not accept it, so agents cannot run on it yet.')
      expect(wrapper.get('[data-testid="assistant-credential-check"]').text()).toContain('Anthropic rejected this key.')
      expect(JSON.stringify(sent(TURN))).not.toContain('SECRETVALUE')
    })

    it('there is one credential card: the old connect-claude card is not shown', async () => {
      turns = [final('Connect Claude.', [{ action: 'connect-claude' }])]
      await openPanel()
      await say('run my agent')
      expect(wrapper.find('[data-testid="assistant-claude"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="assistant-claude-key"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="assistant-credential"]').exists()).toBe(false)
      expect(wrapper.findAll('.bubble').at(-1).text()).toContain('Connect Claude.')
    })

    it('opens the first result in front of the person where the assistant leads, once', async () => {
      vi.useFakeTimers()
      const { useAssistantStore } = await import('@/stores/assistant')
      runs = [{ status: 'success', ended: true, ok: true, error: '' }]
      turns = [final("It's running.", [{ action: 'run', agent: 'issue-digest', execution_id: 'exec-12345', title: 'First run' }]), final('Here is what it produced.')]
      await openPanel()
      useAssistantStore().status = { ...useAssistantStore().status, leads_setup: true }
      await say('run it')
      await vi.advanceTimersByTimeAsync(1600)
      await flushPromises()
      expect(pushed).toEqual(['/agents/issue-digest/executions/exec-12345'])
      expect(wrapper.get('[data-testid="assistant-result"]').attributes('href')).toContain('/agents/issue-digest/executions/exec-12345')
      vi.useRealTimers()
    })

    it('elsewhere the result is a link, not a jump', async () => {
      vi.useFakeTimers()
      runs = [{ status: 'success', ended: true, ok: true, error: '' }]
      turns = [final("It's running.", [{ action: 'run', agent: 'issue-digest', execution_id: 'exec-12345', title: 'A run' }]), final('Done.')]
      await openPanel()
      await say('run it')
      await vi.advanceTimersByTimeAsync(1600)
      await flushPromises()
      expect(pushed).toEqual([])
      expect(wrapper.find('[data-testid="assistant-result"]').exists()).toBe(true)
      vi.useRealTimers()
    })
  })

  // trinity-enterprise#1356: on a newly set-up instance the first status is read
  // while the account report is still pending, so it carries nothing to say.
  // The panel used to write its own hello at once, and the settled re-read a
  // moment later brought Trinity's own opening, which took the hello's place:
  // the first thing a new person read changed under them. The opening is now
  // decided once the status has settled, and a line already shown is not shown
  // again on a reload or a second sign-in.
  describe('one opening, decided once', () => {
    const NUDGE = { key: 'first-agent', text: "You don't have an agent yet. Tell me one task you'd hand off." }
    const SETTLED = { available: true, contacted: true, reachable: true, plan: 'free', email: null, allowance: ALLOWANCE, trial_terms: { version: 't0', url: '', accepted: 't0' }, profile_known: false }
    const PENDING = { available: true, contacted: false, reachable: null, plan: null, email: null, allowance: null, trial_terms: { version: 't0', url: '', accepted: null }, bind: { state: 'pending' } }
    const seen = []
    // What the panel shows as its opening right now: its own words when empty, else the messages.
    const opening = () => {
      const empty = wrapper.find('[data-testid="assistant-welcome"]')
      if (empty.exists()) return `welcome: ${empty.text()}`
      return wrapper.findAll('.bubble').map((b) => b.text()).join(' | ')
    }
    const look = () => { const now = opening(); if (now) seen.push(now) }
    const distinct = () => [...new Set(seen)]
    // A page load: a new script with this browser's storage, signed in.
    const load = async () => {
      wrapper.unmount()
      setActivePinia(createPinia())
      const { useAuthStore } = await import('@/stores/auth')
      useAuthStore().isAuthenticated = true
      wrapper = mount(AssistantWidget, { attachTo: document.body, global: { stubs: {
        ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' },
        HelpChatWidget: { template: '<div />' } } } })
      look()
      await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
      look()
      for (const ms of [0, 0, 500, 1000, 2000, 4000, 8000]) {
        await vi.advanceTimersByTimeAsync(ms)
        await flushPromises()
        look()
      }
    }

    beforeEach(() => {
      seen.length = 0
      vi.useFakeTimers({ toFake: ['setTimeout'] })
    })
    afterEach(() => { vi.useRealTimers() })

    it("Trinity's opening: shown alone on first open, on reload and on a second sign-in", async () => {
      statusReads = [{ ...PENDING }]
      status = { ...SETTLED, nudge: { ...NUDGE } }
      await load()
      expect(distinct()).toEqual([NUDGE.text])
      expect(wrapper.findAll('.bubble').length).toBe(1)

      seen.length = 0
      await load()                                   // a reload: Trinity says the same line again
      expect(distinct()).toEqual([NUDGE.text])
      expect(wrapper.findAll('.bubble').length).toBe(1)

      seen.length = 0
      sessionStorage.clear()                         // a second sign-in, in a new session
      status = { ...SETTLED, nudge: null }           // Trinity was told it was seen: it does not say it again
      await load()
      expect(distinct()).toEqual([NUDGE.text])
      expect(wrapper.findAll('.bubble').length).toBe(1)
    })

    it("the panel's own hello: shown alone on first open, on reload and on a second sign-in", async () => {
      statusReads = [{ ...PENDING }]
      status = { ...SETTLED }
      await load()
      const shown = distinct()
      expect(shown.length).toBe(1)
      expect(shown[0]).toContain("I'm Trinity's assistant")

      seen.length = 0
      await load()
      expect(distinct()).toEqual(shown)

      seen.length = 0
      sessionStorage.clear()
      await load()
      expect(distinct()).toEqual(shown)
    })

    // trinity-enterprise#1355 + #1356: the first-session question is an opening
    // like any other, so it waits on the same gate. Decided on the early status
    // (no account report yet, so no `leads_setup`) the panel wrote its own hello,
    // and the question never came once the settled status said the assistant leads.
    it('the first-session question waits for the settled status, and is the only opening', async () => {
      agentsList = [{ name: 'trinity-system', status: 'running', is_system: true }]
      statusReads = [{ ...PENDING }]
      status = { ...SETTLED, leads_setup: true }
      await load()
      expect(distinct()).toHaveLength(1)
      expect(distinct()[0]).toContain('Where are you starting from?')
      expect(wrapper.findAll('.bubble')).toHaveLength(1)
    })

    // Until the agent list is in, an empty fleet looks like a first session. A
    // person who already has an agent of their own must never be asked it.
    it('waits for the agent list: someone with an agent of their own gets the hello, never the question', async () => {
      agentsList = [{ name: 'trinity-system', status: 'running', is_system: true }, { name: 'issue-digest', status: 'running' }]
      let release
      agentsWait = new Promise((r) => { release = r })
      status = { ...SETTLED, leads_setup: true }
      await load()
      expect(seen).toEqual([])                                  // nothing decided while the list is out
      expect(wrapper.find('[data-testid="assistant-opening-pending"]').exists()).toBe(true)
      release()
      await flushPromises()
      look()
      expect(distinct()).toHaveLength(1)
      expect(distinct()[0]).toContain("I'm Trinity's assistant")
      expect(distinct()[0]).not.toContain('Where are you starting from?')
    })

    // The settle re-read can fail with the status already on screen. The error
    // line only shows before there is a status, so the placeholder must stay:
    // otherwise the conversation is blank until the next re-read.
    it('a failed re-read while settling keeps the placeholder: never a blank conversation', async () => {
      statusReads = [{ ...PENDING }, { fails: true }]
      status = { ...SETTLED }
      const blanks = []
      wrapper.unmount()
      setActivePinia(createPinia())
      const { useAuthStore } = await import('@/stores/auth')
      useAuthStore().isAuthenticated = true
      wrapper = mount(AssistantWidget, { attachTo: document.body, global: { stubs: {
        ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' },
        HelpChatWidget: { template: '<div />' } } } })
      await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
      let failedSeen = false
      for (const ms of [0, 0, 250, 250, 250, 250, 500, 500, 1000, 2000, 4000]) {
        await vi.advanceTimersByTimeAsync(ms)
        await flushPromises()
        const { useAssistantStore } = await import('@/stores/assistant')
        if (useAssistantStore().statusError) failedSeen = true
        const shows = opening() || wrapper.find('[data-testid="assistant-opening-pending"]').exists()
        if (wrapper.find('[data-testid="assistant-input"]').exists() && !shows) blanks.push(ms)
      }
      expect(failedSeen).toBe(true)
      expect(blanks).toEqual([])
    })

    // The other side of that (validation XsCeM4B5n3JEwNbhNhJF_A on #4736): when
    // the read fails and the retry error is what the panel says, nothing pulses
    // under it as if the assistant were still loading.
    it('a failed first read shows the retry error alone: no loading placeholder pulses under it', async () => {
      statusReads = [{ fails: true }, { fails: true }, { fails: true }, { fails: true }, { fails: true }, { fails: true }]
      status = { ...SETTLED }
      const both = []
      let errorSeen = false
      wrapper.unmount()
      setActivePinia(createPinia())
      const { useAuthStore } = await import('@/stores/auth')
      useAuthStore().isAuthenticated = true
      wrapper = mount(AssistantWidget, { attachTo: document.body, global: { stubs: {
        ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' },
        HelpChatWidget: { template: '<div />' } } } })
      await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
      for (const ms of [0, 0, 250, 500, 1000, 2000, 4000]) {
        await vi.advanceTimersByTimeAsync(ms)
        await flushPromises()
        const error = document.body.textContent.includes("Couldn't load the assistant")
        if (error) errorSeen = true
        const pulsing = wrapper.findAll('[aria-busy="true"]').length > 0
        if (error && pulsing) both.push(ms)
      }
      expect(errorSeen).toBe(true)
      expect(both).toEqual([])

      // And with the status on screen (re-validation Zqjq3aKcBH1mjog-KtmXmA on #4736):
      // a re-read that fails while the opening settles sets statusError, but the
      // retry error belongs to the no-status branch only. It must never render
      // beside the conversation, where it would sit over a working panel.
      statusReads = [{ ...PENDING }, { fails: true }, { fails: true }, { fails: true }]
      status = { ...SETTLED }
      const beside = []
      let failedWithStatus = false
      wrapper.unmount()
      setActivePinia(createPinia())
      const auth = await import('@/stores/auth')
      auth.useAuthStore().isAuthenticated = true
      wrapper = mount(AssistantWidget, { attachTo: document.body, global: { stubs: {
        ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' },
        HelpChatWidget: { template: '<div />' } } } })
      await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
      for (const ms of [0, 0, 250, 250, 250, 250, 500, 500, 1000, 2000, 4000]) {
        await vi.advanceTimersByTimeAsync(ms)
        await flushPromises()
        const { useAssistantStore } = await import('@/stores/assistant')
        const store = useAssistantStore()
        if (store.status && store.statusError) failedWithStatus = true
        const conversation = wrapper.find('[data-testid="assistant-input"]').exists()
        const error = document.body.textContent.includes("Couldn't load the assistant")
        if (conversation && error) beside.push(ms)
      }
      expect(failedWithStatus).toBe(true)
      expect(beside).toEqual([])
    })

    it('someone it already knows, with nothing to say: its own words, and nothing replaces them', async () => {
      statusReads = [{ ...PENDING }]
      status = { ...SETTLED, profile_known: true }
      await load()
      const shown = distinct()
      expect(shown.length).toBe(1)
      expect(shown[0]).toContain('get agents working for you')
    })
  })
})
