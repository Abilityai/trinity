/**
 * Resizable Workspace columns (ent#492).
 *
 * Owns the numbers and the rules; the handle component only emits deltas. That
 * split is what lets both handles be one component: a handle knows how far the
 * pointer moved, and nothing else about which column it is dragging.
 *
 * The layout is a CSS grid on `Portal.vue`:
 *
 *     grid-template-columns: var(--ws-sidebar) minmax(0, 1fr) var(--ws-rail)
 *
 * so the conversation is the flexible middle by construction and never needs a
 * width of its own — which is also why it cannot be dragged directly. Widening
 * the messages is done by narrowing a neighbour, and that is the AC.
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'

// Defaults are today's classes, so an install that has never dragged anything
// renders byte-identically to before: `w-72` sidebar, `w-96` rail.
export const SIDEBAR_DEFAULT = 288
export const RAIL_DEFAULT = 384
// The collapsed rail is a fixed strip, not a resizable column (ent#474's 48px).
export const RAIL_COLLAPSED = 48

// Clamps. The minima are where a column stops being able to do its job: a
// sidebar narrower than this truncates every agent name to nothing, and a rail
// narrower than this cannot lay out the Work card's step list.
export const SIDEBAR_MIN = 200
export const RAIL_MIN = 280

// #2617: the MAXIMA are derived from the viewport, not fixed pixels.
//
// They used to be `SIDEBAR_MAX = 480` / `RAIL_MAX = 560`, applied
// unconditionally — which meant the wider the display, the SMALLER the share a
// person could give a column. On a 2560px screen the rail stopped at ~22%,
// reported by the operator as "it does not go bigger than like 20% or
// something. I should be able to make it whatever I want, say half the screen
// width." That is the opposite of what a resize handle is for, and it
// contradicted this block's own stated intent ("deliberately generous — this is
// a person arranging their own screen, not a layout that needs defending from
// them"), which an absolute pixel number cannot express.
//
// The right bound was already in this file. `CONVERSATION_MIN` is the readable
// floor for the message column, and `fitsThreeColumns` already treats it as the
// thing a layout must not break. So a column may take everything left after the
// OTHER two: the conversation's floor is the one rule that stops a drag, at
// every screen size, and it protects a narrow window exactly as before.
//
// The two are symmetric on purpose. `SIDEBAR_MAX` had the same shape of problem
// (AC 7) — nobody had hit it, but keeping one derived and one fixed would leave
// the next reader guessing which rule this file follows.

export function railMaxFor(viewportWidth, sidebarWidth) {
  return columnMaxFor(viewportWidth, sidebarWidth, RAIL_MIN)
}

export function sidebarMaxFor(viewportWidth, railWidth) {
  return columnMaxFor(viewportWidth, railWidth, SIDEBAR_MIN)
}

/**
 * The space one column may take: the viewport, less its neighbour, less the
 * conversation's floor.
 *
 * Never returns less than the column's own minimum. A maximum below the minimum
 * would invert the clamp — `Math.min(max, Math.max(min, px))` would then pin
 * the column to the MAXIMUM and quietly break the floor that minimum exists to
 * hold. On a viewport with no room for three columns the auto-collapse rule
 * (`fitsThreeColumns` → `enforceFit`) is what applies, not a negative ceiling.
 *
 * A viewport of 0 — no `window`, a test, an element not yet laid out — is not a
 * measurement, so it yields the minimum rather than a number derived from
 * nothing. Callers that can be reached before layout use the stored width
 * directly; see `railWidth`.
 */
function columnMaxFor(viewportWidth, otherWidth, ownMin) {
  const vp = Math.floor(Number(viewportWidth) || 0)
  const other = Math.round(Number(otherWidth) || 0)
  if (vp <= 0) return ownMin
  return Math.max(ownMin, vp - other - CONVERSATION_MIN)
}

// The conversation's floor. Below this a thread stops being readable, so the
// AC's rule applies: the rail auto-collapses rather than the conversation being
// squeezed. It is checked against the VIEWPORT, so it also covers a window that
// was resized rather than a handle that was dragged.
export const CONVERSATION_MIN = 480

