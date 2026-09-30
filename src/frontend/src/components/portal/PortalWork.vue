<!--
  The rail's Work tab body (trinity-enterprise#525 — ent#457's Activity, homed
  by the 2026-09-02 ruling as the first docked tab of the ent#474 rail).

  Two sections, in the order a person needs them:

    Now              a live card per in-flight job — Stop where the platform
                     would accept it, steps where the agent publishes them
    Earlier          bounded history: "N in the last 30 days · latest 3
                     shown", Show all expands inside the rail's own scroll
                     axis (principle 28); loop runs are one kind here, not a
                     second surface (ent#458 AC 3)

  Asks are not drawn here (trinity-enterprise#610, the 2026-09-30 ruling —
  round 5's "Work is the asks' home" is reversed): their home is the Inbox,
  filtered to the agent, on every door. While any of this chat's agents waits on
  you, ONE line says so and links there — on the Now heading's row, which is
  always drawn, so the line arriving moves nothing (principle 30); in the empty
  state, under the empty copy. It counts from the one store list the sidebar
  reads (`portal.openAsks`), never a narrowed fetch.

  A room groups everything by participating agent, and an agent with nothing
  in flight still gets its row (the rail's own rule, `groupByParticipant`).

  Reads `stores/portalWork.js`; never fetches. The shell owns the feed
  (`composables/usePortalRailFeeds.js`), so the collapsed rail signals "2
  running" with this body unmounted. Platform door only — the rail never
  renders this tab for a client, and the `visible` gate is belt-and-braces
  under that. Loading is a skeleton keyed on the feed's VERDICT (#2540), a
  failed first fetch is `LoadFailed`, a failed refresh keeps the rows under an
  `InlineError` (#1926).
-->
<template>
  <div v-if="visible" class="space-y-5" data-testid="portal-work">
    <PortalSkeleton v-if="view.state === 'loading'" variant="rail" />

    <LoadFailed
      v-else-if="view.state === 'failed'"
      title="Couldn't load what's running"
      :message="store.error || 'The work for this chat could not be read.'"
      :retrying="store.loading"
      @retry="store.refresh()"
    />

    <template v-else>
      <InlineError v-if="view.stale" :message="store.error" @dismiss="store.error = null" />
      <InlineError v-if="stopError" :message="stopError" @dismiss="stopError = ''" />

      <!-- Empty: teaches the next action (principle 16). -->
      <div v-if="view.state === 'empty'" class="py-8 text-center" data-testid="portal-work-empty">
        <p class="text-sm font-semibold">{{ emptyCopy.title }}</p>
        <p class="mt-1.5 text-xs text-gray-500 dark:text-gray-400 max-w-[36ch] mx-auto">{{ emptyCopy.body }}</p>
        <BaseButton v-if="emptyCopy.action" size="sm" variant="secondary" class="mt-4" @click="$emit('see-hints')">{{ emptyCopy.action }}</BaseButton>
        <p v-if="waiting" class="mt-4 text-xs" data-testid="portal-work-waiting-line">
          {{ waiting.label }} ·
          <router-link :to="waiting.to" :class="INBOX_LINK" data-testid="portal-work-open-inbox">Open in Inbox</router-link>
        </p>
      </div>

      <template v-else>
        <!-- Now -->
        <section data-testid="portal-work-now">
          <div class="flex items-baseline justify-between gap-3 mb-2" data-testid="portal-work-now-head">
            <h3 :class="OVERLINE_ROW">Now</h3>
            <p v-if="waiting" class="min-w-0 truncate text-xs" data-testid="portal-work-waiting-line">
              {{ waiting.label }} ·
              <router-link :to="waiting.to" :class="INBOX_LINK" data-testid="portal-work-open-inbox">Open in Inbox</router-link>
            </p>
          </div>
          <template v-if="participants.length > 1">
            <div v-for="[agent, list] in groupedNow" :key="agent" class="mb-3 last:mb-0">
              <div class="flex items-center gap-2 min-w-0 mb-1.5">
                <PortalAvatar :name="agent" :size="18" />
                <span class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 truncate">{{ agent }}</span>
                <BaseBadge v-if="list.length" variant="info" dot class="ml-auto">{{ list.length }} running</BaseBadge>
                <span v-else class="ml-auto text-xs text-gray-400">nothing in flight</span>
              </div>
              <div class="space-y-2">
                <PortalWorkCard
                  v-for="it in list"
                  :key="it.id"
                  :item="it"
                  compact
                  :elapsed-seconds="elapsedOf(it)"
                  :live-step="stepOf(it)"
                  :children="childrenOf(it)"
                  :can-stop="it.can_stop"
                  :stopping="store.stoppingIds.includes(it.id)"
                  @stop="onStop"
                />
              </div>
            </div>
          </template>
          <template v-else>
            <p v-if="!store.live.length" class="text-xs text-gray-500 dark:text-gray-400" data-testid="portal-work-now-empty">Nothing running right now.</p>
            <div v-else class="space-y-2">
              <PortalWorkCard
                v-for="it in store.live"
                :key="it.id"
                :item="it"
                compact
                :show-agent="!it.agent_name"
                :elapsed-seconds="elapsedOf(it)"
                :live-step="stepOf(it)"
                :children="childrenOf(it)"
                :can-stop="it.can_stop"
                :stopping="store.stoppingIds.includes(it.id)"
                @stop="onStop"
              />
            </div>
          </template>
        </section>

        <!-- Earlier -->
        <section data-testid="portal-work-earlier">
          <h3 :class="OVERLINE">Earlier</h3>
          <p class="text-xs text-gray-500 dark:text-gray-400 tabular-nums" data-testid="portal-work-earlier-summary">{{ summary }}</p>
          <template v-if="participants.length > 1">
            <div v-for="[agent, list] in groupedEarlier" :key="agent" class="mt-2">
              <div v-if="list.length" class="flex items-center gap-2 min-w-0 mb-1.5">
                <PortalAvatar :name="agent" :size="18" />
                <span class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 truncate">{{ agent }}</span>
              </div>
              <div class="space-y-2">
                <PortalWorkCard v-for="it in list" :key="it.id" :item="it" compact @ask-about-it="onAsk" />
              </div>
            </div>
          </template>
          <div v-else class="mt-2 space-y-2">
            <PortalWorkCard v-for="it in shownEarlier" :key="it.id" :item="it" compact :show-agent="!it.agent_name" @ask-about-it="onAsk" />
          </div>
          <BaseButton
            v-if="store.earlier.length > EARLIER_PREVIEW"
            size="sm"
            variant="ghost"
            class="mt-2"
            data-testid="portal-work-show-all"
            @click="expanded = !expanded"
          >{{ expanded ? 'Show fewer' : `Show all ${store.earlier.length}` }}</BaseButton>
        </section>
      </template>
    </template>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { usePortalWorkStore } from '@/stores/portalWork'
import { useClientPortalStore } from '@/stores/clientPortal'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import PortalAvatar from './PortalAvatar.vue'
import PortalSkeleton from './PortalSkeleton.vue'
import PortalWorkCard from './PortalWorkCard.vue'
import { groupByParticipant, railEmptyCopy } from './portalRail'
import { asksHomeRoute, asksWaitingLabel } from './portalUtils'
import {
  EARLIER_PREVIEW, askAboutItPrefill, childrenForChat, earlierSlice, earlierSummary,
  liveElapsedSeconds, workView,
} from './portalWork'
import { resolveActivityText } from '@/utils/workActivity'

const props = defineProps({
  participants: { type: Array, default: () => [] },
  tab: { type: Object, default: null },
  // The open thread in a 1:1 — scopes the children.
  chatId: { type: String, default: null },
})
const emit = defineEmits(['open-thread', 'see-hints', 'ask-about-it'])

const store = usePortalWorkStore()
const portal = useClientPortalStore()

const participants = computed(() => props.participants)
const visible = computed(() => portal.isPlatformSession && participants.value.length > 0)

const view = computed(() => workView({
  participants: participants.value,
  hasLoaded: store.hasLoaded,
  error: store.error,
  count: store.now.length + store.earlier.length,
}))

// ent#428: the same rows the sidebar counts, narrowed to this chat's agents.
// trinity-enterprise#610 (the 09-30 ruling): counted, never drawn — one line to
// the Inbox, narrowed to the agent when only one of them is waiting on you.
const waiting = computed(() => {
  const names = new Set(participants.value)
  const asks = (portal.openAsks || []).filter((a) => names.has(a.agent_name))
  if (!asks.length) return null
  const agents = new Set(asks.map((a) => a.agent_name))
  return {
    label: asksWaitingLabel(asks.length),
    to: asksHomeRoute(agents.size === 1 ? asks[0].agent_name : null),
  }
})

const expanded = ref(false)
const shownEarlier = computed(() => earlierSlice(store.earlier, expanded.value))
const summary = computed(() => earlierSummary({
  total: store.earlierTotal,
  shown: shownEarlier.value.length,
  windowDays: store.windowDays,
  pageLimit: store.earlierLimit,
}))
const groupedNow = computed(() => groupByParticipant(store.live, participants.value))
const groupedEarlier = computed(() => groupByParticipant(shownEarlier.value, participants.value))

const EMPTY_FALLBACK = {
  title: 'Nothing running right now',
  body: 'When an agent takes on a longer job from this chat, it shows up here step by step.',
  action: 'See what you can ask',
}
const emptyCopy = computed(() => railEmptyCopy(props.tab || { empty: EMPTY_FALLBACK }, participants.value))

// One 1 s tick for every live clock in the body, only while something is live.
const nowMs = ref(Date.now())
let tick = null
watch(() => store.hasLive, (on) => {
  if (on && !tick) tick = setInterval(() => { nowMs.value = Date.now() }, 1000)
  if (!on && tick) { clearInterval(tick); tick = null }
}, { immediate: true })
onBeforeUnmount(() => { if (tick) clearInterval(tick) })

function elapsedOf(item) {
  return liveElapsedSeconds(item, { fetchedAtMs: store.fetchedAt, nowMs: nowMs.value })
}
// trinity-enterprise#620: the same line, same vocabulary as the in-chat card —
// delegated, scheduled and room runs included. `nowMs` is read so the age
// check re-runs on the tab's own tick; a line the agent stopped renewing
// drops out rather than sticking.
function stepOf(item) {
  return resolveActivityText({ live: true, activity: store.activityFor(item), nowMs: nowMs.value })
}
function childrenOf(item) {
  return childrenForChat(store.now, item.chat_id, item.id)
}

const stopError = ref('')
async function onStop(item) {
  stopError.value = ''
  const res = await store.stopItem(item)
  if (!res.success) stopError.value = "Couldn't stop that job — it may still be running."
}

// Ask about it: a prefill the shell hands to the composer. Never sent here.
function onAsk(item) {
  emit('ask-about-it', askAboutItPrefill(item, { participants: participants.value }))
}

const OVERLINE = 'text-[11px] font-semibold uppercase tracking-wide text-gray-400 mb-2'
// The Now heading shares its row with the waiting line; the row owns the gap.
const OVERLINE_ROW = OVERLINE.replace(' mb-2', '')
// The Inbox link: a 44px target on a phone and the design-system ring (A2 round 1's rule).
const INBOX_LINK = 'inline-flex items-center max-sm:min-h-11 text-action-primary-600 dark:text-action-primary-400 hover:underline rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40'
</script>
