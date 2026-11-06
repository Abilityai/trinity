// @vitest-environment jsdom
/**
 * trinity-enterprise#584 — the REAL `usePortalAgentPage` projects `/page`'s
 * `capabilities_state`. capabilityDescriptions.mount.spec.js mocks this
 * composable to drive the rail's arms, so this is the test that goes red if
 * the projection itself is dropped.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { defineComponent, ref } from 'vue'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))

import { useClientPortalStore } from '@/stores/clientPortal'
import { usePortalAgentPage, clearPortalAgentPageCache } from '@/composables/usePortalAgentPage'

async function stateFor(payload) {
  const store = useClientPortalStore()
  store.fetchAgentPage = vi.fn(async () => payload)
  let api
  mount(defineComponent({
    setup() {
      api = usePortalAgentPage(ref('scout'), ref('7d'))
      return () => null
    },
  }))
  await flushPromises()
  return api.capabilitiesState.value
}

beforeEach(() => {
  setActivePinia(createPinia())
  clearPortalAgentPageCache()
})

describe('usePortalAgentPage capabilitiesState', () => {
  it('carries the page\'s verdict', async () => {
    expect(await stateFor({ agent_name: 'scout', capabilities: [], capabilities_state: 'unavailable' })).toBe('unavailable')
  })

  it('reads a page from before the field as ready', async () => {
    expect(await stateFor({ agent_name: 'scout', capabilities: [] })).toBe('ready')
  })
})
