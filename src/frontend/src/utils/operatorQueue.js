/**
 * Operator-queue answers — the ONE home of the respond payload, the
 * controls-kind switch and the type labels (#2370).
 *
 * THE rule: `response` is the DECISION the agent reads — the chosen option,
 * the typed answer, or `acknowledged`; `response_text` is an optional
 * free-text note. Every frontend producer of
 * `POST /api/operator-queue/{id}/respond` builds its body here: the desktop
 * store (QueueCard / QueueItemDetail / acknowledgeItem all funnel through
 * `respondToItem`) and the `/m` mobile admin. The bug class this module closes
 * was a SECOND producer hand-building the body — `/m` POSTed a hard-coded
 * `response: 'approved'` with the tapped option in `response_text`, so a Deny
 * was recorded as an approval and a typed answer became a note. The sync
 * write-back copies `response` to the agent's queue file verbatim and the
 * ent#329 resume dispatch frames it as "the answer", so the field is
 * load-bearing.
 *
 * Known non-consumers: the Workspace asks panel posts to its own endpoint with
 * its own model (`client_portal/asks`) — tracked as #2375; the MCP tool is
 * TypeScript and cannot import this module.
 *
 * Pure and import-free: `vitest.config.js` pins `environment: 'node'`, so this
 * is the unit-testable home of the rules and the views are thin callers.
 */

/**
 * Normalize an item's `options` to a list of strings. The DB stores whatever
 * JSON the agent wrote (any truthy value round-trips, the ingestion clamp only
 * size-checks), so a bare string would otherwise be iterated character by
 * character. Primitives are stringified; objects, nulls and empty strings are
 * dropped; anything that is not an array is no options at all.
 *
 * @param {{options?: unknown}|undefined} item
 * @returns {string[]}
 */
export function optionsOf(item) {
  const raw = item?.options
  if (!Array.isArray(raw)) return []
  return raw
    .filter((o) => typeof o === 'string' || typeof o === 'number' || typeof o === 'boolean')
    .map((o) => String(o))
    .filter((s) => s.length > 0)
}

/**
 * Which controls an item gets — by TYPE, and total:
 *
 *   approval with usable options → 'approval'    (option buttons → note → Send)
 *   approval without options     → 'question'    (the operator still has a
 *                                   decision to express, so give them a box)
 *   question                     → 'question'    (text answer)
 *   everything else              → 'acknowledge' ("Got it") — alert, and any
 *                                   type this build does not recognise
 *
 * A question that happens to carry options still gets a text answer — an
 * option button is a one-tap decision, and only an approval asks for one.
 *
 * **The unknown-type default is `acknowledge`, and that changed (ent#499).** It
 * was `question`, which is wrong in both directions. `type` is free TEXT with no
 * CHECK and the platform already emits non-protocol types (`skill_not_found`,
 * #1410; `workspace_problem_report`, ent#499) — those are INFORMATIONAL, nothing
 * is waiting on an answer, and offering a freeform box invites an operator to
 * type a reply that goes nowhere. Under ent#329 an answer can also spend a turn.
 * Acknowledge is the affordance that always terminates an item, which is what an
 * unrecognised one needs: a budgeted alert type whose items cannot be closed
 * jams its own pending cap permanently.
 *
 * @param {{type?: unknown, options?: unknown}|undefined} item
 * @returns {'approval'|'acknowledge'|'question'}
 */
export function queueResponseKind(item) {
  const type = item?.type
  if (type === 'approval') {
    return optionsOf(item).length > 0 ? 'approval' : 'question'
  }
  if (type === 'question') return 'question'
  return 'acknowledge'
}

/**
 * The wire body for `POST /api/operator-queue/{id}/respond`.
 *
 * `response` is passed through untouched — an option must stay byte-identical
 * to what the agent offered, so the caller decides what the decision is and
 * this never trims or rewrites it. The note is trimmed and an empty note is
 * `null`, never `""`.
 *
 * @param {unknown} response  the decision
 * @param {unknown} [note]    optional free text
 * @returns {{response: string, response_text: string|null}}
 */
export function queueResponseBody(response, note = '') {
  const text = typeof note === 'string' ? note.trim() : ''
  return { response: String(response), response_text: text.length ? text : null }
}

/**
 * Build the body for one of the three answer kinds, or `null` when there is
 * nothing valid to send (no option chosen, blank answer, unknown kind) — the
 * caller then keeps its Send control disabled / does nothing.
 *
 * @param {{kind?: 'approval'|'question'|'acknowledge', option?: unknown, note?: unknown, answer?: unknown}} [input]
 * @returns {{response: string, response_text: string|null}|null}
 */
export function buildQueueResponse({ kind, option, note = '', answer = '' } = {}) {
  switch (kind) {
    case 'approval': {
      const opt = typeof option === 'string' ? option : option == null ? '' : String(option)
      if (!opt.length) return null
      return queueResponseBody(opt, note)
    }
    case 'question': {
      const text = typeof answer === 'string' ? answer.trim() : ''
      if (!text.length) return null
      return queueResponseBody(text, '')
    }
    case 'acknowledge':
      return queueResponseBody('acknowledged', '')
    default:
      return null
  }
}

