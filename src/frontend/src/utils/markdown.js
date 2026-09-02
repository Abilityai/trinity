/**
 * Shared markdown rendering utility with DOMPurify sanitization (H-005).
 *
 * All v-html content in the app should use this utility to prevent XSS
 * from agent responses, dashboard widgets, queue items, etc.
 */
import { marked } from 'marked'
import DOMPurify from 'dompurify'

// Configure marked globally
marked.setOptions({
  breaks: true,
  gfm: true
})

// Custom renderer: open links in new tab (marked v5+ token object API)
marked.use({
  renderer: {
    link({ href, title, text }) {
      const titleAttr = title ? ` title="${title}"` : ''
      return `<a href="${href}"${titleAttr} target="_blank" rel="noopener noreferrer">${text}</a>`
    }
  }
})

// Allow target and rel attributes for links (DOMPurify strips them by default)
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

/**
 * Render markdown to sanitized HTML.
 *
 * @param {string} content - Raw markdown string
 * @returns {string} Sanitized HTML string safe for v-html
 */
export function renderMarkdown(content) {
  if (!content) return ''
  const html = marked(content)
  return DOMPurify.sanitize(html)
}

/**
 * Sanitize agent-authored HTML for `v-html` (ent#438).
 *
 * The sibling of `renderMarkdown` for content that arrives as markup rather
 * than markdown — the canvas `html` block, which is what the Gemini voice
 * panel tools write. It goes through the SAME DOMPurify instance, so the
 * link hardening configured above (`target=_blank`, `rel=noopener
 * noreferrer`) applies to it too; a second sanitizer would be a second policy
 * to keep in step, which is the H-005 failure one level up.
 *
 * @param {string} html - Raw agent-authored HTML
 * @returns {string} Sanitized HTML safe for v-html
 */
export function sanitizeHtml(html) {
  if (!html) return ''
  return DOMPurify.sanitize(String(html))
}
