// @vitest-environment jsdom
/**
 * trinity-enterprise#1355 — the setup dialog, mounted: where the instance says
 * the assistant leads the first session, the dialog does not open by itself;
 * an install where it does not lead is unchanged. Pins the wiring from the
 * assistant store into the dialog's context, which the predicate tests in
 * firstRunSteps.spec.js cannot see.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api', () => {
  const client = {
    get: vi.fn(async () => ({ data: {} })),
    post: vi.fn(async () => ({ data: {} })),
    put: vi.fn(async () => ({ data: {} })),
    delete: vi.fn(async () => ({ data: {} })),
  }
  return { default: client }
})
vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/', params: {}, query: {} }),
  useRouter: () => ({ push: async () => {}, replace: async () => {} }),
}))

import FirstRunOverlay from '@/components/onboarding/FirstRunOverlay.vue'
import { useAssistantStore } from '@/stores/assistant'
import { useAuthStore } from '@/stores/auth'
import { useFirstRunStore } from '@/stores/firstRun'
import { useSessionsStore } from '@/stores/sessions'

describe('setup dialog and the assistant-led first session', () => {
  let wrapper

  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    setActivePinia(createPinia())
    // A fresh install: first admin sign-in, Claude not connected, nothing of their own.
    const sessions = useSessionsStore()
    sessions.featureFlagsLoaded = true
    sessions.featureFlagsFailed = false
    sessions.claudeAuthConfigured = false
    const auth = useAuthStore()
    auth.profileVerified = true
    auth.user = { role: 'admin', email: 'admin@example.com' }
    const firstRun = useFirstRunStore()
    firstRun.loaded = true
    firstRun.firstRun = true
    firstRun.fetchState = async () => {}
  })
  afterEach(() => wrapper?.unmount())

  const mountDialog = async () => {
    wrapper = mount(FirstRunOverlay, { attachTo: document.body, shallow: true })
    await flushPromises()
    return wrapper.emitted('update:open')?.at(-1)?.[0] === true
  }

  it('opens on a self-hosted install once the corner is known (unchanged)', async () => {
    useAssistantStore().corner = 'help'
    expect(await mountDialog()).toBe(true)
  })

  it('does not open while it is not yet known whether the assistant is offered', async () => {
    useAssistantStore().corner = 'undecided'
    expect(await mountDialog()).toBe(false)
  })

  it('does not open where the assistant leads the first session', async () => {
    const assistant = useAssistantStore()
    assistant.corner = 'assistant'
    assistant.status = { available: true, leads_setup: true }
    expect(await mountDialog()).toBe(false)
  })

  it('opens where the assistant is offered but does not lead', async () => {
    const assistant = useAssistantStore()
    assistant.corner = 'assistant'
    assistant.status = { available: true, leads_setup: false }
    expect(await mountDialog()).toBe(true)
  })
})