/** The desktop (QueueCard) labels for the three protocol types. */
export const QUEUE_TYPE_LABELS = Object.freeze({
  approval: 'Needs approval',
  question: 'Question',
  alert: 'Heads up',
})

/**
 * One label set across desktop and `/m`. An unknown type falls back to the raw
 * string and a missing one to '' — never a blank line from a wrong field name.
 *
 * @param {unknown} type
 * @returns {string}
 */
export function queueTypeLabel(type) {
  if (typeof type !== 'string') return ''
  return QUEUE_TYPE_LABELS[type] || type
}

/**
 * Copy for a respond the server refused because the item is no longer
 * pending — 409 when somebody else resolved it first (#1017), 400 when it was
 * already terminal, 404 when the row is gone. Attribution-free on purpose: the
 * status may be responded, cancelled or expired, and "another operator" is a
 * guess.
 */
export const QUEUE_RESPONSE_NOT_RECORDED =
  'This item is no longer pending (already answered, cancelled or expired) — your response was not recorded.'

/**
 * Did `POST …/respond` refuse because the item is no longer pending?
 * 409 = lost race, 400 = already terminal, 404 = the row is gone (purged or
 * never existed) — in every case the item is not waiting for this answer. A
 * 5xx, a 403 or a transport error is NOT a refusal: the item may still be
 * waiting, so the caller keeps the form for a retry.
 *
 * @param {unknown} err  an axios error
 * @returns {boolean}
 */
export function respondRefusedAsNotPending(err) {
  const status = err?.response?.status
  return status === 409 || status === 400 || status === 404
}

/**
 * #2915 — what the platform last established about the agent's own copy of an
 * item, as ONE badge rule every surface calls (QueueCard, ResolvedCard, /m,
 * PortalAsks). Returns `null` when nothing needs saying (confirmed, not aging,
 * delivered) so a healthy card stays quiet; otherwise `{ label, variant, title }`
 * for a `BaseBadge` (variant is a BaseBadge variant, never a raw colour).
 *
 * Inputs are the fields the API already carries: `sync_state`, `sync_detail`,
 * `last_confirmed_at`, `delivery_state`, `delivery_detail`, `aging`,
 * `aged_since`. The portal projection carries a coarse `sync` instead of
 * `sync_state`; both spellings are read.
 */
export const SYNC_BADGE_COPY = Object.freeze({
  changed: 'Changed by the agent',
  closed_by_filer: 'Closed by the agent',
  closed: 'Closed by the agent',
  missing: 'Gone from the agent',
  stale_id: 'Re-used id',
  unconfirmed: 'Unconfirmed',
  undelivered: 'Answer not delivered',
  aging: 'Waiting',
})

export function queueSyncBadge(item) {
  if (!item || typeof item !== 'object') return null
  const state = item.sync_state || item.sync
  // Delivery outranks sync on an answered item: "your answer never landed" is
  // the fact the operator needs first.
  if (item.delivery_state === 'undelivered') {
    return { label: SYNC_BADGE_COPY.undelivered, variant: 'danger', title: deliveryTitle(item) }
  }
  if (state === 'changed') {
    return { label: SYNC_BADGE_COPY.changed, variant: 'warning', title: changedTitle(item.sync_detail) + since(item) }
  }
  if (state === 'closed_by_filer' || state === 'closed') {
    return { label: SYNC_BADGE_COPY.closed_by_filer, variant: 'warning', title: 'The agent closed this on its side; it is still waiting for you here.' + since(item) }
  }
  if (state === 'missing') {
    return { label: SYNC_BADGE_COPY.missing, variant: 'warning', title: 'The agent no longer carries this item in its queue file.' + since(item) }
  }
  if (state === 'stale_id') {
    return { label: SYNC_BADGE_COPY.stale_id, variant: 'neutral', title: 'The agent re-used this id after the item was closed; the re-ask was not admitted.' }
  }
  if (state === 'unconfirmed') {
    return { label: SYNC_BADGE_COPY.unconfirmed, variant: 'neutral', title: unconfirmedTitle(item) }
  }
  if (item.aging) {
    return { label: SYNC_BADGE_COPY.aging, variant: 'warning', title: item.aged_since ? `Past the aging bound since ${item.aged_since}` : 'Past the aging bound' }
  }
  return null
}

function changedTitle(detail) {
  const fields = typeof detail === 'string' && detail ? detail.split(',').join(', ') : 'content'
  return `The agent rewrote this item since it was ingested (${fields}). You are reading the original.`
}

/** " Since <ts>." when the platform recorded when it established the state. */
function since(item) {
  return item.sync_updated_at ? ` Since ${item.sync_updated_at}.` : ''
}

