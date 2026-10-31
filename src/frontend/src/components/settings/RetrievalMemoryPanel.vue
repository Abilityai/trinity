<template>
  <!--
    Retrieval memory (trinity-enterprise#1022, MEM-002) — the platform switch
    and its honest status. Off by default. The switch is a plain system setting
    (`trinity_memory_enabled`) written through the settings store; the status
    line comes from GET /api/settings/memory and says why memory cannot run
    when it cannot (no embedding model, no platform Anthropic key), rather
    than reporting an "on" that does nothing.
  -->
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg" data-testid="retrieval-memory-panel">
    <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
      <h2 class="text-lg font-medium text-gray-900 dark:text-white">Retrieval memory</h2>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Everything an agent does leaves facts behind — what people told it, what was decided,
        what it attempted and how it ended — and the ones relevant to the next request are
        put back in front of it. Facts stay in this instance's own database; extraction uses
        the platform Anthropic key. Off by default; each agent can be kept out. While on, the
        multilingual embedding model adds about 600 MB of RAM to each backend worker.
      </p>
    </div>

    <div class="p-6 space-y-4">
      <div v-if="error" class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ error }}</div>

      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0">
          <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Remember across runs</p>
          <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400" data-testid="retrieval-memory-status">
            <template v-if="view.state === 'loading'">Checking…</template>
            <template v-else-if="!status.enabled">
              Currently off — nothing is captured and nothing is recalled. Turning it on starts
              capturing from the next completed run.
            </template>
            <template v-else-if="!status.available">
              On, but unavailable — {{ status.reason || 'the embedding model did not load' }}.
            </template>
            <template v-else>
              On. {{ status.fact_count }} fact{{ status.fact_count === 1 ? '' : 's' }} stored
              <template v-if="status.last_retained_at">· last captured {{ fmt(status.last_retained_at) }}</template>
              <template v-else>· nothing captured yet</template>
              <template v-if="status.reason"> · {{ status.reason }}</template>
            </template>
          </p>
        </div>
        <BaseToggle
          :model-value="!!status.enabled"
          :disabled="saving || view.state === 'loading'"
          aria-label="Remember across runs"
          data-testid="retrieval-memory-toggle"
          @update:model-value="toggle"
        />
      </div>

      <p v-if="view.state !== 'loading' && status.enabled" class="text-xs text-gray-500 dark:text-gray-400">
        Model: <code class="rounded bg-gray-100 dark:bg-gray-750 px-1 py-0.5 text-gray-700 dark:text-gray-300">{{ status.embedding_model }}</code>
        · store: {{ status.backend }} · daily capture limit per agent: {{ status.daily_retain_limit }}
      </p>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import api from '../../api'
import BaseToggle from '../base/BaseToggle.vue'
import { useSettingsStore } from '../../stores/settings'
import { viewState } from '../../utils/loadingState'

const settingsStore = useSettingsStore()
const status = ref({ enabled: false, available: false, reason: null, fact_count: 0 })
const loading = ref(false)
const hasLoaded = ref(false)
const saving = ref(false)
const error = ref('')
// Loading means "no data yet" (design-system p13/p14): a refresh after the
// toggle swaps values in place and never replaces the status with a spinner.
const view = computed(() => viewState({ loading: loading.value, hasLoaded: hasLoaded.value, error: error.value || null }))

function fmt(iso) {
  try { return new Date(iso).toLocaleString() } catch { return iso }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await api.get('/api/settings/memory')
    status.value = data || status.value
    hasLoaded.value = true
  } catch (e) {
    error.value = 'Could not read the memory status.'
  } finally {
    loading.value = false
  }
}

async function toggle(next) {
  saving.value = true
  error.value = ''
  try {
    await settingsStore.updateSetting('trinity_memory_enabled', next ? 'true' : 'false')
    await load()
  } catch (e) {
    error.value = 'Could not change the memory switch.'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>
