<template>
  <div
    class="bg-white dark:bg-gray-800 rounded-xl border transition-all duration-200"
    data-testid="queue-card"
    :aria-expanded="isExpanded"
    :class="isExpanded
      ? 'border-blue-300 dark:border-blue-600 shadow-lg ring-1 ring-blue-100 dark:ring-blue-900/50'
      : 'border-gray-200 dark:border-gray-700 shadow-sm hover:shadow-md hover:border-gray-300 dark:hover:border-gray-600 cursor-pointer'"
    @click="!isExpanded && store.toggleExpand(item.id)"
  >
    <!-- Card Header — always visible -->
    <div class="p-4">
      <div class="flex items-start gap-3">
        <!-- Agent Avatar -->
        <AgentAvatar :name="item.agent_name" :avatar-url="agentAvatarUrl" size="lg" />

        <!-- Main content -->
        <div class="flex-1 min-w-0">
          <!-- Agent name + time -->
          <div class="flex items-center gap-2 mb-0.5">
            <span
              class="text-sm font-semibold text-gray-900 dark:text-white"
              :title="agentNameTooltip(agentsStore.agentRefForSlug(item.agent_name))"
            >{{ item.agent_name }}</span>
            <span class="text-xs text-gray-400 dark:text-gray-500">&middot;</span>
            <span class="text-xs text-gray-400 dark:text-gray-400">{{ timeAgo(item.created_at) }}</span>
          </div>

          <!-- Title -->
          <h3 class="text-base text-gray-900 dark:text-white leading-snug">
            {{ item.title }}
          </h3>

          <!-- Type + priority pills (subtle) -->
          <div class="flex items-center gap-2 mt-1.5">
            <span class="text-xs px-2 py-0.5 rounded-full" :class="typePill(item.type)">
              {{ queueTypeLabel(item.type) }}
            </span>
            <span
              v-if="item.priority === 'critical' || item.priority === 'high'"
              class="text-xs px-2 py-0.5 rounded-full"
              :class="priorityPill(item.priority)"
            >
              {{ item.priority }}
            </span>
          </div>
        </div>

        <!-- Expand/collapse toggle -->
        <button
          v-if="isExpanded"
          @click.stop="store.toggleExpand(item.id)"
          class="flex-shrink-0 p-1 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 rounded"
          aria-label="Collapse"
        >
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
    </div>

    <!-- Expanded Content -->
    <div v-if="isExpanded" class="border-t border-gray-100 dark:border-gray-700">
      <!-- Question body -->
      <div class="px-4 pt-3 pb-4">
        <div class="prose prose-sm dark:prose-invert max-w-none text-gray-700 dark:text-gray-300" v-html="renderMarkdown(item.question)"></div>
      </div>

      <!-- Context (collapsible) -->
      <div v-if="item.context && Object.keys(item.context).length > 0" class="px-4 pb-4">
        <button
          @click.stop="showContext = !showContext"
          class="text-xs text-gray-400 dark:text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 flex items-center gap-1"
        >
          <svg class="w-3.5 h-3.5 transition-transform" :class="showContext ? 'rotate-90' : ''" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
          </svg>
          {{ showContext ? 'Hide' : 'Show' }} details
        </button>
        <div v-if="showContext" class="mt-2 bg-gray-50 dark:bg-gray-900/50 rounded-lg p-3">
          <div v-for="(value, key) in item.context" :key="key" class="flex gap-3 text-xs py-0.5">
            <span class="text-gray-400 dark:text-gray-400 font-mono min-w-[100px]">{{ key }}</span>
            <a
              v-if="isUrl(String(value))"
              :href="String(value)"
              target="_blank"
              rel="noopener"
              class="text-blue-600 dark:text-blue-400 hover:underline font-mono"
              @click.stop
            >{{ value }}</a>
            <span v-else class="text-gray-700 dark:text-gray-300 font-mono">{{ value }}</span>
          </div>
        </div>
      </div>

      <!-- Response area -->
      <div class="px-4 pb-4">
        <!-- Approval: option buttons -->
        <div v-if="item.type === 'approval' && item.options" class="space-y-3">
          <div class="flex flex-wrap gap-2">
            <button
              v-for="(option, idx) in item.options"
              :key="option"
              @click.stop="selectedOption = option"
              class="px-4 py-2 rounded-lg text-sm font-medium border-2 transition-all"
              :class="selectedOption === option
                ? optionClass(idx)
                : 'border-gray-200 dark:border-gray-600 text-gray-600 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-500'"
            >
              {{ option }}
            </button>
          </div>
          <div class="flex gap-2">
            <input
              v-model="responseText"
              type="text"
              class="flex-1 text-sm rounded-lg border-gray-200 dark:border-gray-600 dark:bg-gray-700 dark:text-white placeholder-gray-400 dark:placeholder-gray-500 focus:ring-blue-500 focus:border-blue-500"
              placeholder="Add a note (optional)..."
              @click.stop
              @keydown.enter.stop="selectedOption && submitApproval()"
            />
            <button
              @click.stop="submitApproval"
              :disabled="!selectedOption"
              class="px-5 py-2 rounded-lg text-sm font-medium text-white transition-colors"
              :class="selectedOption
                ? 'bg-blue-600 hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600'
                : 'bg-gray-200 dark:bg-gray-700 text-gray-400 dark:text-gray-500 cursor-not-allowed'"
            >
              Send
            </button>
          </div>
        </div>

        <!-- Question: freeform answer -->
        <div v-else-if="item.type === 'question'" class="space-y-2">
          <textarea
            v-model="responseText"
            rows="3"
            class="w-full text-sm rounded-lg border-gray-200 dark:border-gray-600 dark:bg-gray-700 dark:text-white placeholder-gray-400 dark:placeholder-gray-500 focus:ring-blue-500 focus:border-blue-500"
            placeholder="Type your answer..."
            @click.stop
          ></textarea>
          <div class="flex justify-end">
            <button
              @click.stop="submitAnswer"
              :disabled="!responseText.trim()"
              class="px-5 py-2 rounded-lg text-sm font-medium text-white transition-colors"
              :class="responseText.trim()
                ? 'bg-blue-600 hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600'
                : 'bg-gray-200 dark:bg-gray-700 text-gray-400 dark:text-gray-500 cursor-not-allowed'"
            >
              Send Answer
            </button>
          </div>
        </div>

        <!-- Alert: acknowledge -->
        <div v-else-if="item.type === 'alert'" class="flex justify-end">
          <button
            @click.stop="store.acknowledgeItem(item.id)"
            class="px-5 py-2 rounded-lg text-sm font-medium text-gray-700 dark:text-gray-200 bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
          >
            Got it
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { renderMarkdown } from '../../utils/markdown'
import { useOperatorQueueStore } from '../../stores/operatorQueue'
import { useAgentsStore } from '../../stores/agents'
import { agentNameTooltip } from '../../utils/agentName'
import { queueTypeLabel } from '../../utils/operatorQueue'
import AgentAvatar from '../AgentAvatar.vue'

