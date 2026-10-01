// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A — sign-off: an ask's "Open the conversation" link.
 *
 * It was an INLINE button followed by the inline "Got it", so the two ran
 * together on one line; and it was action-primary-600 (no dark half) on the
 * amber ask card — low contrast and at war with the card. It now sits on its
 * own line, in the card's own ink, underlined, with a trailing arrow.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAsks from '@/components/portal/PortalAsks.vue'

const ask = (id, over = {}) => ({
  id, agent_name: 'ops', kind: 'notification', priority: 'high',
  title: 'Backup job failed twice', question: 'The nightly backup failed.', options: null,
  created_at: '2026-09-20T10:00:00Z', expires_at: null, status: 'pending',
  chat_id: 'main-1', sync: 'confirmed', aging: false, ended_at: null, ended_by: null, ...over,
})

beforeEach(() => { setActivePinia(createPinia()) })

function mountOne(over) {
  const store = useClientPortalStore()
  store.asksAvailable = true
  store.asks = [ask('n1', over)]
  return mount(PortalAsks, { props: { agentName: 'ops', currentSessionId: 'elsewhere' } })
}

describe('the thread link on an ask card', () => {
  it('is a block of its own, so the controls start on the next line', () => {
    const w = mountOne()
    const link = w.find('[data-testid="portal-ask-open-thread-n1"]')
    expect(link.exists()).toBe(true)
    expect(link.classes()).toContain('flex')        // a block-level box, not inline
    expect(link.classes()).toContain('w-fit')       // …only as wide as its words
  })

  it('is drawn in the card ink, underlined — never a raw brand blue on amber', () => {
    const link = mountOne().find('[data-testid="portal-ask-open-thread-n1"]')
    // Its INK is the card's: no text colour of its own (the focus ring may use the token).
    expect(link.classes().some((c) => /^(dark:)?(hover:)?text-(?!xs|sm|left)/.test(c))).toBe(false)
    expect(link.classes()).toContain('underline')
    expect(link.find('svg').exists()).toBe(true)    // the trailing arrow
  })

  it('the acknowledge control still renders beneath it', () => {
    const w = mountOne()
    expect(w.find('[data-testid="portal-ask-ack-n1"]').exists()).toBe(true)
  })
})
