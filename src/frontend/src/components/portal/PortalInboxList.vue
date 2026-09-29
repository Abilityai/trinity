<!--
  trinity-enterprise#610 — one Inbox tab's rows.

  A bounded list (principle 28): its own scroll, the total stated above it
  ("70 · latest 50 shown"), and rows that are real `<button>`s in visual order,
  so Tab walks them and Enter / Space opens one (principle 23). Presentational:
  the rows arrive built (`portalInbox.js`), selection is the parent's, and the
  only thing this emits is "open this one".

  Two honest states a row can be drawn in without leaving the list (principle 5,
  D8/D11): a chat that was just READ keeps its place with no count, and an ask
  that ENDED while selected keeps its place drawn ended — both until the
  selection moves on.
-->
<template>
  <div class="flex flex-col min-h-0 h-full">
    <p
      class="shrink-0 px-4 pt-3 pb-2 text-[11px] font-semibold uppercase tracking-wide tabular-nums text-gray-500 dark:text-gray-400"
      data-testid="inbox-list-total"
    >{{ head }}</p>
    <ul class="flex-1 min-h-0 overflow-y-auto px-2 pb-2 space-y-1" :aria-label="label" data-testid="inbox-list">
      <li v-for="it in items" :key="it.key">
        <button
          type="button"
          class="w-full text-left rounded-lg px-3 py-2.5 flex items-start gap-3 transition focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40"
          :class="it.key === selectedKey
            ? 'bg-gray-100 dark:bg-gray-750'
            : 'hover:bg-gray-50 dark:hover:bg-gray-800'"
          :aria-current="it.key === selectedKey ? 'true' : undefined"
          :data-inbox-row="it.key"
          :data-testid="`inbox-row-${it.key}`"
          @click="$emit('open', it)"
        >
          <!-- Identity by SHAPE (principle 24): each ask kind its own outline
               (shield-check / question-mark-circle / bell), a speech bubble for
               a chat — in gray, so an ended ask is not orange and the kind is
               never carried by hue (§3g A10). The kind is spoken. -->
          <template v-if="it.type === 'ask'">
            <svg
              class="w-4 h-4 mt-0.5 shrink-0 text-gray-500 dark:text-gray-400"
              fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"
            ><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" :d="askKindIcon(it.ask && it.ask.kind).path" /></svg>
            <span class="sr-only">{{ kindLabel(it) }}</span>
          </template>
          <svg
            v-else
            class="w-4 h-4 mt-0.5 shrink-0"
            :class="meta(it)"
            fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"
          ><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" /></svg>

          <span class="min-w-0 flex-1">
            <span class="flex items-center gap-2 min-w-0">
              <span class="text-sm font-medium truncate text-gray-900 dark:text-gray-100">{{ labelFor(it.agent_name) }}</span>
              <span v-if="it.type === 'thread'" class="text-xs truncate" :class="meta(it)">· {{ threadTitle(it) }}</span>
              <span
                class="ml-auto shrink-0 text-xs tabular-nums"
                :class="meta(it)"
                :title="absolute(it.at)"
              >{{ relative(it.at) }}</span>
            </span>

            <span v-if="it.type === 'ask'" class="mt-0.5 block text-sm truncate" :class="secondary(it)">{{ it.title }}</span>
            <span v-else-if="it.latest && it.latest.excerpt" class="mt-0.5 block text-sm truncate" :class="secondary(it)">{{ it.latest.excerpt }}</span>

            <span class="mt-1 flex items-center gap-1.5 flex-wrap">
              <BaseBadge v-if="it.type === 'thread' && it.n > 0" variant="primary" class="tabular-nums" :data-testid="`inbox-row-new-${it.key}`">{{ newLabel(it.n) }}</BaseBadge>
              <BaseBadge v-if="it.type === 'thread' && it.readInPlace" variant="neutral" :data-testid="`inbox-row-read-${it.key}`">Read</BaseBadge>
              <!-- `failed` carries an icon AND a word — never hue alone. -->
              <BaseBadge v-if="outcomeOf(it) === 'failed'" variant="danger" :data-testid="`inbox-row-failed-${it.key}`">
                <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 18L18 6M6 6l12 12" /></svg>
                Didn't finish
              </BaseBadge>
              <BaseBadge v-else-if="outcomeOf(it) === 'done'" variant="success">
                <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" /></svg>
                Finished
              </BaseBadge>
              <BaseBadge v-if="it.latest && it.latest.kind === 'deliverable'" variant="purple">Deliverable</BaseBadge>
              <!-- §3g A10: what DIFFERS between pending asks — a high or critical
                   priority, and an expiry within the day — at most two badges.
                   "Waiting on you" was true of every row, so it said nothing. -->
              <template v-if="it.type === 'ask' && it.status === 'pending'">
                <BaseBadge
                  v-if="priorityBadge(it.ask && it.ask.priority)"
                  :variant="priorityBadge(it.ask.priority).variant"
                  data-testid="inbox-row-badge-priority"
                >{{ priorityBadge(it.ask.priority).label }}</BaseBadge>
                <BaseBadge
                  v-if="expiry(it)"
                  :variant="expiry(it).variant"
                  class="tabular-nums"
                  :title="absolute(it.ask.expires_at)"
                  data-testid="inbox-row-badge-expiry"
                >{{ expiry(it).label }}</BaseBadge>
              </template>
              <BaseBadge
                v-else-if="it.type === 'ask'"
                :variant="it.status === 'expired' ? 'warning' : 'neutral'"
                :data-testid="`inbox-row-ended-${it.key}`"
              >{{ askStatusLabel(it) }}</BaseBadge>
            </span>
          </span>
        </button>
      </li>
    </ul>
    <div
      v-if="notes.length || total > items.length"
      class="shrink-0 px-4 pt-2 pb-3 border-t border-gray-200 dark:border-gray-750 space-y-0.5 text-[12.5px] text-gray-600 dark:text-gray-300"
      data-testid="inbox-list-footer"
    >
      <p v-if="total > items.length" class="flex items-center gap-3" data-testid="inbox-list-page">
        <span class="tabular-nums">Showing {{ items.length }} of {{ total }}</span>
        <BaseButton variant="secondary" size="sm" data-testid="inbox-show-more" @click="$emit('show-more')">Show more</BaseButton>
      </p>
      <p v-for="n in notes" :key="n">{{ n }}</p>
    </div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import { relativeTime } from './portalUtils'
