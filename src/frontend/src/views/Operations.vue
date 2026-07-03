<template>
  <div class="min-h-screen bg-gray-50 dark:bg-gray-900">
    <NavBar />

    <!--
      Operations (#1109) — the single fleet-operations surface. Absorbs the
      former standalone Health (/monitoring) and Executions (/executions)
      pages as tabs alongside the existing Operating Room tabs. Container is
      max-w-7xl so the wide Health grid / Executions table breathe; the
      narrow operator card-feed tabs re-constrain themselves to max-w-3xl.
    -->
    <main class="max-w-7xl mx-auto py-6 px-4 sm:px-6">
      <!-- Page Header -->
      <div class="mb-6">
        <h1 class="text-2xl font-bold text-gray-900 dark:text-white">Operations</h1>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
          {{ subtitle }}
        </p>
      </div>

      <!-- Tabs: Needs Response / Notifications / Health (admin) / Executions / Resolved + Refresh -->
      <div class="flex items-center gap-1 mb-6 border-b border-gray-200 dark:border-gray-700 overflow-x-auto">
        <button
          @click="switchTab('needs-response')"
          class="px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px whitespace-nowrap"
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
          class="px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px whitespace-nowrap"
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
        <!-- Health tab is admin-only, gated at the tab level (#1109). The
             panel render below is independently gated on isAdmin so a
             non-admin deep-linking ?tab=health never mounts the panel. -->
        <button
          v-if="isAdmin"
          @click="switchTab('health')"
          class="px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px whitespace-nowrap"
          :class="activeTab === 'health'
            ? 'border-blue-500 text-blue-600 dark:text-blue-400'
            : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'"
        >
          Health
        </button>
        <button
          @click="switchTab('executions')"
          class="px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px whitespace-nowrap"
          :class="activeTab === 'executions'
            ? 'border-blue-500 text-blue-600 dark:text-blue-400'
            : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'"
        >
          Executions
        </button>
        <button
          @click="switchTab('reports')"
          class="px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px whitespace-nowrap"
          :class="activeTab === 'reports'
            ? 'border-blue-500 text-blue-600 dark:text-blue-400'
            : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'"
        >
          Reports
        </button>
        <button
          @click="switchTab('resolved')"
          class="px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px whitespace-nowrap"
          :class="activeTab === 'resolved'
            ? 'border-blue-500 text-blue-600 dark:text-blue-400'
            : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'"
        >
          Resolved
        </button>

        <!-- Spacer + Clear All / Refresh buttons (operator tabs only —
             Health/Executions panels carry their own refresh controls) -->
        <div class="ml-auto flex items-center gap-1 pb-1">
          <!-- Clear All (#1017) — hidden when the active tab has nothing to clear -->
          <button
            v-if="isOperatorTab && clearableCount > 0"
            @click="showClearConfirm = true"
            :disabled="clearing"
            class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-status-danger-600 dark:text-status-danger-400 hover:text-status-danger-700 dark:hover:text-status-danger-300 rounded-md hover:bg-status-danger-50 dark:hover:bg-status-danger-900/20 transition-colors disabled:opacity-50 whitespace-nowrap"
            data-testid="ops-clear-all"
          >
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
            </svg>
            Clear All
          </button>
          <button
            v-if="isOperatorTab"
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

      <!-- Needs Response Tab (narrow card feed) -->
      <div v-if="activeTab === 'needs-response'" class="max-w-3xl mx-auto">
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

      <!-- Notifications Tab (narrow card feed) -->
      <div v-if="activeTab === 'notifications'" class="max-w-3xl mx-auto">
        <NotificationsPanel />
      </div>

      <!-- Health Tab (admin-only — fleet monitoring) -->
      <div v-if="activeTab === 'health' && isAdmin">
        <MonitoringPanel />
      </div>

      <!-- Executions Tab (fleet execution list) -->
      <div v-if="activeTab === 'executions'">
        <ExecutionsPanel />
      </div>

      <!-- Reports Tab (#918) -->
      <div v-if="activeTab === 'reports'">
        <ReportsPanelFleet />
      </div>

      <!-- Resolved Items Tab (narrow card feed) -->
      <div v-if="activeTab === 'resolved'" class="max-w-3xl mx-auto">
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

    <!-- Clear All confirmation (#1017) -->
    <ConfirmDialog
      :visible="showClearConfirm"
      :title="clearConfirmTitle"
      :message="clearConfirmMessage"
      confirm-text="Clear All"
      variant="danger"
      @confirm="confirmClearAll"
      @cancel="showClearConfirm = false"
      @update:visible="showClearConfirm = $event"
    />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import NavBar from '../components/NavBar.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import QueueCard from '../components/operator/QueueCard.vue'
import ResolvedCard from '../components/operator/ResolvedCard.vue'
import NotificationsPanel from '../components/operator/NotificationsPanel.vue'
import MonitoringPanel from '../components/MonitoringPanel.vue'
import ExecutionsPanel from '../components/ExecutionsPanel.vue'
import ReportsPanelFleet from '../components/ReportsPanelFleet.vue'
import { useOperatorQueueStore } from '../stores/operatorQueue'
import { useNotificationsStore } from '../stores/notifications'
import { useAgentsStore } from '../stores/agents'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const operatorQueueStore = useOperatorQueueStore()
const notificationsStore = useNotificationsStore()
const agentsStore = useAgentsStore()
const authStore = useAuthStore()

const isAdmin = computed(() => authStore.role === 'admin')

// Health is admin-only; non-admins must not reach it even via deep link.
const VALID_TABS = ['needs-response', 'notifications', 'health', 'executions', 'reports', 'resolved']
const OPERATOR_TABS = ['needs-response', 'notifications', 'resolved']

function resolveTab(q) {
  // A non-admin landing on ?tab=health (e.g. the /monitoring redirect) is
  // bounced to the default tab rather than shown an empty Health surface.
  if (q === 'health' && !isAdmin.value) return 'needs-response'
  return VALID_TABS.includes(q) ? q : 'needs-response'
}

const activeTab = ref(resolveTab(route.query.tab))

const isOperatorTab = computed(() => OPERATOR_TABS.includes(activeTab.value))

const subtitle = computed(() => {
  if (activeTab.value === 'health') {
    return 'Fleet-wide health status and alerts'
  }
  if (activeTab.value === 'executions') {
    return 'All task runs across your fleet'
  }

  const queueCount = operatorQueueStore.pendingCount
  const notifCount = notificationsStore.pendingCount
  const total = queueCount + notifCount

  if (total === 0) {
    return 'All clear — your agents are working independently'
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

// --- Clear All (#1017) ---
const showClearConfirm = ref(false)
const clearing = ref(false)

// What the active tab can clear. For notifications this is only a visibility
// heuristic (pendingCount from the badge poll is effectively 0/1 — #1143 —
// so we also count loaded non-dismissed rows); the dismiss-all endpoint
// itself clears pending + acknowledged beyond the loaded page.
const clearableCount = computed(() => {
  if (activeTab.value === 'needs-response') return operatorQueueStore.openItems.length
  if (activeTab.value === 'resolved') return operatorQueueStore.resolvedItems.length
  if (activeTab.value === 'notifications') {
    const loadedNonDismissed = notificationsStore.notifications
      .filter(n => n.status !== 'dismissed').length
    return Math.max(notificationsStore.pendingCount, loadedNonDismissed)
  }
  return 0
})

const clearConfirmTitle = computed(() => {
  if (activeTab.value === 'needs-response') return 'Cancel all pending items?'
  if (activeTab.value === 'notifications') return 'Dismiss all notifications?'
  return 'Clear resolved items?'
})

const clearConfirmMessage = computed(() => {
  const n = clearableCount.value
  if (activeTab.value === 'needs-response') {
    return `This cancels ${n} pending ${n === 1 ? 'item' : 'items'} shown here. The agents waiting on them will be told their requests were cancelled and will not receive an answer. This affects all operators of these agents.`
  }
  if (activeTab.value === 'notifications') {
    return 'This dismisses every non-dismissed notification from your accessible agents — including any not shown by the current filters — for all operators of these agents.'
  }
  return `This permanently deletes ${n === 1 ? 'this resolved item' : 'the resolved items'} for all operators of these agents. Items still awaiting agent confirmation are kept.`
})

async function confirmClearAll() {
  showClearConfirm.value = false
  clearing.value = true
  try {
    if (activeTab.value === 'needs-response') {
      const ids = operatorQueueStore.openItems.map(i => i.id)
      await operatorQueueStore.bulkCancel(ids)
    } else if (activeTab.value === 'notifications') {
      await notificationsStore.dismissAll()
    } else if (activeTab.value === 'resolved') {
      await operatorQueueStore.clearResolved()
    }
  } catch (err) {
    // Store actions set their own error state; nothing else to do here.
    console.error('Clear All failed:', err)
  } finally {
    clearing.value = false
  }
}

// If the role resolves asynchronously after mount and confirms non-admin,
// bounce off the Health tab. (For admins deep-linking ?tab=health this
// re-selects health once isAdmin flips true.)
watch(isAdmin, (admin) => {
  if (!admin && activeTab.value === 'health') {
    switchTab('needs-response')
  } else if (admin && route.query.tab === 'health' && activeTab.value !== 'health') {
    activeTab.value = 'health'
  }
})

onMounted(() => {
  // Operator-queue polling runs at the container level (drives both the
  // Needs Response / Resolved feeds and the NavBar Operations badge).
  operatorQueueStore.startPolling(10000)
  // Ensure agent data (including avatars) is available for QueueCard/ResolvedCard
  // and the Executions agent filter. Single fetch here dedupes the panels'
  // own length-guarded fetches.
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
