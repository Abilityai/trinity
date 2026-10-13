<!--
  #3060 — the Inbox's skeleton, in the ready Inbox's footprint.

  The Workspace's first load (the roster and the stage resolving) used to draw
  the CONVERSATION skeleton on the Inbox route too — header, thread, composer —
  so the Inbox then landed on a different frame. This is the Inbox's own:

    shell  the whole Inbox frame, drawn by Portal.vue while the stage has no
           verdict on the Inbox route: PortalInbox's header, the SAME tab strip
           (OverflowTabs over INBOX_TAB_SHELL, badge slots reserved), the list
           column at the width PortalInbox picks (`inboxLayout` over the same
           container width and rail allowance), and the pane block beside it.
    rows   the list's rows at PortalInboxList's row box (px-2 / space-y-1,
           rows px-3 py-2.5: icon, the agent line, the excerpt line, the badge
           line) — used by the shell AND by PortalInbox's own loading arm.
    pane   PortalInboxPane's header strip and body blocks.

  Class strings of the shared frame are PortalInbox's own, and
  `portalInboxSkeleton.mount.spec.js` mounts both and compares them, so the two
  cannot drift apart silently.

  The recipe is PortalSkeleton's (#2540): pulse blocks in the chrome fills,
  `animate-pulse motion-reduce:animate-none`, `aria-busy`, one `sr-only` line.
  What is interactive here is live — the phone Menu button and the tabs (a tab
  pick is written to `?tab=`, which the Inbox reads when it lands) — so nothing
  renders as a control that does not work (principle 23).
-->
<template>
  <!-- ================================ SHELL ================================ -->
  <div
    v-if="part === 'shell'"
    ref="rootEl"
    class="flex-1 min-h-0 flex flex-col"
    aria-busy="true"
    data-testid="inbox-skeleton"
    :data-layout="layout.mode"
  >
    <header class="shrink-0 flex items-start gap-3 px-4 pt-4 pb-2" data-testid="inbox-skeleton-header">
      <button
        type="button"
        class="sm:hidden -ml-2 h-11 w-11 shrink-0 flex items-center justify-center rounded-md text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-750 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
        aria-label="Menu"
        data-testid="inbox-skeleton-menu"
        @click="$emit('open-menu')"
      >
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" /></svg>
      </button>
      <div class="min-w-0 flex-1">
        <h1 class="text-lg font-semibold text-gray-900 dark:text-gray-100">Inbox</h1>
        <p class="text-[12.5px] text-gray-600 dark:text-gray-300 max-sm:hidden">What needs you, and what came back, across your agents.</p>
      </div>
      <BaseButton v-if="tab !== 'action'" variant="ghost" size="sm" class="max-sm:min-h-11" disabled>{{ markAllLabel(0) }}</BaseButton>
      <div class="shrink-0 flex items-center min-h-8"><slot name="header-end" /></div>
    </header>

    <div class="shrink-0 px-4 border-b border-gray-200 dark:border-gray-750" data-testid="inbox-skeleton-tabs">
      <OverflowTabs :tabs="INBOX_TAB_SHELL" :model-value="tab" dense tablist-label="Inbox" @update:model-value="onTab" />
    </div>

    <div class="flex-1 min-h-0 flex">
      <div class="min-h-0 flex-col shrink-0 border-gray-200 dark:border-gray-750 focus:outline-none" :class="listColClass" data-testid="inbox-skeleton-list">
        <p class="shrink-0 px-4 pt-3 pb-2 text-[11px] font-semibold uppercase tracking-wide" aria-hidden="true">&nbsp;</p>
        <PortalInboxSkeleton part="rows" :announce="false" />
      </div>
      <div v-if="!stacked" class="min-w-0 flex-1 min-h-0 flex flex-col">
        <PortalInboxSkeleton part="pane" />
      </div>
    </div>
    <span class="sr-only">Loading your inbox…</span>
  </div>

  <!-- ================================= ROWS ================================ -->
  <div
    v-else-if="part === 'rows'"
    class="px-2 pb-2 space-y-1"
    :aria-busy="announce ? 'true' : undefined"
    data-testid="inbox-skeleton-rows"
  >
    <div v-for="(w, i) in ROW_WIDTHS" :key="i" class="rounded-lg px-3 py-2.5 flex items-start gap-3" data-testid="inbox-skeleton-row">
      <div :class="[BLOCK_STRONG, 'w-4 h-4 mt-0.5 shrink-0 rounded']"></div>
      <div class="min-w-0 flex-1">
        <div class="h-5 flex items-center gap-2">
          <div :class="[BLOCK_STRONG, 'h-3 w-24 rounded']"></div>
          <div :class="[BLOCK, 'ml-auto h-3 w-10 rounded']"></div>
        </div>
        <div class="mt-0.5 h-5 flex items-center"><div :class="[BLOCK, 'h-3 rounded', w]"></div></div>
        <div class="mt-1 h-5 flex items-center"><div :class="[BLOCK, 'h-4 w-16 rounded-full']"></div></div>
      </div>
    </div>
    <span v-if="announce" class="sr-only">Loading your inbox…</span>
  </div>

  <!-- ================================= PANE ================================ -->
  <div v-else-if="part === 'pane'" class="flex flex-col min-h-0 h-full" aria-hidden="true" data-testid="inbox-skeleton-pane">
    <div class="shrink-0 flex items-center gap-2 px-4 py-3 border-b border-gray-200 dark:border-gray-750 min-h-[3.25rem]">
      <div :class="[BLOCK_STRONG, 'h-3 w-40 rounded']"></div>
    </div>
    <div class="flex-1 min-h-0 overflow-hidden px-4 py-4">
      <div class="max-w-[var(--ws-message-max,64rem)] mx-auto space-y-3">
        <div v-for="i in 3" :key="i" :class="[BLOCK, 'h-12 rounded-lg']"></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import BaseButton from '@/components/base/BaseButton.vue'
