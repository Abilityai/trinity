/**
 * #4344 — the one place an axios error becomes the sentence a person reads.
 *
 * The Workspace and the public chat used to render `err.response.data.detail`
 * as-is: a FastAPI validation list printed `[object Object]`, a 5xx printed
 * "Internal Server Error", and machine refusals ("agents: at most 8 names per
 * request", "This endpoint requires a portal-delegate API key") reached a
 * client. Order:
 *
 *   1. an object `detail` with a string `message` — the backend wrote it for a
 *      person (every coded refusal in `client_portal` carries one);
 *   2. a known `detail.code` the caller maps to copy;
 *   3. a plain-string `detail` below 500 that reads as a sentence;
 *   4. the caller's fallback — including a network failure (no response).
 *
 * Pure module: `vitest` runs it in `environment: 'node'`.
 */

const SENTENCE_MAX = 200
// `field: value` / `agents: at most …` — a key prefix is machine text.
const KEY_PREFIX = /^[A-Za-z_][\w.-]*:\s/
const MACHINE_WORDS = /\/api\/|\bAPI key\b|\bendpoint\b/i

/** Does this string read as a sentence written for a person? */
export function isSentence(text) {
  if (typeof text !== 'string') return false
  const t = text.trim()
  if (!t || t.length > SENTENCE_MAX) return false
  if (!t.includes(' ')) return false
  if (!/^[A-Z]/.test(t)) return false
  if (KEY_PREFIX.test(t)) return false
  if (MACHINE_WORDS.test(t)) return false
  return true
}

/**
 * @param {unknown} err  an axios error (or anything thrown)
 * @param {string} fallback  the caller's own sentence for this verb
 * @param {{ codes?: Record<string, string> }} [options]
 * @returns {string}
 */
export function personFacingError(err, fallback, { codes = {} } = {}) {
  const response = err?.response
  if (!response) return fallback
  const detail = response.data?.detail
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    if (typeof detail.message === 'string' && detail.message.trim()) return detail.message.trim()
    if (typeof detail.code === 'string' && typeof codes[detail.code] === 'string') return codes[detail.code]
    return fallback
  }
  const status = Number.isFinite(response.status) ? response.status : 0
  if (typeof detail === 'string' && status < 500 && isSentence(detail)) return detail.trim()
  return fallback
}
