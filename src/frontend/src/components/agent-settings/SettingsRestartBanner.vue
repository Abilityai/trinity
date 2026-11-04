<template>
  <!--
    The ONE restart banner (trinity-enterprise#887). It replaces the banners
    file sharing, shared folders and the resources dialog each drew in their
    own card: it names every change waiting for a restart and carries the one
    button that applies them.

    It keeps the same footprint when nothing is waiting (a quiet line instead
    of nothing). A restart-gated toggle lives BELOW this banner, so a banner
    that appeared from nothing would push that toggle out from under the
    pointer the moment it was flipped (principles 4 and 29).
  -->
  <div
    data-testid="settings-restart-banner"
    :data-pending="items.length > 0 ? 'true' : 'false'"
    class="min-h-[52px] rounded-lg border px-4 py-2.5 flex flex-wrap items-center gap-x-3 gap-y-2"
    :class="items.length > 0
      ? 'bg-status-warning-50 border-status-warning-200 dark:bg-status-warning-500/10 dark:border-status-warning-500/30'
      : 'bg-gray-50 border-gray-200 dark:bg-gray-900 dark:border-gray-750'"
    :role="items.length > 0 ? 'status' : undefined"
  >
    <template v-if="items.length > 0">
      <p class="min-w-0 flex-1 text-sm">
        <span class="font-semibold text-status-warning-700 dark:text-status-warning-300">{{ headline }}</span>
        <span class="ml-2 text-status-warning-700 dark:text-status-warning-300" data-testid="settings-restart-names">{{ names }}</span>
      </p>
      <BaseButton
        variant="secondary"
        size="sm"
        :loading="restarting"
        :loading-label="running ? 'Restarting…' : 'Starting…'"
        data-testid="settings-restart-now"
        @click="$emit('restart')"
      >{{ running ? 'Restart now' : 'Start now' }}</BaseButton>
    </template>
    <p v-else class="text-[12.5px] text-gray-600 dark:text-gray-400">
      Every change here is live. Anything that needs a restart is listed in this bar.
    </p>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import BaseButton from '../base/BaseButton.vue'
import { useAgentRestartStore } from '../../stores/agentRestart'

const props = defineProps({
  agentName: { type: String, required: true },
  agentStatus: { type: String, default: '' },
  restarting: { type: Boolean, default: false },
})
defineEmits(['restart'])

const restartStore = useAgentRestartStore()
const items = computed(() => restartStore.pendingFor(props.agentName))
const running = computed(() => props.agentStatus === 'running')
const names = computed(() => items.value.map((i) => i.label).join(', '))
const headline = computed(() => {
  const n = items.value.length
  const what = n === 1 ? '1 change waits' : `${n} changes wait`
  return running.value ? `${what} for a restart` : `${what} for the agent to start`
})
</script>
