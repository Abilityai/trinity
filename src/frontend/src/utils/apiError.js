/**
 * Turn an Axios/FastAPI error into a string a human should read.
 *
 * WHY THIS EXISTS
 * ---------------
 * FastAPI returns two different shapes under `detail`:
 *   - handler-raised HTTPException -> a string  ("Admin access required")
 *   - request-validation failure (422) -> an ARRAY of Pydantic error objects
 *     ([{type, loc, msg, input, ctx}, …])
 *
 * The app has ~200 callsites doing `e.response?.data?.detail || e.message`, which
 * renders the second shape by stringifying the raw array straight into the page:
 *
 *   [ { "type": "less_than_equal", "loc": [ "body", "max_rows" ],
 *       "msg": "Input should be less than or equal to 1000000", … } ]
 *
 * That is not an error message; it is a stack trace wearing one. This normalises
 * both shapes. Introduced for #1644; the other callsites are a separate cleanup.
 *
 * @param {unknown} err     the caught error (Axios error, or anything)
 * @param {string}  fallback message when nothing usable can be extracted
 * @returns {string} a single human-readable sentence
 */
export function apiErrorMessage(err, fallback = 'Something went wrong') {
  const detail = err?.response?.data?.detail

  // 422: array of Pydantic validation errors. Prefer the field's own message —
  // it already reads as a sentence ("Input should be less than or equal to
  // 1000000") — and prefix the field name so the user knows WHICH input.
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => {
        const msg = typeof d?.msg === 'string' ? d.msg : null
        if (!msg) return null
        // loc is like ["body", "max_rows"]; the last segment is the field.
        const field = Array.isArray(d?.loc) ? d.loc[d.loc.length - 1] : null
        return field && field !== 'body' ? `${field}: ${msg}` : msg
      })
      .filter(Boolean)
    if (parts.length) return parts.join('; ')
  }

  if (typeof detail === 'string' && detail.trim()) return detail
  // Some endpoints return {message: …} rather than {detail: …}
  const message = err?.response?.data?.message
  if (typeof message === 'string' && message.trim()) return message
  if (typeof err?.message === 'string' && err.message.trim()) return err.message
  return fallback
}
