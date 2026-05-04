<template>
  <div class="p-6 space-y-6">
    <!-- Loading State -->
    <div v-if="loading" class="text-center py-8">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-500 mx-auto"></div>
      <p class="text-gray-500 dark:text-gray-400 mt-2">Loading payment config...</p>
    </div>

    <template v-else>
      <!-- Configuration Section -->
      <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
        <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <div>
            <h3 class="text-lg font-medium text-gray-900 dark:text-white">Nevermined x402 Payments</h3>
            <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
              Configure per-request monetization via the x402 payment protocol
            </p>
          </div>
          <!-- Enable/Disable Toggle -->
          <div v-if="config" class="flex items-center space-x-2">
            <span class="text-sm text-gray-500 dark:text-gray-400">{{ config.enabled ? 'Enabled' : 'Disabled' }}</span>
            <button
              @click="toggleEnabled"
              :disabled="toggling || !canEdit"
              :class="[
                'relative inline-flex h-6 w-11 flex-shrink-0 rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none',
                canEdit ? 'cursor-pointer' : 'cursor-not-allowed opacity-50',
                config.enabled ? 'bg-status-success-500' : 'bg-gray-300 dark:bg-gray-600'
              ]"
            >
              <span
                :class="[
                  'pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out',
                  config.enabled ? 'translate-x-5' : 'translate-x-0'
                ]"
              />
            </button>
          </div>
        </div>

        <div class="p-4 space-y-4">
          <!-- Read-only notice for shared users -->
          <div v-if="!canEdit" class="bg-status-warning-50 dark:bg-status-warning-900/20 border border-status-warning-200 dark:border-status-warning-800 rounded-lg p-3">
            <p class="text-sm text-status-warning-800 dark:text-status-warning-300">
              You have view-only access to this agent's payment configuration. Only the agent owner can modify settings.
            </p>
          </div>

          <!-- Paid Endpoint URL (show when configured) -->
          <div v-if="config && config.enabled" class="bg-status-success-50 dark:bg-status-success-900/20 border border-status-success-200 dark:border-status-success-800 rounded-lg p-3">
            <label class="block text-xs font-medium text-status-success-700 dark:text-status-success-400 mb-1">Paid Endpoint</label>
            <div class="flex items-center space-x-2">
              <code class="flex-1 text-sm text-status-success-800 dark:text-status-success-300 break-all">{{ paidEndpointUrl }}</code>
              <button
                @click="copyEndpoint"
                class="text-status-success-600 hover:text-status-success-700 dark:text-status-success-400 dark:hover:text-status-success-300 flex-shrink-0"
                title="Copy to clipboard"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                </svg>
              </button>
            </div>
          </div>

          <!-- Configuration Form -->
          <form @submit.prevent="saveConfig" class="space-y-4">
            <!-- API Key -->
            <div>
              <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">NVM API Key</label>
              <div class="relative">
                <input
                  v-model="form.nvm_api_key"
                  :type="showApiKey ? 'text' : 'password'"
                  :disabled="!canEdit"
                  placeholder="sandbox:eyJhbGci..."
                  :class="['w-full px-3 py-2 pr-10 border border-gray-300 dark:border-gray-600 rounded-md text-sm focus:ring-indigo-500 focus:border-indigo-500', canEdit ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white' : 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 cursor-not-allowed']"
                />
                <button
                  type="button"
                  @click="showApiKey = !showApiKey"
                  class="absolute inset-y-0 right-0 pr-3 flex items-center text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                >
                  <svg v-if="showApiKey" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.878 9.878L6.99 7.09m8.132 8.132l2.88 2.88M3 3l18 18" />
                  </svg>
                  <svg v-else class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                  </svg>
                </button>
              </div>
              <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">From nevermined.app Settings. Format: env:jwt</p>
            </div>

            <!-- Environment -->
            <div>
              <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Environment</label>
              <select
                v-model="form.nvm_environment"
                :disabled="!canEdit"
                :class="['w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm focus:ring-indigo-500 focus:border-indigo-500', canEdit ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white' : 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 cursor-not-allowed']"
              >
                <option value="sandbox">Sandbox (testnet)</option>
                <option value="live">Live (mainnet)</option>
                <option value="staging_sandbox">Staging Sandbox</option>
                <option value="staging_live">Staging Live</option>
                <option value="custom">Custom</option>
              </select>
            </div>

            <!-- Agent ID and Plan ID side by side -->
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Agent ID</label>
                <input
                  v-model="form.nvm_agent_id"
                  type="text"
                  :disabled="!canEdit"
                  placeholder="304071263598..."
                  :class="['w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm focus:ring-indigo-500 focus:border-indigo-500', canEdit ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white' : 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 cursor-not-allowed']"
                />
              </div>
              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Plan ID</label>
                <input
                  v-model="form.nvm_plan_id"
                  type="text"
                  :disabled="!canEdit"
                  placeholder="828208014058..."
                  :class="['w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm focus:ring-indigo-500 focus:border-indigo-500', canEdit ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white' : 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 cursor-not-allowed']"
                />
              </div>
            </div>

            <!-- Credits per Request -->
            <div>
              <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Credits per Request</label>
              <input
                v-model.number="form.credits_per_request"
                type="number"
                min="1"
                :disabled="!canEdit"
                :class="['w-32 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm focus:ring-indigo-500 focus:border-indigo-500', canEdit ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white' : 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 cursor-not-allowed']"
              />
            </div>

            <!-- Actions (owner/admin only) -->
            <div v-if="canEdit" class="flex items-center space-x-3 pt-2">
              <button
                type="submit"
                :disabled="saving || !isFormValid"
                class="px-4 py-2 bg-indigo-600 text-white text-sm font-medium rounded-md hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {{ saving ? 'Saving...' : (config ? 'Update Configuration' : 'Save Configuration') }}
              </button>
              <button
                v-if="config"
                type="button"
                @click="deleteConfig"
                :disabled="deleting"
                class="px-4 py-2 bg-status-danger-600 text-white text-sm font-medium rounded-md hover:bg-status-danger-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {{ deleting ? 'Removing...' : 'Remove' }}
              </button>
            </div>
          </form>
        </div>
      </div>

      <!-- Payment Log Section -->
      <div v-if="config" class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
        <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <h3 class="text-lg font-medium text-gray-900 dark:text-white">Payment Log</h3>
          <button
            @click="loadPaymentLog"
            class="text-sm text-indigo-600 hover:text-indigo-700 dark:text-indigo-400 dark:hover:text-indigo-300"
          >
            Refresh
          </button>
        </div>

        <div v-if="paymentLog.length === 0" class="p-4 text-center text-gray-500 dark:text-gray-400 text-sm">
          No payment activity yet
        </div>

        <div v-else class="overflow-x-auto">
          <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
            <thead class="bg-gray-50 dark:bg-gray-900/50">
              <tr>
                <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Time</th>
                <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Action</th>
                <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Subscriber</th>
                <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Credits</th>
                <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Tx Hash</th>
                <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Status</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
              <tr v-for="entry in paymentLog" :key="entry.id" class="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                <td class="px-4 py-2 text-sm text-gray-600 dark:text-gray-300 whitespace-nowrap">{{ formatTime(entry.created_at) }}</td>
                <td class="px-4 py-2">
                  <span :class="actionBadgeClass(entry.action)" class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium">
                    {{ entry.action }}
                  </span>
                </td>
                <td class="px-4 py-2 text-sm text-gray-600 dark:text-gray-300 font-mono">{{ truncateAddress(entry.subscriber_address) }}</td>
                <td class="px-4 py-2 text-sm text-gray-600 dark:text-gray-300">{{ entry.credits_amount ?? '-' }}</td>
                <td class="px-4 py-2 text-sm text-gray-600 dark:text-gray-300 font-mono">{{ truncateHash(entry.tx_hash) }}</td>
                <td class="px-4 py-2">
                  <span v-if="entry.success" class="text-status-success-600 dark:text-status-success-400 text-sm">OK</span>
                  <span v-else class="text-status-danger-600 dark:text-status-danger-400 text-sm" :title="entry.error">Failed</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import api from '../api'
