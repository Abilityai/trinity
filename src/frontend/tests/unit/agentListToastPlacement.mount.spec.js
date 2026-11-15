// @vitest-environment jsdom
/**
 * The Dashboard list's toast sits bottom-right, clear of the page controls
 * (#1953, the placement AgentDetail.vue and FilesPanel.vue already use).
 *
 * `fixed top-20 right-4` lands on the Create Agent button and the
 * Timeline / Grid / List switch; the tool-access toggle's longer sentences
 * (trinity-enterprise#341) made it cover more. `bottom-24` clears the global
 * help FAB (bottom-6 + h-14), which is why it is not `bottom-4`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(() => Promise.resolve({ data: {} })),
    post: vi.fn(() => Promise.resolve({ data: {} })),
    put: vi.fn(() => Promise.resolve({
      data: { profile: 'strict_read_only', profile_label: 'Strict read-only', read_only_layer: true,
        read_only_applied: true, restart_required: true },
    })),
    delete: vi.fn(() => Promise.resolve({ data: {} })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import AgentListPanel from '../../src/components/AgentListPanel.vue'

let wrapper
beforeEach(() => setActivePinia(createPinia()))
afterEach(() => wrapper?.unmount())

describe('the Dashboard list toast', () => {
  it('is anchored bottom-right, never over the page controls (#1953)', async () => {
    wrapper = mount(AgentListPanel, {
      props: { agents: [{ name: 'alpha', status: 'running', runtime: 'claude-code', tags: [],
        is_owner: true, tool_access_profile: 'full', read_only_enabled: false }] },
      global: { stubs: { AgentAvatar: true, RuntimeBadge: true, RunningStateToggle: true,
        AutonomyToggle: true, CapacityMeter: true, RouterLink: true } },
    })
    await wrapper.find('[data-agent="alpha"] [role="switch"]').trigger('click')
    await flushPromises()
    const toast = wrapper.findAll('div').find((d) => d.classes().includes('fixed')
      && d.text().includes('Strict read-only is on'))
    expect(toast, 'a toast was raised').toBeTruthy()
    expect(toast.classes()).toEqual(expect.arrayContaining(['bottom-24', 'right-6']))
    expect(toast.classes()).not.toContain('top-20')
  })
})
