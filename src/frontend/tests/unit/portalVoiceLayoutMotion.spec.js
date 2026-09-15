/**
 * #2640 — starting a call is one motion, and the orb stays round.
 *
 * Two defects, one report:
 *
 * 1. **The layout jumped.** `Portal.vue` toggled the stage between `flex-1` and
 *    `sm:flex-[2_1_0%]` on `voiceCall.active` and swapped `PortalRail` for
 *    `PortalVoiceCanvas` in the same frame. Nothing transitioned, so both
 *    columns landed at their new shares in one paint, and End call jumped back.
 *
 * 2. **The orb rendered squashed.** `VoiceOverlay.vue::resizeCanvas` sized the
 *    canvas bitmap ONCE, from the `watch(canvasEl)` that fires on mount. There
 *    was no `ResizeObserver`, no window listener and no per-frame check, while
 *    the canvas is `absolute inset-0 w-full h-full` — so every later change to
 *    the column's width left CSS stretching a stale bitmap into an ellipse.
 *    The overlay also mounts in the same tick the call re-lays out the columns,
 *    so the single measurement could capture the PRE-call width on its own.
 *
 * The resize path is exercised for real: `resizeCanvas` is DOM code, so the
 * test drives it against a stub canvas whose box changes, which is what the AC
 * asks for ("bitmap size follows a changed box size"). The layout half is
 * source-asserted — this project has no component-mount harness (`package.json`
 * carries no @vue/test-utils, jsdom or happy-dom; vitest runs
 * `environment: 'node'`), and a transition is a class contract, not a value.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

/**
 * Every transitioning element's class string, read out of CLASS ATTRIBUTES.
 *
 * Deliberately not a file-wide match for `transition-[...]`: this shell's
 * comments discuss the transitions they configure, and a raw scan reports the
 * PROSE as a transitioning element with no `motion-reduce`. Matching
 * `class="..."` and `*-class="..."` keeps the guard on the code.
 */
function transitionClasses(src) {
  const out = []
  const attr = /(?:^|\s)(?:[a-z-]*-)?class="([^"]*)"/g
  let m
  while ((m = attr.exec(src)) !== null) {
    if (m[1].includes('transition-[')) out.push(m[1])
  }
  return out
}
const SHELL = read('../../src/views/Portal.vue')
const OVERLAY = read('../../src/components/chat/VoiceOverlay.vue')

