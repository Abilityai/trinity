/**
 * Post-render code-block decoration (#2515) — pure, and deliberately importing
 * NOTHING.
 *
 * Not `marked` (the caller owns parsing), not `dompurify` (which cannot be
 * imported in a DOM-less node process), not Vue. That is what makes every rule
 * below reachable from a unit test, which is the point: this module builds
 * HTML, so its behaviour on hostile input has to be provable rather than
 * inspected.
 *
 * The pipeline is `marked → stripCodeBlockMarkers → decorateCodeBlocks →
 * DOMPurify.sanitize`. Decoration runs BEFORE sanitization on purpose, so every
 * byte that ever reaches `v-html` has passed the one DOMPurify policy (H-005
 * stays literally true rather than "true except for the wrapper").
 */

/** The marker attributes the decorator emits, and the SFC's handler selects on. */
export const DATA_CODE_BLOCK = 'data-code-block'
export const DATA_COPY_CODE = 'data-copy-code'

/**
 * Everything the decorator injects, so the sanitizer pin is DERIVED from the
 * emitter rather than a hand-copied list that drifts from it.
 */
export const CODE_BLOCK_VOCAB = Object.freeze({
  tags: Object.freeze(['div', 'span', 'button', 'pre', 'code']),
  attrs: Object.freeze(['class', 'type', 'aria-label', DATA_CODE_BLOCK, DATA_COPY_CODE]),
})

export const CODE_BLOCK_FALLBACK_LABEL = 'code'

const OPEN = '<pre><code'
const CLOSE = '</code></pre>'
// The label is the ONLY non-constant byte injected into the HTML, so its
// charset excludes every character that could end an attribute or open a tag.
const LABEL_RE = /^[a-z0-9][a-z0-9_+#.-]{0,23}$/
const LANGUAGE_CLASS_RE = /\blanguage-([^"\s]+)/

/**
 * The language label for a fence's info string, or `''` when it is not one we
 * are willing to print. `c++` and `objective-c` pass; `a"b` does not, and the
 * bar falls back to a neutral "code" rather than dropping the block.
 */
export function codeLanguageLabel(info) {
  const s = String(info ?? '').trim().toLowerCase()
  return LABEL_RE.test(s) ? s : ''
}

/** marked terminates a block with exactly one newline; the clipboard should not carry it. */
export function codeBlockText(text) {
  const s = String(text ?? '')
  return s.endsWith('\n') ? s.slice(0, -1) : s
}

/**
 * Remove the decorator's marker attributes from UNTRUSTED input.
 *
 * marked passes raw HTML in markdown straight through, and DOMPurify keeps
 * `data-*`. Without this, an agent could emit its own `<div data-code-block>`
 * carrying a `<button data-copy-code>` — a control that looks like ours and
 * copies something the reader cannot see. Pastejacking, delivered by whatever
 * the agent was told to say.
 *
 * Stripping the markers from the input means only decorator-built wrappers can
 * ever carry them, so the handler's `closest('[data-code-block]')` can only
 * ever land on a wrapper this module built.
 */
export function stripCodeBlockMarkers(html) {
  const s = String(html ?? '')
  if (!s) return ''
  // Every attribute spelling: bare, ="…", ='…', =…  — value-less first would
  // leave `="x"` dangling, so the valued forms are matched ahead of the bare one.
  return s.replace(
    /\s(?:data-code-block|data-copy-code)(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]*))?/gi,
    '',
  )
}

/**
 * Wrap each of marked's code blocks in a labelled bar with a Copy control.
 *
 * A LINEAR indexOf scan rather than a regex: the obvious lazy-regex version is
 * quadratic on adversarial input (a message of tens of thousands of `<pre><code>`
 * openers measured in seconds, on the render path).
 *
 * SINGLE-PASS BY CONTRACT — the caller runs it once per render. There is
 * deliberately no "already decorated, bail out" early return: it would key on a
 * marker in the input, and one agent-authored `code-block` div would then
 * disable decoration for the entire message.
 *
 * Two structural rules keep this from decorating something it should not:
 *
 *   1. It matches the BARE `<pre><code` opener that marked emits. A `<pre
 *      style="display:none">` written as raw HTML does not match, so a hidden
 *      block never gets a Copy button.
 *   2. It decorates only blocks whose body carries no literal `<`. marked
 *      HTML-escapes fence contents (`x<y` → `x&lt;y`), so a `<` inside a body
 *      proves the block came through raw-HTML passthrough rather than from the
 *      parser — and a raw block could nest a hidden element whose text a copy of
 *      the wrapper's `pre` would silently pick up. Genuine fenced, indented and
 *      `~~~` blocks are all escaped, so all of them still qualify.
 */
export function decorateCodeBlocks(html) {
  const s = String(html ?? '')
  if (!s) return ''

  let out = ''
  let cursor = 0

  for (;;) {
    const start = s.indexOf(OPEN, cursor)
    if (start === -1) break

    const tagEnd = s.indexOf('>', start + OPEN.length)
    if (tagEnd === -1) break

    const bodyStart = tagEnd + 1
    const close = s.indexOf(CLOSE, bodyStart)
    if (close === -1) break

    const attrs = s.slice(start + OPEN.length, tagEnd)
    const body = s.slice(bodyStart, close)
    const blockEnd = close + CLOSE.length

    // Rule 2 above. Skip past this block without decorating it; its bytes are
    // still emitted verbatim and still sanitized by the caller.
    if (body.includes('<')) {
      out += s.slice(cursor, blockEnd)
      cursor = blockEnd
      continue
    }

    const classMatch = attrs.match(LANGUAGE_CLASS_RE)
    const label = classMatch ? codeLanguageLabel(classMatch[1]) : ''

    out += s.slice(cursor, start)
    out += `<div class="code-block" ${DATA_CODE_BLOCK}>`
    out += '<div class="code-block-bar">'
    out += `<span class="code-block-lang">${label || CODE_BLOCK_FALLBACK_LABEL}</span>`
    out += `<button type="button" class="code-block-copy" ${DATA_COPY_CODE} aria-label="Copy code">Copy</button>`
    out += '</div>'
    out += s.slice(start, blockEnd)
    out += '</div>'
    cursor = blockEnd
  }

  return cursor === 0 ? s : out + s.slice(cursor)
}
