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

