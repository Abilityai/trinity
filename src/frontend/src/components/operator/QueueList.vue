<template>
  <div class="flex flex-col h-full">
    <!-- Filters -->
    <div class="p-3 border-b border-gray-200 dark:border-gray-700 space-y-2">
      <div class="flex items-center gap-2">
        <select
          :value="store.filters.type"
          @change="store.setFilter('type', $event.target.value)"
          class="flex-1 text-xs rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 focus:ring-blue-500 focus:border-blue-500"
        >
          <option value="">All Types</option>
          <option value="approval">Approvals</option>
          <option value="question">Questions</option>
          <option value="alert">Alerts</option>
        </select>
        <select
          :value="store.filters.priority"
          @change="store.setFilter('priority', $event.target.value)"
          class="flex-1 text-xs rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 focus:ring-blue-500 focus:border-blue-500"
        >
          <option value="">All Priority</option>
          <option value="critical">Critical</option>
          <option value="high">High</option>
          <option value="medium">Medium</option>
          <option value="low">Low</option>
        </select>
      </div>
      <div class="flex items-center gap-2">
        <select
          :value="store.filters.agentName"
          @change="store.setFilter('agentName', $event.target.value)"
          class="flex-1 text-xs rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 focus:ring-blue-500 focus:border-blue-500"
        >
          <option value="">All Agents</option>
          <option v-for="name in store.agentNames" :key="name" :value="name">{{ agentOptionLabel(agentsStore.agentRefForSlug(name)) }}</option>
        </select>
        <select
          :value="store.filters.status"
          @change="store.setFilter('status', $event.target.value)"
          class="flex-1 text-xs rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 focus:ring-blue-500 focus:border-blue-500"
        >
          <option value="">All Status</option>
          <option value="pending">Pending</option>
          <option value="responded">Responded</option>
          <option value="acknowledged">Acknowledged</option>
        </select>
      </div>
    </div>

    <!-- Queue Items -->
    <div class="flex-1 overflow-y-auto">
      <div v-if="store.filteredItems.length === 0" class="p-6 text-center text-gray-500 dark:text-gray-400 text-sm">
        No items match your filters
      </div>
      <button
        v-for="item in store.filteredItems"
        :key="item.id"
        @click="store.selectItem(item.id)"
        class="w-full text-left p-3 border-b border-gray-100 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-750 transition-colors"
        :class="{
          'bg-blue-50 dark:bg-blue-900/20 border-l-2 border-l-blue-500': store.selectedItemId === item.id,
          'border-l-2 border-l-transparent': store.selectedItemId !== item.id
        }"
      >
        <div class="flex items-start gap-2">
          <!-- Priority Indicator -->
          <span
            class="mt-0.5 flex-shrink-0 inline-block w-2 h-2 rounded-full"
            :class="priorityColor(item.priority)"
          ></span>

          <div class="flex-1 min-w-0">
            <!-- Top row: type badge + title -->
            <div class="flex items-center gap-1.5 mb-0.5">
              <span class="flex-shrink-0" :title="item.type">
                <component :is="typeIcon(item.type)" class="w-3.5 h-3.5" :class="typeIconColor(item.type)" />
              </span>
              <span class="text-sm font-medium text-gray-900 dark:text-white truncate">
                {{ item.title }}
              </span>
            </div>

            <!-- Bottom row: agent + time + status -->
            <div class="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
              <span class="truncate" :title="agentNameTooltip(agentsStore.agentRefForSlug(item.agent_name))">{{ item.agent_name }}</span>
              <span>&middot;</span>
              <span class="flex-shrink-0">{{ timeAgo(item.created_at) }}</span>
              <span v-if="item.status !== 'pending'" class="flex-shrink-0">
                &middot;
                <span
                  class="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium"
                  :class="statusBadge(item.status)"
                >
                  {{ item.status }}
                </span>
              </span>
            </div>
          </div>

          <!-- Priority label for critical/high -->
          <span
            v-if="item.priority === 'critical' || item.priority === 'high'"
            class="flex-shrink-0 text-xs font-medium px-1.5 py-0.5 rounded"
            :class="{
              'bg-status-danger-100 text-status-danger-700 dark:bg-status-danger-900/30 dark:text-status-danger-400': item.priority === 'critical',
              'bg-status-urgent-100 text-status-urgent-700 dark:bg-status-urgent-900/30 dark:text-status-urgent-400': item.priority === 'high'
            }"
          >
            {{ item.priority }}
          </span>
        </div>
      </button>
    </div>
  </div>
</template>

<script setup>
import { h } from 'vue'
import { useOperatorQueueStore } from '../../stores/operatorQueue'
import { useAgentsStore } from '../../stores/agents'
import { agentNameTooltip, agentOptionLabel } from '../../utils/agentName'

const store = useOperatorQueueStore()
const agentsStore = useAgentsStore()

function priorityColor(priority) {
  const colors = {
    critical: 'bg-status-danger-500 animate-pulse',
    high: 'bg-status-urgent-500',
    medium: 'bg-status-warning-500',
    low: 'bg-status-info-400'
  }
  return colors[priority] || 'bg-gray-400'
}

// Simple SVG icon components for each type
function typeIcon(type) {
  if (type === 'approval') {
    return {
      render() {
        return h('svg', { fill: 'none', stroke: 'currentColor', viewBox: '0 0 24 24', 'stroke-width': '2' }, [
          h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', d: 'M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z' })
        ])
      }
    }
  }
  if (type === 'question') {
    return {
      render() {
        return h('svg', { fill: 'none', stroke: 'currentColor', viewBox: '0 0 24 24', 'stroke-width': '2' }, [
          h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', d: 'M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z' })
        ])
      }
    }
  }
  // alert
  return {
    render() {
      return h('svg', { fill: 'none', stroke: 'currentColor', viewBox: '0 0 24 24', 'stroke-width': '2' }, [
        h('path', { 'stroke-linecap': 'round', 'stroke-linejoin': 'round', d: 'M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9' })
      ])
    }
  }
}

function typeIconColor(type) {
  const colors = {
    approval: 'text-accent-purple-500 dark:text-accent-purple-400',
    question: 'text-blue-500 dark:text-blue-400',
    alert: 'text-state-autonomous-500 dark:text-state-autonomous-400'
  }
  return colors[type] || 'text-gray-500'
}

function statusBadge(status) {
  const badges = {
    responded: 'bg-status-success-100 text-status-success-700 dark:bg-status-success-900/30 dark:text-status-success-400',
    acknowledged: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400',
    expired: 'bg-status-danger-100 text-status-danger-600 dark:bg-status-danger-900/30 dark:text-status-danger-400'
  }
  return badges[status] || ''
}

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
</script>
