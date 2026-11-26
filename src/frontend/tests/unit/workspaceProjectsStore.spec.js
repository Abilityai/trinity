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

// trinity-enterprise#1155 — the person fields' scoped reads, through the real store.
describe('the people reads', () => {
  it('a project people search asks the project, with the query and the purpose, and unwraps people', async () => {
    useClientPortalStore()
    const ROWS = [{ email: 'bob@example.com', label: 'Bob Baker' }]
    axios.get.mockResolvedValueOnce({ data: { people: ROWS } })
    const rows = await useProjectsStore().fetchProjectPeople('prj 1', 'bo', 'owner')
    expect(rows).toEqual(ROWS)
    expect(axios.get).toHaveBeenCalledTimes(1)
    const [path, opts] = axios.get.mock.calls[0]
    expect(path).toBe('/api/enterprise/projects/prj%201/people')
    expect(opts.params).toEqual({ q: 'bo', purpose: 'owner' })
  })

  it('a project people search with no people field reads as nobody', async () => {
    axios.get.mockResolvedValueOnce({ data: {} })
    expect(await useProjectsStore().fetchProjectPeople('prj_1', 'x', 'member')).toEqual([])
  })

  it('the company people search asks the collection, with only the query, and unwraps people', async () => {
    const ROWS = [{ email: 'carol@example.com', label: 'Carol Chen' }]
    axios.get.mockResolvedValueOnce({ data: { people: ROWS } })
    const rows = await useProjectsStore().fetchCompanyPeople('ca')
    expect(rows).toEqual(ROWS)
    const [path, opts] = axios.get.mock.calls[0]
    expect(path).toBe('/api/enterprise/projects/people')
    expect(opts.params).toEqual({ q: 'ca' })
    axios.get.mockResolvedValueOnce({ data: {} })
    expect(await useProjectsStore().fetchCompanyPeople('zz')).toEqual([])
  })
})

describe('ent#1235 — lifecycle, mode and runs waiting', () => {
  it('Start, Stop and the mode go to their routes, then the page is re-read', async () => {
    const projects = useProjectsStore()
    axios.post.mockResolvedValue({ data: {} })
    axios.patch.mockResolvedValue({ data: {} })
    axios.get.mockResolvedValue({ data: { id: 'prj_1', projects: [] } })
    await projects.startProject('prj_1')
    await projects.stopProject('prj_1')
    await projects.setMode('prj_1', 'propose')
    const posts = axios.post.mock.calls.map((c) => c[0])
    expect(posts).toContain('/api/enterprise/projects/prj_1/start')
    expect(posts).toContain('/api/enterprise/projects/prj_1/stop')
    expect(axios.patch).toHaveBeenCalledWith('/api/enterprise/projects/prj_1', { mode: 'propose' }, expect.anything())
    expect(axios.get.mock.calls.some((c) => c[0] === '/api/enterprise/projects/prj_1')).toBe(true)
  })

  it('reads the runs waiting, and sends one batch decision with its note', async () => {
    const projects = useProjectsStore()
    axios.get.mockResolvedValueOnce({ data: { runs: [{ id: 'R-1' }] } })
    expect(await projects.fetchRunsWaiting('prj_1')).toEqual([{ id: 'R-1' }])
    expect(axios.get.mock.calls[0][0]).toBe('/api/enterprise/projects/prj_1/runs/waiting')
    axios.get.mockResolvedValue({ data: { tasks: [] } })
    axios.post.mockResolvedValueOnce({ data: { results: [{ id: 'R-1', outcome: 'declined' }] } })
    const out = await projects.decideRuns('prj_1', 'decline', ['R-1'], 'Not now')
    expect(out).toEqual([{ id: 'R-1', outcome: 'declined' }])
    expect(axios.post).toHaveBeenCalledWith('/api/enterprise/projects/prj_1/runs/decline', { ids: ['R-1'], note: 'Not now' }, expect.anything())
    axios.post.mockResolvedValueOnce({ data: { results: [] } })
    await projects.decideRuns('prj_1', 'approve', ['R-2'])
    expect(axios.post).toHaveBeenLastCalledWith('/api/enterprise/projects/prj_1/runs/approve', { ids: ['R-2'] }, expect.anything())
  })
})

// --- trinity-enterprise#957: the tasks are polled, and a board move is one write ----

