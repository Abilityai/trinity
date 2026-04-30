<template>
  <div class="min-h-screen bg-gray-50 dark:bg-gray-900">
    <NavBar />

    <main class="max-w-3xl mx-auto py-6 px-4 sm:px-6">
      <!-- Page Header -->
      <div class="mb-6">
        <h1 class="text-2xl font-bold text-gray-900 dark:text-white">Operating Room</h1>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
          {{ subtitle }}
        </p>
      </div>

      <!-- Tabs: Needs Response / Notifications / Cost Alerts / Resolved + Refresh -->
      <div class="flex items-center gap-1 mb-6 border-b border-gray-200 dark:border-gray-700">
        <button
          @click="switchTab('needs-response')"
          class="px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px"
          :class="activeTab === 'needs-response'
            ? 'border-blue-500 text-blue-600 dark:text-blue-400'
            : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'"
        >
          Needs Response
          <span
            v-if="operatorQueueStore.pendingCount > 0"
            class="ml-1.5 px-1.5 py-0.5 text-xs font-medium rounded-full"
            :class="activeTab === 'needs-response'
              ? 'bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400'
              : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'"
          >
            {{ operatorQueueStore.pendingCount }}
          </span>
        </button>
        <button
          @click="switchTab('notifications')"
          class="px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px"
          :class="activeTab === 'notifications'
            ? 'border-blue-500 text-blue-600 dark:text-blue-400'
            : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'"
        >
          Notifications
          <span
            v-if="notificationsStore.pendingCount > 0"
            class="ml-1.5 px-1.5 py-0.5 text-xs font-medium rounded-full"
            :class="activeTab === 'notifications'
              ? 'bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400'
              : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'"
          >
            {{ notificationsStore.pendingCount }}
          </span>
        </button>
        <button
          @click="switchTab('resolved')"
          class="px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px"
          :class="activeTab === 'resolved'
            ? 'border-blue-500 text-blue-600 dark:text-blue-400'
            : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'"
        >
          Resolved
        </button>

        <!-- Spacer + Refresh button -->
        <div class="ml-auto flex items-center pb-1">
          <button
            @click="refresh"
            :disabled="operatorQueueStore.loading"
            class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors disabled:opacity-50"
            title="Refresh"
          >
            <svg
              class="w-3.5 h-3.5"
              :class="{ 'animate-spin': operatorQueueStore.loading }"
              fill="none" stroke="currentColor" viewBox="0 0 24 24"
            >
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
            Refresh
          </button>
        </div>
      </div>

      <!-- Needs Response Tab -->
      <div v-if="activeTab === 'needs-response'">
        <!-- Empty state -->
        <div v-if="operatorQueueStore.openItems.length === 0" class="text-center py-16">
          <div class="inline-flex items-center justify-center w-16 h-16 rounded-full bg-status-success-100 dark:bg-status-success-900/20 mb-4">
            <svg class="w-8 h-8 text-status-success-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
            </svg>
          </div>
          <h3 class="text-lg font-medium text-gray-900 dark:text-white">All caught up</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Your agents are working independently. Nice.</p>
        </div>

        <!-- Card feed -->
        <div v-else class="space-y-3">
          <QueueCard
            v-for="item in operatorQueueStore.openItems"
            :key="item.id"
            :item="item"
          />
        </div>
      </div>

      <!-- Notifications Tab -->
      <div v-if="activeTab === 'notifications'">
        <NotificationsPanel />
      </div>

      <!-- Resolved Items Tab -->
      <div v-if="activeTab === 'resolved'">
        <div v-if="operatorQueueStore.resolvedItems.length === 0" class="text-center py-16">
          <p class="text-sm text-gray-500 dark:text-gray-400">No resolved items yet</p>
        </div>

        <div v-else class="space-y-2">
          <ResolvedCard
            v-for="item in operatorQueueStore.resolvedItems"
            :key="item.id"
            :item="item"
          />
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import NavBar from '../components/NavBar.vue'
import QueueCard from '../components/operator/QueueCard.vue'
import ResolvedCard from '../components/operator/ResolvedCard.vue'
import NotificationsPanel from '../components/operator/NotificationsPanel.vue'
import { useOperatorQueueStore } from '../stores/operatorQueue'
import { useNotificationsStore } from '../stores/notifications'
import { useAgentsStore } from '../stores/agents'

const route = useRoute()
const router = useRouter()
const operatorQueueStore = useOperatorQueueStore()
const notificationsStore = useNotificationsStore()
const agentsStore = useAgentsStore()

const VALID_TABS = ['needs-response', 'notifications', 'resolved']

// Initialize tab from query param or default
const activeTab = ref(
  VALID_TABS.includes(route.query.tab) ? route.query.tab : 'needs-response'
)

const subtitle = computed(() => {
  const queueCount = operatorQueueStore.pendingCount
  const notifCount = notificationsStore.pendingCount
  const total = queueCount + notifCount

  if (total === 0) {
    return 'All clear \u2014 your agents are working independently'
  }

  const parts = []
  if (queueCount > 0) parts.push(`${queueCount} pending ${queueCount === 1 ? 'response' : 'responses'}`)
  if (notifCount > 0) parts.push(`${notifCount} ${notifCount === 1 ? 'notification' : 'notifications'}`)
  return parts.join(', ')
})

function switchTab(tab) {
  activeTab.value = tab
  router.replace({ query: { ...route.query, tab } })
}

function refresh() {
  operatorQueueStore.fetchItems()
  notificationsStore.fetchPendingCount()
}

onMounted(() => {
  operatorQueueStore.startPolling(10000)
  // Ensure agent data (including avatars) is available for QueueCard/ResolvedCard
  if (agentsStore.agents.length === 0) {
    agentsStore.fetchAgents()
  }
})

onUnmounted(() => {
  operatorQueueStore.stopPolling()
})

// Auto-expand first item when items arrive
watch(() => operatorQueueStore.openItems.length, (len) => {
  if (len > 0 && !operatorQueueStore.expandedItemId) {
    operatorQueueStore.toggleExpand(operatorQueueStore.openItems[0].id)
  }
})
</script>
