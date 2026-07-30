<template>
  <!-- ent#277. Gated on the entitlement: in an OSS-only build (or an unentitled
       enterprise build) `enterprise_features` omits the id and this renders
       nothing at all — no dead toggle promising a feature the backend 404s. -->
  <div v-if="entitled" class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-5">
    <div class="flex items-start justify-between gap-4">
      <div>
        <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">Cross-model validation</h3>
        <p class="mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-xl">
          After a validated execution, have a <strong>different</strong> model review the result.
          The referee is called directly by Trinity with only the task and the output — it gets no
          workspace, no tools and no session, so it cannot be steered by the agent it is judging.
        </p>
      </div>
      <label class="shrink-0 inline-flex items-center gap-2 cursor-pointer">
        <input type="checkbox" v-model="form.enabled" class="rounded text-action-primary-600 focus:ring-action-primary-500" />
        <span class="text-sm text-gray-700 dark:text-gray-300">Enabled</span>
      </label>
    </div>

    <!-- The latency/cost tradeoff is stated HERE, at configuration time, rather
         than discovered from a bill later (explicit AC on ent#277). -->
    <p v-if="config?.latency_note" class="mt-3 text-xs text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900/40 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2">
      {{ config.latency_note }}
    </p>

    <div class="mt-4 grid grid-cols-1 sm:grid-cols-3 gap-3">
      <div>
        <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Provider</label>
        <select v-model="form.provider" class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 text-sm px-3 py-2 focus:outline-none">
          <option :value="null">—</option>
          <option v-for="p in config?.available_providers || []" :key="p" :value="p">{{ p }}</option>
        </select>
      </div>
      <div>
        <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Referee model</label>
        <input v-model="form.model" type="text" placeholder="claude-haiku-4-5"
               class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 text-sm px-3 py-2 focus:outline-none" />
      </div>
      <div>
        <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">
          Base URL <span class="text-gray-400">(optional)</span>
        </label>
        <input v-model="form.base_url" type="text" placeholder="self-hosted / compatible endpoint"
               class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 text-sm px-3 py-2 focus:outline-none" />
      </div>
    </div>

    <div class="mt-3">
      <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">
        API key
        <!-- Never render the stored key: the endpoint only reports whether one
             resolves. This line is the whole read surface for the credential. -->
        <span v-if="config?.key_configured" class="ml-1 text-status-success-600 dark:text-status-success-400">— configured</span>
        <span v-else class="ml-1 text-status-warning-600 dark:text-status-warning-400">— not set (falls back to the platform key for this provider)</span>
      </label>
      <input v-model="form.api_key" type="password" placeholder="leave blank to keep the current key"
             class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 text-sm px-3 py-2 focus:outline-none" />
    </div>

    <div class="mt-4 flex items-center gap-3">
      <button @click="save" :disabled="saving"
              class="px-3 py-1.5 rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm disabled:opacity-50">
        {{ saving ? 'Saving…' : 'Save' }}
      </button>
      <span v-if="error" class="text-xs text-status-danger-600 dark:text-status-danger-400">{{ error }}</span>
      <span v-else-if="saved" class="text-xs text-status-success-600 dark:text-status-success-400">Saved</span>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import api from '../api'
import { useEnterpriseStore } from '../stores/enterprise'

const props = defineProps({ agentName: { type: String, required: true } })

const enterprise = useEnterpriseStore()
const entitled = computed(() => enterprise.isEntitled('cross_model_validation'))

const config = ref(null)
const saving = ref(false)
const saved = ref(false)
const error = ref('')
const form = reactive({ enabled: false, provider: null, model: '', base_url: '', api_key: '' })

const BASE = '/api/enterprise/cross-model-validation'

async function load() {
  if (!entitled.value) return
  try {
    const { data } = await api.get(`${BASE}/agents/${props.agentName}/config`)
    config.value = data
    Object.assign(form, {
      enabled: data.enabled,
      provider: data.provider,
      model: data.model || '',
      base_url: data.base_url || '',
      api_key: '',
    })
  } catch (e) {
    error.value = e?.response?.data?.detail || 'Could not load configuration'
  }
}

async function save() {
  saving.value = true
  saved.value = false
  error.value = ''
  try {
    const payload = {
      enabled: form.enabled,
      provider: form.provider,
      model: form.model || null,
      base_url: form.base_url || null,
    }
    // Blank means "leave the stored key alone" — sending null would clear it,
    // so an operator toggling `enabled` can't accidentally wipe the credential.
    if (form.api_key) payload.api_key = form.api_key
    const { data } = await api.put(`${BASE}/agents/${props.agentName}/config`, payload)
    config.value = data
    form.api_key = ''
    saved.value = true
  } catch (e) {
    error.value = e?.response?.data?.detail || 'Save failed'
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  await enterprise.loadFeatureFlags()
  await load()
})
</script>
