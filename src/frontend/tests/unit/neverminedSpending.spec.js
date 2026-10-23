/**
 * Pure rules behind the Nevermined spending panel (abilityai/trinity-enterprise#967).
 *
 * `utils/neverminedSpending.js` is what `NeverminedSpendingPanel.vue` executes
 * to turn the ent#966 GET shape into a state, a money string and a recovery
 * action; the panel's own mount spec proves the wiring. These run in node.
 */
import { describe, it, expect } from 'vitest'
import {
  spendingView,
  recoveryFor,
  formatCents,
  expiryParts,
  retryAfterSeconds,
  statusBadge,
  COPY,
  FORBIDDEN_WORDING,
  POSITIVE_WORDING,
} from '@/utils/neverminedSpending.js'
import { apiErrorCode } from '@/utils/apiError.js'

const LIVE = (over = {}) => ({
  source: 'nevermined', status: 'active',
  remaining_cents: 600, spending_limit_cents: 1000, spent_cents: 400,
  transactions_used: 2, max_transactions: 10, currency: 'USD',
  expires_at: '2026-11-15T00:00:00Z', fetched_at: '2026-10-23T01:00:00Z', error: null,
  ...over,
})
const BODY = (over = {}) => ({
  agent_name: 'a', configured: true, funded: false, delegation: null, live: null, history: [], ...over,
})
const FUNDED = (live = LIVE()) => BODY({
  funded: true,
  delegation: { delegation_id: 'dlg_12345678', payment_method_id: 'pm_12345678', provider: 'stripe', environment: 'sandbox', created_by: 'o@example.com', created_at: '2026-10-20T00:00:00Z' },
  live,
})
const view = (data, over = {}) => spendingView({ data, hasLoaded: true, error: null, canEdit: true, agentStatus: 'running', ...over })

describe('spendingView', () => {
  it('is loading before the first read lands, failed when it failed', () => {
    expect(spendingView({ hasLoaded: false }).state).toBe('loading')
    expect(spendingView({ hasLoaded: false, error: 'boom' }).state).toBe('failed')
  })

  it('maps the GET shape onto the five ready states', () => {
    expect(view(BODY({ configured: false })).state).toBe('not_configured')
    expect(view(BODY()).state).toBe('not_funded')
    expect(view(FUNDED()).state).toBe('funded')
    expect(view(FUNDED(LIVE({ source: 'unavailable', status: 'unknown' }))).state).toBe('funded_unavailable')
    expect(view(FUNDED(LIVE({ status: 'unknown' }))).state).toBe('funded_unavailable')
    expect(view(FUNDED(null)).state).toBe('funded_unavailable')
  })

  it('a failed refresh with data on screen is the ready state plus stale', () => {
    const v = view(FUNDED(), { error: 'boom' })
    expect(v.state).toBe('funded')
    expect(v.stale).toBe(true)
  })

  it('offers Enroll only to the owner tier, on a running agent, when not funded', () => {
    expect(view(BODY()).canEnroll).toBe(true)
    expect(view(BODY(), { canEdit: false }).canEnroll).toBe(false)
    expect(view(BODY(), { agentStatus: 'stopped' }).canEnroll).toBe(false)
    expect(view(BODY(), { agentStatus: 'stopped' }).enrollBlocked).toBe('not_running')
    expect(view(FUNDED()).canEnroll).toBe(false)
  })

  it('offers Revoke only to the owner tier on a funded agent — never a replace', () => {
    expect(view(FUNDED()).canRevoke).toBe(true)
    expect(view(FUNDED(LIVE({ source: 'unavailable', status: 'unknown' }))).canRevoke).toBe(true)
    expect(view(FUNDED(), { canEdit: false }).canRevoke).toBe(false)
    expect(view(BODY()).canRevoke).toBe(false)
    expect(view(FUNDED()).canEnroll).toBe(false)
  })

  it('shows the pending banner only on a not-funded agent with a pending mandate', () => {
    expect(view(BODY(), { hasPending: true }).showPendingBanner).toBe(true)
    expect(view(FUNDED(), { hasPending: true }).showPendingBanner).toBe(false)
    expect(view(BODY()).showPendingBanner).toBe(false)
  })
})

describe('formatCents', () => {
  it('formats cents as money in the reported currency', () => {
    expect(formatCents(1234, 'USD')).toBe('$12.34')
    expect(formatCents(0, 'USD')).toBe('$0.00')
  })
  it('never assumes USD and never invents a number', () => {
    expect(formatCents(1234, null)).toBe('12.34 (currency not reported)')
    expect(formatCents(null, 'USD')).toBe('—')
    expect(formatCents(undefined, 'USD')).toBe('—')
    expect(formatCents(Number.NaN, 'USD')).toBe('—')
  })
  it('falls back to a plain amount for a code Intl does not know', () => {
    expect(formatCents(500, 'NOTACODE')).toBe('5.00 NOTACODE')
  })
})

describe('expiryParts', () => {
  const now = Date.parse('2026-10-23T00:00:00Z')
  it('future → "in N days" with an absolute time for the hover', () => {
    const p = expiryParts('2026-11-15T00:00:00Z', now)
    expect(p.relative).toBe('in 23 days')
    expect(p.past).toBe(false)
    expect(p.absolute).not.toBe('')
  })
  it('past → "expired N ago"', () => {
    const p = expiryParts('2026-10-20T00:00:00Z', now)
    expect(p.relative).toBe('expired 3 days ago')
    expect(p.past).toBe(true)
  })
  it('hours and minutes agree with their count', () => {
    expect(expiryParts('2026-10-23T01:00:00Z', now).relative).toBe('in 1 hour')
    expect(expiryParts('2026-10-23T00:05:00Z', now).relative).toBe('in 5 minutes')
  })
  it('null or unparsable → "—"', () => {
    expect(expiryParts(null, now)).toEqual({ relative: '—', absolute: '', past: false })
    expect(expiryParts('not a date', now).relative).toBe('—')
  })
})

