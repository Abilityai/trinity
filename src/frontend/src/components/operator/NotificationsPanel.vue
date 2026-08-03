<template>
  <div>
    <!-- Filters -->
    <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4 mb-6">
      <div class="flex flex-wrap gap-4 items-end">
        <!-- Agent filter -->
        <div class="flex-1 min-w-[150px]">
          <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Agent</label>
          <select
            v-model="agentFilter"
            @change="applyFilters"
            class="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200"
          >
            <option value="">All Agents</option>
            <option v-for="agent in availableAgents" :key="agent" :value="agent">
              {{ agentOptionLabel(agentsStore.agentRefForSlug(agent)) }}
            </option>
          </select>
        </div>

        <!-- Type filter -->
        <div class="flex-1 min-w-[150px]">
          <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Type</label>
          <select
            v-model="typeFilter"
            @change="applyFilters"
            class="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200"
          >
            <option value="">All Types</option>
            <option value="alert">Alert</option>
            <option value="info">Info</option>
            <option value="status">Status</option>
            <option value="completion">Completion</option>
            <option value="question">Question</option>
          </select>
        </div>

        <!-- Priority filter -->
        <div class="flex-1 min-w-[150px]">
          <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Priority</label>
          <select
            v-model="priorityFilter"
            @change="applyFilters"
            class="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200"
          >
            <option value="">All Priorities</option>
            <option value="urgent">Urgent</option>
            <option value="high">High</option>
            <option value="normal">Normal</option>
            <option value="low">Low</option>
          </select>
        </div>

        <!-- Status filter -->
        <div class="flex-1 min-w-[150px]">
          <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Status</label>
          <select
            v-model="statusFilter"
            @change="applyFilters"
            class="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200"
          >
            <option value="pending">Pending</option>
            <option value="acknowledged">Acknowledged</option>
            <option value="">All</option>
          </select>
        </div>

        <!-- Show dismissed checkbox -->
        <div class="flex items-center gap-2">
          <input
            type="checkbox"
            id="showDismissedNotifs"
            v-model="showDismissed"
            @change="applyFilters"
            class="w-4 h-4 text-blue-600 bg-gray-100 border-gray-300 rounded focus:ring-blue-500 dark:focus:ring-blue-600 dark:ring-offset-gray-800 focus:ring-2 dark:bg-gray-700 dark:border-gray-600"
          />
          <label for="showDismissedNotifs" class="text-sm text-gray-500 dark:text-gray-400">
            Show dismissed
          </label>
        </div>

        <!-- Clear filters -->
        <button
          v-if="hasActiveFilters"
          @click="clearFilters"
          class="text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 flex items-center gap-1"
        >
          <XMarkIcon class="w-4 h-4" />
          Clear filters
        </button>
      </div>
    </div>

    <!-- Stats -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
      <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <div class="text-3xl font-bold text-status-danger-600 dark:text-status-danger-400">{{ notificationsStore.pendingCount }}</div>
        <div class="text-xs text-gray-500 dark:text-gray-400">Pending</div>
      </div>
      <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <div class="text-3xl font-bold text-status-success-600 dark:text-status-success-400">{{ acknowledgedCount }}</div>
        <div class="text-xs text-gray-500 dark:text-gray-400">Acknowledged</div>
      </div>
      <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <div class="text-3xl font-bold text-gray-900 dark:text-white">{{ notificationsStore.totalCount }}</div>
        <div class="text-xs text-gray-500 dark:text-gray-400">Total</div>
      </div>
      <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <div class="text-3xl font-bold text-blue-600 dark:text-blue-400">{{ Object.keys(notificationsStore.agentCounts).length }}</div>
        <div class="text-xs text-gray-500 dark:text-gray-400">Agents</div>
      </div>
    </div>

    <!-- Bulk Actions -->
    <div v-if="notificationsStore.selectedIds.length > 0" class="bg-blue-50 dark:bg-blue-900/30 rounded-lg shadow p-4 mb-4 flex items-center justify-between">
      <span class="text-sm text-blue-700 dark:text-blue-300">
        {{ notificationsStore.selectedIds.length }} selected
      </span>
      <div class="flex gap-2">
        <button
          @click="bulkAcknowledge"
          class="px-3 py-1.5 text-xs font-medium text-white bg-status-success-600 hover:bg-status-success-700 rounded-lg"
        >
          Acknowledge Selected
        </button>
        <button
          @click="bulkDismiss"
          class="px-3 py-1.5 text-xs font-medium text-white bg-gray-600 hover:bg-gray-700 rounded-lg"
        >
          Dismiss Selected
        </button>
        <button
          @click="notificationsStore.clearSelection"
          class="px-3 py-1.5 text-xs font-medium text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200"
        >
          Cancel
        </button>
      </div>
    </div>

    <!-- Notifications List -->
    <div class="bg-white dark:bg-gray-800 rounded-lg shadow">
      <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
        <h2 class="text-lg font-medium text-gray-900 dark:text-white">Notifications</h2>
        <div v-if="displayedNotifications.length > 0" class="flex items-center gap-2">
          <input
            type="checkbox"
            :checked="allSelected"
            @change="toggleSelectAll"
            class="w-4 h-4 text-blue-600 bg-gray-100 border-gray-300 rounded focus:ring-blue-500 dark:focus:ring-blue-600 dark:ring-offset-gray-800 focus:ring-2 dark:bg-gray-700 dark:border-gray-600"
          />
          <label class="text-xs text-gray-500 dark:text-gray-400">Select all</label>
        </div>
      </div>

      <!-- Action failure (#1926) — acknowledge / dismiss / bulk failures used to
           go to console only, so the row stayed put and the verb looked like it
           had simply done nothing. Errors persist here until dismissed
           (principle 18: toasts are for completed verbs). -->
      <div v-if="actionError" class="px-6 pt-4">
        <InlineError
          :message="actionError"
          :detail="actionErrorDetail"
          @dismiss="clearActionError"
        />
      </div>

      <!-- Loading state (#1926) — the body used to render blank during the
           first fetch: neither list, nor empty state, nor spinner. -->
      <div v-if="firstLoad" class="px-6 py-12 text-center" aria-busy="true">
        <svg class="w-8 h-8 mx-auto mb-4 animate-spin text-gray-400 dark:text-gray-500" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
        </svg>
        <p class="text-sm text-gray-500 dark:text-gray-400">Loading notifications…</p>
      </div>

      <!-- Failed state (#1926) — previously there was none at all. -->
      <LoadFailed
        v-else-if="loadFailed"
        title="Couldn't load notifications"
        message="We can't show what your agents have sent. Check your connection and try again."
        :detail="notificationsStore.error"
        :retrying="loading"
        @retry="fetchNotifications"
      />

      <div v-else class="divide-y divide-gray-200 dark:divide-gray-700">
        <div
          v-for="notification in displayedNotifications"
          :key="notification.id"
          class="px-6 py-4 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
          :class="{
            'bg-status-danger-50 dark:bg-status-danger-900/10': notification.status === 'pending' && notification.priority === 'urgent',
            'bg-status-urgent-50 dark:bg-status-urgent-900/10': notification.status === 'pending' && notification.priority === 'high',
            'opacity-60': notification.status === 'dismissed'
          }"
        >
          <div class="flex items-start gap-3">
            <!-- Checkbox -->
            <input
              type="checkbox"
              :checked="notificationsStore.selectedIds.includes(notification.id)"
              @change="notificationsStore.toggleSelected(notification.id)"
              class="mt-1 w-4 h-4 text-blue-600 bg-gray-100 border-gray-300 rounded focus:ring-blue-500 dark:focus:ring-blue-600 dark:ring-offset-gray-800 focus:ring-2 dark:bg-gray-700 dark:border-gray-600"
            />

            <!-- Priority/Type Icon -->
            <div
              class="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center"
              :class="getPriorityIconBg(notification.priority)"
            >
              <component :is="getTypeIcon(notification.notification_type)" class="w-5 h-5" :class="getPriorityIconColor(notification.priority)" />
            </div>

            <!-- Content -->
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="px-2 py-0.5 text-xs font-medium rounded" :class="getPriorityBadge(notification.priority)">
                  {{ notification.priority.toUpperCase() }}
                </span>
                <router-link
                  :to="`/agents/${notification.agent_name}`"
                  :title="agentNameTooltip(agentsStore.agentRefForSlug(notification.agent_name))"
                  class="font-medium text-blue-600 dark:text-blue-400 hover:underline truncate"
                >
                  {{ notification.agent_name }}
                </router-link>
                <span class="text-xs text-gray-500 dark:text-gray-400">
                  {{ formatRelativeTime(notification.created_at) }}
                </span>
              </div>

              <h3 class="font-medium text-gray-900 dark:text-white mt-1">{{ notification.title }}</h3>

              <p v-if="notification.message" class="text-sm text-gray-600 dark:text-gray-400 mt-1">
                {{ truncateMessage(notification) }}
                <button
                  v-if="notification.message.length > 150"
                  @click="toggleExpanded(notification.id)"
                  class="text-blue-600 dark:text-blue-400 hover:underline text-xs ml-1"
                >
                  {{ expandedIds.includes(notification.id) ? 'Show less' : 'Show more' }}
                </button>
              </p>

              <div class="flex items-center gap-4 mt-2 text-xs text-gray-500 dark:text-gray-400">
                <span v-if="notification.category" class="px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded">
                  {{ notification.category }}
                </span>
                <span class="px-2 py-0.5 rounded" :class="getTypeBadge(notification.notification_type)">
                  {{ notification.notification_type }}
                </span>
                <span v-if="notification.status === 'acknowledged'" class="flex items-center gap-1 text-status-success-600 dark:text-status-success-400">
                  <CheckIcon class="w-3 h-3" />
                  Acknowledged
                </span>
                <span v-if="notification.status === 'dismissed'" class="text-gray-400 dark:text-gray-500">
                  Dismissed
                </span>
              </div>

              <!-- Metadata (expandable) -->
              <div v-if="notification.metadata && expandedIds.includes(notification.id)" class="mt-3 p-2 bg-gray-50 dark:bg-gray-700 rounded text-xs font-mono overflow-x-auto">
                <pre>{{ JSON.stringify(notification.metadata, null, 2) }}</pre>
              </div>
            </div>

            <!-- Actions -->
            <div class="flex items-center gap-2 flex-shrink-0">
              <span
                class="px-2 py-1 text-xs font-medium rounded capitalize"
                :class="getStatusBadge(notification.status)"
              >
                {{ notification.status }}
              </span>

              <button
                v-if="notification.status === 'pending'"
                @click="acknowledge(notification.id)"
                class="px-3 py-1.5 text-xs font-medium text-status-success-700 dark:text-status-success-300 bg-status-success-100 dark:bg-status-success-900/30 border border-status-success-300 dark:border-status-success-700 rounded hover:bg-status-success-200 dark:hover:bg-status-success-900/50"
                title="Acknowledge"
              >
                <CheckIcon class="w-4 h-4" />
              </button>

              <button
                v-if="notification.status !== 'dismissed'"
                @click="dismiss(notification.id)"
                class="px-3 py-1.5 text-xs font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-50 dark:hover:bg-gray-600"
                title="Dismiss"
              >
                <XMarkIcon class="w-4 h-4" />
              </button>
            </div>
          </div>
        </div>

        <!-- Empty State — only once a fetch has SUCCEEDED and returned zero -->
        <div v-if="displayedNotifications.length === 0" class="px-6 py-12 text-center">
          <InboxIcon class="w-12 h-12 mx-auto mb-4 text-gray-300 dark:text-gray-600" />
          <p class="text-lg font-medium text-gray-900 dark:text-white">
            {{ hasActiveFilters ? 'No matching events' : 'No events yet' }}
          </p>
          <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
            {{ hasActiveFilters ? 'Try adjusting your filters' : 'Notifications from your agents will appear here when they send them.' }}
          </p>
          <button
            v-if="hasActiveFilters"
            @click="clearFilters"
            class="mt-4 px-4 py-2 text-sm font-medium text-blue-600 dark:text-blue-400 hover:underline"
          >
            Clear all filters
          </button>
        </div>
      </div>

      <!-- Load More -->
      <div v-if="notificationsStore.hasMore && displayedNotifications.length > 0" class="px-6 py-4 border-t border-gray-200 dark:border-gray-700">
        <button
          @click="loadMore"
          :disabled="loading"
          class="w-full py-2 text-sm font-medium text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700 rounded-lg"
        >
          {{ loading ? 'Loading...' : 'Load more' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useNotificationsStore } from '../../stores/notifications'
import { useAgentsStore } from '../../stores/agents'
import { agentNameTooltip, agentOptionLabel } from '../../utils/agentName'
import LoadFailed from '../LoadFailed.vue'
import InlineError from '../InlineError.vue'
import { apiErrorMessage } from '../../utils/apiError'
import {
  XMarkIcon,
  CheckIcon,
  InboxIcon,
  ExclamationTriangleIcon,
  InformationCircleIcon,
  ChartBarIcon,
  CheckCircleIcon,
  QuestionMarkCircleIcon,
  BellIcon,
} from '@heroicons/vue/24/outline'

const notificationsStore = useNotificationsStore()
const agentsStore = useAgentsStore()

// State
const loading = ref(false)
// #1926: failures of the row verbs (acknowledge / dismiss / bulk) surface here,
// next to the controls, and persist until dismissed.
const actionError = ref('')
const actionErrorDetail = ref('')
const statusFilter = ref('pending')
const priorityFilter = ref('')
const agentFilter = ref('')
const typeFilter = ref('')
const showDismissed = ref(false)
const expandedIds = ref([])

// Computed
const hasActiveFilters = computed(() => {
  return statusFilter.value !== 'pending' ||
         priorityFilter.value !== '' ||
         agentFilter.value !== '' ||
         typeFilter.value !== '' ||
         showDismissed.value
})

const availableAgents = computed(() => {
  const agents = new Set()
  notificationsStore.notifications.forEach(n => agents.add(n.agent_name))
  return Array.from(agents).sort()
})

const displayedNotifications = computed(() => {
  let result = [...notificationsStore.notifications]

  if (typeFilter.value) {
    result = result.filter(n => n.notification_type === typeFilter.value)
  }

  if (!showDismissed.value) {
    result = result.filter(n => n.status !== 'dismissed')
  }

  return result
})

const acknowledgedCount = computed(() => {
  return notificationsStore.notifications.filter(n => n.status === 'acknowledged').length
})

const allSelected = computed(() => {
  return displayedNotifications.value.length > 0 &&
         displayedNotifications.value.every(n => notificationsStore.selectedIds.includes(n.id))
})

// Lifecycle
onMounted(() => {
  fetchNotifications()
})

// Methods
// #1926 loading / failed / empty triad. `hasLoaded` flips only on a succeeded
// fetch, so an empty list before the first response reads as loading, and a
// failed first fetch reads as failed — not as "No events yet".
const firstLoad = computed(() => !notificationsStore.hasLoaded && !notificationsStore.error)
const loadFailed = computed(() => !notificationsStore.hasLoaded && !!notificationsStore.error)

function clearActionError() {
  actionError.value = ''
  actionErrorDetail.value = ''
}

// The store's bulk helpers use Promise.allSettled, so they RESOLVE even when
// every item failed — a try/catch alone would report success on a total
// failure (#1926). Inspect the settlement and say how many did not apply.
function reportSettled(results, attempted, verb, past) {
  const rejected = (results || []).filter(r => r.status === 'rejected')
  if (!rejected.length) return
  const failed = rejected.length
  actionError.value = failed === attempted
    ? `Couldn't ${verb} the ${attempted} selected notification(s) — none were changed. Try again.`
    : `${failed} of ${attempted} notification(s) couldn't be ${past} and are unchanged. Try again.`
  actionErrorDetail.value = apiErrorMessage(rejected[0].reason, 'Request failed')
}

function reportActionFailure(err, what) {
  console.error(`Failed to ${what}:`, err)
  actionError.value = `Couldn't ${what}. Nothing was changed — try again.`
  actionErrorDetail.value = apiErrorMessage(err, 'Request failed')
}

async function fetchNotifications() {
  loading.value = true
  try {
    await notificationsStore.fetchNotifications({
      status: statusFilter.value || undefined,
      priority: priorityFilter.value || undefined,
      agentName: agentFilter.value || undefined,
      offset: 0,
    })
  } finally {
    loading.value = false
  }
}

function applyFilters() {
  notificationsStore.setFilters({
    status: statusFilter.value,
    priority: priorityFilter.value,
    agentName: agentFilter.value,
    showDismissed: showDismissed.value,
  })
  fetchNotifications()
}

function clearFilters() {
  statusFilter.value = 'pending'
  priorityFilter.value = ''
  agentFilter.value = ''
  typeFilter.value = ''
  showDismissed.value = false
  notificationsStore.clearFilters()
  fetchNotifications()
}

async function acknowledge(notificationId) {
  clearActionError()
  try {
    await notificationsStore.acknowledgeNotification(notificationId)
  } catch (err) {
    reportActionFailure(err, 'acknowledge this notification')
  }
}

async function dismiss(notificationId) {
  clearActionError()
  try {
    await notificationsStore.dismissNotification(notificationId)
  } catch (err) {
    reportActionFailure(err, 'dismiss this notification')
  }
}

async function bulkAcknowledge() {
  clearActionError()
  const count = notificationsStore.selectedIds.length
  try {
    reportSettled(
      await notificationsStore.bulkAcknowledge(notificationsStore.selectedIds),
      count,
      'acknowledge',
      'acknowledged'
    )
  } catch (err) {
    reportActionFailure(err, 'acknowledge the selected notifications')
  }
}

async function bulkDismiss() {
  clearActionError()
  const count = notificationsStore.selectedIds.length
  try {
    reportSettled(
      await notificationsStore.bulkDismiss(notificationsStore.selectedIds),
      count,
      'dismiss',
      'dismissed'
    )
  } catch (err) {
    reportActionFailure(err, 'dismiss the selected notifications')
  }
}

function loadMore() {
  notificationsStore.loadMore()
}

function toggleSelectAll() {
  if (allSelected.value) {
    notificationsStore.clearSelection()
  } else {
    displayedNotifications.value.forEach(n => {
      if (!notificationsStore.selectedIds.includes(n.id)) {
        notificationsStore.toggleSelected(n.id)
      }
    })
  }
}

function toggleExpanded(notificationId) {
  const index = expandedIds.value.indexOf(notificationId)
  if (index === -1) {
    expandedIds.value.push(notificationId)
  } else {
    expandedIds.value.splice(index, 1)
  }
}

function truncateMessage(notification) {
  if (!notification.message) return ''
  if (expandedIds.value.includes(notification.id)) return notification.message
  return notification.message.length > 150 ? notification.message.substring(0, 150) + '...' : notification.message
}

function formatRelativeTime(dateStr) {
  if (!dateStr) return ''
  const date = new Date(dateStr)
  const now = new Date()
  const diffMs = now - date
  const diffMins = Math.floor(diffMs / 60000)
  const diffHours = Math.floor(diffMs / 3600000)
  const diffDays = Math.floor(diffMs / 86400000)

  if (diffMins < 1) return 'just now'
  if (diffMins < 60) return `${diffMins}m ago`
  if (diffHours < 24) return `${diffHours}h ago`
  return `${diffDays}d ago`
}

function getTypeIcon(type) {
  const icons = {
    alert: ExclamationTriangleIcon,
    info: InformationCircleIcon,
    status: ChartBarIcon,
    completion: CheckCircleIcon,
    question: QuestionMarkCircleIcon,
  }
  return icons[type] || BellIcon
}

function getPriorityIconBg(priority) {
  const classes = {
    urgent: 'bg-status-danger-100 dark:bg-status-danger-900/30',
    high: 'bg-status-urgent-100 dark:bg-status-urgent-900/30',
    normal: 'bg-status-info-100 dark:bg-status-info-900/30',
    low: 'bg-gray-100 dark:bg-gray-700',
  }
  return classes[priority] || 'bg-gray-100 dark:bg-gray-700'
}

function getPriorityIconColor(priority) {
  const classes = {
    urgent: 'text-status-danger-600 dark:text-status-danger-400',
    high: 'text-status-urgent-600 dark:text-status-urgent-400',
    normal: 'text-status-info-600 dark:text-status-info-400',
    low: 'text-gray-600 dark:text-gray-400',
  }
  return classes[priority] || 'text-gray-600 dark:text-gray-400'
}

function getPriorityBadge(priority) {
  const classes = {
    urgent: 'bg-status-danger-100 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300',
    high: 'bg-status-urgent-100 dark:bg-status-urgent-900/30 text-status-urgent-700 dark:text-status-urgent-300',
    normal: 'bg-status-info-100 dark:bg-status-info-900/30 text-status-info-700 dark:text-status-info-300',
    low: 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300',
  }
  return classes[priority] || 'bg-gray-100 text-gray-700'
}

function getTypeBadge(type) {
  const classes = {
    alert: 'bg-status-danger-100 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300',
    info: 'bg-status-info-100 dark:bg-status-info-900/30 text-status-info-700 dark:text-status-info-300',
    status: 'bg-accent-purple-100 dark:bg-accent-purple-900/30 text-accent-purple-700 dark:text-accent-purple-300',
    completion: 'bg-status-success-100 dark:bg-status-success-900/30 text-status-success-700 dark:text-status-success-300',
    question: 'bg-status-warning-100 dark:bg-status-warning-900/30 text-status-warning-700 dark:text-status-warning-300',
  }
  return classes[type] || 'bg-gray-100 text-gray-700'
}

function getStatusBadge(status) {
  const classes = {
    pending: 'bg-status-warning-100 dark:bg-status-warning-900/30 text-status-warning-700 dark:text-status-warning-300',
    acknowledged: 'bg-status-success-100 dark:bg-status-success-900/30 text-status-success-700 dark:text-status-success-300',
    dismissed: 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400',
  }
  return classes[status] || 'bg-gray-100 text-gray-700'
}
</script>
