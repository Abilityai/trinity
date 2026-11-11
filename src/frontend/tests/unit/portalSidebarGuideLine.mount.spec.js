// @vitest-environment jsdom
/**
 * Workspace Build: the person's own guide says what it is for under its name, always.
 * A chat title in its place ("AI audit for ...") read as the guide's description.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import PortalSidebar from '@/components/portal/PortalSidebar.vue'

let router
beforeEach(async () => {
  setActivePinia(createPinia())
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div />' } }] })
  await router.push('/workspace')
  await router.isReady()
})

const now = Date.now()

describe('PortalSidebar: their own guide', () => {
  it('says what the guide does under its name, never a chat title, while other agents keep theirs', async () => {
    const threads = [
      { id: 't1', agent_name: 'atlas-0a1b2c3d', title: 'AI audit for Ballers football platform', last_message_at: new Date(now - 60_000).toISOString(), unread: 0 },
      { id: 't2', agent_name: 'scout', title: 'Weekly numbers', last_message_at: new Date(now - 120_000).toISOString(), unread: 0 },
    ]
    const w = mount(PortalSidebar, {
      props: {
        roster: [{ name: 'atlas-0a1b2c3d', display_label: 'Atlas', owned: true }, { name: 'scout', display_label: 'Scout', owned: true }],
        threads,
        clientEmail: 'me@example.com',
      },
      global: { plugins: [router] },
    })
    await flushPromises()
    const rows = w.findAll('[data-agent-row]').length ? w.findAll('[data-agent-row]') : w.findAll('li, button, a')
    const atlas = rows.find((r) => r.text().includes('Atlas') && !r.text().includes('Scout'))
    expect(atlas.text()).toContain('Builds and runs agents')
    expect(atlas.text()).not.toContain('AI audit for Ballers')
    const scout = rows.find((r) => r.text().includes('Scout') && !r.text().includes('Atlas'))
    expect(scout.text()).toContain('Weekly numbers')
  })
})
