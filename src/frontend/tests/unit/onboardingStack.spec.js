/**
 * The Dashboard onboarding stack shows ONE card at a time (#2380).
 *
 * Four first-run surfaces landed on this view from four separate issues, none
 * aware of the others: the hardening guide (#2380), the front desk (ent#319),
 * the activation checklist (ent#238) and the sign-in email nudge (#2381). On a
 * fresh marketplace install all four predicates can be true at once, which put
 * ~520px of chrome and four dismiss buttons above the product.
 *
 * The gate is CSS rather than a lifted predicate on purpose. Every card is
 * `v-if`'d, so "the first element child" already means "the highest-priority
 * card that wants to speak" — and each card keeps its visibility where it
 * lives today (its own store, its own localStorage dismissal), which a
 * Dashboard-level chain would have had to duplicate and then keep in sync.
 * Dismissing the top card reveals the next for free.
 *
 * Asserted on source text because `vitest.config.js` runs `environment: 'node'`
 * with no component-mount harness (the ent#392 rule).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const read = (rel) =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

const DASHBOARD = read('../../src/views/Dashboard.vue')

// DOM order IS priority order: security posture first, then the sign-in
// identity prompt, then the two getting-started nudges. `FinishSetupCard`
// (ent#437) carries the #2381 sign-in-email ask that AdminEmailNudge used to.
const CARDS = [
  ['HardeningGuide', '../../src/components/onboarding/HardeningGuide.vue'],
  ['FrontDeskPanel', '../../src/components/onboarding/FrontDeskPanel.vue'],
  ['ActivationChecklist', '../../src/components/onboarding/ActivationChecklist.vue'],
  ['FinishSetupCard', '../../src/components/onboarding/FinishSetupCard.vue'],
]

describe('one onboarding card at a time', () => {
  it('hides every sibling after the first inside the stack', () => {
    // `> * ~ *` and not `:not(:first-child)`: both are correct here, but the
    // sibling form states the intent — everything AFTER the first element.
    expect(DASHBOARD).toMatch(/\.onboarding-stack > \* ~ \*\s*\{\s*display:\s*none;?\s*\}/)
  })

  it('wraps all four cards, and only them, in the stack', () => {
    const stack = DASHBOARD.slice(
      DASHBOARD.indexOf('<div class="onboarding-stack">'),
      DASHBOARD.indexOf('</div>', DASHBOARD.indexOf('<FinishSetupCard />'))
    )
    expect(stack, 'stack wrapper must exist').toContain('onboarding-stack')
    for (const [name] of CARDS) {
      expect(stack, `${name} must be inside the stack`).toContain(`<${name}`)
    }
  })

  it('keeps the cards in priority order, highest first', () => {
    const positions = CARDS.map(([name]) => DASHBOARD.indexOf(`<${name}`))
    expect(positions.every((p) => p > -1), 'every card is mounted').toBe(true)
    expect([...positions].sort((a, b) => a - b)).toEqual(positions)
  })
})

describe('the visible card is spaced on both sides', () => {
  // The stack wrapper deliberately carries no margin: with every card hidden it
  // must collapse to nothing rather than leave a phantom gap. So each card owns
  // both margins — and the bottom one is load-bearing, because the pane below
  // (timeline header, grid surface) is full-bleed with no top padding of its
  // own, leaving the card's border touching it.
  it.each(CARDS)('%s owns mt-3 and mb-3', (name, rel) => {
    const root = read(rel).split('\n').find((l) => l.includes('mx-4'))
    expect(root, `${name} root class`).toBeTruthy()
    expect(root, `${name} needs a top gap`).toMatch(/\bmt-3\b/)
    expect(root, `${name} needs a bottom gap`).toMatch(/\bmb-3\b/)
  })

  it('puts no margin on the wrapper itself', () => {
    expect(DASHBOARD).toContain('<div class="onboarding-stack">')
  })

  /*
   * The one-card rule hides with CSS, so every hidden card still MOUNTS. Any
   * "shown once" bookkeeping a card does off a Vue predicate is therefore spent
   * behind whichever card is holding the slot.
   *
   * ent#437's warm re-ask is the live case: one shot per browser, and it exists
   * precisely because a cold ask at first login converts badly. Marking it on
   * `consentVisible` burned it while the hardening guide sat on top, and the
   * operator met the weaker cold copy later — if they met one at all.
   */
  it('marks the warm ask shown only once it has really been seen', () => {
    const sfc = read('../../src/components/onboarding/FinishSetupCard.vue')

    // Real visibility, not the render predicate. A sibling being dismissed
    // changes no state this component can watch, so an observer is the only
    // instrument that sees the card arrive.
    expect(sfc).toContain('IntersectionObserver')
    expect(sfc).toMatch(/isIntersecting/)
    expect(sfc).toContain('persistWarmShown()')

    // The regression itself: persisting from inside the consentVisible watcher.
    const watcher = sfc.slice(sfc.indexOf('watch(\n  consentVisible'))
    const body = watcher.slice(0, watcher.indexOf('{ immediate: true }'))
    expect(body, 'the visibility watcher must not spend the warm ask').not.toContain(
      'persistWarmShown'
    )

    // Intersecting is necessary, not sufficient — the observer re-checks that
    // the section still wants to render and is still the warm variant.
    const marker = sfc.slice(sfc.indexOf('function markWarmAskSeen'))
    const markerBody = marker.slice(0, marker.indexOf('\n}'))
    expect(markerBody).toContain('consentVisible.value')
    expect(markerBody).toContain("variant.value !== 'warm'")
  })
})
