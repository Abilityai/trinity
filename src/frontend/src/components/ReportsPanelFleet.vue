<template>
  <div>
    <!-- KPI tiles -->
    <div class="grid grid-cols-2 sm:grid-cols-3 gap-3 mb-4">
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2">
        <p class="text-[10px] uppercase tracking-wide text-gray-500">Total reports</p>
        <p class="text-base font-semibold text-gray-900 dark:text-gray-100">{{ store.stats?.total ?? '—' }}</p>
      </div>
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2">
        <p class="text-[10px] uppercase tracking-wide text-gray-500">Report types</p>
        <p class="text-base font-semibold text-gray-900 dark:text-gray-100">{{ typeCount }}</p>
      </div>
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2">
        <p class="text-[10px] uppercase tracking-wide text-gray-500">Agents reporting</p>
        <p class="text-base font-semibold text-gray-900 dark:text-gray-100">{{ store.stats?.agents ?? '—' }}</p>
      </div>
    </div>

    <!-- Filters -->
    <div class="flex flex-wrap items-center gap-2 mb-4">
      <select
        :value="store.filters.agent"
        class="text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-2 py-1"
        @change="store.setFilter('agent', $event.target.value)"
      >
        <option value="">All agents</option>
        <option v-for="name in agentNames" :key="name" :value="name">{{ name }}</option>
      </select>
      <select
        :value="store.filters.report_type"
        class="text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-2 py-1"
        @change="store.setFilter('report_type', $event.target.value)"
      >
        <option value="">All types</option>
        <option v-for="t in typeOptions" :key="t" :value="t">{{ t }}</option>
      </select>
      <select
        :value="store.filters.hours"
        class="text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-2 py-1"
        @change="store.setFilter('hours', Number($event.target.value))"
      >
        <option :value="24">24h</option>
        <option :value="168">7d</option>
        <option :value="720">30d</option>
        <option :value="0">All time</option>
      </select>
      <input
        :value="store.filters.search"
        type="search"
        placeholder="Search title / type / agent"
        class="text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-2 py-1 flex-1 min-w-[12rem]"
        @input="onSearch($event.target.value)"
      />
    </div>

    <p v-if="store.error" class="text-sm text-red-600 mb-3">{{ store.error }}</p>

    <div v-if="store.loading && store.reports.length === 0" class="text-sm text-gray-400">Loading…</div>
    <div v-else-if="store.reports.length === 0" class="text-sm text-gray-400">No reports match these filters.</div>

    <ul v-else class="space-y-2">
      <li
        v-for="report in store.reports"
        :key="report.id"
        class="border border-gray-200 dark:border-gray-700 rounded-lg"
      >
        <button
          class="w-full flex items-center justify-between gap-3 px-3 py-2 text-left"
          @click="store.toggleExpanded(report.id)"
        >
          <div class="min-w-0">
            <p class="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{{ report.title }}</p>
            <div class="flex items-center gap-2 mt-0.5">
              <router-link
                :to="'/agents/' + report.agent_name + '?tab=reports'"
                class="text-[11px] text-blue-600 dark:text-blue-400 hover:underline"
                @click.stop
              >{{ report.agent_name }}</router-link>
              <span class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300">{{ report.report_type }}</span>
              <span class="text-[11px] text-gray-400">{{ report.created_at }}</span>
            </div>
          </div>
          <span class="text-gray-400 text-xs shrink-0">{{ store.expandedId === report.id ? '▾' : '▸' }}</span>
        </button>
        <div v-if="store.expandedId === report.id" class="border-t border-gray-100 dark:border-gray-800 px-3 py-3">
          <div v-if="!store.payloads[report.id]" class="text-xs text-gray-400">Loading report…</div>
          <ReportRenderer
            v-else
            :report-type="report.report_type"
            :display-hint="report.display_hint"
            :payload="store.payloads[report.id].payload"
          />
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted } from 'vue'
import { useFleetReportsStore } from '../stores/reports'
import { useAgentsStore } from '../stores/agents'
import ReportRenderer from './reports/ReportRenderer.vue'

const store = useFleetReportsStore()
const agentsStore = useAgentsStore()

const agentNames = computed(() => (agentsStore.agents || []).map((a) => a.name).sort())
const typeOptions = computed(() => Object.keys(store.stats?.by_type || {}).sort())
const typeCount = computed(() => Object.keys(store.stats?.by_type || {}).length || '—')

let _searchTimer = null
function onSearch(value) {
  if (_searchTimer) clearTimeout(_searchTimer)
  _searchTimer = setTimeout(() => store.setFilter('search', value), 300)
}

onMounted(() => {
  store.setActive(true)
  store.refresh()
  if (!agentsStore.agents || agentsStore.agents.length === 0) {
    agentsStore.fetchAgents && agentsStore.fetchAgents()
  }
})
onUnmounted(() => {
  store.setActive(false)
  if (_searchTimer) clearTimeout(_searchTimer)
})
</script>
