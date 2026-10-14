// @vitest-environment jsdom
/**
 * #3738 — a pending attachment can be removed from the Workspace composer.
 *
 * A chip had no ×: the only way to take back a wrong file was to send it or
 * reload. Three things the issue did not name make "remove" more than a splice:
 *
 *   1. the batch loop iterates its OWN list, so splicing a queued entry out of
 *      `entries` still uploaded it — the `removed` flag is what stops it;
 *   2. the store's carry log would re-add a removed file on @mention
 *      escalation, so the discard forgets it there too;
 *   3. a file that already landed is deleted from the inbox by the name the
 *      server stored it under — best effort, never a chip error.
 *
 * Mounted (#2918) for the chip itself: the × is a real button inside a form,
 * so the test clicks it and counts submits. The room is mounted too: a removed
 * chip there is one delete per participant, which only the component wires.
 *
 * @source-text-pin: section 4 walks both composers' parsed templates to pin
 *   that EVERY chip site uses the shared chip with `@remove` on its own loop
 *   entry and a bound lock; the mounts prove one site each, not the set.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { readFileSync } from 'fs'
import path from 'path'
import { parse } from '@vue/compiler-sfc'
import { baseParse } from '@vue/compiler-core'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
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

import PortalAttachmentChip from '@/components/portal/PortalAttachmentChip.vue'
import { usePortalFileDrop, attachmentState } from '@/composables/usePortalFileDrop'
import { useClientPortalStore } from '@/stores/clientPortal'
import PortalRoom from '@/components/portal/PortalRoom.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
window.matchMedia = window.matchMedia || ((q) => ({
  matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
}))

function deferred() {
  let resolve
  let reject
  const p = new Promise((res, rej) => { resolve = res; reject = rej })
  return { p, resolve, reject }
}
const file = (name, size = 10) => new File(['x'.repeat(size)], name)
const names = (drop) => drop.entries.value.map((e) => e.name)

afterEach(() => {
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

// ---------------------------------------------------------------------------
// 1. the composable — the one home for the remove rule
// ---------------------------------------------------------------------------

describe('#3738 usePortalFileDrop.remove', () => {
  it('a sent file: drops exactly that chip, keeps the others in order, deletes its copy once', async () => {
    const discard = vi.fn(async () => {})
    const drop = usePortalFileDrop(async (f) => ({ filename: `stored_${f.name}` }), { discard })
    await drop.addFiles([file('a.pdf'), file('b.pdf'), file('c.pdf')])

    // The chip hands over the reactive proxy, not the raw entry the loop holds.
    drop.remove(drop.entries.value[1])
    expect(names(drop)).toEqual(['a.pdf', 'c.pdf'])
    await flushPromises()
    expect(discard).toHaveBeenCalledTimes(1)
    expect(discard.mock.calls[0][0].serverName).toBe('stored_b.pdf')
  })

  it('a failed file: removed, nothing to delete', async () => {
    const discard = vi.fn(async () => {})
    const drop = usePortalFileDrop(async () => ({}), { discard })
    await drop.addFiles([file('empty.txt', 0)])
    expect(attachmentState(drop.entries.value[0])).toBe('failed')

    drop.remove(drop.entries.value[0])
    await flushPromises()
    expect(drop.entries.value).toEqual([])
    expect(discard).not.toHaveBeenCalled()
  })

  it('a queued file: never uploads once removed', async () => {
    const first = deferred()
    const upload = vi.fn((f) => (f.name === 'a.pdf' ? first.p : Promise.resolve({ filename: f.name })))
    const discard = vi.fn(async () => {})
    const drop = usePortalFileDrop(upload, { discard })
    const batch = drop.addFiles([file('a.pdf'), file('b.pdf')])
    await flushPromises()
    expect(upload).toHaveBeenCalledTimes(1)          // b.pdf is waiting behind a.pdf

    drop.remove(drop.entries.value[1])
    expect(names(drop)).toEqual(['a.pdf'])
    first.resolve({ filename: 'a.pdf' })
    await batch

    expect(upload).toHaveBeenCalledTimes(1)
    expect(upload.mock.calls[0][0].name).toBe('a.pdf')
    expect(discard).not.toHaveBeenCalled()
  })

  it('an in-flight file: the chip goes now, the delete waits for the upload to land', async () => {
    const pending = deferred()
    const discard = vi.fn(async () => {})
    const drop = usePortalFileDrop(() => pending.p, { discard })
    const batch = drop.addFiles([file('a.pdf')])
    await flushPromises()

    drop.remove(drop.entries.value[0])
    expect(drop.entries.value).toEqual([])
    await flushPromises()
    expect(discard).not.toHaveBeenCalled()

    pending.resolve({ filename: 'a_1.pdf' })
    await batch
    await flushPromises()
    expect(discard).toHaveBeenCalledTimes(1)
    expect(discard.mock.calls[0][0].serverName).toBe('a_1.pdf')
  })

  it('an in-flight file whose upload then fails: nothing to delete', async () => {
    const pending = deferred()
    const discard = vi.fn(async () => {})
    const drop = usePortalFileDrop(() => pending.p, { discard })
    const batch = drop.addFiles([file('a.pdf')])
    await flushPromises()

    drop.remove(drop.entries.value[0])
    pending.reject(new Error('refused'))
    await batch
    await flushPromises()
    expect(discard).not.toHaveBeenCalled()
  })

  it('an in-flight file that partly landed (room): the receipt on the error is discarded', async () => {
    const pending = deferred()
    const discard = vi.fn(async () => {})
    const drop = usePortalFileDrop(() => pending.p, { discard })
    const batch = drop.addFiles([file('a.pdf')])
    await flushPromises()

    drop.remove(drop.entries.value[0])
    const err = new Error('ranger refused')
    err.receipt = { stored: [{ agent: 'scout', filename: 'a.pdf' }] }
    pending.reject(err)
    await batch
    await flushPromises()
    expect(discard).toHaveBeenCalledTimes(1)
    expect(discard.mock.calls[0][0].receipt).toEqual({ stored: [{ agent: 'scout', filename: 'a.pdf' }] })
  })

  it('a failing delete is a console.warn naming the file and status — never a chip error, never a throw', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const err = Object.assign(new Error('gone'), { response: { status: 404 } })
    const discard = vi.fn(async () => { throw err })
    const drop = usePortalFileDrop(async () => ({ filename: 'a.pdf' }), { discard })
    await drop.addFiles([file('a.pdf')])
    const entry = drop.entries.value[0]

    expect(() => drop.remove(entry)).not.toThrow()
    await flushPromises()
    expect(warn).toHaveBeenCalledTimes(1)
    expect(warn.mock.calls[0]).toContain('a.pdf')
    expect(warn.mock.calls[0]).toContain(404)
    expect(entry.error).toBe('')
  })

  it('a double click is one removal and one delete', async () => {
    const discard = vi.fn(async () => {})
    const drop = usePortalFileDrop(async (f) => ({ filename: f.name }), { discard })
    await drop.addFiles([file('a.pdf'), file('b.pdf')])
    const entry = drop.entries.value[0]

    drop.remove(entry)
    drop.remove(entry)
    drop.removeAt(5)
    await flushPromises()
    expect(names(drop)).toEqual(['b.pdf'])
    expect(discard).toHaveBeenCalledTimes(1)
  })

  it('two chips of one name share one inbox file: removing one deletes nothing', async () => {
    // The inbox overwrites by name, so deleting here would take the other
    // chip's file with it.
    const discard = vi.fn(async () => {})
    const drop = usePortalFileDrop(async (f) => ({ filename: f.name }), { discard })
    await drop.addFiles([file('a.pdf')])
    await drop.addFiles([file('a.pdf')])

    drop.remove(drop.entries.value[0])
    await flushPromises()
    expect(discard).not.toHaveBeenCalled()
    expect(drop.entries.value).toHaveLength(1)
    expect(attachmentState(drop.entries.value[0])).toBe('sent')
  })

  it('a failed chip of the same name never uploaded: removing the sent one still deletes it', async () => {
    // The refused chip holds no inbox copy, so it cannot be "the same file".
    const discard = vi.fn(async () => {})
    const drop = usePortalFileDrop(async (f) => ({ filename: f.name }), { discard })
    await drop.addFiles([file('a.pdf')])
    await drop.addFiles([file('a.pdf', 0)])
    expect(drop.entries.value.map(attachmentState)).toEqual(['sent', 'failed'])

    drop.remove(drop.entries.value[0])
    await flushPromises()
    expect(discard).toHaveBeenCalledTimes(1)
    expect(discard.mock.calls[0][0].serverName).toBe('a.pdf')
  })
})

// ---------------------------------------------------------------------------
// 2. the store — the carry log forgets, the inbox copy goes
// ---------------------------------------------------------------------------

describe('#3738 clientPortal.discardComposerUpload', () => {
  let store
  beforeEach(() => {
    setActivePinia(createPinia())
    store = useClientPortalStore()
  })

  it('forgets the carry entry for that file and agent, deletes by stored name, marks the rail stale', async () => {
    const a = file('a.pdf')
    const b = file('b.pdf')
    store.noteUploadForCarry('scout', a)
    store.noteUploadForCarry('ranger', a)
    store.noteUploadForCarry('scout', b)
    store.deleteUpload = vi.fn(async () => {})
    const before = store.pendingUploadNotes.scout || 0

    await store.discardComposerUpload('scout', 'a_stored.pdf', a)

    expect(store.carryableUploadsFor('scout').map((e) => e.name)).toEqual(['b.pdf'])
    expect(store.carryableUploadsFor('ranger').map((e) => e.name)).toEqual(['a.pdf'])
    expect(store.deleteUpload).toHaveBeenCalledWith('scout', 'a_stored.pdf')
    expect(store.pendingUploadNotes.scout).toBe(before + 1)
  })

  it('forgets the carry entry even when the DELETE fails, and rejects so the caller can log it', async () => {
    const a = file('a.pdf')
    store.noteUploadForCarry('scout', a)
    store.deleteUpload = vi.fn(async () => { throw new Error('429') })

    await expect(store.discardComposerUpload('scout', 'a.pdf', a)).rejects.toThrow('429')
    expect(store.carryableUploadsFor('scout')).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// 3. the chip — mounted inside a form
// ---------------------------------------------------------------------------

describe('#3738 PortalAttachmentChip ×', () => {
  async function mountComposer({ locked = false } = {}) {
    const onSubmit = vi.fn((e) => e.preventDefault())
    const drop = usePortalFileDrop(async (f) => ({ filename: f.name }), { discard: vi.fn(async () => {}) })
    await drop.addFiles([file('notes.txt'), file('report.pdf'), file('deck.key')])
    const Host = defineComponent({
      setup() {
        return () => h('form', { onSubmit }, [
          h('textarea'),
          ...drop.entries.value.map((f, i) => h(PortalAttachmentChip, {
            key: i, entry: f, locked, testid: 'portal-attachment-chip', onRemove: () => drop.remove(f),
          })),
        ])
      },
    })
    const w = mount(Host, { attachTo: document.body })
    return { w, drop, onSubmit }
  }

  it('names the file it removes', async () => {
    const { w } = await mountComposer()
    const xs = w.findAll('[data-testid="portal-attachment-chip-remove"]')
    expect(xs).toHaveLength(3)
    expect(xs[1].attributes('aria-label')).toBe('Remove report.pdf')
    expect(xs[1].element.tagName).toBe('BUTTON')
  })

  it('removes exactly that chip and never submits the form', async () => {
    const { w, drop, onSubmit } = await mountComposer()
    await w.findAll('[data-testid="portal-attachment-chip-remove"]')[1].trigger('click')

    expect(names(drop)).toEqual(['notes.txt', 'deck.key'])
    expect(onSubmit).not.toHaveBeenCalled()
    expect(w.findAll('[data-testid="portal-attachment-chip"]').map((c) => c.text()))
      .toEqual([expect.stringContaining('notes.txt'), expect.stringContaining('deck.key')])
  })

  it('does not take focus from the composer on press', async () => {
    const { w } = await mountComposer()
    const ev = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
    w.find('[data-testid="portal-attachment-chip-remove"]').element.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(true)
  })

  it('is inert while locked', async () => {
    const { w, drop } = await mountComposer({ locked: true })
    const x = w.find('[data-testid="portal-attachment-chip-remove"]')
    expect(x.attributes('disabled')).toBeDefined()
    await x.trigger('click')
    expect(drop.entries.value).toHaveLength(3)
  })

  it('renders the sent arm through its slot', () => {
    const w = mount(PortalAttachmentChip, {
      props: { entry: { name: 'a.pdf', uploading: false, error: '', done: true }, testid: 'portal-room-attachment-chip' },
      slots: { sent: '<span class="opacity-70">· to scout</span>' },
    })
    expect(w.text()).toContain('· to scout')
    expect(w.find('[data-testid="portal-room-attachment-chip-remove"]').exists()).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// 4. the room — one delete per participant, by each one's stored name
// ---------------------------------------------------------------------------

describe('#3738 the room composer', () => {
  const ROOM = {
    id: 'room_1', name: 'Pricing review', status: 'open',
    participants: [
      { kind: 'user', identity: 'alice', role: 'moderator' },
      { kind: 'agent', identity: 'scout' },
      { kind: 'agent', identity: 'ranger' },
    ],
    messages: [], own_tags: {}, working: [],
  }
  let store
  let wrapper

  beforeEach(async () => {
    localStorage.clear()
    setActivePinia(createPinia())
    store = useClientPortalStore()
    store.portalToken = 'tok'
    store.fetchRoom = vi.fn(async () => JSON.parse(JSON.stringify(ROOM)))
    // The real carry log, so the test sees what an escalation would see.
    store.uploadDocument = vi.fn(async (agent, f) => {
      store.noteUploadForCarry(agent, f)
      return { filename: `${agent}_${f.name}` }
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/workspace/r/:roomId', component: { template: '<div />' } }],
    })
    await router.push('/workspace/r/room_1')
    await router.isReady()
    wrapper = mount(PortalRoom, {
      props: { roomId: 'room_1', roster: [{ name: 'scout' }, { name: 'ranger' }] },
      global: { plugins: [router] },
      attachTo: document.body,
    })
    await flushPromises()
  })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  async function dropFile(f) {
    // The root is the drop target; the template's leading comment makes the
    // wrapper a fragment, so the event is dispatched on the element itself.
    const ev = new Event('drop', { bubbles: true, cancelable: true })
    Object.defineProperty(ev, 'dataTransfer', { value: { types: ['Files'], files: [f] } })
    wrapper.find('div').element.dispatchEvent(ev)
    await flushPromises()
  }

  it('× deletes every participant\'s copy and forgets every carry entry', async () => {
    store.deleteUpload = vi.fn(async () => {})
    const f = file('a.pdf')
    await dropFile(f)
    expect(store.uploadDocument).toHaveBeenCalledTimes(2)
    const x = wrapper.find('[data-testid="portal-room-attachment-chip-remove"]')
    expect(x.attributes('aria-label')).toBe('Remove a.pdf')

    await x.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="portal-room-attachment-chip"]').exists()).toBe(false)
    expect(store.deleteUpload.mock.calls).toEqual([['scout', 'scout_a.pdf'], ['ranger', 'ranger_a.pdf']])
    expect(store.carryableUploadsFor('scout')).toEqual([])
    expect(store.carryableUploadsFor('ranger')).toEqual([])
  })

  it('one participant refusing the delete: the other is still deleted, both carry entries forgotten, one warn', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    store.deleteUpload = vi.fn(async (agent) => {
      if (agent === 'scout') throw Object.assign(new Error('rate'), { response: { status: 429 } })
    })
    await dropFile(file('a.pdf'))
    await wrapper.find('[data-testid="portal-room-attachment-chip-remove"]').trigger('click')
    await flushPromises()

    expect(store.deleteUpload).toHaveBeenCalledWith('ranger', 'ranger_a.pdf')
    expect(store.carryableUploadsFor('scout')).toEqual([])
    expect(store.carryableUploadsFor('ranger')).toEqual([])
    expect(warn).toHaveBeenCalledTimes(1)
    expect(warn.mock.calls[0]).toContain(429)
  })

  it('a chip that partly landed and failed, removed after the failure, deletes the copies that landed', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    store.deleteUpload = vi.fn(async () => {})
    store.uploadDocument = vi.fn(async (agent, f) => {
      if (agent === 'ranger') throw Object.assign(new Error('refused'), { response: { status: 413 } })
      store.noteUploadForCarry(agent, f)
      return { filename: `${agent}_${f.name}` }
    })
    await dropFile(file('a.pdf'))
    expect(wrapper.find('[data-testid="portal-room-attachment-chip"]').exists()).toBe(true)

    await wrapper.find('[data-testid="portal-room-attachment-chip-remove"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="portal-room-attachment-chip"]').exists()).toBe(false)
    expect(store.deleteUpload.mock.calls).toEqual([['scout', 'scout_a.pdf']])
    expect(store.carryableUploadsFor('scout')).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// 5. both composers render every chip through the shared component
// ---------------------------------------------------------------------------

describe.each([
  ['PortalConversation', 'portal-attachment-chip'],
  ['PortalRoom', 'portal-room-attachment-chip'],
])('#3738 %s chip sites', (component, testid) => {
  const SRC = readFileSync(path.resolve(process.cwd(), `src/components/portal/${component}.vue`), 'utf8')
  const { descriptor } = parse(SRC, { filename: `${component}.vue` })
  const all = []
  const walk = (node) => {
    for (const c of node.children || []) {
      if (c.type === 1) { all.push(c); walk(c) }
    }
  }
  walk(baseParse(descriptor.template.content))
  const attr = (n, name) => (n.props || []).find((p) => p.type === 6 && p.name === name)
  const dir = (n, name, arg) => (n.props || []).find((p) => p.type === 7 && p.name === name && (!arg || p.arg?.content === arg))

  it('has no hand-rolled chip left', () => {
    const rolled = all.filter((n) => n.tag !== 'PortalAttachmentChip' && attr(n, 'data-testid')?.value?.content === testid)
    expect(rolled).toEqual([])
  })

  it('every chip removes its own loop entry, behind a bound lock', () => {
    const chips = all.filter((n) => n.tag === 'PortalAttachmentChip')
    expect(chips.length).toBeGreaterThan(0)
    for (const chip of chips) {
      const loopVar = dir(chip, 'for').exp.content.match(/^\(?\s*(\w+)/)[1]
      expect(attr(chip, 'testid').value.content).toBe(testid)
      expect(dir(chip, 'on', 'remove').exp.content).toMatch(new RegExp(`^\\w+\\(${loopVar}\\)$`))
      expect(dir(chip, 'bind', 'locked')).toBeTruthy()
    }
  })
})
