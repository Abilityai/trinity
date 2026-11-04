// @vitest-environment jsdom
/**
 * The one restart behind the Settings banner, driven through the page
 * (trinity-enterprise#887). The page is shallow-mounted over a fake backend;
 * the banner's "Restart now" is the `restart` event of the (stubbed) Settings
 * home, the header's switch is the `toggle` event of the (stubbed) header.
 *
 * Pinned here:
 *   - a start that FAILS keeps every waiting change listed (nothing applied,
 *     so nothing may be cleared) and does not bump the epoch;
 *   - a start that succeeds clears the list and bumps the epoch;
 *   - a manual stop/start from the header settles the list the same way —
 *     the container now runs what is stored, whichever button started it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'

const backend = vi.hoisted(() => {
  window.matchMedia = () => ({
    matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  })
  const state = { agent: null, failStart: false }
  const get = async (url) => {
    if (/^\/api\/agents\/[^/]+$/.test(String(url))) return { data: { ...state.agent } }
    return { data: {} }
  }
  const post = async (url) => {
    const u = String(url)
    if (u.endsWith('/stop')) { state.agent.status = 'stopped'; return { data: { message: 'stopped' } } }
    if (u.endsWith('/start')) {
      if (state.failStart) throw new Error('Failed to start agent: image missing')
      state.agent.status = 'running'
      return { data: { message: 'started' } }
    }
    return { data: {} }
  }
  const client = { get, post, put: get, patch: get, delete: get }
  return { state, client }
})

vi.mock('../../src/api', () => ({ default: backend.client }))
vi.mock('axios', () => {
  const instance = {
    ...backend.client,
    defaults: { headers: { common: {} } },
    interceptors: { request: { use() {} }, response: { use() {} } },
  }
  instance.create = () => instance
  return { default: instance }
})
vi.mock('../../src/utils/websocket', () => ({
  useWebSocket: () => ({ connect() {}, on() {}, off() {} }),
}))

import AgentDetail from '../../src/views/AgentDetail.vue'
import AgentSettingsHome from '../../src/components/agent-settings/AgentSettingsHome.vue'
import AgentHeader from '../../src/components/AgentHeader.vue'
import { useAgentRestartStore } from '../../src/stores/agentRestart'

const Blank = { render: () => h('div') }
let wrapper
let restart

async function open(status) {
  backend.state.agent = { name: 'alpha', status, can_share: true, can_delete: true }
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'Dashboard', component: Blank },
      { path: '/agents/:name', name: 'AgentDetail', component: Blank },
    ],
  })
  await router.push('/agents/alpha?tab=settings')
  const pinia = createPinia()
  setActivePinia(pinia)
  restart = useAgentRestartStore()
  wrapper = shallowMount(AgentDetail, { global: { plugins: [router, pinia] } })
  await flushPromises()
  // The page's own reports have settled; now two rows say they wait.
  restart.report('alpha', 'guardrails', 'Guardrail rules', true)
  restart.report('alpha', 'github-token', 'GitHub token', true)
}
const labels = () => restart.pendingFor('alpha').map((p) => p.label)
const pressRestart = async () => {
  wrapper.findComponent(AgentSettingsHome).vm.$emit('restart')
  await flushPromises()
}

beforeEach(() => {
  localStorage.clear()
  backend.state.failStart = false
})
afterEach(() => wrapper?.unmount())

describe('the Settings banner restart', () => {
  it('keeps every waiting change when the start fails', async () => {
    await open('stopped')
    backend.state.failStart = true
    await pressRestart()
    expect(labels()).toEqual(['Guardrail rules', 'GitHub token'])
    expect(restart.epochFor('alpha')).toBe(0)
    expect(wrapper.text()).not.toContain('The waiting changes now apply')
  })

  it('clears the list and moves the epoch once the agent is running again', async () => {
    await open('stopped')
    await pressRestart()
    expect(labels()).toEqual([])
    expect(restart.epochFor('alpha')).toBe(1)
  })

  it('a manual start from the header settles the list too', async () => {
    await open('stopped')
    wrapper.findComponent(AgentHeader).vm.$emit('toggle')
    await flushPromises()
    expect(backend.state.agent.status).toBe('running')
    expect(labels()).toEqual([])
    expect(restart.epochFor('alpha')).toBe(1)
  })
})
