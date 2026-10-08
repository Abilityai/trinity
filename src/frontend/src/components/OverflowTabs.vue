<script>
// The one knob for the fixed-tab width (#2579) — a companion `<script>` block
// because `<script setup>` cannot carry a named export, and both rows plus the
// spec have to be provably the same number.
export const FIXED_TAB_WIDTH = 'w-40'
</script>

<script setup>
/**
 * Responsive tab strip with a "More ▾" overflow dropdown (#1114).
 *
 * Replaces horizontal-scroll tab bars: renders as many tabs inline as fit the
 * container width and collapses the trailing remainder into a right-aligned
 * disclosure menu. Re-measures on container resize (ResizeObserver) and after
 * web-font load. Data-driven and reusable — drives AgentDetail's tab nav and
 * can serve any `{ id, label, badge? }` + active-id strip.
 *
 * Measurement strategy (deterministic "priority+" nav): a hidden, zero-layout
 * mirror row renders ALL tabs (+ a worst-case More button) so every tab's
 * width is measurable even while it lives in the dropdown; the visible row
 * shows the computed split. No flicker — defaults to all-inline until the
 * first measurement, so the common fits-everything case is correct on first
 * paint with zero snap.
 *
 * `fixedWidth` (#2579) is an OPT-IN for a strip of unbounded labels — the
 * Workspace's chat tabs, whose labels are user and model text. Under it every
 * tab is FIXED_TAB_WIDTH wide, its label clamps with an ellipsis, and the full
 * text rides `title=` on the button and on the menu row. Default `false`, and
 * EVERY width-related class below is gated on it, so the strips of short fixed
 * labels (Agent Detail, Library, the portal rail) are byte-identical without it
 * — including the native tooltip, which would otherwise appear on "Overview".
 *
 * The width class lands in BOTH rows for the reason the pinned glyph does: a
 * visible row that renders wider than the mirror measures is a strip that
 * overflows one tab too late. The visible button additionally needs `shrink-0`
 * and the visible nav `overflow-hidden`, which the mirror needs neither of —
 * `inlineCount` starts at +Infinity, so on first paint every tab is inline; a
 * truncating label drops the button's min-content to padding, and flex's
 * default `flex-shrink: 1` would squeeze the whole row to ~50px per tab for a
 * frame while the `width: max-content` mirror still reports the real 160.
 */
import { ref, computed, onMounted, onUnmounted, watch, nextTick, h } from 'vue'
import DraftMark from './base/DraftMark.vue'
// #1925 — the fit arithmetic moved to a pure module so NavBar's priority+ link
// row packs by the SAME rule instead of a second copy, and so the rule is
// reachable by a plain unit test.
import { computeInlineCount, FIT_EPSILON } from '../utils/overflowFit'

