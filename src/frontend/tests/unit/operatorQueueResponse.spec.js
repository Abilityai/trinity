import { describe, it, expect, beforeEach, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { nextTick } from 'vue'
import { setActivePinia, createPinia } from 'pinia'
import {
  optionsOf,
  queueResponseKind,
  queueResponseBody,
  buildQueueResponse,
  queueTypeLabel,
  QUEUE_TYPE_LABELS,
  QUEUE_RESPONSE_NOT_RECORDED,
  respondRefusedAsNotPending,
} from '@/utils/operatorQueue'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }
  return { default: inst }
})
vi.mock('@/api', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})
import axios from 'axios'
import { useOperatorQueueStore } from '@/stores/operatorQueue'

/**
 * #2370 — the respond payload has ONE builder.
 *
 * The bug: `/m` hand-built `POST /api/operator-queue/{id}/respond` with a
 * hard-coded `response: 'approved'` and the tapped option in `response_text`,
 * so a Deny was recorded as an approval. The agent reads `response` as the
 * decision (write-back + ent#329 framing), so the field is load-bearing.
 * `utils/operatorQueue.js` is now the only place the body, the controls-kind
 * switch and the type labels are decided; the desktop store and `/m` are
 * callers. `vitest.config.js` pins `environment: 'node'` (no component
 * mounting), so the decision table is proved here and the wiring is pinned by
 * source assertions + a behavioural store test (axios mocked).
 */

const here = dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(resolve(here, '../../src', rel), 'utf8')
// ------------------------------------------------------------- optionsOf

describe('optionsOf — the DB stores whatever JSON the agent wrote', () => {
  it('returns a string list for a string array', () => {
    expect(optionsOf({ options: ['Approve', 'Deny'] })).toEqual(['Approve', 'Deny'])
  })
  it('stringifies primitives and drops objects, nulls and empty strings', () => {
    expect(optionsOf({ options: [1, true, 'x', null, { label: 'no' }, '', ['nested']] })).toEqual(['1', 'true', 'x'])
  })
  it.each([
    ['null', { options: null }],
    ['missing', {}],
    ['a bare string (would be iterated char by char)', { options: 'Approve' }],
    ['a dict', { options: { a: 1 } }],
    ['a number', { options: 3 }],
    ['no item', undefined],
  ])('is [] for %s', (_label, item) => {
    expect(optionsOf(item)).toEqual([])
  })
})

// ------------------------------------------------------ queueResponseKind

describe('queueResponseKind — controls switch on the item TYPE (desktop parity), total', () => {
  it.each([
    ['approval with options → buttons', { type: 'approval', options: ['a', 'b'] }, 'approval'],
    ['approval with [] → text answer (desktop shows nothing)', { type: 'approval', options: [] }, 'question'],
    ['approval with null options → text answer', { type: 'approval', options: null }, 'question'],
    ['approval with malformed options → text answer', { type: 'approval', options: 'yes' }, 'question'],
    ['question → text answer', { type: 'question', options: null }, 'question'],
    ['question carrying options still gets a text answer (never one-tap)', { type: 'question', options: ['x'] }, 'question'],
    ['alert → acknowledge', { type: 'alert', options: null }, 'acknowledge'],
    ['unknown type → text answer', { type: 'weird', options: ['x'] }, 'question'],
    ['missing type → text answer', { options: ['x'] }, 'question'],
  ])('%s', (_label, item, kind) => {
    expect(queueResponseKind(item)).toBe(kind)
  })
})

// ------------------------------------------------------- queueResponseBody

describe('queueResponseBody — the wire shape', () => {
  it('puts the decision in response and a missing note as null (never "")', () => {
    expect(queueResponseBody('Deny')).toEqual({ response: 'Deny', response_text: null })
    expect(queueResponseBody('Deny', '')).toEqual({ response: 'Deny', response_text: null })
  })
  it('trims the note; whitespace-only is null', () => {
    expect(queueResponseBody('Deny', '  not today  ')).toEqual({ response: 'Deny', response_text: 'not today' })
    expect(queueResponseBody('Deny', '   ')).toEqual({ response: 'Deny', response_text: null })
  })
  it('never trims or rewrites the decision — an option must stay byte-identical to what the agent offered', () => {
    expect(queueResponseBody(' Approve ', '')).toEqual({ response: ' Approve ', response_text: null })
    expect(queueResponseBody(42, null)).toEqual({ response: '42', response_text: null })
  })
})

