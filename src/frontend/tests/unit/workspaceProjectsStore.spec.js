/**
 * ent#661 — the Projects capability flag and the projects store.
 *
 * - `projectsAvailable` is raised ONLY by a successful roster that says
 *   `projects_available === true` (the #2128 channel), so an older backend, an
 *   outside client and a failed read all read "not available".
 * - The list store keeps the four states honest (principle 15): loading = no
 *   verdict yet, a failed REFRESH keeps the rows and records the error, and a
 *   write refreshes what it changed.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
  globalThis.window = globalThis.window || { location: { pathname: '/workspace' } }
})

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: { Authorization: 'Bearer platform-jwt' } }),
}))

vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import axios from 'axios'
import { useClientPortalStore } from '@/stores/clientPortal'
import { useProjectsStore } from '@/stores/projects'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('roster capability', () => {
  it('is raised only by a strict true', async () => {
    const portal = useClientPortalStore()
    axios.get.mockResolvedValueOnce({ data: { agents: [], projects_available: true } })
    await portal.fetchRoster()
    expect(portal.projectsAvailable).toBe(true)
    axios.get.mockResolvedValueOnce({ data: { agents: [], projects_available: 'true' } })
    await portal.fetchRoster()
    expect(portal.projectsAvailable).toBe(false)
  })

  it('defaults closed and an older backend stays closed', async () => {
    const portal = useClientPortalStore()
    expect(portal.projectsAvailable).toBe(false)
    axios.get.mockResolvedValueOnce({ data: { agents: [] } })
    await portal.fetchRoster()
    expect(portal.projectsAvailable).toBe(false)
  })
})

describe('projects store', () => {
  const P1 = { id: 'prj_1', name: 'Q4', visibility: 'members', my_role: 'creator' }

  it('loads the list and records a verdict', async () => {
    const s = useProjectsStore()
    expect(s.listLoaded).toBe(false)
    axios.get.mockResolvedValueOnce({ data: { projects: [P1] } })
    await s.fetchList()
    expect(axios.get.mock.calls[0][0]).toBe('/api/enterprise/projects')
    expect(s.list).toEqual([P1])
    expect(s.listLoaded).toBe(true)
    expect(s.listError).toBe(null)
  })

  it('a failed refresh keeps the rows and says so', async () => {
    const s = useProjectsStore()
    axios.get.mockResolvedValueOnce({ data: { projects: [P1] } })
    await s.fetchList()
    axios.get.mockRejectedValueOnce({ response: { status: 500 } })
    await s.fetchList()
    expect(s.list).toEqual([P1])
    expect(s.listError).toMatch(/try again/i)
  })

  it('create posts the body and refreshes the list', async () => {
    const s = useProjectsStore()
    axios.post.mockResolvedValueOnce({ data: P1 })
    axios.get.mockResolvedValueOnce({ data: { projects: [P1] } })
    const out = await s.create({ name: 'Q4', goal: 'G', link_thread_id: 's1' })
    expect(out).toEqual(P1)
    expect(axios.post.mock.calls[0][0]).toBe('/api/enterprise/projects')
    expect(axios.post.mock.calls[0][1]).toMatchObject({ link_thread_id: 's1' })
    expect(s.forLink('thread', 's1')).toBe(undefined)   // invalidated, will re-read
  })

  it('caches the project a chat belongs to, and a link write invalidates it', async () => {
    const s = useProjectsStore()
    axios.get.mockResolvedValueOnce({ data: { project: P1 } })
    await s.fetchForLink('thread', 's1')
    expect(s.forLink('thread', 's1')).toEqual(P1)
    axios.delete.mockResolvedValueOnce({})
    axios.get.mockResolvedValueOnce({ data: { projects: [] } })
    await s.unlink('prj_1', 'thread', 's1')
    expect(axios.delete.mock.calls[0][0]).toBe('/api/enterprise/projects/prj_1/links/thread/s1')
    expect(s.forLink('thread', 's1')).toBe(null)
  })

  it('a project page failure is kept per id', async () => {
    const s = useProjectsStore()
    axios.get.mockRejectedValueOnce({ response: { status: 404, data: { detail: { code: 'project_not_found' } } } })
    await s.fetchProject('prj_x')
    expect(s.pageState('prj_x')).toMatchObject({ loaded: false, notFound: true })
  })
})
