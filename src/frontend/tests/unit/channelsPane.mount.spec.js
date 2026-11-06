// @vitest-environment jsdom
/**
 * trinity-enterprise#340 — Settings › Channels.
 *
 * Mounted through the real pane and sections, with only the channel panels stubbed
 * and the network mocked. What is pinned: the sections in the ruled order; the
 * header reporting only what's confirmed; `&channel=` opening a dialog and clearing
 * on close; a value that failed to load never reading as empty, and never opening
 * an editor that could overwrite it; the connector living in Integrations.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))
vi.mock('../../src/api', () => ({ default: api }))
const ax = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('axios', () => ({ default: ax }))

import ChannelsPane from '@/components/agent-settings/ChannelsPane.vue'
import IntegrationsPane from '@/components/agent-settings/IntegrationsPane.vue'
import { useEnterpriseStore } from '@/stores/enterprise'
import { useSessionsStore } from '@/stores/sessions'
import { deepLinkChannel, paneSummary, publicLinksStatus, rosterSummary } from '@/utils/channelsPane'

const AGENT = 'scout'
const U = `/api/agents/${AGENT}`
const STUB = { template: '<div class="stub-panel" />' }
const STUBS = {
  SlackChannelPanel: { template: '<div data-testid="slack-panel" />' },
  SlackAgentBotPanel: { template: '<div data-testid="bot-panel" />' },
  TelegramChannelPanel: { template: '<div data-testid="telegram-panel" />' },
  WhatsAppChannelPanel: STUB,
  VoipChannelPanel: { template: '<div data-testid="voip-panel" />' },
  PublicLinksPanel: STUB,
  PortalClientsPanel: STUB,
  VoiceRepliesControl: STUB,
  ConnectorChannelPanel: { template: '<div data-testid="connector-panel" />' },
  McpExposedPanel: STUB,
  AgentMcpKeyPanel: STUB,
  OverflowTabs: {
    props: ['tabs', 'modelValue'],
    emits: ['update:modelValue'],
    template: '<div><button v-for="t in tabs" :key="t.id" :data-tab="t.id" @click="$emit(\'update:modelValue\', t.id)">{{ t.label }}</button></div>',
  },
}

let responses
const mounted = []
const body = () => new DOMWrapper(document.body)
const fail = (status) => Object.assign(new Error(String(status)), { response: { status } })

function answer(url) {
  const r = responses[url]
  if (r === undefined) return Promise.resolve({ data: {} })
  return r instanceof Error ? Promise.reject(r) : Promise.resolve({ data: r })
}

async function render(path = `/agents/${AGENT}?tab=settings&section=channels`, props = {}) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/agents/:name', component: STUB }],
  })
  await router.push(path)
  await router.isReady()
  const w = mount(ChannelsPane, {
    props: { agentName: AGENT, shares: [{ email: 'a@example.com' }], ...props },
    attachTo: document.body,
    global: { plugins: [router], stubs: STUBS },
  })
  await flushPromises()
  mounted.push(w)
  return { w, router }
}

const headings = (w) => w.findAll('h3').map((h) => h.text())
const testid = (w, id) => w.find(`[data-testid="${id}"]`)

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
  setActivePinia(createPinia())
  for (const fn of [...Object.values(api), ...Object.values(ax)]) fn.mockReset()
  responses = {
    [`${U}/access-policy`]: { require_email: true, open_access: false },
    [`${U}/access-requests`]: [],
    [`${U}/clients`]: [],
    [`${U}/public-channel-model`]: { public_channel_model: '', available_models: ['model-a', 'model-b'], platform_default: 'model-a' },
    [`${U}/public-prompt`]: { public_channel_system_prompt: '' },
    [`${U}/slack/channel`]: { bound: true, channel_name: 'ops' },
    [`${U}/telegram`]: { configured: false },
    [`${U}/whatsapp`]: { configured: false },
    [`${U}/public-links`]: [{ enabled: true }, { enabled: false }],
  }
  ax.get.mockImplementation(answer)
  api.get.mockImplementation(answer)
  const sessions = useSessionsStore()
  sessions.loadFeatureFlags = vi.fn()
  const enterprise = useEnterpriseStore()
  enterprise.loadFeatureFlags = vi.fn()
})

afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount())
  document.body.innerHTML = ''
})

describe('what the pane holds', () => {
  it('has the ruled sections, in order', async () => {
    const { w } = await render()
    expect(headings(w)).toEqual(['Who can chat', 'Messaging', 'Web', 'How it speaks outside', 'Who has talked to it'])
  })

  it('has no MCP connector and no file sharing of its own', async () => {
    const { w } = await render()
    expect(w.text()).not.toContain('MCP connector')
    expect(testid(w, 'channels-storage-link').text()).toBe('Settings › Storage')
  })

  it('offers VoIP only where the platform has it', async () => {
    const { w } = await render()
    expect(testid(w, 'channel-row-voice-calls').exists()).toBe(false)
    useSessionsStore().voipAvailable = true
    await flushPromises()
    expect(testid(w, 'channel-row-voice-calls').exists()).toBe(true)
  })
})

describe('the header line', () => {
  it('says the policy, who waits, and where it is reachable', async () => {
    responses[`${U}/access-requests`] = [{ id: 'r', email: 'x@example.com', channel: 'web' }]
    const { w } = await render()
    expect(testid(w, 'channels-summary').text()).toBe('Restricted · 1 waiting · Reachable on Slack, public links')
  })

  it('leaves out a channel whose check failed, rather than saying it is off', async () => {
    responses[`${U}/slack/channel`] = fail(500)
    const { w } = await render()
    expect(testid(w, 'channels-summary').text()).toBe('Restricted · No one waiting · Reachable on public links')
  })

  it('leaves out the policy and the count when they failed', async () => {
    responses[`${U}/access-policy`] = fail(500)
    responses[`${U}/access-requests`] = fail(500)
    const { w } = await render()
    const line = testid(w, 'channels-summary').text()
    expect(line).not.toContain('Restricted')
    expect(line).not.toContain('waiting')
  })
})

describe('deep links', () => {
  it('&channel=telegram opens that dialog, and closing it clears the parameter', async () => {
    const { router } = await render(`/agents/${AGENT}?tab=settings&section=channels&channel=telegram`)
    expect(body().find('[data-testid="telegram-panel"]').exists()).toBe(true)
    body().find('[aria-label="Close"]').element.click()
    await flushPromises()
    expect(body().find('[data-testid="telegram-panel"]').exists()).toBe(false)
    expect(router.currentRoute.value.query).toEqual({ tab: 'settings', section: 'channels' })
  })

  it('an unknown channel opens nothing', async () => {
    await render(`/agents/${AGENT}?tab=settings&section=channels&channel=connector`)
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
  })

  it('a hidden channel opens nothing', async () => {
    await render(`/agents/${AGENT}?tab=settings&section=channels&channel=voip`)
    expect(body().find('[data-testid="voip-panel"]').exists()).toBe(false)
  })
})

describe('the Slack dialog', () => {
  const openSlack = async (w) => {
    await testid(w, 'channel-row-slack').findAll('button').find((b) => b.text() === 'Manage').trigger('click')
    await flushPromises()
  }

  it('has no tabs where the dedicated bot is not entitled', async () => {
    const { w } = await render()
    await openSlack(w)
    expect(body().find('[data-testid="slack-panel"]').exists()).toBe(true)
    expect(body().find('[data-tab="bot"]').exists()).toBe(false)
  })

  it('puts the dedicated bot on a tab where it is', async () => {
    useEnterpriseStore().$patch({ enterpriseFeatures: ['slack_per_agent_bots'] })
    const { w } = await render()
    await openSlack(w)
    expect(body().find('[data-testid="bot-panel"]').exists()).toBe(false)
    await body().find('[data-tab="bot"]').trigger('click')
    expect(body().find('[data-testid="bot-panel"]').exists()).toBe(true)
    expect(body().find('[data-testid="slack-panel"]').exists()).toBe(false)
  })
})

describe('who can chat', () => {
  it('warns when Restricted has nobody approved, and not otherwise', async () => {
    const { w } = await render(undefined, { shares: [] })
    expect(testid(w, 'channels-restricted-heads-up').exists()).toBe(true)
    const again = await render()
    expect(testid(again.w, 'channels-restricted-heads-up').exists()).toBe(false)
  })

  it('a failed policy read offers Retry and no control to press', async () => {
    responses[`${U}/access-policy`] = fail(500)
    const { w } = await render(undefined, { shares: [] })
    const row = testid(w, 'channels-policy')
    expect(row.text()).toContain("Couldn't load the policy")
    expect(row.find('[aria-pressed]').exists()).toBe(false)
    expect(testid(w, 'channels-restricted-heads-up').exists()).toBe(false)
  })

  it('a failed request read never says no one is waiting', async () => {
    responses[`${U}/access-requests`] = fail(500)
    const { w } = await render()
    const row = testid(w, 'channels-pending')
    expect(row.text()).toContain("Couldn't load requests")
    expect(row.text()).not.toContain('No one waiting')
  })

  it('choosing a mode saves it', async () => {
    ax.put.mockResolvedValue({ data: { require_email: true, open_access: true } })
    const { w } = await render()
    await testid(w, 'channels-policy').findAll('button').find((b) => b.text() === 'Open').trigger('click')
    await flushPromises()
    expect(ax.put.mock.calls[0].slice(0, 2)).toEqual([`${U}/access-policy`, { require_email: true, open_access: true }])
    expect(testid(w, 'channels-summary').text()).toContain('Open')
  })
})

describe('how it speaks outside', () => {
  it('a failed model read offers Retry and never opens an editor', async () => {
    responses[`${U}/public-channel-model`] = fail(500)
    const { w } = await render()
    expect(testid(w, 'channels-model-change').exists()).toBe(false)
    expect(testid(w, 'channels-model').text()).toContain("Couldn't load")
  })

  it('a failed instructions read never reads Not set, and never opens an editor', async () => {
    responses[`${U}/public-prompt`] = fail(500)
    const { w } = await render()
    const row = testid(w, 'channels-instructions')
    expect(row.text()).toContain("Couldn't load")
    expect(row.text()).not.toContain('Not set')
    expect(testid(w, 'channels-instructions-edit').exists()).toBe(false)
  })

  it('shows the platform default, changes the model in a dialog, and saves once', async () => {
    ax.put.mockResolvedValue({ data: { public_channel_model: 'model-b', is_overridden: true } })
    const { w } = await render()
    expect(testid(w, 'channels-model-value').text()).toBe('Platform default (model-a)')
    await testid(w, 'channels-model-change').trigger('click')
    await flushPromises()
    await body().find('#channels-model-select').setValue('model-b')
    await body().find('[data-testid="channels-model-save"]').trigger('click')
    await flushPromises()
    expect(ax.put.mock.calls[0].slice(0, 2)).toEqual([`${U}/public-channel-model`, { model: 'model-b' }])
    expect(testid(w, 'channels-model-value').text()).toBe('model-b')
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
  })

  it('a refused model save keeps the dialog and the old value', async () => {
    ax.put.mockRejectedValue(Object.assign(fail(400), { response: { status: 400, data: { detail: 'No.' } } }))
    const { w } = await render()
    await testid(w, 'channels-model-change').trigger('click')
    await flushPromises()
    await body().find('#channels-model-select').setValue('model-b')
    await body().find('[data-testid="channels-model-save"]').trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    expect(testid(w, 'channels-model-value').text()).toBe('Platform default (model-a)')
  })

  it('cancelling instructions discards the draft', async () => {
    const { w } = await render()
    await testid(w, 'channels-instructions-edit').trigger('click')
    await flushPromises()
    await body().find('#channels-instructions-text').setValue('draft')
    body().findAll('button').find((b) => b.text() === 'Cancel').element.click()
    await flushPromises()
    await testid(w, 'channels-instructions-edit').trigger('click')
    await flushPromises()
    expect(body().find('#channels-instructions-text').element.value).toBe('')
  })
})

describe('who has talked to it', () => {
  it('summarises and opens the roster', async () => {
    responses[`${U}/clients`] = [{ channel: 'telegram', identity: '1', display_name: 'Ann', message_count: 3, last_active: new Date().toISOString() }]
    const { w } = await render()
    expect(testid(w, 'channels-roster-summary').text()).toBe('1 person · 1 active this week')
    await testid(w, 'channels-roster-open').trigger('click')
    await flushPromises()
    expect(body().find('table').text()).toContain('Ann')
  })

  it('says no one yet, with nothing to open', async () => {
    const { w } = await render()
    expect(testid(w, 'channels-roster-summary').text()).toBe('No one yet')
    expect(testid(w, 'channels-roster-open').exists()).toBe(false)
  })

  it('a failed read never says no one yet', async () => {
    responses[`${U}/clients`] = fail(500)
    const { w } = await render()
    const row = testid(w, 'channels-roster')
    expect(row.text()).toContain("Couldn't load the roster")
    expect(row.text()).not.toContain('No one yet')
  })
})

describe('Settings › Integrations', () => {
  const integrations = (isSystem) => {
    const w = mount(IntegrationsPane, { props: { agentName: AGENT, isSystem }, attachTo: document.body, global: { stubs: STUBS } })
    mounted.push(w)
    return w
  }

  it('holds the MCP connector, in a dialog', async () => {
    const w = integrations(false)
    expect(body().find('[data-testid="connector-panel"]').exists()).toBe(false)
    await testid(w, 'integrations-connector').find('button').trigger('click')
    expect(body().find('[data-testid="connector-panel"]').exists()).toBe(true)
  })

  it('does not offer it on the system agent', () => {
    expect(testid(integrations(true), 'integrations-connector').exists()).toBe(false)
  })
})

describe('the pure rules', () => {
  it('only opens channels it knows', () => {
    expect(deepLinkChannel('slack')).toBe('slack')
    expect(deepLinkChannel('connector')).toBeNull()
    expect(deepLinkChannel(undefined)).toBeNull()
  })

  it('counts only live links', () => {
    expect(publicLinksStatus([{ enabled: true }, { enabled: false }])).toEqual({ connected: true, label: '1 link' })
    expect(publicLinksStatus([])).toEqual({ connected: false, label: '0 links' })
  })

  it('counts activity in the last week only', () => {
    const now = Date.parse('2026-11-08T00:00:00Z')
    const list = [{ last_active: '2026-11-07T00:00:00Z' }, { last_active: '2026-10-01T00:00:00Z' }, { last_active: null }]
    expect(rosterSummary(list, now)).toBe('3 people · 1 active this week')
  })

  it('builds the header from what is known', () => {
    expect(paneSummary({})).toBe('')
    expect(paneSummary({ mode: 'open', pending: 0, reachable: [] })).toBe('Open · No one waiting · Not reachable anywhere yet')
  })
})