import { newLabel } from './portalInbox'
import { priorityBadge, expiresSoonLabel, needsExpiryTick, askKindIcon } from './portalAskUrgency'
import { queueTypeLabel } from '@/utils/operatorQueue'
import { formatLocalDateTime } from '@/utils/timestamps'

const props = defineProps({
  items: { type: Array, default: () => [] },
  // The line above the rows — the container's, because it counts LIVE rows
  // (a ghost is not a member of the tab) and knows the tab's full total.
  head: { type: String, default: '' },
  // Lines under the rows that say what the tab does NOT hold, in tertiary ink
  // (§3g D-4b) — the ended-ask window, the asks cap, rooms.
  notes: { type: Array, default: () => [] },
  // §3g SM / C4: the rows in the tab (the window is `items`); the paging line
  // shows only while some are not rendered.
  total: { type: Number, default: 0 },
  selectedKey: { type: String, default: null },
  // agent name → the human-facing label (the roster's display_label).
  labels: { type: Object, default: () => ({}) },
  label: { type: String, default: 'Inbox items' },
})
defineEmits(['open', 'show-more'])

const labelFor = (name) => props.labels[name] || name || ''
const relative = (iso) => relativeTime(iso)

// Principle 22: relative for recency, absolute + timezone on hover.
let zone = ''
try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || '' } catch { zone = '' }
const absolute = (iso) => (iso ? `${formatLocalDateTime(iso)}${zone ? ` (${zone})` : ''}` : undefined)

const threadTitle = (it) => (it.is_main ? 'Main' : (it.title || 'Chat')) + (it.archived ? ' · archived' : '')
const outcomeOf = (it) => (it.type === 'thread' ? it.latest?.outcome || null : null)

// Ink by ground (#2201): tertiary ink is for surface, not chrome — a selected
// row sits on a chrome fill, so its meta steps up one tier. Mutually exclusive
// arms, never two utilities of one property in one string (#2662).
const meta = (it) => (it.key === props.selectedKey
  ? 'text-gray-600 dark:text-gray-300'
  : 'text-gray-500 dark:text-gray-400')
const secondary = (it) => (it.key === props.selectedKey
  ? 'text-gray-700 dark:text-gray-200'
  : 'text-gray-600 dark:text-gray-300')

// §3g A10: the expiry badge counts down on a 30 s clock that runs ONLY while a
// pending row has an expiry within the day.
const now = ref(Date.now())
let tick = null
const stopTick = () => { if (tick) { clearInterval(tick); tick = null } }
watch(() => needsExpiryTick(props.items.filter((it) => it.type === 'ask').map((it) => it.ask || {}), now.value), (on) => {
  if (on && !tick) tick = setInterval(() => { now.value = Date.now() }, 30_000)
  else if (!on) stopTick()
}, { immediate: true })
onBeforeUnmount(stopTick)
const expiry = (it) => expiresSoonLabel(it.ask && it.ask.expires_at, now.value)
const kindLabel = (it) => queueTypeLabel(it.ask && it.ask.kind) || 'Ask'

function askStatusLabel(it) {
  const s = it.status
  if (s === 'answered') return it.ask?.ended_by === 'you' ? 'Answered · you' : 'Answered'
  if (s === 'expired') return 'Expired'
  if (s === 'cancelled') return 'Cancelled'
  return s || ''
}
</script>