// The readable cap on the message column, and THE one place it is defined
// (AC 2). Wider than the `max-w-4xl` (896px) it replaces, because the whole
// point of narrowing the rail is that the messages get the room.
export const MESSAGE_MAX = 1100

// Arrow-key step, and the coarse step for shift-arrow. A resize a keyboard user
// cannot feel is not accessible, and one they cannot do precisely is not either.
export const KEY_STEP = 16
export const KEY_STEP_COARSE = 64

const KEY_PREFIX = 'trinity-workspace-columns'
// Mirrors `stores/clientPortal.js`'s own constant. Read-only here: this only
// asks WHETHER a portal session exists, never what its token is.
const PORTAL_TOKEN_KEY = 'trinity.portalToken'

export function storageKey(userKey) {
  // Per user (AC 4): two people on one browser must not inherit each other's
  // layout. An absent identity gets its own bucket rather than sharing the
  // first user's — `anon` is a bucket, not a fallback into someone else's.
  const who = String(userKey || '').trim().toLowerCase() || 'anon'
  return `${KEY_PREFIX}:${who}`
}

/**
 * Who this layout belongs to, resolved SYNCHRONOUSLY at setup.
 *
 * This is the whole difficulty of AC 4, which asks for two things that pull
 * against each other: keyed per user, AND applied before first paint. The
 * portal store's `clientEmail` cannot serve — it starts `null` and is filled
 * from a network response, so a key built on it reads `anon` on every reload
 * and writes under the email a moment later. Caught live: the width persisted
 * correctly and came back as the default, because the read and the write used
 * different keys.
 *
 * `auth0_user` is written to localStorage at login and is therefore already
 * there when this runs. A portal client has no such record, so it gets one
 * shared `client` bucket — that browser holds one portal token at a time, so
 * there is no second client to collide with.
 *
 * Deliberately NEVER derived from token material: the key is written back to
 * localStorage in the clear, and a namespace is not worth putting a slice of a
 * credential where anything can read it.
 */
export function resolveLayoutIdentity(storage) {
  try {
    const raw = storage?.getItem?.('auth0_user')
    if (raw) {
      const u = JSON.parse(raw)
      const who = u?.username || u?.email
      if (who) return String(who).trim().toLowerCase()
    }
    if (storage?.getItem?.(PORTAL_TOKEN_KEY)) return 'client'
  } catch {
    // Unreadable storage is an anonymous bucket, never a crash.
  }
  return 'anon'
}

// #2617: the ceiling is passed IN rather than read from a constant or a global,
// which is what keeps these pure and testable at several viewport widths. An
// omitted `max` means "no viewport-derived ceiling, floor only" — the shape the
// persistence path needs, for the reason on `readStored`.
export function clampSidebar(px, max = Infinity) {
  return Math.min(max, Math.max(SIDEBAR_MIN, Math.round(Number(px) || 0)))
}

export function clampRail(px, max = Infinity) {
  return Math.min(max, Math.max(RAIL_MIN, Math.round(Number(px) || 0)))
}

/**
 * Does this viewport have room for all three columns at these widths?
 *
 * Pure so the auto-collapse rule is testable without a browser. Returns the
 * decision, not the action: the caller owns the rail's open state.
 */
export function fitsThreeColumns(viewportWidth, sidebar, rail) {
  return (viewportWidth - sidebar - rail) >= CONVERSATION_MIN
}

