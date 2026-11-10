// @vitest-environment jsdom
/**
 * The models member agents run on (trinity-enterprise#1202): the read-only
 * Settings panel, mounted for real against the member-agents settings view.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }))
vi.mock('../../src/api', () => ({ default: api }))

import MemberAgentModelPanel from '../../src/components/settings/MemberAgentModelPanel.vue'
import MemberWelcomePanel from '../../src/components/settings/MemberWelcomePanel.vue'
import { useEnterpriseStore } from '../../src/stores/enterprise'
import { useMemberWelcomeStore } from '../../src/stores/memberWelcome'

const WELCOME = { text: 'Hi', is_default: true, replaces_hello: false, max_length: 4000 }
const MODEL = { setting_key: 'k', resolved: 'claude-opus-5-5', source: 'default', verified_against_subscription: false }

function agent(name, resolved, source, refused = null, template = null) {
  return { agent_name: name, template_model: template, resolved, source, refused_reason: refused }
}

function settings(agents, summary = 'summary') {
  return { data: { welcome: WELCOME, model: MODEL,
    wake_model: { agents, summary, platform_default: 'claude-sonnet-4-6', verified_against_subscription: false } } }
}

async function mountPanel({ entitled = true, component = MemberAgentModelPanel } = {}) {
  setActivePinia(createPinia())
  useEnterpriseStore().enterpriseFeatures = entitled ? ['member_agents'] : []
  const wrapper = mount(component)
  await flushPromises()
  return wrapper
}

describe('MemberAgentModelPanel', () => {
  beforeEach(() => {
    api.get.mockReset(); api.put.mockReset()
  })

  it('renders nothing and reads nothing without the entitlement', async () => {
    api.get.mockResolvedValue(settings([]))
    const w = await mountPanel({ entitled: false })
    expect(w.find('[data-testid="member-model-panel"]').exists()).toBe(false)
    expect(api.get).not.toHaveBeenCalled()
  })

  it('shows the member model and collapses to the summary when every agent agrees', async () => {
    api.get.mockResolvedValue(settings([
      agent('member-1', 'claude-opus-5', 'template'),
      agent('member-2', 'claude-opus-5', 'template'),
    ], 'Every member agent runs on its template.'))
    const w = await mountPanel()
    expect(w.find('[data-testid="member-model-turn"]').text()).toContain('claude-opus-5-5')
    expect(w.find('[data-testid="member-model-turn"]').text()).toContain('default')
    expect(w.find('[data-testid="member-model-summary"]').text()).toBe('Every member agent runs on its template.')
    expect(w.find('[data-testid="member-model-list"]').exists()).toBe(false)
    expect(w.findAll('[data-testid="member-model-refused"]')).toHaveLength(0)
  })

  it('names a refused template model and what runs instead, and lists the agents', async () => {
    api.get.mockResolvedValue(settings([
      agent('member-1', 'claude-opus-5', 'template'),
      agent('member-2', 'claude-sonnet-4-6', 'default', 'not_in_catalog', 'claude-opus-9'),
    ]))
    const w = await mountPanel()
    const [callout] = w.findAll('[data-testid="member-model-refused"]')
    expect(callout.text()).toContain("member-2's template names claude-opus-9")
    expect(callout.text()).toContain('not in the model catalog')
    expect(callout.text()).toContain('claude-sonnet-4-6')
    const rows = w.findAll('[data-testid="member-model-row"]')
    expect(rows.map((r) => r.text())).toEqual([
      expect.stringContaining('member-1'), expect.stringContaining('member-2'),
    ])
    expect(rows[1].text()).toContain('default')
  })

  it('shares one GET with the welcome panel, and a welcome save keeps the view', async () => {
    api.get.mockResolvedValue(settings([agent('member-1', 'claude-opus-5', 'template')]))
    setActivePinia(createPinia())
    useEnterpriseStore().enterpriseFeatures = ['member_agents']
    const welcome = mount(MemberWelcomePanel)
    const models = mount(MemberAgentModelPanel)
    await flushPromises()
    expect(api.get).toHaveBeenCalledTimes(1)
    api.put.mockResolvedValue({ data: { ...WELCOME, text: 'Hello', is_default: false } })
    await useMemberWelcomeStore().save('Hello', false)
    await flushPromises()
    expect(models.find('[data-testid="member-model-turn"]').text()).toContain('claude-opus-5-5')
    welcome.unmount(); models.unmount()
  })

  it('offers a retry when the read fails', async () => {
    api.get.mockRejectedValue(new Error('boom'))
    const w = await mountPanel()
    expect(w.text()).toContain("Couldn't load the member agent models")
  })
})
