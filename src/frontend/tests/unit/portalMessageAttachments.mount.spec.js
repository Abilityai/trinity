// @vitest-environment jsdom
/**
 * #3265 — a sent Workspace message shows what was attached to it.
 *
 * The composer's chip used to vanish when the turn went out and the bubble
 * showed only the typed text. Now the message carries its attachments: a
 * thumbnail per image, a chip per other file, a failed chip with its reason
 * for an upload that did not land. Mounted (#2918): what the person sees on the
 * message is the bug, so the test reads the rendered DOM.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

import PortalMessageAttachments from '@/components/portal/PortalMessageAttachments.vue'
import {
  sentAttachments, attachmentsForRequest, isImageAttachment, thumbnailUrl, _resetThumbnails,
} from '@/components/portal/portalMessageAttachments'

let created = 0
beforeEach(() => {
  _resetThumbnails()
  created = 0
  globalThis.URL.createObjectURL = vi.fn(() => `blob:thumb-${++created}`)
  globalThis.URL.revokeObjectURL = vi.fn()
})
afterEach(() => { document.body.innerHTML = '' })

const IMG = { filename: 'q3-chart.png', size_bytes: 830, mime_type: 'image/png', failed: false, error: null }
const PDF = { filename: 'notes.pdf', size_bytes: 12000, mime_type: 'application/pdf', failed: false, error: null }
const BAD = { filename: 'big.mov', size_bytes: null, mime_type: null, failed: true, error: 'Too large (40 MB).' }

function render(attachments, loadBlob = vi.fn(async () => new Blob(['x'], { type: 'image/png' }))) {
  const w = mount(PortalMessageAttachments, {
    attachTo: document.body,
    props: { attachments, agentName: 'scout', loadBlob },
  })
  return { w, loadBlob }
}

describe('PortalMessageAttachments (#3265)', () => {
  it('shows an image as a thumbnail, fetched once through the upload route', async () => {
    const { w, loadBlob } = render([IMG])
    await flushPromises()
    const img = w.find('[data-testid="portal-message-attachment-image"] img')
    expect(img.exists()).toBe(true)
    expect(img.attributes('src')).toMatch(/^blob:thumb-/)
    expect(img.attributes('alt')).toBe('q3-chart.png')
    expect(loadBlob).toHaveBeenCalledWith('scout', 'q3-chart.png')

    // The same picture on a re-rendered thread costs no second request.
    render([IMG], loadBlob)
    await flushPromises()
    expect(loadBlob).toHaveBeenCalledTimes(1)
  })

  it('uses the local file for a message sent from this tab', async () => {
    const file = new File(['x'], 'q3-chart.png', { type: 'image/png' })
    const { w, loadBlob } = render([{ ...IMG, file }])
    await flushPromises()
    expect(w.find('[data-testid="portal-message-attachment-image"] img').exists()).toBe(true)
    expect(loadBlob).not.toHaveBeenCalled()
  })

  it('shows any other file as a named chip with its size', async () => {
    const { w } = render([PDF])
    const chip = w.find('[data-testid="portal-message-attachment-file"]')
    expect(chip.text()).toContain('notes.pdf')
    expect(chip.text()).toContain('11.7 KB')
    expect(w.find('[data-testid="portal-message-attachment-image"]').exists()).toBe(false)
  })

  it('shows a failed upload as failed, with its reason — never drops it', async () => {
    const { w } = render([IMG, BAD])
    const failed = w.find('[data-testid="portal-message-attachment-failed"]')
    expect(failed.exists()).toBe(true)
    expect(failed.text()).toContain('big.mov')
    expect(failed.text()).toContain('Too large (40 MB).')
  })

  it('falls back to a chip when the thumbnail cannot be read', async () => {
    const { w } = render([IMG], vi.fn(async () => { throw new Error('429') }))
    await flushPromises()
    expect(w.find('[data-testid="portal-message-attachment-image"]').exists()).toBe(false)
    expect(w.find('[data-testid="portal-message-attachment-file"]').text()).toContain('q3-chart.png')
  })

  it('downloads the file on click through the same route', async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const { w, loadBlob } = render([PDF])
    await w.find('[data-testid="portal-message-attachment-file"]').trigger('click')
    await flushPromises()
    expect(loadBlob).toHaveBeenCalledWith('scout', 'notes.pdf')
    expect(click).toHaveBeenCalledTimes(1)
    click.mockRestore()
  })

  it('says so beside the file when a download fails', async () => {
    const { w } = render([PDF], vi.fn(async () => { throw new Error('gone') }))
    await w.find('[data-testid="portal-message-attachment-file"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="portal-message-attachment-error"]').text()).toContain("Couldn't download notes.pdf")
  })

  it('renders nothing for a message without attachments', () => {
    expect(render([]).w.find('[data-testid="portal-message-attachments"]').exists()).toBe(false)
  })
})

describe('portalMessageAttachments helpers (#3265)', () => {
  const file = new File(['x'], 'Pasted image.png', { type: 'image/png' })

  it('snapshots settled composer entries, naming each by its stored name', () => {
    const out = sentAttachments([
      { name: 'Pasted image.png', serverName: 'Pasted_image.png', size: 1, file, error: '' },
      { name: 'big.mov', size: 9, file, error: 'Too large.' },
    ])
    expect(out[0]).toMatchObject({ filename: 'Pasted_image.png', mime_type: 'image/png', failed: false, error: null })
    expect(out[0].file).toBe(file)
    expect(out[1]).toMatchObject({ filename: 'big.mov', failed: true, error: 'Too large.', file: null })
  })

  it('sends names and failures, never bytes', () => {
    expect(attachmentsForRequest(null)).toBeNull()
    expect(attachmentsForRequest([])).toBeNull()
    expect(attachmentsForRequest([{ ...IMG, file }, BAD])).toEqual([
      { filename: 'q3-chart.png' },
      { filename: 'big.mov', failed: true, error: 'Too large (40 MB).' },
    ])
  })

  it('treats only a non-failed image as a thumbnail', () => {
    expect(isImageAttachment(IMG)).toBe(true)
    expect(isImageAttachment(PDF)).toBe(false)
    expect(isImageAttachment({ ...IMG, failed: true })).toBe(false)
    expect(isImageAttachment({ filename: 'a.png', mime_type: null, file })).toBe(true)
  })

  it('forgets a failed thumbnail so a later render can retry', async () => {
    const fail = vi.fn(async () => { throw new Error('429') })
    await expect(thumbnailUrl('scout', IMG, fail)).rejects.toThrow()
    const ok = vi.fn(async () => new Blob(['x']))
    await expect(thumbnailUrl('scout', IMG, ok)).resolves.toMatch(/^blob:/)
    expect(ok).toHaveBeenCalledTimes(1)
  })
})

describe('usePortalFileDrop keeps the stored name (#3265)', () => {
  it('records the name the upload route stored the file under', async () => {
    const { usePortalFileDrop } = await import('@/composables/usePortalFileDrop')
    const f = new File(['x'], 'Pasted image (3).png', { type: 'image/png' })
    const g = new File(['y'], 'plain.txt', { type: 'text/plain' })
    const drop = usePortalFileDrop(async (file) => (file === f ? { filename: 'Pasted_image_3.png' } : undefined))
    await drop.addFiles([f, g])
    expect(drop.entries.value.map((e) => e.serverName)).toEqual(['Pasted_image_3.png', 'plain.txt'])
  })
})
