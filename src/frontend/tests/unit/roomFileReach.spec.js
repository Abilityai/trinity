/**
 * #2794 (second half) — a file put into a room reaches every agent in it,
 * however it was put there.
 *
 * The first half of this issue made an attachment survive the escalation from a
 * 1:1 into a room. Testing that live turned up the rest of the path, and it was
 * worse than the original report: in a room with two agents, the client sent a
 * screenshot from the rail, asked the SECOND agent about it, and got "I don't
 * see any image attached". Three independent gaps, each individually invisible:
 *
 *   1. **The rail aimed at one agent.** Its `Send to` select defaulted to the
 *      first participant while the room's own drop zone fanned out — two
 *      surfaces, two meanings for "send a file to this chat", and the one with
 *      the visible control was the wrong one. Fixed in `portalFiles.js`, which
 *      is where a rule has to live to be testable at all (`environment: 'node'`,
 *      no mount harness).
 *
 *   2. **Pasting did nothing.** No paste handler existed on either composer, so
 *      the single most common way to attach a screenshot was inert and silent.
 *
 *   3. **No agent was ever told.** That half is backend and is pinned by
 *      `tests/unit/test_2794_room_file_awareness.py`.
 *
 * Source guards cover the wiring, per this file's sibling: a rule can be proven
 * here, a `.vue` binding cannot.
 */
import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'
import {
  ALL_PARTICIPANTS,
  defaultUploadTarget,
  resolveRecipients,
  uploadReceipt,
  uploadTargetLabel,
  uploadTargets,
} from '@/components/portal/portalFiles'
import {
  clipboardHasText, filesFromClipboard, usePortalFileDrop,
} from '@/composables/usePortalFileDrop'

const read = (rel) => stripComments(
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8'),
)
const RAIL = read('../../src/components/portal/PortalRailFiles.vue')
const ROOM = read('../../src/components/portal/PortalRoom.vue')
const CONVERSATION = read('../../src/components/portal/PortalConversation.vue')

const TWO = ['analyst-demo', 'sidekick']

// ---------------------------------------------------------------------------
// 1. who a file goes to
// ---------------------------------------------------------------------------

describe('the rail sends a room file to the whole room', () => {
  it('defaults a multi-agent chat to everyone', () => {
    // The line that fixes the report. The old default was `participants[0]`.
    expect(defaultUploadTarget(TWO)).toBe(ALL_PARTICIPANTS)
    expect(resolveRecipients(defaultUploadTarget(TWO), TWO)).toEqual(TWO)
  })

  it('offers no fan-out entry in a 1:1, because there is no choice to make', () => {
    expect(uploadTargets(['solo'])).toEqual([{ value: 'solo', label: 'solo' }])
    expect(defaultUploadTarget(['solo'])).toBe('solo')
  })

  it('lists everyone first, then each agent, so one recipient stays reachable', () => {
    const values = uploadTargets(TWO).map((t) => t.value)
    expect(values).toEqual([ALL_PARTICIPANTS, 'analyst-demo', 'sidekick'])
  })

  it('honours an explicit single pick', () => {
    expect(resolveRecipients('sidekick', TWO)).toEqual(['sidekick'])
  })

  it('falls back to the fan-out when the pick has left the room', () => {
    // Recoverable (the rail has a delete) vs. silent loss. Only one of those is
    // the failure this issue is about.
    expect(resolveRecipients('departed', TWO)).toEqual(TWO)
  })

  it('resolves to nobody only when there IS nobody', () => {
    expect(resolveRecipients(ALL_PARTICIPANTS, [])).toEqual([])
  })

  it('names the recipients before the file is let go of', () => {
    expect(uploadTargetLabel(ALL_PARTICIPANTS, TWO)).toBe('analyst-demo and sidekick')
    expect(uploadTargetLabel('sidekick', TWO)).toBe('sidekick')
    expect(uploadTargetLabel(ALL_PARTICIPANTS, ['a', 'b', 'c'])).toBe('all 3 agents')
  })

  it('states BOTH halves of a fan-out in the receipt', () => {
    // "Sent shot.png" over a two-agent fan-out was true and was read as "both
    // of them have it" — which, before this, was false.
    expect(uploadReceipt({ files: ['shot.png'], recipients: TWO }))
      .toBe('Sent “shot.png” to analyst-demo and sidekick.')
    expect(uploadReceipt({ files: ['a', 'b'], recipients: ['solo'] }))
      .toBe('Sent 2 files to solo.')
  })

  it('claims nothing when nothing was sent', () => {
    expect(uploadReceipt({ files: [], recipients: TWO })).toBe('')
    expect(uploadReceipt({ files: ['a'], recipients: [] })).toBe('')
  })
})