const props = defineProps({
  item: { type: Object, required: true }
})

const store = useOperatorQueueStore()
const agentsStore = useAgentsStore()

const isExpanded = computed(() => store.expandedItemId === props.item.id)
const agentAvatarUrl = computed(() => {
  const agent = agentsStore.agents.find(a => a.name === props.item.agent_name)
  return agent?.avatar_url || null
})

const selectedOption = ref(null)
const responseText = ref('')
const showContext = ref(false)

// Reset form when collapsed
watch(isExpanded, (val) => {
  if (!val) {
    selectedOption.value = null
    responseText.value = ''
    showContext.value = false
  }
})

function submitApproval() {
  if (!selectedOption.value) return
  store.respondToItem(props.item.id, selectedOption.value, responseText.value)
}

function submitAnswer() {
  if (!responseText.value.trim()) return
  store.respondToItem(props.item.id, responseText.value.trim(), '')
}

function optionClass(idx) {
  if (idx === 0) return 'border-status-success-500 bg-status-success-50 text-status-success-700 dark:border-status-success-400 dark:bg-status-success-900/20 dark:text-status-success-300'
  if (idx === 1) return 'border-status-danger-500 bg-status-danger-50 text-status-danger-700 dark:border-status-danger-400 dark:bg-status-danger-900/20 dark:text-status-danger-300'
  return 'border-blue-500 bg-blue-50 text-blue-700 dark:border-blue-400 dark:bg-blue-900/20 dark:text-blue-300'
}

function typePill(type) {
  return {
    approval: 'bg-accent-purple-50 text-accent-purple-600 dark:bg-accent-purple-900/20 dark:text-accent-purple-400',
    question: 'bg-blue-50 text-blue-600 dark:bg-blue-900/20 dark:text-blue-400',
    alert: 'bg-state-autonomous-50 text-state-autonomous-600 dark:bg-state-autonomous-900/20 dark:text-state-autonomous-400'
  }[type] || ''
}

function priorityPill(priority) {
  return {
    critical: 'bg-status-danger-50 text-status-danger-600 dark:bg-status-danger-900/20 dark:text-status-danger-400',
    high: 'bg-status-urgent-50 text-status-urgent-600 dark:bg-status-urgent-900/20 dark:text-status-urgent-400'
  }[priority] || ''
}

// renderMarkdown imported from utils/markdown

function timeAgo(isoString) {
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

function isUrl(str) {
  return str.startsWith('http://') || str.startsWith('https://')
}
</script>
