// @vitest-environment jsdom
/**
 * #3446 — the floating help launcher sat on top of the Send button on the agent
 * Chat tab (`fixed bottom-6 right-6`, the composer's own corner).
 *
 * The launcher stands down on that tab, the way the Workspace routes already
 * turn the widget off for the same collision. It is decided from the route —
 * the tab is in the URL since #2900 — so this mounts the real `App.vue` and the
 * real `HelpChatWidget` over a router that carries the REAL `/agents/:name`
 * route meta, and walks the URL.
 *
 * Only the closed launcher goes: a help conversation the user has open holds
 * unsent state in the component and must survive a click on the Chat tab.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'

vi.mock('../../src/utils/websocket', () => ({ useWebSocket: () => ({ connect: vi.fn() }) }))

import App from '../../src/App.vue'
import { routes } from '../../src/router'
import { useAuthStore } from '../../src/stores/auth'

const LAUNCHER = '[aria-label="Open help chat"]'
const PANEL = '[role="dialog"][aria-label="Help chat"]'
const Blank = { render: () => h('div') }

let wrapper
beforeEach(() => {
  localStorage.clear()
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches: false, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  }))
})
afterEach(() => wrapper?.unmount())

async function mountApp(start) {
  const real = routes.find((r) => r.name === 'AgentDetail')
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'Dashboard', component: Blank },
      { path: real.path, name: real.name, component: Blank, meta: real.meta },
    ],
  })
  await router.push(start)
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().isAuthenticated = true
  wrapper = mount(App, { global: { plugins: [router, pinia] }, attachTo: document.body })
  await flushPromises()
  const go = async (to) => { await router.push(to); await flushPromises() }
  return { go }
}

describe('#3446 help launcher on the agent page', () => {
  it('is offered on the agent page', async () => {
    await mountApp('/agents/alpha')
    expect(wrapper.find(LAUNCHER).exists()).toBe(true)
  })

  it('stands down on the Chat tab, where it covered Send', async () => {
    await mountApp('/agents/alpha?tab=chat')
    expect(wrapper.find(LAUNCHER).exists()).toBe(false)
  })

  it('follows the tab as the URL changes, both ways', async () => {
    const { go } = await mountApp('/agents/alpha?tab=tasks')
    expect(wrapper.find(LAUNCHER).exists()).toBe(true)
    await go('/agents/alpha?tab=chat')
    expect(wrapper.find(LAUNCHER).exists()).toBe(false)
    await go('/agents/alpha?tab=git')
    expect(wrapper.find(LAUNCHER).exists()).toBe(true)
  })

  it('a ?tab=chat on some other page hides nothing', async () => {
    await mountApp('/?tab=chat')
    expect(wrapper.find(LAUNCHER).exists()).toBe(true)
  })

  it('an open help conversation survives a move to the Chat tab', async () => {
    const { go } = await mountApp('/agents/alpha')
    await wrapper.get(LAUNCHER).trigger('click')
    await flushPromises()
    expect(wrapper.find(PANEL).exists()).toBe(true)
    await go('/agents/alpha?tab=chat')
    expect(wrapper.find(PANEL).exists()).toBe(true)
    // Closing it there does not put the launcher back over Send.
    await wrapper.get('[aria-label="Close help chat"]').trigger('click')
    await flushPromises()
    expect(wrapper.find(PANEL).exists()).toBe(false)
    expect(wrapper.find(LAUNCHER).exists()).toBe(false)
  })
})
