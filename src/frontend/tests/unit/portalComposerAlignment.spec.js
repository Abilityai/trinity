/**
 * #2662 — the composer is ONE shell: the field on top, the controls in a row
 * inside it. The shell carries the border, fill and focus ring; the textarea is
 * transparent and borderless. Both portal composers share the shape, which is
 * why every assertion below runs over both files (the #2211 lesson: the same
 * markup lives in two places and a fix in one silently leaves the twin broken).
 *
 * What #2259 left behind, still enforced: `block` on the textarea, `w-full`, and
 * 44px action boxes. What #2662 retired: `items-end` on the form. It mattered
 * only while the buttons shared a row with a growing field — and that sharing
 * is exactly what cost the field its width (143px at 375px, a placeholder over
 * four lines) and what left 34px when ent#403 tried to add a model picker to it.
 *
 * ---- the original #2259 note, kept because `block` is still load-bearing ----
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
// The one import of a real value in this otherwise source-text file: the ghost
// recipe's height is a VALUE, and reading it as text matches its own comment.
import { FIELD_GHOST_CLASS, FIELD_GHOST_VALID_CLASS } from '../../src/components/base/fieldClasses.js'

const read = (name) =>
  readFileSync(fileURLToPath(new URL(`../../src/components/portal/${name}`, import.meta.url)), 'utf8')

/**
 * The composer `<form>` only. Scoping matters: both files hold other textareas
 * and other `<button>`s (the voice-error dismiss, the agent picker), and a
 * file-wide match would assert this layout of controls that have nothing to do
 * with it.
 */
