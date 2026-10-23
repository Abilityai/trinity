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
  ACTIONS,
  FORBIDDEN_WORDING,
  POSITIVE_WORDING,
  unwrapSuccess,
  unwrapSession,
  inlineWidth,
  envNoteTone,
  embedErrorMessage,
  revokeMessage,
  persistPending,
  readPending,
  clearPending,
  PENDING_PREFIX,
  buildRedirectUrl,
  newNonce,
  persistNonce,
  readNonce,
  clearNonce,
  NONCE_PREFIX,
  redirectReturn,
  hasRedirectReturn,
  isFrameBlocked,
  REDIRECT_MAX_AGE_MS,
  RETURN_MISMATCH,
  FALLBACK_CAUSES,
  neverReadyMessage,
  retryHoldLabel,
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
    // Addendum 2026-10-23 (F4): a hand-set NVM_API_KEY blocks the bind; the
    // admin removes it and binds the same ids — the same recovery.
    expect(recoveryFor('agent_key_present', 409, 'bind')).toBe('retry_bind')
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

const WALLET = '0x' + 'a'.repeat(40)
const SESSION = { sessionToken: 'tok', userId: 'u1', userWallet: WALLET, apiKeyHash: 'h', expiresAt: '2099-01-01T00:00:00Z' }

describe('unwrapSuccess — setupCard ids → the bind body', () => {
  it('accepts both ids matching the bind pattern', () => {
    expect(unwrapSuccess({ delegationId: 'dlg_12345678', paymentMethodId: 'pm_abcdefgh' }))
      .toEqual({ delegationId: 'dlg_12345678', paymentMethodId: 'pm_abcdefgh' })
  })
  it('paymentMethodId is optional on the bind', () => {
    expect(unwrapSuccess({ delegationId: 'dlg_12345678' })).toEqual({ delegationId: 'dlg_12345678' })
  })
  it('anything off-pattern is null — no POST is made with it', () => {
    expect(unwrapSuccess(null)).toBeNull()
    expect(unwrapSuccess({})).toBeNull()
    expect(unwrapSuccess({ delegationId: 'short' })).toBeNull()
    expect(unwrapSuccess({ delegationId: 'dlg 12345678' })).toBeNull()
    expect(unwrapSuccess({ delegationId: 'x'.repeat(129) })).toBeNull()
    expect(unwrapSuccess({ delegationId: 'dlg_12345678', paymentMethodId: 'pm/../x1' })).toBeNull()
    expect(unwrapSuccess({ delegationId: 'dlg_12345678', paymentMethodId: 42 })).toBeNull()
  })
  it('8 and 128 characters are the inclusive bounds', () => {
    expect(unwrapSuccess({ delegationId: 'a'.repeat(8) })).not.toBeNull()
    expect(unwrapSuccess({ delegationId: 'a'.repeat(128) })).not.toBeNull()
    expect(unwrapSuccess({ delegationId: 'a'.repeat(7) })).toBeNull()
  })
})

describe('unwrapSession — TD-7 tolerant read of the forwarded Nevermined body', () => {
  it('bare, .session and .data all yield the five keys the SDK checks', () => {
    expect(unwrapSession(SESSION)).toEqual(SESSION)
    expect(unwrapSession({ session: SESSION, extra: 1 })).toEqual(SESSION)
    expect(unwrapSession({ data: { ...SESSION, junk: 'x' } })).toEqual(SESSION)
  })
  it('a missing key, an empty string or a non-0x wallet is null', () => {
    expect(unwrapSession(null)).toBeNull()
    expect(unwrapSession({})).toBeNull()
    expect(unwrapSession({ ...SESSION, apiKeyHash: undefined })).toBeNull()
    expect(unwrapSession({ ...SESSION, sessionToken: '' })).toBeNull()
    expect(unwrapSession({ ...SESSION, userWallet: 'abc' })).toBeNull()
  })
})

describe('inlineWidth — the SDK clamps inline frames to [440, 720]', () => {
  it('clamps and floors', () => {
    expect(inlineWidth(300)).toBe(440)
    expect(inlineWidth(500.7)).toBe(500)
    expect(inlineWidth(1200)).toBe(720)
    expect(inlineWidth(Infinity)).toBe(720)
  })
})

describe('envNoteTone — keyed on the backend prefix only (addendum W3)', () => {
  it('maps the two prefixes', () => {
    expect(envNoteTone('agent_not_running — the Nevermined mandate is revoked …')).toBe('stopped')
    expect(envNoteTone('agent_unreachable — the Nevermined mandate is revoked …')).toBe('unreachable')
    expect(envNoteTone('something else')).toBe('other')
    expect(envNoteTone(null)).toBeNull()
  })
})

describe('embedErrorMessage — userMessage first, never the raw message', () => {
  it('prefers userMessage and falls back per code', () => {
    expect(embedErrorMessage({ code: 'NETWORK', message: 'ECONNRESET at upstream-host', userMessage: 'Try later' })).toBe('Try later')
    expect(embedErrorMessage({ code: 'UNAUTHORIZED', message: 'jwt bad' })).toBe(COPY.embedUnauthorized)
    expect(embedErrorMessage({ code: 'PAYMENT_NOT_CONFIRMED', message: 'x' })).toBe(COPY.embedPayment)
    expect(embedErrorMessage({ code: 'UNKNOWN', message: 'stack' })).toBe(COPY.embedUnknown)
    expect(embedErrorMessage({ code: 'NETWORK', message: 'secret detail' })).not.toContain('secret')
  })
})

