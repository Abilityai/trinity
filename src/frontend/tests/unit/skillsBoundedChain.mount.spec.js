// @vitest-environment jsdom
/**
 * trinity#3448 — the Skills surfaces fill the window: the pages' half of the chain.
 *
 * A BoundedRegion only bounds a list when EVERY element from the page root down
 * to it is a flex column growing from a 0px basis. One block wrapper, or one
 * `flex-1`, and the list regrows the page (measured at 30,306 px for 300 rows,
 * #3451). jsdom cannot measure that, so this pins the chain itself on the real
 * pages, and the geometry is the eyeball. It walks the DOM from the mounted
 * panel up to the root, so a wrapper added later fails here by name. The tabs
 * that are not bounded keep the page they had.
 *
 * Shallow-mounted over a real vue-router (`agentDetailTabUrl.mount.spec.js`).
 * Inside the panels: `skillsTab.mount` and `librarySkillsFilter.mount`.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'

const backend = vi.hoisted(() => {
  // uPlot (pulled in by a chart panel) probes this at import time.
  window.matchMedia = () => ({
    matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  })
  const request = async (url) => {
    if (/^\/api\/agents\/[^/]+$/.test(String(url))) return { data: { name: 'alpha', status: 'stopped', can_share: true } }
    if (url === '/api/templates') return { data: [] }
    return { data: {} }
  }
  return { client: { get: request, post: request, put: request, patch: request, delete: request } }
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
import Library from '../../src/views/Library.vue'

const Blank = { render: () => h('div') }

let wrapper
afterEach(() => wrapper?.unmount())

async function open(page, start) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'Dashboard', component: Blank },
      { path: '/agents/:name', name: 'AgentDetail', component: Blank },
      { path: '/library', name: 'Library', component: Blank },
    ],
  })
  await router.push('/')
  await router.push(start)
  const pinia = createPinia()
  setActivePinia(pinia)
  wrapper = shallowMount(page, { global: { plugins: [router, pinia] } })
  await flushPromises()
}

// Every element strictly between `from` and the page root, nearest first.
function links(from) {
  const out = []
  for (let el = from.parentElement; el && el !== wrapper.element; el = el.parentElement) out.push(el)
  return out
}

function expectChain(panel) {
  const chain = links(panel)
  expect(chain.length).toBeGreaterThan(2)
  for (const el of chain) {
    expect([...el.classList], el.outerHTML.slice(0, 120)).toEqual(expect.arrayContaining(['flex', 'flex-col', 'grow', 'basis-0']))
    expect([...el.classList]).not.toContain('flex-1')
  }
  expect([...wrapper.element.classList]).toEqual(expect.arrayContaining(['min-h-dvh', 'flex', 'flex-col']))
  expect([...wrapper.element.classList]).not.toContain('min-h-screen')
  // `mx-auto` suppresses the stretch in a column flexbox unless `w-full` restores it.
  expect([...wrapper.find('main').element.classList]).toContain('w-full')
}

describe('AgentDetail', () => {
  it('the Skills tab is a link-by-link chain from the root to the panel', async () => {
    await open(AgentDetail, '/agents/alpha?tab=skills')
    expectChain(wrapper.find('skills-tab-stub').element)
  })

  it('the other tabs keep the page they had', async () => {
    await open(AgentDetail, '/agents/alpha?tab=git')
    expect(wrapper.classes()).toContain('min-h-screen')
    expect(wrapper.classes()).not.toContain('min-h-dvh')
    expect(wrapper.find('main').classes()).toContain('overflow-visible')
  })

  it('Tasks keeps its fullscreen page', async () => {
    await open(AgentDetail, '/agents/alpha?tab=tasks')
    expect(wrapper.classes()).toContain('h-screen')
    expect(wrapper.classes()).not.toContain('min-h-dvh')
  })
})

describe('Library', () => {
  it('the Skills tab is a link-by-link chain from the root to the panel', async () => {
    await open(Library, '/library?tab=skills')
    expectChain(wrapper.find('library-skills-section-stub').element)
  })

  it('Agent Templates keeps the page it had', async () => {
    await open(Library, '/library?tab=templates')
    expect(wrapper.classes()).toContain('min-h-screen')
    expect(wrapper.classes()).not.toContain('min-h-dvh')
    expect(wrapper.find('main').classes()).not.toContain('grow')
  })
})
