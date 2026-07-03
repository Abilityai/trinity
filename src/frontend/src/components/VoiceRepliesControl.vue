<template>
  <!--
    Shared per-agent voice-replies control (epic #24). Reads/writes the shared
    agent-level TTS config (GET/PUT /api/agents/{name}/voice-replies) so every
    channel panel (Telegram #25, WhatsApp trinity-enterprise#56) uses one control
    over one config — no per-channel duplication.
  -->
  <div class="pt-4 border-t border-gray-200 dark:border-gray-700">
    <div class="flex items-start gap-3">
      <label class="relative inline-flex items-center cursor-pointer mt-0.5" :class="{ 'opacity-50 cursor-not-allowed': !voice.available }">
        <input
          type="checkbox"
          class="sr-only peer"
          :checked="voice.enabled"
          :disabled="!voice.available || voiceSaving"
          @change="toggleVoice($event.target.checked)"
        />
        <div class="w-11 h-6 bg-gray-200 dark:bg-gray-700 peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-action-primary-500 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:bg-white after:border after:border-gray-300 after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-action-primary-600"></div>
      </label>
      <div class="flex-1">
        <div class="text-sm font-medium text-gray-900 dark:text-gray-100">Voice replies</div>
        <div class="text-xs text-gray-500 dark:text-gray-400">
          Speak this agent's replies as a voice note (ElevenLabs). Long replies fall back to text.
          <span v-if="!voice.available" class="block mt-1 text-status-warning-600 dark:text-status-warning-400">
            Voice is unavailable — the platform has no ElevenLabs API key configured.
          </span>
        </div>
      </div>
    </div>
    <div v-if="voice.enabled || voiceId" class="mt-3">
      <label :for="`tts-voice-id-${agentName}`" class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">ElevenLabs voice ID</label>
      <div class="flex gap-2">
        <input
          :id="`tts-voice-id-${agentName}`"
          v-model="voiceId"
          type="text"
          placeholder="e.g. 21m00Tcm4TlvDq8ikWAM"
          :disabled="!voice.available || voiceSaving"
          class="flex-1 text-sm border border-gray-300 dark:border-gray-600 rounded-md px-3 py-1.5 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-action-primary-500 disabled:opacity-50"
        />
        <button
          type="button"
          @click="saveVoice"
          :disabled="!voice.available || voiceSaving"
          class="px-3 py-1.5 text-sm font-medium rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50"
        >Save</button>
      </div>
      <p class="mt-1 text-xs text-gray-400 dark:text-gray-500">Paste a voice ID from your ElevenLabs account.</p>
      <p v-if="message" class="mt-1 text-xs" :class="messageError ? 'text-status-danger-600' : 'text-status-success-600'">{{ message }}</p>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import api from '../api'

const props = defineProps({
  agentName: { type: String, required: true },
})

const voice = ref({ enabled: false, available: false })
const voiceId = ref('')
const voiceSaving = ref(false)
const message = ref('')
const messageError = ref(false)

const notify = (text, isError = false) => {
  message.value = text
  messageError.value = isError
  setTimeout(() => { message.value = '' }, 3000)
}

async function loadVoice() {
  try {
    const { data } = await api.get(`/api/agents/${props.agentName}/voice-replies`)
    voice.value = { enabled: !!data.enabled, available: !!data.available }
    voiceId.value = data.voice_id || ''
  } catch {
    voice.value = { enabled: false, available: false }
  }
}

async function saveVoice() {
  voiceSaving.value = true
  try {
    const { data } = await api.put(`/api/agents/${props.agentName}/voice-replies`, {
      enabled: voice.value.enabled,
      voice_id: voiceId.value.trim() || null,
    })
    voice.value = { ...voice.value, enabled: !!data.enabled }
    voiceId.value = data.voice_id || ''
    notify('Voice settings saved')
  } catch (e) {
    notify(e.response?.data?.detail || 'Failed to save voice settings', true)
  } finally {
    voiceSaving.value = false
  }
}

async function toggleVoice(enabled) {
  // Enabling without a voice id would 400 — keep the toggle visually on and let
  // the user paste an id + Save. Disabling persists immediately.
  voice.value = { ...voice.value, enabled }
  if (enabled && !voiceId.value.trim()) return
  await saveVoice()
}

watch(() => props.agentName, loadVoice)
onMounted(loadVoice)
</script>
