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
import { hardenMediaAttributes, restrictToCanvasKit } from './sanitizeHooks'
import DOMPurify from 'dompurify'

/**
 * The one policy every markdown/html surface sanitises under (ent#537).
 *
 * `FORBID_TAGS: ['style']` is load-bearing: DOMPurify's default tag list
 * ADMITS `<style>`, and a `<style>` element in the body is document-global —
 * so before this an agent message, report or canvas block could ship
 * `<style>.x{position:fixed;inset:0}</style>` and restyle the whole page,
 * including a customer's Workspace on a `roster` canvas. No attribute
 * allowlist closes that, only forbidding the element does. Mermaid SVG is the
 * one surface that legitimately carries a `<style>` (its own, id-scoped) and
 * it has its own explicit entry point below.
 */
const BASE_CONFIG = Object.freeze({ FORBID_TAGS: ['style'] })

/**
 * Canvas mode (ent#537): the base policy plus the kit allowlist. `canvasKit`
 * is not a DOMPurify option — DOMPurify copies every key of the config it is
 * given and hands the whole object to each hook as its THIRD argument, so the
 * hook below reads it there. A per-call flag, not module state: there is no
 * "left set after a throw" mode. `id` is dropped because an agent-chosen id
 * collides with the app's own anchors and `aria-*` targets.
 */
const CANVAS_CONFIG = Object.freeze({ FORBID_TAGS: ['style'], FORBID_ATTR: ['id'], canvasKit: true })

// Allow target and rel attributes for links (DOMPurify strips them by default)
DOMPurify.addHook('afterSanitizeAttributes', (node, _data, config) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
  // ent#536 — the CSP admits https images page-wide, so every sanitised
  // <img> drops the referrer and no inline style may load a url(). The rule
  // is a pure module (`sanitizeHooks.js`) because this file cannot be imported
  // without a DOM.
  hardenMediaAttributes(node)
  // ent#537 — on a canvas, only the design kit's classes and a bounded
  // width/max-width survive. Canvas-scoped rather than app-wide because chat
  // and report markdown depend on the `code-block*` classes the decorator
  // injects before sanitising (#2515).
  if (config && config.canvasKit) restrictToCanvasKit(node)
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
  return DOMPurify.sanitize(decorateCodeBlocks(stripCodeBlockMarkers(marked(content))), BASE_CONFIG)
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
  return DOMPurify.sanitize(html, BASE_CONFIG)
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
  return DOMPurify.sanitize(String(html), BASE_CONFIG)
}

/**
 * Sanitise agent-authored HTML for a CANVAS block (ent#537): the base policy
 * plus the design-kit allowlist — a class outside `utils/canvasKit.js::
 * KIT_CLASSES` is dropped, an inline style keeps only a bounded `width` /
 * `max-width`, and `id` goes. Same DOMPurify instance, same hook; the kit
 * rule is a per-call config flag the hook reads.
 */
export function sanitizeCanvasHtml(html) {
  if (!html) return ''
  return DOMPurify.sanitize(String(html), CANVAS_CONFIG)
}

/**
 * Render markdown for a canvas block (ent#537) — `renderMarkdown` under the
 * canvas-mode policy, so raw kit markup inside the markdown keeps its `ck-*`
 * classes and nothing else.
 */
export function renderCanvasMarkdown(content) {
  if (!content) return ''
  return DOMPurify.sanitize(marked(content), CANVAS_CONFIG)
}

/**
 * Sanitise a Mermaid-rendered SVG for `v-html` (ent#536, split out by ent#537).
 *
 * The one entry point that does NOT forbid `<style>`: mermaid emits the
 * diagram's own id-scoped stylesheet inside the `<svg>`, and without it every
 * node renders unstyled. Same DOMPurify instance and hooks as everything
 * else; only the element policy differs, and it differs HERE, by name, so the
 * exception is visible rather than a default nobody chose.
 */
export function sanitizeSvg(svg) {
  if (!svg) return ''
  return DOMPurify.sanitize(String(svg))
}
