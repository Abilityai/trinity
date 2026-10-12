/**
 * #3166 — a turn shows its OWN reply, and a reload puts each reply under its
 * own question.
 *
 * Two turns on one thread (the chat open in two tabs): the second tab waited for
 * "the newest reply that differs from the baseline", so a reply from the other
 * turn that landed first was shown as its own. Every row now carries the
 * execution id of the turn that wrote it. When the server sends that field, a
 * turn accepts only the row with its own id — a row with no id (a completion
 * report) is never "maybe ours". Rows from an older backend (no field at all)
 * keep the old matching.
 */
import { describe, it, expect } from 'vitest'
import { replyFromHistory, replyBaseline, pairRepliesWithQuestions, assistantRow } from '../../src/components/portal/portalUtils.js'

const q = (id, execution_id, content = id) => ({ id, role: 'user', content, source: null, execution_id })
const r = (id, execution_id, content = id) => ({ id, role: 'assistant', content, source: null, execution_id })

describe('replyFromHistory with the turn id (#3166)', () => {
  it('does not accept a newer reply from a different execution', () => {
    const baseline = replyBaseline([q('q0', 'e0'), r('r0', 'e0')])
    const rows = [q('q0', 'e0'), r('r0', 'e0'), q('qA', 'eA'), q('qB', 'eB'), r('rA', 'eA', '1 … 15')]
    expect(replyFromHistory(rows, baseline, 'eB')).toBeNull()
  })

  it('finds its own reply even when it is not the newest row', () => {
    const baseline = replyBaseline([])
    const rows = [q('qB', 'eB'), r('rB', 'eB', 'mine'), r('rA', 'eA', 'theirs')]
    expect(replyFromHistory(rows, baseline, 'eB')).toMatchObject({ id: 'rB', response: 'mine' })
  })

  it('never takes a row with no execution id (a completion report) as its reply', () => {
    const baseline = replyBaseline([r('r0', 'e0')])
    const rows = [r('r0', 'e0'), q('qB', 'eB'), r('rep', null, 'report')]
    expect(replyFromHistory(rows, baseline, 'eB')).toBeNull()
  })

  it('keeps the old matching for rows from a backend without the field', () => {
    const legacy = (id, role) => ({ id, role, content: id, source: null })
    const baseline = replyBaseline([legacy('r0', 'assistant')])
    const rows = [legacy('r0', 'assistant'), legacy('q1', 'user'), legacy('r1', 'assistant')]
    expect(replyFromHistory(rows, baseline, 'eB')).toMatchObject({ id: 'r1' })
    expect(replyFromHistory([legacy('r0', 'assistant')], baseline, 'eB')).toBeNull()
  })

  it('keeps the old matching when the caller has no execution id', () => {
    const baseline = replyBaseline([r('r0', 'e0')])
    expect(replyFromHistory([r('r0', 'e0'), r('r1', 'e1')], baseline)).toMatchObject({ id: 'r1' })
  })

  it('ignores a spoken row carrying the same id', () => {
    const rows = [q('qB', 'eB'), { ...r('v', 'eB'), source: 'voice' }]
    expect(replyFromHistory(rows, replyBaseline([]), 'eB')).toBeNull()
  })
})

describe('pairRepliesWithQuestions (#3166)', () => {
  const ids = (rows) => rows.map((m) => m.id)

  it('puts each reply under its own question when two turns interleave', () => {
    const rows = [q('qA', 'eA'), q('qB', 'eB'), r('rA', 'eA'), r('rB', 'eB')]
    expect(ids(pairRepliesWithQuestions(rows))).toEqual(['qA', 'rA', 'qB', 'rB'])
  })

  it('leaves an already-ordered thread alone', () => {
    const rows = [q('qA', 'eA'), r('rA', 'eA'), q('qB', 'eB'), r('rB', 'eB')]
    expect(ids(pairRepliesWithQuestions(rows))).toEqual(['qA', 'rA', 'qB', 'rB'])
  })

  it('leaves a reply in place when its question is outside the window or unstamped', () => {
    const rows = [q('qB', null), r('rA', 'eA'), r('rB', 'eB')]
    expect(ids(pairRepliesWithQuestions(rows))).toEqual(['qB', 'rA', 'rB'])
  })

  it('does not move a reply across a voice call', () => {
    const v = { id: 'v1', role: 'user', content: 'said', source: 'voice', voice_call_id: 'c1' }
    const rows = [q('qA', 'eA'), v, r('rA', 'eA')]
    expect(ids(pairRepliesWithQuestions(rows))).toEqual(['qA', 'v1', 'rA'])
  })

  it('leaves a retried question unanswered when its reply belongs to the retry', () => {
    // The failed turn's row was moved to the retry (eR), so the reply sits under it.
    const rows = [q('q', 'eR'), q('qX', 'eX'), r('rR', 'eR')]
    expect(ids(pairRepliesWithQuestions(rows))).toEqual(['q', 'rR', 'qX'])
  })

  it('tolerates junk', () => {
    expect(pairRepliesWithQuestions(null)).toEqual([])
    expect(pairRepliesWithQuestions([null, r('r', 'e')]).length).toBe(2)
  })
})

describe('assistantRow carries the execution id (#3166)', () => {
  it('maps execution_id', () => {
    expect(assistantRow({ content: 'x', execution_id: 'e1' }).executionId).toBe('e1')
    expect(assistantRow({ content: 'x' }).executionId).toBeNull()
  })
})
