/**
 * #2794 — attachments survive a 1:1 → room escalation.
 *
 * The bug: `PortalConversation` uploads a dropped file straight into the
 * CURRENT agent's inbox as it is attached, and the `escalate-to-room` event
 * carried only `{ agents, message }`. So @mentioning a second agent moved the
 * conversation to a room and left the file behind — the person had watched a
 * chip confirm the upload, so they believed both agents had it; only the
 * original one ever did, and the room showed no trace of a file at all.
 *
 * Three halves:
 *
 *   1. the pure carry rules (`portalAttachments.js`) — who still needs the
 *      file, what travels, and what the room is told;
 *   2. the composable's two new affordances — the retained `File` handle and
 *      an awaitable batch, which are what make a carry possible at all;
 *   3. source guards for the wiring no unit test can reach, since
 *      `vitest.config.js` pins `environment: 'node'` with no mount harness.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'
import {
  partitionAttachments, fanOutPlan, nameList, carriedNotice, noticeIsProblem, mergeCarrySources,
} from '@/components/portal/portalAttachments'
import {
  pruneCarryLog, CARRY_MAX_ENTRIES, CARRY_MAX_AGE_MS, CARRY_MAX_BYTES,
} from '@/stores/clientPortal'
import { usePortalFileDrop } from '@/composables/usePortalFileDrop'

const read = (rel) => stripComments(
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8'),
)
const CONVERSATION = read('../../src/components/portal/PortalConversation.vue')
const PORTAL = read('../../src/views/Portal.vue')
const ROOM = read('../../src/components/portal/PortalRoom.vue')

const sent = (name, file = { name }) => ({ name, uploading: false, error: '', done: true, file })
const failed = (name) => ({ name, uploading: false, error: 'Too large.', done: false, file: { name } })
const uploading = (name) => ({ name, uploading: true, error: '', done: false, file: { name } })

// ---------------------------------------------------------------------------
// 1. the carry rules
// ---------------------------------------------------------------------------

describe('#2794 partitionAttachments', () => {
  it('carries only what actually uploaded', () => {
    const ok = sent('a.pdf')
    const { carried, dropped } = partitionAttachments([ok, failed('b.pdf')])
    expect(carried).toEqual([ok])
    expect(dropped.map((e) => e.name)).toEqual(['b.pdf'])
  })

  it('treats a still-uploading entry as dropped, never as landed', () => {
    // Callers await `settled()` first, so an in-flight entry here means the
    // wait was skipped — reporting it beats assuming it made it.
    const { carried, dropped } = partitionAttachments([uploading('a.pdf')])
    expect(carried).toEqual([])
    expect(dropped.map((e) => e.name)).toEqual(['a.pdf'])
  })

  it('drops an entry with no readable handle rather than planning around it', () => {
    // A plan built from a handle-less entry fans out `undefined`.
    const { carried, dropped } = partitionAttachments([{ name: 'a.pdf', done: true, uploading: false, error: '' }])
    expect(carried).toEqual([])
    expect(dropped.map((e) => e.name)).toEqual(['a.pdf'])
  })

  it('survives junk', () => {
    expect(partitionAttachments(null)).toEqual({ carried: [], dropped: [] })
    expect(partitionAttachments([null, undefined, {}])).toEqual({ carried: [], dropped: [] })
  })
})

describe('#2794 fanOutPlan', () => {
  it('sends each carried file to every participant the origin agent is not', () => {
    // The room's own rule (one upload per participant), applied to the ones
    // that do not already have it.
    const plan = fanOutPlan([sent('a.pdf')], { origin: 'scout', participants: ['scout', 'sage', 'vault'] })
    expect(plan).toHaveLength(1)
    expect(plan[0].agents).toEqual(['sage', 'vault'])
  })

  it('excludes the origin agent BY NAME, not by position', () => {
    // The shell builds `agents` as [origin, ...mentioned]; a plan that trusted
    // that order would double-send the day the order changes.
    const plan = fanOutPlan([sent('a.pdf')], { origin: 'scout', participants: ['sage', 'scout'] })
    expect(plan[0].agents).toEqual(['sage'])
  })

  it('collapses a duplicate mention — the cost is per recipient', () => {
    const plan = fanOutPlan([sent('a.pdf')], { origin: 'scout', participants: ['scout', 'sage', 'sage'] })
    expect(plan[0].agents).toEqual(['sage'])
  })

  it('plans nothing when nobody new needs it', () => {
    // Better than an entry with an empty agent list, which reads as a success.
    expect(fanOutPlan([sent('a.pdf')], { origin: 'scout', participants: ['scout'] })).toEqual([])
    expect(fanOutPlan([], { origin: 'scout', participants: ['scout', 'sage'] })).toEqual([])
  })

  it('carries the handle through so the caller uploads the same bytes', () => {
    const file = { name: 'a.pdf' }
    const plan = fanOutPlan([sent('a.pdf', file)], { origin: 'scout', participants: ['scout', 'sage'] })
    expect(plan[0].file).toBe(file)
  })

  it('survives junk', () => {
    expect(fanOutPlan(null, {})).toEqual([])
    expect(fanOutPlan([sent('a.pdf')], undefined)).toEqual([])
  })
})

describe('#2794 nameList', () => {
  it('reads aloud', () => {
    expect(nameList(['a'])).toBe('a')
    expect(nameList(['a', 'b'])).toBe('a and b')
    expect(nameList(['a', 'b', 'c'])).toBe('a, b and c')
    expect(nameList([])).toBe('')
    expect(nameList(null)).toBe('')
  })
})

describe('#2794 carriedNotice', () => {
  it('says nothing when there were no attachments', () => {
    // An escalation without files must not grow a line about files.
    expect(carriedNotice({})).toBeNull()
    expect(carriedNotice({ carried: [], dropped: [], failures: [], recipients: ['sage'] })).toBeNull()
  })

  it('names the files and who else got them', () => {
    const text = carriedNotice({ carried: [sent('a.pdf')], recipients: ['sage', 'vault'] })
    expect(text).toContain('a.pdf')
    expect(text).toContain('sage and vault')
  })

  it('names a partial failure per file and per agent', () => {
    // "Some uploads failed" is not actionable; this is.
    const text = carriedNotice({
      carried: [sent('a.pdf'), sent('b.pdf')],
      failures: [{ name: 'b.pdf', agents: ['vault'] }],
      recipients: ['sage', 'vault'],
    })
    expect(text).toContain("b.pdf didn't reach vault")
    // The delivered line must not claim the file that did not arrive. Match the
    // sentence itself rather than slicing the string: the failure sentence also
    // starts with the file's name, so a naive prefix slice reads it back.
    expect(text).toMatch(/Sent with your message: a\.pdf —/)
    expect(text).not.toMatch(/Sent with your message: [^—]*b\.pdf/)
  })

  it('names what never left the 1:1 — never silently dropped', () => {
    const text = carriedNotice({ carried: [], dropped: [failed('b.pdf')], recipients: ['sage'] })
    expect(text).toContain('b.pdf')
    expect(text).toContain('not carried over')
  })

  it('classifies itself so the room knows how loudly to say it', () => {
    expect(noticeIsProblem({ carried: [sent('a.pdf')] })).toBe(false)
    expect(noticeIsProblem({ dropped: [failed('b.pdf')] })).toBe(true)
    expect(noticeIsProblem({ failures: [{ name: 'b.pdf', agents: ['vault'] }] })).toBe(true)
    // A failure naming no agent is not a failure anyone can act on.
    expect(noticeIsProblem({ failures: [{ name: 'b.pdf', agents: [] }] })).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// 2. the composable
// ---------------------------------------------------------------------------

const fileOf = (name, size = 10) => ({ name, size })

describe('#2794 usePortalFileDrop keeps the handle and can be awaited', () => {
  it('keeps the File on the entry so a second destination is reachable', () => {
    const drop = usePortalFileDrop(async () => {})
    const f = fileOf('a.pdf')
    return drop.addFiles([f]).then(() => {
      expect(drop.entries.value[0].file).toBe(f)
    })
  })

  it('a real File stays usable — markRaw does not break FormData', async () => {
    // The claim in the comment, checked rather than asserted: `markRaw` defines
    // a property on the object, and a File that refused it would fail deep
    // inside `FormData.append` where the cause is invisible.
    const drop = usePortalFileDrop(async () => {})
    const real = new File(['x'], 'a.txt', { type: 'text/plain' })
    await drop.addFiles([real])
    const kept = drop.entries.value[0].file
    expect(kept).toBe(real)
    expect(() => new FormData().append('file', kept)).not.toThrow()
  })

  it('settled() resolves only once the batch has landed', async () => {
    let release
    const gate = new Promise((r) => { release = r })
    const drop = usePortalFileDrop(() => gate)

    const batch = drop.addFiles([fileOf('a.pdf')])
    let done = false
    const waiter = drop.settled().then(() => { done = true })
    await Promise.resolve()
    expect(done, 'settled() resolved while the upload was still in flight').toBe(false)

    release()
    await batch
    await waiter
    expect(done).toBe(true)
    expect(drop.entries.value[0].uploading).toBe(false)
  })

  it('settled() is a no-op when nothing is in flight', async () => {
    const drop = usePortalFileDrop(async () => {})
    await expect(drop.settled()).resolves.toEqual([])
  })

  it('settled() never rejects — a failed upload is its own chip’s business', async () => {
    const drop = usePortalFileDrop(async () => { throw new Error('nope') })
    const batch = drop.addFiles([fileOf('a.pdf')])
    await expect(drop.settled()).resolves.toBeDefined()
    await batch
    expect(drop.entries.value[0].error).toBeTruthy()
  })

  it('a second drop chains onto the first rather than racing it', async () => {
    // Two overlapping batches firing together is the request burst the
    // sequencing exists to avoid, and settled() would resolve early.
    const order = []
    let release
    const gate = new Promise((r) => { release = r })
    const drop = usePortalFileDrop(async (file) => {
      order.push(`start:${file.name}`)
      if (file.name === 'a.pdf') await gate
      order.push(`end:${file.name}`)
    })

    const first = drop.addFiles([fileOf('a.pdf')])
    await Promise.resolve()
    const second = drop.addFiles([fileOf('b.pdf')])
    await Promise.resolve()
    expect(order).toEqual(['start:a.pdf'])

    release()
    await Promise.all([first, second])
    expect(order).toEqual(['start:a.pdf', 'end:a.pdf', 'start:b.pdf', 'end:b.pdf'])
    await expect(drop.settled()).resolves.toBeDefined()
  })
})

// ---------------------------------------------------------------------------
// 3. the wiring
// ---------------------------------------------------------------------------

describe('#2794 the 1:1 hands the attachments over', () => {
  it('waits for in-flight uploads before escalating', () => {
    // "Never silently dropped" — and this is the last moment waiting is
    // possible, because the composer is about to unmount.
    expect(CONVERSATION).toMatch(/await attachmentsSettled\(\)/)
  })

  it('cannot re-enter while it waits — a second Enter must not eat the message', () => {
    // The await is seconds long, and `input.value` is cleared BEFORE it. A
    // second send in that window would clear the newly typed text and emit a
    // second escalation, which `Portal.vue`'s own `escalating` flag drops on
    // the floor: message gone, no error, no composer to recover it from.
    const start = CONVERSATION.indexOf('async function send()')
    const send = CONVERSATION.slice(start, CONVERSATION.indexOf('async function submitUserText', start))
    expect(send).toMatch(/if \(!text \|\| sending\.value \|\| escalatingNow\.value\) return/)
    expect(send).toMatch(/escalatingNow\.value = true/)
    // Released on BOTH paths: a flag left set would outlive a failed
    // escalation and leave the composer the shell just restored dead.
    expect(send).toMatch(/} finally \{[\s\S]*escalatingNow\.value = false/)
  })

  it('emits them with the message', () => {
    expect(CONVERSATION).toMatch(/attachments: attachments\.value\.slice\(\)/)
  })

  it('does NOT clear them — that IS the failed-escalation recovery', () => {
    // On success this component unmounts as the room opens; on failure the
    // shell gives the text back and the chips are still standing beside it.
    // The send() body ONLY: `clearAttachments()` legitimately lives in
    // `deliver()`, which is where an ordinary turn clears its chips, and a
    // whole-file scan would read that one.
    const start = CONVERSATION.indexOf('async function send()')
    const escalation = CONVERSATION.slice(start, CONVERSATION.indexOf('async function submitUserText', start))
    expect(escalation).toMatch(/emit\('escalate-to-room'/)
    expect(escalation).not.toMatch(/clearAttachments\(\)/)
  })
})

describe('#2794 the shell fans them out', () => {
  const body = PORTAL.slice(
    PORTAL.indexOf('async function onEscalateToRoom'),
    PORTAL.indexOf('async function onEscalateToRoom') + 3000,
  )

  it('uploads through the same store action a room-native drop uses', () => {
    expect(body).toMatch(/store\.uploadDocument\(name, item\.file\)/)
  })

  it('uploads BEFORE posting the message', () => {
    // The message is what wakes the mentioned agent; a turn that starts before
    // the file is in its inbox cannot see the thing it was asked about.
    const upload = body.indexOf('store.uploadDocument')
    const post = body.indexOf('store.postRoomMessage')
    expect(upload).toBeGreaterThan(-1)
    expect(post).toBeGreaterThan(-1)
    expect(upload).toBeLessThan(post)
  })

  it('excludes the origin agent from the fan-out', () => {
    expect(body).toMatch(/fanOutPlan\(carried, \{ origin: agents\[0\], participants: agents \}\)/)
  })

  it('names the recipients off the plan, not off `agents` a second time', () => {
    // Two places deciding who the recipients are is how the notice ends up
    // naming somebody the fan-out never wrote to (and saying "sage and sage"
    // for a duplicate mention).
    expect(body).toMatch(/const recipients = plan\.length \? plan\[0\]\.agents : \[\]/)
  })

  it('reports a per-agent miss instead of failing the whole carry', () => {
    expect(body).toMatch(/missed\.push\(name\)/)
    expect(body).toMatch(/failures\.push\(\{ name: item\.name, agents: missed \}\)/)
  })

  it('hands the room a notice scoped to that room', () => {
    expect(body).toMatch(/roomCarryNotice\.value = \{ roomId,/)
    expect(PORTAL).toMatch(/roomCarryNotice\.value\.roomId === activeRoomIdFromRoute\.value/)
  })
})

describe('#2794 the room shows what came with the message', () => {
  it('renders the carry notice', () => {
    expect(ROOM).toMatch(/data-testid="portal-room-carry-notice"/)
    expect(ROOM).toMatch(/carryNotice: \{ type: Object, default: null \}/)
  })

  it('retires the carry notice on the next message — it describes history by then', () => {
    const send = ROOM.slice(ROOM.indexOf('async function send()'), ROOM.indexOf('async function addAgent'))
    expect(send).toMatch(/if \(props\.carryNotice\) emit\('dismiss-carry-notice'\)/)
    // The escalation's own first post is made by the shell, so this cannot
    // retire the notice before it has been read.
    expect(PORTAL).toMatch(/await store\.postRoomMessage\(roomId, message\)/)
  })

  it('clears its own chips once a message has gone', () => {
    // The 1:1's rule, which the room never had: without it a room accumulated
    // every chip it had ever drawn.
    const send = ROOM.slice(ROOM.indexOf('async function send()'), ROOM.indexOf('async function addAgent'))
    expect(send).toMatch(/clearAttachments\(\)/)
    expect(send.indexOf('clearAttachments()')).toBeGreaterThan(send.indexOf('postRoomMessage'))
  })
})


// ---------------------------------------------------------------------------
// 4. the OTHER upload surface (#2794 follow-up)
// ---------------------------------------------------------------------------

const railEntry = (name, over = {}) => ({
  agent: 'scout', name, size: 10, file: { name }, at: Date.now(), ...over,
})

describe('#2794 mergeCarrySources — the composer is not the only way to attach', () => {
  it('carries a file that only the rail knows about', () => {
    // THE defect this closes: the rail's Files panel sends straight to its
    // "Send to" target and keeps no pending state, so the escalation saw
    // nothing — no carry, and not even a notice saying so.
    const merged = mergeCarrySources([], [railEntry('deck.pdf')])
    expect(merged.map((e) => e.name)).toEqual(['deck.pdf'])
    const { carried } = partitionAttachments(merged)
    expect(carried).toHaveLength(1)
  })

  it('does not double-carry a file both surfaces saw', () => {
    // A composer upload goes through the same funnel, so it is in BOTH views.
    const chip = sent('a.pdf')
    chip.size = 10
    const merged = mergeCarrySources([chip], [railEntry('a.pdf')])
    expect(merged).toHaveLength(1)
    expect(merged[0]).toBe(chip)      // the composer entry wins — it holds the live outcome
  })

  it('treats same-name-different-size as two files', () => {
    const chip = sent('a.pdf'); chip.size = 10
    const merged = mergeCarrySources([chip], [railEntry('a.pdf', { size: 999 })])
    expect(merged).toHaveLength(2)
  })

  it('keeps a failed composer chip failed — the rail must not mask it', () => {
    // Otherwise a file that failed in the composer would be reported as
    // carried because a same-named rail entry sat behind it.
    const bad = failed('a.pdf'); bad.size = 10
    const merged = mergeCarrySources([bad], [railEntry('a.pdf')])
    const { carried, dropped } = partitionAttachments(merged)
    expect(carried).toEqual([])
    expect(dropped.map((e) => e.name)).toEqual(['a.pdf'])
  })

  it('normalises rail entries into the shape the rest of the module reads', () => {
    const [e] = mergeCarrySources([], [railEntry('deck.pdf')])
    // `uploadDocument` logs only AFTER the server took the file, so these are
    // landed by construction.
    expect(e.done).toBe(true)
    expect(e.uploading).toBe(false)
    expect(e.error).toBe('')
    expect(e.file).toBeTruthy()
  })

  it('survives junk from either side', () => {
    expect(mergeCarrySources(null, null)).toEqual([])
    expect(mergeCarrySources([null], [null, {}, { name: 'x' }])).toEqual([])  // no file → not carryable
  })
})

describe('#2794 pruneCarryLog — the log retains File objects, so it is bounded', () => {
  it('drops entries past the age window', () => {
    const now = Date.now()
    const kept = pruneCarryLog([
      railEntry('old.pdf', { at: now - CARRY_MAX_AGE_MS - 1 }),
      railEntry('new.pdf', { at: now }),
    ], now)
    expect(kept.map((e) => e.name)).toEqual(['new.pdf'])
  })

  it('caps the entry count, keeping the newest', () => {
    const now = Date.now()
    const many = Array.from({ length: CARRY_MAX_ENTRIES + 5 }, (_, i) =>
      railEntry(`f${i}.pdf`, { at: now - (CARRY_MAX_ENTRIES + 5 - i) }))
    const kept = pruneCarryLog(many, now)
    expect(kept).toHaveLength(CARRY_MAX_ENTRIES)
    expect(kept[kept.length - 1].name).toBe(`f${CARRY_MAX_ENTRIES + 4}.pdf`)
  })

  it('caps retained bytes, evicting oldest first', () => {
    const now = Date.now()
    const big = CARRY_MAX_BYTES / 2 + 1
    const kept = pruneCarryLog([
      railEntry('old.bin', { at: now - 3, size: big }),
      railEntry('mid.bin', { at: now - 2, size: big }),
      railEntry('new.bin', { at: now - 1, size: big }),
    ], now)
    expect(kept.map((e) => e.name)).toEqual(['new.bin'])
  })

  it('keeps a single over-cap file rather than refusing to carry it', () => {
    // Evicting it would silently drop the one file the person cares about.
    const now = Date.now()
    const kept = pruneCarryLog([railEntry('huge.bin', { at: now, size: CARRY_MAX_BYTES * 4 })], now)
    expect(kept).toHaveLength(1)
  })

  it('drops an entry whose File is gone', () => {
    expect(pruneCarryLog([railEntry('x.pdf', { file: null })])).toEqual([])
  })
})

describe('#2794 the carry boundary is drawn where the chips clear', () => {
  const CONV = CONVERSATION
  it('opening a conversation consumes NOTHING — mounting is not sending', () => {
    // The regression this pins, found by the operator on the live instance:
    // the rail is a SIBLING of the stage and survives navigation, so attaching
    // from wherever you are and THEN opening the chat you want to escalate
    // from is the ordinary gesture — and a mount boundary ate exactly that
    // upload, carrying nothing and saying nothing. A thread switch and ⌘J
    // remount this component too, so one boundary broke several gestures.
    const start = CONV.indexOf('onMounted(async () => {')
    const mounted = CONV.slice(start, start + 900)
    expect(mounted).not.toMatch(/markUploadsCarried/)
  })

  it('the only consume points are a sent turn and an escalation', () => {
    // Stated as a whole-file count so a third one cannot be added quietly.
    const conv = (CONV.match(/store\.markUploadsCarried\(/g) || []).length
    const portal = (PORTAL.match(/store\.markUploadsCarried\(/g) || []).length
    expect(conv).toBe(1)     // deliver(), beside clearAttachments()
    expect(portal).toBe(1)   // onEscalateToRoom()
  })

  it('a sent turn consumes them too', () => {
    const i = CONV.indexOf('clearAttachments()')
    expect(CONV.slice(i, i + 300)).toMatch(/store\.markUploadsCarried\(props\.agent\?\.name\)/)
  })

  it('the escalation consumes them, so a second one cannot re-carry', () => {
    const body = PORTAL.slice(PORTAL.indexOf('async function onEscalateToRoom'),
                              PORTAL.indexOf('async function onEscalateToRoom') + 3000)
    expect(body).toMatch(/mergeCarrySources\(attachments, store\.carryableUploadsFor\(agents\[0\]\)\)/)
    expect(body).toMatch(/store\.markUploadsCarried\(agents\[0\]\)/)
  })

  it('the funnel every surface shares is what records them', () => {
    const STORE = readFileSync(fileURLToPath(new URL('../../src/stores/clientPortal.js', import.meta.url)), 'utf8')
    const fn = STORE.slice(STORE.indexOf('async uploadDocument'), STORE.indexOf('async uploadDocument') + 700)
    expect(fn).toMatch(/this\.noteUploadForCarry\(agentName, file\)/)
  })
})
