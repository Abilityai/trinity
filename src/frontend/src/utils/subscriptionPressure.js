// #471 — pure display logic for subscription pressure/headroom.
// Kept out of components so vitest (node env, no mount harness) can reach it,
// and so the Settings panel, AgentTile chip and AgentListPanel badge cannot
// drift on what a reading means.

/**
 * Badge decision for one agent's pressure entry (the batch endpoint row).
 * Returns null (no badge) or { level: 'crit'|'warn', text, title }.
 *
 * AC rule: badge renders on 1+ failure events in 24h; severity upgrades to
 * 'crit' only on the one-gate `rate_limited_now` (2h machinery predicate OR
 * fresh provider verdict) — the 24h count alone never claims "limited now".
 *
 * #2352 — the badge distinguishes a CREDENTIAL problem from a QUOTA one:
 *
 *  - `token_status === 'invalid_token'` is a live provider verdict: the probe
 *    was rejected. It outranks everything, including `rate_limited_now`, since
 *    a probe that could not authenticate learned nothing about the quota.
 *  - Otherwise, "sub 429s" is claimed only when the failures are NOT purely
 *    auth-kind. `failure_events_24h` is the total and counts auth failures too,
 *    so labelling an auth-only history "429s" would trade the old wrong word
 *    ("limit", from the kind-blind predicate this release narrowed) for a new
 *    one. `auth_failures_24h` is the slice that makes the distinction possible.
 *
 * Both new fields default to absent-safe values, so an older payload (or a
 * cached response mid-deploy) degrades to exactly the pre-#2352 wording rather
 * than to a wrong one.
 */
export function pressureBadge(entry) {
  if (!entry || entry.auth_mode !== 'subscription') return null
  const events = entry.failure_events_24h || 0
  const authEvents = entry.auth_failures_24h || 0
  const tokenRejected = entry.token_status === 'invalid_token'
  const limitedNow = !!entry.rate_limited_now
  if (!events && !limitedNow && !tokenRejected) return null
  const sub = entry.subscription_name || 'subscription'

  // Auth-only: every recorded failure was the provider refusing the credential.
  const authOnly = authEvents > 0 && authEvents === events
  const level = tokenRejected || limitedNow ? 'crit' : 'warn'
  const text = tokenRejected || authOnly ? 'sub auth' : (limitedNow ? 'sub limit' : 'sub 429s')

  const parts = [`${sub}:`]
  if (tokenRejected) parts.push('provider token rejected — re-register it in Settings,')
  else if (limitedNow) parts.push('rate-limited now,')
  parts.push(`${events} failure event${events === 1 ? '' : 's'} in 24h`)
  if (authEvents > 0) parts.push(`(${authEvents} auth)`)
  if (entry.utilization_5h_pct != null) parts.push(`· 5h window ${entry.utilization_5h_pct}% used`)
  return { level, text, title: parts.join(' ') }
}

/** "62% of 5h window · resets 14:05" — null when the window is absent. */
export function headroomWindowLabel(win, windowName) {
  if (!win || win.utilization_pct == null) return null
  let label = `${win.utilization_pct}% of ${windowName} window`
  const reset = formatResetTime(win.resets_at)
  if (reset) label += ` · resets ${reset}`
  return label
}

/** ISO-Z → local HH:MM (same-day) or "Mon DD HH:MM". Null-safe. */
export function formatResetTime(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (isNaN(d.getTime())) return null
  const now = new Date()
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (d.toDateString() === now.toDateString()) return hm
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${hm}`
}

/** Honest source line for a usage payload: where the reading came from + age. */
export function usageSourceLabel(usage) {
  if (!usage) return null
  if (usage.source === 'anthropic' && usage.headroom) {
    const age = usage.headroom.snapshot_age_seconds
    if (age == null) return 'actual (Anthropic)'
    if (age < 90) return 'actual (Anthropic) · just now'
    const mins = Math.round(age / 60)
    return `actual (Anthropic) · ${mins} min ago`
  }
  return 'observed (estimate from recorded consumption)'
}

/** True when the reading is provider-truth AND recent enough to present as a gauge. */
export function headroomIsFresh(usage, freshnessSeconds = 1800) {
  const age = usage?.headroom?.snapshot_age_seconds
  return usage?.source === 'anthropic' && age != null && age <= freshnessSeconds
}

/**
 * Failure-kind split line: "3 rate-limit · 1 auth" (null when empty).
 * `unknown` (pre-#471 rows) is shown as such — never silently folded.
 */
export function failureKindLabel(byKind) {
  if (!byKind) return null
  const names = { rate_limit: 'rate-limit', auth: 'auth', unknown: 'unclassified' }
  const parts = Object.entries(byKind)
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${n} ${names[k] || k}`)
  return parts.length ? parts.join(' · ') : null
}

/** Tier 0: should this agent's cost render as API-equivalent (≈)? */
export function isSubscriptionFunded(authModeOrEntry) {
  const mode = typeof authModeOrEntry === 'string'
    ? authModeOrEntry
    : authModeOrEntry?.auth_mode
  return mode === 'subscription'
}
