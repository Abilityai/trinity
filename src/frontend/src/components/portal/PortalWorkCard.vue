<!--
  The live execution card (trinity-enterprise#525, the visual half of ent#457).

  ONE component for both homes — under the message that started the job in the
  chat, and in the rail's Work tab — so a job reads the same way wherever a
  person meets it: the status word, how long it has been going, what it is
  doing right now, the steps of a pipeline with the agent holding each one,
  and the controls the platform can honestly offer:

    Stop            the existing terminate route (ent#155) — only where it
                    would be accepted (`can_stop`, mirrored server-side)
    Open in Work    the rail, on this tab
    Ask about it    a PREFILL of the composer, never a send — the ruled lesser
                    control for a job that failed, timed out, was stopped or
                    was lost sight of; a step-level restart is a platform
                    capability Trinity does not have (#919)

  Three sentences for steps, never two (ruling 2, reviewed): the stages when
  the agent publishes them, "doesn't report steps" when it reachably does not,
  "could not be read right now" when nobody can tell — a stopped agent must
  never be described as one that doesn't report.

  The chat's card reserves its steps row and Stop's slot from first paint
  (`reserveLiveRows`, #2964), so the feed's read never grows it mid-turn.

  Presentational: every decision is `portalWork.js`. Two clocks meet here and
  one rule settles them — while the stream is live its last line is the
  current step; the stages list and its holder come from the feed.
-->
<template>
  <div
    class="rounded-xl border bg-white dark:bg-gray-800 px-3.5 py-3 text-sm"
    :class="[live ? 'border-action-primary-500/40' : 'border-gray-200 dark:border-gray-700', compact ? '' : 'max-w-[85%]']"
    :data-testid="`portal-work-card-${item.id}`"
    :data-outcome="item.outcome"
    :data-live="live ? 'true' : 'false'"
  >
    <!-- Status row: shape + weight + word, never hue alone (principle 24). -->
    <div class="flex items-center gap-2 min-w-0">
      <span v-if="live" :class="LIVE_DOT" aria-hidden="true"></span>
      <span v-else :class="[TERMINAL_DOT, toneDot]" aria-hidden="true"></span>
      <span class="font-medium" :class="toneText">{{ statusWord }}</span>
      <span v-if="clock" class="text-xs text-gray-500 dark:text-gray-400 tabular-nums" data-testid="portal-work-elapsed">{{ clock }}</span>
      <BaseBadge variant="neutral" class="ml-auto shrink-0">{{ kindWord }}</BaseBadge>
    </div>

    <p class="mt-1.5 text-gray-800 dark:text-gray-100 line-clamp-2 break-words" data-testid="portal-work-title">{{ item.title }}</p>
    <p v-if="showAgent && item.agent_name" class="mt-0.5 text-xs text-gray-500 dark:text-gray-400 truncate">{{ item.agent_name }}</p>
    <p v-else-if="showAgent && !item.agent_name" class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">another agent</p>

    <!-- What it is doing right now (trinity-enterprise#620): ONE row of fixed
         height, reserved for the whole live life of the card, so a line
         arriving or changing never grows the card or shifts what is below.
         A new line slides up over the previous one (a swap under
         reduced motion); the row is keyed on the line's queue key, so an
         identical line never re-animates. Empty = no live signal — the card
         says nothing rather than something invented (AC #3). -->
    <div v-if="live" class="mt-1.5 relative h-4 overflow-hidden" data-testid="portal-work-activity" aria-live="polite">
      <Transition name="portal-activity">
        <p
          v-if="shownStep"
          :key="shownStep.key"
          class="absolute inset-x-0 top-0 h-4 leading-4 text-xs text-gray-600 dark:text-gray-300 truncate"
          data-testid="portal-work-step"
        >{{ shownStep.text }}</p>
      </Transition>
    </div>

    <!-- Steps: the stages, or the honest sentence. -->
    <template v-if="live">
      <ol v-if="steps.kind === 'stages'" class="mt-2 space-y-1" data-testid="portal-work-stages">
        <li v-for="s in stages" :key="s.id" class="flex items-center gap-2 text-xs min-w-0">
          <span :class="stageGlyph(s.state)" aria-hidden="true">{{ s.state === 'done' ? '✓' : s.state === 'current' ? '●' : '○' }}</span>
          <span class="truncate" :class="s.state === 'current' ? 'font-medium text-gray-800 dark:text-gray-100' : 'text-gray-500 dark:text-gray-400'">{{ s.name }}</span>
          <span v-if="holderOf(s)" class="ml-auto shrink-0 text-gray-500 dark:text-gray-400">{{ holderOf(s) }}</span>
        </li>
      </ol>
      <!-- #2964: in the chat's card (`reserveLiveRows`) the row exists from
           first paint — blank and aria-hidden while the feed has not read the
           turn — and holds ONE line, so `pending → none | unknown` swaps the
           text in place. Only the agent's name truncates; the claim
           ("doesn't report steps.") is never cut (ent#525's "says so"). -->
      <p
        v-else-if="steps.kind !== 'pending' || reserveLiveRows"
        class="mt-1.5 text-xs text-gray-500 dark:text-gray-400"
        :class="reserveLiveRows ? 'h-4 leading-4 flex min-w-0 whitespace-nowrap' : ''"
        :aria-hidden="steps.kind === 'pending' ? 'true' : undefined"
        :title="reserveLiveRows && steps.text ? steps.text : undefined"
        :data-testid="steps.kind === 'pending' ? 'portal-work-reserved-steps' : `portal-work-steps-${steps.kind}`"
      >
        <template v-if="reserveLiveRows && steps.who">
          <span class="truncate" data-testid="portal-work-sentence-who">{{ steps.who }}</span>
          <span class="shrink-0 whitespace-pre" data-testid="portal-work-sentence-claim">{{ steps.text.slice(steps.who.length) }}</span>
        </template>
        <span v-else-if="reserveLiveRows" class="truncate">{{ steps.text }}</span>
        <template v-else>{{ steps.text }}</template>
      </p>
    </template>

    <!-- Delegated work this turn handed on: who holds it now. -->
    <ul v-if="live && children.length" class="mt-2 space-y-1" data-testid="portal-work-children">
      <li v-for="c in children" :key="c.id" class="flex items-center gap-2 text-xs min-w-0">
        <span :class="LIVE_DOT_SM" aria-hidden="true"></span>
        <span class="text-gray-500 dark:text-gray-400 shrink-0">{{ c.agent_name ? `held by ${c.agent_name}` : 'held by another agent' }}</span>
        <span class="truncate text-gray-700 dark:text-gray-200">{{ c.title }}</span>
      </li>
    </ul>

    <!-- A terminal that is not success: the honest line, then the lesser control. -->
    <p v-if="!live && item.error" class="mt-1.5 text-xs text-status-danger-700 dark:text-status-danger-300 break-words" data-testid="portal-work-error">{{ item.error }}</p>
    <p v-if="!live && item.outcome === 'lost'" class="mt-1.5 text-xs text-gray-500 dark:text-gray-400">Nothing is watching this any more — it may still finish, but it can't be followed from here.</p>

    <div v-if="hasActions" class="mt-2.5 flex flex-wrap items-center gap-2">
      <!-- #2964: the chat's card holds Stop's slot from first paint — invisible,
           disabled, aria-hidden and out of the tab order until the 202's id
           makes a stop possible (ent#155), then the SAME button turns usable,
           so Open in Work never moves. Other hosts render Stop only when it can be used. -->
      <BaseButton
        v-if="live && (canStop || reserveLiveRows)"
        size="sm"
        variant="secondary"
        :disabled="stopping || !canStop"
        :class="canStop ? '' : 'invisible'"
        :aria-hidden="canStop ? undefined : 'true'"
        :tabindex="canStop ? undefined : -1"
        :data-testid="canStop ? `portal-work-stop-${item.id}` : 'portal-work-stop-reserved'"
        @click="$emit('stop', item)"
      >{{ stopping ? 'Stopping…' : 'Stop' }}</BaseButton>
      <BaseButton
        v-if="askable"
        size="sm"
        variant="secondary"
        :data-testid="`portal-work-ask-${item.id}`"
        @click="$emit('ask-about-it', item)"
      >Ask about it</BaseButton>
      <BaseButton
        v-if="showOpenInWork"
        size="sm"
        variant="ghost"
        :data-testid="`portal-work-open-${item.id}`"
        @click="$emit('open-work', item)"
      >Open in Work</BaseButton>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import { createActivityLineQueue } from '@/utils/workActivity'
import BaseButton from '@/components/base/BaseButton.vue'
import {
  formatElapsed, holderLine, isHonestTerminal, isLive, kindLabel, stageRows, stepsLine,
  workStatusLabel, workTone,
} from './portalWork'

const props = defineProps({
  item: { type: Object, required: true },
  // The activity line's TEXT (#620) — composed by the host through
  // `resolveActivityText` from the stream (own turn) or the Work read
  // (every other run). The card owns the motion: minimum display time,
  // no re-animation on an identical line, the row's fixed height.
  liveStep: { type: String, default: null },
  // Seconds elapsed, driven by the host's clock — see `liveElapsedSeconds`.
  elapsedSeconds: { type: Number, default: null },
  // In-flight items bound to this chat other than the item itself.
  children: { type: Array, default: () => [] },
  // Whether Stop may be offered: the host's own gate (the chat's
  // `canCancelTurn`, the tab's `item.can_stop`).
  canStop: { type: Boolean, default: false },
  stopping: { type: Boolean, default: false },
  showOpenInWork: { type: Boolean, default: false },
  // The rail's denser form; the chat's card sits in the bubble column.
  compact: { type: Boolean, default: false },
  // A room / the tab names the agent; a 1:1 already has its avatar.
  showAgent: { type: Boolean, default: false },
  // #2964: the chat's live card only. Reserves two rows from first paint so
  // the card keeps one shape while the turn runs: the steps sentence
  // (`pending → none | unknown` swaps text in place) and Stop's slot (no id
  // → id). It does NOT cover `stages ↔ unknown` when a second in-flight row
  // appears, nor a feed row going stale mid-turn — those are other jumps,
  // not a reason to widen this flag. Every other host keeps the default.
  reserveLiveRows: { type: Boolean, default: false },
})
defineEmits(['stop', 'open-work', 'ask-about-it'])

const live = computed(() => isLive(props.item))

// ---- the activity row's motion (#620) ------------------------------------------
// A burst of tool calls becomes a calm sequence: each line holds for
// ACTIVITY_MIN_DISPLAY_MS, only the latest pending line is kept, and the
// queue's key changes only when the TEXT changes. `shownStep` is what the
// row renders; a 250 ms tick promotes a pending line once the minimum passed.
const queue = createActivityLineQueue()
const shownStep = ref(null)
let _tick = null
function _sync(next) { shownStep.value = next ? { text: next.text, key: next.key } : null }
watch(() => (live.value ? props.liveStep : null), (text) => { _sync(queue.offer(text ?? null)) }, { immediate: true })
watch(live, (isLive) => {
  if (isLive) {
    if (!_tick) _tick = setInterval(() => { _sync(queue.tick()) }, 250)
  } else {
    if (_tick) { clearInterval(_tick); _tick = null }
    queue.clear(); _sync(null)   // the line is gone at terminal (AC #8)
  }
}, { immediate: true })
onBeforeUnmount(() => { if (_tick) clearInterval(_tick) })
const statusWord = computed(() => workStatusLabel(props.item))
const kindWord = computed(() => kindLabel(props.item.kind))
const clock = computed(() => (live.value ? formatElapsed(props.elapsedSeconds) : null))
const steps = computed(() => stepsLine(props.item.steps, props.item.agent_name))
const stages = computed(() => stageRows(props.item.steps))
const askable = computed(() => !live.value && isHonestTerminal(props.item.outcome))
const hasActions = computed(() => (live.value && (props.canStop || props.reserveLiveRows)) || askable.value || props.showOpenInWork)

// A stage's holder is the executing agent unless the definition names one;
// the server masks an off-roster name to null, so null here means "another
// agent" — never silently the agent you are talking to.
function holderOf(stage) {
  return holderLine(stage.holder, { agentName: props.item.agent_name, masked: stage.holder === null })
}

// ---- ink ----------------------------------------------------------------------
const LIVE_DOT = 'block w-2 h-2 rounded-full bg-action-primary-500 ring-[3px] ring-action-primary-500/[.28] motion-safe:animate-pulse shrink-0'
const LIVE_DOT_SM = 'block w-1.5 h-1.5 rounded-full bg-action-primary-500 shrink-0'
const TERMINAL_DOT = 'block w-2 h-2 rounded-full shrink-0'

const toneText = computed(() => {
  switch (workTone(props.item)) {
    case 'active': return 'text-action-primary-600 dark:text-action-primary-400'
    case 'ok': return 'text-status-success-700 dark:text-status-success-400'
    case 'warn': return 'text-status-warning-700 dark:text-status-warning-400'
    case 'danger': return 'text-status-danger-600 dark:text-status-danger-400'
    default: return 'text-gray-600 dark:text-gray-300'
  }
})
const toneDot = computed(() => {
  switch (workTone(props.item)) {
    case 'ok': return 'bg-status-success-500'
    case 'warn': return 'bg-status-warning-500'
    case 'danger': return 'bg-status-danger-500'
    default: return 'bg-gray-400'
  }
})
function stageGlyph(state) {
  if (state === 'done') return 'text-status-success-700 dark:text-status-success-400 shrink-0'
  if (state === 'current') return 'text-action-primary-600 dark:text-action-primary-400 shrink-0'
  return 'text-gray-400 shrink-0'
}
</script>

<style scoped>
/* trinity-enterprise#620: the activity line slides up into its fixed row. Both
   the leaving and the entering line are absolutely positioned inside the
   `h-4 overflow-hidden` row, so nothing outside it moves. */
.portal-activity-enter-active,
.portal-activity-leave-active {
  transition: transform 180ms ease-out, opacity 180ms ease-out;
}
.portal-activity-enter-from {
  transform: translateY(100%);
  opacity: 0;
}
.portal-activity-leave-to {
  transform: translateY(-100%);
  opacity: 0;
}
@media (prefers-reduced-motion: reduce) {
  .portal-activity-enter-active,
  .portal-activity-leave-active {
    transition: none;
  }
}
</style>
