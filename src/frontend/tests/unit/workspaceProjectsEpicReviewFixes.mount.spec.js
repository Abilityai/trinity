// @vitest-environment jsdom
/**
 * Regression tests for the code-review fixes on the combined Projects epic PR
 * (abilityai/trinity#4395): stale reads never land under another brief, a
 * refused mark shows the server's state again, an unmarked brief is never
 * described as read by agents, the file mark is offered only where the server
 * allows it, and a steward-options reply for an earlier opening is dropped.
 * Every race test resolves the OLDER read last.
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
import ProjectContext from '@/components/portal/projects/ProjectContext.vue'
import ProjectMaterial from '@/components/portal/projects/ProjectMaterial.vue'
import ProjectCreateModal from '@/components/portal/projects/ProjectCreateModal.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const EVA = 'eva@example.com'
const BOB = 'bob@example.com'
const brief = (id, over = {}) => ({ id, kind: 'project-file', task_id: null, filename: `${id}.md`, mime: 'text/markdown',
  size_bytes: 10, for_agents: false, created_by: BOB, created_at: '2026-11-09T10:00:00Z', updated_at: '2026-11-09T10:00:00Z', ...over })
const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)

function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store = useProjectsStore()
})

const ctx = (items, props = {}) => mount(ProjectContext, {
  props: { projectId: 'prj_1', items, canContribute: true, myEmail: BOB, ...props }, attachTo: document.body,
})

describe('briefs — stale reads are dropped', () => {
  it('editing A then B: A’s late reply never fills B’s editor', async () => {
    const reads = { ctx_a: deferred(), ctx_b: deferred() }
    store.readContext = vi.fn((_p, id) => reads[id].promise)
    const w = ctx([brief('ctx_a'), brief('ctx_b')])
    const edits = w.findAll('button').filter((b) => b.text() === 'Edit')
    await edits[0].trigger('click')
    await edits[1].trigger('click')
    reads.ctx_b.resolve({ content: 'B text' })
    await flushPromises()
    reads.ctx_a.resolve({ content: 'A text' })                  // the older read lands last
    await flushPromises()
    expect(document.getElementById('context-content').value).toBe('B text')
    expect(document.getElementById('context-filename').value).toBe('ctx_b.md')
  })

  it('text typed before the read lands is kept', async () => {
    const read = deferred()
    store.readContext = vi.fn(() => read.promise)
    const w = ctx([brief('ctx_a')])
    await w.findAll('button').find((b) => b.text() === 'Edit').trigger('click')
    const area = document.getElementById('context-content')
    area.value = 'my own words'
    area.dispatchEvent(new Event('input'))
    read.resolve({ content: 'server text' })
    await flushPromises()
    expect(document.getElementById('context-content').value).toBe('my own words')
  })

  it('previewing A then B: A’s late reply never shows under B', async () => {
    const reads = { ctx_a: deferred(), ctx_b: deferred() }
    store.readContext = vi.fn((_p, id) => reads[id].promise)
    const w = ctx([brief('ctx_a', { for_agents: true }), brief('ctx_b', { for_agents: true })])
    const opens = w.findAll('button').filter((b) => b.text().includes('.md'))
    await opens[0].trigger('click')
    await opens[1].trigger('click')
    reads.ctx_b.resolve({ content: 'B text' })
    await flushPromises()
    reads.ctx_a.resolve({ content: 'A text' })
    await flushPromises()
    expect(byTestId('context-preview-text').textContent).toBe('B text')
  })
})

describe('briefs — the mark and the preview tell the truth', () => {
  it('a refused mark puts the box back', async () => {
    store.updateContext = vi.fn().mockRejectedValue({ response: { status: 403, data: { detail: { code: 'not_permitted', message: 'No.' } } } })
    ctx([brief('ctx_a')])
    const box = byTestId('context-mark-ctx_a')
    box.checked = true
    box.dispatchEvent(new Event('change'))
    await flushPromises()
    expect(byTestId('context-mark-ctx_a').checked).toBe(false)
  })

  it('an unmarked long brief is not described as something an agent reads', async () => {
    store.readContext = vi.fn().mockResolvedValue({ content: 'x'.repeat(300 * 1024) })
    const w = ctx([brief('ctx_a', { for_agents: false })], { limits: { file_bytes: 524288, agent_read_bytes: 204800 } })
    await w.findAll('button').find((b) => b.text().includes('.md')).trigger('click')
    await flushPromises()
    expect(byTestId('context-preview-unmarked')).not.toBeNull()
    expect(byTestId('context-preview-truncated')).toBeNull()
  })

  it('the preview cuts at the server’s budget, not a copied constant', async () => {
    store.readContext = vi.fn().mockResolvedValue({ content: 'y'.repeat(2048) })
    const w = ctx([brief('ctx_a', { for_agents: true })], { limits: { file_bytes: 524288, agent_read_bytes: 1024 } })
    await w.findAll('button').find((b) => b.text().includes('.md')).trigger('click')
    await flushPromises()
    expect(byTestId('context-preview-truncated')).not.toBeNull()
    expect(byTestId('context-preview-text').textContent.length).toBe(1024)
  })

  it('a brief whose author email differs only in case is still mine to edit', () => {
    ctx([brief('ctx_a', { created_by: 'Bob@Example.com' })])
    expect(byTestId('context-mark-ctx_a')).not.toBeNull()
  })
})

const file = (id, over = {}) => ({ id, agent_name: 'scout', filename: `${id}.txt`, size_bytes: 5, mime_type: 'text/plain',
  created_at: '2026-11-09T10:00:00Z', expires_at: '2099-01-01T00:00:00Z', audience: 'members', linked_by: BOB,
  for_agents: false, download_url: '#', ...over })
const material = (files, me = BOB, role = 'member') => mount(ProjectMaterial, {
  props: { myEmail: me, project: { id: 'prj_1', files, reports: [], decisions: [], context: [], my_role: role,
    can: { contribute: true, manage_members: role === 'creator' }, archived_at: null } },
  attachTo: document.body,
})

describe('linked files — the mark is offered only where the server allows it', () => {
  it('not on a binary file', () => {
    material([file('f_pdf', { mime_type: 'application/pdf', filename: 'deck.pdf' })])
    expect(byTestId('file-mark-f_pdf')).toBeNull()
  })

  it('not to a member who did not link it; yes to the linker and to the creator', () => {
    material([file('f1', { linked_by: EVA })])
    expect(byTestId('file-mark-f1')).toBeNull()
    document.body.innerHTML = ''
    material([file('f1', { linked_by: BOB })])
    expect(byTestId('file-mark-f1')).not.toBeNull()
    document.body.innerHTML = ''
    material([file('f1', { linked_by: BOB })], EVA, 'creator')
    expect(byTestId('file-mark-f1')).not.toBeNull()
  })

  it('a refused mark puts the box back', async () => {
    store.markItemForAgents = vi.fn().mockRejectedValue({ response: { status: 415, data: { detail: { code: 'not_text' } } } })
    material([file('f1')])
    const box = byTestId('file-mark-f1')
    box.checked = true
    box.dispatchEvent(new Event('change'))
    await flushPromises()
    expect(byTestId('file-mark-f1').checked).toBe(false)
  })
})

describe('create dialog — a reply for an earlier opening is dropped', () => {
  it('reopen quickly: the first opening’s late failure does not blank the options', async () => {
    const first = deferred()
    const second = deferred()
    store.fetchStewardOptions = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const w = mount(ProjectCreateModal, { props: { modelValue: true, myEmail: EVA, agents: ['pm-steward'] }, attachTo: document.body })
    await flushPromises()
    await w.setProps({ modelValue: false })
    await w.setProps({ modelValue: true })
    second.resolve({ set: { available: true }, agents: [{ agent_name: 'pm-steward', holds_set: false, can_assign: true }] })
    await flushPromises()
    first.reject(new Error('late'))                               // the older opening fails last
    await flushPromises()
    const el = document.getElementById('project-steward')
    el.value = 'agent:pm-steward'
    el.dispatchEvent(new Event('change'))
    await flushPromises()
    expect(byTestId('project-steward-skills-hint').textContent).toContain('pm-steward gets the project-management skills')
  })
})
