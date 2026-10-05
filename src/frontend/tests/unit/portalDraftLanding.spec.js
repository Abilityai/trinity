/**
 * ent#784 arm 3 — "drafts win": opening an agent lands on its newest draft.
 *
 * The operator's 2026-10-05 ruling settled the precedence a landing resolves
 * by, and this file owns the pure half of the arm it added: a link to a chat,
 * then `lastOpenSessionId` (ent#621), then the chat holding an unsent draft,
 * then a new chat. Before this the rule always answered a new chat, so a draft
 * typed into an existing chat was not where opening the agent took you — the
 * draft mark on the agent's row pointed at words you could not get back to by
 * clicking it (`e2e/workspace-drafts.spec.js:92` and `:116`).
 *
 * The candidate set is the one that lights the sidebar's mark. That is the
 * point of `isDraftedThread` being shared rather than copied, and it is why
 * the archived and room cases are asserted here: both sides have to agree, or
 * "the mark means click here to continue" is only true by coincidence.
 */
import { describe, it, expect } from 'vitest'
import { agentLanding } from '@/components/portal/portalUtils'
import {
  agentsWithDrafts, draftedLandingFor, isDraftedThread,
} from '@/components/portal/portalDrafts'

// As `fetchAllSessions` returns them, after the shell's `decorate()` has
// stamped `hasDraft` from the drafts store's key set: most recent first.
const row = (id, agent, extra = {}) => ({
  id, session_id: id, agent_name: agent, last_message_at: '2026-09-27T10:00:00Z', message_count: 2, ...extra,
})
const at = (text, updatedAt) => ({ text, updatedAt })

