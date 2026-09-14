/**
 * #2449 — the agent page renders asks from ONE source.
 *
 * It used to render them twice: `<PortalAsks>` off the `/asks` store, and a
 * second "Waiting on you" section off `page.asks`, an older projection of the
 * same `operator_queue` rows. Both guards were true at the same time, so a
 * client saw every ask twice — once with answer controls, once with a
 * "Reply in chat →" link.
 *
 * The two halves also disagreed, which is why deleting one was the fix rather
 * than styling around it:
 *   - `page.asks` capped at 20, `/asks` fetches 200;
 *   - `page.asks` carried NO `status` and NO `expires_at`, so that section
 *     could not show an expired ask as expired (ent#429's AC) and counted one
 *     in the Overview badge where the sidebar's `askCount` — pending-only by
 *     design — did not.
 *
 * These are source assertions because this project has no component-mount
 * harness (the `portalAgentPageUx.spec.js` precedent). They guard the
 * MECHANISM: one ask source in the component, and no reader of the removed
 * payload field anywhere.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { stripComments } from './helpers/stripComments'

// Comments are stripped before every scan, using the SHARED helper (#2161).
// A source-substring guard cannot tell a check from a paragraph about the
// check: the first draft of this file failed against the very fix it guards,
// because the comment naming `page.asks` matched the assertion hunting for a
// reader of `page.asks`. The same lesson landed this morning on the ent#430 CAS
// guard, which had to move to `ast.unparse` for the identical reason. The
// shared helper also handles unterminated and nested HTML comments, which a
// hand-rolled non-greedy regex does not.
const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src')
// ent#523: the agent page was dismantled. It mounted `PortalAsks` as ONE of
// two copies on screen — the conversation mounts the other, and that is the
// one that survived (a pending ask belongs above the composer you would
// answer it in). #2449's rule is unchanged and now guards that surface.
const PAGE = join(SRC, 'components', 'portal', 'PortalConversation.vue')
const page = stripComments(readFileSync(PAGE, 'utf8'))
const SIDEBAR = stripComments(
  readFileSync(join(SRC, 'components', 'portal', 'PortalSidebar.vue'), 'utf8'),
)

function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(join(dir, e.name)) : [join(dir, e.name)],
  )
}

describe('#2449 — one ask entity, one rendering per surface', () => {
  it('mounts PortalAsks, the shared component, exactly once', () => {
    expect(page.match(/<PortalAsks\b/g) || []).toHaveLength(1)
  })

  it('has no second ask section of its own', () => {
    // The removed block's heading. A future re-introduction under a different
    // heading is caught by the payload-reader test below, which is the one
    // that cannot be worked around by renaming.
    expect(page).not.toContain('Waiting on you')
  })

  it('never reads the removed page.asks payload field', () => {
    // The load-bearing assertion: the divergent projection is unreachable, so
    // it cannot be re-wired to a surface and silently lose status/expiry again.
    expect(page).not.toMatch(/page\.value\?\.asks|page\.asks\b/)
  })

  it('renders the section from the store list, not a page payload', () => {
    expect(page).toContain('store.asksForAgent(props.agent.name)')
  })

  it('counts the surviving badge from the SAME store, pending-only', () => {
    // ent#523: the agent page's own count went with the page; the sidebar row
    // badge is the one that remains, and it reads the store's `openAsks` —
    // pending-only by that getter's own rationale, since an expired ask is not
    // something a badge should nag about.
    expect(SIDEBAR).toContain('asksByAgent(asksStore.openAsks)')
  })

  it('no frontend file reads page.asks any more', () => {
    const offenders = walk(SRC)
      .filter((f) => /\.(vue|js)$/.test(f))
      .filter((f) => /page\.value\?\.asks|\bpage\.asks\b/.test(stripComments(readFileSync(f, 'utf8'))))
    expect(offenders).toEqual([])
  })

  it('keeps the cap constants from outliving the list they capped', () => {
    // ASKS_CAP mirrored the backend MAX_ASKS=20. The surviving list is fetched
    // at 200, so a stale "20+" badge would have been a wrong number, not a
    // rounded one.
    expect(page).not.toContain('ASKS_CAP')
    expect(page).not.toContain('ASKS_PREVIEW')
  })
})
