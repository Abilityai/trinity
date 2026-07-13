<template>
  <!--
    Per-channel "voice allowed here" flag (ent#117). Voice enable + voice selection
    live once on the agent Settings surface (VoiceRepliesControl); this only toggles
    whether the agent may speak on THIS channel. Effective voice = agent-enabled AND
    this flag AND platform TTS available.
  -->
  <div class="pt-4 border-t border-gray-200 dark:border-gray-700">
    <div class="flex items-start gap-3">
      <label
        class="relative inline-flex items-center cursor-pointer mt-0.5"
        :class="{ 'opacity-50 cursor-not-allowed': !canToggle }"
      >
        <input
          type="checkbox"
          class="sr-only peer"
          :checked="channelOn"
          :disabled="!canToggle || saving"
          @change="toggleChannel($event.target.checked)"
        />
        <div class="w-11 h-6 bg-gray-200 dark:bg-gray-700 peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-action-primary-500 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:bg-white after:border after:border-gray-300 after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-action-primary-600"></div>
      </label>
      <div class="flex-1">
        <div class="text-sm font-medium text-gray-900 dark:text-gray-100">Voice replies on {{ channelLabel }}</div>
        <div class="text-xs text-gray-500 dark:text-gray-400">
          Allow this agent to answer with a spoken voice note here (it chooses per message).
          <span v-if="!available" class="block mt-1 text-status-warning-600 dark:text-status-warning-400">
            Voice is unavailable — the platform has no ElevenLabs key configured.
          </span>
          <span v-else-if="!agentEnabled" class="block mt-1 text-status-warning-600 dark:text-status-warning-400">
            Enable voice replies for this agent in Settings first.
          </span>
        </div>
        <p v-if="message" class="mt-1 text-xs" :class="messageError ? 'text-status-danger-600' : 'text-status-success-600'">{{ message }}</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import api from '../api'

const props = defineProps({
  agentName: { type: String, required: true },
  channel: { type: String, required: true }, // telegram | slack | whatsapp
})

const CHANNEL_LABELS = { telegram: 'Telegram', slack: 'Slack', whatsapp: 'WhatsApp' }
const channelLabel = computed(() => CHANNEL_LABELS[props.channel] || props.channel)

const available = ref(false)
const agentEnabled = ref(false)
const channelOn = ref(false)
const saving = ref(false)
const message = ref('')
const messageError = ref(false)

const canToggle = computed(() => available.value && agentEnabled.value)

const notify = (text, isError = false) => {
  message.value = text
  messageError.value = isError
  setTimeout(() => { message.value = '' }, 3000)
}

async function load() {
  try {
    const { data } = await api.get(`/api/agents/${props.agentName}/voice-replies`)
    available.value = !!data.available
    agentEnabled.value = !!data.enabled
    channelOn.value = !!(data.channels && data.channels[props.channel])
  } catch {
    available.value = false
    agentEnabled.value = false
    channelOn.value = false
  }
}

async function toggleChannel(enabled) {
  channelOn.value = enabled
  saving.value = true
  try {
    const { data } = await api.put(`/api/agents/${props.agentName}/voice-replies`, {
      channels: { [props.channel]: enabled },
    })
    channelOn.value = !!(data.channels && data.channels[props.channel])
    notify('Saved')
  } catch (e) {
    channelOn.value = !enabled
    notify(e.response?.data?.detail || 'Failed to save', true)
  } finally {
    saving.value = false
  }
}

watch(() => props.agentName, load)
onMounted(load)
</script>
