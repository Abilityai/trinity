/**
 * Shared markdown rendering utility with DOMPurify sanitization (H-005).
 *
 * All v-html content in the app should use this utility to prevent XSS
 * from agent responses, dashboard widgets, queue items, etc.
 */
// The configured parser, from its one home (#2515). `marked.use` mutates the
// package singleton, so the configuration lives in a module a unit test can
// import — this file cannot be imported without a DOM (DOMPurify's stub has no
// `addHook`), which would otherwise put the parser's behaviour out of reach.
import { marked } from './markedConfig'
import { decorateCodeBlocks, stripCodeBlockMarkers } from './codeBlocks'
import DOMPurify from 'dompurify'

// Allow target and rel attributes for links (DOMPurify strips them by default)
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

/**
 * Render markdown to sanitized HTML, with each code block wrapped in a labelled
 * bar carrying a Copy control (#2515).
 *
 * A SEPARATE export rather than an option on `renderMarkdown`, and emphatically
 * not a global `marked.use({ renderer: { code } })`: `renderMarkdown` has twelve
 * consumers — dashboards, queue cards, reports, executions, loops, the Agent
 * Detail chat — and a global override would sprout a Workspace copy control on
 * every one of them. Decoration is opt-in per surface.
 *
 * Order is load-bearing. The markers are stripped from the parser's output
 * BEFORE decoration, so an agent cannot ship its own wrapper (see
 * `stripCodeBlockMarkers`); decoration runs BEFORE sanitization, so every byte
 * that reaches `v-html` has passed the one DOMPurify policy.
 *
 * @param {string} content - Raw markdown string
 * @returns {string} Sanitized HTML string safe for v-html
 */
export function renderMarkdownWithCodeBlocks(content) {
  if (!content) return ''
  return DOMPurify.sanitize(decorateCodeBlocks(stripCodeBlockMarkers(marked(content))))
}

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
