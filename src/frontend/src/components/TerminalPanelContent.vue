<template>
  <div
    :class="[
      'bg-gray-900 overflow-hidden flex flex-col',
      isFullscreen ? 'h-full' : 'rounded-b-lg'
    ]"
    :style="isFullscreen ? {} : { height: '600px' }"
  >
    <!-- Fullscreen Header (only in fullscreen mode) -->
    <div
      v-if="isFullscreen"
      class="flex items-center justify-between px-4 py-2 bg-gray-800 border-b border-gray-700"
    >
      <span class="text-sm font-medium text-gray-300">{{ agentName }} - Terminal</span>
      <button
        @click="$emit('toggle-fullscreen')"
        class="p-1.5 text-gray-400 hover:text-gray-200 rounded transition-colors"
        title="Exit Fullscreen (Esc)"
      >
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 9V4.5M9 9H4.5M9 9L3.75 3.75M9 15v4.5M9 15H4.5M9 15l-5.25 5.25M15 9h4.5M15 9V4.5M15 9l5.25-5.25M15 15h4.5M15 15v4.5m0-4.5l5.25 5.25" />
        </svg>
      </button>
    </div>

    <!-- Terminal Content -->
    <div class="flex-1 min-h-0">
      <AgentTerminal
        v-if="agentStatus === 'running'"
        ref="terminalRef"
        :agent-name="agentName"
        :runtime="runtime"
        :model="model"
        :auto-connect="true"
        :show-fullscreen-toggle="!isFullscreen"
        :is-fullscreen="isFullscreen"
        @connected="$emit('connected')"
        @disconnected="$emit('disconnected')"
        @error="$emit('error', $event)"
        @toggle-fullscreen="$emit('toggle-fullscreen')"
      />
      <div
        v-else
        class="flex items-center justify-center h-full text-gray-400"
      >
        <div class="text-center max-w-md">
          <svg class="w-12 h-12 mx-auto mb-4 text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
          <p class="text-lg font-medium mb-2">Agent is not running</p>
          <p class="text-sm text-gray-500 mb-4">Start the agent to access the terminal</p>

          <!-- API Key Setting -->
          <div v-if="canShare" class="mb-4 p-4 bg-gray-800 rounded-lg text-left">
            <div class="flex items-center justify-between mb-2">
              <span class="text-sm font-medium text-gray-300">Authentication</span>
              <span v-if="apiKeySetting.restart_required" class="text-xs text-status-warning-400">Restart required</span>
            </div>
            <div class="space-y-2">
              <label class="flex items-center space-x-3 cursor-pointer">
                <input
                  type="radio"
                  :checked="apiKeySetting.use_platform_api_key"
                  @change="$emit('update-api-key-setting', true)"
                  :disabled="apiKeySettingLoading"
                  class="text-indigo-500 focus:ring-indigo-500"
                />
                <div>
                  <span class="text-sm text-gray-200">Use Platform API Key</span>
                  <p class="text-xs text-gray-500">Claude uses Trinity's configured Anthropic API key</p>
                </div>
              </label>
              <label class="flex items-center space-x-3 cursor-pointer">
                <input
                  type="radio"
                  :checked="!apiKeySetting.use_platform_api_key"
                  @change="$emit('update-api-key-setting', false)"
                  :disabled="apiKeySettingLoading"
                  class="text-indigo-500 focus:ring-indigo-500"
                />
                <div>
                  <span class="text-sm text-gray-200">Authenticate in Terminal</span>
                  <p class="text-xs text-gray-500">Run "claude login" after starting to use your own subscription</p>
                </div>
              </label>
            </div>
          </div>

          <button
            @click="$emit('start-agent')"
            :disabled="actionLoading"
            class="bg-status-success-600 hover:bg-status-success-700 disabled:bg-status-success-400 text-white text-sm font-medium py-2 px-4 rounded-lg"
          >
            {{ actionLoading ? 'Starting...' : 'Start Agent' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import AgentTerminal from './AgentTerminal.vue'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  agentStatus: {
    type: String,
    default: 'stopped'
  },
  runtime: {
    type: String,
    default: 'claude-code'
  },
  model: {
    type: String,
    default: 'sonnet'
  },
  isFullscreen: {
    type: Boolean,
    default: false
  },
  canShare: {
    type: Boolean,
    default: false
  },
  apiKeySetting: {
    type: Object,
    default: () => ({ use_platform_api_key: true })
  },
  apiKeySettingLoading: {
    type: Boolean,
    default: false
  },
  actionLoading: {
    type: Boolean,
    default: false
  }
})

defineEmits([
  'toggle-fullscreen',
  'connected',
  'disconnected',
  'error',
  'start-agent',
  'update-api-key-setting'
])

// Expose terminal ref for parent access
const terminalRef = ref(null)

defineExpose({
  terminalRef,
  // Forward terminal methods
  connect: () => terminalRef.value?.connect?.(),
  disconnect: () => terminalRef.value?.disconnect?.(),
  fit: () => terminalRef.value?.fit?.(),
  focus: () => terminalRef.value?.focus?.()
})
</script>
