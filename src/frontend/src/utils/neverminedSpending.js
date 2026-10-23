/**
 * Pure rules for the Nevermined spending panel (abilityai/trinity-enterprise#967).
 *
 * `components/NeverminedSpendingPanel.vue` executes these; they live here so
 * node-env specs can pin them (the `utils/aauthSettings.js` + `AAuthPanel.vue`
 * pattern). Input is the ent#966 `GET /api/nevermined/agents/{name}/delegation`
 * shape (requirements §23.8 FR-3/FR-5).
 *
 * Wording (ent#932): Nevermined holds the card and the mandate and settles
 * every purchase; Trinity records only the mandate id. Every string in `COPY`
 * names Nevermined, none matches `FORBIDDEN_WORDING` (mirrored from
 * `tests/unit/test_ent966_facilitator_wording.py`), and the positive literal
 * is present.
 */
import { viewState } from './loadingState.js'
import { formatLocalDateTime } from './timestamps.js'

/** Mirror of the backend wording guard's FORBIDDEN regex (case-insensitive). */
export const FORBIDDEN_WORDING =
  /trinity\s+(charges|holds|stores|keeps|saves)\s+(the|your|a)\s+card|trinity\s+settles|trinity\s+(is|acts as)\s+(the\s+)?merchant/i

/** The positive phrase the wording guard requires on every text surface. */
export const POSITIVE_WORDING = /Nevermined card mandate|Nevermined holds the card/

export const COPY = Object.freeze({
  title: 'Spending · Nevermined card mandate',
  subtitle:
    'What this agent can buy from outside services through a card mandate. Nevermined holds the card and the mandate; Trinity records only the mandate id.',
  loading: "Reading this agent's Nevermined spending…",
  failedTitle: "Couldn't read this agent's Nevermined spending",
  failedMessage: 'The Nevermined spending read failed. Check your connection and try again.',
  staleSubject: "this agent's Nevermined spending",
  refresh: 'Refresh from Nevermined',
  notConfigured: 'Card funding through Nevermined is not set up on this instance.',
  notConfiguredAdminLink: 'Set up the Nevermined organization in Settings › Integrations',
  notConfiguredOther: 'An admin sets up the Nevermined organization first.',
  notFunded: 'No Nevermined card mandate is bound to this agent.',
  expectation:
    'A Nevermined card mandate pays card-priced services and Nevermined plans; wallet-priced (x402) services still need a funded wallet.',
  enroll: 'Enroll a card with Nevermined',
  notRunning: "Start the agent first — Trinity writes the Nevermined mandate id into the agent's credential file.",
  sharedViewer: "The agent's owner can fund it with a Nevermined card mandate.",
  nextRun: "Takes effect at the agent's next run; Nevermined settles each purchase against the mandate.",
  readFrom: 'Read from Nevermined',
  unavailableBadge: 'Nevermined unreachable',
  unavailable: 'Nevermined did not answer; nothing is estimated locally.',
  revoke: 'Revoke at Nevermined',
  boundBy: 'Bound to this agent through Nevermined by',
  mandateId: 'Nevermined mandate',
  transactionsLink: 'Transactions at Nevermined',
  pastMandates: 'Past Nevermined mandates',
})

/** Rendered for a number Nevermined did not report — never a derived figure. */
export const UNKNOWN = 'unknown'
export const DASH = '—'

/**
 * Reduce the read flags + GET body to the panel's state and its affordances.
 *
 * state: loading | failed | not_configured | not_funded | funded | funded_unavailable
 * A funded agent offers Revoke only — a second bind is refused upstream (R1).
 */
export function spendingView({
  data = null, hasLoaded = false, error = null, canEdit = false, agentStatus = '', hasPending = false,
} = {}) {
  const base = viewState({ hasLoaded, error, count: 1 })
  let state = base.state
  if (state === 'ready') {
    if (!data?.configured) state = 'not_configured'
    else if (!data.funded) state = 'not_funded'
    else state = isUnavailable(data.live) ? 'funded_unavailable' : 'funded'
  }
  const running = agentStatus === 'running'
  const notFunded = state === 'not_funded'
  const funded = state === 'funded' || state === 'funded_unavailable'
  return {
    state,
    stale: base.stale,
    canEnroll: Boolean(canEdit && running && notFunded),
    enrollBlocked: canEdit && notFunded && !running ? 'not_running' : null,
    canRevoke: Boolean(canEdit && funded),
    showPendingBanner: Boolean(notFunded && hasPending),
  }
}

