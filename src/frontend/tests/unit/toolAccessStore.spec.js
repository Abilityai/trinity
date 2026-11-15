/**
 * The agents store's tool-access actions address the right routes
 * (trinity-enterprise#341) — the components that call them are mounted over a
 * mocked store, so the URLs and bodies are pinned here against axios.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const http = vi.hoisted(() => ({
  get: vi.fn(async () => ({ data: { profile: 'full' } })),
  put: vi.fn(async () => ({ data: { profile: 'strict_read_only' } })),
}))
vi.mock('axios', () => {
  const inst = {
    get: http.get, put: http.put, post: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import { useAgentsStore } from '../../src/stores/agents'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('tool-access store actions', () => {
  it('getToolAccess reads the agent route', async () => {
    expect(await useAgentsStore().getToolAccess('scout')).toEqual({ profile: 'full' })
    expect(http.get).toHaveBeenCalledWith('/api/agents/scout/tool-access', expect.anything())
  })

  it('setToolAccess sends the body as given', async () => {
    const body = { profile: 'custom', custom: { read_only: true, disallowed_tools: ['Bash'] } }
    await useAgentsStore().setToolAccess('scout', body)
    expect(http.put).toHaveBeenCalledWith('/api/agents/scout/tool-access', body, expect.anything())
  })

  it('setReadOnly is the shortcut route', async () => {
    expect(await useAgentsStore().setReadOnly('scout', false)).toEqual({ profile: 'strict_read_only' })
    expect(http.put).toHaveBeenCalledWith('/api/agents/scout/read-only', { enabled: false }, expect.anything())
  })
})