describe('revokeMessage — the §4.4 confirmation', () => {
  it('names Nevermined as the card holder and states the amount', () => {
    const m = revokeMessage('$6.00')
    expect(m).toContain('Nevermined holds the card')
    expect(m).toContain('The remaining $6.00 cannot be spent through this agent')
    expect(FORBIDDEN_WORDING.test(m)).toBe(false)
  })
  it('an unknown amount is not invented', () => {
    expect(revokeMessage('unknown')).toContain('The remaining balance cannot be spent')
    expect(revokeMessage('—')).not.toContain('—')
  })
  it('the ruled action labels are wording-guard clean', () => {
    expect(ACTIONS.revokeConfirm).toBe('Revoke mandate')
    expect(ACTIONS.retryBind).toBe('Retry bind')
    for (const [k, v] of Object.entries(ACTIONS)) expect(FORBIDDEN_WORDING.test(v), k).toBe(false)
  })
})

describe('pending — a Nevermined mandate not yet bound, per agent', () => {
  function memStore() {
    const m = new Map()
    return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), m }
  }
  const REC = { agent: 'a', delegationId: 'dlg_12345678', paymentMethodId: 'pm_abcdefgh', provider: 'stripe', at: 1 }

  it('persist → read round-trips under nvm-pending:<agent>', () => {
    const s = memStore()
    expect(persistPending(REC, s)).toBe(true)
    expect([...s.m.keys()]).toEqual([`${PENDING_PREFIX}a`])
    expect(PENDING_PREFIX).toBe('nvm-pending:')
    expect(readPending('a', s)).toEqual(REC)
  })
  it('another agent reads nothing; clear removes it', () => {
    const s = memStore()
    persistPending(REC, s)
    expect(readPending('b', s)).toBeNull()
    clearPending('a', s)
    expect(readPending('a', s)).toBeNull()
  })
  it('a malformed or mismatched record is null, never thrown', () => {
    const s = memStore()
    s.setItem(`${PENDING_PREFIX}a`, '{not json')
    expect(readPending('a', s)).toBeNull()
    s.setItem(`${PENDING_PREFIX}a`, JSON.stringify({ ...REC, agent: 'b' }))
    expect(readPending('a', s)).toBeNull()
    s.setItem(`${PENDING_PREFIX}a`, JSON.stringify({ ...REC, delegationId: 'bad' }))
    expect(readPending('a', s)).toBeNull()
  })
  it('a storage that throws degrades to memory-only (false / null)', () => {
    const boom = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('blocked') }, removeItem() { throw new Error('blocked') } }
    expect(persistPending(REC, boom)).toBe(false)
    expect(readPending('a', boom)).toBeNull()
    expect(() => clearPending('a', boom)).not.toThrow()
    expect(persistPending(REC, null)).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Checkpoint C — redirect fallback, redirect return, Retry-After hold
// ---------------------------------------------------------------------------

function memStore() {
  const m = new Map()
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    raw: m,
  }
}

describe('buildRedirectUrl', () => {
  it("is Nevermined's hosted /cards/setup page with exactly the four params", () => {
    const url = new URL(buildRedirectUrl({
      sessionToken: 'tok en', returnUrl: 'https://trinity.example.com/agents/a', state: 'n-1', provider: 'stripe',
    }))
    expect(url.origin).toBe('https://embed.nevermined.app')
    expect(url.pathname).toBe('/cards/setup')
    expect([...url.searchParams.keys()]).toEqual(['sessionToken', 'returnUrl', 'state', 'provider'])
    expect(url.searchParams.get('sessionToken')).toBe('tok en')
    expect(url.searchParams.get('returnUrl')).toBe('https://trinity.example.com/agents/a')
    expect(url.searchParams.get('state')).toBe('n-1')
    expect(url.searchParams.get('provider')).toBe('stripe')
  })

  it('defaults provider to stripe', () => {
    const url = new URL(buildRedirectUrl({ sessionToken: 't', returnUrl: 'https://x/agents/a', state: 's' }))
    expect(url.searchParams.get('provider')).toBe('stripe')
  })
})

