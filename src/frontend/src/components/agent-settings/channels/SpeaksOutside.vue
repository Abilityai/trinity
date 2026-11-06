<template>
  <SettingsSection title="How it speaks outside">
    <SettingRow
      label="Public chat model"
      hint="For public links, channels and paid chat. Your own chats and scheduled runs keep the main model."
      testid="channels-model"
    >
      <template #value>
        <span v-if="modelFailed" class="text-xs text-status-danger-700 dark:text-status-danger-400">Couldn't load</span>
        <span v-else-if="modelLoaded" class="text-xs text-gray-600 dark:text-gray-300" data-testid="channels-model-value">{{ modelLabel }}</span>
      </template>
      <template #action>
        <BaseButton v-if="modelFailed" size="sm" variant="ghost" @click="loadModel">Retry</BaseButton>
        <BaseButton v-else size="sm" variant="secondary" :disabled="!modelLoaded" data-testid="channels-model-change" @click="openModel">Change</BaseButton>
      </template>
    </SettingRow>
    <ChannelConfigDialog v-if="modelOpen" title="Public chat model" @close="modelOpen = false">
      <BaseSelect id="channels-model-select" v-model="modelDraft" label="Model" :disabled="modelSaving">
        <option value="">Use platform default{{ modelDefault ? ` (${modelDefault})` : '' }}</option>
        <option v-for="m in modelOptions" :key="m" :value="m">{{ m }}</option>
      </BaseSelect>
      <div class="mt-4 flex justify-end gap-2">
        <BaseButton variant="secondary" size="sm" @click="modelOpen = false">Cancel</BaseButton>
        <BaseButton size="sm" :loading="modelSaving" loading-label="Saving…" :disabled="modelDraft === model" data-testid="channels-model-save" @click="saveModel">Save</BaseButton>
      </div>
    </ChannelConfigDialog>

    <SettingRow
      label="Additional instructions"
      hint="Added on public and channel chats only. Your own chats, scheduled runs, loops and agent-to-agent calls don't get them."
      testid="channels-instructions"
    >
      <template #value>
        <span v-if="promptFailed" class="text-xs text-status-danger-700 dark:text-status-danger-400">Couldn't load</span>
        <span v-else-if="promptLoaded" class="text-xs text-gray-600 dark:text-gray-300" data-testid="channels-instructions-value">{{ promptLabel }}</span>
      </template>
      <template #action>
        <BaseButton v-if="promptFailed" size="sm" variant="ghost" @click="loadPrompt">Retry</BaseButton>
        <BaseButton v-else size="sm" variant="secondary" :disabled="!promptLoaded" data-testid="channels-instructions-edit" @click="openPrompt">Edit</BaseButton>
      </template>
    </SettingRow>
    <ChannelConfigDialog v-if="promptOpen" title="Additional instructions" @close="promptOpen = false">
      <BaseTextarea
        id="channels-instructions-text"
        v-model="promptDraft"
        label="Instructions for outside audiences"
        help="Use it for persona, scope limits, disclaimers, or guardrails. Voice calls have their own prompt."
        :maxlength="PUBLIC_PROMPT_MAX"
        :rows="6"
        :disabled="promptSaving"
        placeholder="e.g. Always answer in the visitor's language. Never mention internal project names."
      />
      <div class="mt-3 flex items-center justify-between gap-2">
        <span class="text-xs tabular-nums text-gray-500 dark:text-gray-400">{{ promptDraft.length }} / {{ PUBLIC_PROMPT_MAX }}</span>
        <div class="flex items-center gap-2">
          <BaseButton variant="secondary" size="sm" :disabled="promptSaving" @click="promptOpen = false">Cancel</BaseButton>
          <BaseButton size="sm" :loading="promptSaving" loading-label="Saving…" :disabled="promptDraft === prompt" data-testid="channels-instructions-save" @click="savePrompt">Save</BaseButton>
        </div>
      </div>
    </ChannelConfigDialog>

    <div class="px-4 py-3">
      <VoiceRepliesControl :agent-name="agentName" embedded />
    </div>
  </SettingsSection>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import axios from 'axios'
