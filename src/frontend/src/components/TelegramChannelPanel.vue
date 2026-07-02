<template>
  <div>
    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Telegram Bot</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      Connect a Telegram bot so users can chat with this agent via Telegram DMs and group chats.
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
      Only the agent owner can manage Telegram bot settings.
    </div>

    <!-- Connected State -->
    <div v-else-if="binding.configured" class="space-y-3">
      <div class="p-4 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-gray-200 dark:border-gray-700">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-3">
            <span class="inline-block w-2.5 h-2.5 rounded-full bg-status-success-500"></span>
            <div>
              <p class="text-sm font-medium text-gray-900 dark:text-white">
                @{{ binding.bot_username }}
              </p>
              <a
                v-if="binding.bot_link"
                :href="binding.bot_link"
                target="_blank"
                rel="noopener noreferrer"
                class="text-xs text-action-primary-600 dark:text-action-primary-400 hover:underline"
              >
                {{ binding.bot_link }}
              </a>
            </div>
          </div>
          <div class="flex items-center gap-2">
            <button
              @click="verifyBot"
              :disabled="verifying"
              class="text-sm text-action-primary-600 dark:text-action-primary-400 hover:text-action-primary-800 dark:hover:text-action-primary-300 disabled:opacity-50"
            >
              {{ verifying ? 'Verifying...' : 'Verify' }}
            </button>
            <button
              @click="disconnectBot"
              :disabled="disconnecting"
              class="text-sm text-status-danger-600 dark:text-status-danger-400 hover:text-status-danger-800 dark:hover:text-status-danger-300 disabled:opacity-50"
            >
              {{ disconnecting ? 'Removing...' : 'Disconnect' }}
            </button>
          </div>
        </div>
      </div>

      <!-- Webhook Warning -->
      <div v-if="!binding.webhook_url" class="p-3 rounded-lg text-sm bg-status-warning-50 dark:bg-status-warning-900/30 text-status-warning-700 dark:text-status-warning-300">
        Bot connected but webhook not registered. Set a <router-link to="/settings" class="underline font-medium hover:text-status-warning-800 dark:hover:text-status-warning-200">Public URL in Settings</router-link> for Telegram messages to reach this agent.
      </div>

      <!-- Group Chats Section -->
      <div v-if="groups.length > 0" class="mt-4">
        <h4 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
          Group Chats ({{ groups.length }})
        </h4>
        <div class="space-y-2">
          <div
            v-for="group in groups"
            :key="group.id"
            class="p-3 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-gray-200 dark:border-gray-700"
          >
            <div class="flex items-center justify-between mb-2">
              <div class="flex items-center gap-2">
                <svg class="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
                <span class="text-sm font-medium text-gray-900 dark:text-white">
                  {{ group.chat_title || `Group ${group.chat_id}` }}
                </span>
                <span class="text-xs text-gray-400">{{ group.chat_type }}</span>
              </div>
              <button
                @click="removeGroup(group)"
                class="text-xs text-status-danger-500 hover:text-status-danger-700 dark:hover:text-status-danger-400"
              >
                Remove
              </button>
            </div>

            <!-- Trigger Mode -->
            <div class="flex items-center gap-4 text-xs flex-wrap">
              <label class="flex items-center gap-1.5 cursor-pointer">
                <input
                  type="radio"
                  :name="`trigger-${group.id}`"
                  value="mention"
                  :checked="group.trigger_mode === 'mention'"
                  @change="updateGroup(group, { trigger_mode: 'mention' })"
                  class="text-action-primary-600 focus:ring-action-primary-500"
                />
                <span class="text-gray-600 dark:text-gray-400">@mention only</span>
              </label>
              <label class="flex items-center gap-1.5 cursor-pointer">
                <input
                  type="radio"
                  :name="`trigger-${group.id}`"
                  value="all"
                  :checked="group.trigger_mode === 'all'"
                  @change="updateGroup(group, { trigger_mode: 'all' })"
                  class="text-action-primary-600 focus:ring-action-primary-500"
                />
                <span class="text-gray-600 dark:text-gray-400">All messages</span>
              </label>
              <label class="flex items-center gap-1.5 cursor-pointer" title="Agent sees all messages but can choose not to respond">
                <input
                  type="radio"
                  :name="`trigger-${group.id}`"
                  value="observe"
                  :checked="group.trigger_mode === 'observe'"
                  @change="updateGroup(group, { trigger_mode: 'observe' })"
                  class="text-action-primary-600 focus:ring-action-primary-500"
                />
                <span class="text-gray-600 dark:text-gray-400">Observe</span>
              </label>
            </div>

            <!-- Welcome Message Toggle -->
            <div class="mt-2">
              <label class="flex items-center gap-1.5 cursor-pointer text-xs">
                <input
                  type="checkbox"
                  :checked="group.welcome_enabled"
                  @change="updateGroup(group, { welcome_enabled: !group.welcome_enabled })"
                  class="rounded text-action-primary-600 focus:ring-action-primary-500"
                />
                <span class="text-gray-600 dark:text-gray-400">Welcome new members</span>
              </label>
              <div v-if="group.welcome_enabled" class="mt-1.5">
                <input
                  type="text"
                  :value="group.welcome_text || ''"
                  @blur="updateGroup(group, { welcome_text: $event.target.value })"
                  placeholder="Welcome, {name}! I'm here to help."
                  class="w-full text-xs px-2 py-1.5 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-1 focus:ring-action-primary-500"
                />
                <p class="mt-0.5 text-xs text-gray-400">Use {name} for the user's first name</p>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div v-else-if="binding.configured && binding.webhook_url" class="mt-3 text-xs text-gray-400 dark:text-gray-500">
        No group chats yet. Add the bot to a Telegram group to see it here.
      </div>

      <!-- Voice replies (epic #24 / #25) — shared agent-level TTS config -->
      <div class="mt-5 pt-4 border-t border-gray-200 dark:border-gray-700">
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
              Speak this agent's replies as a Telegram voice note (ElevenLabs). Long replies fall back to text.
              <span v-if="!voice.available" class="block mt-1 text-status-warning-600 dark:text-status-warning-400">
                Voice is unavailable — the platform has no ElevenLabs API key configured.
              </span>
            </div>
          </div>
        </div>
        <div v-if="voice.enabled || voiceId" class="mt-3">
          <label for="tts-voice-id" class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">ElevenLabs voice ID</label>
          <div class="flex gap-2">
            <input
              id="tts-voice-id"
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
        </div>
      </div>
    </div>

    <!-- Disconnected State — Token Input -->
    <div v-else>
      <form @submit.prevent="connectBot" class="space-y-3">
        <div>
          <label for="telegram-token" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Bot Token
          </label>
          <input
            id="telegram-token"
            v-model="botToken"
            type="password"
            placeholder="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
            :disabled="connecting"
            class="w-full max-w-lg px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-action-primary-500 disabled:bg-gray-100 dark:disabled:bg-gray-900"
          />
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Get a token from <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer" class="text-action-primary-600 dark:text-action-primary-400 hover:underline">@BotFather</a> on Telegram.
          </p>
        </div>
        <button
          type="submit"
          :disabled="connecting || !botToken.trim()"
          class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <svg v-if="connecting" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
          </svg>
          {{ connecting ? 'Connecting...' : 'Connect Bot' }}
        </button>
      </form>
    </div>

    <!-- Messages -->
    <div v-if="message" :class="[
      'mt-3 p-3 rounded-lg text-sm',
      message.type === 'success' ? 'bg-status-success-50 dark:bg-status-success-900/30 text-status-success-700 dark:text-status-success-300' : 'bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300'
    ]">
      {{ message.text }}
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import api from '../api'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  }
})

