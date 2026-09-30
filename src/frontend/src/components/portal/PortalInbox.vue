<!--
  trinity-enterprise#610 — the Workspace Inbox: what needs you, and what came
  back, across your agents.

  Four windows onto data the shell already holds (plan D6) — no store, no feed,
  no endpoint of its own:
    Action  the ONE asks list's pending rows (`clientPortal.openAsks`)
    Unread  chats with new arrivals — the shell's `sidebarThreads`, the same
            projection the sidebar sums, so the two never disagree (D13)
    All     every chat of any age, merged with asks (ended ones for the 7 days
            the server keeps them); the list pages it (§3g D-4)
  Every rule lives in `portalInbox.js`; this file wires them to the screen.

  Honest state per tab (principle 15): a skeleton while that tab's source has
  no verdict, `LoadFailed` when its first read failed, the empty copy only after
  a read SUCCEEDED and returned nothing, and a stale banner beside data a
  refresh failed to replace. The tab and the selection live in the URL
  (`?tab=&item=`, written with `replace`), so a reload lands where you were.
-->
<template>
  <div ref="rootEl" class="flex-1 min-h-0 flex flex-col" data-testid="inbox" :data-layout="layout.mode" @keydown.esc="onEsc">
    <!-- §3g L5: over a STACKED pane the Inbox's own title, subtitle and tabs
         step aside (~130px back at 390) — the pane's Back is the way out. -->
    <header class="shrink-0 flex items-start gap-3 px-4 pt-4 pb-2" :class="overPane ? 'hidden' : ''" data-testid="inbox-header">
      <!-- §3g A5: on a phone the sidebar is a drawer, and the Inbox is the
           landing — without this it was a dead end. 44px, the touch floor. -->
      <button
        type="button"
        class="sm:hidden -ml-2 h-11 w-11 shrink-0 flex items-center justify-center rounded-md text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-750 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
        aria-label="Menu"
        data-testid="inbox-menu"
        @click="$emit('open-menu')"
      >
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" /></svg>
      </button>
      <div class="min-w-0 flex-1">
        <h1 class="text-lg font-semibold text-gray-900 dark:text-gray-100">Inbox</h1>
        <p class="text-[12.5px] text-gray-600 dark:text-gray-300 max-sm:hidden" data-testid="inbox-subtitle">What needs you, and what came back, across your agents.</p>
      </div>
      <!-- §3g A9: Unread and All only — Action's rows are asks, which a read
           does not touch — named with the number of chats it reads, and
           confirmed first when that is more than one. -->
      <BaseButton
        v-if="tab !== 'action'"
        variant="ghost"
        size="sm"
        class="max-sm:min-h-11"
        :disabled="!unreadRows.length || markingAll"
        :loading="markingAll"
        loading-label="Marking…"
        data-testid="inbox-mark-all-read"
        @click="askMarkAll"
      >{{ markAllLabel(unreadRows.length) }}</BaseButton>
      <!-- Sign-off: the shell's light/dark switch, through the SAME seam the
           chat and the room headers expose — without it the Inbox, the
           Workspace's landing, was the one stage with no theme control. -->
      <div class="shrink-0 flex items-center min-h-8"><slot name="header-end" /></div>
    </header>
    <ConfirmDialog
      v-model:visible="markAllConfirmOpen"
      variant="info"
      confirm-variant="primary"
      :title="markAllCopy.title"
      :message="markAllCopy.message"
      :confirm-text="markAllCopy.confirm"
      @confirm="markAllRead"
    />
    <p
      v-if="notification && notification.type === 'success'"
      role="status"
      class="fixed bottom-6 right-6 z-50 px-4 py-3 rounded-lg shadow-lg text-sm border bg-status-success-100 border-status-success-300 text-status-success-700 dark:bg-status-success-900/50 dark:border-status-success-700 dark:text-status-success-300"
      data-testid="inbox-toast"
    >{{ notification.message }}</p>
    <InlineError
      v-if="markAllError"
      class="mx-4 mb-2"
      :message="markAllError"
      data-testid="inbox-mark-all-error"
      @dismiss="markAllError = ''"
    />

    <div class="shrink-0 px-4 border-b border-gray-200 dark:border-gray-750" :class="overPane ? 'hidden' : ''" data-testid="inbox-tabs">
      <OverflowTabs :tabs="tabStrip" :model-value="tab" dense tablist-label="Inbox" @update:model-value="onTab" />
      <!-- §3g C2: Action narrowed to one agent — a second dense strip, only
           when two or more agents are waiting on you; the choice is `?from=`. -->
      <div v-if="facets.length" class="pb-1" data-testid="inbox-agent-facets">
        <OverflowTabs :tabs="facets" :model-value="activeFrom || FROM_ALL" dense tablist-label="Asks by agent" @update:model-value="onFrom" />
      </div>
    </div>

    <div class="flex-1 min-h-0 flex">
      <!-- The list. STACKED (the container is under 720px — §3g A4) it and
           the pane are successive states of one column (D12); SPLIT they sit
           side by side, the list 320px, or 384px from 1100. -->
      <div
        ref="listColEl"
        tabindex="-1"
        class="min-h-0 flex-col shrink-0 border-gray-200 dark:border-gray-750 focus:outline-none"
        :class="listColClass"
        data-testid="inbox-list-column"
      >
        <InlineError
          v-if="view.stale"
          class="mx-3 mt-3"
          :message="staleText"
          retryable
          data-testid="inbox-stale"
          @retry="retry"
        />
        <InlineError
          v-if="asksStale"
          class="mx-3 mt-3"
          :message="asksStaleText"
          retryable
          data-testid="inbox-asks-stale"
          @retry="store.fetchAsks()"
        />
        <!-- The list states its total on a line above the rows; that line's box is
             reserved in every other state too, so the list never jumps when
             its data lands (layout stability). Same box as PortalInboxList's. -->
        <p
          v-if="view.state !== 'ready'"
          class="shrink-0 px-4 pt-3 pb-2 text-[11px] font-semibold uppercase tracking-wide"
          aria-hidden="true"
          data-testid="inbox-list-total-reserve"
        >&nbsp;</p>
        <!-- #3060: rows at the ready row's box (PortalInboxList's), the same
             placeholder the stage skeleton draws, so neither hand-off moves. -->
        <PortalInboxSkeleton v-if="view.state === 'loading'" part="rows" data-testid="inbox-list-loading" />
        <LoadFailed
          v-else-if="view.state === 'failed'"
          dense
          :title="failedTitle"
          message="Nothing is lost — try again in a moment."
          data-testid="inbox-list-failed"
          @retry="retry"
        />
        <div v-else-if="view.state === 'empty'" class="px-4 pt-6 text-center" data-testid="inbox-empty">
          <p class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ empty.title }}</p>
          <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-300">{{ empty.body }}</p>
          <router-link
            v-if="empty.link"
            :to="empty.link.to"
            class="mt-3 inline-block text-sm text-action-primary-600 dark:text-action-primary-400 hover:underline"
            data-testid="inbox-empty-link"
          >{{ empty.link.label }}</router-link>
        </div>
        <PortalInboxList
          v-else
          ref="listEl"
          :items="paged.shown"
          :total="paged.total"
          @show-more="showMore"
          :head="head"
          :head-exact="headExact"
          :notes="footerNotes"
          :selected-key="selectedKey"
          :labels="labels"
          :label="`${tabLabel} items`"
          @open="open"
        />
      </div>

      <!-- On a phone the columns follow the RESOLVED item, not the key: a key
           that names a chat deleted elsewhere must show the list, never a
           pane-less dead end with no Back. -->
      <div class="min-w-0 flex-1 min-h-0 flex-col" :class="(stacked || noRows) && !selectedItem ? 'hidden' : 'flex'">
        <PortalInboxPane
          v-if="selectedItem"
          ref="paneEl"
          :key="selectedItem.key"
          :item="selectedItem"
          :agent-label="labels[selectedItem.agent_name] || selectedItem.agent_name || ''"
          :show-back="stacked"
          :stacked="stacked"
          :canvas-count="canvasCount"
          @open-canvas="$emit('open-canvas')"
          :read-failed="readFailedKey === selectedItem.key"
          @rendered="(k) => { renderedKey = k }"
          @mark-read="readNow(selectedItem)"
          @dismiss-read-error="readFailedKey = null"
          @back="back"
          @open-chat="(url) => url && $emit('open-chat', url)"
          @reply="(url, reply) => url && $emit('reply', url, reply)"
          @open-thread="(t) => $emit('open-chat', `/workspace/c/${t.id}`)"
        />
        <!-- #3060: while the list has no verdict there is nothing to pick yet —
             the pane's placeholder, as the stage skeleton draws it. -->
        <PortalInboxSkeleton v-else-if="view.state === 'loading' && !stacked" part="pane" />
        <p v-else-if="!noRows" class="m-auto px-6 text-center text-sm text-gray-600 dark:text-gray-300" data-testid="inbox-pane-none">
          Pick something on the left to read it here.
        </p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import BaseButton from '@/components/base/BaseButton.vue'