import OverflowTabs from '@/components/OverflowTabs.vue'
import { useContainerWidth } from '@/composables/useContainerWidth'
import { INBOX_TAB_SHELL, inboxLayout, markAllLabel, normalizeInboxTab } from './portalInbox'

const props = defineProps({
  // 'shell' | 'rows' | 'pane'
  part: { type: String, default: 'shell' },
  // The shell's rail allowance — the prop PortalInbox takes, so both pick the
  // same split / stacked layout from the same width.
  railAllowance: { type: Number, default: 0 },
  // `rows` nested in the shell stays silent: the shell carries the one line.
  announce: { type: Boolean, default: true },
})
defineEmits(['open-menu'])

const PULSE = 'animate-pulse motion-reduce:animate-none'
const BLOCK = `${PULSE} bg-gray-100 dark:bg-gray-800/60`
const BLOCK_STRONG = `${PULSE} bg-gray-200 dark:bg-gray-800`
// Varied excerpt widths, so the placeholder reads as a list, not a stack.
const ROW_WIDTHS = ['w-3/4', 'w-2/3', 'w-4/5', 'w-1/2']

// ---- shell only ----------------------------------------------------------------
// The route is read only by the shell; `rows` / `pane` are mounted inside the
// Inbox and in unit specs without a router.
const route = props.part === 'shell' ? useRoute() : null
const router = props.part === 'shell' ? useRouter() : null
// PortalInbox's own default before its verdict: the URL's tab, else Action.
const tab = computed(() => normalizeInboxTab(route?.query?.tab) || 'action')
function onTab(id) {
  if (!router || !route) return
  router.replace({ path: route.path, query: { ...route.query, tab: id } })
}

// The same layout rule PortalInbox runs (§3g A4), over the same inputs: this
// container's width, the rail allowance, and the phone viewport.
const phone = ref(false)
const mq = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
  ? window.matchMedia('(max-width: 639px)')
  : null
const syncPhone = () => { phone.value = !!mq?.matches }
syncPhone()
onMounted(() => mq?.addEventListener?.('change', syncPhone))
onBeforeUnmount(() => mq?.removeEventListener?.('change', syncPhone))
const rootEl = ref(null)
const containerWidth = useContainerWidth(rootEl)
const layout = ref(inboxLayout({ width: 0, phoneViewport: phone.value }))
watch([containerWidth, () => props.railAllowance, phone], ([width, allowance, phoneViewport]) => {
  const next = inboxLayout({ width, allowance, phoneViewport, prev: layout.value.mode })
  if (next.mode !== layout.value.mode || next.wide !== layout.value.wide) layout.value = next
})
const stacked = computed(() => layout.value.mode === 'stacked')
// PortalInbox's `listColClass` for a list with no verdict and no selection.
const listColClass = computed(() => {
  if (stacked.value) return 'flex w-full max-w-3xl mx-auto'
  return layout.value.wide ? 'flex w-96 border-r' : 'flex w-80 border-r'
})
</script>
