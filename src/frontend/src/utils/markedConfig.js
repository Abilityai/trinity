/**
 * The ONE marked configuration.
 *
 * `marked.setOptions` and `marked.use` mutate the package singleton, so there
 * is exactly one configured parser in the app and every consumer shares it.
 * That is fine — what is not fine is configuring it inside `markdown.js`, which
 * cannot be imported in a DOM-less node process: DOMPurify without a DOM
 * exports a stub with no `addHook`, so `markdown.js` throws at import time and
 * no unit test can reach the parser it configures.
 *
 * Splitting the config out means a spec can exercise the SAME parser the app
 * builds. That matters for the code-block decorator (#2515), which keys on
 * marked's exact fence output: a future syntax highlighter registered here
 * changes that output, and the spec must go red rather than stay green while
 * every Copy button silently disappears.
 *
 * Register future marked configuration HERE, not at a call site.
 */
import { marked } from 'marked'

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

export { marked }
