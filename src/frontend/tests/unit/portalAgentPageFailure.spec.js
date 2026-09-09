/**
 * #2597 — a failed `/agents/{name}/page` fetch must not render as an empty agent.
 *
 * `usePortalAgentPage` has always returned `error` and `loaded`. Both consumers
 * ignored one half of them, in opposite directions:
 *
 *   * `PortalAgentDetails` destructured `{ header, capabilities }` only, so a
 *     failed fetch fell through to the computed fallbacks — identity became the
 *     slug, the description vanished, and "This agent hasn't published anything
 *     it can do yet" appeared. That last one is a POSITIVE claim about the
 *     agent, made on no evidence. "Your chats" kept working (it is a slice of
 *     the shell's thread list, a different fetch), which made the rest read as
 *     loaded rather than broken.
 *   * `PortalAgentBand` reads `error` but gates its banner on `loaded`, so it
 *     covers a failed REFRESH and leaves a failed FIRST load as a skeleton that
 *     never resolves — the same rule's other half, one component over.
 *
 * ent#547 widened the first one: the body is now mounted per participant in a
 * room, so N independent chances to fail silently, including for a participant
 * off the viewer's roster where `_require_roster` answers a uniform 404.
 *
 * There is no component-mount harness in this project (no `@vue/test-utils`),
 * so these are source-structure guards in the `workspaceRoomsGate.spec.js`
 * shape, comments stripped first so prose about the rule is not scanned as
 * code.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { stripComments } from './helpers/stripComments'

const read = (rel) => stripComments(readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8'))

const details = read('../../src/components/portal/PortalAgentDetails.vue')
const band = read('../../src/components/portal/PortalAgentBand.vue')
const composable = read('../../src/composables/usePortalAgentPage.js')

describe('#2597 — the composable already answered; the consumers had to ask', () => {
  it('still exposes the verdict, the error and the retry', () => {
    // The fix is entirely on the consumer side. If this ever stops being true
    // the guards below are testing something that cannot work.
    for (const name of ['loaded', 'error']) {
      expect(composable, name).toContain(name)
    }
    // `reload` by NAME, not by the shape it happened to have: it was the alias
    // `reload: load` and #2604 made it `const reload = () => load({ force: true })`.
    // What the consumers need is that the composable RETURNS something called
    // `reload` — pinning the spelling of its definition made this guard go red
    // on a refactor that changed nothing it cares about.
    expect(composable).toMatch(/\breload\b/)
    expect(composable).toMatch(/return\s*\{[^}]*\breload\b[^}]*\}/s)
  })
})

describe('#2597 — the Info body tells the truth about its own fetch', () => {
  it('reads the verdict and the error, not just what it renders', () => {
    expect(details).toMatch(
      /const \{[^}]*\berror\b[^}]*\}\s*=\s*usePortalAgentPage\(/
    )
    expect(details).toMatch(
      /const \{[^}]*\bloaded\b[^}]*\}\s*=\s*usePortalAgentPage\(/
    )
    expect(details).toMatch(
      /const \{[^}]*\breload\b[^}]*\}\s*=\s*usePortalAgentPage\(/
    )
  })

  it('names the first-load failure as a failure, with a retry', () => {
    // LoadFailed is the "failed" member of the loading/empty/failed triad
    // (#1926) — it must not borrow the empty state's copy, which is exactly
    // what the bug did.
    expect(details).toMatch(/<LoadFailed[\s\S]{0,240}@retry="reload"/)
  })

  it('never makes the "nothing published" claim on a failed first load', () => {
    // The defect in one line: the empty-state copy is a claim the client has
    // no evidence for when the fetch that would have populated it failed.
    // Whatever guards it must reference the failure verdict.
    const idx = details.indexOf("hasn't published anything it can do yet")
    expect(idx).toBeGreaterThan(-1)
    const guard = details.slice(Math.max(0, idx - 400), idx)
    expect(guard).toMatch(/pageFailed|!pageFailed|v-else/)
  })

  it('keeps a failed REFRESH beside the data instead of replacing it (ent#253)', () => {
    expect(details).toMatch(/<InlineError[\s\S]{0,200}@retry="reload"/)
  })

  it('gates on the verdict, never on a bare loading flag (#2540/#1927)', () => {
    // The loadingGateRatchet counts `v-if="<loading flag>"`; this states the
    // positive rule for the block being added.
    expect(details).not.toMatch(/v-if="loading"/)
    expect(details).not.toMatch(/v-if="pageLoading"/)
  })

  it('leaves "Your chats" out of the page-fetch failure — it is a different fetch', () => {
    // Stated as a rule because the tempting fix is one wrapper around the whole
    // body, which would hide a list that is still perfectly good: `chats` is a
    // slice of the shell's thread list, not of `/page`.
    const chatsIdx = details.indexOf('Your chats')
    const emptyIdx = details.indexOf('No conversations yet.')
    expect(chatsIdx).toBeGreaterThan(-1)
    expect(emptyIdx).toBeGreaterThan(chatsIdx)
    expect(details.slice(chatsIdx, emptyIdx)).not.toMatch(/pageFailed/)
  })
})

describe('#2597 — the band fails the other way, and is fixed with it', () => {
  it('gives a failed first load its own arm, with a retry', () => {
    // `InlineError v-if="error && loaded"` covers a refresh only. Without a
    // first-load arm the band shows its three pulsing placeholders forever,
    // with no error and no retry.
    //
    // Pinned as the PROPERTY — an arm gated on `error && !loaded` that offers
    // a retry — rather than as a named component. The first cut of this fix
    // used `LoadFailed` and a guard naming it would have been re-pointed at the
    // primitive rather than at the rule when that changed.
    expect(band).toMatch(/v-if="error && !loaded"[\s\S]{0,200}@retry="reload"/)
  })

  it('still keeps a failed refresh beside the numbers', () => {
    expect(band).toMatch(/<InlineError v-if="error && loaded"/)
  })

  it('never renders the stat numbers on a failed first load', () => {
    // The stats branch reads `stats`, which falls back to
    // `{ total_executions: 0 }` — zeros are a claim about performance where
    // "no data" is a claim about nothing, so this is the same "empty agent"
    // defect the details panel had. The failure must therefore be an ARM of
    // the same v-if chain, never a banner rendered beside a live row.
    const failIdx = band.indexOf('v-if="error && !loaded"')
    const skeletonIdx = band.indexOf('v-else-if="!loaded"')
    const statsIdx = band.indexOf('<template v-else>')
    expect(failIdx).toBeGreaterThan(-1)
    expect(skeletonIdx).toBeGreaterThan(failIdx)
    expect(statsIdx).toBeGreaterThan(skeletonIdx)
  })

  it('puts the failure INSIDE the row rather than in place of it', () => {
    // Caught re-reviewing my own first fix: replacing the WHOLE row with the
    // failure took the 7d/14d/30d `<select>` with it, and changing the window
    // was a second way out of a failure (`watch(timeWindow)` re-fetches)
    // alongside the retry button.
    //
    // ent#547 has since DELETED that selector — the window is fixed at 7 days,
    // because two controls for one fact is how the band and the panel came to
    // disagree — so the specific assertion this test used to make (the
    // selector still follows the failure arm) now asserts a control that does
    // not exist. What survives it is the structural half: the failure is an
    // ARM INSIDE the row, so anything else the row ever holds is a sibling of
    // the chain and cannot be swallowed by it. Kept rather than deleted,
    // because the row is where a future control would land.
    const rowIdx = band.indexOf('class="flex flex-wrap items-center')
    const failIdx = band.indexOf('v-if="error && !loaded"')
    expect(rowIdx).toBeGreaterThan(-1)
    expect(failIdx).toBeGreaterThan(rowIdx)
    expect(band).not.toContain('<select')   // ent#547 removed it; see above
  })

  it('uses one failure language, not two', () => {
    // Both faces of the failure are `InlineError`. A centred `LoadFailed`
    // block inside a horizontal band would also roughly triple its height on
    // failure and shift the conversation column (contract principle 4).
    expect(band).not.toContain('LoadFailed')
  })
})
