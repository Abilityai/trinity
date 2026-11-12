// @vitest-environment jsdom
/**
 * trinity-enterprise#1240 — a task links to the GitHub issue or PR where its
 * work happens, mounted.
 *
 *   - each ref is `owner/repo#N`, an anchor only to an https:// url, opened
 *     with rel="noopener noreferrer";
 *   - a ref Trinity couldn't read says so, in the warning tone, never "closed";
 *   - a contributor adds a ref by typing it, and the server's refusal is shown
 *     inline; a remove control drops one;
 *   - a viewer reads only;
 *   - the task list shows a ref count only when there is one.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import { useProjectsStore } from '@/stores/projects'
import ProjectTaskRefs from '@/components/portal/projects/ProjectTaskRefs.vue'
import ProjectTasks from '@/components/portal/projects/ProjectTasks.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const READ_AT = new Date(Date.now() - 4 * 60 * 1000).toISOString()
const OPEN = { id: 'ref_1', provider: 'github', repo: 'acme/web', number: 42, kind: 'pull',
  url: 'https://github.com/acme/web/pull/42', title: 'Ship the brief <b>now</b>', state: 'open', state_reason: null,
  read_at: READ_AT, stale: false, readable: true, error: null }
const UNREADABLE = { ...OPEN, id: 'ref_2', number: 7, kind: 'issue', url: 'https://github.com/acme/web/issues/7',
  title: 'Old title', state: null, readable: false, error: 'unavailable' }
const BAD_URL = { ...OPEN, id: 'ref_3', number: 9, url: 'javascript:alert(1)' }

const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
let store

beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store = useProjectsStore()
})

function refs(props = {}) {
  return mount(ProjectTaskRefs, {
    props: { projectId: 'prj_1', taskId: 'T-001', refs: [OPEN], canEdit: true, ...props },
    attachTo: document.body,
  })
}

async function type(id, value) {
  const el = document.getElementById(id)
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

describe('ent#1240 — a ref row', () => {
  it('renders owner/repo#N as an https anchor opened with noopener, and the title as text', () => {
    refs()
    const a = byTestId('task-ref-link-ref_1')
    expect(a.tagName).toBe('A')
    expect(a.getAttribute('href')).toBe('https://github.com/acme/web/pull/42')
    expect(a.getAttribute('target')).toBe('_blank')
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
    expect(a.textContent.trim()).toBe('acme/web#42')
    const row = byTestId('task-ref-ref_1')
    expect(row.textContent).toContain('Ship the brief <b>now</b>')
    expect(row.querySelector('b')).toBeNull()
    expect(row.textContent).toContain('Open')
    expect(row.textContent).toMatch(/read .*ago/)
  })

  it('a non-https url renders no anchor', () => {
    refs({ refs: [BAD_URL] })
    const row = byTestId('task-ref-ref_3')
    expect(row.querySelector('a')).toBeNull()
    expect(row.textContent).toContain('acme/web#9')
  })

  it("an unreadable ref says couldn't read and last read, never closed", () => {
    refs({ refs: [UNREADABLE] })
    const row = byTestId('task-ref-ref_2')
    expect(row.textContent).toContain("Couldn't read")
    expect(row.textContent).not.toMatch(/closed/i)
    expect(row.textContent).toMatch(/last read .*ago/)
  })

  it('with no refs, says what a ref is', () => {
    refs({ refs: [] })
    expect(byTestId('task-refs-empty').textContent).toMatch(/evidence/i)
  })
})

describe('ent#1240 — adding and removing', () => {
  it('the add field sends the typed text to addTaskRefs and emits the new list', async () => {
    store.addTaskRefs = vi.fn().mockResolvedValue({ refs: [OPEN, UNREADABLE] })
    const w = refs()
    await type('task-ref-input', '  acme/web#7 ')
    byTestId('task-ref-add').click()
    await flushPromises()
    expect(store.addTaskRefs).toHaveBeenCalledWith('prj_1', 'T-001', ['acme/web#7'])
    expect(w.emitted('changed')[0][0]).toEqual([OPEN, UNREADABLE])
    expect(document.getElementById('task-ref-input').value).toBe('')
  })

  it("shows the server's refusal inline and keeps what was typed", async () => {
    const err = { response: { status: 422, data: { detail: { code: 'invalid_ref', message: 'Use owner/repo#N or a GitHub issue or PR link.' } } } }
    store.addTaskRefs = vi.fn().mockRejectedValue(err)
    refs()
    await type('task-ref-input', 'not a ref')
    byTestId('task-ref-add').click()
    await flushPromises()
    expect(document.body.textContent).toContain('Use owner/repo#N or a GitHub issue or PR link.')
    expect(document.getElementById('task-ref-input').value).toBe('not a ref')
  })

  it('the remove control calls removeTaskRef', async () => {
    store.removeTaskRef = vi.fn().mockResolvedValue(undefined)
    const w = refs({ refs: [OPEN, UNREADABLE] })
    byTestId('task-ref-remove-ref_2').click()
    await flushPromises()
    expect(store.removeTaskRef).toHaveBeenCalledWith('prj_1', 'T-001', 'ref_2')
    expect(w.emitted('changed')[0][0]).toEqual([OPEN])
  })

  it('a non-contributor sees no add field and no remove control', () => {
    refs({ canEdit: false, refs: [OPEN] })
    expect(document.getElementById('task-ref-input')).toBeNull()
    expect(byTestId('task-ref-add')).toBeNull()
    expect(byTestId('task-ref-remove-ref_1')).toBeNull()
    expect(byTestId('task-ref-link-ref_1')).not.toBeNull()
  })
})

describe('ent#1240 — evidence beside the done claim', () => {
  it('a task awaiting verification lists its evidence', () => {
    const merged = { ...OPEN, state: 'merged' }
    refs({ refs: [merged], evidence: [merged], verifying: true })
    const ev = byTestId('task-ref-evidence')
    expect(ev.textContent).toContain('acme/web#42')
    expect(ev.textContent).toContain('Merged')
  })
})

describe('ent#1240 — the task list', () => {
  it('shows a ref-count glyph only on a task with refs', async () => {
    store.fetchTasks = vi.fn().mockResolvedValue(undefined)
    store.tasks = { prj_1: { loaded: true, error: null, rows: [
      { id: 'T-001', title: 'With refs', status: 'active', priority: 'p2', refs: [OPEN, UNREADABLE] },
      { id: 'T-002', title: 'Without', status: 'active', priority: 'p2', refs: [] },
    ] } }
    mount(ProjectTasks, { props: { projectId: 'prj_1', canContribute: true }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-task-refs-T-001').textContent).toContain('2')
    expect(byTestId('project-task-refs-T-002')).toBeNull()
  })
})