import OverflowTabs from '@/components/OverflowTabs.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import { useContainerWidth } from '@/composables/useContainerWidth'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import { useNotification } from '@/composables/useNotification'
import PortalInboxList from './PortalInboxList.vue'
import PortalInboxPane from './PortalInboxPane.vue'
import PortalInboxSkeleton from './PortalInboxSkeleton.vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { viewState, staleBannerMessage } from '@/utils/loadingState'
import {
  actionItems, unreadItems, allItems, inboxCounts, defaultInboxTab, normalizeInboxTab,
  stableRows, emptyVisit, isGhost, resolveItem, parseItemKey, listHeadLabel,
  markAllLabel, markAllConfirm, allFooterNotes, pageWindow, PAGE_SIZE, inboxLayout, listHeadExact,
  INBOX_TAB_SHELL, agentFacets, filterByAgent, activeAgentFilter, normalizeFrom, FROM_ALL,
} from './portalInbox'
import { askBadgeTitle, unreadBadgeTitle } from './portalUtils'
import { capCount } from '@/utils/tabTitle'

const props = defineProps({
  // The shell's `sidebarThreads` — the same projection the sidebar sums (D13).
  threads: { type: Array, default: () => [] },
  // `thread:<id>` → {latest, first_unread_message_id}, from the same response.
  previews: { type: Object, default: () => ({}) },
  // The shell's verdict on the thread list (D13): latched on the first good read.
  threadsLoaded: { type: Boolean, default: false },
  threadsFailed: { type: Boolean, default: false },
  // agent name → display label.
  labels: { type: Object, default: () => ({}) },
  isPlatform: { type: Boolean, default: false },
  // The rail's width while it is not yet a column (0 once it is): counted
  // before it arrives, so a preview that brings it in cannot flip the layout
  // (§3g A4).
  railAllowance: { type: Number, default: 0 },
  // §3g C10: the selected item's agent's canvases (the shell reads the rail's
  // feed); 0 hides Open canvas.
  canvasCount: { type: Number, default: 0 },
  // §3g S5: the shell's `markRead(kind, id)` — resolves true / false, never
  // rejects (S4). A function rather than an emit, because the pane needs its
  // verdict: a false result is `inbox-pane-read-error`.
  markRead: { type: Function, default: null },
})
const emit = defineEmits(['refresh', 'open-chat', 'reply', 'update:preview', 'open-menu', 'open-canvas'])

