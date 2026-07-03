<template>
  <div class="p-6">
    <div class="flex items-center justify-between mb-4">
      <div>
        <h2 class="text-lg font-semibold text-gray-900 dark:text-gray-100">Reports</h2>
        <p class="text-xs text-gray-500">Structured reports this agent has published.</p>
      </div>
      <button
        class="text-xs px-2.5 py-1 rounded border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
        :disabled="store.loading"
        @click="store.fetchReports()"
      >Refresh</button>
    </div>

    <p v-if="store.error" class="text-sm text-red-600 mb-3">{{ store.error }}</p>

    <div v-if="store.loading && store.reports.length === 0" class="text-sm text-gray-400">Loading…</div>
    <div v-else-if="store.reports.length === 0" class="text-sm text-gray-400">
      No reports yet. Agents publish reports via the <code>report</code> MCP tool.
    </div>

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
              <span class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300">{{ report.report_type }}</span>
              <span class="text-[11px] text-gray-400">{{ report.created_at }}</span>
            </div>
          </div>
          <span class="text-gray-400 text-xs shrink-0">{{ store.expandedId === report.id ? '▾' : '▸' }}</span>
        </button>

        <div v-if="store.expandedId === report.id" class="border-t border-gray-100 dark:border-gray-800 px-3 py-3">
          <div v-if="report.period_start || report.period_end" class="text-[11px] text-gray-400 mb-2">
            Period: {{ report.period_start || '…' }} → {{ report.period_end || '…' }}
          </div>
          <div v-if="!store.payloads[report.id]" class="text-xs text-gray-400">Loading report…</div>
          <ReportRenderer
            v-else
            :report-type="report.report_type"
            :display-hint="report.display_hint"
            :payload="store.payloads[report.id].payload"
          />
          <div v-if="canDelete" class="mt-3 text-right">
            <button
              class="text-xs px-2 py-1 rounded border border-red-300 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20"
              @click="store.deleteReport(report.id)"
            >Delete</button>
          </div>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, watch } from 'vue'
import { useReportsStore } from '../stores/reports'
import ReportRenderer from './reports/ReportRenderer.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  canDelete: { type: Boolean, default: false },
})

const store = useReportsStore()

function load() {
  store.setAgent(props.agentName)
  store.fetchReports()
}

onMounted(load)
watch(() => props.agentName, load)
onUnmounted(() => store.clearAgent())
</script>
