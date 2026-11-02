<template>
  <section
    class="shrink-0 flex items-center gap-3 px-3 sm:px-6 py-3 border-b border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900"
    aria-label="Direct call"
    data-testid="direct-call-stage"
  >
    <div
      class="shrink-0 w-11 h-11 rounded-full bg-action-primary-600 dark:bg-action-primary-500 transition-transform duration-75 motion-reduce:transition-none"
      :class="call.status.value === 'thinking' ? 'motion-safe:animate-pulse' : ''"
      :style="{ transform: `scale(${orbScale})` }"
      aria-hidden="true"
    ></div>
    <div class="min-w-0 flex-1">
      <p class="text-sm font-medium text-gray-900 dark:text-gray-100 truncate" role="status" aria-live="polite" data-testid="direct-call-state">{{ stateText }}</p>
      <p class="text-xs text-gray-600 dark:text-gray-300 truncate" data-testid="direct-call-heard">
        <template v-if="call.notice.value">{{ call.notice.value }}</template>
        <template v-else-if="call.liveText.value">Heard: {{ call.liveText.value }}</template>
        <template v-else>{{ statusLine }}</template>
      </p>
      <p v-if="call.notice.value || call.liveText.value" class="hidden sm:block text-xs text-gray-500 dark:text-gray-400 truncate" data-testid="direct-call-line">{{ statusLine }}</p>
    </div>
    <BaseButton variant="ghost" size="sm" :aria-pressed="call.muted.value" data-testid="direct-call-mute" @click="call.toggleMute()">
      {{ call.muted.value ? 'Unmute' : 'Mute' }}
    </BaseButton>
    <BaseButton variant="secondary" size="sm" data-testid="direct-call-end" @click="$emit('end')">End call</BaseButton>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import { directStateText, directStatusLine } from './portalDirectCall'

const props = defineProps({ call: { type: Object, required: true } })
defineEmits(['end'])

const stateText = computed(() => directStateText({
  status: props.call.status.value, muted: props.call.muted.value, tool: props.call.tool.value,
}))
const statusLine = computed(() => directStatusLine({
  runtime: props.call.runtime.value, provider: props.call.provider.value, earlySpeech: props.call.earlySpeech.value,
}) + firstWord.value)
const firstWord = computed(() => {
  const ms = props.call.timing.value?.first_audio_ms
  return Number.isFinite(ms) ? ` · first word in ${(ms / 1000).toFixed(1)} s` : ''
})
const orbScale = computed(() => 1 + Math.min(0.35, (props.call.amplitude.value || 0) * 2))
</script>
