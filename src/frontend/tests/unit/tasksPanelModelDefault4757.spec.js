// @vitest-environment jsdom
/**
 * #4757 — a task the operator did not pick a model for names no model, so the
 * backend resolves it: the agent's own default, else the platform default
 * (Sonnet 5.5), stepped down to a model an older base image can run. A
 * hard-coded id in the form would be posted as an explicit model and bypass
 * both. A remembered choice is still sent.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

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

async function submit(w) {
  await w.find('textarea').setValue('do it')
  await w.find('textarea').trigger('keydown', { key: 'Enter' })
  await flushPromises()
  return axios.post.mock.calls.find(([u]) => u.endsWith('/task'))[1]
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  axios.get.mockClear()
  axios.post.mockClear()
  server.cap = 7200
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

describe('task model default', () => {
  it('posts no model when none was chosen, so the backend picks it', async () => {
    const payload = await submit(await mountPanel())
    expect(payload).not.toHaveProperty('model')
  })

  it('still posts a remembered choice', async () => {
    localStorage.setItem('trinity-task-model-fin', 'claude-opus-5-5')
    const payload = await submit(await mountPanel())
    expect(payload.model).toBe('claude-opus-5-5')
  })
})
