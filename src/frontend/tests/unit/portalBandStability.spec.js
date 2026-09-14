/**
 * #2580 defect 1 — the agent band must not re-render or refetch when you switch
 * between two chats with the SAME agent.
 *
 * WHAT THIS FILE CAN AND CANNOT PROVE, stated up front because the gap is the
 * interesting part. `vitest.config.js` pins `environment: 'node'` and this
 * project has no component-mount harness, so "the component did not remount"
 * is not observable here — there is nothing to mount and no layout engine. The
 * behaviour was verified in Chromium against a live stack (6 chat-tab switches
 * on one agent → 0 `GET /agents/{name}/page` requests and 0 skeleton/scanline
 * frames, with a control proving the instrumentation could see a request at
 * all: switching AGENT produced exactly 1). `e2e/workspace-compact-header.spec.js`
 * holds that in CI.
 *
 * What a node test CAN hold is the two decisions that make the remount free,
 * and it holds them as executable rules rather than as source greps:
 *
 *   1. the freshness rule — when `load` is allowed to go to the network;
 *   2. the seeding ORDER — the cache is read during setup, not in `onMounted`.
 *
 * (2) is a source assertion because it is about where a call sits, which is the
 * honest limit of a node env. It is pinned term by term rather than as one
 * literal line (the 2026-08-24 ledger entry: a guard that pins a rule as one
 * literal string fails the next legitimate edit and tempts you to delete it).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { shouldRefetchPage, PAGE_FRESH_MS } from '@/composables/usePortalAgentPage'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const COMPOSABLE = read('../../src/composables/usePortalAgentPage.js')
const PORTAL = read('../../src/views/Portal.vue')
const CONV = read('../../src/components/portal/PortalConversation.vue')

const NOW = 1_800_000_000_000

describe('#2580 — when the band is allowed to refetch', () => {
  it('serves a warm cache without a request, which is the whole defect', () => {
    // The band lives inside a thread-keyed component, so it is rebuilt on every
    // chat-tab switch. Before this rule each rebuild issued a fresh page request
    // for numbers that had not moved.
    expect(shouldRefetchPage({ cached: { fetchedAt: NOW }, now: NOW })).toBe(false)
    expect(shouldRefetchPage({ cached: { fetchedAt: NOW - 1 }, now: NOW })).toBe(false)
    expect(shouldRefetchPage({ cached: { fetchedAt: NOW - (PAGE_FRESH_MS - 1) }, now: NOW })).toBe(false)
  })

  it('refetches once the window has passed — the cache is not a freeze', () => {
    // The staleness ceiling stays bounded. A "never refetch while mounted" cache
    // would make the band's numbers whatever they were when the tab was opened.
    expect(shouldRefetchPage({ cached: { fetchedAt: NOW - PAGE_FRESH_MS }, now: NOW })).toBe(true)
    expect(shouldRefetchPage({ cached: { fetchedAt: NOW - 10 * PAGE_FRESH_MS }, now: NOW })).toBe(true)
  })

  it('always refetches on a miss and on an explicit reload', () => {
    expect(shouldRefetchPage({ cached: null, now: NOW })).toBe(true)
    expect(shouldRefetchPage({ now: NOW })).toBe(true)
    // `reload` is the ent#253 retry beside a stale-data banner. A retry that
    // answered from cache would report success without re-asking anything.
    expect(shouldRefetchPage({ cached: { fetchedAt: NOW }, now: NOW, force: true })).toBe(true)
  })

  it('treats an unusable timestamp as STALE, never as fresh', () => {
    // Fail in the direction of one wasted request rather than a band that never
    // updates again. Covers a legacy bare-payload entry, a corrupted stamp and a
    // backwards clock jump.
    expect(shouldRefetchPage({ cached: {}, now: NOW })).toBe(true)
    expect(shouldRefetchPage({ cached: { fetchedAt: undefined }, now: NOW })).toBe(true)
    expect(shouldRefetchPage({ cached: { fetchedAt: '1800000000000' }, now: NOW })).toBe(true)
    expect(shouldRefetchPage({ cached: { fetchedAt: NaN }, now: NOW })).toBe(true)
    expect(shouldRefetchPage({ cached: { fetchedAt: Infinity }, now: NOW })).toBe(true)
    expect(shouldRefetchPage({ cached: { fetchedAt: NOW + 60_000 }, now: NOW })).toBe(true)
  })

  it('is called with the force flag rather than re-deciding inline', () => {
    // One decision, one home. An inline `Date.now() - cached.fetchedAt < X`
    // beside the call would be a second copy of this rule that no test reaches.
    expect(COMPOSABLE).toContain('shouldRefetchPage({ cached, now: Date.now(), force })')
    expect(COMPOSABLE).toContain('const reload = () => load({ force: true })')
  })
})

describe('#2580 — the cache is read before the first paint, not after it', () => {
  it('seeds during setup, so a rebuilt band never paints a placeholder', () => {
    // `onMounted` runs AFTER the first render. Reading the cache only there is
    // exactly why a remounted band flashed its skeleton and the chart flashed
    // its scanline while the data sat in memory: the verdict was computed one
    // frame too late. This is the #2540/#1927 rule one layer up — a placeholder
    // means "no data yet", and a warm cache is data.
    const setupSeed = COMPOSABLE.indexOf('const seed = ')
    // The REGISTRATION, not the `import { onMounted }` at the top of the file —
    // matching the import made this assertion compare the seed against line 22
    // and fail on correct code.
    const mounted = COMPOSABLE.indexOf('onMounted(() => load())')
    expect(setupSeed, 'the setup-time seed must exist').toBeGreaterThan(-1)
    expect(mounted, 'load must still run on mount for a cold cache').toBeGreaterThan(-1)
    expect(setupSeed, 'the seed must be read before onMounted is registered').toBeLessThan(mounted)
    // And it must actually set the verdict, or the seed changes nothing.
    const seedBlock = COMPOSABLE.slice(setupSeed, mounted)
    expect(seedBlock).toContain('loaded.value = true')
    expect(seedBlock).toContain('page.value = seed.payload')
  })

  it('stores a timestamp beside the payload, or freshness is undecidable', () => {
    expect(COMPOSABLE).toContain('pageCache.set(key, { payload: fresh, fetchedAt: Date.now() })')
    expect(COMPOSABLE).toContain('page.value = cached.payload')
  })
})

describe('#2580 — the band keys on the agent', () => {
  it('is keyed by agent name where the shell mounts it', () => {
    // The issue's own words for the rule. The key is what makes an AGENT change
    // rebuild the band; a thread change must not touch it.
    expect(PORTAL).toMatch(/<PortalAgentBand\s+:key="activeAgent\.name"\s+:agent-name="activeAgent\.name"/)
    // The key must not carry the thread. `convKey` is the conversation's, and
    // borrowing it here would reinstate the defect through the front door.
    const bandAt = PORTAL.indexOf('<PortalAgentBand')
    expect(PORTAL.slice(bandAt, bandAt + 200)).not.toContain('convKey')
  })

  it('keeps the band UNDER the header, which is why the remount is fixed at its source', () => {
    // The operator ruled a band "under the header" (2026-09-06), and the header
    // lives inside PortalConversation — so the band stays in its slot and the
    // remount is made free rather than removed. Hoisting it to a sibling would
    // render an agent's numbers above its name.
    expect(CONV).toContain('<slot name="band" />')
    const headerEnd = CONV.indexOf('</header>')
    const slotAt = CONV.indexOf('<slot name="band" />')
    expect(headerEnd).toBeGreaterThan(-1)
    expect(slotAt, 'the band renders below the header').toBeGreaterThan(headerEnd)
  })

  it('keeps the conversation root sized for being the only child', () => {
    // Paired with the slot above: `h-full` is correct ONLY while the band is
    // inside this column. If a later change lifts the band out to a sibling,
    // this must become `flex-1 min-h-0` in the same commit or the composer is
    // pushed out of an `overflow-hidden` shell with no scrollbar to recover it.
    const root = CONV.slice(CONV.indexOf('<div\n    class="relative flex flex-col'), CONV.indexOf('@dragenter'))
    expect(root).toContain('h-full')
  })
})
