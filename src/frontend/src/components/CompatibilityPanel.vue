<script setup>
/**
 * Agent compatibility section (#668) — rendered inside the Overview tab.
 *
 * Reuses the Overview "needs attention" idiom: a compact summary line (hidden
 * when clean unless expanded) that opens to the full checklist grouped by
 * category. STATIC checks are fetched first for instant paint; AI checks load
 * asynchronously (cache-backed by the results table, so they show on every
 * visit). Auto-fixable (gitignore) checks get a one-click Fix button.
 */
import { ref, computed, watch, onMounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { renderMarkdown } from '../utils/markdown'

const props = defineProps({
  agent: { type: Object, required: true },
})

const agentsStore = useAgentsStore()
const agentName = computed(() => props.agent?.name)

const report = ref(null)
const loading = ref(false)
const aiLoading = ref(false)
const expanded = ref(false)
const error = ref(null)
const fixing = ref({})        // check_id -> bool
const fixMessage = ref(null)  // { text, ok }

const failing = computed(() => (report.value?.checks || []).filter((c) => c.status === 'fail'))
const hardCount = computed(() => report.value?.hard_count || 0)
const softCount = computed(() => report.value?.soft_count || 0)
const infoCount = computed(() => report.value?.info_count || 0)
const issueCount = computed(() => hardCount.value + softCount.value)
const isUnavailable = computed(() => report.value?.overall_status === 'unavailable')

// Group all checks by category for the expanded checklist, in catalog order.
const grouped = computed(() => {
  const out = []
  const seen = {}
  for (const c of report.value?.checks || []) {
    if (!seen[c.category]) {
      seen[c.category] = { category: c.category, checks: [] }
      out.push(seen[c.category])
    }
    seen[c.category].checks.push(c)
  }
  return out
})

const summaryTone = computed(() => {
  if (isUnavailable.value) return 'gray'
  if (hardCount.value > 0) return 'danger'
  if (softCount.value > 0) return 'warning'
  return 'success'
})

function fmtTime(iso) {
  if (!iso) return null
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

async function load({ includeAi = false } = {}) {
  const name = agentName.value
  if (!name) return
  error.value = null
  if (includeAi) aiLoading.value = true
  else loading.value = true
  try {
    report.value = await agentsStore.getCompatibility(name, { includeAi })
  } catch (e) {
    error.value = e?.response?.data?.detail || 'Failed to load compatibility report'
  } finally {
    loading.value = false
    aiLoading.value = false
  }
}

// Two-phase: STATIC first (instant), then auto-run AI once if it was never run.
async function loadInitial() {
  await load({ includeAi: false })
  if (report.value && report.value.container_running && !report.value.ai_ran_at) {
    load({ includeAi: true })
  }
}

async function rerun() {
  fixMessage.value = null
  await load({ includeAi: true })
}

async function applyFix(checkId) {
  const name = agentName.value
  if (!name) return
  fixing.value = { ...fixing.value, [checkId]: true }
  fixMessage.value = null
  try {
    const res = await agentsStore.fixCompatibilityIssue(name, checkId)
    fixMessage.value = { text: res.message, ok: res.fixed }
    // Refresh STATIC checks to reflect the fix (AI verdicts come from cache).
    await load({ includeAi: false })
  } catch (e) {
    fixMessage.value = { text: e?.response?.data?.detail || 'Fix failed', ok: false }
  } finally {
    const next = { ...fixing.value }
    delete next[checkId]
    fixing.value = next
  }
}

function statusIcon(c) {
  if (c.status === 'pass') return 'pass'
  if (c.status === 'skipped') return 'skip'
  return c.severity // hard | soft | info  → colored fail
}

onMounted(loadInitial)
watch(() => agentName.value, loadInitial)
</script>

<template>
  <div v-if="report || loading">
    <!-- Summary line (compact; reuses the Overview needs-attention idiom) -->
    <button
      class="w-full flex items-center justify-between px-4 py-3 rounded-lg border transition-colors text-left"
      :class="{
        'bg-status-danger-50 dark:bg-status-danger-900/30 border-status-danger-200 dark:border-status-danger-800 hover:bg-status-danger-100 dark:hover:bg-status-danger-900/50': summaryTone === 'danger',
        'bg-status-warning-50 dark:bg-status-warning-900/30 border-status-warning-200 dark:border-status-warning-800 hover:bg-status-warning-100 dark:hover:bg-status-warning-900/50': summaryTone === 'warning',
        'bg-status-success-50 dark:bg-status-success-900/20 border-status-success-200 dark:border-status-success-800 hover:bg-status-success-100 dark:hover:bg-status-success-900/40': summaryTone === 'success',
        'bg-gray-50 dark:bg-gray-800/60 border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800': summaryTone === 'gray',
      }"
      @click="expanded = !expanded"
    >
      <span class="flex items-center text-sm font-medium"
        :class="{
          'text-status-danger-800 dark:text-status-danger-300': summaryTone === 'danger',
          'text-status-warning-800 dark:text-status-warning-300': summaryTone === 'warning',
          'text-status-success-800 dark:text-status-success-300': summaryTone === 'success',
          'text-gray-700 dark:text-gray-300': summaryTone === 'gray',
        }"
      >
        <svg class="w-5 h-5 mr-2 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <template v-if="loading && !report">Checking agent compatibility…</template>
        <template v-else-if="isUnavailable">Compatibility — {{ report?.message || 'unavailable' }}</template>
        <template v-else-if="issueCount === 0">Compatible — all checks passing</template>
        <template v-else>
          {{ issueCount }} compatibility {{ issueCount === 1 ? 'issue' : 'issues' }}
          <span v-if="hardCount > 0" class="ml-1 text-status-danger-700 dark:text-status-danger-400">({{ hardCount }} must-fix)</span>
        </template>
      </span>
      <span class="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
        <span v-if="aiLoading">analyzing…</span>
        <span>{{ expanded ? 'Hide' : 'Details' }} {{ expanded ? '▲' : '▼' }}</span>
      </span>
    </button>

    <!-- Expanded checklist -->
    <div v-if="expanded" class="mt-2 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
      <div class="flex items-center justify-between px-4 py-2 border-b border-gray-100 dark:border-gray-700">
        <span class="text-xs text-gray-500 dark:text-gray-400">
          <template v-if="report?.ai_ran_at">AI checks last run {{ fmtTime(report.ai_ran_at) }}</template>
          <template v-else>AI checks not yet run</template>
        </span>
        <button
          class="text-xs font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline disabled:opacity-50"
          :disabled="aiLoading || !report?.container_running"
          @click="rerun"
        >{{ aiLoading ? 'Running…' : 'Re-run analysis' }}</button>
      </div>

      <p v-if="error" class="px-4 py-2 text-sm text-status-danger-600 dark:text-status-danger-400">{{ error }}</p>
      <p v-if="fixMessage" class="px-4 py-2 text-sm"
         :class="fixMessage.ok ? 'text-status-success-600 dark:text-status-success-400' : 'text-status-danger-600 dark:text-status-danger-400'">
        {{ fixMessage.text }}
      </p>

      <div v-for="grp in grouped" :key="grp.category" class="border-b border-gray-100 dark:border-gray-700 last:border-0">
        <h4 class="px-4 pt-3 pb-1 text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">{{ grp.category }}</h4>
        <ul>
          <li v-for="c in grp.checks" :key="c.check_id" class="px-4 py-2 flex items-start gap-3">
            <!-- status icon -->
            <span class="mt-0.5 shrink-0">
              <svg v-if="statusIcon(c) === 'pass'" class="w-4 h-4 text-status-success-500" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M16.7 5.3a1 1 0 010 1.4l-8 8a1 1 0 01-1.4 0l-4-4a1 1 0 011.4-1.4l3.3 3.3 7.3-7.3a1 1 0 011.4 0z" clip-rule="evenodd" /></svg>
              <svg v-else-if="statusIcon(c) === 'skip'" class="w-4 h-4 text-gray-400" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M4 10a1 1 0 011-1h10a1 1 0 110 2H5a1 1 0 01-1-1z" clip-rule="evenodd" /></svg>
              <svg v-else class="w-4 h-4" :class="c.severity === 'hard' ? 'text-status-danger-500' : (c.severity === 'soft' ? 'text-status-warning-500' : 'text-gray-400')" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM9 9a1 1 0 012 0v4a1 1 0 11-2 0V9zm1-5a1 1 0 100 2 1 1 0 000-2z" clip-rule="evenodd" /></svg>
            </span>
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="text-xs font-mono text-gray-400 dark:text-gray-500">{{ c.check_id }}</span>
                <span class="text-sm text-gray-800 dark:text-gray-200">{{ c.message }}</span>
                <span v-if="c.status === 'fail'" class="text-[10px] uppercase font-semibold px-1.5 py-0.5 rounded"
                  :class="c.severity === 'hard' ? 'bg-status-danger-100 text-status-danger-700 dark:bg-status-danger-900/40 dark:text-status-danger-300' : (c.severity === 'soft' ? 'bg-status-warning-100 text-status-warning-700 dark:bg-status-warning-900/40 dark:text-status-warning-300' : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300')">{{ c.severity }}</span>
                <span v-if="c.type === 'ai'" class="text-[10px] uppercase font-semibold px-1.5 py-0.5 rounded bg-action-primary-50 text-action-primary-600 dark:bg-action-primary-900/30 dark:text-action-primary-400">AI</span>
              </div>
              <div
                v-if="c.explanation && c.status === 'fail'"
                class="mt-1 text-xs text-gray-500 dark:text-gray-400 prose-xs dark:prose-invert max-w-none"
                v-html="renderMarkdown(c.explanation)"
              ></div>
              <p v-else-if="c.status === 'skipped' && c.skip_reason === 'no_api_key'" class="mt-0.5 text-xs text-gray-400 dark:text-gray-500">
                Skipped — no Anthropic API key configured
              </p>
            </div>
            <!-- Fix button for auto-fixable failures -->
            <button
              v-if="c.auto_fixable && c.status === 'fail'"
              class="shrink-0 text-xs font-medium px-2 py-1 rounded bg-action-primary-600 hover:bg-action-primary-700 text-white disabled:opacity-50"
              :disabled="fixing[c.check_id]"
              @click="applyFix(c.check_id)"
            >{{ fixing[c.check_id] ? 'Fixing…' : 'Fix' }}</button>
          </li>
        </ul>
      </div>
    </div>
  </div>
</template>