describe('recoveryFor', () => {
  it('a named code selects the recovery action', () => {
    expect(recoveryFor('agent_not_running', 409, 'bind')).toBe('retry_bind')
    expect(recoveryFor('delegation_active', 409, 'bind')).toBe('reread')
    expect(recoveryFor('bind_in_progress', 409, 'bind')).toBe('reread')
    expect(recoveryFor('nevermined_not_configured', 409, 'mint')).toBe('reread')
    expect(recoveryFor('no_active_delegation', 404, 'revoke')).toBe('reread')
    expect(recoveryFor('delegation_not_found', 422, 'bind')).toBe('new_mandate')
    expect(recoveryFor('delegation_inactive', 422, 'bind')).toBe('new_mandate')
    expect(recoveryFor('email_required', 422, 'mint')).toBe('none')
  })
  it('upstream trouble retries the step it happened in', () => {
    for (const code of ['nevermined_busy', 'nevermined_unreachable', 'nevermined_rejected', 'agent_unreachable']) {
      expect(recoveryFor(code, 503, 'mint')).toBe('retry_mint')
      expect(recoveryFor(code, 503, 'bind')).toBe('retry_bind')
      expect(recoveryFor(code, 503, 'revoke')).toBe('retry_revoke')
      expect(recoveryFor(code, 503, 'read')).toBe('reread')
    }
  })
  it('with no code (429 string detail, network) the status decides', () => {
    expect(recoveryFor(null, 429, 'bind')).toBe('retry_bind')
    expect(recoveryFor(null, 429, 'mint')).toBe('retry_mint')
    expect(recoveryFor(null, 500, 'read')).toBe('reread')
    expect(recoveryFor(null, undefined, 'read')).toBe('reread')
    expect(recoveryFor(null, 422, 'bind')).toBe('new_mandate')
    expect(recoveryFor(null, 403, 'bind')).toBe('none')
  })
})

describe('retryAfterSeconds', () => {
  const err = (status, headers = {}) => ({ response: { status, headers } })
  it('reads the lower-cased Retry-After header on 429/503', () => {
    expect(retryAfterSeconds(err(429, { 'retry-after': '17' }))).toBe(17)
    expect(retryAfterSeconds(err(503, { 'retry-after': '5' }))).toBe(5)
  })
  it('defaults to 5 s when the header is missing or junk', () => {
    expect(retryAfterSeconds(err(429))).toBe(5)
    expect(retryAfterSeconds(err(503, { 'retry-after': 'soon' }))).toBe(5)
  })
  it('is null for a status that asks for no wait', () => {
    expect(retryAfterSeconds(err(409, { 'retry-after': '9' }))).toBeNull()
    expect(retryAfterSeconds({})).toBeNull()
  })
})

describe('apiErrorCode', () => {
  it('returns detail.code for a named refusal and null otherwise', () => {
    expect(apiErrorCode({ response: { data: { detail: { code: 'delegation_active', message: 'x' } } } })).toBe('delegation_active')
    expect(apiErrorCode({ response: { data: { detail: 'Too many requests. Try again in 3 seconds.' } } })).toBeNull()
    expect(apiErrorCode({ response: { data: { detail: [{ msg: 'bad', loc: ['body', 'x'] }] } } })).toBeNull()
    expect(apiErrorCode({ response: { data: { detail: { message: 'no code' } } } })).toBeNull()
    expect(apiErrorCode(new Error('network'))).toBeNull()
  })
})

describe('statusBadge', () => {
  it('encodes each status in a glyph as well as a colour', () => {
    const glyphs = new Set()
    for (const s of ['active', 'exhausted', 'expired', 'revoked', 'unknown']) {
      const b = statusBadge(s)
      expect(b.glyph).toBeTruthy()
      expect(b.label).toBeTruthy()
      glyphs.add(b.glyph)
    }
    expect(glyphs.size).toBe(5)
    expect(statusBadge('active').variant).toBe('success')
    expect(statusBadge('something_new').label).toBe('something_new')
  })
})

describe('COPY — Nevermined holds the card (ent#932)', () => {
  const strings = Object.entries(COPY)

  it('every string names Nevermined', () => {
    expect(strings.length).toBeGreaterThan(10)
    for (const [key, s] of strings) expect(s, key).toMatch(/Nevermined/)
  })

  it('no string reads as Trinity holding a card or settling money', () => {
    for (const [key, s] of strings) expect(FORBIDDEN_WORDING.test(s.split(/\s+/).join(' ')), key).toBe(false)
  })

  it('the mirrored regex catches the phrasing the backend guard catches', () => {
    for (const bad of ['Trinity charges your card', 'Trinity holds the card', 'Trinity stores your card', 'Trinity settles every purchase', 'Trinity acts as the merchant']) {
      expect(FORBIDDEN_WORDING.test(bad), bad).toBe(true)
    }
  })

  it('carries the positive literal the wording guard requires', () => {
    expect(strings.some(([, s]) => POSITIVE_WORDING.test(s))).toBe(true)
    expect(COPY.subtitle).toContain('Nevermined holds the card')
  })
})
