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
        put back in front of it. Facts stay in this instance's own database; extraction runs
        on the agent's own subscription or the platform Anthropic key (below). Off by default; each
        agent can be kept out. While on, the
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

      <!--
        trinity-enterprise#1027: where the extraction call runs. Editable while
        the platform switch is off (the value is saved and applies once it is
        on). The status slot is always rendered at one line so changing the
        option never moves the row (principle 29).
      -->
      <div class="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-4" data-testid="retrieval-memory-extractor-row">
        <div class="min-w-0 sm:flex-1">
          <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Extraction runs on</p>
          <p class="mt-0.5 text-[12.5px] text-gray-500 dark:text-gray-400">
            Facts are extracted by a small model call after each run — the same door the conversation summaries and thread titles use. On the agent's subscription it
            uses the agent's own subscription token, directly from the platform; the platform key uses the
            instance's Anthropic key.<template v-if="view.state !== 'loading' && !status.enabled"> Saved now, applies once the switch above is on.</template>
          </p>
          <p class="mt-1 min-h-[18px] text-[12.5px]" :class="extractorStatus.warn ? 'text-status-warning-700 dark:text-status-warning-400' : 'text-gray-500 dark:text-gray-400'" data-testid="retrieval-memory-extractor-status">{{ extractorStatus.text }}</p>
          <InlineError
            v-if="extractorError"
            class="mt-2"
            :message="extractorError"
            @dismiss="extractorError = ''"
          />
        </div>
        <BaseSelect
          :model-value="status.credential_mode || 'auto'"
          :disabled="savingExtractor || view.state === 'loading'"
          size="sm"
          aria-label="Extraction runs on"
          data-testid="retrieval-memory-extractor"
          @update:model-value="setExtractor"
        >
          <option value="auto">The agent's subscription when it has one, else the platform key</option>
          <option value="subscription">Subscriptions only</option>
          <option value="platform_key">Platform API key only</option>
        </BaseSelect>
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
import BaseSelect from '../base/BaseSelect.vue'
import InlineError from '../InlineError.vue'
import { useSettingsStore } from '../../stores/settings'
import { viewState } from '../../utils/loadingState'

const settingsStore = useSettingsStore()
const status = ref({ enabled: false, available: false, reason: null, fact_count: 0, credential_mode: 'auto', platform_key: false })
const loading = ref(false)
const hasLoaded = ref(false)
const saving = ref(false)
const savingExtractor = ref(false)
const error = ref('')
const extractorError = ref('')

// Honest status for the extractor choice (ent#1027): the one case that
// captures nothing is said in the warning tier; the slot is always one line.
const extractorStatus = computed(() => {
  if (view.value.state === 'loading') return { text: '', warn: false }
  const mode = status.value.credential_mode || 'auto'
  if (mode === 'platform_key' && !status.value.platform_key) return { text: 'No platform key — nothing is captured', warn: true }
  if (mode === 'subscription') return { text: 'Only agents on a subscription are covered; nothing is captured for the rest', warn: false }
  if (mode === 'platform_key') return { text: 'Every call uses the platform key', warn: false }
  return { text: status.value.platform_key ? 'Agents on a subscription use it; the platform key covers the rest' : 'Agents on a subscription use it; no platform key to fall back on', warn: false }
})
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

async function setExtractor(value) {
  savingExtractor.value = true
  extractorError.value = ''
  try {
    await settingsStore.updateSetting('trinity_model_credential', value)
    await load()
  } catch (e) {
    extractorError.value = 'Could not change where extraction runs. The saved choice is shown.'
    await load()
  } finally {
    savingExtractor.value = false
  }
}

onMounted(load)
</script>
