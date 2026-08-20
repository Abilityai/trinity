import { describe, it, expect } from 'vitest'
import {
  authFailureCount,
  headroomStatus,
  isTokenRejected,
  pressureHeadline,
  subscriptionPressureRows,
  subscriptionSeverity,
  warnReason,
  windowReadings,
} from '@/utils/subscriptionPressureTile'
import { pressureBadge } from '@/utils/subscriptionPressure'

/**
 * #2352 / #2353 — a rejected token is not a rate limit.
 *
 * The tile reported "rate-limited" for three states that are not throttling:
 * an auth failure (via the backend's kind-blind 2h predicate, #2352), a
 * provider token the probe could not authenticate with, and — one step further
 * down — a probe that errored outright (#2353). All three showed the same red
 * LIMIT chip, so an operator looking at a subscription with a dead credential
 * was told to wait out a window that was never full.
 *
 * These cases are the ones whose regression is SILENT: every one of them
 * renders a plausible row.
 */

const win = (o = {}) => ({ input_tokens: 0, output_tokens: 0, cost_usd: 0, message_count: 0, ...o })

function usage(o = {}) {
  return {
    window_5h: win(),
    window_7d: win(),
    agents: [],
    failure_events_24h: 0,
    failure_events_by_kind: {},
    rate_limited_now: false,
    source: 'observed',
    headroom: null,
    ...o,
  }
}

/**
 * A probe that came back with a verdict but no usable windows — the exact
 * payload shape a 401/403 or a transport failure produces (`_probe` returns
 * `{fetched_at, status}` only, and `decorate_usage` leaves `source` at
 * "observed" for any non-ok status).
 */
function probed(status, o = {}) {
  return usage({
    headroom: { five_hour: null, seven_day: null, snapshot_age_seconds: 30, status },
    ...o,
  })
}

describe('headroomStatus / isTokenRejected', () => {
  it('reads the probe verdict off the payload', () => {
    expect(headroomStatus(probed('invalid_token'))).toBe('invalid_token')
    expect(headroomStatus(probed('error'))).toBe('error')
    expect(headroomStatus(usage())).toBeNull()
  })

  it('is null — never a guess — when the usage read itself failed', () => {
    expect(headroomStatus({ error: true })).toBeNull()
    expect(isTokenRejected({ error: true })).toBe(false)
    expect(headroomStatus(undefined)).toBeNull()
  })

  it('does not decay with the snapshot age, unlike the percentages', () => {
    // A number goes stale; "this credential was refused" does not become false
    // by sitting still — only another probe can change it.
    const old = probed('invalid_token', {})
    old.headroom.snapshot_age_seconds = 60 * 60 * 6
    expect(isTokenRejected(old)).toBe(true)
    expect(windowReadings(old)).toBeNull()
  })
})

describe('pressureHeadline precedence (#2353)', () => {
  it('says the token is invalid instead of claiming a limit', () => {
    expect(pressureHeadline(probed('invalid_token'))).toBe('token invalid')
  })

  it('a rejected token outranks rate_limited_now', () => {
    // A probe that could not authenticate learned NOTHING about the quota, so
    // a limit claim here is unfounded, not merely less useful.
    expect(pressureHeadline(probed('invalid_token', { rate_limited_now: true })))
      .toBe('token invalid')
  })

  it('still reports genuine throttling', () => {
    expect(pressureHeadline(probed('rate_limited', { rate_limited_now: true })))
      .toBe('rate-limited')
  })

  it('reports a failed probe as absent data — never as ok', () => {
    expect(pressureHeadline(probed('error'))).toBe('no provider data')
  })

  it('ranks a failed probe below real failures on record', () => {
    const u = probed('error', {
      failure_events_24h: 2,
      failure_events_by_kind: { rate_limit: 2 },
    })
    expect(pressureHeadline(u)).toBe('2× 429')
  })

  it('leaves the healthy and unavailable ends untouched', () => {
    expect(pressureHeadline(probed('ok'))).toBe('ok')
    expect(pressureHeadline(usage())).toBe('ok')
    expect(pressureHeadline({ error: true })).toBe('unavailable')
  })
})

describe('subscriptionSeverity — auth is its own state', () => {
  it('a rejected token is auth, not crit', () => {
    expect(subscriptionSeverity(probed('invalid_token'))).toBe('auth')
  })

  it('auth wins even when the backend also flagged a limit', () => {
    expect(subscriptionSeverity(probed('invalid_token', { rate_limited_now: true })))
      .toBe('auth')
  })

  it('genuine throttling is still crit', () => {
    expect(subscriptionSeverity(probed('rate_limited', { rate_limited_now: true })))
      .toBe('crit')
  })

  it('a failed probe alone is not an alarm', () => {
    // Nothing is known — but nothing is claimed either. `pressureHeadline`
    // carries the disclosure; the chip stays quiet.
    expect(subscriptionSeverity(probed('error'))).toBe('ok')
  })
})

