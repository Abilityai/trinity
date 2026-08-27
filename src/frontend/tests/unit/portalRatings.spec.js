/**
 * ent#366 — rating a message and a deliverable.
 *
 * The decidable parts are pure (`portalUtils.js`) because this project runs
 * vitest in `environment: 'node'` with no mount harness. The source assertions
 * at the bottom cover what only source can: that the control appears on the
 * agent's persisted messages and on deliverable cards, that a failed rating is
 * shown rather than swallowed, and that the tally renders as counts.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import {
  ratingLabels,
  nextRating,
  shouldPromptForComment,
  ratingTallyText,
  feedbackAcknowledgement,
  RATINGS_EMPTY_TEXT,
  RATINGS_UNAVAILABLE_TEXT,
  FEEDBACK_SENT_TEXT,
  FEEDBACK_RECORDED_TEXT,
} from '../../src/components/portal/portalUtils'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const rating = read('../../src/components/portal/PortalRating.vue')
const conversation = read('../../src/components/portal/PortalConversation.vue')
const deliverables = read('../../src/components/portal/PortalDeliverables.vue')
const agentPage = read('../../src/components/portal/PortalAgentPage.vue')
const store = read('../../src/stores/clientPortal.js')

describe('the words differ by what is being judged', () => {
  it('asks about an answer for a message and about work for a deliverable', () => {
    expect(ratingLabels('message').down).toBe('Not helpful')
    expect(ratingLabels('deliverable').down).toBe('Not what I needed')
  })

  it('falls back rather than rendering undefined on an unknown kind', () => {
    expect(ratingLabels('something-new').up).toBe('Helpful')
    expect(ratingLabels(undefined).up).toBe('Helpful')
  })
})

describe('clicking', () => {
  it('changing your mind switches the rating', () => {
    expect(nextRating('up', 'down')).toBe('down')
    expect(nextRating(null, 'up')).toBe('up')
  })

  it('clicking the rating you already gave is a no-op, not an un-rate', () => {
    // There is no retract endpoint; clearing locally would show a state the
    // server does not have.
    expect(nextRating('up', 'up')).toBeNull()
    expect(nextRating('down', 'down')).toBeNull()
  })

  it('only a negative rating asks for words', () => {
    // Asking a happy person to explain themselves is how you stop getting either.
    expect(shouldPromptForComment('down')).toBe(true)
    expect(shouldPromptForComment('up')).toBe(false)
    expect(shouldPromptForComment(null)).toBe(false)
  })
})

describe('the tally is counts, never a percentage (AC #4)', () => {
  it('shows both figures so the denominator is on screen', () => {
    expect(ratingTallyText({ up: 3, down: 1, total: 4 })).toBe('3 helpful · 1 not helpful')
  })

  it('uses the deliverable vocabulary when asked about deliverables', () => {
    expect(ratingTallyText({ up: 2, down: 0, total: 2 }, 'deliverable'))
      .toBe('2 useful · 0 not what i needed')
  })

  it('never renders a percentage', () => {
    for (const t of [{ up: 1, down: 0, total: 1 }, { up: 0, down: 1, total: 1 }, { up: 7, down: 3, total: 10 }]) {
      expect(ratingTallyText(t)).not.toContain('%')
    }
  })

  it('distinguishes "nobody rated" from "we could not read the ratings"', () => {
    expect(ratingTallyText({ up: 0, down: 0, total: 0 })).toBe(RATINGS_EMPTY_TEXT)
    expect(ratingTallyText({ unavailable: true })).toBe(RATINGS_UNAVAILABLE_TEXT)
    expect(ratingTallyText(null)).toBe(RATINGS_UNAVAILABLE_TEXT)
    expect(ratingTallyText(undefined)).toBe(RATINGS_UNAVAILABLE_TEXT)
  })
})

describe('what the person is told afterwards', () => {
  it('claims a hand-off only when there was one (AC #6)', () => {
    expect(feedbackAcknowledgement('dispatched')).toBe(FEEDBACK_SENT_TEXT)
    expect(feedbackAcknowledgement('skill_not_installed')).toBe(FEEDBACK_RECORDED_TEXT)
    expect(feedbackAcknowledgement(null)).toBe(FEEDBACK_RECORDED_TEXT)
  })

  it('never says nothing happened — the comment is durable either way', () => {
    for (const v of ['dispatched', 'skill_not_installed', null, undefined]) {
      expect(feedbackAcknowledgement(v)).toMatch(/Thanks/)
    }
  })
})

describe('what only source can answer', () => {
  it('rates the agent’s persisted messages, and only those', () => {
    // A reply composed locally during a live turn has no row id yet, so a thumb
    // would have nothing to point at.
    expect(conversation).toContain('target-kind="message"')
    expect(conversation).toMatch(/<PortalRating[\s\S]{0,200}v-if="m\.id"/)
  })

  it('carries the caller’s own rating back from history', () => {
    expect(conversation).toContain('myRating: m.my_rating || null')
    expect(conversation).toContain(':initial-rating="m.myRating"')
  })

  it('rates deliverables with the deliverable vocabulary', () => {
    expect(deliverables).toContain('target-kind="deliverable"')
  })

  it('shows a failed rating next to its own control', () => {
    // A rating that silently failed leaves the person believing they were heard.
    expect(rating).toContain('error.value = e?.response?.data?.detail')
    expect(rating).not.toContain('console.error')
  })

  it('does not swallow the rating error in the store', () => {
    // Deliberately unlike the fail-soft deliverables read.
    expect(store).toMatch(/async submitRating\([\s\S]{0,400}portalHttp\.post/)
    expect(store).not.toMatch(/async submitRating\([\s\S]{0,400}catch \{[\s\S]{0,60}return null/)
  })

  it('renders the agent-page tally as two counts', () => {
    expect(agentPage).toContain('ratings.up')
    expect(agentPage).toContain('ratings.down')
    expect(agentPage).not.toMatch(/pct\(ratings/)
  })
})
