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
 */
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'

const props = defineProps({
  // [{ id: string, label: string, badge?: string|number, signal?: 'live'|'updated' }]
  // `signal` (ent#474) draws the rail's activity dot after the label — the
  // ringed "live" shape or the plain "updated" one — in the visible row, the
  // overflow menu AND the mirror row, so the measured width includes it.
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
})
const emit = defineEmits(['update:modelValue'])

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

const EPSILON = 1 // px tolerance for sub-pixel rounding in the fit decision

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
  props.tabs.map((t) => `${t.id}:${t.label}:${t.badge ?? ''}:${t.signal ?? ''}`).join('|')
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
  const cw = containerWidth.value
  // Not yet measured / hidden container / stale widths → render all inline.
  if (cw <= 0 || tabWidths.value.length !== props.tabs.length) {
    inlineCount.value = props.tabs.length
    return
  }
  const widths = tabWidths.value
  const total = widths.reduce((a, b) => a + b, 0)
  if (total <= cw + EPSILON) {
    inlineCount.value = props.tabs.length // everything fits — no More button
    return
  }
  // Reserve room for the More trigger, then pack from the left.
  const avail = cw - moreWidth.value
  let acc = 0
  let count = 0
  for (let i = 0; i < widths.length; i++) {
    if (acc + widths[i] <= avail + EPSILON) {
      acc += widths[i]
      count++
    } else {
      break
    }
  }
  // If exactly one tab overflows and it would fit without reserving the More
  // trigger, keep it inline rather than spend More-width to hide one item.
  if (count === widths.length - 1 && acc + widths[count] <= cw + EPSILON) {
    count = widths.length
  }
  // Always keep at least one tab inline when even the first one fits.
  if (count === 0 && widths[0] <= cw + EPSILON) count = 1
  inlineCount.value = count
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
    <nav class="-mb-px flex">
      <button
        v-for="tab in inlineTabs"
        :key="tab.id"
        type="button"
        @click="select(tab.id)"
        :class="[
          tabPad,
          'border-b-2 font-medium transition-colors whitespace-nowrap inline-flex items-center',
          modelValue === tab.id
            ? 'border-action-primary-500 text-action-primary-600 dark:text-action-primary-400'
            : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
        ]"
      >
        {{ tab.label }}
        <span
          v-if="tab.badge"
          class="ml-1.5 px-1.5 py-0.5 text-[10px] font-semibold bg-status-success-100 dark:bg-status-success-900/50 text-status-success-700 dark:text-status-success-300 rounded-full leading-none"
        >
          {{ tab.badge }}
        </span>
        <span
          v-if="tab.signal"
          class="ml-1.5 rounded-full bg-action-primary-500"
          :class="tab.signal === 'live'
            ? 'w-2 h-2 ring-[3px] ring-action-primary-500/[.28] motion-safe:animate-pulse'
            : 'w-1.5 h-1.5'"
          aria-hidden="true"
        ></span>
      </button>

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
      <button
        v-for="tab in overflowTabs"
        :key="tab.id"
        data-menu-item
        type="button"
        @click="select(tab.id)"
        :class="[
          'w-full px-4 py-2 text-left text-sm transition-colors flex items-center justify-between gap-2',
          modelValue === tab.id
            ? 'bg-action-primary-50 dark:bg-action-primary-900/30 text-action-primary-700 dark:text-action-primary-300 font-medium'
            : 'text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700'
        ]"
      >
        <span>{{ tab.label }}</span>
        <span
          v-if="tab.badge"
          class="px-1.5 py-0.5 text-[10px] font-semibold bg-status-success-100 dark:bg-status-success-900/50 text-status-success-700 dark:text-status-success-300 rounded-full leading-none"
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
        <button
          v-for="tab in tabs"
          :key="`m-${tab.id}`"
          data-measure-tab
          type="button"
          tabindex="-1"
          :class="tabPad"
          class="border-b-2 font-medium whitespace-nowrap inline-flex items-center"
        >
          {{ tab.label }}
          <span
            v-if="tab.badge"
            class="ml-1.5 px-1.5 py-0.5 text-[10px] font-semibold rounded-full leading-none"
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
