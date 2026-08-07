<template>
  <div class="h-full flex flex-col">
    <!-- Empty state -->
    <div v-if="!item" class="flex-1 flex items-center justify-center text-gray-400 dark:text-gray-400">
      <div class="text-center">
        <svg class="mx-auto h-12 w-12 mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" />
        </svg>
        <p class="text-sm">Select an item from the queue</p>
      </div>
    </div>

    <!-- Item Detail -->
    <div v-else class="flex-1 overflow-y-auto">
      <!-- Header -->
      <div class="p-4 border-b border-gray-200 dark:border-gray-700">
        <div class="flex items-start justify-between gap-3">
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-1">
              <span
                class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium"
                :class="typeBadge(item.type)"
              >
                {{ item.type }}
              </span>
              <span
                class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium"
                :class="priorityBadge(item.priority)"
              >
                {{ item.priority }}
              </span>
              <span
                v-if="item.status !== 'pending'"
                class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium"
                :class="statusBadge(item.status)"
              >
                {{ item.status }}
              </span>
            </div>
            <h2 class="text-lg font-semibold text-gray-900 dark:text-white">
              {{ item.title }}
            </h2>
          </div>
        </div>

        <!-- Meta row -->
        <div class="mt-2 flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400">
          <span class="inline-flex items-center gap-1" :title="agentNameTooltip(agentsStore.agentRefForSlug(item.agent_name))">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
            </svg>
            {{ item.agent_name }}
          </span>
          <span class="inline-flex items-center gap-1">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            {{ formatDate(item.created_at) }}
          </span>
          <span v-if="item.expires_at" class="inline-flex items-center gap-1 text-state-autonomous-600 dark:text-state-autonomous-400">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
            Expires {{ formatDate(item.expires_at) }}
          </span>
          <span class="text-gray-400 dark:text-gray-400">{{ item.request_id || item.id }}</span>
        </div>
      </div>

      <!-- Question / Description -->
      <div class="p-4 border-b border-gray-200 dark:border-gray-700">
        <div class="prose prose-sm dark:prose-invert max-w-none" v-html="renderMarkdown(item.question)"></div>
      </div>

      <!-- Context -->
      <div v-if="item.context && Object.keys(item.context).length > 0" class="p-4 border-b border-gray-200 dark:border-gray-700">
        <h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">Context</h3>
        <div class="bg-gray-50 dark:bg-gray-800 rounded-lg p-3">
          <dl class="space-y-1.5">
            <div v-for="(value, key) in item.context" :key="key" class="flex items-start gap-3 text-sm">
              <dt class="flex-shrink-0 min-w-[120px] max-w-[200px] text-gray-500 dark:text-gray-400 font-mono text-xs truncate" :title="key">{{ key }}</dt>
              <dd class="text-gray-900 dark:text-white font-mono text-xs break-all">
                <a
                  v-if="isUrl(String(value))"
                  :href="String(value)"
                  target="_blank"
                  rel="noopener"
                  class="text-blue-600 dark:text-blue-400 hover:underline"
                >{{ value }}</a>
                <span v-else>{{ value }}</span>
              </dd>
            </div>
          </dl>
        </div>
      </div>

      <!-- Already responded info -->
      <div v-if="item.status !== 'pending'" class="p-4 border-b border-gray-200 dark:border-gray-700">
        <h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">Response</h3>
        <div class="bg-status-success-50 dark:bg-status-success-900/20 rounded-lg p-3 space-y-1">
          <p class="text-sm font-medium text-status-success-800 dark:text-status-success-300">{{ item.response }}</p>
          <p v-if="item.response_text" class="text-sm text-status-success-700 dark:text-status-success-400">{{ item.response_text }}</p>
          <p class="text-xs text-status-success-600 dark:text-status-success-500">
            by {{ item.responded_by_email }} &middot; {{ formatDate(item.responded_at) }}
          </p>
        </div>
      </div>

      <!-- Response Controls (only for pending items) -->
      <div v-if="item.status === 'pending'" class="p-4">
        <h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">Respond</h3>

        <!-- Approval type: option buttons -->
        <div v-if="item.type === 'approval' && item.options" class="space-y-3">
          <div class="flex flex-wrap gap-2">
            <button
              v-for="(option, idx) in item.options"
              :key="option"
              @click="selectedOption = option"
              class="px-4 py-2 rounded-lg text-sm font-medium border-2 transition-colors"
              :class="selectedOption === option
                ? optionSelectedClass(idx)
                : 'border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:border-gray-400 dark:hover:border-gray-500'"
            >
              {{ option }}
            </button>
          </div>

          <!-- Optional notes -->
          <div>
            <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1">Notes (optional)</label>
            <textarea
              v-model="responseText"
              rows="2"
              class="w-full text-sm rounded-lg border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white focus:ring-blue-500 focus:border-blue-500"
              placeholder="Add context for the agent..."
            ></textarea>
          </div>

          <button
            @click="submitResponse"
            :disabled="!selectedOption"
            class="w-full py-2 px-4 rounded-lg text-sm font-medium text-white transition-colors"
            :class="selectedOption
              ? 'bg-blue-600 hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600'
              : 'bg-gray-300 dark:bg-gray-600 cursor-not-allowed'"
          >
            Submit Response
          </button>
        </div>

        <!-- Question type: freeform textarea -->
        <div v-else-if="item.type === 'question'" class="space-y-3">
          <textarea
            v-model="responseText"
            rows="4"
            class="w-full text-sm rounded-lg border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white focus:ring-blue-500 focus:border-blue-500"
            placeholder="Type your answer..."
          ></textarea>
          <button
            @click="submitQuestionResponse"
            :disabled="!responseText.trim()"
            class="w-full py-2 px-4 rounded-lg text-sm font-medium text-white transition-colors"
            :class="responseText.trim()
              ? 'bg-blue-600 hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600'
              : 'bg-gray-300 dark:bg-gray-600 cursor-not-allowed'"
          >
            Send Answer
          </button>
        </div>

        <!-- Alert type: acknowledge button -->
        <div v-else-if="item.type === 'alert'" class="space-y-3">
          <textarea
            v-model="responseText"
            rows="2"
            class="w-full text-sm rounded-lg border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white focus:ring-blue-500 focus:border-blue-500"
            placeholder="Optional notes..."
          ></textarea>
          <button
            @click="submitAcknowledge"
            class="w-full py-2 px-4 rounded-lg text-sm font-medium text-white bg-state-autonomous-600 hover:bg-state-autonomous-700 dark:bg-state-autonomous-500 dark:hover:bg-state-autonomous-600 transition-colors"
          >
            Acknowledge
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, computed } from 'vue'
import { renderMarkdown } from '../../utils/markdown'
import { useOperatorQueueStore } from '../../stores/operatorQueue'
import { useAgentsStore } from '../../stores/agents'
import { agentNameTooltip } from '../../utils/agentName'