import { useNotification } from '../composables'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  canEdit: {
    type: Boolean,
    default: true
  }
})

const { showNotification } = useNotification()

// State
const loading = ref(false)
const saving = ref(false)
const deleting = ref(false)
const toggling = ref(false)
const showApiKey = ref(false)
const config = ref(null)
const paymentLog = ref([])

const form = ref({
  nvm_api_key: '',
  nvm_environment: 'sandbox',
  nvm_agent_id: '',
  nvm_plan_id: '',
  credits_per_request: 1
})

// Computed
const paidEndpointUrl = computed(() => {
  const base = window.location.origin
  return `${base}/api/paid/${props.agentName}/chat`
})

const isFormValid = computed(() => {
  return form.value.nvm_api_key &&
    form.value.nvm_environment &&
    form.value.nvm_agent_id &&
    form.value.nvm_plan_id &&
    form.value.credits_per_request >= 1
})

// Methods
async function loadConfig() {
  loading.value = true
  try {
    const { data } = await api.get(`/api/nevermined/agents/${props.agentName}/config`)
    config.value = data
    // Populate form (API key field stays empty — it's encrypted server-side)
    form.value.nvm_environment = data.nvm_environment
    form.value.nvm_agent_id = data.nvm_agent_id
    form.value.nvm_plan_id = data.nvm_plan_id
    form.value.credits_per_request = data.credits_per_request
    // Load payment log when config exists
    await loadPaymentLog()
  } catch (err) {
    if (err.response?.status !== 404) {
      showNotification('Failed to load Nevermined config', 'error')
    }
    config.value = null
  } finally {
    loading.value = false
  }
}

