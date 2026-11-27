// @vitest-environment jsdom
/**
 * trinity-enterprise#1409 — the assistant introduces Cornelius as the company
 * brain after the first result, and walks the first document in.
 * Mounted through the real AssistantWidget and the real store; the instance
 * (`/api/onboarding/brain`, the task route, the execution read) is faked at
 * the api seam.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const ALLOWANCE = { plan: 'free', period_hours: 24, messages_left: 20, messages_limit: 25, builds_left: 0, builds_limit: 1, reached: null, resets_at: null }
const OWN = 'bakery-scout'
const BRAIN = 'cornelius'
const calls = []
let brain          // what /api/onboarding/brain answers (null → 500)
let brainAfterRun  // what it answers once the setup run ended
let runStatus
let seeded

vi.mock('@/api', () => {
  const respond = (method, url, body, config) => {
    calls.push({ method, url, body, params: config?.params })
    if (url === '/api/enterprise/assistant/status') {
      return { available: true, contacted: true, reachable: true, plan: 'free', email: null, allowance: null, leads_setup: true,
        trial_terms: { version: 't0', url: '', accepted: 't0' }, profile_known: true }
    }
    if (url === '/api/onboarding/first-run') {
      return { first_run: false, seeded_agents: seeded, own_agent_count: 1, brain_agent: seeded.includes(BRAIN) ? BRAIN : null }
    }
    if (url === '/api/onboarding/brain') {
      if (brain === null) throw Object.assign(new Error('down'), { response: { status: 500 } })
      return brain
    }
    if (url === '/api/enterprise/assistant/turn' || url === '/api/enterprise/assistant/turn/continue') {
      return { status: 'final', conversation_id: 'c1', text: 'Thanks.', cards: [], allowance: ALLOWANCE }
    }
    if (url === `/api/agents/${BRAIN}/task`) return { execution_id: 'exec-brain-1', status: 'accepted' }
    if (url === `/api/agents/${BRAIN}/executions/exec-brain-1`) {
      if (runStatus !== 'running') brain = brainAfterRun
      return { id: 'exec-brain-1', status: runStatus, error: runStatus === 'failed' ? 'Authentication failure: Not logged in' : null }
    }
    if (url === `/api/enterprise/assistant/runs/${OWN}/exec-own-1`) return { status: 'success', ended: true, ok: true }
    if (url === '/api/agents') {
      return [{ name: 'trinity-system', status: 'running', is_system: true }, { name: OWN, status: 'running' },
        ...seeded.map((name) => ({ name, status: 'running', ...(name === BRAIN ? { display_label: 'Cornelius' } : {}) }))]
    }
    if (url === '/api/agents/autonomy-status') return {}
    return {}
  }
  const call = (method) => vi.fn(async (url, a, b) => {
    const config = method === 'get' || method === 'delete' ? a : b
    return { status: 200, data: respond(method, url, method === 'get' ? undefined : a, config) }
  })
  return { default: { get: call('get'), post: call('post'), put: call('put'), delete: call('delete') } }
})
vi.mock('vue-router', () => ({ useRoute: () => ({ path: '/', params: {}, query: {} }), useRouter: () => ({ push: async () => {} }) }))
vi.mock('@/stores/agents', () => ({ useAgentsStore: () => ({ fetchAgents: async () => {} }) }))

import AssistantWidget from '@/components/assistant/AssistantWidget.vue'
import { useAssistantStore } from '@/stores/assistant'
import { useFirstRunStore } from '@/stores/firstRun'
import { BRAIN_OFFER_KEY, teamLabel } from '@/utils/companyBrain'
import { FIRST_RESULT_KEY, examplesOutOfSight, examplesToHide } from '@/utils/firstSession'

const TURN = '/api/enterprise/assistant/turn'
const posted = (url) => calls.filter((c) => c.url === url && c.method === 'post')
const brainCards = (w) => w.findAll('[data-testid="assistant-card"]').filter((c) => c.text().includes('company brain'))

describe('the company brain (ent#1409)', () => {
  let wrapper
  let store

  // The person's first agent ran once and its result is in front of them.
  const firstResult = async () => {
    store.messages.push({ id: 'm-run', role: 'assistant', content: '', cards: [{
      id: 'c-run', card: { action: 'run', agent: OWN, execution_id: 'exec-own-1', title: 'its first task' },
      label: 'Running', note: '', state: 'making', reason: '', progress: '', picked: '', since: Date.now(), fraction: null,
    }] })
    const following = store.followRun('m-run', 'c-run', { path: '/' })
    await vi.runAllTimersAsync()
    await following
    await flushPromises()
  }
  const press = async (card, words) => {
    await card.findAll('button').find((b) => b.text() === words).trigger('click')
    await flushPromises()
  }

  beforeEach(async () => {
    vi.useFakeTimers()
    setActivePinia(createPinia())
    localStorage.clear()
    sessionStorage.clear()
    calls.length = 0
    seeded = ['acme-scout', BRAIN]
    brain = { agent: BRAIN, state: 'not_set_up', notes: 0 }
    brainAfterRun = { agent: BRAIN, state: 'ready', notes: 0 }
    runStatus = 'success'
    wrapper = mount(AssistantWidget, { attachTo: document.body, global: { stubs: { HelpChatWidget: { template: '<div />' } } } })
    store = useAssistantStore()
    await useFirstRunStore().fetchState(true)
    await store.refreshFleet()
    await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
  })
  afterEach(() => {
    wrapper.unmount()
    vi.useRealTimers()
  })

  describe('it is never an example', () => {
    it('the Grid hides the other examples but never the brain', () => {
      expect(examplesToHide({ leads_setup: true }, seeded, 0, false, BRAIN)).toEqual(['acme-scout'])
    })
    it('the panel keeps the other examples out of sight, never the brain', () => {
      expect(store.outOfSight().has('acme-scout')).toBe(true)
      expect(store.outOfSight().has(BRAIN)).toBe(false)
      expect(examplesOutOfSight({ leads_setup: true }, [{ name: OWN }], seeded, false, BRAIN)).toEqual(['acme-scout'])
    })
    it('the assistant is told about it in the fleet and in its own context line', async () => {
      await store.send('hello', { path: '/' })
      const ctx = posted(TURN).at(-1).body.context
      expect(ctx.fleet.map((a) => a.name)).toContain(BRAIN)
      expect(ctx.fleet.map((a) => a.name)).not.toContain('acme-scout')
      expect(ctx.brain).toEqual({ agent: BRAIN, state: 'not_set_up', notes: 0 })
    })
  })

  describe('the offer', () => {
    it('comes after the first result, never before it', async () => {
      expect(brainCards(wrapper)).toHaveLength(0)
      await firstResult()
      expect(localStorage.getItem(FIRST_RESULT_KEY)).toBeTruthy()
      const [card] = brainCards(wrapper)
      expect(card.text()).toContain('Already on your team: Cornelius, your company brain')
      expect(card.text()).toContain('came with this instance')
      expect(card.text()).toContain("you don't type anything")
      expect(card.findAll('button').map((b) => b.text())).toEqual(['Set it up', 'Not now'])
    })

    it('is not offered on a later result', async () => {
      localStorage.setItem(FIRST_RESULT_KEY, '1')
      await firstResult()
      expect(brainCards(wrapper)).toHaveLength(0)
    })

    it('declined once, it does not come back', async () => {
      await firstResult()
      await press(brainCards(wrapper)[0], 'Not now')
      expect(localStorage.getItem(BRAIN_OFFER_KEY)).toBe('declined')
      await store.offerBrain()
      await flushPromises()
      expect(brainCards(wrapper)).toHaveLength(1)
    })

    it('is not made when the brain was deleted', async () => {
      brain = { agent: null, state: 'absent', notes: null }
      await firstResult()
      expect(brainCards(wrapper)).toHaveLength(0)
    })

    it('says honestly when the brain cannot be read', async () => {
      brain = { agent: BRAIN, state: 'unknown', notes: null }
      await firstResult()
      expect(brainCards(wrapper)[0].text()).toContain("I can't reach it right now")
    })
  })

  describe('accepting it', () => {
    it('sets the brain up with no command typed, shows progress and says when it is ready', async () => {
      runStatus = 'running'
      await firstResult()
      const card = brainCards(wrapper)[0]
      await press(card, 'Set it up')
      expect(posted(`/api/agents/${BRAIN}/task`).map((c) => c.body)).toEqual([{ message: '/first-run --empty', async_mode: true }])
      await vi.advanceTimersByTimeAsync(5000)
      await flushPromises()
      expect(brainCards(wrapper)[0].find('[data-testid="assistant-job"]').text()).toContain('Setting up Cornelius')

      runStatus = 'success'
      await vi.runAllTimersAsync()
      await flushPromises()
      const done = brainCards(wrapper)[0].text()
      expect(done).toContain('Cornelius is ready, with no notes yet.')
      expect(done).toContain('Drop files to send to Cornelius')
      expect(done).toContain('take this in')
      const told = posted(TURN).at(-1).body.message
      expect(told).toContain('The company brain Cornelius is ready with no notes yet.')
    })

    it('a brain already ready only shows the way in', async () => {
      brain = { agent: BRAIN, state: 'ready', notes: 12 }
      await firstResult()
      const card = brainCards(wrapper)[0]
      expect(card.text()).toContain('It is ready, with 12 notes.')
      await press(card, 'Show me how')
      expect(posted(`/api/agents/${BRAIN}/task`)).toHaveLength(0)
      expect(brainCards(wrapper)[0].text()).toContain('Cornelius is ready, with 12 notes.')
    })

    it('a setup that fails says why, in the agent’s own words', async () => {
      runStatus = 'failed'
      brainAfterRun = { agent: BRAIN, state: 'not_set_up', notes: 0 }
      await firstResult()
      await press(brainCards(wrapper)[0], 'Set it up')
      await vi.runAllTimersAsync()
      await flushPromises()
      const text = brainCards(wrapper)[0].text()
      expect(text).toContain("That didn't work.")
      expect(text).toContain('Not logged in')
    })
  })
})

describe('the offer card names a team, not one agent (ent#1409, ent#1416)', () => {
  it('a second core agent joins the same title with one more entry', () => {
    expect(teamLabel([{ name: 'Cornelius', role: 'company brain' }])).toBe('Already on your team: Cornelius, your company brain')
    expect(teamLabel([{ name: 'Corbin', role: 'Chief of Staff' }, { name: 'Cornelius', role: 'company brain' }]))
      .toBe('Already on your team: Corbin, your Chief of Staff; and Cornelius, your company brain')
    expect(teamLabel([])).toBe('')
  })
})
