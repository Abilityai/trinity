<template>
  <div>
    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Slack Channel</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      Bind this agent to a Slack channel so users can interact via @mentions.
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
      Only the agent owner can manage Slack channel bindings.
    </div>

    <!-- Bound State -->
    <div v-else-if="channel.bound" class="p-4 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-gray-200 dark:border-gray-700">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-3">
          <span class="inline-block w-2.5 h-2.5 rounded-full bg-status-success-500"></span>
          <div>
            <p class="text-sm font-medium text-gray-900 dark:text-white">
              #{{ channel.channel_name }}
            </p>
            <p class="text-xs text-gray-500 dark:text-gray-400">
              {{ channel.workspace_name }}
            </p>
          </div>
        </div>
        <div class="flex items-center gap-2">
          <!-- DM default control: badge if already default, button otherwise -->
          <span
            v-if="channel.is_dm_default"
            class="inline-flex items-center gap-1 text-xs font-medium text-action-primary-700 dark:text-action-primary-300 bg-action-primary-50 dark:bg-action-primary-900/30 border border-action-primary-200 dark:border-action-primary-800 rounded px-2 py-1"
            :title="dmDefaultTooltip"
          >
            <svg class="w-3 h-3" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/></svg>
            DM default
          </span>
          <button
            v-else
            @click="makeDmDefault"
            :disabled="makingDefault"
            :title="dmDefaultTooltip"
            class="text-xs px-2 py-1 border border-action-primary-300 dark:border-action-primary-700 text-action-primary-700 dark:text-action-primary-300 hover:bg-action-primary-50 dark:hover:bg-action-primary-900/30 rounded disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {{ makingDefault ? 'Setting...' : 'Make default' }}
          </button>
          <button
            @click="unbindChannel"
            :disabled="unbinding || unbindBlocked"
            :title="unbindBlocked ? unbindBlockedTooltip : undefined"
            class="text-sm text-status-danger-600 dark:text-status-danger-400 hover:text-status-danger-800 dark:hover:text-status-danger-300 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {{ unbinding ? 'Removing...' : 'Unbind' }}
          </button>
        </div>
      </div>

      <!-- Voice replies (epic #24 / #26) — shared agent-level TTS control -->
      <VoiceRepliesControl :agent-name="agentName" class="mt-4" />
    </div>

    <!-- Unbound State -->
    <div v-else>
      <button
        @click="createChannel"
        :disabled="creating"
        class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        <svg v-if="creating" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
        </svg>
        {{ creating ? 'Creating...' : 'Create Slack Channel' }}
      </button>
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
import { ref, computed, onMounted, watch } from 'vue'
import axios from 'axios'
import VoiceRepliesControl from './VoiceRepliesControl.vue'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  }
})

const loading = ref(true)
const creating = ref(false)
const unbinding = ref(false)
const makingDefault = ref(false)
const accessDenied = ref(false)
const channel = ref({ bound: false })
const message = ref(null)

const dmDefaultTooltip =
  'Direct messages to the bot in this Slack workspace (no @mention, ' +
  'no channel context) are routed to the DM-default agent. Only one ' +
  'agent per workspace can be the DM default at a time.'

// Unbind is blocked when this agent is the DM default AND other agents
// are bound to the same workspace — otherwise DMs would have nowhere to
// land. The owner has to promote a different agent first. (#584)
const unbindBlocked = computed(
  () =>
    channel.value.bound &&
    channel.value.is_dm_default &&
    (channel.value.workspace_agent_count ?? 1) > 1
)
const unbindBlockedTooltip =
  'This agent is the DM default for the workspace. Set a different ' +
  'agent as DM default first, then you can unbind this one.'

async function loadChannel() {
  loading.value = true
  message.value = null
  accessDenied.value = false
  try {
    const response = await axios.get(`/api/agents/${props.agentName}/slack/channel`)
    channel.value = response.data
  } catch (e) {
    if (e.response?.status === 403) {
      accessDenied.value = true
    } else {
      console.error('Failed to load Slack channel:', e)
    }
    channel.value = { bound: false }
  } finally {
    loading.value = false
  }
}

async function createChannel() {
  creating.value = true
  message.value = null
  try {
    const response = await axios.post(`/api/agents/${props.agentName}/slack/channel`)
    const data = response.data

    if (data.status === 'already_bound') {
      message.value = { type: 'success', text: `Already bound to #${data.channel_name} in ${data.workspace_name}` }
    } else {
      message.value = { type: 'success', text: `Channel #${data.channel_name} created in ${data.workspace_name}` }
    }

    await loadChannel()
    setTimeout(() => { message.value = null }, 3000)
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to create Slack channel'
    message.value = { type: 'error', text: detail }
  } finally {
    creating.value = false
  }
}

async function unbindChannel() {
  unbinding.value = true
  message.value = null
  try {
    await axios.delete(`/api/agents/${props.agentName}/slack/channel`)
    message.value = { type: 'success', text: 'Channel unbound' }
    await loadChannel()
    setTimeout(() => { message.value = null }, 3000)
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to unbind channel'
    message.value = { type: 'error', text: detail }
  } finally {
    unbinding.value = false
  }
}

async function makeDmDefault() {
  makingDefault.value = true
  message.value = null
  try {
    const response = await axios.put(
      `/api/agents/${props.agentName}/slack/channel/dm-default`
    )
    const data = response.data
    if (data.status === 'unchanged') {
      message.value = { type: 'success', text: 'Already the DM default' }
    } else {
      const prev = data.previous ? ` (was ${data.previous})` : ''
      message.value = { type: 'success', text: `Set as DM default${prev}` }
    }
    await loadChannel()
    setTimeout(() => { message.value = null }, 3000)
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to set DM default'
    message.value = { type: 'error', text: detail }
  } finally {
    makingDefault.value = false
  }
}

watch(() => props.agentName, () => loadChannel())
onMounted(() => loadChannel())
</script>