const loading = ref(true)
const connecting = ref(false)
const disconnecting = ref(false)
const verifying = ref(false)
const accessDenied = ref(false)
const binding = ref({ configured: false })
const groups = ref([])
const botToken = ref('')
const message = ref(null)

// Voice replies (epic #24 / #25) — shared agent-level TTS config
const voice = ref({ enabled: false, available: false })
const voiceId = ref('')
const voiceSaving = ref(false)

async function loadBinding() {
  loading.value = true
  message.value = null
  accessDenied.value = false
  try {
    const response = await api.get(`/api/agents/${props.agentName}/telegram`)
    binding.value = response.data
    if (response.data.configured) {
      await loadGroups()
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

async function loadGroups() {
  try {
    const response = await api.get(`/api/agents/${props.agentName}/telegram/groups`)
    groups.value = response.data
  } catch (e) {
    groups.value = []
  }
}

async function loadVoice() {
  try {
    const response = await api.get(`/api/agents/${props.agentName}/voice-replies`)
    voice.value = { enabled: !!response.data.enabled, available: !!response.data.available }
    voiceId.value = response.data.voice_id || ''
  } catch (e) {
    voice.value = { enabled: false, available: false }
  }
}

async function saveVoice() {
  voiceSaving.value = true
  message.value = null
  try {
    const response = await api.put(`/api/agents/${props.agentName}/voice-replies`, {
      enabled: voice.value.enabled,
      voice_id: voiceId.value.trim() || null,
    })
    voice.value = { ...voice.value, enabled: !!response.data.enabled }
    voiceId.value = response.data.voice_id || ''
    message.value = { type: 'success', text: 'Voice settings saved' }
    setTimeout(() => { message.value = null }, 3000)
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to save voice settings'
    message.value = { type: 'error', text: detail }
    setTimeout(() => { message.value = null }, 3000)
  } finally {
    voiceSaving.value = false
  }
}

async function toggleVoice(enabled) {
  // Turning on without a voice id would 400 — keep the toggle visually on and let
  // the user paste an id + Save. Turning off persists immediately.
  voice.value = { ...voice.value, enabled }
  if (enabled && !voiceId.value.trim()) return
  await saveVoice()
}

async function connectBot() {
  connecting.value = true
  message.value = null
  try {
    const response = await api.put(`/api/agents/${props.agentName}/telegram`, {
      bot_token: botToken.value.trim()
    })
    botToken.value = ''
    binding.value = response.data
    message.value = { type: 'success', text: `Bot @${response.data.bot_username} connected` }
    setTimeout(() => { message.value = null }, 3000)
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to connect bot'
    message.value = { type: 'error', text: detail }
  } finally {
    connecting.value = false
  }
}

async function disconnectBot() {
  disconnecting.value = true
  message.value = null
  try {
    await api.delete(`/api/agents/${props.agentName}/telegram`)
    message.value = { type: 'success', text: 'Bot disconnected' }
    binding.value = { configured: false }
    groups.value = []
    setTimeout(() => { message.value = null }, 3000)
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to disconnect bot'
    message.value = { type: 'error', text: detail }
  } finally {
    disconnecting.value = false
  }
}

async function verifyBot() {
  verifying.value = true
  message.value = null
  try {
    const response = await api.post(`/api/agents/${props.agentName}/telegram/test`, {
      message: 'Verification check'
    })
    if (response.data.ok) {
      message.value = { type: 'success', text: response.data.message }
    } else {
      message.value = { type: 'error', text: response.data.message || 'Verification failed' }
    }
    setTimeout(() => { message.value = null }, 3000)
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to verify bot'
    message.value = { type: 'error', text: detail }
  } finally {
    verifying.value = false
  }
}

async function updateGroup(group, updates) {
  try {
    const response = await api.put(
      `/api/agents/${props.agentName}/telegram/groups/${group.id}`,
      updates
    )
    // Update local state
    const idx = groups.value.findIndex(g => g.id === group.id)
    if (idx !== -1) {
      groups.value[idx] = { ...groups.value[idx], ...response.data }
    }
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to update group config'
    message.value = { type: 'error', text: detail }
    setTimeout(() => { message.value = null }, 3000)
  }
}

async function removeGroup(group) {
  try {
    await api.delete(`/api/agents/${props.agentName}/telegram/groups/${group.id}`)
    groups.value = groups.value.filter(g => g.id !== group.id)
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to remove group'
    message.value = { type: 'error', text: detail }
    setTimeout(() => { message.value = null }, 3000)
  }
}

watch(() => props.agentName, () => loadBinding())
onMounted(() => loadBinding())
</script>
