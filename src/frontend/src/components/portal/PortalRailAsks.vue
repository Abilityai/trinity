<!--
  The rail's Asks tab (trinity-enterprise#836): the agent's open asks, one line
  each, and a click opens that ask in the Inbox.

  A door, not a second answer surface. Since the 2026-09-30 ruling on ent#610
  an agent's asks live in the Inbox filtered to it, and a background ask shows
  ONLY there — so from the agent's page there was nothing to click. This tab
  lists what is waiting and leads to it; answering happens in the Inbox.

  Reads the ONE asks feed (`clientPortal.openAsks`) through the Inbox's own
  builder (`portalRailAsks.js` → `actionItems`), so the rows are exactly the
  Action tab narrowed to the participants — same items, same order — and the
  head is the waiting line every other door carries (`PortalAsksWaitingLine`:
  "2 asks waiting on you · Open in Inbox"). Rows are real links (keyboard
  reachable, announced by kind), in a room grouped by participant with absence
  visible (the rail's "Room" rule).

  ent#892: the tab is always present now (no presence rule), so the body owns
  every state — a skeleton before the asks feed has a verdict, LoadFailed for
  a failed first read, and with nothing waiting the registry's empty copy
  naming the agent plus "Open the Inbox" (the agent's asks home).
-->
<template>
  <div data-testid="portal-rail-asks">
    <PortalSkeleton v-if="view.state === 'loading'" variant="rail" />

    <LoadFailed
      v-else-if="view.state === 'failed'"
      title="Couldn't load asks"
      message="The asks waiting on you could not be read."
      @retry="portal.fetchAsks()"
    />

    <template v-else>
    <!-- A failed REFRESH keeps what loaded and says so (#1926, ent#253) — the
         rail's other feeds do the same (Canvas, Files, Loops). -->
    <InlineError
      v-if="view.stale && !staleDismissed"
      class="mb-2"
      :message="staleText"
      retryable
      data-testid="portal-rail-asks-stale"
      @retry="portal.fetchAsks()"
      @dismiss="staleDismissed = true"
    />

    <!-- Empty state that teaches (principle 16): the agent's asks home. -->
    <div v-if="view.state === 'empty'" class="py-8 text-center" data-testid="portal-rail-asks-empty">
      <p class="text-sm font-semibold">{{ empty.title }}</p>
      <p class="mt-1.5 text-xs text-gray-500 dark:text-gray-400 max-w-[36ch] mx-auto">{{ empty.body }}</p>
      <BaseButton size="sm" variant="secondary" class="mt-4" data-testid="portal-rail-asks-open-inbox" @click="openInbox">
        {{ empty.action }}
      </BaseButton>
    </div>

    <template v-else>
    <PortalAsksWaitingLine :agent-names="participants" testid-prefix="portal-rail-asks" class="mb-2" />

    <template v-for="[agent, rows] in groups" :key="agent">
      <!-- Room: one rail, every participant gets its row — absence is visible. -->
      <div v-if="grouped" class="mt-3 mb-1.5 flex items-center gap-2 min-w-0" :data-testid="`portal-rail-asks-group-${agent}`">
        <PortalAvatar :name="agent" :size="18" />
        <span class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 truncate">{{ labelFor(agent) }}</span>
        <span v-if="!rows.length" class="ml-auto text-xs text-gray-500 dark:text-gray-400">nothing waiting</span>
      </div>

      <ul v-if="rows.length" class="space-y-1" :aria-label="grouped ? `Asks from ${labelFor(agent)}` : 'Asks waiting on you'">
        <li v-for="it in rows" :key="it.key">
          <router-link
            :to="askInboxRoute(it)"
            class="w-full flex items-center gap-2.5 rounded-lg px-2.5 py-2 min-w-0 transition hover:bg-gray-50 dark:hover:bg-gray-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40"
            :title="it.title"
            :data-testid="`portal-rail-ask-${it.id}`"
            @click="$emit('open', it)"
          >
            <!-- Identity by SHAPE (principle 24): the kind's own outline, in
                 gray, as the Inbox row draws it (§3g A10); the kind is spoken. -->
            <svg class="w-4 h-4 shrink-0 text-gray-500 dark:text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" :d="askKindIcon(it.ask && it.ask.kind).path" /></svg>
            <span class="sr-only">{{ kindLabel(it) }}:</span>
            <span class="min-w-0 flex-1 text-sm truncate text-gray-900 dark:text-gray-100">{{ it.title }}</span>
            <!-- Principle 22: relative for recency, absolute + zone on hover. -->
            <span class="shrink-0 text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400" :title="absolute(it.at)">{{ relative(it.at) }}</span>
          </router-link>
        </li>
      </ul>
    </template>
    </template>
    </template>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useClientPortalStore } from '@/stores/clientPortal'
import BaseButton from '@/components/base/BaseButton.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import PortalAvatar from './PortalAvatar.vue'
import PortalSkeleton from './PortalSkeleton.vue'
import PortalAsksWaitingLine from './PortalAsksWaitingLine.vue'
import { railAskItems, askInboxRoute, railAsksView, ASKS_TAB_ID } from './portalRailAsks'
import { RAIL_TABS, railEmptyCopy } from './portalRail'
import { asksHomeRoute, relativeTime } from './portalUtils'
import { agentLabels } from './portalInbox'
import { askKindIcon } from './portalAskUrgency'
import { queueTypeLabel } from '@/utils/operatorQueue'
import { formatLocalDateTime } from '@/utils/timestamps'
import { staleBannerMessage } from '@/utils/loadingState'

const props = defineProps({
  participants: { type: Array, default: () => [] },
})
const emit = defineEmits(['open'])

const portal = useClientPortalStore()
const router = useRouter()

const items = computed(() => railAskItems(portal.openAsks, props.participants))
const view = computed(() => railAsksView({
  participants: props.participants,
  asksLoaded: portal.asksLoaded,
  asksAbsent: portal.asksAbsent,
  asksFailed: portal.asksFailed,
  count: items.value.length,
}))
// The stale banner: dismissible, back on the next failed refresh; a read that
// succeeds clears it by itself (`asksFailed` drops, so `view.stale` does).
const staleDismissed = ref(false)
watch(() => portal.asksFailed, (failed) => { if (failed) staleDismissed.value = false })
const staleText = computed(() => staleBannerMessage('your asks', portal.asksLoadedAt))
const empty = computed(() => railEmptyCopy(RAIL_TABS.find((t) => t.id === ASKS_TAB_ID), props.participants))
// A 1:1 opens the Inbox filtered to its agent; a room, the whole Action list.
function openInbox() {
  const one = props.participants.length === 1 ? props.participants[0] : null
  router.push(asksHomeRoute(one))
  emit('open', null)
}
const grouped = computed(() => props.participants.length > 1)
// A 1:1 is one unlabelled group; a room is one group per participant, in
// participant order, each present even with nothing in it.
const groups = computed(() => {
  if (!grouped.value) return [[props.participants[0] || '', items.value]]
  return props.participants.map((agent) => [agent, items.value.filter((it) => it.agent_name === agent)])
})

const labels = computed(() => agentLabels(portal.agents))
const labelFor = (name) => labels.value[name] || name
const kindLabel = (it) => queueTypeLabel(it.ask && it.ask.kind) || 'Ask'
const relative = (iso) => relativeTime(iso)

let zone = ''
try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || '' } catch { zone = '' }
const absolute = (iso) => (iso ? `${formatLocalDateTime(iso)}${zone ? ` (${zone})` : ''}` : undefined)
</script>