const store = useClientPortalStore()
const route = useRoute()
const router = useRouter()

// ---- layout (§3g A4) ------------------------------------------------------------
// The phone VIEWPORT is read SYNCHRONOUSLY in setup, not on mount: the preview
// watcher below is `immediate`, and a phone that read as desktop for that first
// run would preview (and start the rail feeds for) a row nobody tapped. Before
// the container is measured the layout falls back on it.
const phone = ref(false)
const mq = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
  ? window.matchMedia('(max-width: 639px)')
  : null
const syncPhone = () => { phone.value = !!mq?.matches }
syncPhone()
onMounted(() => mq?.addEventListener?.('change', syncPhone))
onBeforeUnmount(() => mq?.removeEventListener?.('change', syncPhone))

// Split or stacked is the CONTAINER's width (`inboxLayout`), with hysteresis:
// the previous mode is an input, so it is kept in a ref, not derived.
const rootEl = ref(null)
const containerWidth = useContainerWidth(rootEl)
const layout = ref(inboxLayout({ width: 0, phoneViewport: phone.value }))
watch([containerWidth, () => props.railAllowance, phone], ([width, allowance, phoneViewport]) => {
  const next = inboxLayout({ width, allowance, phoneViewport, prev: layout.value.mode })
  if (next.mode !== layout.value.mode || next.wide !== layout.value.wide) layout.value = next
})
const stacked = computed(() => layout.value.mode === 'stacked')
// The pane is on screen in place of the list.
const overPane = computed(() => stacked.value && !!selectedItem.value)
// Round 3: stacked, the list keeps a reading width (at 1280 with the rail open
// it ran 984px, the time ~900px from the name); and a tab with NO rows is one
// column — split, it put a 776px "Pick something on the left" beside nothing.
const noRows = computed(() => view.value.state === 'empty' || view.value.state === 'failed')
const listColClass = computed(() => {
  if (stacked.value) return selectedItem.value ? 'hidden w-full' : 'flex w-full max-w-3xl mx-auto'
  if (noRows.value && !selectedItem.value) return 'flex w-full'
  return layout.value.wide ? 'flex w-96 border-r' : 'flex w-80 border-r'
})

