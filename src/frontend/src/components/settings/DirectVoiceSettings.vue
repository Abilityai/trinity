<template>
  <div class="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700" data-testid="direct-voice-settings">
    <h3 class="text-sm font-medium text-gray-900 dark:text-white">Direct calls</h3>
    <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
      In a Workspace call, every spoken turn goes to the agent and its own reply is read aloud.
      Audio and transcripts go to the speech provider. Changes apply to the next call.
    </p>
    <div class="mt-3 flex flex-wrap items-center gap-4">
      <BaseToggle
        :model-value="state.enabled"
        label="Offer Direct calls"
        :disabled="saving"
        @update:model-value="save({ enabled: $event })"
      />
      <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-200">
        Speech provider
        <BaseSelect
          :model-value="state.provider"
          :options="PROVIDERS"
          :disabled="saving"
          data-testid="direct-voice-provider"
          @update:model-value="save({ provider: $event })"
        />
      </label>
    </div>
    <p
      class="mt-2 text-sm"
      :class="state.reason ? 'text-status-warning-700 dark:text-status-warning-400' : 'text-gray-500 dark:text-gray-400'"
      role="status"
      data-testid="direct-voice-state"
    >{{ state.reason || (state.ready_provider ? `Using ${state.ready_provider}.` : '') }}</p>
    <p v-if="error" class="mt-1 text-sm text-status-danger-700 dark:text-status-danger-400" data-testid="direct-voice-error">{{ error }}</p>
  </div>
</template>

<script setup>
// Settings for Workspace Direct calls (trinity-enterprise#1046). Admin-only: the parent
// renders it for admins, and the route refuses everyone else.
import { onMounted, reactive, ref } from 'vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import BaseToggle from '@/components/base/BaseToggle.vue'
import { useSettingsStore } from '@/stores/settings'

const PROVIDERS = [
  { value: 'auto', label: 'Automatic' },
  { value: 'gemini', label: 'Gemini' },
  { value: 'elevenlabs', label: 'ElevenLabs' },
]
const settings = useSettingsStore()
const state = reactive({ enabled: false, provider: 'auto', ready_provider: null, reason: null })
const saving = ref(false)
const error = ref('')

async function save(payload) {
  saving.value = true
  error.value = ''
  try {
    Object.assign(state, await settings.setDirectVoiceSettings(payload))
  } catch (e) {
    error.value = e?.response?.data?.detail || 'Could not save the Direct call settings.'
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  try {
    Object.assign(state, await settings.getDirectVoiceSettings())
  } catch {
    error.value = 'Could not read the Direct call settings.'
  }
})
</script>
