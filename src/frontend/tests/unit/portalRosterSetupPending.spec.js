// @vitest-environment jsdom
/**
 * ent#1201 — the roster's `agent_setup_pending` bit, and the quiet refresh the
 * set-up landing polls with.
 *
 * The real `fetchRoster` runs against the real `portalHttp` client with a stub
 * adapter (the `portalTermsRefusal.spec.js` harness). The quiet refresh must
 * never put the stage back into its loading phase (no skeleton flash over the
 * "being set up" line) and a failed poll must not paint an error over it.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { portalHttp, useClientPortalStore } from '../../src/stores/clientPortal'

function answer(status, data) {
  return async (config) => {
    if (status < 400) return { status, data, headers: {}, config, statusText: 'OK' }
    const error = new Error(`HTTP ${status}`)
    error.config = config
    error.response = { status, data, headers: {}, config }
    throw error
  }
}

let store
beforeEach(() => {
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.portalToken = 'tok'
})
afterEach(() => { delete portalHttp.defaults.adapter })

describe('agent_setup_pending on the roster', () => {
  it('is raised only by a roster that says exactly true', async () => {
    portalHttp.defaults.adapter = answer(200, { agents: [], agent_setup_pending: true })
    await store.fetchRoster()
    expect(store.agentSetupPending).toBe(true)
    portalHttp.defaults.adapter = answer(200, { agents: [], agent_setup_pending: 'true' })
    await store.fetchRoster()
    expect(store.agentSetupPending).toBe(false)
    portalHttp.defaults.adapter = answer(200, { agents: [] })
    await store.fetchRoster()
    expect(store.agentSetupPending).toBe(false)
  })
})

describe('refreshRoster (quiet)', () => {
  it('keeps the verdict while it reads, and lands the agent when it appears', async () => {
    portalHttp.defaults.adapter = answer(200, { agents: [], agent_setup_pending: true })
    await store.fetchRoster()
    expect(store.rosterLoaded).toBe(true)

    let seenLoaded = null
    portalHttp.defaults.adapter = async (config) => {
      seenLoaded = store.rosterLoaded
      return { status: 200, data: { agents: [{ name: 'bob-assistant' }], agent_setup_pending: false }, headers: {}, config, statusText: 'OK' }
    }
    await store.refreshRoster()
    expect(seenLoaded).toBe(true)
    expect(store.agents.map((a) => a.name)).toEqual(['bob-assistant'])
    expect(store.agentSetupPending).toBe(false)
  })

  it('a failed poll leaves the screen as it was', async () => {
    portalHttp.defaults.adapter = answer(200, { agents: [], agent_setup_pending: true })
    await store.fetchRoster()
    portalHttp.defaults.adapter = answer(500, { detail: 'boom' })
    await store.refreshRoster()
    expect(store.error).toBe(null)
    expect(store.rosterLoaded).toBe(true)
    expect(store.agentSetupPending).toBe(true)
  })
})