export function readStored(storage, userKey) {
  try {
    const raw = storage?.getItem?.(storageKey(userKey))
    if (!raw) return null
    const v = JSON.parse(raw)
    if (!v || typeof v !== 'object') return null
    // #2617 (AC 4): floored, never CAPPED to the current viewport. What is
    // stored is the width the person asked for; the cap is applied where the
    // number is USED (`railWidth`), so a layout arranged on a 2560px monitor
    // comes back intact on that monitor after a stint on a laptop. Clamping
    // here would have written the laptop's ceiling back over their choice on
    // the first `commit()`, which is "silently discarded" wearing a clamp.
    return {
      sidebar: Number.isFinite(v.sidebar) ? clampSidebar(v.sidebar) : SIDEBAR_DEFAULT,
      rail: Number.isFinite(v.rail) ? clampRail(v.rail) : RAIL_DEFAULT,
    }
  } catch {
    // A corrupt or unreadable value is the default layout, never a crash on a
    // surface whose whole job is to render someone's conversation.
    return null
  }
}

export function writeStored(storage, userKey, widths) {
  try {
    storage?.setItem?.(storageKey(userKey), JSON.stringify({
      sidebar: clampSidebar(widths.sidebar),
      rail: clampRail(widths.rail),
    }))
  } catch {
    // Private mode, quota, blocked storage: the layout still works for this
    // session, it just does not survive a reload.
  }
}

function safeStorage() {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null
  } catch {
    return null
  }
}

/**
 * @param {object} opts
 * @param {import('vue').Ref<boolean>} opts.railOpen  the rail's own state
 * @param {(open: boolean) => void} [opts.setRailOpen]  auto-collapse hook
 */
