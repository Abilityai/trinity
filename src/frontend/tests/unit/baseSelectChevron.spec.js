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

const SRC = readFileSync(
  fileURLToPath(new URL('../../src/components/base/BaseSelect.vue', import.meta.url)),
  'utf8'
)
// HTML comments, block comments and line comments — the prose must not be able
// to satisfy any assertion below.
const CODE = SRC.replace(/<!--[\s\S]*?-->/g, '')
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '')

describe('#2662 BaseSelect open-state chevron', () => {
  it('flips the chevron while the native picker is open', () => {
    expect(CODE).toContain('[&:open~svg]:rotate-180')
    // Resolved through the recipe, never hard-coded on the shared <select>.
    expect(CODE).toMatch(/:class="\[recipe\.field,[^"]*recipe\.flip\]"/)
  })

  it('keeps the chevron as the select\'s immediate next sibling', () => {
    // The rule is a sibling combinator, so the chevron must FOLLOW the select
    // with no element between them. Wrapping the svg — or moving it above the
    // select — leaves the class in place and silently kills the flip, which no
    // class-string assertion elsewhere would notice.
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
