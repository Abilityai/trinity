/**
 * #2666 — the Files-tab preview must not grow past the panel's clip.
 *
 * The panel root is `flex h-[calc(100vh-220px)] overflow-hidden` and nothing in
 * the chain scrolls horizontally. The preview column beside the file tree was a
 * flex item at the default `min-width: auto`, i.e. it could not shrink below the
 * MIN-CONTENT width of everything inside it — and the `<pre>` that renders file
 * text had no break rule, so one unbreakable token (a path, a URL, a base64
 * blob, a run of `===`) became that min-content width. The pane grew to fit the
 * token, the root clipped it, and there was no scrollbar anywhere to reach it.
 * Because the `<pre>` is `whitespace-pre-wrap`, ORDINARY lines then wrapped at
 * the blown-out width too, so the whole preview was cut off, not just the token.
 *
 * Both classes are load-bearing, and each fixes a different half — measured in a
 * real browser (Chromium, 1280px viewport, a file whose content holds one
 * 320-char unbreakable token), reading `scrollWidth/clientWidth`:
 *
 *   neither          root 3048/1280   pane 2768   pre 2720   sidebar 280px
 *   break-words only root 3048/1280   pane 2768   pre 2720   sidebar 280px
 *   min-w-0 only     root 1280/1280   pane  960   pre 2720/912 (scrollbar)
 *   both             root 1280/1280   pane  960   pre  912/912  sidebar 320px
 *
 * `break-words` alone changes nothing: with `min-width: auto` the pane still
 * sizes to min-content, which a break-word rule does not reduce. `min-w-0` alone
 * satisfies the "pane stops growing" half but leaves the token overflowing into
 * the `<pre>`'s own `overflow-auto` — a horizontal scrollbar at the bottom of an
 * element that can be 80 KB tall, i.e. below the fold and unreachable for
 * exactly the files that need it. Only the pair leaves every line readable.
 *
 * vitest here runs `environment: 'node'` with no component-mount harness, and
 * even a DOM harness computes no layout — so the browser measurement above
 * cannot live in CI. This is the source-structure guard for it, the same shape
 * as `agentDetailDeepLink.spec.js` and the Python AST guards.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

const filesPanel = read('../../src/components/FilesPanel.vue')
const filePreview = read('../../src/components/file-manager/FilePreview.vue')
const portalPreview = read('../../src/components/portal/PortalFilePreview.vue')

/** Classes of the element whose class attribute contains every given token. */
function classesOfElementWith(source, ...tokens) {
  const matches = [...source.matchAll(/class="([^"]*)"/g)]
    .map((m) => m[1])
    .filter((cls) => tokens.every((t) => cls.split(/\s+/).includes(t)))
  expect(matches, `no element found with classes ${tokens.join(' + ')}`).toHaveLength(1)
  return matches[0].split(/\s+/)
}

/** The break rules that make a long unbreakable token wrap rather than overflow. */
const BREAK_RULES = ['break-words', 'break-all', 'overflow-wrap-anywhere']

describe('#2666 Files-tab preview stays inside the panel clip', () => {
  it('the preview column can shrink below its min-content width', () => {
    // The right-hand pane in the two-panel layout. Identified by its own
    // background, which nothing else in the file carries.
    const cls = classesOfElementWith(filesPanel, 'flex-1', 'bg-gray-50', 'dark:bg-gray-900')
    expect(cls).toContain('min-w-0')
  })

  it('the panel root still clips, which is why min-w-0 is required', () => {
    // If this ever stops being true — because the root gained a horizontal
    // scroll instead — the reasoning above needs re-deriving, not just the class.
    const cls = classesOfElementWith(filesPanel, 'h-[calc(100vh-220px)]')
    expect(cls).toContain('overflow-hidden')
  })

  it('the text preview breaks an unbreakable token instead of overflowing', () => {
    const cls = classesOfElementWith(filePreview, 'whitespace-pre-wrap')
    expect(BREAK_RULES.some((rule) => cls.includes(rule))).toBe(true)
  })

  it('matches the PortalFilePreview precedent it was fixed against', () => {
    // The Workspace preview renders text the same way and has never shown this;
    // #2666 is the agent-side panel catching up. If that file changes its mind
    // about wrap-vs-scroll, the two surfaces should be reconciled deliberately.
    const cls = classesOfElementWith(portalPreview, 'whitespace-pre-wrap')
    expect(BREAK_RULES.some((rule) => cls.includes(rule))).toBe(true)
  })
})
