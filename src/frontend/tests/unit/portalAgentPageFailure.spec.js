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
    for (const name of ['loaded', 'error', 'reload: load']) {
      expect(composable, name).toContain(name)
    }
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
  it('does not leave a failed first load as a permanent skeleton', () => {
    // `InlineError v-if="error && loaded"` covers a refresh only. Without a
    // first-load arm the band shows its three pulsing placeholders forever,
    // with no error and no retry.
    expect(band).toMatch(/<LoadFailed[\s\S]{0,240}@retry="reload"/)
  })

  it('still keeps a failed refresh beside the numbers', () => {
    expect(band).toMatch(/<InlineError v-if="error && loaded"/)
  })

  it('does not show the skeleton and the failure at once', () => {
    // The skeleton branch is `v-if="!loaded"`; a first-load failure must take
    // its own arm out of that same chain, not render alongside it.
    expect(band).toMatch(/v-if="!loaded && !error"/)
  })
})
