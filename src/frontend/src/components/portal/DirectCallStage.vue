<template>
  <!-- One fixed-height row, so nothing below it moves as the call changes state: the bars
       carry the state, a short word names it, one line says what was heard. -->
  <section
    class="shrink-0 h-14 flex items-center gap-3 px-3 sm:px-6 border-b border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900"
    aria-label="Direct call"
    data-testid="direct-call-stage"
  >
    <CallBars :state="barsState" :amplitude="call.amplitude.value || 0" :muted="call.muted.value" :size="32" />
    <div class="min-w-0 flex-1 flex items-baseline gap-2">
      <span class="shrink-0 w-20 text-xs font-medium text-gray-700 dark:text-gray-200" role="status" aria-live="polite" data-testid="direct-call-state">{{ stateText }}</span>
      <span class="min-w-0 truncate text-xs text-gray-500 dark:text-gray-400" data-testid="direct-call-heard">{{ detail }}</span>
    </div>
    <!-- ent#1087: stops what the agent is saying and the turn behind it; the call stays
         on. Always here, disabled with its reason when there is nothing to stop, so the
         row never changes shape. -->
    <button
      type="button"
      class="shrink-0 w-11 h-11 rounded-full flex items-center justify-center transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500 text-gray-700 hover:bg-gray-200 dark:text-gray-200 dark:hover:bg-gray-800 disabled:opacity-40 disabled:hover:bg-transparent disabled:cursor-not-allowed"
      :disabled="!call.canStop.value"
      :aria-label="call.canStop.value ? 'Stop' : 'Nothing to stop'"
      :title="call.canStop.value ? 'Stop (Esc while it speaks)' : 'Nothing to stop'"
      data-testid="direct-call-stop"
      @click="call.interrupt()"
    >
      <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
        <rect x="7" y="7" width="10" height="10" rx="1.5" stroke-width="2" />
      </svg>
    </button>
    <button
      type="button"
      class="shrink-0 w-11 h-11 rounded-full flex items-center justify-center transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500"
      :class="call.muted.value
        ? 'bg-gray-200 text-gray-900 dark:bg-gray-750 dark:text-gray-100'
        : 'text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800'"
      :aria-label="call.muted.value ? 'Unmute' : 'Mute'"
      :title="call.muted.value ? 'Unmute' : 'Mute'"
      :aria-pressed="call.muted.value"
      data-testid="direct-call-mute"
      @click="call.toggleMute()"
    >
      <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
        <path stroke-linecap="round" stroke-linejoin="round" d="M12 18.75a6 6 0 006-6v-1.5m-6 7.5a6 6 0 01-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 01-3-3V4.5a3 3 0 116 0v8.25a3 3 0 01-3 3z" />
        <path v-if="call.muted.value" stroke-linecap="round" d="M4 4l16 16" />
      </svg>
    </button>
    <button
      type="button"
      class="shrink-0 w-11 h-11 rounded-full flex items-center justify-center text-white bg-status-danger-600 hover:bg-status-danger-700 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-status-danger-400 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-900"
      aria-label="End call"
      title="End call (Esc when it is quiet)"
      data-testid="direct-call-end"
      @click="$emit('end')"
    >
      <!-- The handset, turned down: hang up. -->
      <svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path transform="rotate(135 12 12)" d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z" />
      </svg>
    </button>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import CallBars from './CallBars.vue'
import { directBarsState, directDetail, directStateText } from './portalDirectCall'

const props = defineProps({ call: { type: Object, required: true } })
defineEmits(['end'])

const stateText = computed(() => directStateText({ status: props.call.status.value, muted: props.call.muted.value }))
const detail = computed(() => directDetail({
  notice: props.call.notice.value,
  heard: props.call.liveText.value,
  status: props.call.status.value,
  tool: props.call.tool.value,
  runtime: props.call.runtime.value,
  earlySpeech: props.call.earlySpeech.value,
}))
const barsState = computed(() => directBarsState(props.call.status.value))
</script>