function deliveryTitle(item) {
  const tried = item.delivery_updated_at ? ` Last tried ${item.delivery_updated_at}.` : ''
  const terminal = item.status === 'cancelled' || item.status === 'expired'
  switch (item.delivery_detail) {
    case 'entry_changed': return 'The agent rewrote the item after you answered; the answer was not delivered.'
    case 'closed_by_filer': return 'The agent closed the item on its side; the answer was not delivered.'
    case 'entry_missing':
      // A cancellation whose entry the agent already dropped has nothing left
      // to land on: recorded, never retried, and not an escalation.
      return terminal
        ? "The agent's queue file no longer carries this item, so the cancellation could not be written; the agent has already dropped it."
        : `The item is gone from the agent's queue file; the answer is re-added when the file is readable.${tried}`
    case 'file_missing': return `The agent has no queue file; retrying.${tried}`
    case 'agent_not_running': return 'The agent is not running; the answer is delivered when it starts.'
    case 'conflict': return `The agent was writing its file at the same moment; retrying.${tried}`
    default: return `The write to the agent failed; retrying.${tried}`
  }
}

function unconfirmedTitle(item) {
  const why = {
    agent_not_running: 'the agent is not running',
    timeout: 'the agent did not answer in time',
    unreachable: 'the agent could not be reached',
    invalid_json: "the agent's queue file is not valid JSON",
    wrong_shape: "the agent's queue file is not the expected shape",
    oversize_file: "the agent's queue file is too large to read",
  }[item.sync_detail] || 'the platform could not reconcile it'
  const when = item.last_confirmed_at ? ` Last confirmed ${item.last_confirmed_at}.` : ' Never confirmed.'
  return `Not confirmed with the agent: ${why}.${when}`
}

/** True when a respond/answer was refused with 409 `item_diverged` (#2915). */
export function respondRefusedAsDiverged(err) {
  const status = err?.response?.status
  if (status !== 409) return false
  const detail = err?.response?.data?.detail
  const code = detail && typeof detail === 'object' ? detail.code : null
  return code === 'item_diverged'
}

export const QUEUE_RESPONSE_DIVERGED =
  'The agent changed this item after you opened it. Review it and send again to answer anyway.'

/**
 * trinity-enterprise#611 — how an item ENDED, as ONE rule every surface renders
 * (ResolvedCard, the `/m` "Recently ended" strip, PortalAsks). `null` while the
 * item is still pending.
 *
 * `{ kind, label, who, when }`:
 *   - `kind` — `answered | cancelled | expired`: the ledger's `disposition`,
 *     else the terminal status (a row that ended before the ledger). The
 *     Workspace projection's own `status` (`answered`) reads the same way.
 *   - `who` — the person, for the Operating Room (`disposed_by_email`, or a
 *     legacy answer's `responded_by_email`); the Workspace projection's coarse
 *     `ended_by` (`you` / `the operator`); `timeout` for an expiry; `null` when
 *     the platform does not know.
 *   - `when` — the ledger's `disposed_at`, the projection's `ended_at`, or a
 *     legacy answer's `responded_at`. NEVER `created_at`: that is when the ask
 *     was filed, and showing it as the ending time is the defect this replaces.
 */
export const ENDING_LABELS = Object.freeze({
  answered: 'Answered',
  cancelled: 'Cancelled',
  expired: 'Expired',
})

function endingKind(item) {
  const d = item.disposition
  if (d === 'answered' || d === 'cancelled' || d === 'expired') return d
  const s = item.status
  if (s === 'responded' || s === 'acknowledged' || s === 'answered') return 'answered'
  if (s === 'cancelled' || s === 'expired') return s
  return null
}

export function queueEnding(item) {
  if (!item || typeof item !== 'object') return null
  const kind = endingKind(item)
  if (!kind) return null
  const when = item.disposed_at || item.ended_at || (kind === 'answered' ? item.responded_at : null) || null
  let who = null
  if (kind === 'expired') {
    who = 'timeout'
  } else if (item.ended_by === 'you') {
    who = 'you'
  } else if (item.ended_by === 'operator') {
    who = 'the operator'
  } else {
    who = item.disposed_by_email || (kind === 'answered' ? item.responded_by_email : null) || null
  }
  return { kind, label: ENDING_LABELS[kind], who, when }
}

/** The ending in words: "Cancelled by op@…", "Answered by you",
 *  "Expired — nobody answered in time", or the bare label when nobody is known. */
export function queueEndingText(ending) {
  if (!ending) return ''
  if (ending.kind === 'expired') return `${ending.label} — nobody answered in time`
  return ending.who ? `${ending.label} by ${ending.who}` : ending.label
}

/** What the resolved feed sorts by (#627 AC6): when the item ended; a legacy
 *  answer's time; else — the only timestamp such a row has — when it was filed. */
export function queueEndingSortTime(item) {
  return (item && (item.disposed_at || item.responded_at || item.created_at)) || ''
}

/** Up to `max` items that ENDED, most recent ending first — the `/m` strip. */
export function recentlyEnded(items, max = 5) {
  if (!Array.isArray(items)) return []
  return items
    .filter((i) => queueEnding(i))
    .sort((a, b) => String(queueEndingSortTime(b)).localeCompare(String(queueEndingSortTime(a))))
    .slice(0, max)
}
