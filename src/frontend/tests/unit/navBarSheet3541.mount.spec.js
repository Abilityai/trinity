// @vitest-environment jsdom
/**
 * #3541 — the phone sheet is only reachable if NavBar renders it and hands it
 * the bar's own link set. navSheet3541.mount.spec.js mounts NavSheet alone, so
 * it stays green with the `<NavSheet :links="navLinks" />` line deleted from
 * NavBar. MOUNTED through NavBar: the trigger is in the bar, and opening it
 * lists the same pages the desktop row links to.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, ref } from 'vue'

vi.mock('vue-router', () => ({
  useRouter: () => ({ currentRoute: ref({ path: '/', fullPath: '/', query: {} }), push: vi.fn() }),
}))
vi.mock('@/utils/websocket', () => ({ useWebSocket: () => ({ isConnected: ref(true) }) }))
vi.mock('@/composables/useBuildInfo', () => ({
  useBuildInfo: () => ({ load: () => Promise.resolve(), info: ref(null), displayVersion: ref(''), isMissing: ref(false) }),
}))
vi.mock('axios', () => {
  const inst = { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } }
  return { default: { ...inst, create: () => inst } }
})
vi.mock('@/api', () => {
  const inst = { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})

import NavBar from '@/components/NavBar.vue'
import { useAuthStore } from '@/stores/auth'

let wrapper

describe('NavBar renders the phone sheet with its own links (#3541)', () => {
  beforeEach(() => { globalThis.ResizeObserver = class { observe() {} disconnect() {} } })
  afterEach(() => { wrapper?.unmount(); wrapper = null; document.body.innerHTML = ''; document.body.style.overflow = '' })

  it('carries the menu button, and tapping it lists the bar\'s pages', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useAuthStore().user = { username: 'someone', role: 'admin' }
    wrapper = mount(NavBar, {
      attachTo: document.body,
      global: {
        plugins: [pinia],
        stubs: { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' }, ThemeChoice: true },
      },
    })
    await flushPromises()

    const trigger = document.querySelector('[data-nav-sheet-trigger]')
    expect(trigger).not.toBeNull()
    expect(document.querySelector('[data-nav-sheet]')).toBeNull()

    trigger.click()
    await nextTick()
    await nextTick()

    const hrefs = [...document.querySelectorAll('[data-nav-sheet-item]')].map((el) => el.getAttribute('href'))
    expect(hrefs).toEqual(expect.arrayContaining(['/', '/library', '/operations', '/settings']))
  })
})
