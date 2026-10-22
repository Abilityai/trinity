// @vitest-environment jsdom
/**
 * #3626 — the Tasks form's Timeout dropdown follows the agent's execution
 * timeout. The backend clamps a task's timeout to that cap, so an option above
 * it is a promise the run will not keep: options above the cap are hidden, the
 * cap itself is offered, the cap is named next to the dropdown, and the task
 * is posted with the capped value even when the remembered choice is higher.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

// cap: a number, null (read fails), or a map agent → number | Promise (held read).
const server = vi.hoisted(() => ({ cap: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn(async (url) => {
      if (url.endsWith('/timeout')) {
        const agent = url.split('/')[3]
        const cap = server.cap !== null && typeof server.cap === 'object' ? server.cap[agent] : server.cap
        if (cap === null) return Promise.reject(new Error('down'))
        return Promise.resolve(cap).then(c => ({ data: { execution_timeout_seconds: c } }))
      }
      if (url.endsWith('/queue')) return { data: { is_busy: false, queue_length: 0 } }
      if (url.endsWith('/executions/running')) return { data: { executions: [] } }
      if (url.includes('/executions?')) return { data: [] }
      return { data: {} }
    }),
    post: vi.fn(async () => ({ data: { execution_id: 'e1', status: 'queued' } })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

import axios from 'axios'
import TasksPanel from '../../src/components/TasksPanel.vue'

let wrapper
async function mountPanel() {
  wrapper = mount(TasksPanel, {
    props: { agentName: 'fin', agentStatus: 'running' },
    global: { stubs: { ModelSelector: true, SkeletonLoader: true, LoadFailed: true, 'router-link': true } },
  })
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
  return wrapper
}
const options = (w) => w.find('[data-testid="task-timeout"]').findAll('option').map(o => Number(o.element.value))

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  axios.get.mockClear()
  axios.post.mockClear()
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

describe('task timeout follows the agent cap', () => {
  it('a 60 s cap hides every longer option, offers it, names the cap and posts 60', async () => {
    server.cap = 60
    localStorage.setItem('trinity-task-timeout-fin', '3600')
    const w = await mountPanel()
    expect(options(w)).toEqual([60])
    expect(w.find('[data-testid="task-timeout"]').element.value).toBe('60')
    expect(w.find('[data-testid="task-timeout-cap"]').text()).toContain('1 min')

    await w.find('textarea').setValue('do it')
    await w.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    const [, payload] = axios.post.mock.calls.find(([u]) => u.endsWith('/task'))
    expect(payload.timeout_seconds).toBe(60)
  })

  it('a 30 min cap keeps the shorter options and the cap', async () => {
    server.cap = 1800
    const w = await mountPanel()
    expect(options(w)).toEqual([300, 900, 1800])
  })

  it('a cap at or above every option changes nothing and shows no note', async () => {
    server.cap = 7200
    const w = await mountPanel()
    expect(options(w)).toEqual([300, 900, 1800, 3600, 7200])
    expect(w.find('[data-testid="task-timeout-cap"]').exists()).toBe(false)
  })

  it('switching agents never shows or posts the previous agent\'s cap, and restores its remembered choice', async () => {
    let release
    const held = new Promise((resolve) => { release = resolve })
    server.cap = { fin: 60, bob: held }
    localStorage.setItem('trinity-task-timeout-bob', '1800')
    const w = await mountPanel()
    expect(options(w)).toEqual([60])

    await w.setProps({ agentName: 'bob' })
    await flushPromises()
    expect(w.find('[data-testid="task-timeout-cap"]').exists()).toBe(false)
    expect(w.find('[data-testid="task-timeout"]').element.value).toBe('1800')
    await w.find('textarea').setValue('go')
    await w.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    const [, payload] = axios.post.mock.calls.find(([u]) => u === '/api/agents/bob/task')
    expect(payload.timeout_seconds).toBe(1800)

    release(7200)
    await flushPromises()
    expect(options(w)).toEqual([300, 900, 1800, 3600, 7200])
  })

  it('a late read for the agent the page left is dropped', async () => {
    let release
    const held = new Promise((resolve) => { release = resolve })
    server.cap = { fin: held, bob: 7200 }
    const w = await mountPanel()
    await w.setProps({ agentName: 'bob' })
    await flushPromises()
    release(60)
    await flushPromises()
    expect(options(w)).toEqual([300, 900, 1800, 3600, 7200])
  })

  it('an unreadable cap leaves the dropdown as it was', async () => {
    server.cap = null
    const w = await mountPanel()
    expect(options(w)).toEqual([300, 900, 1800, 3600, 7200])
  })
})