function isUnavailable(live) {
  return !live || live.source === 'unavailable' || live.status === 'unknown'
}

/** Money from integer cents in the currency Nevermined reported. */
export function formatCents(cents, currency) {
  if (typeof cents !== 'number' || !Number.isFinite(cents)) return DASH
  const amount = cents / 100
  if (!currency) return `${amount.toFixed(2)} (currency not reported)`
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(amount)
  } catch {
    return `${amount.toFixed(2)} ${currency}`
  }
}

/** A purchase count, or "—" when Nevermined did not report it. */
export function formatCount(n) {
  return typeof n === 'number' && Number.isFinite(n) ? String(n) : DASH
}

const units = [
  ['day', 86400],
  ['hour', 3600],
  ['minute', 60],
]

function span(seconds) {
  for (const [unit, size] of units) {
    if (seconds >= size) {
      const n = Math.floor(seconds / size)
      return `${n} ${unit}${n === 1 ? '' : 's'}`
    }
  }
  return 'less than a minute'
}

/**
 * Expiry as relative copy plus an absolute time for the hover (principle 22).
 * @returns {{relative: string, absolute: string, past: boolean}}
 */
export function expiryParts(iso, now = Date.now()) {
  const at = iso ? Date.parse(iso) : Number.NaN
  if (Number.isNaN(at)) return { relative: DASH, absolute: '', past: false }
  const diff = Math.round((at - now) / 1000)
  const past = diff < 0
  return {
    relative: past ? `expired ${span(-diff)} ago` : `in ${span(diff)}`,
    absolute: formatLocalDateTime(iso),
    past,
  }
}

/**
 * The recovery action a refusal selects. The backend's message is the copy
 * (`apiErrorMessage`); this picks only what the panel offers next.
 *
 * @param {string|null} code   `detail.code`, or null (429 string detail, network)
 * @param {number|undefined} status
 * @param {'read'|'mint'|'bind'|'revoke'} phase  the step that failed
 * @returns {'reread'|'retry_mint'|'retry_bind'|'retry_revoke'|'new_mandate'|'none'}
 */
export function recoveryFor(code, status, phase = 'read') {
  const retry = { read: 'reread', mint: 'retry_mint', bind: 'retry_bind', revoke: 'retry_revoke' }[phase] || 'reread'
  switch (code) {
    case 'nevermined_not_configured':
    case 'delegation_active':
    case 'bind_in_progress':
    case 'no_active_delegation':
      return 'reread'
    case 'delegation_not_found':
    case 'delegation_inactive':
      return 'new_mandate'
    case 'agent_not_running':
      // The mandate already exists at Nevermined: start the agent, bind the same ids.
      return 'retry_bind'
    case 'nevermined_busy':
    case 'nevermined_unreachable':
    case 'nevermined_rejected':
    case 'agent_unreachable':
      return retry
    case null:
    case undefined:
    case '':
      break
    default:
      return 'none'
  }
  if (status === 429 || status === undefined || status === null || status >= 500) return retry
  if (status === 422 && phase === 'bind') return 'new_mandate'
  if (status === 404 && phase === 'read') return 'reread'
  return 'none'
}

/** Seconds to wait before a retry on 429/503 (axios lower-cases headers); else null. */
export function retryAfterSeconds(err) {
  const status = err?.response?.status
  if (status !== 429 && status !== 503) return null
  const raw = err.response.headers?.['retry-after']
  const n = Number.parseInt(raw, 10)
  return Number.isFinite(n) && n > 0 ? n : 5
}

const BADGES = {
  active: { variant: 'success', glyph: '✓', label: 'Active' },
  exhausted: { variant: 'warning', glyph: '◐', label: 'Exhausted' },
  expired: { variant: 'neutral', glyph: '⌛', label: 'Expired' },
  revoked: { variant: 'danger', glyph: '✕', label: 'Revoked' },
  unknown: { variant: 'neutral', glyph: '?', label: 'Unknown' },
}

/** Mandate status as a badge: colour AND glyph (principle 24). */
export function statusBadge(status) {
  return BADGES[status] || { variant: 'neutral', glyph: '•', label: String(status || 'unknown') }
}

/** The last 8 characters of an id, for a compact "…1a2b3c4d" display. */
export function idTail(id) {
  if (!id) return DASH
  const s = String(id)
  return s.length > 8 ? `…${s.slice(-8)}` : s
}
