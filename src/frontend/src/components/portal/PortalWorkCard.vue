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

    <!-- What it is doing right now: the stream's last line wins while live. -->
    <p v-if="live && liveStep" class="mt-1.5 text-xs text-gray-600 dark:text-gray-300 truncate" data-testid="portal-work-step">{{ liveStep }}</p>

    <!-- Steps: the stages, or the honest sentence. -->
    <template v-if="live">
      <ol v-if="steps.kind === 'stages'" class="mt-2 space-y-1" data-testid="portal-work-stages">
        <li v-for="s in stages" :key="s.id" class="flex items-center gap-2 text-xs min-w-0">
          <span :class="stageGlyph(s.state)" aria-hidden="true">{{ s.state === 'done' ? '✓' : s.state === 'current' ? '●' : '○' }}</span>
          <span class="truncate" :class="s.state === 'current' ? 'font-medium text-gray-800 dark:text-gray-100' : 'text-gray-500 dark:text-gray-400'">{{ s.name }}</span>
          <span v-if="holderOf(s)" class="ml-auto shrink-0 text-gray-500 dark:text-gray-400">{{ holderOf(s) }}</span>
        </li>
      </ol>
      <p v-else-if="steps.kind !== 'pending'" class="mt-1.5 text-xs text-gray-500 dark:text-gray-400" :data-testid="`portal-work-steps-${steps.kind}`">{{ steps.text }}</p>
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
      <BaseButton
        v-if="live && canStop"
        size="sm"
        variant="secondary"
        :disabled="stopping"
        :data-testid="`portal-work-stop-${item.id}`"
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
import { computed } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import {
  formatElapsed, holderLine, isHonestTerminal, isLive, kindLabel, stageRows, stepsLine,
  workStatusLabel, workTone,
} from './portalWork'

const props = defineProps({
  item: { type: Object, required: true },
  // The stream's last line (chat only, while streaming).
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
})
defineEmits(['stop', 'open-work', 'ask-about-it'])

const live = computed(() => isLive(props.item))
const statusWord = computed(() => workStatusLabel(props.item))
const kindWord = computed(() => kindLabel(props.item.kind))
const clock = computed(() => (live.value ? formatElapsed(props.elapsedSeconds) : null))
const steps = computed(() => stepsLine(props.item.steps, props.item.agent_name))
const stages = computed(() => stageRows(props.item.steps))
const askable = computed(() => !live.value && isHonestTerminal(props.item.outcome))
const hasActions = computed(() => (live.value && props.canStop) || askable.value || props.showOpenInWork)

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
    case 'ok': return 'text-status-success-600 dark:text-status-success-400'
    case 'warn': return 'text-status-warning-600 dark:text-status-warning-400'
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
  if (state === 'done') return 'text-status-success-600 dark:text-status-success-400 shrink-0'
  if (state === 'current') return 'text-action-primary-600 dark:text-action-primary-400 shrink-0'
  return 'text-gray-400 shrink-0'
}
</script>