// ------------------------------------------------------ buildQueueResponse

describe('buildQueueResponse — the three answer kinds', () => {
  it('approval: the tapped option is the response, the note rides response_text', () => {
    expect(buildQueueResponse({ kind: 'approval', option: 'Deny' })).toEqual({ response: 'Deny', response_text: null })
    expect(buildQueueResponse({ kind: 'approval', option: 'Deny', note: ' not today ' }))
      .toEqual({ response: 'Deny', response_text: 'not today' })
  })
  it('approval without a chosen option is not sendable', () => {
    expect(buildQueueResponse({ kind: 'approval', option: null, note: 'x' })).toBeNull()
    expect(buildQueueResponse({ kind: 'approval', option: '', note: 'x' })).toBeNull()
    expect(buildQueueResponse({ kind: 'approval' })).toBeNull()
  })
  it('question: the typed answer IS the response (trimmed), never the note', () => {
    expect(buildQueueResponse({ kind: 'question', answer: '  yes, go  ' })).toEqual({ response: 'yes, go', response_text: null })
    expect(buildQueueResponse({ kind: 'question', answer: 'ok', note: 'ignored' })).toEqual({ response: 'ok', response_text: null })
  })
  it('question: a blank answer is not sendable', () => {
    expect(buildQueueResponse({ kind: 'question', answer: '   ' })).toBeNull()
    expect(buildQueueResponse({ kind: 'question' })).toBeNull()
  })
  it('acknowledge: the desktop "Got it" shape', () => {
    expect(buildQueueResponse({ kind: 'acknowledge' })).toEqual({ response: 'acknowledged', response_text: null })
  })
  it('unknown kind / no args → null', () => {
    expect(buildQueueResponse({ kind: 'nope', option: 'x', answer: 'y' })).toBeNull()
    expect(buildQueueResponse()).toBeNull()
  })
  it('the literal that was the bug can no longer be produced from an option tap', () => {
    for (const opt of ['Approve', 'Deny', 'reject', 'approve']) {
      const body = buildQueueResponse({ kind: 'approval', option: opt })
      expect(body.response).toBe(opt)
      expect(body.response_text).toBeNull()
    }
  })
})

// ---------------------------------------------------------- queueTypeLabel

describe('queueTypeLabel — desktop QueueCard labels, shared', () => {
  it('maps the three protocol types', () => {
    expect(queueTypeLabel('approval')).toBe('Needs approval')
    expect(queueTypeLabel('question')).toBe('Question')
    expect(queueTypeLabel('alert')).toBe('Heads up')
    expect(QUEUE_TYPE_LABELS).toEqual({ approval: 'Needs approval', question: 'Question', alert: 'Heads up' })
  })
  it('falls back to the raw type string, and to "" when there is none — never a blank from a wrong field name', () => {
    expect(queueTypeLabel('custom')).toBe('custom')
    expect(queueTypeLabel(undefined)).toBe('')
    expect(queueTypeLabel(null)).toBe('')
    expect(queueTypeLabel(42)).toBe('')
  })
})

// ------------------------------------------------- respondRefusedAsNotPending

describe('respondRefusedAsNotPending — 409 (lost race), 400 (already terminal) and 404 (row gone) mean "not recorded"', () => {
  it.each([
    [409, true],
    [400, true],
    [404, true],
    [500, false],
    [403, false],
    [422, false],
  ])('status %s → %s', (status, expected) => {
    expect(respondRefusedAsNotPending({ response: { status } })).toBe(expected)
  })
  it('a transport error (no response) is not a refusal', () => {
    expect(respondRefusedAsNotPending(new Error('network'))).toBe(false)
    expect(respondRefusedAsNotPending(undefined)).toBe(false)
  })
  it('the copy names no actor — the status may be responded, cancelled or expired', () => {
    expect(QUEUE_RESPONSE_NOT_RECORDED).toMatch(/no longer pending/)
    expect(QUEUE_RESPONSE_NOT_RECORDED).toMatch(/not recorded/)
    expect(QUEUE_RESPONSE_NOT_RECORDED).not.toMatch(/another operator/)
  })
})

// ------------------------------------------------------------ store (behavioural)

