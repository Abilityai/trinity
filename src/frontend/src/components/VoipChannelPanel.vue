<template>
  <div>
    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Voice Calls (Twilio / VoIP)</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      Connect a Twilio voice sender so this agent can place outbound phone calls.
      Each agent brings its own Twilio account. Outbound only.
    </p>

    <!-- Loading -->
    <div v-if="loading" class="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
      <svg class="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
      </svg>
      Loading...
    </div>

    <!-- Access Denied -->
    <div v-else-if="accessDenied" class="text-sm text-gray-500 dark:text-gray-400">
      Only the agent owner can manage voice-call settings.
    </div>

    <!-- Connected State -->
    <div v-else-if="binding.configured" class="space-y-3">
      <div class="p-4 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-gray-200 dark:border-gray-700">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-3">
            <span
              class="inline-block w-2.5 h-2.5 rounded-full"
              :class="binding.enabled ? 'bg-status-success-500' : 'bg-gray-400 dark:bg-gray-600'"
            ></span>
            <div>
              <p class="text-sm font-medium text-gray-900 dark:text-white">
                {{ binding.from_number }}
                <span
                  v-if="!binding.enabled"
                  class="ml-2 px-1.5 py-0.5 text-xs rounded bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-300"
                >Disabled</span>
              </p>
              <p class="text-xs text-gray-500 dark:text-gray-400">
                AccountSid: {{ truncatedSid }}
                <span v-if="binding.display_name"> · {{ binding.display_name }}</span>
              </p>
            </div>
          </div>
          <div class="flex items-center gap-2">
            <button
              @click="toggleEnabled"
              :disabled="toggling"
              class="text-sm text-action-primary-600 dark:text-action-primary-400 hover:text-action-primary-800 dark:hover:text-action-primary-300 disabled:opacity-50"
            >
              {{ toggling ? 'Saving...' : (binding.enabled ? 'Disable' : 'Enable') }}
            </button>
            <button
              @click="disconnectBinding"
              :disabled="disconnecting"
              class="text-sm text-status-danger-600 dark:text-status-danger-400 hover:text-status-danger-800 dark:hover:text-status-danger-300 disabled:opacity-50"
            >
              {{ disconnecting ? 'Removing...' : 'Disconnect' }}
            </button>
          </div>
        </div>
      </div>

      <!-- Daily cap -->
      <div v-if="binding.daily_call_cap" class="text-xs text-gray-500 dark:text-gray-400">
        Daily call cap: <strong>{{ binding.daily_call_cap }}</strong> calls / 24h
      </div>

      <!-- Voice picker (persisted per-agent voice, #28) -->
      <div class="p-3 rounded-lg bg-gray-50 dark:bg-gray-900/50 border border-gray-200 dark:border-gray-700">
        <label for="voip-voice" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
          Agent voice
        </label>
        <div class="flex items-center gap-2">
          <select
            id="voip-voice"
            v-model="selectedVoice"
            :disabled="savingVoice"
            class="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-action-primary-500 disabled:opacity-50 text-sm"
          >
            <option v-for="v in VOICES" :key="v.id" :value="v.id">{{ v.label }}</option>
          </select>
          <button
            @click="saveVoice"
            :disabled="savingVoice || selectedVoice === binding.voice_name"
            class="px-3 py-2 border border-transparent rounded-md text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {{ savingVoice ? 'Saving...' : 'Save' }}
          </button>
        </div>
        <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
          Applies to outbound calls and the in-app voice overlay. Default is Kore.
        </p>
      </div>
    </div>

    <!-- Disconnected State — Credentials Form -->
    <div v-else>
      <form @submit.prevent="connectBinding" class="space-y-3 max-w-lg">
        <div>
          <label for="voip-account-sid" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Twilio Account SID
          </label>
          <input
            id="voip-account-sid"
            v-model="form.accountSid"
            type="text"
            placeholder="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            :disabled="connecting"
            class="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-action-primary-500 disabled:bg-gray-100 dark:disabled:bg-gray-900 font-mono text-xs"
          />
        </div>
        <div>
          <label for="voip-auth-token" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Auth Token
          </label>
          <input
            id="voip-auth-token"
            v-model="form.authToken"
            type="password"
            placeholder="Paste your Twilio Auth Token"
            :disabled="connecting"
            class="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-action-primary-500 disabled:bg-gray-100 dark:disabled:bg-gray-900"
          />
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            From Twilio Console — stored encrypted, never displayed back.
          </p>
        </div>
        <div>
          <label for="voip-from" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            From Number
          </label>
          <input
            id="voip-from"
            v-model="form.fromNumber"
            type="text"
            placeholder="+14155551234"
            :disabled="connecting"
            class="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-action-primary-500 disabled:bg-gray-100 dark:disabled:bg-gray-900 font-mono text-xs"
          />
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            A Twilio voice-capable number in E.164 format (e.g. <code class="font-mono">+14155551234</code>).
          </p>
        </div>
        <div>
          <label for="voip-cap" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Daily call cap <span class="text-gray-400 dark:text-gray-500">(optional)</span>
          </label>
          <input
            id="voip-cap"
            v-model.number="form.dailyCallCap"
            type="number"
            min="1"
            placeholder="50"
            :disabled="connecting"
            class="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-action-primary-500 disabled:bg-gray-100 dark:disabled:bg-gray-900 text-sm"
          />
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Maximum outbound calls per 24h. Defaults to 50 if left blank.
          </p>
        </div>
        <button
          type="submit"
          :disabled="connecting || !canSubmit"
          class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <svg v-if="connecting" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
          </svg>
          {{ connecting ? 'Validating...' : 'Connect' }}
        </button>
      </form>
    </div>

    <!-- Messages -->
    <div
      v-if="message"
      :class="[
        'mt-3 p-3 rounded-lg text-sm',
        message.type === 'success'
          ? 'bg-status-success-50 dark:bg-status-success-900/30 text-status-success-700 dark:text-status-success-300'
          : 'bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300'
      ]"
    >
      {{ message.text }}
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import api from '../api'
import { VOICES, DEFAULT_VOICE_NAME } from '../constants/voices'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  }
})