describe('#2640 — the column swap is animated, not discrete', () => {
  it('the conversation column transitions its flex-grow', () => {
    // `flex-grow` is a <number> and therefore animatable, which is what lets
    // the share move continuously between 1 (no call) and 2 (call).
    expect(SHELL).toMatch(
      /<main[\s\S]{0,400}transition-\[flex-grow\] duration-300 ease-out motion-reduce:transition-none motion-reduce:duration-0/
    )
  })

  it('keeps the split as flex SHARES — #2581 is not reverted', () => {
    // The percentages summed to 100% + an 18rem sidebar and the shell clipped
    // the canvas column off the right edge. Animating is not a licence to go
    // back to them.
    expect(SHELL).toMatch(/voiceCall\.active \? 'flex-1 sm:flex-\[2_1_0%\]' : 'flex-1'/)
    expect(SHELL).toMatch(/sm:flex sm:flex-\[3_1_0%\]/)
    expect(SHELL).not.toMatch(/sm:w-\[60%\]|sm:w-\[40%\]/)
  })

  it('the canvas column ramps its own share in and out', () => {
    // A newly inserted element has no value to transition FROM, so the ramp is
    // expressed as Vue enter/leave classes rather than a class toggle.
    expect(SHELL).toMatch(/enter-from-class="!grow-0 opacity-0"/)
    expect(SHELL).toMatch(/leave-to-class="!grow-0 opacity-0"/)
    expect(SHELL).toMatch(
      /enter-active-class="transition-\[flex-grow,opacity\] duration-300 ease-out motion-reduce:transition-none motion-reduce:duration-0"/
    )
    expect(SHELL).toMatch(
      /leave-active-class="transition-\[flex-grow,opacity\] duration-300 ease-out motion-reduce:transition-none motion-reduce:duration-0"/
    )
  })

  it('the grow-0 end state is `!`-marked so class order cannot decide it', () => {
    // `grow-0` and `sm:flex-[3_1_0%]` are both single-class selectors setting
    // flex-grow. Without `!` the winner would be whichever Tailwind emitted
    // last, which is not a contract.
    expect(SHELL).toMatch(/!grow-0/)
  })

  it('honours prefers-reduced-motion on every transitioning element', () => {
    // AC 1: instant, no animation. Counted rather than merely present, so a
    // future third transitioning element cannot be added without one.
    //
    // BOTH classes, and the second one is the load-bearing half. Tailwind's
    // `transition-none` emits only `transition-property: none` — the
    // `duration-300` beside it still applies, so `transitionDuration` stays
    // `.3s`. Vue's `getTransitionInfo` reads exactly that property to size the
    // fallback timer it resolves `@after-leave` on, so with `transition-none`
    // alone nothing animates and every leave is still gated for 300ms: the
    // canvas vanishes, the column sits empty, the rail pops in. Asserting only
    // the class string is why this shipped green (#2640 review).
    // Scanned out of CLASS ATTRIBUTES, not out of the file's prose: #2676's
    // comments name `transition-[width]` while explaining it, and a raw
    // file-wide match would have failed on the explanation rather than on the
    // code. `transitionClasses` is shared with the test below so the two
    // cannot disagree about what counts as a transitioning element.
    const transitions = transitionClasses(SHELL)
    expect(transitions.length).toBeGreaterThan(0)
    for (const t of transitions) {
      expect(t, `missing motion-reduce on: ${t}`).toContain('motion-reduce:transition-none')
      expect(t, `transition-none without duration-0 still runs a 300ms timer: ${t}`)
        .toContain('motion-reduce:duration-0')
    }
  })

  it('the zero duration is what Vue reads, not the property reset', () => {
    // The relation the class pair encodes, stated once so a future edit that
    // drops `duration-0` as "redundant" has something to fail against: every
    // transitioning element pairs a non-zero duration with a reduced-motion
    // zero, and the zero is declared AFTER it so the cascade lands the right
    // way round (Tailwind emits variants after base utilities).
    const transitions = transitionClasses(SHELL)
    for (const t of transitions) {
      expect(t).toMatch(/duration-\d+/)
      expect(t.indexOf('motion-reduce:duration-0'))
        .toBeGreaterThan(t.search(/\bduration-\d+/))
    }
  })

  it('the rail column is a WIDTH, so it moves with the canvas (#2676)', () => {
    // The #2640 review finding, and what this replaces: the rail was a bare
    // `v-if` on a `shrink-0` flex sibling carrying no width transition, so on
    // call end it appeared at full size in one frame — up to the dragged
    // `--ws-rail`, which on a wide rail is a bigger step than the 211px snap
    // #2640 removed.
    expect(SHELL).toMatch(/enter-from-class="!w-0"/)
    expect(SHELL).toMatch(/leave-to-class="!w-0"/)
    expect(SHELL).toMatch(/transition-\[width\] duration-300 ease-out overflow-hidden/)
    // The width is the RENDERED one, so a collapsed rail animates its 48px and
    // an open one animates whatever it was dragged to — one binding, both
    // states, and it agrees with the inner aside at rest.
    expect(SHELL).toMatch(/data-testid="ws-rail-column"[\s\S]{0,80}|w-\[var\(--ws-rail,24rem\)\]/)
  })

  it('does not transition the width outside the enter/leave window', () => {
    // The reason this is Vue enter/leave classes and not a permanent
    // `transition-[width]`: `--ws-rail` is rewritten on every `pointermove` of
    // a rail drag, and an always-on transition would make dragging rubber-band
    // by 300ms. Vue adds the active class for the window and removes it after.
    const column = SHELL.slice(
      SHELL.indexOf('data-testid="ws-rail-column"') - 400,
      SHELL.indexOf('data-testid="ws-rail-column"') + 200,
    )
    const staticClass = column.match(/class="([^"]*w-\[var\(--ws-rail[^"]*)"/)
    expect(staticClass, 'the rail column wrapper was not found').toBeTruthy()
    expect(staticClass[1]).not.toMatch(/transition-/)
  })

  it('retires voiceCanvasLeaving — the hazard is gone by construction', () => {
    // That flag held the rail out of the row for the canvas's whole leave,
    // because a rail mounting at FULL width beside a shrinking canvas put three
    // columns in a row sized for two. A rail entering from zero width is
    // complementary to a canvas leaving towards zero grow, so the row's total
    // is conserved at every frame and the two motions overlap instead of
    // running back to back.
    expect(SHELL).not.toMatch(/@before-leave="voiceCanvasLeaving/)
    expect(SHELL).not.toMatch(/@after-leave="voiceCanvasLeaving/)
    expect(SHELL).not.toMatch(/const voiceCanvasLeaving = ref\(/)
    expect(SHELL).not.toMatch(/!voiceCanvasLeaving/)
  })

  it('a tabless rail leaves no empty column behind', () => {
    // `PortalRail`'s own root carries `v-if="tabs.length"`. With the width now
    // on a wrapper this view owns, that gate has to be read out here too — or a
    // rail with no visible tabs renders a full-width empty column.
    expect(SHELL).toMatch(/railTabs\.value\.length/)
  })

  it('both columns read ONE condition, so they cannot both claim the slot', () => {
    // The `v-else-if` chain guaranteed that by construction; the <Transition>
    // wrapper broke the adjacency it needs, so it is written down instead.
    expect(SHELL).toMatch(/const voiceCanvasHasColumn = computed\(/)
    expect(SHELL).toMatch(/v-if="voiceCanvasHasColumn"/)
    expect(SHELL).not.toMatch(/<PortalRail[\s\S]{0,120}v-else-if=/)
  })
})

// ── The orb's bitmap follows its box ─────────────────────────────────────────

/**
 * A stub canvas: a settable box, a bitmap, and a 2d context that records the
 * transform. Enough to drive the real `resizeCanvas` contract without a DOM.
 */
function stubCanvas(width, height) {
  return {
    width: 0,
    height: 0,
    box: { width, height },
    getBoundingClientRect() { return { width: this.box.width, height: this.box.height } },
  }
}

/**
 * `resizeCanvas` lives inside a `<script setup>` block, so it cannot be
 * imported. This is the same computation, extracted from the component source
 * by executing its body — a copy would prove nothing about the shipped code.
 */
function resizeCanvasFromSource() {
  const body = OVERLAY.slice(
    OVERLAY.indexOf('function resizeCanvas()'),
    OVERLAY.indexOf('// Both, deliberately.'),
  )
  expect(body, 'resizeCanvas() not found in VoiceOverlay.vue').toContain('getBoundingClientRect')
  const state = { cssWidth: 0, cssHeight: 0, pixelRatio: 1, canvasEl: { value: null } }
  const factory = new Function(
    'state', 'DPR_CAP', 'window',
    `let { cssWidth, cssHeight, pixelRatio } = state; const canvasEl = state.canvasEl;
     ${body}
     return () => { resizeCanvas(); state.cssWidth = cssWidth; state.cssHeight = cssHeight; state.pixelRatio = pixelRatio }`,
  )
  return { state, run: (dpr) => factory(state, 2, { devicePixelRatio: dpr })() }
}

describe('#2640 — the orb bitmap tracks its box', () => {
  it('sizes the bitmap from the box, with device-pixel-ratio scaling', () => {
    const { state, run } = resizeCanvasFromSource()
    state.canvasEl.value = stubCanvas(400, 300)
    run(2)
    expect(state.canvasEl.value.width).toBe(800)
    expect(state.canvasEl.value.height).toBe(600)
    // The render loop works in CSS pixels and lets the context scale, so the
    // 45px core and the particles' fixed radii keep meaning what they meant.
    expect(state.cssWidth).toBe(400)
    expect(state.cssHeight).toBe(300)
    expect(state.pixelRatio).toBe(2)
  })

  it('FOLLOWS a changed box — the reported bug', () => {
    // The call's column swap, a rail drag, a window resize, the sm breakpoint.
    const { state, run } = resizeCanvasFromSource()
    const canvas = stubCanvas(400, 300)
    state.canvasEl.value = canvas
    run(1)
    expect([canvas.width, canvas.height]).toEqual([400, 300])
    canvas.box = { width: 900, height: 300 }
    run(1)
    expect([canvas.width, canvas.height]).toEqual([900, 300])
    // The aspect ratio the bitmap is drawn at now matches the box's. Before the
    // fix the bitmap stayed 400x300 and CSS stretched it to 900x300 — the
    // ellipse in the report.
    expect(canvas.width / canvas.height).toBeCloseTo(900 / 300)
  })

  it('caps the pixel ratio', () => {
    // A 3x display would otherwise quadruple the fill cost of a full-column
    // particle field for detail nobody can see at this blur radius.
    const { state, run } = resizeCanvasFromSource()
    state.canvasEl.value = stubCanvas(400, 300)
    run(3)
    expect(state.pixelRatio).toBe(2)
    expect(state.canvasEl.value.width).toBe(800)
  })

  it('treats a missing devicePixelRatio as 1 rather than 0', () => {
    const { state, run } = resizeCanvasFromSource()
    state.canvasEl.value = stubCanvas(400, 300)
    run(undefined)
    expect(state.pixelRatio).toBe(1)
    expect(state.canvasEl.value.width).toBe(400)
  })

  it('ignores a zero-sized box instead of throwing the last good size away', () => {
    // A hidden or not-yet-laid-out canvas measures 0. Sizing a bitmap to 0
    // draws nothing; the observer fires again once it has a box.
    const { state, run } = resizeCanvasFromSource()
    const canvas = stubCanvas(400, 300)
    state.canvasEl.value = canvas
    run(1)
    canvas.box = { width: 0, height: 0 }
    run(1)
    expect([canvas.width, canvas.height]).toEqual([400, 300])
  })

  it('does not reassign the bitmap when the size is unchanged', () => {
    // Assigning to canvas.width CLEARS the canvas and resets its context, so an
    // observer firing on a sub-pixel reflow would otherwise blank the orb
    // continuously. Proven by making the setter observable.
    const { state, run } = resizeCanvasFromSource()
    let writes = 0
    let w = 0
    const canvas = stubCanvas(400, 300)
    Object.defineProperty(canvas, 'width', {
      get: () => w,
      set: (v) => { writes++; w = v },
    })
    state.canvasEl.value = canvas
    run(1)
    expect(writes).toBe(1)
    run(1)
    expect(writes, 'a same-size resize must not touch the bitmap').toBe(1)
  })
})

describe('#2640 — the overlay observes, and stops observing', () => {
  it('uses a ResizeObserver AND a window listener, for different events', () => {
    // The observer catches the box moving under a stable window (the column
    // swap, a rail drag); the window listener catches a devicePixelRatio change
    // — dragging to a different-density monitor resizes no box at all.
    expect(OVERLAY).toMatch(/new ResizeObserver\(\(\) => resizeCanvas\(\)\)/)
    expect(OVERLAY).toMatch(/window\.addEventListener\('resize', resizeCanvas\)/)
    expect(OVERLAY).toMatch(/typeof ResizeObserver === 'function'/)
  })

  it('tears both down with the canvas and on unmount', () => {
    expect(OVERLAY).toMatch(/window\.removeEventListener\('resize', resizeCanvas\)/)
    expect(OVERLAY).toMatch(/resizeObserver\.disconnect\(\)/)
    expect(OVERLAY).toMatch(/onUnmounted\(\(\) => \{\s*unobserveCanvas\(\)/)
  })

  it('re-scales rather than re-seeding — the orb does not restart on a resize', () => {
    // The particle field lives in a fixed coordinate space around (0,0) and is
    // seeded once in startLoop. Nothing in the resize path may touch it.
    const resizePath = OVERLAY.slice(
      OVERLAY.indexOf('function resizeCanvas()'),
      OVERLAY.indexOf('function startLoop()'),
    )
    expect(resizePath).not.toContain('initParticles')
    expect(resizePath).not.toContain('buildSprites')
  })

  it('the render loop draws in CSS pixels via the context transform', () => {
    expect(OVERLAY).toMatch(/ctx\.setTransform\(pixelRatio, 0, 0, pixelRatio, 0, 0\)/)
    expect(OVERLAY).toMatch(/const W = cssWidth, H = cssHeight/)
  })
})
