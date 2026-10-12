// @vitest-environment jsdom
/**
 * #2900 — the agent detail page keeps its tab in the URL.
 *
 * The page itself, shallow-mounted over a real vue-router on memory history:
 * the tab strip's `v-model` is the click, `router.back()` is Back, and a fresh
 * mount at a URL is the reload. `agentTabRoute.mount.spec.js` covers the
 * binding exhaustively; this pins that AgentDetail is actually wired to it —
 * including the handlers that select a tab AND write another query key, and the
 * #2130 guard the issue names as an acceptance criterion.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'

// One fake backend behind both HTTP entry points the page uses. `agent` is the
// row GET /api/agents/{name} returns; `gate`, when set, holds every OTHER
// request open — the slow mount batch #2130 was about.
const backend = vi.hoisted(() => {
  // uPlot (pulled in by a chart panel) probes this at import time.
  window.matchMedia = () => ({
    matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  })
  const state = { agent: null, gate: null }
  const request = async (url) => {
    if (/^\/api\/agents\/[^/]+$/.test(String(url))) return { data: { ...state.agent } }
    if (state.gate) await state.gate
    return { data: {} }
  }
  const client = { get: request, post: request, put: request, patch: request, delete: request }
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
import OverflowTabs from '../../src/components/OverflowTabs.vue'
import OverviewPanel from '../../src/components/OverviewPanel.vue'

const Blank = { render: () => h('div') }

let wrapper
let router
beforeEach(() => {
  localStorage.clear()
  backend.state.agent = { name: 'alpha', status: 'stopped', can_share: true }
  backend.state.gate = null
})
afterEach(() => wrapper?.unmount())

async function open(start, { settle = true } = {}) {
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'Dashboard', component: Blank },
      { path: '/agents/:name', name: 'AgentDetail', component: Blank },
    ],
  })
  await router.push('/')
  await router.push(start)
  const pinia = createPinia()
  setActivePinia(pinia)
  wrapper = shallowMount(AgentDetail, { global: { plugins: [router, pinia] } })
  if (settle) await flushPromises()
}

const strip = () => wrapper.findComponent(OverflowTabs)
const active = () => strip().props('modelValue')
const url = () => router.currentRoute.value.fullPath
const query = () => router.currentRoute.value.query
const clickTab = async (id) => { strip().vm.$emit('update:modelValue', id); await flushPromises() }
const back = async () => { router.back(); await flushPromises() }

describe('#2900 AgentDetail keeps its tab in the URL', () => {
  it('AC1 — clicking a tab updates the URL', async () => {
    await open('/agents/alpha')
    expect(active()).toBe('overview')
    await clickTab('chat')
    expect(active()).toBe('chat')
    expect(url()).toBe('/agents/alpha?tab=chat')
  })

  it('AC2 — Back returns to the previous tab, not off the page', async () => {
    await open('/agents/alpha')
    await clickTab('tasks')
    await clickTab('git')

    await back()
    expect(url()).toBe('/agents/alpha?tab=tasks')
    expect(active()).toBe('tasks')

    await back()
    expect(url()).toBe('/agents/alpha')
    expect(active()).toBe('overview')
  })

  it('AC3/AC4 — the URL a click produced reopens that tab on a fresh load', async () => {
    await open('/agents/alpha')
    await clickTab('schedules')
    const reloadTo = url()
    wrapper.unmount()

    await open(reloadTo)
    expect(active()).toBe('schedules')
  })

  it('opening a task from Overview is ONE navigation carrying tab and execution', async () => {
    await open('/agents/alpha')
    wrapper.findComponent(OverviewPanel).vm.$emit('open-task', 'exec-7')
    await flushPromises()
    expect(active()).toBe('tasks')
    expect(query()).toEqual({ tab: 'tasks', execution: 'exec-7' })
    await back()
    expect(url()).toBe('/agents/alpha')
    expect(active()).toBe('overview')
  })

  it('an Overview shortcut to another tab is a history step too', async () => {
    await open('/agents/alpha')
    wrapper.findComponent(OverviewPanel).vm.$emit('navigate-tab', 'schedules')
    await flushPromises()
    expect(url()).toBe('/agents/alpha?tab=schedules')
    await back()
    expect(active()).toBe('overview')
  })
})

describe('AC5 — the #2130 tab-restore guard still holds', () => {
  // A deep link to a tab this viewer cannot see is dropped once the agent has
  // loaded. That reconcile runs AFTER the slow mount batch.
  function holdMountBatch() {
    let release
    backend.state.gate = new Promise((resolve) => { release = resolve })
    return async () => { release(); await flushPromises() }
  }

  it('the late reconcile does not move a user who clicked in the meantime', async () => {
    backend.state.agent.can_share = false
    const release = holdMountBatch()
    await open('/agents/alpha?tab=sharing')
    expect(strip().exists()).toBe(true)   // agent loaded; the batch is still out

    await clickTab('reports')
    expect(url()).toBe('/agents/alpha?tab=reports')

    await release()
    expect(active()).toBe('reports')
    expect(url()).toBe('/agents/alpha?tab=reports')
  })

  it('left alone, the fallback to Overview replaces the dead link instead of stacking on it', async () => {
    backend.state.agent.can_share = false
    await open('/agents/alpha?tab=sharing')
    expect(active()).toBe('overview')
    expect(url()).toBe('/agents/alpha?tab=overview')
    // Back leaves the page: there is no ?tab=sharing entry to bounce through.
    await back()
    expect(url()).toBe('/')
  })

  it('a visible deep link is left exactly as it was', async () => {
    await open('/agents/alpha?tab=sharing')
    expect(active()).toBe('sharing')
    expect(url()).toBe('/agents/alpha?tab=sharing')
  })
})
