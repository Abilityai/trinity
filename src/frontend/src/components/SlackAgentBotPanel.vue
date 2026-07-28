<template>
  <!-- Per-agent Slack bot identity (ent#222). Entitlement-gated: hidden entirely
       in OSS / unentitled builds, never a blank or broken section. -->
  <div v-if="entitled" class="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0">
        <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100">
          Dedicated Slack bot
        </h4>
        <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
          Give this agent its own Slack bot identity — its own name and avatar,
          directly DM-able and <code>@mention</code>-able, alongside other agents
          in the same channel.
        </p>
      </div>
      <span
        v-if="status.configured"
        class="shrink-0 inline-flex items-center gap-1 text-xs font-medium rounded px-2 py-1"
        :class="status.enabled
          ? 'text-status-success-700 dark:text-status-success-300 bg-status-success-50 dark:bg-status-success-900/30'
          : 'text-gray-600 dark:text-gray-300 bg-gray-100 dark:bg-gray-700'"
      >
        {{ status.enabled ? 'Active' : 'Disabled' }}
      </span>
    </div>

    <p v-if="loading" class="text-xs text-gray-500 dark:text-gray-400 mt-3">Loading…</p>

    <!-- Configured -->
    <div v-else-if="status.configured" class="mt-3">
      <div class="text-sm text-gray-700 dark:text-gray-300">
        <span class="font-medium">{{ status.bot_name || 'bot' }}</span>
        <span class="text-gray-500 dark:text-gray-400">
          · {{ status.bot_user_id }} · team {{ status.team_id }}
        </span>
      </div>
      <div class="flex items-center gap-3 mt-3">
        <button
          type="button"
          @click="toggleEnabled"
          :disabled="busy"
          class="text-xs px-2 py-1 border rounded disabled:opacity-50 disabled:cursor-not-allowed
                 border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200
                 hover:bg-gray-50 dark:hover:bg-gray-700"
        >
          {{ status.enabled ? 'Disable' : 'Enable' }}
        </button>
        <button
          type="button"
          @click="showForm = !showForm"
          :disabled="busy"
          class="text-xs px-2 py-1 border rounded disabled:opacity-50
                 border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200
                 hover:bg-gray-50 dark:hover:bg-gray-700"
        >
          Replace tokens
        </button>
        <button
          type="button"
          @click="removeBot"
          :disabled="busy"
          class="text-xs text-status-danger-600 dark:text-status-danger-400
                 hover:text-status-danger-800 disabled:opacity-50"
        >
          Remove
        </button>
      </div>
    </div>

    <p v-else class="text-xs text-gray-500 dark:text-gray-400 mt-3">
      Not configured — this agent posts under the shared workspace bot.
    </p>

    <!-- Token form -->
    <div v-if="showForm || (!status.configured && !loading)" class="mt-3 space-y-2">
      <input
        v-model="botToken"
        type="password"
        autocomplete="off"
        placeholder="Bot token (xoxb-…)"
        class="w-full text-sm px-2 py-1.5 rounded border border-gray-300 dark:border-gray-600
               bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
      />
      <input
        v-model="appToken"
        type="password"
        autocomplete="off"
        placeholder="App-level token (xapp-…, needs connections:write)"
        class="w-full text-sm px-2 py-1.5 rounded border border-gray-300 dark:border-gray-600
               bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
      />
      <button
        type="button"
        @click="save"
        :disabled="busy || !botToken || !appToken"
        class="text-xs px-3 py-1.5 rounded bg-action-primary-600 text-white
               hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {{ busy ? 'Validating…' : 'Save & validate' }}
      </button>
      <p class="text-xs text-gray-500 dark:text-gray-400">
        Tokens are validated against Slack and stored encrypted; they are never shown again.
      </p>
    </div>

    <p
      v-if="message"
      class="text-xs mt-2"
      :class="message.type === 'error'
        ? 'text-status-danger-600 dark:text-status-danger-400'
        : 'text-status-success-600 dark:text-status-success-400'"
    >
      {{ message.text }}
    </p>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import axios from 'axios'
import { useEnterpriseStore } from '../stores/enterprise'

const props = defineProps({
  agentName: { type: String, required: true },
})

const enterpriseStore = useEnterpriseStore()
const entitled = computed(() => enterpriseStore.isEntitled('slack_per_agent_bots'))

const status = ref({ configured: false })
const loading = ref(false)
const busy = ref(false)
const showForm = ref(false)
const botToken = ref('')
const appToken = ref('')
const message = ref(null)

const BASE = '/api/enterprise/slack-agent-bots/agents'

function flash(type, text) {
  message.value = { type, text }
  if (type === 'success') setTimeout(() => { message.value = null }, 3000)
}

// The backend returns a NAMED code for every refusal (wrong token type, bot
// already bound elsewhere, Slack unreachable) — surface it rather than a generic
// failure, so the operator knows what to fix.
function describe(e, fallback) {
  const d = e?.response?.data?.detail
  return (d && (d.message || d)) || fallback
}

async function load() {
  if (!entitled.value) return
  loading.value = true
  try {
    const { data } = await axios.get(`${BASE}/${props.agentName}`)
    status.value = data || { configured: false }
    showForm.value = false
  } catch (e) {
    status.value = { configured: false }
  } finally {
    loading.value = false
  }
}

async function save() {
  busy.value = true
  message.value = null
  try {
    await axios.put(`${BASE}/${props.agentName}`, {
      bot_token: botToken.value.trim(),
      app_token: appToken.value.trim(),
    })
    botToken.value = ''
    appToken.value = ''
    flash('success', 'Slack bot configured')
    await load()
  } catch (e) {
    flash('error', describe(e, 'Failed to configure the Slack bot'))
  } finally {
    busy.value = false
  }
}

async function toggleEnabled() {
  busy.value = true
  try {
    await axios.put(`${BASE}/${props.agentName}/enabled`, { enabled: !status.value.enabled })
    await load()
  } catch (e) {
    flash('error', describe(e, 'Failed to update'))
  } finally {
    busy.value = false
  }
}

async function removeBot() {
  busy.value = true
  try {
    await axios.delete(`${BASE}/${props.agentName}`)
    flash('success', 'Dedicated bot removed')
    await load()
  } catch (e) {
    flash('error', describe(e, 'Failed to remove'))
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>