describe('agentLanding — arm 3, the chat holding a draft', () => {
  it('lands on the drafted thread instead of a new chat', () => {
    const threads = [row('s2', 'a'), row('s1', 'a', { hasDraft: true })]
    const drafts = { 'thread:s1': at('half a thought for A', 100) }
    expect(agentLanding({ agentName: 'a', threads, drafts }))
      .toEqual({ agentName: 'a', sessionId: 's1' })
  })

  it('lands on a new chat when the draft is the unsaved one', () => {
    // `new:<agent>` IS the new chat's key, so `sessionId: null` is not a
    // fallthrough here — it is the landing that restores those words.
    const threads = [row('s1', 'a')]
    expect(agentLanding({ agentName: 'a', threads, drafts: { 'new:a': at('for A', 100) } }))
      .toEqual({ agentName: 'a', sessionId: null })
  })

  it('picks the newest edit when several chats hold drafts', () => {
    const threads = [row('s2', 'a', { hasDraft: true }), row('s1', 'a', { hasDraft: true })]
    const drafts = { 'thread:s1': at('older', 100), 'thread:s2': at('newer', 200) }
    expect(agentLanding({ agentName: 'a', threads, drafts }).sessionId).toBe('s2')
    // …and the other way round, so this is reading `updatedAt` rather than
    // list order (which would make the assertion above pass for free).
    expect(agentLanding({
      agentName: 'a',
      threads,
      drafts: { 'thread:s1': at('newer', 300), 'thread:s2': at('older', 200) },
    }).sessionId).toBe('s1')
  })

  it('weighs the unsaved chat against the threads by the same clock', () => {
    const threads = [row('s1', 'a', { hasDraft: true })]
    expect(agentLanding({
      agentName: 'a', threads, drafts: { 'thread:s1': at('older', 100), 'new:a': at('newer', 200) },
    }).sessionId).toBeNull()
    expect(agentLanding({
      agentName: 'a', threads, drafts: { 'thread:s1': at('newer', 300), 'new:a': at('older', 200) },
    }).sessionId).toBe('s1')
  })

  it('resolves a tie to the newest listed thread, never at random', () => {
    const threads = [row('s2', 'a', { hasDraft: true }), row('s1', 'a', { hasDraft: true })]
    const drafts = { 'thread:s1': at('same', 100), 'thread:s2': at('same', 100), 'new:a': at('same', 100) }
    for (let i = 0; i < 5; i++) {
      expect(agentLanding({ agentName: 'a', threads, drafts }).sessionId).toBe('s2')
    }
  })

  it('never crosses agents', () => {
    const threads = [row('s9', 'b', { hasDraft: true }), row('s1', 'a')]
    const drafts = { 'thread:s9': at("b's words", 100) }
    expect(agentLanding({ agentName: 'a', threads, drafts }))
      .toEqual({ agentName: 'a', sessionId: null })
  })

  it('ignores an archived drafted chat, and so does the mark', () => {
    // Both halves, in one case, on purpose: a landing that refuses the row
    // while the row still carries the mark is the drift this arm exists to
    // prevent. Reset MOVES the draft off the row it archives, so nothing is
    // lost by refusing to invite the person back into an archive.
    const threads = [row('s1', 'a', { hasDraft: true, archived_at: '2026-09-28T10:00:00Z' })]
    const drafts = { 'thread:s1': at('in the archive', 100) }
    expect(agentLanding({ agentName: 'a', threads, drafts }))
      .toEqual({ agentName: 'a', sessionId: null })
    expect(agentsWithDrafts(threads, new Set())).toEqual(new Set())
  })

  it('ignores a room, and so does the mark', () => {
    const threads = [{ id: 'r1', is_room: true, agent_name: 'a', hasDraft: true }]
    const drafts = { 'room:r1': at('for the room', 100) }
    expect(agentLanding({ agentName: 'a', threads, drafts }))
      .toEqual({ agentName: 'a', sessionId: null })
    expect(agentsWithDrafts(threads, new Set())).toEqual(new Set())
  })

  it('every marked row is a landing candidate (the shared predicate)', () => {
    // The promise in words: a mark on the agent's row means clicking it lands
    // you on those words. So for each agent the mark names, the rule must
    // answer something other than "a new chat you have not typed in" — either
    // the drafted thread, or the unsaved chat that holds the draft.
    const threads = [
      row('s1', 'a', { hasDraft: true }),
      row('s2', 'b'),
      { id: 'r1', is_room: true, agent_name: 'a', hasDraft: true },
      row('s3', 'c', { hasDraft: true, archived_at: '2026-09-28T10:00:00Z' }),
    ]
    const drafts = {
      'thread:s1': at('for A', 100), 'new:b': at('for B', 90),
      'room:r1': at('room', 80), 'thread:s3': at('archived', 70),
    }
    const marked = agentsWithDrafts(threads, new Set(['b']))
    expect(marked).toEqual(new Set(['a', 'b']))
    for (const name of marked) {
      expect(draftedLandingFor({ agentName: name, threads, drafts })).not.toBeNull()
    }
    expect(draftedLandingFor({ agentName: 'c', threads, drafts })).toBeNull()
  })

  it('arm 2 still outranks it — ent#621 returns you where you were', () => {
    const threads = [row('s2', 'a'), row('s1', 'a', { hasDraft: true })]
    const drafts = { 'thread:s1': at('for A', 100) }
    expect(agentLanding({ agentName: 'a', threads, drafts, lastOpenSessionId: 's2' }).sessionId)
      .toBe('s2')
    // A stale id falls THROUGH to the drafts arm, not past it to a new chat.
    expect(agentLanding({ agentName: 'a', threads, drafts, lastOpenSessionId: 'gone' }).sessionId)
      .toBe('s1')
  })

  it('with no drafts the answer is still a new chat', () => {
    const threads = [row('s1', 'a')]
    expect(agentLanding({ agentName: 'a', threads, drafts: {} }))
      .toEqual({ agentName: 'a', sessionId: null })
    // And a caller that passes no map at all keeps the old behaviour rather
    // than throwing — the arm is disabled, not broken.
    expect(agentLanding({ agentName: 'a', threads }))
      .toEqual({ agentName: 'a', sessionId: null })
  })

  it('does not trust a stale stamp over the stored text', () => {
    // `hasDraft` is a projection; the map is the source. A row stamped after
    // the draft was sent must not win an empty landing.
    const threads = [row('s1', 'a', { hasDraft: true })]
    expect(draftedLandingFor({ agentName: 'a', threads, drafts: { 'thread:s1': at('   ', 100) } }))
      .toBeNull()
    expect(draftedLandingFor({ agentName: 'a', threads, drafts: {} })).toBeNull()
  })

  it('isDraftedThread is the predicate both sides read', () => {
    expect(isDraftedThread(row('s1', 'a', { hasDraft: true }))).toBe(true)
    expect(isDraftedThread(row('s1', 'a'))).toBe(false)
    expect(isDraftedThread(row('s1', 'a', { hasDraft: true, archived_at: 'x' }))).toBe(false)
    expect(isDraftedThread({ id: 'r', is_room: true, hasDraft: true, agent_name: 'a' })).toBe(false)
    expect(isDraftedThread({ id: 's', hasDraft: true })).toBe(false)
    expect(isDraftedThread(null)).toBe(false)
  })
})
