// @vitest-environment jsdom
/**
 * trinity-enterprise#1390 — the first session's cards, in the person's words.
 * Mounted through the real AssistantWidget and the real store; every tool card
 * is built from, and checked against, a real instance's toolset
 * (helpers/instanceToolset.js).
 *
 * Seen on the walk of 2026-11-25 (dev, build 173e58cc, gateway 53b5a033b):
 *  1. "Run its first task" listed `agent name`, `message`, `parallel / true`,
 *     `async / true`, and the schedule card `cron expression / 0 9 * * 1`,
 *     `enabled / true`, each under a button that said "Confirm".
 *  2. After connecting Claude: "Not on it yet: ability-knowledge, acme-sage,
 *     acme-scout, acme-scribe, cornelius" — the examples were hidden at that
 *     point, and the person never built cornelius.
 *  3. The Claude card was titled "ANTHROPIC_API_KEY for this instance" and
 *     ended "ANTHROPIC_API_KEY provided to this instance".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { argsFit } from './helpers/instanceToolset'

const ALLOWANCE = { plan: 'free', period_hours: 24, messages_left: 20, messages_limit: 25, builds_left: 0, builds_limit: 1, reached: null, resets_at: null }
const OWN = 'ability-knowledge'
const EXAMPLES = ['acme-sage', 'acme-scout', 'acme-scribe', 'cornelius']
const calls = []
let turns
let saveReport

vi.mock('@/api', () => {
  const respond = (method, url, body, config) => {
    calls.push({ method, url, body, params: config?.params })
    if (url === '/api/enterprise/assistant/status') {
      return { available: true, contacted: true, reachable: true, plan: 'free', email: null, allowance: null, leads_setup: true,
        trial_terms: { version: 't0', url: '', accepted: 't0' }, profile_known: true }
    }
    if (url === '/api/onboarding/first-run') return { first_run: false, seeded_agents: EXAMPLES, own_agent_count: 1 }
    if (url === '/api/enterprise/assistant/turn' || url === '/api/enterprise/assistant/turn/continue') {
      return turns.shift() || { status: 'final', conversation_id: 'c1', text: 'Thanks.', cards: [], allowance: ALLOWANCE }
    }
    if (url === '/api/enterprise/assistant/tools/confirm') return { ok: true, content: '{"status": "accepted", "execution_id": "exec-1390abc"}' }
    if (url === '/api/enterprise/assistant/credential' && method === 'get') {
      return { agent: null, name: config?.params?.name, set: false, current: null, use: '' }
    }
    if (url === '/api/enterprise/assistant/credential' && method === 'post') {
      return { provided: true, agent: body.agent, name: body.name, replaced: false, ...saveReport }
    }
    if (url === '/api/agents') {
      return [{ name: 'trinity-system', status: 'running', is_system: true }, { name: OWN, status: 'running' },
        ...EXAMPLES.map((name) => ({ name, status: 'stopped' }))]
    }
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
import { SHOW_EXAMPLES_KEY } from '@/utils/firstSession'

const TURN = '/api/enterprise/assistant/turn'
const CONFIRM = '/api/enterprise/assistant/tools/confirm'
const final = (text, cards = []) => ({ status: 'final', conversation_id: 'c1', text, cards, allowance: ALLOWANCE })
const posted = (url) => calls.filter((c) => c.url === url && c.method === 'post')
const tool = (name, args) => {
  expect(argsFit(name, args)).toBe(true)          // a card the instance's own tool takes
  return { action: 'tool', tool: name, args }
}
const RUN = tool('chat_with_agent', { agent_name: OWN, message: '/add-source https://bakery.example', parallel: true, async: true })
const SCHEDULE = tool('create_agent_schedule', {
  agent_name: OWN, name: 'Monday brief', cron_expression: '0 9 * * 1', message: '/weekly-brief', timezone: 'Europe/London', enabled: true,
})

describe('the first session, in the person’s words (ent#1390)', () => {
  let wrapper

  const offer = async (cards) => {
    turns = [final('Here it is.', cards)]
    await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="assistant-input"]').setValue('go on')
    await wrapper.get('[data-testid="assistant-input"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    return wrapper.get('[data-testid="assistant-card"]')
  }
  const buttons = (card) => card.findAll('button').map((b) => b.text())
  // What the card says without its details opened.
  const body = (card) => card.text()

  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    sessionStorage.clear()
    calls.length = 0
    turns = []
    saveReport = {}
    wrapper = mount(AssistantWidget, {
      attachTo: document.body,
      global: { stubs: { HelpChatWidget: { template: '<div />' } } },
    })
  })
  afterEach(() => wrapper.unmount())

  describe('a confirm card says what will happen, and its button names it', () => {
    it('the first run: one plain sentence, "Run its first task", and the parameters only behind Details', async () => {
      const card = await offer([RUN])
      expect(body(card)).toContain(`Run its first task: ${OWN}`)
      expect(body(card)).toContain(`Starts ${OWN} on its first task now, as you.`)
      for (const raw of ['agent name', 'parallel', 'async', '/add-source']) expect(body(card)).not.toContain(raw)
      expect(buttons(card)).toContain('Run its first task')
      expect(buttons(card)).not.toContain('Confirm')

      const toggle = card.get('[data-testid="assistant-tool-details-toggle"]')
      expect(toggle.text()).toBe('Details')
      expect(toggle.attributes('aria-expanded')).toBe('false')
      await toggle.trigger('click')
      expect(toggle.attributes('aria-expanded')).toBe('true')
      const details = card.get('[data-testid="assistant-tool"]').text()
      expect(details).toContain('parallel')
      expect(details).toContain('/add-source https://bakery.example')

      await card.findAll('button').find((b) => b.text() === 'Run its first task').trigger('click')
      await flushPromises()
      expect(posted(CONFIRM).map((c) => c.body)).toEqual([{ tool: 'chat_with_agent', args: RUN.args }])
      expect(posted(TURN).at(-1).body.message.startsWith(`Done: Run its first task: ${OWN}.`)).toBe(true)
    })

    it('a schedule: when it runs in words, "Switch it on", and no cron or flag in the body', async () => {
      const card = await offer([SCHEDULE])
      expect(body(card)).toContain(`${OWN} will run “Monday brief” every Monday at 09:00 (Europe/London).`)
      for (const raw of ['cron expression', '0 9 * * 1', 'enabled', 'true']) expect(body(card)).not.toContain(raw)
      expect(buttons(card)).toContain('Switch it on')
      expect(buttons(card)).not.toContain('Confirm')
      await card.get('[data-testid="assistant-tool-details-toggle"]').trigger('click')
      expect(card.get('[data-testid="assistant-tool"]').text()).toContain('0 9 * * 1')
    })

    it('a schedule added switched off says so, and its button adds it', async () => {
      const off = tool('create_agent_schedule', { ...SCHEDULE.args, enabled: false })
      const card = await offer([off])
      expect(body(card)).toContain('It is added switched off.')
      expect(buttons(card)).toContain('Add it switched off')
    })

    it('a tool with no words of its own still gets a sentence and a button named for it, never "Confirm"', async () => {
      const card = await offer([tool('delete_agent', { name: OWN })])
      expect(body(card)).toContain(`Deletes ${OWN} from this instance. This cannot be undone.`)
      expect(buttons(card)).toContain('Delete it')
      expect(buttons(card)).not.toContain('Confirm')
    })
  })

  describe('connecting Claude', () => {
    const ASK = { action: 'credential', name: 'ANTHROPIC_API_KEY', why: 'Your first agent needs Claude to run.' }
    const save = async (value) => {
      await wrapper.get('[data-testid="assistant-credential-input"]').setValue(value)
      await wrapper.get('[data-testid="assistant-credential"]').trigger('submit')
      await flushPromises()
    }

    it('is titled "Connect Claude", says which kind was saved, and shows no variable name', async () => {
      saveReport = { kind: 'subscription', connected_agents: 0, not_using: [], moving: [] }
      const card = await offer([ASK])
      expect(body(card)).toContain('Connect Claude')
      expect(body(card)).not.toMatch(/ANTHROPIC_API_KEY|CLAUDE_SUBSCRIPTION/)
      await save('sk-ant-oat01-' + 't'.repeat(40))
      const after = wrapper.get('[data-testid="assistant-card"]')
      expect(after.get('[data-testid="assistant-credential-outcome"]').text()).toBe('Saved your Claude subscription on this instance.')
      expect(after.text()).not.toMatch(/ANTHROPIC_API_KEY|CLAUDE_SUBSCRIPTION/)
    })

    it('an API key is said as an API key', async () => {
      saveReport = { kind: 'api_key', connected_agents: 0, not_using: [], moving: [] }
      await offer([ASK])
      await save('sk-ant-api03-' + 'k'.repeat(40))
      expect(wrapper.get('[data-testid="assistant-credential-outcome"]').text()).toBe('Saved your Anthropic API key on this instance.')
    })

    it('names only agents the person can see: no hidden example, no system agent — on the card and to the assistant', async () => {
      saveReport = { kind: 'subscription', connected_agents: 0, moving: ['acme-sage'],
        not_using: [OWN, ...EXAMPLES, 'trinity-system'].map((name) => ({ name, model_access: false })) }
      await offer([ASK])
      expect(posted(TURN)[0].body.context.fleet.map((a) => a.name)).toEqual([OWN])   // the examples are not told either
      await save('sk-ant-oat01-' + 't'.repeat(40))

      const report = wrapper.get('[data-testid="assistant-credential-connected"]').text()
      expect(report).toContain(`Not on it yet: ${OWN}.`)
      for (const hidden of [...EXAMPLES, 'trinity-system']) expect(report).not.toContain(hidden)

      const told = posted(TURN).at(-1).body.message
      expect(told).toContain(`Existing agents not on it (nothing was switched): ${OWN} (cannot run as it is).`)
      for (const hidden of [...EXAMPLES, 'trinity-system']) expect(told).not.toContain(hidden)
    })

    it('a switch the assistant proposes names only agents the person can see', async () => {
      const card = await offer([{ action: 'use-claude', kind: 'subscription', agents: [OWN, 'cornelius'] }])
      expect(body(card)).toContain(`Use this subscription on these agents: ${OWN}`)
      expect(body(card)).not.toContain('cornelius')
    })

    it('once the person asked to see the examples, they are named like any other agent', async () => {
      localStorage.setItem(SHOW_EXAMPLES_KEY, 'true')
      saveReport = { kind: 'subscription', connected_agents: 0, moving: [],
        not_using: [OWN, 'cornelius', 'trinity-system'].map((name) => ({ name, model_access: true })) }
      await offer([ASK])
      await save('sk-ant-oat01-' + 't'.repeat(40))
      const report = wrapper.get('[data-testid="assistant-credential-connected"]').text()
      expect(report).toContain(`Not on it yet: ${OWN}, cornelius.`)
      expect(report).not.toContain('trinity-system')
    })
  })
})