// ---- the rows -------------------------------------------------------------------
const counts = computed(() => inboxCounts(props.threads, store.openAsks))
const asksVerdict = computed(() => store.asksLoaded || store.asksAbsent)

// The tab the URL does not name is chosen ONCE, when both sources have a
// verdict — never re-derived, or the strip would jump as counts change.
const initialTab = ref(null)
watch([asksVerdict, () => props.threadsLoaded, () => store.asksFailed, () => props.threadsFailed], () => {
  if (initialTab.value) return
  const settled = (asksVerdict.value || store.asksFailed) && (props.threadsLoaded || props.threadsFailed)
  if (settled) initialTab.value = defaultInboxTab(counts.value)
}, { immediate: true })
const tab = computed(() => normalizeInboxTab(route.query.tab) || initialTab.value || 'action')
const tabLabel = computed(() => ({ action: 'Action', unread: 'Unread', all: 'All' }[tab.value]))

// §3g A3b: the counters are solid, in the colours the pinned row and the agent
// pills use — needs you urgent-700, new primary-700.
// §3g A8c / D-1: the counter caps at 99+ like every other, and the tab is
// NAMED with the full number in words (the badge is then aria-hidden).
// #3060: Action and Unread reserve their badge's width (`badgeSlot`, from
// INBOX_TAB_SHELL) — the counts land after the strip renders, and without the
// slot they slid Unread and All ~29px right.
const tabStrip = computed(() => [
  {
    ...INBOX_TAB_SHELL[0], badge: capCount(counts.value.needs) || null, badgeVariant: 'urgent',
    badgeLabel: counts.value.needs ? `Action, ${askBadgeTitle(counts.value.needs)}` : undefined,
  },
  {
    ...INBOX_TAB_SHELL[1], badge: capCount(counts.value.came) || null, badgeVariant: 'primary',
    badgeLabel: counts.value.came ? `Unread, ${unreadBadgeTitle(counts.value.came)}` : undefined,
  },
  { ...INBOX_TAB_SHELL[2] },
])

// §3g C2: `?from=` narrows Action to one agent. It holds while that agent has
// asks, or while its just-ended ask is the one selected (read from the asks
// list by key, so this never depends on the rows it filters).
const actionBase = computed(() => actionItems(store.openAsks))
const fromQuery = computed(() => (tab.value === 'action' ? normalizeFrom(route.query.from) : null))
const selectedAsk = computed(() => {
  const p = parseItemKey(selectedKey.value)
  if (!p || p.type !== 'ask') return null
  return store.asks.find((a) => a.id === p.id) || null
})
const activeFrom = computed(() => activeAgentFilter(fromQuery.value, actionBase.value, selectedAsk.value))
// A2 r1 (QA P2): the facets keep the order this Action visit first showed —
// an answered ask moves a count, never a chip under the reader. Not reactive
// (it only remembers what the last evaluation drew); leaving Action forgets it.
let facetOrder = null
const facets = computed(() => {
  if (tab.value !== 'action') { facetOrder = null; return [] }
  const f = agentFacets(actionBase.value, props.labels, activeFrom.value, facetOrder)
  facetOrder = f.map((t) => t.id)
  return f
})
// A2 r1 (Codex C4): narrowing to another agent closes an open ask that is not
// theirs — else the pane keeps agent A's ask beside a list of agent B's.
function onFrom(id) {
  droppedFrom.value = null
  const from = normalizeFrom(id)
  const sel = selectedAsk.value
  if (from && sel && sel.agent_name !== from) {
    previewFor.value = { tab: null, key: null }
    replaceQuery({ from, item: undefined })
    return
  }
  replaceQuery({ from: from || undefined })
}

