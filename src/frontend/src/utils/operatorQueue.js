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
 * Which controls an item gets — by TYPE (desktop parity), and total:
 *
 *   approval with usable options → 'approval'    (option buttons → note → Send)
 *   alert                        → 'acknowledge' ("Got it")
 *   everything else              → 'question'    (text answer: question, an
 *                                   approval without options, unknown type)
 *
 * A question that happens to carry options still gets a text answer — an
 * option button is a one-tap decision, and only an approval asks for one.
 *
 * @param {{type?: unknown, options?: unknown}|undefined} item
 * @returns {'approval'|'acknowledge'|'question'}
 */
export function queueResponseKind(item) {
  const type = item?.type
  if (type === 'approval' && optionsOf(item).length > 0) return 'approval'
  if (type === 'alert') return 'acknowledge'
  return 'question'
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
