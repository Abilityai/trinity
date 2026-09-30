// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A2 — the chat's "N more asks" line, retired by the
 * 2026-09-30 ruling (as amended the same day).
 *
 * History: sign-off round 4 put a line under the chat's pinned asks box —
 * "N more asks from this agent · Open in Work" — and §3g B1 (A2 round 1) sent a
 * client to the Inbox instead, because Work is a platform-door tab. The ruling
 * removes the premise: a chat draws only the asks ITS OWN turns raised, as rows
 * of its thread, and every other ask — another chat's, a background one — lives
 * in the Inbox (the sidebar's "needs you" mark and pinned Inbox row lead there).
 * So the chat names no other ask at all, and nothing sits above its composer
 * (principle 30).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import * as portalUtils from '@/components/portal/portalUtils'

vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(() => Promise.resolve({ data: {} })),
    put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import PortalConversation from '@/components/portal/PortalConversation.vue'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'question', title: `Ask ${id}`, question: '?', options: null,
  status: 'pending', created_at: '2026-09-30T10:00:00Z', chat_id: 's2', raised_in_turn: true, ...over,
})

let wrapper
beforeEach(() => { setActivePinia(createPinia()) })
afterEach(() => { wrapper?.unmount(); wrapper = null })

describe("a chat names no ask that isn't its own", () => {
  it.each([
    ['another chat raised', ask('other')],
    ['a background process raised (Main as reply target)', ask('bg', { chat_id: 'main', raised_in_turn: false })],
    ['has no chat', ask('loose', { chat_id: null, raised_in_turn: false })],
  ])('an ask %s: no tile, no count, no link in this chat', async (_label, a) => {
    for (const platform of [false, true]) {
      const store = useClientPortalStore()
      store.asksAvailable = true
      store.asksLoaded = true
      store.isPlatformSession = platform
      store.asks = [a]
      store.fetchHistory = vi.fn(async () => ({ sessionId: 's1', messages: [{ id: 'm1', role: 'user', content: 'hi', created_at: '2026-09-30T09:00:00Z' }] }))
      wrapper = shallowMount(PortalConversation, {
        props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1' },
        global: { renderStubDefaultSlot: true },
      })
      await flushPromises()
      expect(wrapper.find('[data-ask-id]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="portal-chat-asks-elsewhere"]').exists()).toBe(false)
      expect(wrapper.text()).not.toMatch(/more asks? from this agent|Open in Work/)
      wrapper.unmount(); wrapper = null
    }
  })

  it('the helper that wrote the line is gone with it', () => {
    expect(portalUtils.chatAsksElsewhere).toBeUndefined()
    expect(portalUtils.splitChatAsks).toBeUndefined()
  })
})
