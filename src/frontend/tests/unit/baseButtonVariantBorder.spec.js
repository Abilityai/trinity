/**
 * BaseButton's resting border belongs to the VARIANT, never to the base string.
 *
 * The defect: the base class string carried `border-transparent` alongside the
 * reserved `border` width, and `secondary` — the one variant whose border is
 * part of its design — set `border-gray-300`. Both are single-class selectors,
 * so specificity ties and emission order decides. Tailwind emits
 * `.border-transparent` AFTER `.border-gray-300` (measured on the live dev
 * stylesheet: rule 729 vs 698), so the resting keyword won and every secondary
 * button rendered borderless: a white button on a white card with no outline,
 * across 24 call sites.
 *
 * It read as a light-only bug because dark mode won by ACCIDENT, not by design —
 * `dark:border-gray-700` compiles to `.dark .border-gray-700`, one class
 * heavier, which outranks the tie. That asymmetry is why this survived review:
 * the theme most of the team works in was correct.
 *
 * This is the same cascade trap `fieldClasses.js` already documents for
 * FIELD_GHOST_CLASS ("Carrying `border-transparent` in this base string made the
 * ghost's error state borderless"), one primitive over. Two primitives hitting
 * one trap is the reason this is a guard and not just a fix.
 *
 * @source-text-pin: the defect is a CASCADE tie broken by stylesheet emission
 * order, and neither jsdom nor @vue/test-utils evaluates a stylesheet — a mount
 * would report the class list it was handed and prove nothing. This is the
 * deliberate-pin case #2918's ratchet carves out, not a missing mount.
 *
 * (`vitest.config.js` pins `environment: 'node'` only as the DEFAULT; a spec
 * opts into jsdom per file and mounts, which 22 specs already do. That harness
 * exists and is reachable — it simply cannot see a cascade, which is why this
 * one spec stays at the source level.) A source guard cannot observe the
 * cascade, but it pins the one edit that reintroduces it: a colour keyword
 * migrating back into the shared string.
 *
 * Comments are STRIPPED before matching. The block comment in BaseButton.vue
 * names `border-transparent` and `border-gray-300` in exactly the shapes
 * asserted below, so an un-stripped scan would pass on the prose alone.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'

const SRC = readFileSync(
  fileURLToPath(new URL('../../src/components/base/BaseButton.vue', import.meta.url)),
  'utf8'
)
const CODE = stripComments(SRC)

/** The `:class` array literal on the <button> — the shared half. */
const BASE = CODE.slice(CODE.indexOf(':class="['), CODE.indexOf('VARIANT_CLASSES[variant]'))
/** The VARIANT_CLASSES object literal — the per-variant half.
 *  Bounded at the literal's own closing brace: slicing to end-of-file swept in
 *  `defineProps`, so the variant roster below read as ten entries and the
 *  fail-closed check fired on `loadingLabel`. */
const VARIANTS = (() => {
  const open = CODE.indexOf('const VARIANT_CLASSES')
  const close = CODE.indexOf('\n}', open)
  expect(open, 'VARIANT_CLASSES is gone').toBeGreaterThan(-1)
  expect(close, 'VARIANT_CLASSES is not brace-terminated').toBeGreaterThan(open)
  return CODE.slice(open, close)
})()

const variantArm = (name) => {
  const start = VARIANTS.indexOf(`  ${name}:`)
  expect(start, `variant \`${name}\` is missing from VARIANT_CLASSES`).toBeGreaterThan(-1)
  const rest = VARIANTS.slice(start + 1)
  const next = rest.search(/\n {2}\w+:/)
  return next === -1 ? rest : rest.slice(0, next)
}

describe('BaseButton resting border', () => {
  it('strips the prose it claims to strip', () => {
    // Without this the whole file is theatre: the component's block comment
    // explains the bug using the literal strings every assertion below matches
    // on, so a stripper that returned its input unchanged would pass all of it.
    const proseOnly = 'silently beat the variant'
    expect(SRC).toContain(proseOnly)
    expect(CODE).not.toContain(proseOnly)
    // ...while the code itself survives intact.
    expect(CODE).toContain('const VARIANT_CLASSES')
    expect(CODE).toContain('SIZE_CLASSES[size]')
  })

  it('reserves the 1px in the base string without naming a colour', () => {
    // The width must stay shared — it is what keeps the focus ring from costing
    // a layout shift, the same reason FIELD_CLASS carries a bare `border`.
    expect(BASE).toMatch(/\brounded-md border\b/)
    // ...and the colour must NOT be here. This is the regression, in one line.
    expect(BASE).not.toContain('border-transparent')
    expect(BASE).not.toMatch(/\bborder-(gray|action|status)-\d/)
  })

  it('gives every variant its own resting border colour', () => {
    // Fail-closed against a fifth variant a future PR invents: a variant that
    // names no colour inherits `border` with no keyword, which computes to
    // currentColor — a button outlined in its own text colour. Whoever adds one
    // is told here rather than in a screenshot three releases later.
    const declared = [...VARIANTS.matchAll(/\n {2}(\w+):/g)].map((m) => m[1])
    expect(declared).toEqual(['primary', 'secondary', 'danger', 'ghost'])
    for (const name of declared) {
      expect(variantArm(name), `variant \`${name}\` names no border colour`)
        .toMatch(/border-(transparent|gray-\d+|action-[\w-]+|status-[\w-]+)/)
    }
  })

  it('keeps secondary outlined in BOTH themes', () => {
    // The approved spec is explicit that this variant is drawn by its border:
    // design-system-reference.html → `.btn-secondary { … border-color:
    // var(--border-strong) }`. Both arms are asserted because the bug was
    // theme-asymmetric — pinning only the light arm would have let the next
    // edit break dark while this test stayed green.
    const secondary = variantArm('secondary')
    expect(secondary).toContain('border-gray-300')
    expect(secondary).toContain('dark:border-gray-700')
    expect(secondary).not.toContain('border-transparent')
  })

  it('keeps the filled variants and ghost borderless', () => {
    // These three are drawn by their fill (or by nothing), so their reserved
    // 1px must stay invisible. They regress in the opposite direction from
    // secondary — a stray colour here draws an outline nobody asked for.
    for (const name of ['primary', 'danger', 'ghost']) {
      expect(variantArm(name), `variant \`${name}\` should rest on a transparent border`)
        .toContain('border-transparent')
    }
  })
})