describe('operatorQueue store — respondToItem builds its body with the shared builder', () => {
  let store
  // Built per test: the store's optimistic update MUTATES the served row, so a
  // shared fixture would carry `status: 'responded'` into the next test.
  const item = () => ({ id: 'a', agent_name: 'ag', type: 'approval', status: 'pending', priority: 'medium', title: 't', question: 'q', options: ['Approve', 'Deny'], created_at: '2026-08-21T10:00:00Z' })

  beforeEach(async () => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    store = useOperatorQueueStore()
    axios.get.mockResolvedValueOnce({ data: { items: [item()], count: 1 } })
    await store.fetchItems()
    await nextTick()
    expect(store.items.find(i => i.id === 'a').status).toBe('pending')
  })

  it('sends the option as response and a whitespace-only note as null; the optimistic update mirrors the body', async () => {
    axios.post.mockResolvedValueOnce({ data: {} })
    await store.respondToItem('a', 'Deny', '   ')
    expect(axios.post).toHaveBeenCalledTimes(1)
    const [url, body] = axios.post.mock.calls[0]
    expect(url).toBe('/api/operator-queue/a/respond')
    expect(body).toEqual({ response: 'Deny', response_text: null })
    const row = store.items.find(i => i.id === 'a')
    expect(row.status).toBe('responded')
    expect(row.response).toBe('Deny')
    expect(row.response_text).toBeNull()
  })

  it('sends a trimmed note in response_text', async () => {
    axios.post.mockResolvedValueOnce({ data: {} })
    await store.respondToItem('a', 'Approve', '  ship it  ')
    expect(axios.post.mock.calls[0][1]).toEqual({ response: 'Approve', response_text: 'ship it' })
  })

  it('a blank or undefined decision never reaches the wire (the belt behind the callers\' guards)', async () => {
    await store.respondToItem('a', undefined, 'note')
    await store.respondToItem('a', '   ', 'note')
    expect(axios.post).not.toHaveBeenCalled()
    expect(store.items.find(i => i.id === 'a').status).toBe('pending')
  })

  it('acknowledgeItem sends the acknowledged decision with no note', async () => {
    axios.post.mockResolvedValueOnce({ data: {} })
    await store.acknowledgeItem('a')
    expect(axios.post.mock.calls[0][1]).toEqual({ response: 'acknowledged', response_text: null })
  })

  it.each([409, 400, 404])('a %s refusal does not throw, does not mark the row responded, and refetches', async (status) => {
    axios.post.mockRejectedValueOnce({ response: { status, data: { detail: 'nope' } } })
    // The refetch serves the row still pending (the server's truth wins).
    axios.get.mockResolvedValueOnce({ data: { items: [item()], count: 1 } })
    await store.respondToItem('a', 'Deny', '')
    expect(store.items.find(i => i.id === 'a').status).toBe('pending')
    expect(axios.get).toHaveBeenCalledTimes(2)
  })

  // The regression this ordering guards: `fetchItems` nulls `error` in its own
  // synchronous prologue, so assigning the copy BEFORE an un-awaited refetch
  // destroyed it before any render. 400 and 404 used to fall through to the
  // `else` and leave a message, so widening the refusal set without fixing the
  // order would have silenced two statuses that previously reported.
  it.each([409, 400, 404])('a %s refusal leaves the not-recorded copy OBSERVABLE after the refetch', async (status) => {
    axios.post.mockRejectedValueOnce({ response: { status, data: { detail: 'nope' } } })
    axios.get.mockResolvedValueOnce({ data: { items: [item()], count: 1 } })
    await store.respondToItem('a', 'Deny', '')
    await nextTick()
    expect(store.error).toBe(QUEUE_RESPONSE_NOT_RECORDED)
  })

  it('the not-recorded copy outranks a failed refetch — the answer matters more than the staleness', async () => {
    axios.post.mockRejectedValueOnce({ response: { status: 409 } })
    axios.get.mockRejectedValueOnce(new Error('network'))
    await store.respondToItem('a', 'Deny', '')
    await nextTick()
    expect(store.error).toBe(QUEUE_RESPONSE_NOT_RECORDED)
  })

  it('a 500 is NOT a refusal — it keeps the generic message and never refetches', async () => {
    axios.post.mockRejectedValueOnce({ response: { status: 500, data: { detail: 'boom' } } })
    await store.respondToItem('a', 'Deny', '')
    await nextTick()
    expect(store.error).not.toBe(QUEUE_RESPONSE_NOT_RECORDED)
    expect(store.error).toBeTruthy()
    expect(axios.get).toHaveBeenCalledTimes(1) // the beforeEach load only
  })
})

