/**
 * Copying text.
 *
 * TWO entry points, deliberately, for now:
 *
 *   * `copyToClipboard` — the original boolean API, used by four settings
 *     panels (A2A, connector, MCP exposure, MCP keys). Left BYTE-IDENTICAL:
 *     its `console.warn` on failure and its focus restoration are behaviour
 *     those callers have today, none of them is under test, and #2515 has no
 *     business changing what happens when an operator copies an API key.
 *   * `copyText` — richer, and what the Workspace uses (#2515). It returns a
 *     RESULT rather than a boolean, because "unavailable", "blocked" and
 *     "failed" are three different sentences to show a reader and a boolean
 *     collapses them into one silence. It also never logs and never throws.
 *
 * Converging the four legacy callers onto `copyText` is a follow-up, not
 * something to smuggle into a readability fix.
 */

/**
 * Copy text to clipboard with a fallback for hostile environments.
 *
 * `navigator.clipboard.writeText()` rejects when the document doesn't have
 * focus (modal overlays), permission is denied, or the page is served over
 * a non-secure context other than localhost. In those cases we fall back
 * to a synthetic <textarea> + document.execCommand('copy') flow.
 *
 * @param {string} text - The text to copy.
 * @returns {Promise<boolean>} - true if copy succeeded, false otherwise.
 */
export async function copyToClipboard(text) {
  if (text == null) return false
  const value = String(text)

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value)
      return true
    } catch (err) {
      console.warn('navigator.clipboard.writeText failed, falling back:', err)
    }
  }

  return execCommandFallback(value)
}

function execCommandFallback(text) {
  if (typeof document === 'undefined') return false
  const ta = document.createElement('textarea')
  ta.value = text
  // Off-screen but still in the document so execCommand can read selection.
  ta.setAttribute('readonly', '')
  ta.style.position = 'fixed'
  ta.style.top = '0'
  ta.style.left = '0'
  ta.style.width = '1px'
  ta.style.height = '1px'
  ta.style.padding = '0'
  ta.style.border = 'none'
  ta.style.outline = 'none'
  ta.style.boxShadow = 'none'
  ta.style.background = 'transparent'
  ta.style.opacity = '0'

  const previouslyFocused = document.activeElement
  document.body.appendChild(ta)

  try {
    ta.focus()
    ta.select()
    ta.setSelectionRange(0, text.length)
    const ok = document.execCommand('copy')
    return !!ok
  } catch (err) {
    console.error('execCommand("copy") fallback failed:', err)
    return false
  } finally {
    document.body.removeChild(ta)
    if (previouslyFocused && typeof previouslyFocused.focus === 'function') {
      try { previouslyFocused.focus() } catch (_) { /* noop */ }
    }
  }
}

// ---------------------------------------------------------------------------
// #2515 — the result-returning API.
// ---------------------------------------------------------------------------

export const COPY_FEEDBACK_TTL_MS = 2000

export const COPY_CODE_LABEL = 'Copy'
export const COPY_CODE_ARIA = 'Copy code'
export const COPY_MESSAGE_ARIA = 'Copy message'

const FEEDBACK = Object.freeze({
  ok: Object.freeze({ label: 'Copied', tone: 'ok' }),
  unavailable: Object.freeze({ label: 'Copy unavailable', tone: 'error' }),
  denied: Object.freeze({ label: 'Copy blocked', tone: 'error' }),
  error: Object.freeze({ label: 'Copy failed', tone: 'error' }),
})

/**
 * The legacy path, for the case that is not an error at all: `navigator.clipboard`
 * is `undefined` on an insecure origin, and a Trinity reached over plain http on
 * a LAN or Tailscale address is a first-class deployment, not a misconfiguration.
 * Without this, Copy would be permanently dead for those operators and the
 * honest message would be all they ever got.
 *
 * `execCommand` is deprecated and universally implemented — the fallback every
 * clipboard library still ships. The textarea is removed in `finally`, so a
 * throw cannot leave it in the document.
 */
function copyViaExecCommand(text, doc) {
  if (!doc || typeof doc.execCommand !== 'function' || typeof doc.createElement !== 'function') {
    return false
  }
  let ta = null
  try {
    ta = doc.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    // Off-screen rather than hidden: a `display:none` element cannot be selected.
    if (ta.style) {
      ta.style.position = 'fixed'
      ta.style.top = '-1000px'
      ta.style.opacity = '0'
    }
    doc.body.appendChild(ta)
    ta.select?.()
    ta.setSelectionRange?.(0, text.length)
    return doc.execCommand('copy') === true
  } catch {
    return false
  } finally {
    try { ta?.remove?.() } catch { /* the document is already gone */ }
  }
}

/**
 * Copy `text`, and report which way it went.
 *
 * `clipboard.writeText` is called FIRST, with nothing awaited before it: Safari
 * grants clipboard access only inside the task the user's click started, and an
 * `await` before the write spends that transient activation.
 *
 * @returns {Promise<{ok: true, via: 'clipboard'|'execCommand'} |
 *                   {ok: false, reason: 'unavailable'|'denied'|'error'}>}
 */
export async function copyText(text, { clipboard, doc } = {}) {
  const s = String(text ?? '')
  const api = clipboard === undefined
    ? (typeof navigator !== 'undefined' ? navigator.clipboard : null)
    : clipboard
  const document_ = doc === undefined
    ? (typeof document !== 'undefined' ? document : null)
    : doc

  if (api && typeof api.writeText === 'function') {
    try {
      await api.writeText(s)
      return { ok: true, via: 'clipboard' }
    } catch (e) {
      // A denied permission is a different sentence from a broken one: one is
      // something the reader can fix in the address bar, the other is not.
      if (e && (e.name === 'NotAllowedError' || e.name === 'SecurityError')) {
        return { ok: false, reason: 'denied' }
      }
      return { ok: false, reason: 'error' }
    }
  }

  return copyViaExecCommand(s, document_)
    ? { ok: true, via: 'execCommand' }
    : { ok: false, reason: 'unavailable' }
}

/**
 * What the control says next. Identity in TEXT as well as colour — a control
 * that only turns red has said nothing to a reader who cannot see the change.
 */
export function copyFeedback(result) {
  if (result && result.ok) return FEEDBACK.ok
  const reason = result && result.reason
  return FEEDBACK[reason] || FEEDBACK.error
}