export function useColumnResize({ railOpen, setRailOpen = () => {} } = {}) {
  const storage = safeStorage()
  // Resolved from storage, not from the portal store — see
  // `resolveLayoutIdentity`. Held in a ref so a sign-in that happens without a
  // full reload re-reads under the right key instead of leaving this session
  // writing to `anon`.
  const identity = ref(resolveLayoutIdentity(storage))
  // Read synchronously at setup, BEFORE first paint (AC 4): a reload must not
  // flash the default layout and settle into the stored one.
  const stored = readStored(storage, identity.value)
  const sidebar = ref(stored?.sidebar ?? SIDEBAR_DEFAULT)
  // The rail remembers the width it was OPEN at, so collapsing and reopening
  // restores it rather than the default (AC 4, last clause). Collapsed is a
  // fixed strip and never overwrites this number.
  const railOpenWidth = ref(stored?.rail ?? RAIL_DEFAULT)

  // #2617: the maxima depend on the viewport, so the viewport has to be
  // reactive. Seeded synchronously at setup for the same reason the stored
  // widths are — a first paint at the wrong ceiling is a visible jump — and
  // updated by the `resize` listener the auto-collapse rule already installs,
  // so this adds no second listener.
  const viewportWidth = ref(typeof window !== 'undefined' ? window.innerWidth : 0)

  // The DESIRED widths are `sidebar` / `railOpenWidth`; these are what the
  // layout may actually give them right now. Splitting the two is the whole of
  // AC 4: a width arranged on a wide monitor is clamped for display on a narrow
  // one and comes back when there is room, because the desired number was never
  // overwritten.
  // Declared in dependency order. `computed` bodies are lazy, so the reverse
  // would also run — but a reader should not have to prove that to themselves,
  // and a later refactor that reads one of these eagerly would hit a TDZ error
  // that looks nothing like its cause.
  const railMax = computed(() => railMaxFor(viewportWidth.value, sidebar.value))
  const effectiveRail = computed(() => Math.min(railOpenWidth.value, railMax.value))
  // The sidebar's ceiling is measured against what the rail is ACTUALLY taking,
  // not what it would like to take: otherwise a rail whose desired width the
  // viewport cannot honour would keep charging the sidebar for space nobody is
  // using.
  const sidebarMax = computed(() => sidebarMaxFor(viewportWidth.value, effectiveRail.value))
  const effectiveSidebar = computed(() => Math.min(sidebar.value, sidebarMax.value))

  const railWidth = computed(() => (railOpen?.value ? effectiveRail.value : RAIL_COLLAPSED))

  // What the grid reads. Kept as one object so `Portal.vue` binds a single
  // `:style` and cannot set one variable and forget the other.
  const gridStyle = computed(() => ({
    '--ws-sidebar': `${effectiveSidebar.value}px`,
    '--ws-rail': `${railWidth.value}px`,
    '--ws-message-max': `${MESSAGE_MAX}px`,
  }))

  let frame = null
  function commit() {
    // rAF-throttled (AC 8): a pointermove fires far faster than the browser
    // paints, and writing a CSS variable per event is how a drag turns into
    // layout thrash in a thread with hundreds of messages.
    if (frame !== null) return
    frame = requestAnimationFrame(() => {
      frame = null
      writeStored(storage, identity.value, { sidebar: sidebar.value, rail: railOpenWidth.value })
    })
  }

  function resizeSidebar(px) {
    sidebar.value = clampSidebar(px, sidebarMax.value)
    commit()
  }

  function resizeRail(px) {
    railOpenWidth.value = clampRail(px, railMax.value)
    commit()
  }

  function resetSidebar() {
    sidebar.value = SIDEBAR_DEFAULT
    commit()
  }

  function resetRail() {
    railOpenWidth.value = RAIL_DEFAULT
    commit()
  }

  // AC 3: when the viewport cannot fit all three, the RAIL collapses — the
  // conversation is never the column that gets squeezed. Checked on resize as
  // well as on drag, because a window someone dragged narrower is the same
  // situation arrived at differently.
  function enforceFit(vw = typeof window !== 'undefined' ? window.innerWidth : 0) {
    if (!vw) return
    // #2617: record the viewport even when the rail is closed — the maxima are
    // derived from it, and a window resized while the rail was collapsed would
    // otherwise reopen it against a stale ceiling.
    viewportWidth.value = vw
    if (!railOpen?.value) return
    // Tested against the EFFECTIVE width, not the desired one. Since the drag
    // is now bounded by the same conversation floor this asks about, a drag can
    // no longer break the fit at all; what remains is the case this rule was
    // written for — a window narrowed until even `RAIL_MIN` will not fit
    // beside the conversation's floor. Testing the desired width here would
    // collapse a rail that fits, purely because the person once arranged a
    // wider one on a bigger screen.
    if (!fitsThreeColumns(vw, effectiveSidebar.value, effectiveRail.value)) {
      setRailOpen(false)
    }
  }

  // A login that does not reload the page changes who this layout belongs to.
  // Re-reading is right even though it can move the columns: the alternative is
  // this session silently writing one person's layout into another's bucket.
  function refreshIdentity() {
    const next = resolveLayoutIdentity(storage)
    if (next === identity.value) return
    identity.value = next
    const s = readStored(storage, next)
    sidebar.value = s?.sidebar ?? SIDEBAR_DEFAULT
    railOpenWidth.value = s?.rail ?? RAIL_DEFAULT
  }

  let onWindowResize = null
  onMounted(() => {
    onWindowResize = () => enforceFit()
    window.addEventListener('resize', onWindowResize)
    enforceFit()
  })
  onBeforeUnmount(() => {
    if (onWindowResize) window.removeEventListener('resize', onWindowResize)
    if (frame !== null) cancelAnimationFrame(frame)
  })

  return {
    sidebar,
    railOpenWidth,
    railWidth,
    // #2617: the live ceilings. Exposed as computeds rather than folded into
    // `limits` because a template reads `columns.railMax.value` — a ref nested
    // inside a plain object is NOT auto-unwrapped, and `limits.rail.max` would
    // have silently rendered `[object Object]` into `aria-valuemax`.
    railMax,
    sidebarMax,
    effectiveRail,
    effectiveSidebar,
    viewportWidth,
    gridStyle,
    resizeSidebar,
    resizeRail,
    resetSidebar,
    resetRail,
    enforceFit,
    refreshIdentity,
    limits: {
      // The MINIMA and defaults are still constants. The maxima moved out to
      // `railMax` / `sidebarMax` above (#2617) and are deliberately absent
      // here rather than left as stale numbers a caller could still read.
      sidebar: { min: SIDEBAR_MIN, default: SIDEBAR_DEFAULT },
      rail: { min: RAIL_MIN, default: RAIL_DEFAULT },
    },
  }
}
