<!--
  trinity-enterprise#610 — the Workspace Inbox: what needs you, and what came
  back, across your agents.

  Four windows onto data the shell already holds (plan D6) — no store, no feed,
  no endpoint of its own:
    Action  the ONE asks list's pending rows (`clientPortal.openAsks`)
    Unread  chats with new arrivals — the shell's `sidebarThreads`, the same
            projection the sidebar sums, so the two never disagree (D13)
    All     chats active in the last 30 days, merged with asks (ended ones for
            the 7 days the server keeps them), bounded to 50 with the total said
  Every rule lives in `portalInbox.js`; this file wires them to the screen.

  Honest state per tab (principle 15): a skeleton while that tab's source has
  no verdict, `LoadFailed` when its first read failed, the empty copy only after
  a read SUCCEEDED and returned nothing, and a stale banner beside data a
  refresh failed to replace. The tab and the selection live in the URL
  (`?tab=&item=`, written with `replace`), so a reload lands where you were.
-->
<template>
  <div class="flex-1 min-h-0 flex flex-col" data-testid="inbox" @keydown.esc="onEsc">
    <header class="shrink-0 flex items-start gap-3 px-4 pt-4 pb-2">
      <div class="min-w-0 flex-1">
        <h1 class="text-lg font-semibold text-gray-900 dark:text-gray-100">Inbox</h1>
        <p class="text-[12.5px] text-gray-600 dark:text-gray-300">What needs you, and what came back, across your agents.</p>
      </div>
      <BaseButton
        variant="ghost"
        size="sm"
        :disabled="!counts.came || markingAll"
        :loading="markingAll"
        loading-label="Marking…"
        data-testid="inbox-mark-all-read"
        @click="markAllRead"
      >Mark all read</BaseButton>
    </header>
    <InlineError
      v-if="markAllError"
      class="mx-4 mb-2"
      :message="markAllError"
      data-testid="inbox-mark-all-error"
      @dismiss="markAllError = ''"
    />

    <div class="shrink-0 px-4 border-b border-gray-200 dark:border-gray-750">
      <OverflowTabs :tabs="tabStrip" :model-value="tab" dense @update:model-value="onTab" />
    </div>

    <div class="flex-1 min-h-0 flex">
      <!-- The list. Below `sm` it and the pane are successive states of one
           column (D12); from `sm` up they sit side by side. -->
      <div
        ref="listColEl"
        tabindex="-1"
        class="min-h-0 flex-col w-full sm:w-80 lg:w-96 shrink-0 sm:border-r border-gray-200 dark:border-gray-750 focus:outline-none"
        :class="phone && selectedKey ? 'hidden sm:flex' : 'flex'"
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
        <!-- The list states its total on a line above the rows; that line's box is
             reserved in every other state too, so the list never jumps when
             its data lands (layout stability). Same box as PortalInboxList's. -->
        <p
          v-if="view.state !== 'ready'"
          class="shrink-0 px-4 pt-3 pb-2 text-[11px] font-semibold uppercase tracking-wide"
          aria-hidden="true"
          data-testid="inbox-list-total-reserve"
        >&nbsp;</p>
        <div v-if="view.state === 'loading'" class="px-3 space-y-2" aria-busy="true" data-testid="inbox-list-loading">
          <div v-for="i in 4" :key="i" class="animate-pulse motion-reduce:animate-none h-14 rounded-lg bg-gray-100 dark:bg-gray-800/60"></div>
          <span class="sr-only">Loading your inbox…</span>
        </div>
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
          :items="shownItems"
          :total="total"
          :selected-key="selectedKey"
          :labels="labels"
          :label="`${tabLabel} items`"
          @open="(it) => open(it)"
        />
      </div>

      <div class="min-w-0 flex-1 min-h-0 flex-col" :class="phone && !selectedKey ? 'hidden sm:flex' : 'flex'">
        <PortalInboxPane
          v-if="selectedItem"
          ref="paneEl"
          :key="selectedItem.key"
          :item="selectedItem"
          :agent-label="labels[selectedItem.agent_name] || selectedItem.agent_name || ''"
          :show-back="phone"
          @back="back"
          @open-chat="(url) => url && $emit('open-chat', url)"
          @reply="(url) => url && $emit('reply', url)"
          @open-thread="(t) => $emit('open-chat', `/workspace/c/${t.id}`)"
        />
        <p v-else class="m-auto px-6 text-center text-sm text-gray-600 dark:text-gray-300" data-testid="inbox-pane-none">
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
import PortalInboxList from './PortalInboxList.vue'
import PortalInboxPane from './PortalInboxPane.vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { viewState, staleBannerMessage } from '@/utils/loadingState'
import {
  actionItems, unreadItems, allItems, inboxCounts, defaultInboxTab, normalizeInboxTab,
  holdSelected,
} from './portalInbox'

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
})
const emit = defineEmits(['mark-read', 'refresh', 'open-chat', 'reply'])

const store = useClientPortalStore()
const route = useRoute()
const router = useRouter()

// ---- phone (below `sm`) --------------------------------------------------------
// Read SYNCHRONOUSLY in setup, not on mount: the auto-select watcher below is
// `immediate`, and a phone that read as desktop for that first run would select
// (and start the rail feeds for) a row nobody tapped.
const phone = ref(false)
const mq = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
  ? window.matchMedia('(max-width: 639px)')
  : null
const syncPhone = () => { phone.value = !!mq?.matches }
syncPhone()
onMounted(() => mq?.addEventListener?.('change', syncPhone))
onBeforeUnmount(() => mq?.removeEventListener?.('change', syncPhone))

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

