/**
 * #2259 — the composer's buttons must align to the INPUT, not to its wrapper.
 *
 * ent#392 wrapped the textarea in a `relative` div so the typeahead popup had
 * something to anchor to. A `<textarea>` is inline-block, so inside that block
 * wrapper it sat on the baseline and the line box reserved descender space
 * beneath it — the wrapper rendered 6px taller than the field it contained.
 * `items-end` aligns the flex ITEM, i.e. the wrapper, so the buttons
 * bottom-aligned to that dead space and Send hung 6px below the visible input
 * edge. Measured on dev before the fix (Chromium, 1440px):
 *
 *     wrapper  : top 693  bottom 745  h 52
 *     textarea : top 693  bottom 739  h 46   <- 6px of dead space
 *     sendBtn  : top 705  bottom 745  h 40   <- flush with the wrapper, not the field
 *
 * `block` on the textarea removes the line box, so wrapper height == field height.
 *
 * These are SOURCE assertions rather than rendered ones because this project has
 * no component-mount harness (`vitest` runs `environment: 'node'`), which is the
 * same reason `portalComposerTypeahead.spec.js` reads the SFC as text. The
 * geometry itself was verified in a real browser against this branch; what a test
 * can hold here is that neither file quietly loses the two classes that produce
 * it. Both files are asserted together because the composer is the same markup in
 * two places — the #2211 lesson, where a fix landing in one left the twin broken.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const read = (name) =>
  readFileSync(fileURLToPath(new URL(`../../src/components/portal/${name}`, import.meta.url)), 'utf8')

/**
 * The composer `<form>` only. Scoping matters: both files hold other textareas
 * and other `<button>`s (the voice-error dismiss, the agent picker), and a
 * file-wide match would assert this layout of controls that have nothing to do
 * with it.
 */
function composerForm(src) {
  const start = src.search(/<form[^>]*class="[^"]*\bitems-end\b/)
  if (start === -1) return ''
  const end = src.indexOf('</form>', start)
  if (end === -1) return ''
  // Comments are stripped, not skipped past: the block explaining THIS fix names
  // `<textarea>` in prose, and a tag matcher run over the raw source picks that
  // up and then asserts the layout of a sentence.
  return stripHtmlComments(src.slice(start, end))
}

/**
 * Remove every `<!-- … -->` span. Index-walked rather than a single regex
 * `replace`: this is a source-text scrub of a checked-in SFC, not an HTML
 * sanitizer, but CodeQL cannot tell the two apart and flags a one-pass
 * `<!--…-->` replace as an incomplete multi-character sanitization (a `<!--`
 * assembled from the halves of two removed spans survives it). The walk has no
 * such residue: everything between an opener and its closer is dropped, and an
 * unterminated opener drops the rest of the input.
 */
function stripHtmlComments(text) {
  let out = ''
  let i = 0
  for (;;) {
    const open = text.indexOf('<!--', i)
    if (open === -1) return out + text.slice(i)
    out += text.slice(i, open)
    const close = text.indexOf('-->', open + 4)
    if (close === -1) return out
    i = close + 3
  }
}

const SURFACES = [
  ['PortalConversation.vue', read('PortalConversation.vue')],
  ['PortalRoom.vue', read('PortalRoom.vue')],
]

/** The composer textarea's tag, with its attributes. */
function textareaTag(src) {
  const m = composerForm(src).match(/<textarea[\s\S]*?>/)
  return m ? m[0] : ''
}

describe('#2259 workspace composer alignment', () => {
  describe.each(SURFACES)('%s', (_name, src) => {
    it('renders the textarea as a block, so the wrapper cannot be taller than the field', () => {
      // The whole defect in one class. Without it the wrapper carries ~6px of
      // baseline descender space and every `items-end` alignment below is
      // measured against a box the user cannot see.
      expect(textareaTag(src)).toMatch(/class="[^"]*\bblock\b/)
    })

    it('still lets the field fill the flex track', () => {
      // `block` must be ADDITIVE — a textarea that stopped being `w-full` would
      // collapse to its `cols` width, trading one layout bug for another.
      expect(textareaTag(src)).toMatch(/class="[^"]*\bw-full\b/)
      expect(src).toContain('relative flex-1 min-w-0')
    })

    it('keeps the row bottom-aligned so the buttons follow the last line as it grows', () => {
      // Centring would drift the buttons upward with every added line.
      expect(src).toMatch(/<form[^>]*class="[^"]*\bitems-end\b/)
    })

    it('sizes every action button as a 44px box rather than padding around an icon', () => {
      // 44px is on the 4px grid the design contract asks for, and against the
      // 46px single-line composer it puts the icon's centre within 1px of the
      // text line — in the collapsed AND the grown state, since `items-end`
      // measures from the bottom either way. `p-2.5` gave a 40px box, i.e. an
      // icon sitting 3px low even once the wrapper gap was gone.
      const composerButtons = composerForm(src).match(/<button[\s\S]*?>/g) || []
      expect(composerButtons.length).toBeGreaterThan(0)
      for (const b of composerButtons) {
        expect(b).toMatch(/\bh-11\b/)
        expect(b).toMatch(/\bw-11\b/)
        // A fixed box only centres its glyph if it is told to.
        expect(b).toMatch(/\bitems-center\b/)
        expect(b).toMatch(/\bjustify-center\b/)
      }
    })
  })

  it('initialises the room composer once the room has resolved', () => {
    // PortalRoom's composer sits behind `v-if="!isClosed"`, so it does not exist
    // when the component mounts. It gained `autoGrow()` in #2211 but nothing ever
    // called it at startup, leaving `overflow-y` at its stylesheet `auto` — the
    // state #2211 replaced with an explicit `hidden`. Invisible at rest only
    // because the untouched `rows="1"` box happens to fit its one line.
    const src = read('PortalRoom.vue')
    const mounted = src.match(/onMounted\(async \(\) => \{[\s\S]*?\n\}\)/)
    expect(mounted, 'PortalRoom must have an async onMounted').toBeTruthy()
    expect(mounted[0]).toMatch(/await load\(\{ full: true \}\)[\s\S]*autoGrowAfterUpdate\(\)/)
    // Deferred, not synchronous: the textarea is patched in on the next tick.
    expect(src).toMatch(/function autoGrowAfterUpdate\(\)\s*\{\s*nextTick\(autoGrow\)/)
  })
})
