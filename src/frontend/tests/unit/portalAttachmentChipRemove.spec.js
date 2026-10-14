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
 * so the test clicks it and counts submits.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

import PortalAttachmentChip from '@/components/portal/PortalAttachmentChip.vue'
import { usePortalFileDrop, attachmentState } from '@/composables/usePortalFileDrop'
import { useClientPortalStore } from '@/stores/clientPortal'

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