const store = useOperatorQueueStore()
const agentsStore = useAgentsStore()

const item = computed(() => store.selectedItem)

const selectedOption = ref(null)
const responseText = ref('')

// Reset form when selected item changes
watch(() => store.selectedItemId, () => {
  selectedOption.value = null
  responseText.value = ''
})

function submitResponse() {
  if (!selectedOption.value || !item.value) return
  store.respondToItem(item.value.id, selectedOption.value, responseText.value)
  selectedOption.value = null
  responseText.value = ''
}

function submitQuestionResponse() {
  if (!responseText.value.trim() || !item.value) return
  store.respondToItem(item.value.id, responseText.value.trim(), '')
  responseText.value = ''
}

function submitAcknowledge() {
  if (!item.value) return
  store.acknowledgeItem(item.value.id)
  responseText.value = ''
}

function optionSelectedClass(idx) {
  if (idx === 0) return 'border-status-success-500 bg-status-success-50 text-status-success-700 dark:border-status-success-400 dark:bg-status-success-900/30 dark:text-status-success-300'
  if (idx === 1) return 'border-status-danger-500 bg-status-danger-50 text-status-danger-700 dark:border-status-danger-400 dark:bg-status-danger-900/30 dark:text-status-danger-300'
  return 'border-blue-500 bg-blue-50 text-blue-700 dark:border-blue-400 dark:bg-blue-900/30 dark:text-blue-300'
}

function typeBadge(type) {
  const badges = {
    approval: 'bg-accent-purple-100 text-accent-purple-700 dark:bg-accent-purple-900/30 dark:text-accent-purple-400',
    question: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
    alert: 'bg-state-autonomous-100 text-state-autonomous-700 dark:bg-state-autonomous-900/30 dark:text-state-autonomous-400'
  }
  return badges[type] || ''
}

function priorityBadge(priority) {
  const badges = {
    critical: 'bg-status-danger-100 text-status-danger-700 dark:bg-status-danger-900/30 dark:text-status-danger-400',
    high: 'bg-status-urgent-100 text-status-urgent-700 dark:bg-status-urgent-900/30 dark:text-status-urgent-400',
    medium: 'bg-status-warning-100 text-status-warning-700 dark:bg-status-warning-900/30 dark:text-status-warning-400',
    low: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
  }
  return badges[priority] || ''
}

function statusBadge(status) {
  const badges = {
    responded: 'bg-status-success-100 text-status-success-700 dark:bg-status-success-900/30 dark:text-status-success-400',
    acknowledged: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
  }
  return badges[status] || ''
}

// renderMarkdown imported from utils/markdown

function formatDate(isoString) {
  if (!isoString) return ''
  const d = new Date(isoString)
  const now = new Date()
  const diffMs = now - d
  const diffMin = Math.floor(diffMs / 60000)
  const diffHr = Math.floor(diffMs / 3600000)

  if (diffMin < 1) return 'just now'
  if (diffMin < 60) return `${diffMin}m ago`
  if (diffHr < 24) return `${diffHr}h ago`

  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function isUrl(str) {
  return str.startsWith('http://') || str.startsWith('https://')
}
</script>
