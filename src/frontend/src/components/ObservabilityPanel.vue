<template>
  <div
    class="absolute bottom-4 left-4 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 p-4 max-w-md transition-all duration-300"
    :class="{ 'w-96': isExpanded, 'w-48': !isExpanded }"
  >
    <!-- Header -->
    <div class="flex items-center justify-between mb-3">
      <div class="flex items-center space-x-2">
        <div
          :class="[
            'w-2 h-2 rounded-full',
            observabilityStore.isOperational ? 'bg-status-success-500' : 'bg-gray-400'
          ]"
        ></div>
        <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">Observability</h3>
      </div>
      <button
        @click="isExpanded = !isExpanded"
        class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
      >
        <svg
          class="w-4 h-4 transition-transform"
          :class="{ 'rotate-180': isExpanded }"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
    </div>

    <!-- Collapsed: Summary only -->
    <div v-if="!isExpanded && observabilityStore.hasData" class="space-y-2">
      <div class="flex justify-between text-xs">
        <span class="text-gray-500 dark:text-gray-400">Cost</span>
        <span class="font-medium text-gray-900 dark:text-gray-100">{{ observabilityStore.formattedTotalCost }}</span>
      </div>
      <div class="flex justify-between text-xs">
        <span class="text-gray-500 dark:text-gray-400">Tokens</span>
        <span class="font-medium text-gray-900 dark:text-gray-100">{{ observabilityStore.formattedTotalTokens }}</span>
      </div>
    </div>

    <!-- Not enabled message -->
    <div v-if="!observabilityStore.enabled" class="text-xs text-gray-500 dark:text-gray-400">
      OTel not enabled. Set OTEL_ENABLED=1.
    </div>

    <!-- Enabled but not available -->
    <div v-else-if="!observabilityStore.available && observabilityStore.error" class="text-xs text-status-warning-600 dark:text-status-warning-400">
      {{ observabilityStore.error }}
    </div>

    <!-- Expanded: Full details -->
    <div v-else-if="isExpanded && observabilityStore.hasData" class="space-y-4">
      <!-- Cost Breakdown -->
      <div>
        <h4 class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">Cost by Model</h4>
        <div class="space-y-1">
          <div
            v-for="item in observabilityStore.costBreakdown"
            :key="item.model"
            class="flex justify-between text-xs"
          >
            <span class="text-gray-600 dark:text-gray-300">{{ item.model }}</span>
            <span class="font-medium text-gray-900 dark:text-gray-100">{{ item.formattedCost }}</span>
          </div>
          <div class="flex justify-between text-xs pt-1 border-t border-gray-200 dark:border-gray-700">
            <span class="font-medium text-gray-700 dark:text-gray-200">Total</span>
            <span class="font-bold text-gray-900 dark:text-gray-100">{{ observabilityStore.formattedTotalCost }}</span>
          </div>
        </div>
      </div>

      <!-- Token Breakdown -->
      <div>
        <h4 class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">Tokens by Type</h4>
        <div class="space-y-1">
          <div
            v-for="item in observabilityStore.tokensByType"
            :key="item.type"
            class="flex justify-between text-xs"
          >
            <span class="text-gray-600 dark:text-gray-300 capitalize">{{ item.type }}</span>
            <span class="font-medium text-gray-900 dark:text-gray-100">{{ item.formattedCount }}</span>
          </div>
          <div class="flex justify-between text-xs pt-1 border-t border-gray-200 dark:border-gray-700">
            <span class="font-medium text-gray-700 dark:text-gray-200">Total</span>
            <span class="font-bold text-gray-900 dark:text-gray-100">{{ observabilityStore.formattedTotalTokens }}</span>
          </div>
        </div>
      </div>

      <!-- Productivity Metrics -->
      <div>
        <h4 class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">Productivity</h4>
        <div class="grid grid-cols-2 gap-2">
          <div class="bg-gray-50 dark:bg-gray-700/50 rounded px-2 py-1.5">
            <div class="text-xs text-gray-500 dark:text-gray-400">Sessions</div>
            <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ observabilityStore.totals.sessions }}</div>
          </div>
          <div class="bg-gray-50 dark:bg-gray-700/50 rounded px-2 py-1.5">
            <div class="text-xs text-gray-500 dark:text-gray-400">Active Time</div>
            <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ observabilityStore.formattedActiveTime }}</div>
          </div>
          <div class="bg-gray-50 dark:bg-gray-700/50 rounded px-2 py-1.5">
            <div class="text-xs text-gray-500 dark:text-gray-400">Commits</div>
            <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ observabilityStore.totals.commits }}</div>
          </div>
          <div class="bg-gray-50 dark:bg-gray-700/50 rounded px-2 py-1.5">
            <div class="text-xs text-gray-500 dark:text-gray-400">PRs</div>
            <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ observabilityStore.totals.pull_requests }}</div>
          </div>
        </div>
      </div>

      <!-- Lines of Code -->
      <div v-if="observabilityStore.linesBreakdown.length > 0">
        <h4 class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">Lines of Code</h4>
        <div class="flex space-x-3">
          <div
            v-for="item in observabilityStore.linesBreakdown"
            :key="item.type"
            class="text-xs"
          >
            <span
              :class="[
                'font-medium',
                item.type === 'added' ? 'text-status-success-600 dark:text-status-success-400' :
                item.type === 'removed' ? 'text-status-danger-600 dark:text-status-danger-400' :
                'text-gray-600 dark:text-gray-400'
              ]"
            >
              {{ item.type === 'added' ? '+' : item.type === 'removed' ? '-' : '' }}{{ item.count }}
            </span>
            <span class="text-gray-500 dark:text-gray-400 ml-1">{{ item.type }}</span>
          </div>
        </div>
      </div>

      <!-- Last Updated -->
      <div class="text-xs text-gray-400 dark:text-gray-500 text-right">
        Updated {{ formatLastUpdated }}
      </div>
    </div>

    <!-- No data yet -->
    <div v-else-if="isExpanded && observabilityStore.isOperational && !observabilityStore.hasData" class="text-xs text-gray-500 dark:text-gray-400">
      No metrics data yet. Start chatting with agents to generate data.
    </div>

    <!-- ent#253: a refresh that failed with metrics on screen. The numbers
         stay (the store no longer discards them); this line says the reading is
         no longer live, so nothing here presents stale data as fresh. -->
    <p v-if="isStale" class="mt-2 text-xs text-status-warning-600 dark:text-status-warning-400">
      Couldn't refresh — showing the reading from {{ formatLastUpdated }}.
    </p>

    <!-- First load only (ent#253). This used to gate on `loading`, i.e. "a
         fetch is in flight", so the whole panel dimmed behind an overlay
         spinner on EVERY poll. A static placeholder rather than a bespoke
         spinner: this panel is a 192px floating chip, too small for the
         scanline primitive to read as anything but noise. -->
    <div v-if="firstLoad" class="absolute inset-0 bg-white/50 dark:bg-gray-800/50 flex items-center justify-center rounded-lg">
      <span class="text-xs text-gray-500 dark:text-gray-400">Loading…</span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useObservabilityStore } from '@/stores/observability'

const observabilityStore = useObservabilityStore()
const isExpanded = ref(false)

// "No data yet", never "fetch in flight" (#1927 / ent#253). Deliberately not
// `viewState()`: this panel's terminals are the store's own `enabled` /
// `available` verdicts rather than a row count, so only the loading half of
// the rule applies here.
const firstLoad = computed(() => observabilityStore.loading && !observabilityStore.hasLoaded)
const isStale = computed(() => Boolean(observabilityStore.refreshError) && observabilityStore.hasLoaded)

const formatLastUpdated = computed(() => {
  if (!observabilityStore.lastUpdated) return 'never'
  const diff = Date.now() - observabilityStore.lastUpdated.getTime()
  if (diff < 60000) return 'just now'
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
  return observabilityStore.lastUpdated.toLocaleTimeString()
})

onMounted(() => {
  observabilityStore.startPolling()
})

onUnmounted(() => {
  observabilityStore.stopPolling()
})
</script>