describe('the rail is wired to those rules', () => {
  it('renders the option list rather than the bare participants', () => {
    expect(RAIL).toMatch(/v-for="t in targets"/)
    expect(RAIL).not.toMatch(/v-for="p in participants"[^>]*:value="p"/)
  })

  it('falls back to the DEFAULT, never to the first participant', () => {
    expect(RAIL).toContain('defaultUploadTarget(participants.value)')
    expect(RAIL).not.toContain('target.value = participants.value[0]')
  })

  it('uploads once per recipient', () => {
    expect(RAIL).toMatch(/for \(const agent of to\)/)
    expect(RAIL).toContain('feeds.upload(agent, file)')
  })

  it('reports the name the SERVER wrote, not the one that was picked', () => {
    // `_safe_filename` sanitizes server-side and the response carries the name
    // that actually landed; reporting `file.name` would name a file the inbox
    // does not contain — the honesty class this whole PR is about.
    expect(RAIL).toContain('if (res?.filename) landed = res.filename')
    expect(RAIL).toContain('else sent.push(landed)')
  })

  it('names the agents a file MISSED, and counts a partial as a failure', () => {
    // Counting a partial fan-out as a success would rebuild the reported bug
    // inside its own fix: "Sent shot.png to analyst-demo and sidekick" while
    // sidekick got nothing is exactly what made the gap invisible.
    expect(RAIL).toContain('if (missed.length) failed.push')
    expect(RAIL).toMatch(/\$\{file\.name\} → \$\{missed\.join\(', '\)\}/)
    expect(RAIL).toContain('else sent.push(landed)')
  })
})

// ---------------------------------------------------------------------------
// 2. pasting a screenshot
// ---------------------------------------------------------------------------

describe('paste attaches a file', () => {
  const pngFile = { name: 'image.png', size: 12, type: 'image/png' }

  it('reads clipboard files', () => {
    expect(filesFromClipboard({ files: [pngFile] })).toEqual([pngFile])
  })

  it('falls back to items for the browsers that only fill those', () => {
    const data = { files: [], items: [{ kind: 'file', getAsFile: () => pngFile }] }
    expect(filesFromClipboard(data)).toEqual([pngFile])
  })

  it('ignores a text paste entirely', () => {
    const data = { files: [], items: [{ kind: 'string', getAsFile: () => null }] }
    expect(filesFromClipboard(data)).toEqual([])
    expect(filesFromClipboard(null)).toEqual([])
  })

  it('detects text riding along with the image', () => {
    expect(clipboardHasText({ types: ['text/plain', 'Files'] })).toBe(true)
    expect(clipboardHasText({ types: ['Files'] })).toBe(false)
    expect(clipboardHasText(undefined)).toBe(false)
  })

  it('goes through the same batch as a drop', async () => {
    const upload = vi.fn().mockResolvedValue({})
    const { handlers, entries } = usePortalFileDrop(upload)
    const preventDefault = vi.fn()
    await handlers.onPaste({
      preventDefault,
      clipboardData: { files: [pngFile], items: [], types: ['Files'] },
    })
    expect(upload).toHaveBeenCalledOnce()
    expect(entries.value.map((e) => e.name)).toEqual(['image.png'])
    // Nothing else on the clipboard, so the default is suppressed and no stray
    // text lands in the composer.
    expect(preventDefault).toHaveBeenCalled()
  })

  it('does not swallow a paste that also carries text', async () => {
    // Copying out of a rich editor puts both on the clipboard. Attaching the
    // image must not delete the text they meant to paste.
    const upload = vi.fn().mockResolvedValue({})
    const { handlers } = usePortalFileDrop(upload)
    const preventDefault = vi.fn()
    await handlers.onPaste({
      preventDefault,
      clipboardData: { files: [pngFile], items: [], types: ['text/plain', 'Files'] },
    })
    expect(upload).toHaveBeenCalledOnce()
    expect(preventDefault).not.toHaveBeenCalled()
  })

  it('is inert on a plain text paste', async () => {
    const upload = vi.fn()
    const { handlers, entries } = usePortalFileDrop(upload)
    const preventDefault = vi.fn()
    await handlers.onPaste({
      preventDefault,
      clipboardData: { files: [], items: [], types: ['text/plain'] },
    })
    expect(upload).not.toHaveBeenCalled()
    expect(entries.value).toEqual([])
    expect(preventDefault).not.toHaveBeenCalled()
  })

  it('respects the disabled gate, like every other handler', async () => {
    const upload = vi.fn()
    const { handlers } = usePortalFileDrop(upload, { disabled: () => true })
    await handlers.onPaste({
      preventDefault: () => {},
      clipboardData: { files: [pngFile], items: [], types: ['Files'] },
    })
    expect(upload).not.toHaveBeenCalled()
  })
})

describe('both composers accept a paste', () => {
  // The handler is worth nothing unbound, and a binding is exactly what a
  // node-env suite cannot exercise.
  it('the 1:1 composer', () => {
    expect(CONVERSATION).toContain('@paste="dropHandlers.onPaste"')
  })
  it('the room composer', () => {
    expect(ROOM).toContain('@paste="dropHandlers.onPaste"')
  })
})

describe('the room drop still fans out', () => {
  it('uploads to every participating agent', () => {
    // Unchanged by this PR, asserted because it is now HALF of a pair: if this
    // regressed to a single target, the rail would be the only surface that
    // reached the whole room and the bug would return by the other door.
    expect(ROOM).toMatch(/for \(const name of names\) await store\.uploadDocument\(name, file\)/)
  })
})
