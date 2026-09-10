/**
 * #2662 — the BaseSelect chevron points up while its picker is open.
 *
 * The rest of the app already flips a chevron on open (ChatHistoryDropdown,
 * OverflowTabs, InfoPanel, and the disclosure rows in LoopsPanel / TasksPanel /
 * PortalAgentDetails / PortalDeliverables / ChannelDisclosure). Every one of
 * those owns its open state in JS. A NATIVE <select>'s picker is drawn by the
 * platform and reports nothing to the page, so `:open` is the only hook there
 * is — which is why selects were the one control excluded from the idiom.
 *
 * Scoped to the `ghost` recipe ONLY. #2662 is a Workspace composer bug and
 * `field` is what Settings and ResourceModal render, so widening the idiom to
 * every select in the app is a separate issue with its own review. The last
 * test pins that boundary in the direction it can actually drift — someone
 * "tidying" the flip onto the shared <select> would silently change surfaces
 * this PR does not own.
 *
 * Vitest runs `environment: 'node'` here with no mount harness, so these are
 * source-structure assertions like their siblings. Comments are STRIPPED before
 * matching: this PR already shipped a guard that passed on the prose above the
 * constant it was meant to pin, and the block comment in BaseSelect.vue names
 * `:open` in exactly the way that would fake a pass.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'

const SRC = readFileSync(
  fileURLToPath(new URL('../../src/components/base/BaseSelect.vue', import.meta.url)),
  'utf8'
)
// The comment stripper is the SHARED one (`tests/unit/helpers/stripComments.js`,
// extracted in #2161 with the note that it "was about to gain a third copy, and
// the copies carried a real defect"). This file shipped a fourth copy, and then
// re-derived by hand the CodeQL fix — js/incomplete-multi-character-sanitization,
// a non-greedy `<!--[\s\S]*?-->` leaving residue — that the shared helper had
// already solved and documents at length. Importing it is the same rule the
// component under test is here to enforce: reach for the primitive.
const CODE = stripComments(SRC)

describe('#2662 BaseSelect open-state chevron', () => {
  it('strips the prose it claims to strip', () => {
    // Without this, a stripper that returned its input unchanged would pass
    // every assertion below — the whole point is that the block comment names
    // `:open`, `ghost` and `field` in the exact shapes being asserted on.
    const proseOnly = 'Deliberately NOT on `field`'
    expect(SRC).toContain(proseOnly)
    expect(CODE).not.toContain(proseOnly)
    // ...while the code itself survives the scan intact.
    expect(CODE).toContain('FIELD_GHOST_CLASS')
    expect(CODE).toContain('const recipe = computed')
  })

  it('flips the chevron while the native picker is open', () => {
    expect(CODE).toContain('[&:open~svg]:rotate-180')
    // Resolved through the recipe, never hard-coded on the shared <select>.
    expect(CODE).toMatch(/:class="\[recipe\.field,[^"]*recipe\.flip\]"/)
  })

  it('keeps the chevron as the select\'s immediate next sibling', () => {
    // `~` is the GENERAL sibling combinator, so the CSS only needs the svg to
    // FOLLOW the select — an element between the two still matches (verified in
    // Chrome 151: `select ~ svg` yes, `select + svg` no). This asserts the
    // stricter adjacency on purpose, because it is the cheap shape to keep and
    // it also catches the two edits that DO kill the flip silently: wrapping the
    // svg, and moving it above the select. Neither leaves a trace any other
    // class-string assertion would notice — the class stays exactly where it is.
    expect(CODE).toMatch(/<\/select>\s*<svg/)
  })

  it('animates the flip and holds still under reduced motion', () => {
    expect(CODE).toContain('transition-transform')
    expect(CODE).toContain('motion-reduce:transition-none')
  })

  it('stays scoped to ghost — `field` is untouched by this issue', () => {
    const recipeBlock = CODE.slice(CODE.indexOf('const recipe = computed'))
    const [ghostArm, fieldArm] = recipeBlock.split('FIELD_CLASS')
    // The flip and its transition are resolved in the one `recipe` computed, on
    // the ghost arm. Anything on the shared <select> would reach `field` too.
    expect(ghostArm).toContain('[&:open~svg]:rotate-180')
    expect(ghostArm).toContain('transition-transform')
    expect(fieldArm).not.toContain('rotate-180')
    expect(fieldArm).not.toContain('transition-transform')
    expect(fieldArm).toContain("flip: ''")
  })
})