const loading = ref(true)
const connecting = ref(false)
const disconnecting = ref(false)
const toggling = ref(false)
const savingVoice = ref(false)
const accessDenied = ref(false)
const binding = ref({ configured: false })
const message = ref(null)
const selectedVoice = ref(DEFAULT_VOICE_NAME)
const form = ref({ accountSid: '', authToken: '', fromNumber: '', dailyCallCap: null })

const canSubmit = computed(() =>
  form.value.accountSid.trim() &&
  form.value.authToken.trim() &&
  form.value.fromNumber.trim()
)

const truncatedSid = computed(() => {
  const sid = binding.value?.account_sid || ''
  if (sid.length <= 12) return sid
  return `${sid.slice(0, 8)}...${sid.slice(-4)}`
})

function flash(type, text) {
  message.value = { type, text }
  setTimeout(() => { message.value = null }, 3000)
}

async function loadBinding() {
  loading.value = true
  message.value = null
  accessDenied.value = false
  try {
    const response = await api.get(`/api/agents/${props.agentName}/voip`)
    binding.value = response.data
    // Persisted voice is owned per-agent (#28); shown whenever a binding exists.
    if (binding.value.configured) {
      await loadVoice()
    }
  } catch (e) {
    if (e.response?.status === 403) {
      accessDenied.value = true
    }
    binding.value = { configured: false }
  } finally {
    loading.value = false
  }
}

async function loadVoice() {
  try {
    const r = await api.get(`/api/agents/${props.agentName}/voice/name`)
    const voiceName = r.data?.voice_name || DEFAULT_VOICE_NAME
    binding.value = { ...binding.value, voice_name: voiceName }
    selectedVoice.value = voiceName
  } catch {
    binding.value = { ...binding.value, voice_name: DEFAULT_VOICE_NAME }
    selectedVoice.value = DEFAULT_VOICE_NAME
  }
}

async function connectBinding() {
  connecting.value = true
  message.value = null
  try {
    const payload = {
      account_sid: form.value.accountSid.trim(),
      auth_token: form.value.authToken.trim(),
      from_number: form.value.fromNumber.trim(),
    }
    if (form.value.dailyCallCap) payload.daily_call_cap = form.value.dailyCallCap
    const response = await api.put(`/api/agents/${props.agentName}/voip`, payload)
    form.value = { accountSid: '', authToken: '', fromNumber: '', dailyCallCap: null }
    binding.value = response.data
    await loadVoice()
    flash('success', 'Voice binding configured')
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to configure binding'
    message.value = { type: 'error', text: detail }
  } finally {
    connecting.value = false
  }
}

async function toggleEnabled() {
  toggling.value = true
  message.value = null
  try {
    const response = await api.put(`/api/agents/${props.agentName}/voip/enabled`, {
      enabled: !binding.value.enabled,
    })
    binding.value = { ...response.data, voice_name: binding.value.voice_name }
    flash('success', binding.value.enabled ? 'Voice calls enabled' : 'Voice calls disabled')
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to update status'
    message.value = { type: 'error', text: detail }
  } finally {
    toggling.value = false
  }
}

async function saveVoice() {
  savingVoice.value = true
  message.value = null
  try {
    const r = await api.put(`/api/agents/${props.agentName}/voice/name`, {
      voice_name: selectedVoice.value,
    })
    const voiceName = r.data?.voice_name || selectedVoice.value
    binding.value = { ...binding.value, voice_name: voiceName }
    selectedVoice.value = voiceName
    flash('success', 'Voice updated')
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to update voice'
    message.value = { type: 'error', text: detail }
  } finally {
    savingVoice.value = false
  }
}

async function disconnectBinding() {
  disconnecting.value = true
  message.value = null
  try {
    await api.delete(`/api/agents/${props.agentName}/voip`)
    binding.value = { configured: false }
    flash('success', 'Voice binding removed')
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to remove binding'
    message.value = { type: 'error', text: detail }
  } finally {
    disconnecting.value = false
  }
}

watch(() => props.agentName, () => loadBinding())
onMounted(() => loadBinding())
</script>