describe('warnReason — the chip has room for one word', () => {
  it('says auth when every recorded failure was auth-kind', () => {
    const u = usage({ failure_events_24h: 3, failure_events_by_kind: { auth: 3 } })
    expect(subscriptionSeverity(u)).toBe('warn')
    expect(warnReason(u)).toBe('auth')
  })

  it('says events once a real 429 is in the mix', () => {
    const u = usage({
      failure_events_24h: 4,
      failure_events_by_kind: { auth: 3, rate_limit: 1 },
    })
    expect(warnReason(u)).toBe('events')
  })

  it('leaves unclassified rows as generic events, never auth', () => {
    const u = usage({ failure_events_24h: 2, failure_events_by_kind: { unknown: 2 } })
    expect(warnReason(u)).toBe('events')
  })

  it('will not say auth while anything unclassified is in the mix', () => {
    // The per-agent chip gets a total and an auth count and cannot ask a looser
    // question — so this rule is strict equality, and the two surfaces agree on
    // every payload rather than only on the clean ones.
    const u = usage({ failure_events_24h: 3, failure_events_by_kind: { auth: 2, unknown: 1 } })
    expect(warnReason(u)).toBe('events')
    expect(pressureBadge({
      agent_name: 'a',
      auth_mode: 'subscription',
      subscription_name: 'Sub A',
      failure_events_24h: 3,
      auth_failures_24h: 2,
      rate_limited_now: false,
    }).text).toBe('sub 429s')
  })

  it('counts auth failures without reading the total', () => {
    expect(authFailureCount(usage({ failure_events_24h: 9, failure_events_by_kind: { auth: 2 } })))
      .toBe(2)
    expect(authFailureCount(usage({ failure_events_24h: 9 }))).toBe(0)
    expect(authFailureCount({ error: true })).toBe(0)
  })
})

describe('row ordering — the row needing a person sorts above the row needing a wait', () => {
  it('puts a dead token above a genuinely rate-limited subscription', () => {
    const subs = [
      { id: 'limited', name: 'Limited' },
      { id: 'dead', name: 'Dead token' },
      { id: 'fine', name: 'Fine' },
    ]
    const { rows } = subscriptionPressureRows(subs, {
      limited: probed('rate_limited', { rate_limited_now: true }),
      dead: probed('invalid_token'),
      fine: usage(),
    })
    expect(rows.map((r) => r.id)).toEqual(['dead', 'limited', 'fine'])
  })

  it('the dead-token row says so on its face, not only in the tooltip', () => {
    const { rows } = subscriptionPressureRows(
      [{ id: 'dead', name: 'Dead token' }],
      { dead: probed('invalid_token') },
    )
    expect(rows[0].meta).toBe('token invalid')
    expect(rows[0].severity).toBe('auth')
    // The tooltip keeps the full remedy; the face carries the state.
    expect(rows[0].title).toContain('re-register')
  })
})

describe('pressureBadge — the per-agent chip (#2352)', () => {
  const entry = (o = {}) => ({
    agent_name: 'a',
    auth_mode: 'subscription',
    subscription_name: 'Sub A',
    failure_events_24h: 0,
    auth_failures_24h: 0,
    rate_limited_now: false,
    token_status: null,
    ...o,
  })

  it('says auth, not 429s, when every failure was auth-kind', () => {
    // After the predicate split an auth-failing subscription is no longer
    // rate_limited_now, so it lands in the warn tier — where the OLD wording
    // would have called it "429s", trading one wrong word for another.
    const b = pressureBadge(entry({ failure_events_24h: 3, auth_failures_24h: 3 }))
    expect(b.text).toBe('sub auth')
    expect(b.level).toBe('warn')
  })

  it('says 429s when the failures are not purely auth', () => {
    const b = pressureBadge(entry({ failure_events_24h: 3, auth_failures_24h: 1 }))
    expect(b.text).toBe('sub 429s')
  })

  it('a rejected token is crit and names the remedy', () => {
    const b = pressureBadge(entry({ token_status: 'invalid_token' }))
    expect(b.level).toBe('crit')
    expect(b.text).toBe('sub auth')
    expect(b.title).toContain('re-register')
  })

  it('a rejected token outranks rate_limited_now here too', () => {
    const b = pressureBadge(entry({ token_status: 'invalid_token', rate_limited_now: true }))
    expect(b.text).toBe('sub auth')
  })

  it('still says limit for genuine throttling', () => {
    const b = pressureBadge(entry({ rate_limited_now: true, failure_events_24h: 2 }))
    expect(b.text).toBe('sub limit')
    expect(b.level).toBe('crit')
  })

  it('degrades to the pre-#2352 wording on a payload without the new fields', () => {
    // An older backend, or a response cached across a deploy: absent fields must
    // read as "unknown, say nothing new" rather than flipping the word.
    const b = pressureBadge({
      agent_name: 'a',
      auth_mode: 'subscription',
      subscription_name: 'Sub A',
      failure_events_24h: 2,
      rate_limited_now: true,
    })
    expect(b.text).toBe('sub limit')
  })

  it('stays silent for an agent with no pressure at all', () => {
    expect(pressureBadge(entry())).toBeNull()
    expect(pressureBadge(entry({ auth_mode: 'api_key', token_status: 'invalid_token' }))).toBeNull()
  })
})