const baseItems = computed(() => {
  if (tab.value === 'action') return filterByAgent(actionBase.value, activeFrom.value)
  if (tab.value === 'unread') return unreadItems(props.threads, props.previews)
  return allItems(props.threads, store.asks, props.previews)
})
const footerNotes = computed(() => (tab.value === 'all'
  ? allFooterNotes({ hasRooms: props.threads.some((t) => t && t.is_room), askCount: store.asks.length })
  : []))

// §3g S5 (T2): the URL holds only what the reader OPENED. The desktop
// auto-selection is a local preview, kept per tab and told to the shell
// (`update:preview`) so the rail can follow it without a `?item=`.
const routeItem = computed(() => (typeof route.query.item === 'string' ? route.query.item : null))
const previewFor = ref({ tab: null, key: null })
// A STACKED Inbox previews nothing: its pane replaces the list, and a preview
// there would open a chat nobody chose (§3g A4 — no auto-select when stacked).
const previewKey = computed(() => (!stacked.value && previewFor.value.tab === tab.value ? previewFor.value.key : null))
const selectedKey = computed(() => routeItem.value || previewKey.value)
watch(previewKey, (k) => emit('update:preview', k || null))
// §3g C2: a `?from=` that no longer narrows anything (its agent's asks are all gone and
// none is on screen) leaves the URL, once the asks have a verdict.
// Round 2 (QA mobile F5): the head says whose filter was dropped, until the
// reader picks a tab or a facet.
const droppedFrom = ref(null)
watch([fromQuery, activeFrom, asksVerdict], ([q, a, v]) => {
  if (q && !a && v) { droppedFrom.value = q; replaceQuery({ from: undefined }) }
}, { immediate: true })   // a ?from= the page OPENS with (a stale link, Back) is checked too
// Round 2 (QA P2-3): a door that narrows the Inbox to an agent ("Open in
// Inbox" in Work or Info) is a link; the page it leaves takes focus with it,
// so it would land on the body. Focus the list, where the narrowed asks are —
// only when nothing else holds focus (a facet click keeps its own).
watch(fromQuery, async (q) => {
  if (!q || typeof document === 'undefined') return
  await nextTick()
  const at = document.activeElement
  if (!at || at === document.body) listColEl.value?.focus?.({ preventScroll: true })
}, { immediate: true })
// A2 r1 (QA N1): `?from=` narrows Action only — on another tab (a deep link,
// a hand-edited URL) it is ignored, so it leaves the URL rather than lingering.
watch(() => tab.value !== 'action' && route.query.from !== undefined, (stray) => {
  if (stray) replaceQuery({ from: undefined })
}, { immediate: true })
onBeforeUnmount(() => emit('update:preview', null))

// Principle 5 (§3g S1): the rows of one TAB VISIT keep their place — a row
// that leaves stays as a ghost drawn read / ended, a poll never re-sorts, and a
// new row goes in beside its neighbour (`stableRows`). The visit is keyed by
// tab, so a tab change starts a new one by construction; clicking the active
// tab again and a completed bulk read start one explicitly. Not reactive
// itself: the computed returns the next visit, which is kept here (re-applying
// the same fresh list is a no-op, so a lazy re-evaluation is safe).
// §3g C2: the agent filter is part of the visit's key — a new `?from=` is a
// new visit, so the narrowed rows start in their own order.
let visit = { tab: null, ...emptyVisit() }
const visitEpoch = ref(0)
function newVisit() { visit = { tab: null, ...emptyVisit() }; visitEpoch.value++ }
const visitKey = computed(() => `${tab.value}|${activeFrom.value || ''}`)
const live = {
  thread: (id) => props.threads.find((t) => (t.id || t.session_id) === id) || null,
  ask: (id) => store.asks.find((a) => a.id === id) || null,
}
const shownItems = computed(() => {
  visitEpoch.value // eslint-disable-line no-unused-expressions
  const prev = visit.tab === visitKey.value ? visit : emptyVisit()
  const out = stableRows(baseItems.value, prev, live)
  visit = { tab: visitKey.value, ...out.visit }
  return out.rows
})
// §3g SM / C4: 50 rows, then "Show more" — per tab visit: the limit resets on a
// tab change, never on a poll.
const pageLimit = ref(PAGE_SIZE)
watch(tab, () => { pageLimit.value = PAGE_SIZE })
const paged = computed(() => pageWindow(shownItems.value, pageLimit.value, selectedKey.value))
// A window a selection widened (a deep link to row 72) stays that wide for the
// visit: it shrank back to 50 under the reader on the next click (round 3).
watch(() => paged.value.shown.length, (n) => { if (n > pageLimit.value) pageLimit.value = n }, { immediate: true })
// A selection the reader did not click — a deep link, a reload — is scrolled
// to once its row is drawn; it sat ~6,000px below the visible list (round 3).
// A clicked row is already where the reader is looking.
let clickedKey = null
let scrolledTo = null
watch([routeItem, () => paged.value.shown.length], async ([k]) => {
  if (!k || k === clickedKey || k === scrolledTo) return
  await nextTick()
  const row = listColEl.value
    ? [...listColEl.value.querySelectorAll('[data-inbox-row]')].find((r) => r.getAttribute('data-inbox-row') === k)
    : null
  if (!row) return
  scrolledTo = k
  row.scrollIntoView?.({ block: 'nearest' })
}, { immediate: true })
async function showMore() {
  const first = paged.value.shown.length
  pageLimit.value = first + PAGE_SIZE
  await nextTick()
  const rows = listColEl.value ? [...listColEl.value.querySelectorAll('[data-inbox-row]')] : []
  rows[first]?.focus?.()
}

