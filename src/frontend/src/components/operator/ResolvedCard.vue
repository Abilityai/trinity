<template>
  <div class="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-4 opacity-80">
    <div class="flex items-start gap-3">
      <!-- Agent Avatar -->
      <AgentAvatar :name="item.agent_name" :avatar-url="agentAvatarUrl" size="md" />

      <!-- Content -->
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 mb-0.5">
          <span
            class="text-sm font-medium text-gray-700 dark:text-gray-300"
            :title="agentNameTooltip(agentsStore.agentRefForSlug(item.agent_name))"
          >{{ item.agent_name }}</span>
          <span class="text-xs text-gray-400 dark:text-gray-500">&middot;</span>
          <span class="text-xs text-gray-400 dark:text-gray-500">{{ timeAgo(item.created_at) }}</span>
        </div>

        <p class="text-sm text-gray-600 dark:text-gray-400">{{ item.title }}</p>

        <!-- Response (responded/acknowledged) or terminal status (cancelled/expired, #1017) -->
        <div class="mt-2 flex items-center gap-2">
          <span
            v-if="isTerminalWithoutResponse"
            class="inline-flex items-center gap-1 text-xs text-gray-400 dark:text-gray-500"
          >
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
            </svg>
            {{ item.status === 'expired' ? 'Expired' : 'Cancelled' }}
          </span>
          <span v-else class="inline-flex items-center gap-1 text-xs text-status-success-600 dark:text-status-success-400">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
            </svg>
            {{ item.response }}
          </span>
          <span v-if="item.response_text" class="text-xs text-gray-400 dark:text-gray-500">
            &mdash; {{ item.response_text }}
          </span>
          <span class="text-xs text-gray-400 dark:text-gray-500 ml-auto">
            {{ timeAgo(item.responded_at || item.created_at) }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useOperatorQueueStore } from '../../stores/operatorQueue'
import { useAgentsStore } from '../../stores/agents'
import { agentNameTooltip } from '../../utils/agentName'
import AgentAvatar from '../AgentAvatar.vue'

const props = defineProps({
  item: { type: Object, required: true }
})

const store = useOperatorQueueStore()
const agentsStore = useAgentsStore()

const isTerminalWithoutResponse = computed(() =>
  props.item.status === 'cancelled' || props.item.status === 'expired'
)
const agentAvatarUrl = computed(() => {
  const agent = agentsStore.agents.find(a => a.name === props.item.agent_name)
  return agent?.avatar_url || null
})

function timeAgo(isoString) {
  if (!isoString) return ''
  const now = new Date()
  const then = new Date(isoString)
  const diffMs = now - then
  const diffMin = Math.floor(diffMs / 60000)
  const diffHr = Math.floor(diffMs / 3600000)
  const diffDay = Math.floor(diffMs / 86400000)

  if (diffMin < 1) return 'just now'
  if (diffMin < 60) return `${diffMin}m ago`
  if (diffHr < 24) return `${diffHr}h ago`
  return `${diffDay}d ago`
}
</script>
