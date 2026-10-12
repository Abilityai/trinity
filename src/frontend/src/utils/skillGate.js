/**
 * A run request that names a gated skill (trinity-enterprise#751) answers
 * **202** `{status: "pending_approval", code: "approval_pending", request_id,
 * skills, approver_role, expires_at, outcome_delivery, message}`: nothing ran,
 * an approval was raised. It is not an error, and there is no execution to
 * poll — the senders show the server's own `message` and stop (trinity#3274).
 * A refusal arrives as an ordinary error, `detail: {status: "refused", code,
 * message}` with `X-Trinity-Error-Code`; `apiErrorMessage` reads its message.
 */

// The MCP layer's fallback for a body without a message (client.ts parseGateResult).
const FALLBACK_PENDING = 'Not run: this needs approval.'

// How long a toast shows the notice: two sentences, not a one-line "done".
export const PENDING_NOTICE_TOAST = { timeout: 8000 }

/** The backend's notice for a pending approval, or null for any other answer. */
export function pendingApprovalMessage(response) {
  const data = response?.data
  if (response?.status !== 202 || data?.status !== 'pending_approval') return null
  return typeof data.message === 'string' && data.message.trim() ? data.message : FALLBACK_PENDING
}

// `chat_messages.source` of a held request and its notice (trinity#3526).
const GATE_SOURCE = 'gate:'

/**
 * A message loaded from a chat session, as the chat shows it. A held request
 * and its notice come back `held` (shown, and left out of the history sent
 * with later turns); the notice reads the decision once there is one.
 */
export function storedChatMessage(msg) {
  if (!String(msg?.source || '').startsWith(GATE_SOURCE)) return msg
  const decision = msg.role === 'system' ? msg.gate?.text : null
  return { ...msg, held: true, content: decision || msg.content }
}

/** True for a named skill-gate refusal (nothing ran, nothing was raised). */
export function isGateRefusal(err) {
  const headers = err?.response?.headers || {}
  const code = typeof headers.get === 'function'
    ? headers.get('x-trinity-error-code')
    : headers['x-trinity-error-code']
  const detail = err?.response?.data?.detail
  return Boolean(code) && detail?.status === 'refused' && detail?.code === code
}
