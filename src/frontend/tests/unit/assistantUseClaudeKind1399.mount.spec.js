// @vitest-environment jsdom
/**
 * trinity-enterprise#1399 — the "use it on these agents" card names the kind of
 * Claude credential that was actually saved, every time. Mounted through the
 * real AssistantWidget and the real store.
 *
 * The walk of 2026-11-25 19:10Z: a subscription token was saved through the
 * Connect Claude card (the save reported kind "subscription"), and the
 * assistant's follow-up card said "Use this API key on these agents". Its
 * confirm was refused (409, "no Anthropic API key"). The kind came from the
 * model's own argument, which read the credential's name, ANTHROPIC_API_KEY.
 * It now comes from the instance: the kind the card's save reported, or, when
 * nothing was saved in this conversation, the one kind the instance holds.
 * When it holds none, no card is shown, because its confirm could only be
 * refused.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const TOKEN = 'sk-ant-oat01-' + 't'.repeat(40)
const KEY = 'sk-ant-api03-' + 'k'.repeat(40)
const ALLOWANCE = { plan: 'free', period_hours: 24, messages_left: 24, messages_limit: 25, builds_left: 1, builds_limit: 1, reached: null, resets_at: null }
const calls = []
let turns
let held          // what the instance holds: null, 'subscription', 'api_key', 'subscription_and_api_key'
let off           // per kind, the agents not on it (credential._report: not_using), or null: unreadable
let moving        // per kind, the agents already moving onto it (credential._report: moving)
let seeded        // the example agents the instance seeded; hidden from the person during the first session

vi.mock('@/api', () => {
  const respond = (method, url, body, config) => {
    calls.push({ method, url, body, params: config?.params })
    if (url === '/api/enterprise/assistant/status') {
      return { available: true, contacted: true, reachable: true, plan: 'free', email: null, allowance: null, trial_terms: { version: 't0', url: '', accepted: 't0' }, profile_known: true,
        ...(seeded.length ? { leads_setup: true } : {}) }
    }
    if (url === '/api/onboarding/first-run') return { first_run: false, seeded_agents: seeded, own_agent_count: 1 }
    if (url === '/api/enterprise/assistant/turn' || url === '/api/enterprise/assistant/turn/continue') {
      return turns.shift() || { status: 'final', conversation_id: 'c1', text: 'Thanks.', cards: [], allowance: ALLOWANCE }
    }
    if (url === '/api/enterprise/assistant/credential' && method === 'get') {
      return { agent: null, name: config?.params?.name, set: held !== null, current: held, use: '' }
    }
    if (url === '/api/enterprise/assistant/credential' && method === 'post') {
      // What the instance's save answers (credential._claude_save): the kind is the value's shape.
      const kind = body.value.startsWith('sk-ant-api') ? 'api_key' : 'subscription'
      held = held && held !== kind ? 'subscription_and_api_key' : kind
      return { provided: true, agent: null, name: body.name, replaced: false, working: true, connected_agents: 0,
        kind, not_using: (off?.[kind] || []).map((name) => ({ name, model_access: false })), moving: moving[kind] }
    }
    if (url === '/api/enterprise/assistant/credential/agents') {
      if (off === null) throw { response: { status: 502 } }
      const kind = config?.params?.kind
      return { kind, not_using: off[kind].map((name) => ({ name, model_access: false })), moving: moving[kind] }
    }
    if (url === '/api/enterprise/assistant/credential/use') {
      // The instance's own refusals (credential.use), in its order: a kind it
      // does not hold (409), then an agent not among those off that kind (422).
      const has = held === body.kind || held === 'subscription_and_api_key'
      const words = body.kind === 'api_key' ? 'Anthropic API key' : 'Claude subscription'
      if (!has) {
        throw { response: { status: 409, data: { detail: { code: 'no_credential', message: `This instance has no ${words} to switch them to. Nothing was changed.` } } } }
      }
      const extra = body.agents.filter((n) => ![...(off?.[body.kind] || []), ...moving[body.kind]].includes(n))
      if (extra.length) {
        throw { response: { status: 422, data: { detail: { code: 'not_listed', message: `Not among the agents off ${words}: ${extra.join(', ')}. Nothing was changed.` } } } }
      }
      // A switched agent is on that kind now, so it is off it no longer.
      off[body.kind] = off[body.kind].filter((n) => !body.agents.includes(n))
      moving[body.kind] = moving[body.kind].filter((n) => !body.agents.includes(n))
      return { kind: body.kind, now_using: body.agents, not_switched: [] }
    }
    if (url === '/api/agents') return [{ name: 'scout', status: 'running' }, { name: 'scribe', status: 'running' }, ...seeded.map((name) => ({ name, status: 'stopped' }))]
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
const USE = '/api/enterprise/assistant/credential/use'
// The gateway's first-session script asks under ANTHROPIC_API_KEY (gateway/prompt.py step 8).
const ASK = { action: 'credential', name: 'ANTHROPIC_API_KEY', why: 'Your agent runs on Claude, and this connects it.' }
const switchCard = (kind, agents = ['scout']) => ({ action: 'use-claude', kind, agents })
const final = (text, cards = []) => ({ status: 'final', conversation_id: 'c1', text, cards, allowance: ALLOWANCE })
const posted = (url) => calls.filter((c) => c.url === url && c.method === 'post')

describe('the use-claude card names the kind that was saved (ent#1399)', () => {
  let wrapper
  const cards = () => wrapper.findAll('[data-testid="assistant-card"]')
  const useCard = () => cards().find((c) => c.text().includes('on these agents'))
  const button = (card, words) => card.findAll('button').find((b) => b.text() === words)

  const say = async (text) => {
    await wrapper.get('[data-testid="assistant-input"]').setValue(text)
    await wrapper.get('[data-testid="assistant-input"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
  }
  // Step 8 then 8a of the first-session script: the Claude card, the paste,
  // then the model's follow-up card with whatever kind the model chose.
  const connectThenOffer = async (value, modelKind, agents) => {
    turns = [final('Paste the token in the field below.', [ASK]),
      final('scout is not on it yet. Shall I switch it?', [switchCard(modelKind, agents)])]
    await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
    await say('build it')
    await wrapper.get('[data-testid="assistant-credential-input"]').setValue(value)
    await wrapper.get('[data-testid="assistant-credential"]').trigger('submit')
    await flushPromises()
  }
  const confirm = async () => {
    await button(useCard(), 'Yes, switch them').trigger('click')
    await flushPromises()
  }

  // The page as it loads: status and the seeded examples are read on mount.
  const open = () => {
    setActivePinia(createPinia())
    wrapper = mount(AssistantWidget, {
      attachTo: document.body,
      global: { stubs: {
        ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' },
        HelpChatWidget: { template: '<div />' },
      } },
    })
  }

  beforeEach(() => {
    calls.length = 0
    turns = []
    held = null
    off = { subscription: ['scout'], api_key: ['scout'] }     // scout is on neither yet
    moving = { subscription: [], api_key: [] }
    seeded = []
    localStorage.clear()
    sessionStorage.clear()
    open()
  })
  afterEach(() => wrapper.unmount())

  describe.each([
    ['a subscription token', TOKEN, 'subscription', 'this subscription'],
    ['an API key', KEY, 'api_key', 'this API key'],
  ])('after %s is saved', (_label, value, kind, words) => {
    const other = kind === 'subscription' ? 'api_key' : 'subscription'

    it.each([kind, other])('the card names it and its confirm succeeds, whatever kind the model sent (%s)', async (modelKind) => {
      await connectThenOffer(value, modelKind)
      expect(useCard().text()).toContain(`Use ${words} on these agents: scout`)
      await confirm()
      expect(posted(USE).map((c) => c.body)).toEqual([{ kind, agents: ['scout'] }])
      expect(posted(TURN).at(-1).body.message).toBe(`Done: Use ${words} on these agents: scout. Now on it: scout.`)
    })
  })

  it('with both held, the card uses the one just saved and says which', async () => {
    held = 'api_key'
    await connectThenOffer(TOKEN, 'api_key')
    expect(held).toBe('subscription_and_api_key')
    expect(useCard().text()).toContain('Use this subscription on these agents: scout')
    await confirm()
    expect(posted(USE).map((c) => c.body)).toEqual([{ kind: 'subscription', agents: ['scout'] }])
  })

  // ent#1399 (validation EaTT0dhdFseLaORl5bCuZA): the save's own lists say
  // which agents its switch accepts; one it did not list is never offered.
  describe("after a save, the card holds only agents the save listed", () => {
    it('an agent the save did not list is left off the card, and the confirm succeeds', async () => {
      await connectThenOffer(TOKEN, 'api_key', ['scout', 'scribe'])
      expect(useCard().text()).toContain('Use this subscription on these agents: scout')
      expect(useCard().text()).not.toContain('scribe')
      await confirm()
      expect(posted(USE).map((c) => c.body)).toEqual([{ kind: 'subscription', agents: ['scout'] }])
    })

    it('when the save listed none of them, no card is shown', async () => {
      off = { subscription: [], api_key: ['scout'] }
      await connectThenOffer(TOKEN, 'subscription')
      expect(useCard()).toBeUndefined()
      expect(posted(USE)).toHaveLength(0)
    })

    it('an agent the save lists as moving onto it stays on the card', async () => {
      off = { subscription: [], api_key: [] }
      moving = { subscription: ['scout'], api_key: [] }
      await connectThenOffer(TOKEN, 'subscription')
      expect(useCard().text()).toContain('Use this subscription on these agents: scout')
      await confirm()
      expect(posted(USE).map((c) => c.body)).toEqual([{ kind: 'subscription', agents: ['scout'] }])
    })
  })

  // ent#1399 (validation EZddZmEELapW1xpCH-aioA): the save decides the kind
  // only. Which agents are off it is read from the instance when the card is
  // built, as on the one-held path; the save's lists are what it was then.
  describe('after a save, the agents are read from the instance when the card is built', () => {
    const offerAgain = async (agents) => {
      turns = [final('Shall I switch them too?', [switchCard('api_key', agents)])]
      await say('and the others?')
    }
    const later = () => cards().filter((c) => c.text().includes('on these agents')).at(-1)

    it('confirm one agent, then a second card naming both offers only the one still off it', async () => {
      off = { subscription: ['scout', 'scribe'], api_key: ['scout', 'scribe'] }
      await connectThenOffer(TOKEN, 'api_key', ['scout'])
      await confirm()
      expect(posted(USE).map((c) => c.body)).toEqual([{ kind: 'subscription', agents: ['scout'] }])
      await offerAgain(['scout', 'scribe'])
      expect(later().text()).toContain('Use this subscription on these agents: scribe')
      expect(later().text()).not.toContain('scout')
      await button(later(), 'Yes, switch them').trigger('click')
      await flushPromises()
      expect(posted(USE).map((c) => c.body)).toEqual([
        { kind: 'subscription', agents: ['scout'] }, { kind: 'subscription', agents: ['scribe'] }])
      expect(posted(TURN).at(-1).body.message).toBe('Done: Use this subscription on these agents: scribe. Now on it: scribe.')
    })

    it('an agent created after the save is on the card, and the confirm succeeds', async () => {
      off = { subscription: ['scout'], api_key: ['scout'] }
      await connectThenOffer(TOKEN, 'api_key', ['scout'])
      off.subscription.push('scribe')        // scribe is created after the save, off the subscription
      await offerAgain(['scout', 'scribe'])
      expect(later().text()).toContain('Use this subscription on these agents: scout, scribe')
      await button(later(), 'Yes, switch them').trigger('click')
      await flushPromises()
      expect(posted(USE).map((c) => c.body)).toEqual([{ kind: 'subscription', agents: ['scout', 'scribe'] }])
      expect(calls.filter((c) => c.url.endsWith('/credential/agents')).map((c) => c.params))
        .toEqual([{ kind: 'subscription' }, { kind: 'subscription' }])
    })

    it('when the saved kind is no longer held, no card is shown', async () => {
      await connectThenOffer(TOKEN, 'api_key', ['scout'])
      held = 'api_key'                        // the subscription was removed later; a key is held
      await offerAgain(['scout'])
      expect(cards().filter((c) => c.text().includes('on these agents'))).toHaveLength(1)
      held = null
      await offerAgain(['scout'])
      expect(cards().filter((c) => c.text().includes('on these agents'))).toHaveLength(1)
    })

    it("when the instance's list cannot be read, it falls back to the save's lists", async () => {
      await connectThenOffer(TOKEN, 'api_key', ['scout', 'scribe'])
      off = null
      await offerAgain(['scout', 'scribe'])
      expect(later().text()).toContain('Use this subscription on these agents: scout')
      expect(later().text()).not.toContain('scribe')
    })
  })

  describe('with nothing saved in this conversation', () => {
    const offer = async (modelKind, agents) => {
      turns = [final('scout is not on it.', [switchCard(modelKind, agents)])]
      await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
      await flushPromises()
      await say('why can scout not run?')
    }

    it('the card names the one kind the instance holds', async () => {
      held = 'api_key'
      await offer('subscription')
      expect(useCard().text()).toContain('Use this API key on these agents: scout')
      await confirm()
      expect(posted(USE).map((c) => c.body)).toEqual([{ kind: 'api_key', agents: ['scout'] }])
    })

    it('holding one kind, an agent not off it is left off the card', async () => {
      held = 'api_key'
      await offer('api_key', ['scout', 'scribe'])
      expect(useCard().text()).toContain('Use this API key on these agents: scout')
      expect(useCard().text()).not.toContain('scribe')
      await confirm()
      expect(posted(USE).map((c) => c.body)).toEqual([{ kind: 'api_key', agents: ['scout'] }])
      expect(calls.filter((c) => c.url.endsWith('/credential/agents')).map((c) => c.params)).toEqual([{ kind: 'api_key' }])
    })

    it('holding one kind, with every agent on the card already on it, no card is shown', async () => {
      held = 'api_key'
      off = { subscription: ['scout'], api_key: [] }
      await offer('api_key')
      expect(useCard()).toBeUndefined()
      expect(posted(USE)).toHaveLength(0)
    })

    it('holding both, an agent only moving onto a kind counts as off it (not_using ∪ moving)', async () => {
      held = 'subscription_and_api_key'
      off = { subscription: [], api_key: [] }
      moving = { subscription: ['scout'], api_key: [] }
      await offer('api_key')
      expect(useCard().text()).toContain('Use this subscription on these agents: scout')
      await confirm()
      expect(posted(USE).map((c) => c.body)).toEqual([{ kind: 'subscription', agents: ['scout'] }])
    })

    it('holding both, a hidden agent is left out before the kind is chosen', async () => {
      // acme-scout is a hidden example. Only it is off the API key; scout is off the subscription.
      seeded = ['acme-scout', 'acme-sage']
      wrapper.unmount()
      open()
      await flushPromises()
      held = 'subscription_and_api_key'
      off = { subscription: ['scout'], api_key: ['acme-scout'] }
      await offer('api_key', ['scout', 'acme-scout'])
      expect(useCard().text()).toContain('Use this subscription on these agents: scout')
      expect(useCard().text()).not.toContain('acme-scout')
      await confirm()
      expect(posted(USE).map((c) => c.body)).toEqual([{ kind: 'subscription', agents: ['scout'] }])
    })

    // ent#1399 (validation of d456e08): holding both, the card names the kind
    // scout is off, so its confirm is never refused with not_listed.
    it.each([
      ['on the API key', 'api_key', 'subscription', 'this subscription'],
      ['on the subscription', 'subscription', 'api_key', 'this API key'],
    ])('holding both, with scout %s, the card names the other kind whatever the model sent', async (_on, onKind, offKind, words) => {
      held = 'subscription_and_api_key'
      off = { [onKind]: [], [offKind]: ['scout'] }
      await offer(onKind)
      expect(useCard().text()).toContain(`Use ${words} on these agents: scout`)
      await confirm()
      expect(posted(USE).map((c) => c.body)).toEqual([{ kind: offKind, agents: ['scout'] }])
      expect(posted(TURN).at(-1).body.message).toBe(`Done: Use ${words} on these agents: scout. Now on it: scout.`)
    })

    it('holding both, with scout off both, it keeps the kind the assistant named and the confirm succeeds', async () => {
      held = 'subscription_and_api_key'
      await offer('api_key')
      expect(useCard().text()).toContain('Use this API key on these agents: scout')
      await confirm()
      expect(posted(USE).map((c) => c.body)).toEqual([{ kind: 'api_key', agents: ['scout'] }])
    })

    it('holding both, with scout off neither, no card is shown: either confirm would be refused', async () => {
      held = 'subscription_and_api_key'
      off = { subscription: [], api_key: [] }
      await offer('api_key')
      expect(useCard()).toBeUndefined()
      expect(posted(USE)).toHaveLength(0)
    })

    it('holding both, when the instance cannot say which agents are off each, it keeps the kind the assistant named', async () => {
      held = 'subscription_and_api_key'
      off = null
      await offer('subscription')
      expect(useCard().text()).toContain('Use this subscription on these agents: scout')
    })

    it('holding none, no card is shown: its confirm could only be refused', async () => {
      await offer('subscription')
      expect(useCard()).toBeUndefined()
      expect(posted(USE)).toHaveLength(0)
    })
  })
})

// The same walk: the schedule card's title read "Toggle agent schedule: scout".
describe("the schedule switch card's title is in plain words (ent#1399)", () => {
  it('names switching it on or off', async () => {
    const { cardLabel } = await import('@/utils/assistantCards')
    const card = (enabled) => ({ action: 'tool', tool: 'toggle_agent_schedule', args: { agent_name: 'scout', schedule_id: 's1', enabled } })
    expect(cardLabel(card(true), [])).toBe("Switch on scout's schedule")
    expect(cardLabel(card(false), [])).toBe("Switch off scout's schedule")
  })
})