describe('newNonce / persistNonce / readNonce / clearNonce', () => {
  it('nonces are non-empty and distinct', () => {
    const a = newNonce()
    const b = newNonce()
    expect(typeof a).toBe('string')
    expect(a.length).toBeGreaterThanOrEqual(16)
    expect(a).not.toBe(b)
  })

  it('round-trips per agent; another agent reads null; clear removes it', () => {
    const store = memStore()
    expect(persistNonce('a', { nonce: 'n1', at: 100 }, store)).toBe(true)
    expect(store.raw.has(NONCE_PREFIX + 'a')).toBe(true)
    expect(readNonce('a', store)).toEqual({ nonce: 'n1', at: 100 })
    expect(readNonce('b', store)).toBeNull()
    clearNonce('a', store)
    expect(readNonce('a', store)).toBeNull()
  })

  it('malformed or absent storage reads null, never throws', () => {
    const store = memStore()
    store.setItem(NONCE_PREFIX + 'a', '{not json')
    expect(readNonce('a', store)).toBeNull()
    store.setItem(NONCE_PREFIX + 'a', JSON.stringify({ nonce: '', at: 1 }))
    expect(readNonce('a', store)).toBeNull()
    expect(readNonce('a', null)).toBeNull()
    expect(persistNonce('a', { nonce: 'x', at: 1 }, null)).toBe(false)
  })
})

describe('redirectReturn — every outcome', () => {
  const Q = { paymentMethodId: 'pm_12345678', delegationId: 'dlg_12345678', state: 'n1' }
  const NOW = 1_000_000

  it('ok: the stored nonce matches within 30 minutes → the ids', () => {
    expect(redirectReturn(Q, { nonce: 'n1', at: NOW - 1000 }, NOW)).toEqual({
      outcome: 'ok', delegationId: 'dlg_12345678', paymentMethodId: 'pm_12345678',
    })
  })

  it('state_mismatch: another nonce', () => {
    expect(redirectReturn(Q, { nonce: 'other', at: NOW }, NOW).outcome).toBe('state_mismatch')
  })

  it('no_pending: nothing stored in this tab', () => {
    expect(redirectReturn(Q, null, NOW).outcome).toBe('no_pending')
  })

  it('expired: older than 30 minutes (and a stored time in the future)', () => {
    expect(redirectReturn(Q, { nonce: 'n1', at: NOW - REDIRECT_MAX_AGE_MS - 1 }, NOW).outcome).toBe('expired')
    expect(redirectReturn(Q, { nonce: 'n1', at: NOW - REDIRECT_MAX_AGE_MS }, NOW).outcome).toBe('ok')
    expect(redirectReturn(Q, { nonce: 'n1', at: NOW + 60_000 }, NOW).outcome).toBe('expired')
  })

  it('incomplete: a missing key or an id off the bind pattern, before the nonce is consulted', () => {
    const stored = { nonce: 'n1', at: NOW }
    for (const k of ['paymentMethodId', 'delegationId', 'state']) {
      const q = { ...Q }
      delete q[k]
      expect(redirectReturn(q, stored, NOW).outcome, k).toBe('incomplete')
    }
    expect(redirectReturn({ ...Q, delegationId: 'bad id' }, stored, NOW).outcome).toBe('incomplete')
    expect(redirectReturn(null, stored, NOW).outcome).toBe('incomplete')
  })

  it('hasRedirectReturn: any one of the three keys counts', () => {
    expect(hasRedirectReturn({ tab: 'nevermined' })).toBe(false)
    expect(hasRedirectReturn({ state: 'x' })).toBe(true)
    expect(hasRedirectReturn(Q)).toBe(true)
    expect(hasRedirectReturn(undefined)).toBe(false)
  })
})

describe('isFrameBlocked', () => {
  it('true only for a frame-src violation', () => {
    expect(isFrameBlocked({ violatedDirective: 'frame-src' })).toBe(true)
    expect(isFrameBlocked({ violatedDirective: 'frame-src https://embed.nevermined.app' })).toBe(true)
    expect(isFrameBlocked({ violatedDirective: 'connect-src' })).toBe(false)
    expect(isFrameBlocked({})).toBe(false)
  })
})

describe('retryAfterSeconds feeds the Retry bind hold', () => {
  const err = (status, headers = {}) => ({ response: { status, headers } })
  it('429 / 503 hold for the header seconds; 409 holds nothing', () => {
    expect(retryAfterSeconds(err(429, { 'retry-after': '3' }))).toBe(3)
    expect(retryAfterSeconds(err(503, {}))).toBe(5)
    expect(retryAfterSeconds(err(409, { 'retry-after': '3' }))).toBeNull()
  })
  it('the button names the seconds left, then plain Retry bind', () => {
    expect(retryHoldLabel(3)).toBe('Retry bind (3 s)')
    expect(retryHoldLabel(0)).toBe(ACTIONS.retryBind)
  })
})

describe('checkpoint C copy stays inside the wording rules', () => {
  it('fallback causes and the never-ready line name Nevermined; nothing matches FORBIDDEN', () => {
    for (const c of FALLBACK_CAUSES) {
      expect(c).toMatch(/Nevermined/)
      expect(FORBIDDEN_WORDING.test(c), c).toBe(false)
    }
    const ready = neverReadyMessage('https://trinity.example.com')
    expect(ready).toMatch(/Nevermined/)
    expect(ready).toContain('https://trinity.example.com.')
    expect(FORBIDDEN_WORDING.test(ready)).toBe(false)
  })
  it('the redirect-return refusal is the ruling\'s sentence verbatim', () => {
    expect(RETURN_MISMATCH).toBe("The return did not match this browser's request. Nothing was bound.")
    expect(FORBIDDEN_WORDING.test(RETURN_MISMATCH)).toBe(false)
  })
})
