// @vitest-environment jsdom
/**
 * trinity-enterprise#1193 (#1136 step A) — context for a project's agents, mounted.
 *
 *   - a member writes or uploads a brief (on the project or a task) with no agent;
 *   - "Agents on this project get this" is the mark, and turning it on says who
 *     else can read it;
 *   - the preview shows what one agent read returns: the first 200 KB, with the
 *     truncation named;
 *   - a viewer reads only; whoever added a brief, or the creator, changes it;
 *   - a linked file carries the same mark on the Files list.
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
import { agentPreview, AGENT_READ_BYTES, projectErrorMessage } from '@/components/portal/projects/projectsUtils'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const EVA = 'eva@example.com'     // creator
const BOB = 'bob@example.com'     // member
const BRIEF = { id: 'ctx_1', kind: 'project-file', task_id: null, filename: 'brief.md', mime: 'text/markdown',
  size_bytes: 120, for_agents: false, created_by: BOB, created_at: '2026-11-09T10:00:00Z', updated_at: '2026-11-09T10:00:00Z' }

const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
let store

beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store = useProjectsStore()
})

function ctx(props = {}) {
  return mount(ProjectContext, {
    props: { projectId: 'prj_1', items: [BRIEF], canContribute: true, myEmail: BOB, isCreator: false, ...props },
    attachTo: document.body,
  })
}

async function type(id, value) {
  const el = document.getElementById(id)
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

describe('ent#1193 — writing and uploading a brief', () => {
  it('a member writes a brief on a task, marked for agents, and is told who can read it', async () => {
    store.writeContext = vi.fn().mockResolvedValue({ data: { notice: 'Everyone who can read this project can read it.' } })
    const w = ctx({ taskId: 'T-001', items: [] })
    byTestId('context-write').click()
    await flushPromises()
    await type('context-filename', 'kickoff.md')
    await type('context-content', '# Kickoff\n\nThe transcript.')
    byTestId('context-form-mark').click()
    await flushPromises()
    expect(document.body.textContent).toContain('Everyone who can read this project can read it.')
    byTestId('context-save').click()
    await flushPromises()
    expect(store.writeContext).toHaveBeenCalledWith('prj_1', {
      filename: 'kickoff.md', content: '# Kickoff\n\nThe transcript.', for_agents: true, task_id: 'T-001' })
    expect(w.emitted('changed')).toBeTruthy()
    expect(byTestId('context-notice').textContent).toBe('Everyone who can read this project can read it.')
  })

  it('an upload opens in the editor, so it can be marked before it is saved', async () => {
    store.writeContext = vi.fn().mockResolvedValue({ data: {} })
    ctx({ taskId: 'T-002' })
    const input = byTestId('context-upload-input')
    const file = new File(['Transcript line one'], 'call.txt', { type: 'text/plain' })
    Object.defineProperty(input, 'files', { value: [file] })
    input.dispatchEvent(new Event('change'))
    await flushPromises()
    await new Promise((r) => setTimeout(r, 0))
    await flushPromises()
    expect(document.getElementById('context-filename').value).toBe('call.txt')
    expect(document.getElementById('context-content').value).toBe('Transcript line one')
    byTestId('context-form-mark').click()
    await flushPromises()
    byTestId('context-save').click()
    await flushPromises()
    expect(store.writeContext).toHaveBeenCalledWith('prj_1', {
      filename: 'call.txt', content: 'Transcript line one', for_agents: true, task_id: 'T-002' })
  })

  it('an upload that is not .md/.txt is refused before anything is read', async () => {
    ctx()
    const input = byTestId('context-upload-input')
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'deck.pdf', { type: 'application/pdf' })] })
    input.dispatchEvent(new Event('change'))
    await flushPromises()
    expect(document.body.textContent).toContain('A brief is markdown (.md) or plain text (.txt).')
    expect(document.getElementById('context-filename')).toBeNull()
  })

  it("the server's named refusal is said in plain words", async () => {
    store.writeContext = vi.fn().mockRejectedValue({ response: { status: 413, data: { detail: { code: 'context_too_large' } } } })
    ctx({ items: [] })
    byTestId('context-write').click()
    await flushPromises()
    await type('context-filename', 'big.md')
    await type('context-content', 'x')
    byTestId('context-save').click()
    await flushPromises()
    expect(document.body.textContent).toContain('A brief can be at most 512 KB. Split it into several files.')
  })
})

describe('ent#1193 — the mark, and who may change a brief', () => {
  it('the author marks it for agents in place', async () => {
    store.updateContext = vi.fn().mockResolvedValue({ data: { notice: 'Everyone who can read this project can read it.' } })
    ctx()
    byTestId('context-mark-ctx_1').click()
    await flushPromises()
    expect(store.updateContext).toHaveBeenCalledWith('prj_1', 'ctx_1', { for_agents: true })
  })

  it('the creator role may change anyone’s brief; another member may not', () => {
    ctx({ myEmail: EVA, isCreator: true })
    expect(byTestId('context-mark-ctx_1')).not.toBeNull()
    document.body.innerHTML = ''
    ctx({ myEmail: 'carol@example.com' })
    expect(byTestId('context-mark-ctx_1')).toBeNull()
    expect(byTestId('context-delete-ctx_1')).toBeNull()
  })

  it('a viewer reads only — no write, upload or mark', () => {
    ctx({ canContribute: false, myEmail: 'dave@example.com' })
    expect(byTestId('context-write')).toBeNull()
    expect(byTestId('context-upload')).toBeNull()
    expect(byTestId('context-mark-ctx_1')).toBeNull()
    expect(byTestId('context-ctx_1')).not.toBeNull()
  })

  it('an archived project offers no writes', () => {
    ctx({ archived: true })
    expect(byTestId('context-write')).toBeNull()
  })
})

describe('ent#1193 — the preview is what one agent read returns', () => {
  it('a long brief shows the first 200 KB and names the truncation', async () => {
    const long = 'é'.repeat(150 * 1024)              // 300 KB of two-byte characters
    store.readContext = vi.fn().mockResolvedValue({ ...BRIEF, for_agents: true, content: long })
    ctx({ items: [{ ...BRIEF, for_agents: true }] })
    byTestId('context-ctx_1').querySelector('button').click()
    await flushPromises()
    expect(byTestId('context-preview-truncated').textContent).toContain('first')
    const shown = byTestId('context-preview-text').textContent
    expect(new TextEncoder().encode(shown).length).toBeLessThanOrEqual(AGENT_READ_BYTES)
    expect(long.startsWith(shown)).toBe(true)
  })

  it('agentPreview never splits a character and says nothing when it fits', () => {
    expect(agentPreview('short')).toEqual({ text: 'short', truncated: false, total: 5, shown: 5 })
    const cut = agentPreview('ab' + 'é'.repeat(AGENT_READ_BYTES))
    expect(cut.truncated).toBe(true)
    expect(cut.shown % 1 === 0 && cut.text.endsWith('é')).toBe(true)
  })

  it('the error codes have plain words', () => {
    for (const code of ['context_too_large', 'project_context_full', 'unsupported_type', 'not_text', 'empty_brief']) {
      expect(projectErrorMessage({ response: { status: 422, data: { detail: { code } } } })).not.toMatch(/went wrong/)
    }
  })
})

describe('ent#1193 — a linked file carries the mark', () => {
  const PROJECT = {
    id: 'prj_1', created_by: EVA, my_role: 'member', archived_at: null, can: { contribute: true, manage_members: false },
    files: [{ id: 'f1', agent_name: 'scout', filename: 'notes.txt', size_bytes: 10, mime_type: 'text/plain',
      expires_at: '2099-01-01T00:00:00Z', audience: 'members', linked_by: BOB, for_agents: false, download_url: '#' }],
    reports: [], decisions: [], context: [BRIEF, { ...BRIEF, id: 'ctx_t', task_id: 'T-001' }],
  }

  it('a member marks a linked file for agents; the project-level briefs are listed, task briefs are not', async () => {
    store.markItemForAgents = vi.fn().mockResolvedValue({ data: {} })
    mount(ProjectMaterial, { props: { project: PROJECT, myEmail: BOB }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('context-ctx_1')).not.toBeNull()
    expect(byTestId('context-ctx_t')).toBeNull()
    byTestId('file-mark-f1').click()
    await flushPromises()
    expect(store.markItemForAgents).toHaveBeenCalledWith('prj_1', 'file', 'f1', true)
  })

  it('a guest sees no context section', async () => {
    mount(ProjectMaterial, { props: { project: PROJECT, guest: true, myEmail: 'client@outside.example' }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-context')).toBeNull()
  })
})
