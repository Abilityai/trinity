/**
 * #2580 defect 4 — a streamed reply must be rateable as soon as its row id
 * exists, not on the next page load.
 *
 * The reported symptom was that some agent replies had thumbs and others did
 * not, with no rule a reader could see. The cause was not a missing feature and
 * not a timing problem: the persisted row's id was already in hand and was being
 * thrown away. `awaitPersistedReply` polls the history endpoint — that is how it
 * knows the turn finished — and returned only `content` and `cost` from the row
 * the server had just written, so both live-turn paths pushed an id-less
 * message and `<PortalRating v-if="item.message.id">` correctly hid the control.
 * A reload then re-read the same row WITH its id and the thumbs appeared.
 *
 * These are real unit tests rather than source greps because the rule was moved
 * into `portalUtils.js` for exactly that reason: `vitest.config.js` pins
 * `environment: 'node'` with no mount harness, so a mapper inlined in an SFC is
 * a mapper no test can execute — and a dropped object key is invisible to a
 * source assertion, which is precisely the defect class here.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import * as portalUtils from '@/components/portal/portalUtils'

const { assistantRow, replyBaseline, replyFromHistory } = portalUtils

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const CONV = read('../../src/components/portal/PortalConversation.vue')

describe('#2580 — assistantRow carries what a thumb needs', () => {
  it('keeps the persisted id and the caller’s own rating', () => {
    expect(assistantRow({ content: 'hi', id: 'm1', my_rating: 'up' })).toEqual({
      role: 'assistant', content: 'hi', id: 'm1', myRating: 'up',
      source: null, voiceCallId: null,
    })
  })

  it('normalises a missing id to null rather than undefined', () => {
    // The consumer's gate is a truthiness test either way, but an explicit null
    // says "built without one" where an absent key says nothing at all — and it
    // makes the two cases distinguishable in a snapshot or a log.
    expect(assistantRow({ content: 'x' }).id).toBeNull()
    expect(assistantRow({ content: 'x', id: undefined }).id).toBeNull()
    expect(assistantRow({ content: 'x', id: '' }).id).toBeNull()
    expect(assistantRow({}).content).toBe('')
    expect(assistantRow().role).toBe('assistant')
  })

  it('normalises an absent rating to null, never undefined', () => {
    // `initial-rating` is bound straight to this. `undefined` would fall back to
    // the prop default rather than stating "no rating", which is the same value
    // by luck and not by rule.
    expect(assistantRow({ content: 'x' }).myRating).toBeNull()
    expect(assistantRow({ content: 'x', my_rating: null }).myRating).toBeNull()
    expect(assistantRow({ content: 'x', my_rating: 'down' }).myRating).toBe('down')
  })

  it('carries the voice-call fields the thread folds spoken turns by', () => {
    // ent#534's grouping reads these. A mapper that dropped them would silently
    // un-fold a call's transcript into loose messages.
    const row = assistantRow({ content: 'x', source: 'voice', voice_call_id: 'c1' })
    expect(row).toMatchObject({ source: 'voice', voiceCallId: 'c1' })
  })
})

describe('#2580 — replyFromHistory reads the row the server wrote', () => {
  const hist = (n, extra = {}) => Array.from({ length: n }, (_, i) => ({
    role: 'assistant', content: `a${i}`, id: `m${i}`, cost: i, ...extra,
  }))

  it('returns the newest assistant row WITH its id once it differs from the baseline', () => {
    const before = replyBaseline(hist(1))
    const reply = replyFromHistory([...hist(2), { role: 'user', content: 'q' }], before)
    expect(reply).toEqual({ response: 'a1', cost: 1, id: 'm1', myRating: null })
  })

  it('carries an existing rating through', () => {
    const rows = [{ role: 'assistant', content: 'a', id: 'm', cost: null, my_rating: 'up' }]
    expect(replyFromHistory(rows, replyBaseline([]))).toMatchObject({ id: 'm', myRating: 'up' })
  })

  it('returns null while the newest reply is still the baseline — the caller keeps waiting', () => {
    // The baseline is taken before dispatch. Without this the newest row is the
    // PREVIOUS turn's reply, and the poll would return it as this turn's answer
    // — showing the same message twice and attaching a thumb to the wrong one.
    expect(replyFromHistory(hist(1), replyBaseline(hist(1)))).toBeNull()
    expect(replyFromHistory(hist(0), replyBaseline(hist(0)))).toBeNull()
    expect(replyFromHistory(hist(3), replyBaseline(hist(3)))).toBeNull()
  })

  it('reads only assistant rows, so the user’s own turn never satisfies it', () => {
    // The user's half is persisted BEFORE the turn runs, so a naive length test
    // would report a reply the moment the question was saved.
    const rows = [{ role: 'assistant', content: 'old', id: 'm0' },
                  { role: 'user', content: 'new question', id: 'u1' }]
    expect(replyFromHistory(rows, replyBaseline(rows.slice(0, 1)))).toBeNull()
  })

  it('survives a malformed or absent payload rather than throwing mid-poll', () => {
    // This runs inside a polling loop whose whole job is to be patient; an
    // exception here would abort a turn that is merely mid-flight.
    expect(replyFromHistory(undefined, replyBaseline([]))).toBeNull()
    expect(replyFromHistory(null, replyBaseline([]))).toBeNull()
    expect(replyFromHistory([null, undefined], replyBaseline([]))).toBeNull()
    expect(replyFromHistory(hist(1), undefined)).toMatchObject({ id: 'm0' })
    expect(replyBaseline(undefined)).toEqual({ id: null, count: 0 })
  })

  it('reports a row that genuinely has no id as null, not as a fabricated one', () => {
    // The history write is best-effort server-side. A client-invented id would
    // 404 against the ratings route, so "no row to point at" must survive —
    // and with no id to compare, the count is the fallback that still finds it.
    const rows = [{ role: 'assistant', content: 'a' }]
    expect(replyFromHistory(rows, replyBaseline([]))).toMatchObject({ response: 'a', id: null })
  })

  // #2694 — identity, not count. The history read became a window of typed
  // turns plus the spoken rows of the calls among them, so between the
  // baseline read and a poll the window can shift by a whole call.
  it('finds the reply when the window shifted and the count did not grow (#2694)', () => {
    const call = (i) => ({ role: 'assistant', content: `spoken ${i}`, id: `v${i}`, source: 'voice', voice_call_id: 'c1' })
    // baseline: an old call's 3 spoken replies + the previous typed reply
    const before = [call(0), call(1), call(2), { role: 'assistant', content: 'prev', id: 'm0' }]
    // poll: the call fell out of the window; the new reply arrived. Count of
    // assistant rows went 4 → 2 — a count would wait here forever.
    const after = [{ role: 'assistant', content: 'prev', id: 'm0' }, { role: 'user', content: 'q', id: 'u1' },
                   { role: 'assistant', content: 'new', id: 'm1' }]
    expect(replyFromHistory(after, replyBaseline(before))).toMatchObject({ id: 'm1', response: 'new' })
  })

  it('never mistakes a spoken reply for the typed one (#2694)', () => {
    const rows = [{ role: 'assistant', content: 'prev', id: 'm0' },
                  { role: 'assistant', content: 'said aloud', id: 'v9', source: 'voice', voice_call_id: 'c' }]
    expect(replyBaseline(rows)).toEqual({ id: 'm0', count: 1 })
    expect(replyFromHistory(rows, replyBaseline([{ role: 'assistant', content: 'prev', id: 'm0' }]))).toBeNull()
  })

  it('finds the reply right after a call, when the narrow baseline read held only spoken rows (#2694)', () => {
    // The production path after a call: the poll reads the newest 8 rows, and
    // after a ≥8-row call every one of them is spoken or the label — no typed
    // reply, no id to compare. The count fallback still finds the new reply,
    // and a poll that still holds only spoken rows keeps waiting.
    const call = (i, role = 'user') => ({ role, content: `said ${i}`, id: `v${i}`, source: 'voice', voice_call_id: 'c1' })
    const onlySpoken = [call(0), call(1, 'assistant'), call(2), call(3, 'assistant'),
                        { role: 'system', content: 'Voice call · 2 min', id: 'l1', source: 'voice', voice_call_id: 'c1' }]
    const before = replyBaseline(onlySpoken)
    expect(before).toEqual({ id: null, count: 0 })
    expect(replyFromHistory(onlySpoken, before)).toBeNull()
    const after = [...onlySpoken, { role: 'user', content: 'so what did we agree?', id: 'u1' },
                   { role: 'assistant', content: 'Friday', id: 'm1' }]
    expect(replyFromHistory(after, before)).toMatchObject({ id: 'm1', response: 'Friday' })
  })

  it('accepts the local thread rows (voiceCallId spelling) as a baseline on reattach (#2694)', () => {
    const local = [{ role: 'assistant', content: 'prev', id: 'm0', source: null, voiceCallId: null },
                   { role: 'assistant', content: 'aloud', id: 'v1', source: 'voice', voiceCallId: 'c' }]
    expect(replyBaseline(local).id).toBe('m0')
  })
})

describe('Workspace reply baseline follows the server-resolved thread', () => {
  it('keeps Main’s previous reply when dispatch begins without a session id', async () => {
    const previous = {
      id: 'm-previous', role: 'assistant', content: 'the prior answer', cost: null,
      created_at: '2026-09-11T16:00:00.000000Z', source: null, voice_call_id: null,
    }
    const fetchHistory = async (sessionId) => {
      expect(sessionId).toBeNull()
      return {
        sessionId: 'main-session', messages: [previous],
        inFlightExecutionId: null, inFlightWaitBudgetSeconds: null,
        lastTurnOutcome: null, truncated: false,
      }
    }

    const baseline = await portalUtils.readReplyBaseline?.(fetchHistory, null)

    expect(baseline).toEqual({ id: 'm-previous', count: 1 })
    expect(replyFromHistory([previous], baseline)).toBeNull()
  })
})

describe('#2580 — every path that builds a reply uses the shared rule', () => {
  it('routes all three construction sites through assistantRow', () => {
    // Three sites built this row: history load, a completed turn, and a
    // reattached turn. Two dropped the id. Sharing the mapper is what stops a
    // fix landing in one twin and not the other (the #2211 lesson).
    expect((CONV.match(/assistantRow\(/g) || []).length).toBeGreaterThanOrEqual(3)
    expect(CONV).toContain('replyFromHistory(data.messages, baseline)')
    // No hand-built assistant row may survive beside the mapper.
    expect(CONV).not.toMatch(/push\(\{\s*role: 'assistant'/)
  })

  it('spreads the id through the reattach and the synchronous fallback', () => {
    // The streaming path gets the id from the persisted read; the synchronous
    // fallback gets it from the server, which now returns `message_id` (it used
    // to mint the row id inline and discard it).
    expect(CONV).toContain('id: data.id || data.message_id')
    expect(CONV).toMatch(/assistantRow\(\{ content: data\.response, id: data\.id, my_rating: data\.myRating \}\)/)
  })

  it('KEEPS the id gate on the rating control', () => {
    // Deliberately still gated. Per the 2026-09-07 ledger entry, carry the flag
    // and the identifier together and let the consumer refuse an empty id — the
    // fix is to supply the id, never to relax the check. It is what correctly
    // withholds the thumbs from the one reply that truly has no row: one
    // delivered when the server could not persist it.
    expect(CONV).toMatch(/<PortalRating\s+v-if="item\.message\.id"/)
  })

  it('no longer tells the reader that a fresh reply waits for the next load', () => {
    // The old comment documented the defect as though it were a constraint. A
    // stale comment asserting the opposite of the code is how the next reader
    // concludes the behaviour is intended.
    expect(CONV).not.toContain('becomes rateable on the next load')
  })
})
