// #3460 — the Workspace chat message limit, stated before sending.
//
// The server bounds a chat turn (`PortalChatRequest.message`, max_length in
// `src/backend/client_portal/models.py`). The composer enforced nothing, so an
// over-long message went out, came back 422, and was reported as "error 422"
// with the words already cleared from the composer.
//
// The number below is a COPY of one the server owns. It is held equal by
// `tests/unit/portalMessageLimit.mount.spec.js`, which reads the backend model;
// change the server's limit and that test names this line. The server stays the
// authority either way — `messageRefusalReason` quotes ITS number, not this one.
export const PORTAL_MESSAGE_MAX_CHARS = 8000

// The count appears for the last stretch only: a counter under every "ok" would
// be standing noise, and a limit first mentioned at 8001 is the bug.
export const MESSAGE_LIMIT_WARN_WITHIN = 500

const fmt = (n) => n.toLocaleString('en-US')

/**
 * Length as the server measures it. Pydantic's `max_length` on a `str` is
 * Python's `len()` — code points — while `String.length` is UTF-16 units, so an
 * emoji counts once there and twice here. Counting the server's way keeps the
 * two verdicts identical at the boundary instead of refusing a message the
 * server would have taken.
 */
export function messageLength(text) {
  const s = text || ''
  let n = 0
  // eslint-disable-next-line no-unused-vars
  for (const _ of s) n += 1
  return n
}

/**
 * What the composer shows and enforces for a draft.
 * `near` — close enough that the count is worth showing (includes over).
 * `over` — the server would refuse it, so it is not sent.
 */
export function messageLimitState(text, max = PORTAL_MESSAGE_MAX_CHARS) {
  const s = text || ''
  // UTF-16 length is never below the code-point count, so a draft this short
  // cannot be near the limit — and the common case skips the walk.
  if (s.length < max - MESSAGE_LIMIT_WARN_WITHIN) {
    return { length: s.length, max, near: false, over: false, message: '' }
  }
  const length = messageLength(s)
  const over = length > max
  const near = length >= max - MESSAGE_LIMIT_WARN_WITHIN
  let message = ''
  if (over) {
    message = `A message can be at most ${fmt(max)} characters — this one is ${fmt(length)}. Shorten it to send.`
  } else if (near) {
    message = `${fmt(length)} of ${fmt(max)} characters`
  }
  return { length, max, near, over, message }
}

/**
 * The server refused the message as INVALID (a 422 carrying Pydantic's list):
 * say why in words. Returns null for everything else, so the existing delivery
 * reasons keep every case they already explain — including a handler-raised
 * 422, whose `detail` is already a sentence.
 *
 * A validation refusal is the one failure where Retry is wrong by construction:
 * the same words earn the same answer. The caller hands the draft back instead.
 */
export function messageRefusalReason(err) {
  if (err?.response?.status !== 422) return null
  const detail = err.response.data?.detail
  if (!Array.isArray(detail)) return null
  const fieldOf = (d) => (Array.isArray(d?.loc) ? d.loc[d.loc.length - 1] : null)
  const tooLong = detail.find((d) => d?.type === 'string_too_long' && fieldOf(d) === 'message')
  if (tooLong) {
    const max = Number(tooLong.ctx?.max_length) || PORTAL_MESSAGE_MAX_CHARS
    return `That message is too long — a message can be at most ${fmt(max)} characters. Shorten it and send again.`
  }
  const said = detail
    .map((d) => (typeof d?.msg === 'string' ? d.msg.trim() : ''))
    .filter(Boolean)
    .join('; ')
  return said
    ? `The message wasn't accepted — ${said}. Edit it and send again.`
    : "The message wasn't accepted. Edit it and send again."
}