// The head counts LIVE rows, never ghosts, in units (§3g A8); with only ghosts
// left it says "All caught up".
const liveItems = computed(() => shownItems.value.filter((it) => !isGhost(it)))
const labelOf = (name) => (props.labels && props.labels[name]) || name
const head = computed(() => listHeadLabel(tab.value, liveItems.value,
  activeFrom.value ? labelOf(activeFrom.value) : null,
  !activeFrom.value && droppedFrom.value ? labelOf(droppedFrom.value) : null))
const headExact = computed(() => listHeadExact(tab.value, liveItems.value))

// A selection the rendered rows do not hold (an old chat, a deep link) is
// resolved from the shell's data by key — the one fallback, never a slice.
const selectedItem = computed(() => {
  const k = selectedKey.value
  if (!k) return null
  return shownItems.value.find((it) => it.key === k)
    || resolveItem(k, { threads: props.threads, asks: store.asks, previews: props.previews })
})

// ---- per-tab honest state -------------------------------------------------------
const view = computed(() => {
  const count = shownItems.value.length
  if (tab.value === 'action') {
    return viewState({ hasLoaded: asksVerdict.value, error: store.asksFailed, count })
  }
  if (tab.value === 'unread') {
    return viewState({ hasLoaded: props.threadsLoaded, error: props.threadsFailed, count })
  }
  // A11 (§3g S3): All waits on the CHATS only. Its ask rows merge in when the
  // asks read lands (a new key goes in beside its neighbour, `stableRows`), and
  // a failed asks read is a banner above the chats — never LoadFailed over
  // chats that loaded fine.
  return viewState({ hasLoaded: props.threadsLoaded, error: props.threadsFailed, count })
})
const asksStale = computed(() => tab.value === 'all' && store.asksFailed
  && (view.value.state === 'ready' || view.value.state === 'empty'))
const asksStaleText = computed(() => (store.asksLoaded
  ? staleBannerMessage('your asks', store.asksLoadedAt)
  : "Couldn't load your asks — the chats below are current."))
// The thread list keeps no load time, so its banner says "the last data that
// loaded" rather than inventing one.
const staleText = computed(() => (tab.value === 'action'
  ? staleBannerMessage('your asks', store.asksLoadedAt)
  : staleBannerMessage('your chats', null)))
const failedTitle = computed(() => (tab.value === 'action' ? "Couldn't load your asks" : "Couldn't load your chats"))

// A9: an owner is never the addressee of their own agents' asks, so a
// platform session's empty Action points at the door where operator asks live.
const empty = computed(() => {
  if (tab.value === 'action') {
    return props.isPlatform
      ? {
          title: 'Nothing needs your answer',
          body: 'Questions and approvals your agents address to you land here. Asks for the operator live in Operations.',
          link: { to: '/operations', label: 'Open Operations' },
        }
      : {
          title: 'Nothing needs your answer',
          body: 'When an agent you work with needs a decision from you, it lands here.',
        }
  }
  if (tab.value === 'unread') {
    return { title: "You're all caught up", body: 'New replies and deliverables from your agents land here.' }
  }
  return {
    title: 'No chats or asks yet',
    body: 'Start a chat with one of your agents. Its replies and asks land here.',
    link: { to: { path: '/workspace', query: { new: '1' } }, label: 'New chat' },
  }
})