const props = defineProps({
  // [{ id, label, badge?, signal?: 'live'|'updated', pinned?: boolean }]
  // `pinned` (ent#523) draws a bookmark before the label — the Workspace's
  // Main chat, which is pinned first for the life of the (user, agent) pair.
  // Drawn in the MIRROR row too: a glyph the visible row renders and the
  // mirror does not is a tab measured narrower than it draws, which is how a
  // strip starts overflowing one tab too late.
  // `signal` (ent#474) draws the rail's activity dot after the label — the
  // ringed "live" shape or the plain "updated" one — in the visible row, the
  // overflow menu AND the mirror row, so the measured width includes it.
  // `badgeVariant` (ent#610 §3g A3a) picks the count's colour: `success` (the
  // default, the tinted pill every strip has always had), or `urgent` /
  // `primary` — SOLID white ink on the 700 tier (5.18 / 7.90:1), because a
  // counter is solid and a per-row fact is tinted (design-system.md) — or
  // `neutral`, a gray tint for a share of a count that is no event's outcome.
  // `badgeLabel` (A8c) is the tab's accessible name when the bare count would
  // be read as "Action 21"; the badge is then aria-hidden. Neither changes the
  // tab's width, so neither is in the re-measure key.
  // `hasDraft` (trinity-enterprise#657) draws the quiet Draft mark after the
  // label — "this tab holds unsent text" — in the same three places, and in
  // the re-measure key, because it changes the tab's width when it toggles.
  // `badgeSlot` (#3060) is an OPT-IN reservation for a count that lands after
  // the strip renders: the badge span is drawn whether or not `badge` has a
  // value — `invisible` (width kept) while it has none — at a tabular-nums
  // min-width, so the count's arrival does not slide the neighbouring tabs.
  // In the visible row AND the mirror (a tab measured narrower than it draws
  // overflows one tab too late); the dropdown keeps its badge/signal chain
  // untouched (#2794) — a menu row's count moves nothing beside it. A tab
  // without it renders exactly what it did.
  // `closable` (ent#841) draws an × on the tab — and on its overflow-menu row
  // — that emits `close` with the tab id. It is a SIBLING button laid over the
  // tab's right edge (a button nested in a button is invalid HTML), named by
  // `closeLabel`; `closeDisabled` + `closeTitle` make it inert and say why.
  // Hidden until hover/focus from `sm:` up, always drawn below it (no hover on
  // touch — the PortalStarButton rule). The tab reserves the ×'s room in its
  // own padding in BOTH rows, so the measured width is the drawn one. A strip
  // with no closable tab renders exactly what it did.
  tabs: { type: Array, required: true },
  // active tab id
  modelValue: { type: [String, null], required: true },
  // ent#451: the overflow trigger's label, given the hidden count — the
  // contract's counted "N more". Default keeps every existing strip's "More".
  // The mirror row measures the WIDEST label this strip can need (every tab
  // hidden), so a count that grows never reflows the fit decision.
  moreLabel: { type: Function, default: () => 'More' },
  // ent#451: a compact strip for a chat's tabs above the thread — smaller
  // pad and type, same measurement, same overflow behaviour.
  dense: { type: Boolean, default: false },
  // #2579: every tab the same width, labels clamped, full text on hover.
  // Deliberately its own axis rather than a rider on `dense` — density and
  // label-boundedness are different questions, and coupling them would clamp
  // any future dense strip of short fixed labels for nothing.
  fixedWidth: { type: Boolean, default: false },
  // ent#610 §3g B6a: OPT-IN tab semantics. When set, the inline tabs sit in a
  // `role="tablist"` named by this string, each a `role="tab"` with
  // `aria-selected` and a roving tabindex (one tab stop); Arrow Left/Right wrap,
  // Home/End jump, and activation stays MANUAL — arrows move focus, Enter /
  // Space (the button's own click) selects. The More trigger is not a tab and
  // stays outside the tablist. Default off: every strip that does not ask for
  // it renders exactly what it did (flipping the default is #3056).
  tablistLabel: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue', 'close'])

const rootEl = ref(null)        // width-driven container (RO target)
const measureNav = ref(null)    // hidden mirror row
const measureMoreEl = ref(null) // hidden worst-case More button
const moreBtnEl = ref(null)     // visible More trigger (focus return)
const menuEl = ref(null)        // dropdown panel

const containerWidth = ref(0)
const tabWidths = ref([])       // px, aligned to props.tabs
const moreWidth = ref(0)
// Default to all-inline before the first measure so the fits-everything case
// renders correctly on first paint with no collapse/snap (AC: no regression).
const inlineCount = ref(Number.POSITIVE_INFINITY)
const open = ref(false)

let ro = null
let rafId = null
let lastWidth = -1


// Full literal class strings so Tailwind's content scan sees every one. The
// default arm is the exact string the pill always carried; one arm per
// variant, never two colours of one property in a string (#2662).
const BADGE_TONES = {
  success: 'bg-status-success-100 dark:bg-status-success-900/50 text-status-success-700 dark:text-status-success-300',
  urgent: 'bg-status-urgent-700 text-white',
  primary: 'bg-action-primary-700 text-white',
  // A share of a count (the Inbox's per-agent facets, ent#610 A2): a fact about
  // the set, not the outcome of an event, so it is gray, never `status-*`.
  neutral: 'bg-gray-100 dark:bg-gray-750 text-gray-700 dark:text-gray-300',
  // ent#836: the calmer "needs you" count — a ring, no fill, urgent ink at the
  // AA tier (700 light / 400 dark). The agent row's mark in the Workspace
  // sidebar is the same recipe (`PortalSidebar.vue`), so the rail's Asks tab
  // and the row read as one number in one shape. `ring-inset` so the badge
  // keeps the filled tones' exact footprint.
  'urgent-outline': 'ring-1 ring-inset ring-status-urgent-600 dark:ring-status-urgent-400 text-status-urgent-700 dark:text-status-urgent-400 tabular-nums',
}
const badgeTone = (tab) => BADGE_TONES[tab.badgeVariant] || BADGE_TONES.success
// #3060: the reserved badge's footprint — one literal, used by both rows.
const BADGE_SLOT = 'min-w-[1.75rem] text-center tabular-nums'
const badgeSlotClass = (tab) => (tab.badgeSlot ? BADGE_SLOT : '')

// B6a: the wrapper the inline tabs render in — a labelled tablist when asked
// for, and NO element at all otherwise (the slot's nodes are returned bare), so
// a strip that did not opt in keeps its DOM. `<component :is="Fragment">`
// cannot do this: it renders a Fragment with no children.
const TablistWrap = (p, { slots }) => (p.label
  ? h('div', { role: 'tablist', 'aria-label': p.label, class: ['flex', p.clip ? 'min-w-0 overflow-hidden' : ''] }, slots.default?.())
  : slots.default?.())
TablistWrap.props = ['label', 'clip']

// ent#841: a closable tab is the tab button plus its × in one positioned box;
// any other tab is the bare button, so a strip with no closable tab keeps its
// DOM byte-for-byte.
const CLOSE_PAD = 'pr-7'
// The × takes the tab's own ink (`text-current`), so it reads as part of the
// tab in every state and theme without a colour of its own. Disabled is its
// own ARM, never a `disabled:` override of the hover ground (#2662).
const CLOSE_BTN = 'absolute right-1 top-1/2 -translate-y-1/2 p-1 rounded text-current transition opacity-100 sm:opacity-0 sm:group-hover/tab:opacity-100 sm:group-focus-within/tab:opacity-100 focus-visible:opacity-100'
const CLOSE_LIVE = 'hover:bg-gray-100 dark:hover:bg-gray-750'
const CLOSE_INERT = 'cursor-not-allowed'
const CloseX = (p) => h('svg', { class: ['w-3 h-3', p.dim ? 'opacity-40' : ''], fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor', 'aria-hidden': 'true' },
  [h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-width': '2.5', d: 'M6 18L18 6M6 6l12 12' })])
const CloseWrap = (p, { slots }) => {
  if (!p.tab?.closable) return slots.default?.()
  return h('span', { class: ['relative group/tab', p.shrink ? 'inline-flex shrink-0' : 'flex'] }, [
    ...(slots.default?.() || []),
    h('button', {
      type: 'button',
      class: [CLOSE_BTN, p.tab.closeDisabled ? CLOSE_INERT : CLOSE_LIVE],
      'aria-label': p.tab.closeLabel || 'Close',
      title: p.tab.closeTitle || p.tab.closeLabel || 'Close',
      disabled: !!p.tab.closeDisabled,
      'data-tab-close': p.tab.id,
      onClick: (e) => { e.stopPropagation(); if (!p.tab.closeDisabled) p.onClose?.(p.tab.id) },
    }, [h(CloseX, { dim: !!p.tab.closeDisabled })]),
  ])
}
CloseX.props = ['dim']
// `onClose` is a declared prop (what `@close` compiles to), not an emit: a
// functional component's attrs fallthrough would otherwise bind it twice.
CloseWrap.props = { tab: Object, shrink: Boolean, onClose: Function }
// The one tab stop: the selected tab when it is inline, else the first.
const rovingId = computed(() => {
  const inline = inlineTabs.value
  return (inline.find((t) => t.id === props.modelValue) || inline[0])?.id
})
function onTabKeydown(e, i) {
  if (!props.tablistLabel) return
  const list = rootEl.value?.querySelectorAll('[role="tablist"] [role="tab"]')
  const n = list ? list.length : 0
  if (!n) return
  let to = null
  if (e.key === 'ArrowRight') to = (i + 1) % n
  else if (e.key === 'ArrowLeft') to = (i - 1 + n) % n
  else if (e.key === 'Home') to = 0
  else if (e.key === 'End') to = n - 1
  if (to === null) return
  e.preventDefault()
  list[to].focus()
}

const tabPad = computed(() => (props.dense ? 'px-3 py-2 text-xs' : 'px-4 py-3 text-sm'))
const morePad = computed(() => (props.dense ? 'px-3 py-2 text-xs' : 'px-4 py-3 text-sm'))
const moreText = computed(() => props.moreLabel(overflowTabs.value.length))
const moreMeasureText = computed(() => props.moreLabel(props.tabs.length))

const inlineTabs = computed(() => props.tabs.slice(0, inlineCount.value))
const overflowTabs = computed(() => props.tabs.slice(inlineCount.value))
const hasOverflow = computed(() => overflowTabs.value.length > 0)
const activeInOverflow = computed(() =>
  overflowTabs.value.some((t) => t.id === props.modelValue)
)

// Re-measure when the tab set OR any label/badge changes (widths shift).
// `flush: 'post'` runs after the mirror row has rendered the new content.
const tabsSignature = computed(() =>
  props.tabs.map((t) => `${t.id}:${t.label}:${t.badge ?? ''}:${t.signal ?? ''}:${t.pinned ? 'p' : ''}:${t.hasDraft ? 'd' : ''}:${t.closable ? 'c' : ''}`).join('|')
  + `#${moreMeasureText.value}`
)
watch(tabsSignature, () => measure(), { flush: 'post' })

function syncWidth() {
  const w = rootEl.value ? rootEl.value.clientWidth : 0
  lastWidth = w
  containerWidth.value = w
}

function measure() {
  const nav = measureNav.value
  if (!nav) return
  const btns = nav.querySelectorAll('[data-measure-tab]')
  tabWidths.value = Array.from(btns).map((b) => b.getBoundingClientRect().width)
  moreWidth.value = measureMoreEl.value
    ? measureMoreEl.value.getBoundingClientRect().width
    : 80
  recompute()
}

function recompute() {
  // Stale widths (the mirror has not re-rendered for the new tab set yet) →
  // render all inline; this guard is about props, so it stays here while the
  // width arithmetic lives in the shared module.
  if (tabWidths.value.length !== props.tabs.length) {
    inlineCount.value = props.tabs.length
    return
  }
  inlineCount.value = computeInlineCount({
    containerWidth: containerWidth.value,
    itemWidths: tabWidths.value,
    moreWidth: moreWidth.value,
    // Padding-spaced strip: no flex gap to account for.
    gap: 0,
    epsilon: FIT_EPSILON,
  })
}

function onResize() {
  if (rafId != null) return
  rafId = requestAnimationFrame(() => {
    rafId = null
    const w = rootEl.value ? rootEl.value.clientWidth : 0
    if (w === lastWidth) return // width-diff guard: ignore height-only jitter
    lastWidth = w
    containerWidth.value = w
    recompute()
  })
}

function select(id) {
  emit('update:modelValue', id)
  closeMenu()
}

function openMenu() {
  open.value = true
  document.addEventListener('pointerdown', onPointerDown)
  nextTick(() => {
    menuEl.value?.querySelector('[data-menu-item]')?.focus()
  })
}

function closeMenu(returnFocus = false) {
  if (!open.value) return
  open.value = false
  document.removeEventListener('pointerdown', onPointerDown)
  if (returnFocus) moreBtnEl.value?.focus()
}

function toggleMenu() {
  open.value ? closeMenu() : openMenu()
}

function onPointerDown(e) {
  if (rootEl.value && !rootEl.value.contains(e.target)) closeMenu()
}

function onTriggerKeydown(e) {
  if (e.key === 'Escape') closeMenu(true)
}

onMounted(() => {
  ro = new ResizeObserver(onResize)
  if (rootEl.value) ro.observe(rootEl.value)
  nextTick(() => {
    syncWidth()
    measure()
  })
  // Font swap changes intrinsic text widths but does NOT resize the container,
  // so the ResizeObserver never fires — re-measure explicitly once fonts load.
  document.fonts?.ready?.then(() => {
    if (rootEl.value) {
      syncWidth()
      measure()
    }
  })
})

onUnmounted(() => {
  if (ro) ro.disconnect()
  if (rafId != null) cancelAnimationFrame(rafId)
  document.removeEventListener('pointerdown', onPointerDown)
})
</script>

<template>
  <div ref="rootEl" class="relative border-b border-gray-200 dark:border-gray-700">
    <!-- Visible row: inline tabs + right-pushed More trigger -->
    <nav class="-mb-px flex" :class="fixedWidth ? 'overflow-hidden' : ''">
      <TablistWrap :label="tablistLabel" :clip="fixedWidth">
      <CloseWrap v-for="(tab, i) in inlineTabs" :key="tab.id" :tab="tab" shrink @close="(id) => emit('close', id)">
      <button
        type="button"
        :title="tab.signalTitle || (fixedWidth ? tab.label : undefined)"
        :aria-keyshortcuts="tab.ariaKeyshortcuts || undefined"
        :role="tablistLabel ? 'tab' : undefined"
        :aria-selected="tablistLabel ? String(modelValue === tab.id) : undefined"
        :tabindex="tablistLabel ? (tab.id === rovingId ? 0 : -1) : undefined"
        :aria-label="tab.badgeLabel || undefined"
        @click="select(tab.id)"
        @keydown="onTabKeydown($event, i)"
        :class="[
          tabPad,
          tab.closable ? CLOSE_PAD : '',
          fixedWidth ? `${FIXED_TAB_WIDTH} shrink-0` : '',
          'border-b-2 font-medium transition-colors whitespace-nowrap inline-flex items-center',
          modelValue === tab.id
            ? 'border-action-primary-500 text-action-primary-600 dark:text-action-primary-400'
            : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
        ]"
      >
        <svg v-if="tab.pinned" class="w-3.5 h-3.5 mr-1 shrink-0 opacity-70" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 5a2 2 0 012-2h10a2 2 0 012 2v16l-7-3.5L5 21V5z" /></svg>
        <span class="min-w-0 truncate">{{ tab.label }}</span>
        <DraftMark v-if="tab.hasDraft" class="ml-1.5" />
        <span
          v-if="tab.badge || tab.badgeSlot"
          :class="['ml-1.5 shrink-0 px-1.5 py-0.5 text-[10px] font-semibold', badgeTone(tab), 'rounded-full leading-none', badgeSlotClass(tab), tab.badge ? '' : 'invisible']"
          :aria-hidden="tab.badgeLabel ? 'true' : undefined"
          :data-badge-slot="tab.badgeSlot ? '' : undefined"
        >
          {{ tab.badge }}
        </span>
        <span
          v-if="tab.signal"
          class="ml-1.5 shrink-0 rounded-full bg-action-primary-500"
          :class="tab.signal === 'live'
            ? 'w-2 h-2 ring-[3px] ring-action-primary-500/[.28] motion-safe:animate-pulse'
            : 'w-1.5 h-1.5'"
          aria-hidden="true"
        ></span>
      </button>
      </CloseWrap>
      </TablistWrap>

      <!-- More trigger (kept fixed-width "More ▾"; reflects active state when
           the selected tab is in the overflow set — AC). -->
      <button
        v-if="hasOverflow"
        ref="moreBtnEl"
        type="button"
        data-overflow-trigger
        @click="toggleMenu"
        @keydown="onTriggerKeydown"
        :aria-expanded="open"
        aria-controls="overflow-tabs-menu"
        :class="[
          morePad,
          'ml-auto border-b-2 font-medium transition-colors whitespace-nowrap inline-flex items-center gap-1',
          activeInOverflow
            ? 'border-action-primary-500 text-action-primary-600 dark:text-action-primary-400'
            : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
        ]"
      >
        {{ moreText }}
        <span
          v-if="activeInOverflow"
          class="w-1.5 h-1.5 rounded-full bg-action-primary-500"
          aria-hidden="true"
        ></span>
        <svg
          class="w-3.5 h-3.5 transition-transform"
          :class="open ? 'rotate-180' : ''"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          stroke-width="2"
          aria-hidden="true"
        >
          <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
    </nav>

    <!-- Dropdown panel (sibling of nav so it is not clipped). Plain disclosure
         of buttons — NOT a role="menu" (no arrow-key roving), consistent with
         the page's plain-button tabs: Tab traverses items, Escape closes and
         returns focus to the trigger, outside-pointerdown closes. -->
    <div
      v-if="open && hasOverflow"
      id="overflow-tabs-menu"
      ref="menuEl"
      data-overflow-menu
      class="absolute right-0 top-full z-20 mt-px min-w-[12rem] max-h-[70vh] overflow-y-auto py-1 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-md shadow-lg dark:shadow-gray-900"
      @keydown="onTriggerKeydown"
    >
      <CloseWrap v-for="tab in overflowTabs" :key="tab.id" :tab="tab" @close="(id) => emit('close', id)">
      <button
        data-menu-item
        type="button"
        :aria-label="tab.badgeLabel || undefined"
        :title="tab.signalTitle || (fixedWidth ? tab.label : undefined)"
        :aria-keyshortcuts="tab.ariaKeyshortcuts || undefined"
        @click="select(tab.id)"
        :class="[
          'w-full px-4 py-2 text-left text-sm transition-colors flex items-center justify-between gap-2',
          tab.closable ? CLOSE_PAD : '',
          modelValue === tab.id
            ? 'bg-action-primary-50 dark:bg-action-primary-900/30 text-action-primary-700 dark:text-action-primary-300 font-medium'
            : 'text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700'
        ]"
      >
        <svg v-if="tab.pinned" class="w-3.5 h-3.5 mr-1 shrink-0 opacity-70" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 5a2 2 0 012-2h10a2 2 0 012 2v16l-7-3.5L5 21V5z" /></svg>
        <span :class="fixedWidth ? 'max-w-[20rem] truncate' : ''">{{ tab.label }}</span>
        <!-- Its own `v-if`, placed BEFORE the badge/signal pair below: that pair
             is a `v-if`/`v-else-if` chain, and an element inserted between its
             arms would silently repoint the `v-else-if` (#2794). -->
        <DraftMark v-if="tab.hasDraft" />
        <span
          v-if="tab.badge"
          :class="['px-1.5 py-0.5 text-[10px] font-semibold', badgeTone(tab), 'rounded-full leading-none']"
          :aria-hidden="tab.badgeLabel ? 'true' : undefined"
        >
          {{ tab.badge }}
        </span>
        <span
          v-else-if="tab.signal"
          class="rounded-full bg-action-primary-500"
          :class="tab.signal === 'live' ? 'w-2 h-2 ring-[3px] ring-action-primary-500/[.28]' : 'w-1.5 h-1.5'"
          aria-hidden="true"
        ></span>
      </button>
      </CloseWrap>
    </div>

    <!-- Hidden zero-layout mirror row: measures every tab's width (incl. badge)
         and a worst-case More button. visibility:hidden keeps boxes measurable
         (display:none would report 0); the 0×0 overflow:hidden wrapper means it
         contributes no layout and cannot induce page scroll. -->
    <div
      aria-hidden="true"
      class="pointer-events-none"
      style="position: absolute; top: 0; left: 0; width: 0; height: 0; overflow: hidden; visibility: hidden;"
    >
      <nav ref="measureNav" class="-mb-px flex" style="width: max-content;">
        <!-- Under `fixedWidth` the mirror takes the width class and NOTHING
             else: `getBoundingClientRect()` returns the border box, so a 160px
             button whose text overflows still measures 160, and this row is
             `width: max-content` so it never shrinks — it needs neither the
             label span nor `shrink-0`. -->
        <button
          v-for="tab in tabs"
          :key="`m-${tab.id}`"
          data-measure-tab
          type="button"
          tabindex="-1"
          :class="[tabPad, tab.closable ? CLOSE_PAD : '', fixedWidth ? FIXED_TAB_WIDTH : '']"
          class="border-b-2 font-medium whitespace-nowrap inline-flex items-center"
        >
          <svg v-if="tab.pinned" class="w-3.5 h-3.5 mr-1 shrink-0 opacity-70" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 5a2 2 0 012-2h10a2 2 0 012 2v16l-7-3.5L5 21V5z" /></svg>
          {{ tab.label }}
          <DraftMark v-if="tab.hasDraft" class="ml-1.5" />
          <span
            v-if="tab.badge || tab.badgeSlot"
            class="ml-1.5 px-1.5 py-0.5 text-[10px] font-semibold rounded-full leading-none"
            :class="badgeSlotClass(tab)"
          >
            {{ tab.badge }}
          </span>
          <span
            v-if="tab.signal"
            class="ml-1.5 rounded-full"
            :class="tab.signal === 'live' ? 'w-2 h-2' : 'w-1.5 h-1.5'"
          ></span>
        </button>
        <button
          ref="measureMoreEl"
          data-measure-more
          type="button"
          tabindex="-1"
          :class="morePad"
          class="ml-auto border-b-2 font-medium whitespace-nowrap inline-flex items-center gap-1"
        >
          {{ moreMeasureText }}
          <span class="w-1.5 h-1.5 rounded-full"></span>
          <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M19 9l-7 7-7-7" />
          </svg>
        </button>
      </nav>
    </div>
  </div>
</template>
