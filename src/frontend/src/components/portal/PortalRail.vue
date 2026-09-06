<!--
  Workspace conversation rail — the shell (trinity-enterprise#474, slice 1 of
  #472). Built to the approved design pass (2026-09-06).

  Three forms, one component, one body:

    collapsed   a 48px strip beside the conversation — an expand control, then
                one icon button per tab this session may see, each carrying its
                activity signal so the rail says what is live without opening
    open        a 384px column — an `OverflowTabs` strip + collapse control over
                a body that is its OWN scroll axis (principle 8; the chat keeps
                the other)
    sheet       below `sm`: the `PortalFilesPanel` bottom-sheet pattern, opened
                from the strip above the composer (`PortalRailStrip`)

  What this file decides: nothing. Which tabs render, which is active, what a
  signal means, how a room groups, what an empty state says — all of it is the
  pure module `portalRail.js`, because vitest runs node-env here with no mount
  harness. The one gate is `visibleTabs`: the `tabs` prop IS that list, and the
  body is mounted for exactly one of them, so a tab whose door this session
  fails is never rendered and never asked for its data (the per-door test).

  Slot contract for the tabs that dock later (slice 2): a tab supplies its body
  as `#tab-<id>` with `{ tab, participants, signal, group }`, where `group` is
  `groupByParticipant` — the one rule for "a room shows every participant, in
  order, absence visible". With no slot content the rail renders the tab's own
  empty state from the registry, which is what the Work tab shows today.
-->
<template>
  <div
    v-if="tabs.length"
    :class="mode === 'sheet' ? 'fixed inset-0 z-40 sm:hidden' : 'hidden sm:flex shrink-0 min-h-0'"
    :data-testid="mode === 'sheet' ? 'portal-rail-sheet' : 'portal-rail'"
    :data-mode="mode"
  >
    <div v-if="mode === 'sheet'" class="absolute inset-0 bg-black/30" @click="$emit('close')"></div>

    <aside
      :class="ASIDE[mode]"
      :role="mode === 'sheet' ? 'dialog' : undefined"
      :aria-modal="mode === 'sheet' ? 'true' : undefined"
      aria-label="Conversation rail"
    >
      <!-- ============================ COLLAPSED ============================ -->
      <template v-if="mode === 'collapsed'">
        <div class="h-14 shrink-0 flex items-center justify-center border-b border-gray-200 dark:border-gray-800">
          <button
            type="button"
            :class="[ICON_BTN, 'text-gray-400']"
            title="Open"
            aria-label="Open the conversation rail"
            aria-expanded="false"
            data-testid="portal-rail-expand"
            @click="setOpen(true)"
          >
            <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" :d="GLYPHS.expand" /></svg>
          </button>
        </div>
        <div class="pt-2 flex flex-col items-center gap-1">
          <!-- One button per VISIBLE tab; the signal rides the icon's corner.
               gray-400 idle, gray-600 (dark gray-300) when it carries one, so
               the tab with something to say is also the darker glyph
               (principle 24: shape and weight, never hue alone). -->
          <button
            v-for="s in strip"
            :key="s.id"
            type="button"
            :class="[ICON_BTN, 'relative', s.shape ? 'text-gray-600 dark:text-gray-300' : 'text-gray-400']"
            :title="s.title"
            :aria-label="`Open ${s.title}`"
            :data-testid="`portal-rail-tab-${s.id}`"
            @click="openOn(s.id)"
          >
            <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" :d="iconPath(s.id)" /></svg>
            <span v-if="s.shape" class="absolute top-1 right-1" :class="dotClass(s.shape)" aria-hidden="true"></span>
          </button>
        </div>
      </template>

      <!-- ========================== OPEN / SHEET ========================== -->
      <template v-else>
        <div v-if="mode === 'sheet'" class="shrink-0 flex justify-center pt-2">
          <span class="w-9 h-1 rounded-full bg-gray-300 dark:bg-gray-600" aria-hidden="true"></span>
        </div>

        <div class="shrink-0 flex items-stretch" :class="mode === 'open' ? 'h-14' : ''">
          <!-- The contract primitive for a fixed small tab set: it repacks on
               its own resize, so #492's handle needs nothing from here. The
               `signal` field is the label dot the design pass specifies. -->
          <OverflowTabs
            class="flex-1 min-w-0 h-full flex flex-col justify-end"
            :tabs="stripTabs"
            :model-value="active ? active.id : null"
            @update:model-value="setTab"
          />
          <div class="flex items-center px-2 border-b border-gray-200 dark:border-gray-700">
            <button
              type="button"
              :class="[ICON_BTN, 'text-gray-400']"
              :title="mode === 'sheet' ? 'Close' : 'Collapse'"
              :aria-label="mode === 'sheet' ? 'Close the conversation rail' : 'Collapse the conversation rail'"
              :aria-expanded="mode === 'sheet' ? undefined : 'true'"
              data-testid="portal-rail-collapse"
              @click="mode === 'sheet' ? $emit('close') : setOpen(false)"
            >
              <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" :d="mode === 'sheet' ? GLYPHS.close : GLYPHS.collapse" /></svg>
            </button>
          </div>
        </div>

        <!-- The body: the rail's own scroll axis. Exactly ONE tab's content is
             mounted, and only a tab from `tabs` can be `active`. -->
        <div v-if="active" class="flex-1 min-h-0 overflow-y-auto p-4" data-testid="portal-rail-body">
          <slot
            :name="`tab-${active.id}`"
            :tab="active"
            :participants="participants"
            :signal="activeSignal"
            :group="groupByParticipant"
          >
            <!-- Room: one rail, every participant gets its row — absence is
                 visible (design pass, "Room"). -->
            <ul v-if="rows.length" class="space-y-3 mb-4" data-testid="portal-rail-rows">
              <li v-for="row in rows" :key="row.agent" class="flex items-center gap-2 min-w-0">
                <PortalAvatar :name="row.agent" :size="18" />
                <span class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 truncate">{{ row.agent }}</span>
                <!-- The `1 running` info badge with a dot / "nothing in flight"
                     in tertiary ink (design pass, "Room") — the badge primitive,
                     never a lookalike (contract: primitives first). -->
                <BaseBadge v-if="row.live" variant="info" dot class="ml-auto">{{ row.label }}</BaseBadge>
                <span v-else class="ml-auto text-xs text-gray-400">{{ row.label }}</span>
              </li>
            </ul>

            <!-- 1:1 with a turn in flight: an honest line. The live card with
                 its steps and Stop is #457's, and docks into this slot. -->
            <p
              v-if="activeSignal.live && !rows.length"
              class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-200"
              data-testid="portal-rail-live"
            >
              <span :class="dotClass(RAIL_SIGNAL_LIVE)" aria-hidden="true"></span>
              {{ liveLine }}
            </p>

            <!-- Empty state that teaches the next action (principle 16). -->
            <div v-else-if="!activeSignal.live" class="py-8 text-center" data-testid="portal-rail-empty">
              <svg class="w-7 h-7 mx-auto mb-3 text-gray-300 dark:text-gray-700" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" :d="iconPath(active.id)" /></svg>
              <p class="text-sm font-semibold">{{ empty.title }}</p>
              <p class="mt-1.5 text-xs text-gray-500 dark:text-gray-400 max-w-[36ch] mx-auto">{{ empty.body }}</p>
              <BaseButton
                v-if="empty.action"
                size="sm"
                variant="secondary"
                class="mt-4"
                @click="onEmptyAction"
              >{{ empty.action }}</BaseButton>
            </div>
          </slot>
        </div>
      </template>
    </aside>
  </div>
</template>

<script setup>
import { computed, onMounted, onBeforeUnmount } from 'vue'
import OverflowTabs from '../OverflowTabs.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import PortalAvatar from './PortalAvatar.vue'
import {
  RAIL_SIGNAL_LIVE,
  RAIL_SIGNAL_UPDATED,
  activeTabFor,
  collapsedSignals,
  groupByParticipant,
  participantState,
  railEmptyCopy,
  signalFor,
  signalShape,
} from './portalRail'

const props = defineProps({
  // The tabs THIS session may see — already filtered by `visibleTabs`. The
  // rail never widens this list; with none it renders nothing at all.
  tabs: { type: Array, default: () => [] },
  activeTab: { type: [String, null], default: null },
  open: { type: Boolean, default: false },
  // { [signalKey]: { live: n, updated: bool, agents: [] } } — derived by the
  // shell on every render from the source that owns each signal.
  signals: { type: Object, default: () => ({}) },
  participants: { type: Array, default: () => [] },
  // The mobile bottom-sheet form. A sheet is always "open".
  sheet: { type: Boolean, default: false },
})

const emit = defineEmits(['update:open', 'update:activeTab', 'close', 'see-hints'])

const mode = computed(() => (props.sheet ? 'sheet' : props.open ? 'open' : 'collapsed'))

// `activeTabFor` never answers with a tab outside `tabs`: a remembered tab
// this session may not see falls back to the first it may.
const active = computed(() => {
  const id = activeTabFor({ tab: props.activeTab }, props.tabs)
  return props.tabs.find((t) => t.id === id) || null
})

const strip = computed(() => collapsedSignals(props.signals, props.tabs))
const stripTabs = computed(() =>
  props.tabs.map((t) => ({ id: t.id, label: t.label, signal: signalShape(signalFor(props.signals, t)) }))
)
const activeSignal = computed(() => signalFor(props.signals, active.value))
const empty = computed(() => railEmptyCopy(active.value, props.participants))
const rows = computed(() =>
  props.participants.length > 1
    ? props.participants.map((agent) => ({ agent, ...participantState(agent, activeSignal.value) }))
    : []
)
const liveLine = computed(() => {
  const who = activeSignal.value.agents
  if (!who.length) return 'Work is in flight.'
  return `${who.join(', ')} ${who.length > 1 ? 'are' : 'is'} working on your message.`
})

function setOpen(open) { emit('update:open', open) }
function setTab(id) { emit('update:activeTab', id) }
function openOn(id) { setTab(id); setOpen(true) }
function onEmptyAction() {
  if (empty.value.event === 'see-hints') emit('see-hints')
}

// Esc closes the sheet (principle 23). The listener lives only for a sheet
// instance's lifetime; the column form has no overlay to dismiss.
function onKeydown(e) {
  if (e.key === 'Escape' && props.sheet) emit('close')
}
onMounted(() => { if (props.sheet) window.addEventListener('keydown', onKeydown) })
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

// ---- ink ----------------------------------------------------------------------
// Design pass values: collapsed 48px (`w-12`), open 384px (`w-96`), 36px
// icon buttons. Widths are the rail's own for now; #492 hands them to the
// shell's grid variables.
const ASIDE = {
  collapsed: 'w-12 flex flex-col border-l border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900',
  open: 'w-96 flex flex-col min-h-0 border-l border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900',
  sheet: 'absolute inset-x-0 bottom-0 max-h-[85vh] rounded-t-2xl bg-white dark:bg-gray-900 shadow-xl flex flex-col',
}
const ICON_BTN = 'w-9 h-9 rounded-lg flex items-center justify-center hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition'

// Two shapes, one hue (principle 24). Live: 8px dot in a 3px ring at 28%,
// pulsing only where motion is welcome; a static ring under
// `prefers-reduced-motion`. Updated: a plain 6px dot.
function dotClass(shape) {
  if (shape === RAIL_SIGNAL_LIVE) {
    return 'block w-2 h-2 rounded-full bg-action-primary-500 ring-[3px] ring-action-primary-500/[.28] motion-safe:animate-pulse'
  }
  if (shape === RAIL_SIGNAL_UPDATED) return 'block w-1.5 h-1.5 rounded-full bg-action-primary-500'
  return ''
}

// Heroicons outline paths already in the bundle (design pass, "Layout").
const ICONS = {
  bolt: 'M13 10V3L4 14h7v7l9-11h-7z',
  refresh: 'M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15',
  template: 'M4 5a1 1 0 011-1h14a1 1 0 011 1v2a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM4 13a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H5a1 1 0 01-1-1v-6zM16 13a1 1 0 011-1h2a1 1 0 011 1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-6z',
  paperclip: 'M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13',
}
const GLYPHS = {
  expand: 'M11 19l-7-7 7-7m8 14l-7-7 7-7',
  collapse: 'M13 5l7 7-7 7M5 5l7 7-7 7',
  close: 'M6 18L18 6M6 6l12 12',
}
function iconPath(tabId) {
  const tab = props.tabs.find((t) => t.id === tabId)
  return ICONS[tab && tab.icon] || ICONS.bolt
}
</script>
