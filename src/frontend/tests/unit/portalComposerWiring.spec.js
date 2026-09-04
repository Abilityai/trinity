/**
 * #2211 review follow-up — the WIRING of `autoGrow()`, not its arithmetic.
 *
 * Round one extracted `resolveComposerGrowth()` and tested it thoroughly, and every
 * one of the six review findings still slipped through, because all six were in how
 * and where the function is CALLED:
 *
 *   * called synchronously after a programmatic `input.value = …`, so it measured the
 *     old content (Vue patches `v-model` on the next microtask) — and with
 *     `overflow-y` now pinned, that leaves text clipped AND unscrollable rather than
 *     merely un-resized
 *   * called after the paste/drop early-return, so a pasted message never grew
 *   * never called on rewrap, so narrowing the window could clip a fitting draft
 *   * `prose-portal` applied in PortalRoom but defined only in PortalConversation
 *
 * There is no component-mount harness in this project, so these are source-structure
 * guards in the shape of `portalVoiceModePersistence.spec.js` — the same tool the
 * repo already uses for wiring that a pure-function test cannot reach.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

const FILES = ['PortalConversation', 'PortalRoom']
const src = (name) => readFileSync(
  fileURLToPath(new URL(`../../src/components/portal/${name}.vue`, import.meta.url)), 'utf8',
)
// Strip comments: they necessarily quote the very patterns these guards scan for.
const code = (name) => src(name)
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '')

describe.each(FILES)('#2211 wiring — %s', (name) => {
  it('defers growth after every programmatic input mutation', () => {
    const text = code(name)
    // `input.value = <something>` is a programmatic write; the DOM is stale until the
    // next tick, so a bare synchronous `autoGrow()` on the following line is the bug.
    const lines = text.split('\n')
    lines.forEach((line, i) => {
      if (!/^\s*input\.value = /.test(line)) return
      const next = lines.slice(i + 1, i + 3).join(' ')
      if (!/autoGrow/.test(next)) return
      expect(next, `${name}:${i + 1} grows synchronously after a programmatic write`)
        .toMatch(/autoGrowAfterUpdate|nextTick/)
    })
  })

  it('has a deferred variant at all', () => {
    expect(code(name)).toMatch(/function autoGrowAfterUpdate\(\)\s*\{\s*nextTick\(autoGrow\)/)
  })

  it('grows before the paste/drop early-return', () => {
    const body = code(name).split('function onComposerInput')[1].split('\n}')[0]
    const growAt = body.indexOf('autoGrow')
    const returnAt = body.indexOf('return')
    expect(growAt).toBeGreaterThan(-1)
    expect(growAt, `${name}: paste returns before growing`).toBeLessThan(returnAt)
  })

  it('recomputes on viewport resize, and unregisters', () => {
    const text = code(name)
    expect(text).toMatch(/window\.addEventListener\('resize', onViewportResize\)/)
    expect(text).toMatch(/window\.removeEventListener\('resize', onViewportResize\)/)
  })

  it('regrows after a typeahead pick', () => {
    const body = code(name).split('function acceptActive')[1]
    expect(body.split('\n}\n')[0]).toMatch(/autoGrow/)
  })

  it('does not define prose-portal locally — PortalMarkdown is the one home', () => {
    // This assertion used to require the OPPOSITE: each transcript had to carry
    // its own copy of the stylesheet, because the class was applied in both and
    // defined in one, so a room transcript silently got no rules at all. Two
    // copies "kept byte-identical so they cannot drift" is the tell that they
    // wanted to be one thing — #2515 made them one (PortalMarkdown.vue), and
    // the guard now protects the merge instead of the duplication.
    const text = src(name)
    expect(text, `${name} defines prose-portal rules again`)
      .not.toMatch(/\.prose-portal :deep\(/)
    expect(code(name), `${name} should render the shared bubble`)
      .toMatch(/PortalAgentBubble/)
  })
})