function retry() {
  if (tab.value !== 'unread') store.fetchAsks()
  if (tab.value !== 'action') emit('refresh')
}

// ---- selection ------------------------------------------------------------------
const paneEl = ref(null)
const listEl = ref(null)
const listColEl = ref(null)
let returnKey = null
let returnIndex = 0

function replaceQuery(patch) {
  const query = { ...route.query, ...patch }
  for (const k of Object.keys(query)) if (query[k] === undefined || query[k] === null) delete query[k]
  return router.replace({ path: route.path, query })
}

// ---- reading (§3g S5, D-3) -------------------------------------------------------
// A chat is read when the reader OPENED it (a click / Enter, or the initial
// `?item=` of a deep link) AND the pane has rendered it (`rendered`): history,
// deliverables and every payload on screen. Never on a preview, and never
// before the content — a failed load leaves the chat unread.
const initial = parseItemKey(routeItem.value)
const readIntent = ref(initial && initial.type === 'thread' ? routeItem.value : null)
const renderedKey = ref(null)
const readFailedKey = ref(null)
async function readNow(it) {
  if (!it || it.type !== 'thread') return
  readIntent.value = null
  readFailedKey.value = null
  const ok = props.markRead ? await props.markRead('thread', it.id) : true
  if (ok === false) readFailedKey.value = it.key
}
watch([readIntent, renderedKey], ([want, done]) => {
  if (want && want === done) readNow(selectedItem.value?.key === want ? selectedItem.value : resolveItem(want, { threads: props.threads }))
})

// An explicit open (click / Enter / Space). The row keeps its place through the
// read (`stableRows`); the read itself waits for the pane (above).
// §3g A6: STACKED, the pane is a screen of its own, so opening it PUSHES — the
// hardware / browser Back returns to the list, not out of the Workspace. Split,
// it REPLACES: Back must not step through every row the reader looked at.
let pushedKey = null
async function open(it) {
  clickedKey = it.key
  returnIndex = Math.max(0, shownItems.value.findIndex((x) => x.key === it.key))
  if (it.type === 'thread') {
    // The read waits for a render OF THIS OPEN (round 3): a verdict left over
    // from an earlier render of the same chat — the preview, a re-click, a
    // reopen after an ask — would read arrivals the pane never drew. A pane
    // already showing the chat is not remounted, so it reloads.
    renderedKey.value = null
    readIntent.value = it.key
    if (paneEl.value && selectedItem.value?.key === it.key) paneEl.value.reload?.()
  }
  let navigated
  if (stacked.value) {
    pushedKey = it.key
    navigated = router.push({ path: route.path, query: { ...route.query, tab: tab.value, item: it.key } })
  } else {
    navigated = replaceQuery({ tab: tab.value, item: it.key })
  }
  if (stacked.value) {
    // The pane mounts once the URL names the item; focus its heading then.
    returnKey = it.key
    await navigated
    await nextTick()
    paneEl.value?.focusHeading?.()
  }
}

// A click on the active tab starts a new visit: the list re-sorts and its
// ghosts go, which is the one way to ask for that without leaving the tab.
function onTab(next) {
  droppedFrom.value = null
  if (next === tab.value) { newVisit(); return }
  replaceQuery({ tab: next, item: undefined, from: undefined })
}

