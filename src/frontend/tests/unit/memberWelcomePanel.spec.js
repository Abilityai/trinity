// @vitest-environment jsdom
/**
 * A new member's first message (trinity-enterprise#1170): the Settings panel
 * mounted for real, and the terms page's landing rule.
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
  return { data: { welcome: { text: DEFAULT, is_default: true, replaces_hello: false, max_length: 4000, ...welcome } } }
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
    expect(w.find('[data-testid="member-welcome-state"]').text()).toBe('shipped default')
    expect(w.find('[data-testid="member-welcome-preview"]').text())
      .toContain("Hi Alex, I'm your assistant.")
    expect(w.find('[data-testid="member-welcome-restore"]').attributes('disabled')).toBeDefined()
  })

  it('saves the edited text with the toggle', async () => {
    const w = await mountPanel()
    api.put.mockResolvedValue({ data: { text: 'Hello {first_name}', is_default: false, replaces_hello: false, max_length: 4000 } })
    await w.find('textarea').setValue('Hello {first_name}')
    await w.find('[data-testid="member-welcome-save"]').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/enterprise/member-agents/settings/welcome',
      { text: 'Hello {first_name}', replaces_hello: false })
    expect(w.find('[data-testid="member-welcome-saved"]').exists()).toBe(true)
    expect(w.find('[data-testid="member-welcome-state"]').text()).toBe('custom text')
  })

  it('restores the default by sending null', async () => {
    api.get.mockResolvedValue(settings({ text: 'Custom', is_default: false }))
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
