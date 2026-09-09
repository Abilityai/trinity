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

  it('applies to both recipes, so no variant is left out of the idiom', () => {
    // The flip rides on the shared <select>, not on `recipe`, so `field` and
    // `ghost` behave identically. A per-variant chevron rule would be the
    // "third variant half-added" failure the recipe computed exists to prevent.
    const recipeBlock = CODE.slice(CODE.indexOf('const recipe = computed'))
    expect(recipeBlock).not.toContain('rotate-180')
  })
})
