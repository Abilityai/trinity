/**
 * trinity-enterprise#1221 — a message the agent sent between turns is never a
 * turn's answer.
 *
 * `send_message(channel="web")` writes an assistant row marked
 * `source: 'proactive'` into the person's Main. A member waiting on a turn in
 * Main polls for "the newest typed assistant row that differs from the
 * baseline"; the old predicate excluded only `source === 'voice'`, so a
 * proactive row (or a completion report) landing during the wait was shown as
 * that turn's reply. Typed means `!source`, matching the backend's
 * `_TYPED = "source IS NULL"`.
 */
import { describe, it, expect } from 'vitest'
import { replyFromHistory, replyBaseline, latestTypedReply } from '../../src/components/portal/portalUtils.js'

const q = (id) => ({ id, role: 'user', content: id, source: null })
const r = (id, source = null) => ({ id, role: 'assistant', content: id, source })

describe('a sourced assistant row is not a typed reply (ent#1221)', () => {
  it('a proactive row landing during the wait is not the answer', () => {
    const baseline = replyBaseline([q('q0'), r('r0')])
    const rows = [q('q0'), r('r0'), q('q1'), r('p1', 'proactive')]
    expect(replyFromHistory(rows, baseline)).toBeNull()
  })

  it('the count fallback ignores it too', () => {
    const baseline = replyBaseline([q('q0'), { role: 'assistant', content: 'x', source: null }])
    const rows = [q('q0'), { role: 'assistant', content: 'x', source: null },
                  { role: 'assistant', content: 'between turns', source: 'proactive' }]
    expect(replyFromHistory(rows, baseline)).toBeNull()
  })

  it('the real reply after it is still found', () => {
    const baseline = replyBaseline([q('q0'), r('r0')])
    const rows = [q('q0'), r('r0'), q('q1'), r('p1', 'proactive'), r('r1')]
    expect(replyFromHistory(rows, baseline)).toMatchObject({ id: 'r1' })
  })

  it('a completion report is not a typed reply either', () => {
    expect(latestTypedReply([r('r0'), r('c1', 'completion:done')])).toMatchObject({ id: 'r0' })
  })
})