const tabStrip = computed(() => [
  { id: 'action', label: 'Action', badge: counts.value.needs || null },
  { id: 'unread', label: 'Unread', badge: counts.value.came || null },
  { id: 'all', label: 'All' },
])

const all = computed(() => allItems(props.threads, store.asks, props.previews))
const baseItems = computed(() => {
  if (tab.value === 'action') return actionItems(store.openAsks)
  if (tab.value === 'unread') return unreadItems(props.threads, props.previews)
  return all.value.items
})

const selectedKey = computed(() => (typeof route.query.item === 'string' ? route.query.item : null))
// Principle 5: the row you opened stays put, drawn as it is now, until the
// selection moves on.
const held = ref(null)
const live = {
  thread: (id) => props.threads.find((t) => (t.id || t.session_id) === id) || null,
  ask: (id) => store.asks.find((a) => a.id === id) || null,
}
const shownItems = computed(() => holdSelected(baseItems.value, held.value, selectedKey.value, live))
// A selection restored from the URL (reload, deep link, Back in history) is held
// exactly as a clicked one is, so answering it keeps its row in place (D8).
watch([selectedKey, baseItems], ([k, items]) => {
  if (!k || held.value?.item?.key === k) return
  const index = items.findIndex((it) => it.key === k)
  if (index >= 0) held.value = { item: items[index], index }
}, { immediate: true })
const total = computed(() => (tab.value === 'all' ? Math.max(all.value.total, shownItems.value.length) : shownItems.value.length))

const selectedItem = computed(() => {
  const k = selectedKey.value
  if (!k) return null
  return shownItems.value.find((it) => it.key === k)
    || (held.value?.item?.key === k ? held.value.item : null)
    || all.value.items.find((it) => it.key === k)
    || unreadItems(props.threads, props.previews).find((it) => it.key === k)
    || null
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
  return viewState({
    hasLoaded: props.threadsLoaded && asksVerdict.value,
    error: props.threadsFailed || store.asksFailed,
    count,
  })
})
// The thread list keeps no load time, so its banner says "the last data that
// loaded" rather than inventing one.
const staleText = computed(() => (tab.value === 'action'
  ? staleBannerMessage('your asks', store.asksLoadedAt)
  : staleBannerMessage(tab.value === 'unread' ? 'your chats' : 'your inbox', null)))
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
  return { title: 'Nothing in the last 30 days', body: 'Start a chat with one of your agents from the sidebar.' }
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

function replaceQuery(patch) {
  const query = { ...route.query, ...patch }
  for (const k of Object.keys(query)) if (query[k] === undefined || query[k] === null) delete query[k]
  return router.replace({ path: route.path, query })
}

// An explicit open (click / Enter / Space). Opening a chat reads it: the shell's
// `markRead`, once, AFTER the row's snapshot (with its first unread message id)
// is held, so the pane can still show what was new.
async function open(it, { explicit = true } = {}) {
  held.value = { item: it, index: Math.max(0, shownItems.value.findIndex((x) => x.key === it.key)) }
  const navigated = replaceQuery({ tab: tab.value, item: it.key })
  if (!explicit) return
  if (it.type === 'thread') emit('mark-read', 'thread', it.id)
  if (phone.value) {
    // The pane mounts once the URL names the item; focus its heading then.
    returnKey = it.key
    await navigated
    await nextTick()
    paneEl.value?.focusHeading?.()
  }
}

function onTab(next) {
  if (next === tab.value) return
  held.value = null
  replaceQuery({ tab: next, item: undefined })
}

// Back (D12). Focus can only land once the list column is shown again: the
// navigation is awaited, then a tick, because focus() on a display:none element
// is a no-op in a real browser. The row may have left the tab (a chat just read
// off Unread) — then the row now in its place, else the list itself.
async function back() {
  const key = returnKey || held.value?.item?.key || selectedKey.value
  const index = held.value?.index ?? 0
  returnKey = null
  held.value = null
  await replaceQuery({ item: undefined })
  await nextTick()
  const col = listColEl.value
  if (!col) return
  const rows = [...col.querySelectorAll('[data-inbox-row]')]
  const el = rows.find((r) => r.getAttribute('data-inbox-row') === key)
    || rows[Math.min(index, rows.length - 1)]
    || col
  el.focus?.()
}
function onEsc() { if (phone.value && selectedKey.value) back() }

// D10: on desktop the tab's first row is selected, so the rail column does not
// pop in on the first click. Never on phone (a landing there costs no feed),
// and never as a READ — an auto-selection is not the person opening the chat.
watch([() => view.value.state, () => shownItems.value.length, selectedKey, tab, phone], () => {
  if (phone.value || selectedKey.value) return
  if (view.value.state !== 'ready' || !shownItems.value.length) return
  open(shownItems.value[0], { explicit: false })
}, { immediate: true })

// ---- Mark all read (D11) --------------------------------------------------------
const markingAll = ref(false)
const markAllError = ref('')
async function markAllRead() {
  const rows = unreadItems(props.threads, props.previews)
  if (!rows.length || markingAll.value) return
  markingAll.value = true
  markAllError.value = ''
  try {
    // `markChatReadStrict` rethrows — the fire-and-forget `markChatRead` would
    // make every rejection a silent success here.
    const results = await Promise.allSettled(rows.map((r) => store.markChatReadStrict('thread', r.id)))
    const failed = results.filter((r) => r.status === 'rejected').length
    if (failed) {
      markAllError.value = `${failed} of ${rows.length} ${rows.length === 1 ? 'chat' : 'chats'} couldn't be marked read. They keep their count — try again.`
    }
  } finally {
    markingAll.value = false
    emit('refresh')
  }
}
</script>
