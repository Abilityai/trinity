/**
 * ent#492 — resizable Workspace columns.
 *
 * vitest runs `environment: 'node'` with no mount harness, so every decidable
 * rule lives in `useColumnResize` and is tested directly; the parts no unit
 * test can reach (which element carries the grid, where the handles sit, that
 * the widths are variables rather than classes) are source-structure guards
 * with comments stripped first.
 *
 * The bug this file exists to prevent is NOT in any of the pure functions. It
 * was the identity: the key was built from the portal store's `clientEmail`,
 * which is `null` until a network response lands, so every reload READ under
 * `anon` and every drag WROTE under the email. Widths persisted perfectly and
 * came back as the default. Caught by driving a real browser, not by a unit
 * test — so the guard here is on the rule that made it possible.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { stripComments } from './helpers/stripComments'

import {
  storageKey,
  resolveLayoutIdentity,
  clampSidebar,
  clampRail,
  fitsThreeColumns,
  readStored,
  writeStored,
  SIDEBAR_DEFAULT,
  SIDEBAR_MIN,
  SIDEBAR_MAX,
  RAIL_DEFAULT,
  RAIL_MIN,
  RAIL_MAX,
  RAIL_COLLAPSED,
  CONVERSATION_MIN,
  MESSAGE_MAX,
} from '@/composables/useColumnResize'

const read = (rel) => stripComments(readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8'))
const PORTAL = read('../../src/views/Portal.vue')
const HANDLE = read('../../src/components/ColumnResizeHandle.vue')
const SIDEBAR = read('../../src/components/portal/PortalSidebar.vue')
const RAIL = read('../../src/components/portal/PortalRail.vue')
const CONVERSATION = read('../../src/components/portal/PortalConversation.vue')
const ROOM = read('../../src/components/portal/PortalRoom.vue')
const SKELETON = read('../../src/components/portal/PortalSkeleton.vue')

// A localStorage stand-in. `throws` models private mode / blocked storage,
// which must degrade to the default layout rather than take the page down.
function fakeStorage(seed = {}, { throws = false } = {}) {
  const data = { ...seed }
  return {
    getItem: (k) => { if (throws) throw new Error('blocked'); return k in data ? data[k] : null },
    setItem: (k, v) => { if (throws) throw new Error('blocked'); data[k] = String(v) },
    _data: data,
  }
}

// ---------------------------------------------------------------------------
// Identity — the rule the live bug came from
// ---------------------------------------------------------------------------

describe('ent#492 — whose layout is this', () => {
  it('resolves the platform user SYNCHRONOUSLY, from storage', () => {
    // The whole point: available at setup, before first paint. A key built on
    // the portal store's `clientEmail` is `anon` at read time and the email at
    // write time, which silently breaks persistence.
    const s = fakeStorage({ auth0_user: JSON.stringify({ username: 'Admin', email: 'a@b.c' }) })
    expect(resolveLayoutIdentity(s)).toBe('admin')
  })

  it('falls back to the email, then to a client bucket, then to anon', () => {
    expect(resolveLayoutIdentity(fakeStorage({ auth0_user: JSON.stringify({ email: 'A@B.c' }) })))
      .toBe('a@b.c')
    expect(resolveLayoutIdentity(fakeStorage({ 'trinity.portalToken': 'tok' }))).toBe('client')
    expect(resolveLayoutIdentity(fakeStorage({}))).toBe('anon')
  })

  it('never derives the key from token material', () => {
    // The key is written back to localStorage in the clear. A namespace is not
    // worth putting a slice of a credential where anything can read it.
    const s = fakeStorage({ 'trinity.portalToken': 'super-secret-token-value' })
    expect(storageKey(resolveLayoutIdentity(s))).not.toContain('super-secret')
    expect(read('../../src/composables/useColumnResize.js')).not.toMatch(/portalToken.*slice|substr/)
  })

  it('survives unreadable or malformed storage', () => {
    expect(resolveLayoutIdentity(fakeStorage({}, { throws: true }))).toBe('anon')
    expect(resolveLayoutIdentity(fakeStorage({ auth0_user: '{not json' }))).toBe('anon')
    expect(resolveLayoutIdentity(null)).toBe('anon')
  })

  it('gives an unknown identity its OWN bucket, not the first user\'s', () => {
    expect(storageKey('')).toBe('trinity-workspace-columns:anon')
    expect(storageKey('Sam')).toBe('trinity-workspace-columns:sam')
    expect(storageKey('sam')).not.toBe(storageKey('pat'))
  })
})

// ---------------------------------------------------------------------------
// Clamps
// ---------------------------------------------------------------------------

describe('ent#492 — clamps', () => {
  it('bounds each column and rounds to whole pixels', () => {
    expect(clampSidebar(0)).toBe(SIDEBAR_MIN)
    expect(clampSidebar(99999)).toBe(SIDEBAR_MAX)
    expect(clampSidebar(300.4)).toBe(300)
    expect(clampRail(0)).toBe(RAIL_MIN)
    expect(clampRail(99999)).toBe(RAIL_MAX)
  })

  it('has defaults equal to the widths it replaced, so nothing moves on upgrade', () => {
    // `w-72` = 288px, `w-96` = 384px. An install that never drags anything must
    // render byte-identically to before.
    expect(SIDEBAR_DEFAULT).toBe(288)
    expect(RAIL_DEFAULT).toBe(384)
    expect(RAIL_COLLAPSED).toBe(48)
  })

  it('caps the message column wider than the max-w-4xl it replaced', () => {
    // AC 2: narrowing a neighbour has to visibly widen the messages, which it
    // cannot do if the cap is the old 896px.
    expect(MESSAGE_MAX).toBeGreaterThan(896)
  })
})

// ---------------------------------------------------------------------------
// The conversation is never the column that gets squeezed
// ---------------------------------------------------------------------------

describe('ent#492 — auto-collapse (AC 3)', () => {
  it('fits when the conversation clears its floor', () => {
    expect(fitsThreeColumns(1600, SIDEBAR_DEFAULT, RAIL_DEFAULT)).toBe(true)
    expect(fitsThreeColumns(SIDEBAR_DEFAULT + RAIL_DEFAULT + CONVERSATION_MIN, SIDEBAR_DEFAULT, RAIL_DEFAULT))
      .toBe(true)
  })

  it('does not fit one pixel below it', () => {
    expect(fitsThreeColumns(SIDEBAR_DEFAULT + RAIL_DEFAULT + CONVERSATION_MIN - 1, SIDEBAR_DEFAULT, RAIL_DEFAULT))
      .toBe(false)
    expect(fitsThreeColumns(900, SIDEBAR_MAX, RAIL_MAX)).toBe(false)
  })

  it('is a decision, not an action — the caller owns the rail state', () => {
    // Keeps the rule testable without a browser and keeps two owners of the
    // rail's open state from existing.
    expect(typeof fitsThreeColumns(1600, 288, 384)).toBe('boolean')
  })
})

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

describe('ent#492 — persistence', () => {
  it('round-trips through storage', () => {
    const s = fakeStorage()
    writeStored(s, 'sam', { sidebar: 320, rail: 420 })
    expect(readStored(s, 'sam')).toEqual({ sidebar: 320, rail: 420 })
  })

  it('clamps on the way IN as well as out — a hand-edited value cannot break the layout', () => {
    const s = fakeStorage({ 'trinity-workspace-columns:sam': JSON.stringify({ sidebar: 5, rail: 9999 }) })
    expect(readStored(s, 'sam')).toEqual({ sidebar: SIDEBAR_MIN, rail: RAIL_MAX })
  })

  it('reads corrupt, absent and unreadable storage as "no stored layout"', () => {
    expect(readStored(fakeStorage({ 'trinity-workspace-columns:sam': '{not json' }), 'sam')).toBeNull()
    expect(readStored(fakeStorage(), 'sam')).toBeNull()
    expect(readStored(fakeStorage({}, { throws: true }), 'sam')).toBeNull()
    expect(readStored(null, 'sam')).toBeNull()
  })

  it('never throws when storage refuses a write', () => {
    // Private mode: the layout still works this session, it just does not
    // survive a reload.
    expect(() => writeStored(fakeStorage({}, { throws: true }), 'sam', { sidebar: 300, rail: 400 }))
      .not.toThrow()
  })

  it('keeps two users apart', () => {
    const s = fakeStorage()
    writeStored(s, 'sam', { sidebar: 320, rail: 400 })
    writeStored(s, 'pat', { sidebar: 240, rail: 500 })
    expect(readStored(s, 'sam').sidebar).toBe(320)
    expect(readStored(s, 'pat').sidebar).toBe(240)
  })
})

// ---------------------------------------------------------------------------
// Wiring no unit test can reach
// ---------------------------------------------------------------------------

describe('ent#492 — the layout is driven by variables', () => {
  it('is a FLEX row, not a grid, because the handle count varies', () => {
    // The issue's technical notes suggested `grid-template-columns`, and the
    // first cut did that. A grid places children by COUNT, and the rail handle
    // is conditional (AC 1) — so with it absent the rail fell into the handle's
    // track and its own track sat empty. Caught live: the rail's expand button
    // was in the DOM and never became clickable. Flex does not care how many
    // children there are, which is the property this pins.
    expect(PORTAL).toContain('class="flex-1 flex min-h-0"')
    expect(PORTAL).not.toMatch(/grid-template-columns/)
    expect(PORTAL).toContain(':style="columns.gridStyle.value"')
    // The conversation is the flexible middle and carries no width of its own,
    // which is what makes it impossible to drag directly.
    expect(PORTAL).toMatch(/<main class="flex-1 min-w-0/)
    expect(PORTAL).toMatch(/sm:w-\[var\(--ws-sidebar,18rem\)\]/)
  })

  it('mounts both handles, and the rail one only when there is a column to drag', () => {
    expect(PORTAL).toContain('testid="ws-handle-sidebar"')
    expect(PORTAL).toMatch(/<ColumnResizeHandle[\s\S]{0,400}v-if="thirdColumnResizable"/)
    // A collapsed rail is a fixed strip, not a column — the handle goes with
    // the width it would drag.
    expect(PORTAL).toMatch(/thirdColumnResizable = computed\(/)
  })

  it('the columns read the variables rather than fixed classes', () => {
    expect(SIDEBAR).toMatch(/sm:w-full/)          // the grid cell owns the width
    expect(SIDEBAR).toMatch(/w-72/)               // ...but the mobile drawer keeps its own
    expect(RAIL).toMatch(/open: 'w-\[var\(--ws-rail,24rem\)\]/)
    expect(RAIL).toMatch(/collapsed: 'w-12/)      // the strip stays fixed
  })

  it('every message column follows ONE cap, the skeleton included', () => {
    // AC 2 says the cap has one definition. The skeleton matters as much as the
    // thread: it exists to hold the footprint the loaded surface lands on
    // (#2540), so a placeholder capped differently would shift the layout at
    // the moment it is replaced.
    for (const [name, src] of [['conversation', CONVERSATION], ['room', ROOM], ['skeleton', SKELETON]]) {
      expect(src, name).toContain('max-w-[var(--ws-message-max,64rem)]')
      expect(src, name).not.toContain('max-w-4xl')
    }
  })
})

describe('ent#492 — the handle', () => {
  it('is a labelled ARIA separator carrying its value and bounds', () => {
    expect(HANDLE).toContain('role="separator"')
    expect(HANDLE).toContain('aria-orientation="vertical"')
    for (const attr of [':aria-valuenow', ':aria-valuemin', ':aria-valuemax', ':aria-label']) {
      expect(HANDLE).toContain(attr)
    }
    expect(HANDLE).toContain('tabindex="0"')
  })

  it('captures the pointer, so a drag over the Canvas iframe does not stall', () => {
    // AC 8, and the one failure people would actually hit: without capture the
    // iframe swallows the pointer mid-gesture.
    expect(HANDLE).toContain('setPointerCapture')
    expect(HANDLE).toContain('releasePointerCapture')
    expect(HANDLE).toContain('touch-none')
    expect(HANDLE).toContain('select-none')
  })

  it('handles keyboard resize and reset', () => {
    for (const key of ['ArrowLeft', 'ArrowRight', 'Home', 'End']) {
      expect(HANDLE).toContain(`'${key}'`)
    }
    expect(HANDLE).toContain("emit('reset')")
    expect(HANDLE).toContain('@dblclick="$emit(\'reset\')"')
  })

  it('is hidden below sm, where the drawer and the bottom sheet take over', () => {
    // AC 9.
    expect(HANDLE).toMatch(/hidden sm:block/)
  })

  it('uses tokens, never raw colors', () => {
    expect(HANDLE).toMatch(/action-primary/)
    expect(HANDLE).not.toMatch(/#[0-9a-fA-F]{3,6}\b/)
  })

  it('is ONE component used by both handles', () => {
    expect((PORTAL.match(/<ColumnResizeHandle/g) || []).length).toBe(2)
    // `side` is a prop rather than a caller-applied sign flip: the sidebar grows
    // when dragged right, the rail narrows, and getting that backwards is
    // invisible in a test and instant in the hand.
    expect(PORTAL).toContain('side="left"')
    expect(PORTAL).toContain('side="right"')
  })
})
