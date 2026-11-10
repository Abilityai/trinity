// @vitest-environment jsdom
/**
 * A new member's first message (trinity-enterprise#1170): the Settings panel
 * mounted for real, and the terms page's landing rule. trinity-enterprise#1198:
 * where the message comes from — the agent template, this setting, or the
 * shipped default — and what the template declares.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }))
vi.mock('../../src/api', () => ({ default: api }))

import MemberWelcomePanel from '../../src/components/settings/MemberWelcomePanel.vue'
import { useEnterpriseStore } from '../../src/stores/enterprise'
import { afterTermsLocation } from '../../src/utils/termsGate'

const DEFAULT = "Hi {first_name}, I'm {agent_name}. I'm here to help you."

function settings(welcome) {
  return { data: { welcome: { text: DEFAULT, is_default: true, replaces_hello: false, max_length: 4000, source: 'default', template: null, preview: '', ...welcome } } }
}

async function mountPanel({ entitled = true } = {}) {
  setActivePinia(createPinia())
  useEnterpriseStore().enterpriseFeatures = entitled ? ['member_agents'] : []
  const wrapper = mount(MemberWelcomePanel)
  await flushPromises()
  return wrapper
}

describe('MemberWelcomePanel', () => {
  beforeEach(() => {
    api.get.mockReset(); api.put.mockReset()
    api.get.mockResolvedValue(settings())
  })

  it('renders nothing and reads nothing without the entitlement', async () => {
    const w = await mountPanel({ entitled: false })
    expect(w.find('[data-testid="member-welcome-panel"]').exists()).toBe(false)
    expect(api.get).not.toHaveBeenCalled()
  })

  it('loads the stored text and previews it with a sample name', async () => {
    const w = await mountPanel()
    expect(api.get).toHaveBeenCalledWith('/api/enterprise/member-agents/settings')
    expect(w.find('textarea').element.value).toBe(DEFAULT)
    expect(w.find('[data-testid="member-welcome-state"]').text()).toBe('Shipped default')
    expect(w.find('[data-testid="member-welcome-preview"]').text())
      .toContain("Hi Alex, I'm your assistant.")
    expect(w.find('[data-testid="member-welcome-restore"]').attributes('disabled')).toBeDefined()
  })

  it('saves the edited text with the toggle', async () => {
    const w = await mountPanel()
    api.put.mockResolvedValue({ data: { text: 'Hello {first_name}', is_default: false, replaces_hello: false, max_length: 4000, source: 'setting', template: null, preview: 'Hello Alex' } })
    await w.find('textarea').setValue('Hello {first_name}')
    await w.find('[data-testid="member-welcome-save"]').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/enterprise/member-agents/settings/welcome',
      { text: 'Hello {first_name}', replaces_hello: false })
    expect(w.find('[data-testid="member-welcome-saved"]').exists()).toBe(true)
    expect(w.find('[data-testid="member-welcome-state"]').text()).toBe('Set here')
  })

  it('restores the default by sending null', async () => {
    api.get.mockResolvedValue(settings({ text: 'Custom', is_default: false, source: 'setting' }))
    const w = await mountPanel()
    api.put.mockResolvedValue({ data: settings().data.welcome })
    await w.find('[data-testid="member-welcome-restore"]').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/enterprise/member-agents/settings/welcome',
      { text: null, replaces_hello: false })
    expect(w.find('textarea').element.value).toBe(DEFAULT)
  })

  it('refuses a too-long text before sending it', async () => {
    const w = await mountPanel()
    await w.find('textarea').setValue('x'.repeat(4001))
    expect(w.text()).toContain('the most is 4000')
    expect(w.find('[data-testid="member-welcome-save"]').attributes('disabled')).toBeDefined()
  })

  it('says why a save failed', async () => {
    const w = await mountPanel()
    api.put.mockRejectedValue({ response: { data: { detail: { code: 'member_welcome_too_long', message: 'too long' } } } })
    await w.find('textarea').setValue('changed')
    await w.find('[data-testid="member-welcome-save"]').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('The welcome message was not saved')
  })
})

const TEMPLATE_TEXT = "Hi {first_name}, I'm {agent_name}. Here is what I can do."
const TEMPLATE_PREVIEW = "Hi Alex, I'm Scout. Here is what I can do."

function template(over) {
  return { ref: 'github:acme/agent@v1', status: 'ok', code: null, message: null, text: TEMPLATE_TEXT,
    replaces_hello: true, warnings: [], read_at: '2026-11-09T22:00:00Z', last_error: null, ...over }
}

describe('MemberWelcomePanel — where the message comes from (trinity-enterprise#1198)', () => {
  beforeEach(() => {
    api.get.mockReset(); api.put.mockReset()
  })

  it.each([
    ['template', 'From the agent template'],
    ['setting', 'Set here'],
    ['default', 'Shipped default'],
    ['off', 'Off'],
  ])('names the source %s', async (source, label) => {
    api.get.mockResolvedValue(settings({ source, is_default: source === 'default', text: source === 'off' ? '' : 'x' }))
    const w = await mountPanel()
    expect(w.find('[data-testid="member-welcome-state"]').text()).toBe(label)
  })

  it("shows the template's welcome as a member sees it, and the template sets the switch", async () => {
    api.get.mockResolvedValue(settings({ source: 'template', is_default: false, text: TEMPLATE_TEXT,
      replaces_hello: true, template: template(), preview: TEMPLATE_PREVIEW }))
    const w = await mountPanel()
    expect(w.find('[data-testid="member-welcome-template"]').text()).toContain(TEMPLATE_PREVIEW)
    const toggle = w.find('[data-testid="member-welcome-replaces"]')
    expect(toggle.attributes('aria-checked')).toBe('true')
    expect(toggle.attributes('disabled')).toBeDefined()
    expect(w.text()).toContain('Set by the agent template')
    expect(w.find('[data-testid="member-welcome-override"]').exists()).toBe(false)
    expect(w.find('[data-testid="member-welcome-save"]').attributes('disabled')).toBeDefined()
  })

  it("names an override and hands the message back to the template", async () => {
    api.get.mockResolvedValue(settings({ source: 'setting', is_default: false, text: 'Operator words',
      replaces_hello: false, template: template(), preview: 'Operator words' }))
    const w = await mountPanel()
    expect(w.find('[data-testid="member-welcome-override"]').text())
      .toContain("This setting overrides the agent template's welcome")
    expect(w.find('[data-testid="member-welcome-replaces"]').attributes('disabled')).toBeUndefined()
    api.put.mockResolvedValue({ data: settings({ source: 'template', is_default: false, text: TEMPLATE_TEXT,
      replaces_hello: true, template: template(), preview: TEMPLATE_PREVIEW }).data.welcome })
    await w.find('[data-testid="member-welcome-use-template"]').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/enterprise/member-agents/settings/welcome',
      { text: null, replaces_hello: false })
    expect(w.find('[data-testid="member-welcome-state"]').text()).toBe('From the agent template')
    expect(w.find('[data-testid="member-welcome-saved"]').text()).toContain("agent template's welcome")
  })

  it('says why the template welcome is not used', async () => {
    api.get.mockResolvedValue(settings({ template: template({ status: 'refused', text: null,
      code: 'template_welcome_unknown_placeholder', message: 'Unknown placeholders: {name}.' }) }))
    const w = await mountPanel()
    const callout = w.find('[data-testid="member-welcome-template-problem"]')
    expect(callout.text()).toContain('Unknown placeholders: {name}.')
    expect(callout.text()).toContain('template_welcome_unknown_placeholder')
    expect(callout.text()).toContain('shipped default')
    expect(w.find('[data-testid="member-welcome-template"]').exists()).toBe(false)
  })

  it('lists the keys the template declared that were ignored', async () => {
    api.get.mockResolvedValue(settings({ source: 'template', is_default: false, text: TEMPLATE_TEXT,
      template: template({ warnings: ["The template's welcome key 'tone' was ignored."] }), preview: TEMPLATE_PREVIEW }))
    const w = await mountPanel()
    expect(w.find('[data-testid="member-welcome-template-warnings"]').text()).toContain("'tone'")
  })

  it('says when the template declares no welcome', async () => {
    api.get.mockResolvedValue(settings({ template: template({ status: 'absent', text: null }) }))
    const w = await mountPanel()
    expect(w.text()).toContain('The agent template declares no welcome message')
  })
})

describe('afterTermsLocation with a landing (trinity-enterprise#1170)', () => {
  it('a Workspace-only member lands on the conversation the accept named', () => {
    expect(afterTermsLocation(undefined, 'user', '/workspace?agent=member-7'))
      .toBe('/workspace?agent=member-7')
  })

  it('the landing wins over the page they were sent from', () => {
    expect(afterTermsLocation('/workspace', 'user', '/workspace?agent=member-7'))
      .toBe('/workspace?agent=member-7')
  })

  it('an unsafe or absent landing keeps today\'s rule', () => {
    const today = afterTermsLocation('/workspace', 'user')
    expect(afterTermsLocation('/workspace', 'user', '//evil.example')).toEqual(today)
    expect(afterTermsLocation('/workspace', 'user', null)).toEqual(today)
    expect(afterTermsLocation('/workspace', 'user', '/terms')).toEqual(today)
  })
})