// ------------------------------------------------------------------ wiring

describe('wiring — the producers call the builder and the old literals are gone', () => {
  // Raw source, comments included — the old literals may not survive anywhere
  // in these files, not even in a comment.
  const mobile = read('views/MobileAdmin.vue')
  const store = read('stores/operatorQueue.js')
  const card = read('components/operator/QueueCard.vue')

  it('MobileAdmin imports the util and builds every answer with it', () => {
    expect(mobile).toMatch(/from ['"]\.\.\/utils\/operatorQueue['"]/)
    expect(mobile).toMatch(/buildQueueResponse\(\s*\{\s*kind:\s*['"]approval['"]/)
    expect(mobile).toMatch(/buildQueueResponse\(\s*\{\s*kind:\s*['"]question['"]/)
    expect(mobile).toMatch(/buildQueueResponse\(\s*\{\s*kind:\s*['"]acknowledge['"]/)
    expect(mobile).toMatch(/queueResponseKind\(/)
    expect(mobile).toMatch(/queueTypeLabel\(\s*item\.type\s*\)/)
  })
  it('MobileAdmin no longer hard-codes the decision or reads the wrong field', () => {
    expect(mobile).not.toMatch(/response:\s*['"]approved['"]/)
    expect(mobile).not.toMatch(/request_type/)
    // No one-tap answer: an option button selects, it never posts.
    expect(mobile).not.toMatch(/@click="respondToQueueItem\(/)
  })
  it('the desktop store builds its body with queueResponseBody and shares the refusal predicate + copy', () => {
    expect(store).toMatch(/from ['"]\.\.\/utils\/operatorQueue['"]/)
    expect(store).toMatch(/queueResponseBody\(/)
    expect(store).toMatch(/respondRefusedAsNotPending\(/)
    expect(store).toMatch(/QUEUE_RESPONSE_NOT_RECORDED/)
    expect(store).not.toMatch(/response_text:\s*responseText\s*\|\|\s*null/)
  })
  it('QueueCard renders the shared type labels', () => {
    expect(card).toMatch(/from ['"]\.\.\/\.\.\/utils\/operatorQueue['"]/)
    expect(card).toMatch(/queueTypeLabel\(\s*item\.type\s*\)/)
    expect(card).not.toMatch(/function typeLabel\(/)
  })

  // The store's ordering is proved behaviourally above; this pins the shape so
  // the un-awaited form cannot come back.
  it('the desktop store awaits the refetch BEFORE assigning the not-recorded copy', () => {
    expect(store).toMatch(/await fetchItems\(\)\s*\n\s*error\.value = QUEUE_RESPONSE_NOT_RECORDED/)
    expect(store).not.toMatch(/error\.value = QUEUE_RESPONSE_NOT_RECORDED\s*\n\s*fetchItems\(\)/)
  })

  // `/m`'s fetchQueue + prune are `<script setup>` internals and vitest runs
  // `environment: 'node'` (no component mounting), so these two are pinned on
  // source — the same tool this file already uses for view wiring.
  it("/m's stale-fetch guard covers every exit, not just the success path", () => {
    // A superseded poll must not write an error over fresh data...
    expect(mobile).toMatch(/catch \(e\) \{\s*\n\s*if \(seq !== queueFetchSeq\) return/)
    // ...nor drop the spinner while the newer request is still running.
    expect(mobile).toMatch(/if \(seq === queueFetchSeq\) loading\.queue = false/)
    // fetchQueue is the only writer of `loading.queue`, so an unguarded
    // assignment anywhere — at the start of a line, i.e. not behind the
    // guard above — is the regression coming back.
    expect(mobile).not.toMatch(/^\s*loading\.queue = false/m)
  })

  it('/m never prunes the in-flight send guard, and logout still wipes it', () => {
    expect(mobile).toMatch(/live\.has\(id\) \|\| respondingItems\[id\] === true/)
    // resetQueueItemState is a wipe, not a reconcile — delegating to the prune
    // would inherit the in-flight exemption and strand a disabled Send.
    expect(mobile).not.toMatch(/pruneQueueItemState\(\[\]\)/)
  })
})
