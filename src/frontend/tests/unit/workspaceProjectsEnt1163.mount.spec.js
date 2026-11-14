// @vitest-environment jsdom
/**
 * trinity-enterprise#1163 — import a GitHub epic as a project, mounted.
 *
 *   - the import dialog has one source switch (a radio group): "From an agent"
 *     (unchanged, the default) and "From a GitHub epic";
 *   - the epic path pre-validates the field with a named error and an example,
 *     and sends nothing until it parses;
 *   - a clean import calls store.importEpic and opens the project;
 *   - an import that left something out names it before the project opens;
 *   - the server's refusal (409, 422, 503) is shown inline in its own words;
 *   - store.importEpic posts { epic } to /import/epic.
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
import { portalHttp } from '@/stores/clientPortal'
import ProjectImportModal from '@/components/portal/projects/ProjectImportModal.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const q = (sel) => document.querySelector(sel)
const CLEAN = { id: 'prj_1', name: 'Ship it', tasks: 3, unlinked: [], skipped: [], truncated: false, nested: [] }
let store

beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store = useProjectsStore()
})

async function open(props = {}) {
  const w = mount(ProjectImportModal, { props: { modelValue: false, agents: ['a-alice', 'a-bob'], ...props }, attachTo: document.body })
  await w.setProps({ modelValue: true })
  await flushPromises()
  return w
}

async function toEpic() {
  q('[data-source="epic"]').click()
  await flushPromises()
}

async function typeEpic(value) {
  const el = document.getElementById('project-import-epic-ref')
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

async function submit() {
  q('[data-testid="project-import-epic"]').dispatchEvent(new Event('submit', { cancelable: true }))
  await flushPromises()
}

describe('the source switch', () => {
  it('offers both sources, the agent one first and chosen, and swaps the body', async () => {
    await open()
    const radios = [...document.querySelectorAll('[data-testid="project-import-source"] [role="radio"]')]
    expect(radios.map((r) => r.textContent.trim())).toEqual(['From an agent', 'From a GitHub epic'])
    expect(radios.map((r) => r.getAttribute('aria-checked'))).toEqual(['true', 'false'])
    expect(document.getElementById('project-import-agent')).not.toBeNull()
    expect(q('[data-testid="project-import-epic"]')).toBeNull()
    await toEpic()
    expect(q('[data-source="epic"]').getAttribute('aria-checked')).toBe('true')
    expect(document.getElementById('project-import-agent')).toBeNull()
    expect(q('[data-testid="project-import-epic"]').textContent).toContain('nothing is written back to GitHub')
  })

  it('moves with the arrow keys', async () => {
    await open()
    q('[data-testid="project-import-source"]').dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await flushPromises()
    expect(q('[data-source="epic"]').getAttribute('aria-checked')).toBe('true')
    expect(q('[data-source="epic"]').getAttribute('tabindex')).toBe('0')
    expect(document.activeElement).toBe(q('[data-source="epic"]'))
  })
})

describe('the epic path', () => {
  it('names a bad field with an example and sends nothing', async () => {
    const spy = vi.spyOn(store, 'importEpic')
    await open()
    await toEpic()
    await submit()
    expect(q('[data-testid="project-import-epic"]').textContent).toContain("Paste the epic's GitHub issue")
    await typeEpic('just some words')
    await submit()
    expect(q('[data-testid="project-import-epic"]').textContent).toContain("That isn't a GitHub issue")
    expect(q('[data-testid="project-import-epic"]').textContent).toContain('your-org/repo#12')
    expect(spy).not.toHaveBeenCalled()
  })

  it('a clean import calls the store and opens the project', async () => {
    const spy = vi.spyOn(store, 'importEpic').mockResolvedValue(CLEAN)
    const w = await open()
    await toEpic()
    await typeEpic('  https://github.com/acme/web/issues/12  ')
    await submit()
    expect(spy).toHaveBeenCalledWith('  https://github.com/acme/web/issues/12  ')
    expect(w.emitted('imported')).toEqual([[CLEAN]])
    expect(w.emitted('update:modelValue').at(-1)).toEqual([false])
  })

  it('names what was left out before the project opens', async () => {
    const out = { ...CLEAN, skipped: ['priv/ate#2'], truncated: true, unlinked: ['acme/web#9'], nested: ['acme/web#4'] }
    vi.spyOn(store, 'importEpic').mockResolvedValue(out)
    const w = await open()
    await toEpic()
    await typeEpic('acme/web#12')
    await submit()
    expect(w.emitted('imported')).toBeUndefined()
    const text = q('[data-testid="project-import-epic-result"]').textContent
    for (const s of ['3 tasks', "isn't started yet", 'first 100', 'priv/ate#2', 'acme/web#9', 'acme/web#4']) {
      expect(text).toContain(s)
    }
    q('[data-testid="project-import-open"]').click()
    await flushPromises()
    expect(w.emitted('imported')).toEqual([[out]])
    expect(w.emitted('update:modelValue').at(-1)).toEqual([false])
  })

  it.each([
    [409, 'already_imported', 'This epic was already imported.'],
    [422, 'epic_has_no_children', 'Nothing to import — this issue has no sub-issues Trinity can read.'],
    [503, 'github_busy', 'GitHub is limiting Trinity\'s reads right now. Try again in a few minutes.'],
  ])('shows the server refusal %s %s inline and stays open', async (status, code, message) => {
    vi.spyOn(store, 'importEpic').mockRejectedValue({ response: { status, data: { detail: { code, message } } } })
    const w = await open()
    await toEpic()
    await typeEpic('acme/web#12')
    await submit()
    expect(document.body.textContent).toContain(message)
    expect(w.emitted('imported')).toBeUndefined()
  })

  it('words the server\'s invalid_ref for an epic', async () => {
    vi.spyOn(store, 'importEpic').mockRejectedValue({ response: { status: 422, data: { detail: { code: 'invalid_ref', message: 'A ref is a GitHub issue or pull request' } } } })
    await open()
    await toEpic()
    await typeEpic('acme/web#12')
    await submit()
    expect(document.body.textContent).toContain("That isn't a GitHub issue")
    expect(document.body.textContent).not.toContain('A ref is a GitHub issue or pull request')
  })
})

describe('store.importEpic', () => {
  it('posts the trimmed epic to /import/epic and refreshes the list', async () => {
    const post = vi.spyOn(portalHttp, 'post').mockResolvedValue({ data: CLEAN })
    const get = vi.spyOn(portalHttp, 'get').mockResolvedValue({ data: { projects: [] } })
    expect(await store.importEpic('  acme/web#12 ')).toEqual(CLEAN)
    expect(post).toHaveBeenCalledTimes(1)
    const [url, body] = post.mock.calls[0]
    expect(url).toMatch(/\/projects\/import\/epic$/)
    expect(body).toEqual({ epic: 'acme/web#12' })
    await flushPromises()
    expect(get).toHaveBeenCalled()
  })
})