import { useAgentsStore } from '../../../stores/agents'
import { useAuthStore } from '../../../stores/auth'
import { useNotification } from '../../../composables'
import BaseButton from '../../base/BaseButton.vue'
import BaseSelect from '../../base/BaseSelect.vue'
import BaseTextarea from '../../base/BaseTextarea.vue'
import ChannelConfigDialog from '../../ChannelConfigDialog.vue'
import VoiceRepliesControl from '../../VoiceRepliesControl.vue'
import SettingRow from '../SettingRow.vue'
import SettingsSection from '../SettingsSection.vue'

const props = defineProps({ agentName: { type: String, required: true } })

const PUBLIC_PROMPT_MAX = 4000

const agentsStore = useAgentsStore()
const authStore = useAuthStore()
const { showNotification } = useNotification()

// --- Public chat model (#894): '' inherits the platform default ---------------
const model = ref('')
const modelOptions = ref([])
const modelDefault = ref('')
const modelLoaded = ref(false)
const modelFailed = ref(false)
const modelOpen = ref(false)
const modelDraft = ref('')
const modelSaving = ref(false)
const modelLabel = computed(() => model.value || `Platform default${modelDefault.value ? ` (${modelDefault.value})` : ''}`)

const loadModel = async () => {
  modelFailed.value = false
  try {
    const { data } = await axios.get(`/api/agents/${props.agentName}/public-channel-model`, { headers: authStore.authHeader })
    model.value = data.public_channel_model || ''
    modelOptions.value = data.available_models || []
    modelDefault.value = data.platform_default || ''
    modelLoaded.value = true
  } catch (err) {
    console.error('Failed to load public-channel model:', err)
    modelLoaded.value = false
    modelFailed.value = true
  }
}

const openModel = () => {
  modelDraft.value = model.value
  modelOpen.value = true
}

const saveModel = async () => {
  modelSaving.value = true
  try {
    const { data } = await axios.put(
      `/api/agents/${props.agentName}/public-channel-model`,
      { model: modelDraft.value || null },
      { headers: authStore.authHeader },
    )
    model.value = data.public_channel_model || ''
    showNotification(data.is_overridden ? `Public chat model set to ${data.public_channel_model}` : 'Public chat model reset to the platform default', 'success')
    modelOpen.value = false
  } catch (err) {
    console.error('Failed to update public-channel model:', err)
    showNotification(err.response?.data?.detail?.message || err.response?.data?.detail || 'Failed to update public chat model', 'error')
  } finally {
    modelSaving.value = false
  }
}

// --- Additional instructions (#1205) ------------------------------------------
const prompt = ref('')
const promptLoaded = ref(false)
const promptFailed = ref(false)
const promptOpen = ref(false)
const promptDraft = ref('')
const promptSaving = ref(false)
const promptLabel = computed(() => (prompt.value ? `Set · ${prompt.value.length} characters` : 'Not set'))

const loadPrompt = async () => {
  promptFailed.value = false
  try {
    prompt.value = (await agentsStore.fetchPublicChannelPrompt(props.agentName)) || ''
    promptLoaded.value = true
  } catch (err) {
    console.error('Failed to load public instructions:', err)
    promptLoaded.value = false
    promptFailed.value = true
  }
}

const openPrompt = () => {
  promptDraft.value = prompt.value
  promptOpen.value = true
}

const savePrompt = async () => {
  promptSaving.value = true
  try {
    prompt.value = (await agentsStore.savePublicChannelPrompt(props.agentName, promptDraft.value)) || ''
    showNotification('Additional instructions saved', 'success')
    promptOpen.value = false
  } catch (err) {
    console.error('Failed to save public instructions:', err)
    showNotification(err.response?.data?.detail || 'Failed to save instructions', 'error')
  } finally {
    promptSaving.value = false
  }
}

watch(() => props.agentName, (name) => {
  if (!name) return
  loadModel()
  loadPrompt()
}, { immediate: true })
</script>
