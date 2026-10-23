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
  // Enroll → bind (checkpoint B).
  opening: 'Opening the Nevermined card form…',
  binding: 'Binding the Nevermined mandate to this agent…',
  overlayOpen: 'The Nevermined card form is open over this page. Press Esc to close it.',
  funded: 'Funded through Nevermined. The agent can spend from the mandate at its next run.',
  apiKeyKept: 'The agent already held a Nevermined API key — only the Nevermined mandate id was added.',
  alreadyFunded: 'This agent was already funded through Nevermined.',
  mandateExists: 'The mandate already exists at Nevermined; if you do not retry, revoke it there.',
  pendingBanner: 'A Nevermined mandate was created but not bound to this agent.',
  pendingDiscardNote: 'Discard keeps the mandate at Nevermined; revoke it there if you do not want it.',
  sdkLoadFailed: 'Could not load the Nevermined widget. Check your connection and try again.',
  invalidSession: "Nevermined returned a session Trinity's widget could not use.",
  invalidSessionNote:
    'A user without a Nevermined smart-account wallet also lands here; check the account at Nevermined.',
  sessionExpired: 'The Nevermined session expired — start again.',
  badIds: 'Nevermined returned ids Trinity could not use. Nothing was bound.',
  newMandate: 'Create a new Nevermined mandate',
  embedUnauthorized:
    'Nevermined refused the card form session. The session may have expired, or the widget key may not allow this origin.',
  embedNetwork: 'The Nevermined card form could not reach Nevermined. Check your connection and try again.',
  embedPayment: 'Nevermined could not confirm the card. Try again or use another card.',
  embedUnknown: 'The Nevermined card form reported an error. Try again.',
  closeForm: 'Close the Nevermined form',
  // Revoke (checkpoint B).
  revokeTitle: 'Revoke this mandate at Nevermined?',
  revoked: 'Mandate revoked at Nevermined.',
  revokeFailed: 'The Nevermined mandate is still active.',
})

/**
 * Action labels the rulings fix verbatim (plan §4.4 / addendum). They sit
 * beside copy that names Nevermined; they are wording-guard clean but are
 * not required to carry the name themselves.
 */
export const ACTIONS = Object.freeze({
  retryBind: 'Retry bind',
  discard: 'Discard',
  revokeConfirm: 'Revoke mandate',
  tryAgain: 'Try again',
})

/** The §4.4 revoke confirmation, with the amount Nevermined reported. */
export function revokeMessage(remaining) {
  const amount = remaining && remaining !== DASH && remaining !== 'unknown' ? remaining : 'remaining balance'
  const lead = amount === 'remaining balance' ? 'The remaining balance' : `The remaining ${amount}`
  return (
    "Nevermined will refuse the agent's next paid call immediately. " +
    `${lead} cannot be spent through this agent. Nevermined holds the card; nothing about the card changes.`
  )
}

/** Per-code fallback copy for an SDK `EmbedError` without a `userMessage`. */
export function embedErrorMessage(e) {
  if (e && typeof e.userMessage === 'string' && e.userMessage.trim()) return e.userMessage
  switch (e?.code) {
    case 'UNAUTHORIZED': return COPY.embedUnauthorized
    case 'NETWORK': return COPY.embedNetwork
    case 'PAYMENT_NOT_CONFIRMED': return COPY.embedPayment
    default: return COPY.embedUnknown
  }
}

/** The pattern `NeverminedDelegationBind` enforces on both ids (ent#966). */
export const ID_PATTERN = /^[A-Za-z0-9_-]{8,128}$/

/**
 * `setupCard`'s `onSuccess` result → the bind body's ids, or null.
 * `paymentMethodId` is optional on the bind; a present but malformed one fails.
 */
export function unwrapSuccess(result) {
  if (!result || typeof result !== 'object') return null
  const { delegationId, paymentMethodId } = result
  if (typeof delegationId !== 'string' || !ID_PATTERN.test(delegationId)) return null
  if (paymentMethodId === undefined || paymentMethodId === null || paymentMethodId === '') {
    return { delegationId }
  }
  if (typeof paymentMethodId !== 'string' || !ID_PATTERN.test(paymentMethodId)) return null
  return { delegationId, paymentMethodId }
}

const SESSION_STRINGS = ['sessionToken', 'userId', 'expiresAt', 'apiKeyHash']

function isWidgetSession(s) {
  return Boolean(
    s && typeof s === 'object'
      && SESSION_STRINGS.every((k) => typeof s[k] === 'string' && s[k])
      && typeof s.userWallet === 'string' && s.userWallet.startsWith('0x'),
  )
}

/**
 * The widget-session body forwarded verbatim by the backend → the five keys
 * the SDK's `initialize` checks (TD-7): bare, or under `.session` / `.data`.
 */
export function unwrapSession(body) {
  for (const candidate of [body, body?.session, body?.data]) {
    if (isWidgetSession(candidate)) {
      const { sessionToken, userId, userWallet, apiKeyHash, expiresAt } = candidate
      return { sessionToken, userId, userWallet, apiKeyHash, expiresAt }
    }
  }
  return null
}

/** Inline iframe width: the slot's width clamped to the SDK's [440, 720]. */
export const INLINE_MIN_WIDTH = 440
export const INLINE_MAX_WIDTH = 720
export const INLINE_HEIGHT = 720
export function inlineWidth(measured) {
  const n = Number.isFinite(measured) ? measured : INLINE_MAX_WIDTH
  return Math.min(INLINE_MAX_WIDTH, Math.max(INLINE_MIN_WIDTH, Math.floor(n)))
}

/**
 * A revoke's `env_note` → a tone, keyed on the backend's prefix only. The
 * sentence itself is rendered verbatim (addendum 2026-10-23, W3).
 */
export function envNoteTone(note) {
  if (typeof note !== 'string') return null
  if (note.startsWith('agent_not_running')) return 'stopped'
  if (note.startsWith('agent_unreachable')) return 'unreachable'
  return 'other'
}

// ---- pending: a mandate created at Nevermined, not yet bound in Trinity ----

export const PENDING_PREFIX = 'nvm-pending:'

function sessionStore(storage) {
  if (storage !== undefined) return storage
  try {
    return typeof window !== 'undefined' && window.sessionStorage ? window.sessionStorage : null
  } catch {
    return null
  }
}

/** Persist `{agent, delegationId, paymentMethodId?, provider, at}` for its agent. */
export function persistPending(pending, storage) {
  const store = sessionStore(storage)
  if (!store || !pending?.agent) return false
  try {
    store.setItem(PENDING_PREFIX + pending.agent, JSON.stringify(pending))
    return true
  } catch {
    return false
  }
}

/** The persisted pending record for `agent`, or null (malformed → null). */
export function readPending(agent, storage) {
  const store = sessionStore(storage)
  if (!store || !agent) return null
  try {
    const raw = store.getItem(PENDING_PREFIX + agent)
    if (!raw) return null
    const p = JSON.parse(raw)
    if (!p || p.agent !== agent || !unwrapSuccess(p)) return null
    return p
  } catch {
    return null
  }
}

export function clearPending(agent, storage) {
  const store = sessionStore(storage)
  if (!store || !agent) return
  try { store.removeItem(PENDING_PREFIX + agent) } catch { /* storage blocked */ }
}

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
    case 'agent_key_present':
      // The mandate already exists at Nevermined: fix the agent (start it, or
      // remove its hand-set NVM_API_KEY), then bind the same ids.
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
