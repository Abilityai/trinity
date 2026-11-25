// abilityai/trinity-enterprise#1391: the first session's email offer is one confirm card.
// Pinned by running the card: it is accepted only for an agent and schedule the page read,
// it posts no address (the instance uses the caller's own), and it reports done only once
// the schedule reads back as emailed to this person (the read never carries an address).
import { describe, it, expect, vi } from 'vitest'
import { acceptCards, cardLabel, cardNote, runCard, outcomeLine } from '@/utils/assistantCards'

const fleet = [{ name: 'acme-scout', schedules: [{ id: 's1', name: 'Weekly reviews' }] }]
const card = { action: 'email-results', agent: 'acme-scout', schedule_id: 's1' }
const http = (toMe = true, over = {}) => ({
  get: vi.fn(async () => ({ data: [{ id: 's1', enabled: true, email_results: toMe, email_results_to_me: toMe }] })),
  post: vi.fn(async () => ({ data: { email_results: toMe, email_results_to_me: toMe } })),
  ...over,
})

describe('the email-results card', () => {
  it('is accepted only for a schedule the page read, and never with an address in it', () => {
    expect(acceptCards([card], fleet)).toEqual([card])
    expect(acceptCards([{ ...card, schedule_id: 'nope' }], fleet)).toEqual([])
    expect(acceptCards([{ ...card, agent: 'ghost' }], fleet)).toEqual([])
    expect(acceptCards([{ ...card, email: 'eve@example.com' }], fleet)).toEqual([])
  })

  it('says what will happen in words', () => {
    expect(cardLabel(card, fleet)).toBe('Email each result of "Weekly reviews" to you')
    expect(cardNote(card)).toMatch(/your own email address/)
    // names the control that exists for turning it off (SchedulesPanel's button)
    expect(cardNote(card)).toMatch(/"Stop emailing"/)
  })

  it('posts to the email-me route with no address and is done once it reads back set', async () => {
    const h = http()
    expect(await runCard(h, card, fleet)).toEqual({ kind: 'done' })
    expect(h.post).toHaveBeenCalledWith('/api/agents/acme-scout/schedules/s1/email-me', { enabled: true })
    expect(JSON.stringify(h.post.mock.calls[0][1])).not.toMatch(/@/)
  })

  it('is not "done" when the schedule reads back as not emailed to this person', async () => {
    expect((await runCard(http(false), card, fleet)).kind).toBe('unsure')
  })

  it('passes on why the instance refused, so the assistant can say it', async () => {
    const refused = Object.assign(new Error('x'), { response: { status: 502, data: { detail: { code: 'email_not_sent', message: 'The email could not be sent.' } } } })
    const h = http(false, { post: vi.fn(async () => { throw refused }) })
    const out = await runCard(h, card, fleet)
    expect(out.kind).toBe('unsure')
    const declined = Object.assign(new Error('x'), { response: { status: 422, data: { detail: { code: 'no_email_address', message: 'Your account has no email address.' } } } })
    const r = await runCard(http(false, { post: vi.fn(async () => { throw declined }) }), card, fleet)
    expect(outcomeLine(r.kind, cardLabel(card, fleet), r.reason)).toBe(
      'That didn\'t work: Email each result of "Weekly reviews" to you. Your account has no email address.')
  })
})