async function saveConfig() {
  if (!isFormValid.value) return
  saving.value = true
  try {
    const { data } = await api.post(`/api/nevermined/agents/${props.agentName}/config`, form.value)
    config.value = data
    form.value.nvm_api_key = '' // Clear after save
    showNotification('Nevermined configuration saved', 'success')
    await loadPaymentLog()
  } catch (err) {
    showNotification(err.response?.data?.detail || 'Failed to save config', 'error')
  } finally {
    saving.value = false
  }
}

async function deleteConfig() {
  if (!confirm('Remove Nevermined payment configuration? This will disable paid access.')) return
  deleting.value = true
  try {
    await api.delete(`/api/nevermined/agents/${props.agentName}/config`)
    config.value = null
    paymentLog.value = []
    form.value = { nvm_api_key: '', nvm_environment: 'sandbox', nvm_agent_id: '', nvm_plan_id: '', credits_per_request: 1 }
    showNotification('Nevermined configuration removed', 'success')
  } catch (err) {
    showNotification(err.response?.data?.detail || 'Failed to remove config', 'error')
  } finally {
    deleting.value = false
  }
}

async function toggleEnabled() {
  if (!config.value) return
  toggling.value = true
  const newState = !config.value.enabled
  try {
    await api.put(`/api/nevermined/agents/${props.agentName}/config/toggle?enabled=${newState}`)
    config.value.enabled = newState
    showNotification(`Payments ${newState ? 'enabled' : 'disabled'}`, 'success')
  } catch (err) {
    showNotification(err.response?.data?.detail || 'Failed to toggle', 'error')
  } finally {
    toggling.value = false
  }
}

async function loadPaymentLog() {
  try {
    const { data } = await api.get(`/api/nevermined/agents/${props.agentName}/payments`)
    paymentLog.value = data
  } catch {
    // Silently fail — log is supplementary
  }
}

function copyEndpoint() {
  navigator.clipboard.writeText(paidEndpointUrl.value)
  showNotification('Endpoint URL copied', 'success')
}

function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function truncateAddress(addr) {
  if (!addr) return '-'
  if (addr.length <= 12) return addr
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`
}

function truncateHash(hash) {
  if (!hash) return '-'
  if (hash.length <= 14) return hash
  return `${hash.slice(0, 8)}...${hash.slice(-4)}`
}

function actionBadgeClass(action) {
  const classes = {
    verify: 'bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300',
    settle: 'bg-status-success-100 text-status-success-700 dark:bg-status-success-900/50 dark:text-status-success-300',
    settle_failed: 'bg-status-danger-100 text-status-danger-700 dark:bg-status-danger-900/50 dark:text-status-danger-300',
    reject: 'bg-status-warning-100 text-status-warning-700 dark:bg-status-warning-900/50 dark:text-status-warning-300',
  }
  return classes[action] || 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300'
}

// Lifecycle
onMounted(() => {
  loadConfig()
})

watch(() => props.agentName, () => {
  loadConfig()
})
</script>