// Back (D12). Focus can only land once the list column is shown again: the
// navigation is awaited, then a tick, because focus() on a display:none element
// is a no-op in a real browser. The row keeps its place through a read (a
// ghost), so it is normally still there; if it is not (deleted), the row now
// in its place, else the list itself.
// §3g A6: the pane's Back pops the entry OUR open pushed — the same step the
// hardware Back takes — and otherwise (a deep link, a reload) replaces the item
// away, so it never walks out of the Inbox. Focus is restored by the watcher
// below, which the hardware Back reaches too.
async function back() {
  if (pushedKey && pushedKey === routeItem.value) {
    pushedKey = null
    router.back()
    return
  }
  await replaceQuery({ item: undefined })
}
async function focusRowAfterBack(key) {
  const index = returnIndex
  returnKey = null
  await nextTick()
  const col = listColEl.value
  if (!col) return
  const rows = [...col.querySelectorAll('[data-inbox-row]')]
  const el = rows.find((r) => r.getAttribute('data-inbox-row') === key)
    || rows[Math.min(index, rows.length - 1)]
    || col
  el.focus?.()
}
// An item leaving the URL while STACKED — the pane's Back, the hardware Back,
// or the open chat deleted under it — puts the reader back on the list, on the
// row they came from.
watch(routeItem, (now, was) => {
  if (!stacked.value || !was || now) return
  if (pushedKey === was) pushedKey = null
  focusRowAfterBack(returnKey || was)
})
function onEsc() { if (stacked.value && selectedKey.value) back() }
// §3g A4: a layout flip keeps an OPENED item and moves focus to where it now
// is — split → stacked shows its pane (focus the heading), stacked → split puts
// its row beside it (focus the row). Only when focus was inside the Inbox, so a
// resize never steals it from elsewhere (the rail being dragged, the composer).
watch(stacked, async (now, was) => {
  if (now === was || !routeItem.value) return
  const active = typeof document !== 'undefined' ? document.activeElement : null
  const inside = !active || active === document.body || !!rootEl.value?.contains(active)
  await nextTick()
  if (!inside) return
  if (now) { paneEl.value?.focusHeading?.(); return }
  const rows = listColEl.value ? [...listColEl.value.querySelectorAll('[data-inbox-row]')] : []
  rows.find((r) => r.getAttribute('data-inbox-row') === routeItem.value)?.focus?.()
})
// On a phone the pane IS the screen: if the open chat is deleted elsewhere the
// pane has nothing to draw, so it goes Back for the reader — the list, focus on
// the row now in its place — rather than leaving them on an empty column.
watch(selectedItem, (it, prev) => {
  if (stacked.value && prev && !it && selectedKey.value) back()
})

// D10: on desktop the tab's first row is previewed, so the rail column does not
// pop in on the first click. Never on phone (a landing there costs no feed),
// never in the URL, and never as a READ — a preview is not the person opening
// the chat (§3g S5).
watch([() => view.value.state, () => shownItems.value.length, routeItem, previewKey, tab, stacked], () => {
  if (stacked.value || routeItem.value) return
  if (view.value.state !== 'ready' || !shownItems.value.length) return
  if (previewKey.value && shownItems.value.some((it) => it.key === previewKey.value)) return
  previewFor.value = { tab: tab.value, key: shownItems.value[0].key }
}, { immediate: true })

// ---- Mark all read (D11, §3g A9) ------------------------------------------------
const markingAll = ref(false)
const markAllError = ref('')
const markAllConfirmOpen = ref(false)
const { notification, showNotification } = useNotification()
const unreadRows = computed(() => unreadItems(props.threads, props.previews))
const markAllCopy = computed(() => markAllConfirm({
  chats: unreadRows.value.length,
  messages: unreadRows.value.reduce((sum, r) => sum + (Number(r.n) || 0), 0),
}))
function askMarkAll() {
  if (unreadRows.value.length > 1) markAllConfirmOpen.value = true
  else markAllRead()
}
async function markAllRead() {
  const rows = unreadRows.value
  if (!rows.length || markingAll.value) return
  const done = markAllCopy.value.done
  markingAll.value = true
  markAllError.value = ''
  try {
    // Through the shell's `markRead` (S4): each write rolls its own zero back
    // on failure and resolves false — never a silent success.
    const read = props.markRead || (async () => true)
    const results = await Promise.allSettled(rows.map((r) => read('thread', r.id)))
    const failed = results.filter((r) => r.status === 'rejected' || r.value === false).length
    if (failed) {
      markAllError.value = `${failed} of ${rows.length} ${rows.length === 1 ? 'chat' : 'chats'} couldn't be marked read. They keep their count — try again.`
    } else {
      showNotification(done)
      // A completed bulk read starts a new visit: the rows it read leave
      // rather than lingering as ghosts ("You're all caught up").
      await nextTick()
      newVisit()
    }
  } finally {
    markingAll.value = false
    emit('refresh')
  }
}
</script>