describe('ent#957 — task reads and writes', () => {
  const later = () => {
    let resolve, reject
    const promise = new Promise((res, rej) => { resolve = res; reject = rej })
    return { promise, resolve, reject }
  }
  const row = (id, status) => ({ id, title: id, status })

  it('only the newest read of a project writes — an older answer landing late is dropped', async () => {
    const store = useProjectsStore()
    const slow = later()
    const fast = later()
    axios.get.mockReturnValueOnce(slow.promise).mockReturnValueOnce(fast.promise)
    const first = store.fetchTasks('prj_1')
    const second = store.fetchTasks('prj_1')
    fast.resolve({ data: { tasks: [row('T-1', 'done')] } })
    await second
    slow.resolve({ data: { tasks: [row('T-1', 'active')] } })
    await first
    expect(store.tasks.prj_1.rows).toEqual([row('T-1', 'done')])
  })

  it('an older read FAILING late does not put an error over fresh rows', async () => {
    const store = useProjectsStore()
    const slow = later()
    axios.get.mockReturnValueOnce(slow.promise).mockResolvedValueOnce({ data: { tasks: [row('T-1', 'done')] } })
    const first = store.fetchTasks('prj_1')
    await store.fetchTasks('prj_1')
    slow.reject({ response: { status: 500, data: {} } })
    await first
    expect(store.tasks.prj_1).toMatchObject({ rows: [row('T-1', 'done')], error: null })
  })

  it('reads of two projects do not cancel each other', async () => {
    const store = useProjectsStore()
    const a = later()
    axios.get.mockReturnValueOnce(a.promise).mockResolvedValueOnce({ data: { tasks: [row('T-9', 'active')] } })
    const first = store.fetchTasks('prj_a')
    await store.fetchTasks('prj_b')
    a.resolve({ data: { tasks: [row('T-1', 'active')] } })
    await first
    expect(store.tasks.prj_a.rows).toEqual([row('T-1', 'active')])
    expect(store.tasks.prj_b.rows).toEqual([row('T-9', 'active')])
  })

  it('a failed read keeps the rows and records the status a retry cannot change', async () => {
    const store = useProjectsStore()
    axios.get.mockResolvedValueOnce({ data: { tasks: [row('T-1', 'active')] } })
    await store.fetchTasks('prj_1')
    axios.get.mockRejectedValueOnce({ response: { status: 404, data: { detail: { code: 'project_not_found' } } } })
    await store.fetchTasks('prj_1')
    expect(store.tasks.prj_1).toMatchObject({ rows: [row('T-1', 'active')], loaded: true, errorStatus: 404 })
    expect(store.tasks.prj_1.error).toBeTruthy()
  })

  it('a board move sends the status only, and the answer is the row at once — even when the re-read fails', async () => {
    const store = useProjectsStore()
    axios.get.mockResolvedValueOnce({ data: { tasks: [row('T-1', 'active'), row('T-2', 'active')] } })
    await store.fetchTasks('prj_1')
    axios.patch.mockResolvedValueOnce({ data: row('T-1', 'pending-verification') })
    axios.get.mockRejectedValue({ response: { status: 502, data: {} } })
    await store.moveTask('prj_1', 'T-1', 'pending-verification')
    expect(axios.patch.mock.calls[0][0]).toContain('/prj_1/tasks/T-1')
    expect(axios.patch.mock.calls[0][1]).toEqual({ status: 'pending-verification' })
    expect(store.tasks.prj_1.rows.map((r) => r.status)).toEqual(['pending-verification', 'active'])
    await new Promise((r) => setTimeout(r, 0))
    expect(store.tasks.prj_1.rows.map((r) => r.status)).toEqual(['pending-verification', 'active'])
    expect(store.tasks.prj_1.error).toBeTruthy()
    axios.get.mockReset()
  })

  it('a move that carries its reason sends the note beside the status', async () => {
    const store = useProjectsStore()
    axios.get.mockResolvedValue({ data: { tasks: [], entries: [] } })
    axios.patch.mockResolvedValueOnce({ data: row('T-1', 'done') })
    await store.moveTask('prj_1', 'T-1', 'done', 'Published at docs/launch.md')
    expect(axios.patch.mock.calls[0][1]).toEqual({ status: 'done', note: 'Published at docs/launch.md' })
    axios.get.mockReset()
  })

  it('a refused move throws and leaves the rows alone', async () => {
    const store = useProjectsStore()
    axios.get.mockResolvedValueOnce({ data: { tasks: [row('T-1', 'done')] } })
    await store.fetchTasks('prj_1')
    axios.patch.mockRejectedValueOnce({ response: { status: 409, data: { detail: { code: 'task_done' } } } })
    await expect(store.moveTask('prj_1', 'T-1', 'blocked')).rejects.toBeTruthy()
    expect(store.tasks.prj_1.rows).toEqual([row('T-1', 'done')])
  })
})