function composerForm(src) {
  const start = src.search(/<form[^>]*@submit\.prevent="send"/)
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
      // #2662: the anchor wrapper is the shell's first ROW now, not a flex item
      // competing with buttons, so it is `relative` and full width. The ref name
      // is unchanged — the typeahead's outside-click close reads `composerWrap`.
      expect(src).toMatch(/<div ref="composerWrap" class="relative"/)
    })

    it('puts the field and the controls in ONE shell that carries the chrome', () => {
      // #2662. The border, fill and focus ring moved off the textarea onto the
      // shell, which is what makes the controls read as inside the field. Two
      // boxes is the failure mode: a textarea that regains `rounded-2xl` or a
      // background nests a second field inside the first.
      const form = composerForm(src)
      expect(form, 'composer <form> not found — the scope anchor is stale').not.toBe('')
      // The ring is scoped to the FIELD (`has-[textarea:focus]`), not to any
      // descendant: `focus-within` lit the shell when an icon button was tabbed
      // onto and doubled the picker's own ring. 3px, like the field primitive.
      expect(form).toMatch(/<div\s+class="rounded-2xl border[^"]*has-\[textarea:focus\]:ring-\[3px\]/)
      expect(form).not.toMatch(/<div\s+class="rounded-2xl border[^"]*focus-within:/)
      expect(textareaTag(src)).toMatch(/class="[^"]*\bbg-transparent\b/)
      expect(textareaTag(src)).toMatch(/class="[^"]*\bborder-0\b/)
      expect(textareaTag(src)).not.toMatch(/class="[^"]*\brounded-2xl\b/)
    })

    it('bottom-aligns nothing, because the row no longer sits beside the field', () => {
      // The `items-end` this file was written for (#2259) was load-bearing only
      // while the buttons shared a row with a growing textarea. Stacked, the
      // control row is its own line and centres. Asserted rather than deleted:
      // reintroducing `items-end` here would be a silent revert to the layout
      // that cost the field its width — 143px at 375px, four wrapped lines.
      expect(composerForm(src)).not.toMatch(/<form[^>]*class="[^"]*\bitems-end\b/)
      expect(composerForm(src)).toMatch(/<div class="mt-1 flex items-center gap-1">/)
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

    it('lets a click on the shell land in the field', () => {
      // #2662. The chrome moved off the textarea, so the visible box is now
      // bigger than the field — its padding band and the control row's ground
      // read as "the input" and, without a handler, a click there lands on
      // <body>. Before the shell existed the box WAS the textarea. Both
      // surfaces, because both grew the same box.
      expect(composerForm(src)).toMatch(/@click="focusComposerFromShell"/)
      expect(src).toContain('function focusComposerFromShell(event) {')
      // The guard is the load-bearing half: an unconditional focus would steal
      // the click from every control in the row, and from the typeahead, which
      // picks on `mousedown` and is followed by a click that arrives here.
      expect(src).toMatch(/\[role="option"\]/)
    })

    it('holds a composer select to the same 44px box as the buttons beside it', () => {
      // #2662 FINDING-001. The buttons above are `<button>`s; the model picker
      // is a `<BaseSelect>`, so the loop above never saw it — and a 30px select
      // beside 44px buttons is a 30px tap target on a phone. Conditional
      // because only the 1:1 composer has one: the picker is per-agent and a
      // room has several, so PortalRoom's control row holds Send alone. Written
      // as "if there is a select, it wears the ghost recipe" rather than "the
      // select exists", so it keeps biting if the room ever gains one.
      for (const tag of composerForm(src).match(/<BaseSelect[\s\S]*?>/g) || []) {
        expect(tag).toMatch(/\bvariant="ghost"/)
      }
    })

    it('keeps Enter on a composer select from submitting the form', () => {
      // #2662 moved the picker INSIDE the <form>; on dev it was a sibling above
      // it. Chrome and Firefox route Enter on a focused <select> to the form's
      // default button, so the user who arrows to another model and presses
      // Enter to commit sends their draft unread. The regression is invisible in
      // every geometry assertion in this file, and the same trap waits for any
      // select a composer gains later — hence the same "if there is one" shape
      // as the ghost-recipe guard above, over both surfaces.
      for (const tag of composerForm(src).match(/<BaseSelect[\s\S]*?>/g) || []) {
        expect(tag).toMatch(/@keydown\.enter\.prevent/)
      }
    })
  })

  it('gives the ghost recipe the 44px height the composer row is built on', () => {
    // The other half of the guard above: the height lives in the shared recipe,
    // not in the markup, so asserting `variant="ghost"` at the call site only
    // means anything while the recipe still carries `h-11`.
    //
    // Asserted against the IMPORTED VALUE, not the source text. The first
    // version of this read fieldClasses.js as a string and matched /\bh-11\b/,
    // which passed with `h-11` deleted from the class — because the comment
    // above the constant explains the choice and contains the literal `h-11`.
    // A source-text guard over a documented constant tests the prose.
    expect(FIELD_GHOST_CLASS.split(/\s+/)).toContain('h-11')
    // Content-width is the property that keeps the picker out of the field's
    // width budget: `w-full` here would re-create the ent#403 squeeze one level
    // down, with the picker taking the row instead of the field.
    expect(FIELD_GHOST_CLASS.split(/\s+/)).not.toContain('w-full')
  })

  it('keeps the ghost recipe out of every cascade race it can be written into', () => {
    // These are SHAPE guards, and they are honest about what they cannot do: a
    // class-string assertion is structurally blind to the cascade (#2662's own
    // learnings entry — the shell's missing light-mode border was green under
    // test:unit, check:tokens and the ratchet, and only `getComputedStyle` off a
    // live render found it). What a string CAN pin is the arrangement that makes
    // the cascade safe, so a future edit has to re-open the question deliberately.
    //
    // Both of the arrangements below were wrong when this variant first shipped,
    // and both were measured wrong in a real Chromium before being changed.
    const ghost = FIELD_GHOST_CLASS.split(/\s+/)
    const valid = FIELD_GHOST_VALID_CLASS.split(/\s+/)

    // 1. The resting border COLOUR lives on the valid arm, never in the base
    //    string. With `border-transparent` in the base, a ghost select carrying
    //    an `error` rendered with NO danger border: `.border-transparent` is
    //    emitted after `.border-status-danger-500` at equal specificity, so the
    //    resting keyword beat the error colour. `field` never had the bug because
    //    FIELD_CLASS has always kept its border colour on the arms. Measured:
    //    ghost error border was rgba(0,0,0,0) against field's rgb(239,68,68).
    expect(ghost).toContain('border')
    expect(ghost.filter((c) => /^border-(?!\[)[a-z]/.test(c))).toEqual([])
    expect(valid).toContain('border-transparent')

    // 2. Every dark hover tint is paired with a dark disabled reset. The
    //    unvariated `disabled:hover:bg-transparent` outranks `hover:bg-gray-100`
    //    (3 classes/pseudos vs 2) but merely TIES `dark:hover:bg-gray-750`, which
    //    compiles to `:hover:is(.dark *)` and is emitted later — so a disabled
    //    ghost select still lit up under the cursor in dark mode while light was
    //    correct. Written as a pairing over whatever dark hover tints exist, so a
    //    second one added later is covered without editing this test.
    const darkHoverTints = ghost.filter((c) => /^dark:hover:bg-/.test(c))
    expect(darkHoverTints.length).toBeGreaterThan(0)
    expect(ghost).toContain('disabled:hover:bg-transparent')
    expect(ghost, 'a dark hover tint needs its dark disabled reset in the same breath')
      .toContain('dark:disabled:hover:bg-transparent')
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
