<template>
  <!--
    Settings › Storage › Repository › GitHub authentication
    (trinity-enterprise#888). Moved out of the Git tab, which keeps only its
    operational half; the per-agent token (#347) is configuration.

    Emits `status` so the bind row beside it can tell a tokenless agent
    (a public template, read-only) from one that can push.
  -->
  <SettingRow
    label="GitHub authentication"
    hint="The personal access token used for push and pull. A per-agent token lets this agent use a different GitHub account."
    testid="storage-github-auth"
  >
    <template #value>
      <span v-if="!status" class="block h-3 w-24 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-busy="true"><span class="sr-only">Loading token status…</span></span>
      <BaseBadge v-else-if="status.configured" variant="success" size="sm">Agent token set</BaseBadge>
      <BaseBadge v-else-if="status.has_global" variant="info" size="sm">Platform token</BaseBadge>
      <BaseBadge v-else variant="warning" size="sm">No token — push and pull may fail</BaseBadge>
    </template>
    <template #action>
      <template v-if="status && canEdit">
        <BaseButton variant="secondary" size="sm" @click="openModal">{{ status.configured ? 'Replace' : 'Set token' }}</BaseButton>
        <BaseButton v-if="status.configured" variant="ghost" size="sm" :disabled="saving" @click="confirmClear = true">Clear</BaseButton>
      </template>
    </template>
    <template v-if="clearError" #editor>
      <InlineError :message="clearError" @dismiss="clearError = ''" />
    </template>
  </SettingRow>

  <BaseModal v-model="showModal" aria-label="Set a GitHub token" panel-class="w-full max-w-md rounded-[10px] bg-white dark:bg-gray-800 shadow-lg">
    <form class="p-6 space-y-4" @submit.prevent="save">
      <h2 class="text-lg font-semibold text-gray-900 dark:text-gray-100">Set a GitHub token for this agent</h2>
      <BaseInput
        v-model="newPat"
        type="password"
        label="Personal access token"
        placeholder="ghp_…"
        autocomplete="off"
        class="font-mono"
        :error="saveError"
        help="Needs the repo scope. It is checked against GitHub and encrypted at rest; restart the agent for git to use it."
        :disabled="saving"
      />
      <div class="flex justify-end gap-2">
        <BaseButton variant="ghost" :disabled="saving" @click="closeModal">Cancel</BaseButton>
        <BaseButton type="submit" :disabled="!newPat" :loading="saving" loading-label="Checking…">Save token</BaseButton>
      </div>
    </form>
  </BaseModal>

  <ConfirmDialog
    v-model:visible="confirmClear"
    variant="warning"
    title="Clear this agent's token"
    message="Remove the agent-specific GitHub token? This agent falls back to the platform's shared token, which may not reach its repository."
    confirm-text="Clear token"
    cancel-text="Keep token"
    @confirm="clear"
  />
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import { useAgentRestartStore } from '../../stores/agentRestart'
import { apiErrorMessage } from '../../utils/apiError'
import SettingRow from './SettingRow.vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseModal from '../base/BaseModal.vue'
import InlineError from '../InlineError.vue'
import ConfirmDialog from '../ConfirmDialog.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  canEdit: { type: Boolean, default: true },
  notify: { type: Function, default: null },
})
const emit = defineEmits(['status'])

const agentsStore = useAgentsStore()
const restartStore = useAgentRestartStore()
const status = ref(null)
const showModal = ref(false)
const newPat = ref('')
const saving = ref(false)
const saveError = ref('')
const confirmClear = ref(false)
const clearError = ref('')

async function load() {
  try {
    status.value = await agentsStore.getGitHubPATStatus(props.agentName)
  } catch {
    // The status read failing is not "no token": show the platform fallback
    // the backend would use, which is what GitPanel did before the move.
    status.value = { configured: false, source: 'global', has_global: false }
  }
  emit('status', status.value)
}

function openModal() {
  newPat.value = ''
  saveError.value = ''
  showModal.value = true
}
function closeModal() {
  if (!saving.value) showModal.value = false
}

async function save() {
  saving.value = true
  saveError.value = ''
  try {
    const r = await agentsStore.setGitHubPAT(props.agentName, newPat.value)
    await load()
    showModal.value = false
    restartStore.report(props.agentName, 'github-token', 'GitHub token', true)
    props.notify?.(`Token saved for GitHub user ${r?.github_username || ''}.`.replace(' .', '.'), 'success')
  } catch (e) {
    saveError.value = apiErrorMessage(e, "The token wasn't saved. Check it has the repo scope and try again.")
  } finally {
    saving.value = false
  }
}

async function clear() {
  saving.value = true
  clearError.value = ''
  try {
    await agentsStore.clearGitHubPAT(props.agentName)
    await load()
    restartStore.report(props.agentName, 'github-token', 'GitHub token', true)
  } catch (e) {
    clearError.value = apiErrorMessage(e, "The token wasn't cleared. Try again.")
  } finally {
    saving.value = false
  }
}

onMounted(load)
watch(() => props.agentName, () => { status.value = null; load() })
</script>
