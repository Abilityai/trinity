<template>
  <!--
    Settings › General — "What this agent is" (trinity-enterprise#887).

    Every control here moved from somewhere else and exists nowhere else now:
    the label and id edits and the avatar from the header, tags / read-only /
    funding / memory-and-CPU from the header rows, parallel capacity from the
    old Settings cards, the description and template details from the Info
    tab. The execution timeout (ent#353) is new — its endpoints existed, its
    control never shipped.

    Each row that reads a value owns its three states: a skeleton while there
    is no value yet, the value, or an honest "couldn't load" — never a default
    dressed up as the value.
  -->
  <div class="space-y-6">
    <!-- Identity -->
    <SettingsSection title="Identity">
      <SettingRow label="Display name" hint="Shown everywhere a person sees the agent." testid="general-display-name">
        <template #value>
          <span v-if="editing !== 'label'" class="text-sm text-gray-900 dark:text-gray-100">{{ displayName }}</span>
        </template>
        <template #action>
          <BaseButton v-if="canEdit && editing !== 'label'" variant="secondary" size="sm" @click="startEdit('label')">Edit</BaseButton>
        </template>
        <template v-if="editing === 'label'" #editor>
          <form class="flex flex-col gap-2 sm:flex-row sm:items-start" @submit.prevent="saveLabel">
            <BaseInput
              v-model="draft.label"
              class="sm:flex-1"
              :placeholder="agent.name"
              help="Leave empty to show the agent id instead."
              aria-label="Display name"
              data-autofocus
            />
            <div class="flex gap-2">
              <BaseButton type="submit" size="sm">Save</BaseButton>
              <BaseButton variant="ghost" size="sm" @click="cancelEdit">Cancel</BaseButton>
            </div>
          </form>
        </template>
      </SettingRow>

      <SettingRow
        label="Agent id"
        hint="Used in URLs, MCP keys and the container name. Renaming it breaks existing links."
        testid="general-agent-id"
      >
        <template #value>
          <code v-if="editing !== 'slug'" class="text-[12.5px] font-mono px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-750 text-gray-800 dark:text-gray-200">{{ agent.name }}</code>
        </template>
        <template #action>
          <BaseButton v-if="canEdit && editing !== 'slug'" variant="secondary" size="sm" @click="startEdit('slug')">Rename…</BaseButton>
        </template>
        <template v-if="editing === 'slug'" #editor>
          <form class="space-y-2" @submit.prevent="saveSlug">
            <BaseInput
              v-model="draft.slug"
              aria-label="New agent id"
              :error="slugError"
              class="font-mono"
            />
            <p class="text-[12.5px] text-status-warning-700 dark:text-status-warning-400">
              Renaming the id stops and restarts the agent and re-keys its URLs and MCP keys; its data
              volumes stay under the old id. To change only the name people see, edit the display name.
            </p>
            <div class="flex gap-2">
              <BaseButton type="submit" variant="danger" size="sm">Rename id</BaseButton>
              <BaseButton variant="ghost" size="sm" @click="cancelEdit">Cancel</BaseButton>
            </div>
          </form>
        </template>
      </SettingRow>

      <SettingRow label="Avatar" :hint="hasAvatarPrompt ? 'Generated from the identity prompt; the emotion set follows it.' : 'Generate a portrait from a short identity prompt.'">
        <template #value>
          <AgentAvatar :name="agent.name" :avatar-url="agent.avatar_url" size="md" />
        </template>
        <template #action>
          <BaseButton v-if="canEdit && agent.avatar_url && hasAvatarPrompt" variant="ghost" size="sm" title="Show the next emotion in the set" @click="$emit('cycle-emotion')">Next emotion</BaseButton>
          <BaseButton v-if="canEdit" variant="secondary" size="sm" @click="$emit('open-avatar-modal')">Change…</BaseButton>
        </template>
      </SettingRow>

      <SettingRow label="Description" hint="Read from the agent's template — the Workspace, the A2A card and the agent picker show it.">
        <template #value>
          <span v-if="info.state === 'loading'" class="block h-3 w-48 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-busy="true"><span class="sr-only">Loading description…</span></span>
          <span v-else-if="info.state === 'failed'" class="text-[12.5px] text-gray-600 dark:text-gray-400">{{ info.failedText }}</span>
          <span v-else-if="info.data?.description" class="text-sm text-gray-900 dark:text-gray-100 line-clamp-3" :title="info.data.description">{{ info.data.description }}</span>
          <span v-else class="text-[12.5px] text-gray-600 dark:text-gray-400">The template has no description.</span>
        </template>
      </SettingRow>

      <SettingRow label="Tags" hint="For fleet views and filters.">
        <template #value>
          <TagsEditor
            :model-value="tags"
            :editable="canEdit"
            :all-tags="allTags"
            @update:model-value="$emit('update-tags', $event)"
            @add="$emit('add-tag', $event)"
            @remove="$emit('remove-tag', $event)"
          />
        </template>
      </SettingRow>
    </SettingsSection>

    <!-- Model and funding -->
    <SettingsSection title="Model and funding">
      <SettingRow label="Runtime" hint="Claude Code, Gemini CLI or Codex — chosen when the agent was created.">
        <template #value><RuntimeBadge :runtime="agent.runtime" /></template>
      </SettingRow>

      <SettingRow label="Model" hint="For chat, tasks and schedules that do not name their own." testid="general-model">
        <template #value>
          <template v-if="agent.status !== 'running'">
            <span class="text-[12.5px] text-gray-600 dark:text-gray-400">Start the agent to read or change its model.</span>
          </template>
          <span v-else-if="model.state === 'loading'" class="block h-3 w-32 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-busy="true"><span class="sr-only">Loading model…</span></span>
          <span v-else-if="model.state === 'failed'" class="text-[12.5px] text-gray-600 dark:text-gray-400">Couldn't read the model from the agent.</span>
          <span v-else-if="editing !== 'model'" class="text-sm text-gray-900 dark:text-gray-100">{{ model.value || 'Runtime default' }}</span>
        </template>
        <template #action>
          <BaseButton v-if="canEdit && agent.status === 'running' && model.state === 'loaded' && editing !== 'model'" variant="secondary" size="sm" @click="startEdit('model')">Change</BaseButton>
          <BaseButton v-if="agent.status === 'running' && model.state === 'failed'" variant="ghost" size="sm" @click="loadModel">Try again</BaseButton>
        </template>
        <template v-if="editing === 'model'" #editor>
          <form class="space-y-2" @submit.prevent="saveModel">
            <ModelSelector v-model="draft.model" />
            <InlineError :message="model.saveError" @dismiss="model.saveError = ''" />
            <div class="flex gap-2">
              <BaseButton type="submit" size="sm" :loading="model.saving" loading-label="Saving…">Save</BaseButton>
              <BaseButton variant="ghost" size="sm" @click="cancelEdit">Cancel</BaseButton>
            </div>
          </form>
        </template>
      </SettingRow>

      <SettingRow label="Funding" hint="A subscription, or the platform API key. The cost figures on Overview follow this choice." testid="general-funding">
        <template #value>
          <span v-if="!authStatus" class="text-[12.5px] text-gray-600 dark:text-gray-400">Couldn't read how this agent is funded.</span>
          <template v-else>
            <BaseBadge :variant="fundingVariant" size="sm">{{ fundingKind }}</BaseBadge>
            <BaseSelect
              v-if="canChangeFunding"
              size="sm"
              aria-label="Funding"
              :model-value="authStatus.auth_mode === 'subscription' ? authStatus.subscription_name : ''"
              :disabled="subscriptionChanging"
              @update:model-value="$emit('change-subscription', $event)"
            >
              <option value="">Platform API key</option>
              <option v-for="sub in subscriptions" :key="sub.id" :value="sub.name">{{ sub.name }}</option>
            </BaseSelect>
            <span v-else class="text-sm text-gray-900 dark:text-gray-100">{{ fundingLabel }}</span>
          </template>
        </template>
      </SettingRow>
    </SettingsSection>

    <!-- Execution -->
    <SettingsSection title="Execution">
      <SettingRow
        label="Execution timeout"
        hint="The cap for every run this agent starts — chat, tasks, schedules, MCP calls and channel messages. A schedule or task can set a shorter one, never a longer one."
        testid="general-timeout"
      >
        <template #value>
          <span v-if="timeout.state === 'loading'" class="block h-3 w-16 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-busy="true"><span class="sr-only">Loading timeout…</span></span>
          <span v-else-if="timeout.state === 'failed'" class="text-[12.5px] text-gray-600 dark:text-gray-400">Couldn't load the timeout.</span>
          <span v-else-if="editing !== 'timeout'" class="text-sm tabular-nums text-gray-900 dark:text-gray-100" data-testid="general-timeout-value">{{ timeout.minutes }} min</span>
        </template>
        <template #action>
          <BaseButton v-if="canEdit && timeout.state === 'loaded' && editing !== 'timeout'" variant="secondary" size="sm" @click="startEdit('timeout')">Edit</BaseButton>
          <BaseButton v-if="timeout.state === 'failed'" variant="ghost" size="sm" @click="loadTimeout">Try again</BaseButton>
        </template>
        <template v-if="editing === 'timeout'" #editor>
          <form class="space-y-2" novalidate @submit.prevent="saveTimeout">
            <div class="flex flex-col gap-2 sm:flex-row sm:items-start">
              <BaseInput
                v-model="draft.timeout"
                type="number"
                class="sm:w-80"
                aria-label="Execution timeout in minutes"
                :error="timeout.inputError"
                :help="`Minutes, ${TIMEOUT_MIN_MINUTES}–${TIMEOUT_MAX_MINUTES}. Default ${TIMEOUT_DEFAULT_MINUTES}.`"
                data-testid="general-timeout-input"
              />
              <div class="flex gap-2">
                <BaseButton type="submit" size="sm" :loading="timeout.saving" loading-label="Saving…" data-testid="general-timeout-save">Save</BaseButton>
                <BaseButton variant="ghost" size="sm" @click="cancelEdit">Cancel</BaseButton>
              </div>
            </div>
            <div v-if="timeout.saveError" class="space-y-2">
              <InlineError :message="timeout.saveError" @dismiss="timeout.saveError = ''" />
              <BaseButton
                v-if="timeout.schedulesConflict"
                variant="secondary"
                size="sm"
                data-testid="general-timeout-open-schedules"
                @click="$emit('navigate-tab', 'schedules')"
              >Open Schedules</BaseButton>
            </div>
          </form>
        </template>
      </SettingRow>

      <SettingRow label="Parallel capacity" :hint="capacityHint" testid="general-capacity">
        <template #value>
          <span v-if="capacity.state === 'loading'" class="block h-3 w-12 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-busy="true"><span class="sr-only">Loading capacity…</span></span>
          <span v-else-if="capacity.state === 'failed'" class="text-[12.5px] text-gray-600 dark:text-gray-400">Couldn't load the capacity.</span>
          <span v-else-if="editing !== 'capacity'" class="text-sm tabular-nums text-gray-900 dark:text-gray-100">{{ capacity.effective }} {{ capacity.effective === 1 ? 'job' : 'jobs' }}</span>
        </template>
        <template #action>
          <BaseButton v-if="canEdit && capacity.state === 'loaded' && editing !== 'capacity'" variant="secondary" size="sm" @click="startEdit('capacity')">Edit</BaseButton>
          <BaseButton v-if="capacity.state === 'failed'" variant="ghost" size="sm" @click="loadCapacity">Try again</BaseButton>
        </template>
        <template v-if="editing === 'capacity'" #editor>
          <form class="space-y-2" novalidate @submit.prevent="saveCapacity">
            <div class="flex flex-col gap-2 sm:flex-row sm:items-start">
              <BaseInput
                v-model="draft.capacity"
                type="number"
                class="sm:w-80"
                aria-label="Parallel capacity"
                :error="capacity.inputError"
                :help="`Between 1 and ${capacity.ceiling}, the fleet ceiling.`"
              />
              <div class="flex gap-2">
                <BaseButton type="submit" size="sm" :loading="capacity.saving" loading-label="Saving…">Save</BaseButton>
                <BaseButton variant="ghost" size="sm" @click="cancelEdit">Cancel</BaseButton>
              </div>
            </div>
            <InlineError :message="capacity.saveError" @dismiss="capacity.saveError = ''" />
          </form>
        </template>
      </SettingRow>

      <SettingRow label="Read-only mode" hint="Blocks every write to the agent's source files. For audits and demos." testid="general-read-only">
        <template #value>
          <span class="text-[12.5px] text-gray-600 dark:text-gray-400">{{ agent.read_only_enabled ? 'On' : 'Off' }}</span>
          <BaseToggle
            :model-value="!!agent.read_only_enabled"
            aria-label="Read-only mode"
            :disabled="!canEdit || readOnlyLoading"
            @update:model-value="$emit('toggle-read-only')"
          />
        </template>
      </SettingRow>

      <SettingRow label="Memory and CPU" hint="Container limits. A change applies on the next restart." testid="general-resources">
        <template #badge>
          <BaseBadge v-if="resourcesPending" variant="warning" size="sm">restart</BaseBadge>
        </template>
        <template #value>
          <span v-if="resourceLimits.error" class="text-[12.5px] text-gray-600 dark:text-gray-400">Couldn't load the limits.</span>
          <span v-else-if="editing !== 'resources'" class="text-sm tabular-nums text-gray-900 dark:text-gray-100">
            {{ (effectiveMemory || '').toUpperCase() }} · {{ effectiveCpu }} {{ effectiveCpu === '1' ? 'core' : 'cores' }}
          </span>
        </template>
        <template #action>
          <BaseButton v-if="canEdit && !resourceLimits.error && editing !== 'resources'" variant="secondary" size="sm" @click="startEdit('resources')">Edit</BaseButton>
        </template>
        <template v-if="editing === 'resources'" #editor>
          <form class="space-y-2" @submit.prevent="saveResources">
            <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <BaseSelect v-model="draft.memory" label="Memory">
                <option value="">Inherit default ({{ (resourceLimits.current_memory || '4g').toUpperCase() }})</option>
                <option v-for="m in MEMORY_OPTIONS" :key="m" :value="m">{{ m.toUpperCase() }}</option>
              </BaseSelect>
              <BaseSelect v-model="draft.cpu" label="CPU cores">
                <option value="">Inherit default ({{ resourceLimits.current_cpu || '2' }})</option>
                <option v-for="c in CPU_OPTIONS" :key="c" :value="c">{{ c }}</option>
              </BaseSelect>
            </div>
            <p class="text-[12.5px] text-gray-600 dark:text-gray-400">Saved now, applied on the next restart. The bar at the top of Settings restarts the agent when you are ready.</p>
            <div class="flex gap-2">
              <BaseButton type="submit" size="sm" :loading="resourcesSaving" loading-label="Saving…">Save</BaseButton>
              <BaseButton variant="ghost" size="sm" @click="cancelEdit">Cancel</BaseButton>
            </div>
          </form>
        </template>
      </SettingRow>
    </SettingsSection>

    <!-- Template -->
    <SettingsSection title="Template">
      <SettingRow label="Deployed from" hint="The repository or template this agent was created from. Full template metadata is behind the details.">
        <template #value>
          <code v-if="templateRef" class="text-[12.5px] font-mono px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-750 text-gray-800 dark:text-gray-200 break-all">{{ templateRef }}</code>
          <span v-else-if="info.state === 'loading'" class="block h-3 w-40 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-busy="true"><span class="sr-only">Loading template…</span></span>
          <span v-else class="text-[12.5px] text-gray-600 dark:text-gray-400">Created without a template.</span>
        </template>
        <template #action>
          <BaseButton variant="secondary" size="sm" data-testid="general-technical-details" @click="showDetails = true">Technical details</BaseButton>
        </template>
      </SettingRow>
    </SettingsSection>

    <!-- Danger zone -->
    <SettingsSection v-if="showDangerZone" title="Danger zone" danger>
      <SettingRow
        v-if="canReset"
        label="Reset the workspace to main"
        hint="Adopts the repository's main branch as the new baseline. Memory, credentials and schedules are kept; other local changes are discarded."
      >
        <template #action>
          <BaseButton variant="danger" size="sm" :loading="resetting" loading-label="Resetting…" @click="confirmReset = true">Reset…</BaseButton>
        </template>
        <template v-if="resetError" #editor>
          <InlineError :message="resetError" @dismiss="resetError = ''" />
        </template>
      </SettingRow>
      <SettingRow
        v-if="agent.can_delete"
        label="Delete this agent"
        hint="Stops the container and removes the agent. Reports and executions are kept for audit."
      >
        <template #action>
          <BaseButton variant="danger" size="sm" data-testid="general-delete" @click="$emit('delete')">Delete…</BaseButton>
        </template>
      </SettingRow>
    </SettingsSection>

    <!-- Technical details: the Info tab's template metadata, behind the row's button. -->
    <BaseModal v-model="showDetails" aria-label="Technical details" panel-class="w-full max-w-3xl max-h-[85vh] overflow-y-auto rounded-[10px] bg-white dark:bg-gray-800 shadow-lg">
      <div class="flex items-center justify-between px-6 pt-5">
        <h2 class="text-lg font-semibold text-gray-900 dark:text-gray-100">Technical details</h2>
        <BaseButton variant="ghost" size="sm" @click="showDetails = false">Close</BaseButton>
      </div>
      <div class="p-6 pt-4">
        <InfoPanel :agent-name="agent.name" :agent-status="agent.status" @item-click="onInfoItemClick" />
      </div>
    </BaseModal>

    <ConfirmDialog
      v-model:visible="confirmReset"
      title="Reset the workspace to main?"
      message="The workspace adopts main as its new baseline and the result is pushed. Memory, credentials and schedules are kept; any other local change is discarded and cannot be recovered here."
      confirm-text="Reset workspace"
      variant="danger"
      @confirm="resetToMain"
    />
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted, nextTick } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import { agentDisplayName } from '../../utils/agentName'
import {
  TIMEOUT_MIN_MINUTES, TIMEOUT_MAX_MINUTES, TIMEOUT_DEFAULT_MINUTES,
  validateTimeoutMinutes, minutesToSeconds, secondsToMinutes, describeTimeoutError,
} from '../../utils/executionTimeout'
import { apiErrorMessage } from '../../utils/apiError'
import SettingsSection from './SettingsSection.vue'
import SettingRow from './SettingRow.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseSelect from '../base/BaseSelect.vue'
import BaseToggle from '../base/BaseToggle.vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseModal from '../base/BaseModal.vue'
import InlineError from '../InlineError.vue'
import ConfirmDialog from '../ConfirmDialog.vue'
import AgentAvatar from '../AgentAvatar.vue'
import RuntimeBadge from '../RuntimeBadge.vue'
import TagsEditor from '../TagsEditor.vue'
import ModelSelector from '../ModelSelector.vue'
import InfoPanel from '../InfoPanel.vue'

const props = defineProps({
  agent: { type: Object, required: true },
  authStatus: { type: Object, default: null },
  subscriptions: { type: Array, default: null },
  subscriptionChanging: { type: Boolean, default: false },
  readOnlyLoading: { type: Boolean, default: false },
  tags: { type: Array, default: () => [] },
  allTags: { type: Array, default: () => [] },
  resourceLimits: { type: Object, default: () => ({}) },
  resourcesSaving: { type: Boolean, default: false },
  hasAvatarPrompt: { type: Boolean, default: false },
  hasGitSync: { type: Boolean, default: false },
  notify: { type: Function, default: null },
})

const emit = defineEmits([
  'set-label', 'rename', 'open-avatar-modal', 'cycle-emotion', 'update-tags', 'add-tag', 'remove-tag',
  'change-subscription', 'toggle-read-only', 'save-resources', 'delete', 'navigate-tab', 'info-item-click',
])

const MEMORY_OPTIONS = ['1g', '2g', '4g', '8g', '16g', '32g', '64g']
const CPU_OPTIONS = ['1', '2', '4', '8', '16']

const agentsStore = useAgentsStore()
const toast = (msg, type = 'success') => props.notify && props.notify(msg, type)

// Settings is owner-only, so `can_share` is true here; the system agent is the
// one owner-visible agent whose identity and limits are not the owner's to edit.
const canEdit = computed(() => !!props.agent.can_share && !props.agent.is_system)
// `hasGitSync` is page-level and permissive; the reset needs a repository
// actually bound, which only the git config answers.
const gitEnabled = ref(false)
async function loadGitConfig() {
  try {
    const r = await agentsStore.getGitConfig(props.agent.name)
    gitEnabled.value = !!r?.git_enabled
  } catch {
    gitEnabled.value = false
  }
}
const canReset = computed(() => props.hasGitSync && gitEnabled.value)
const showDangerZone = computed(() => canEdit.value && (canReset.value || props.agent.can_delete))
const displayName = computed(() => agentDisplayName(props.agent))

// ---- one inline editor open at a time ------------------------------------
const editing = ref(null)
const draft = reactive({ label: '', slug: '', model: '', timeout: '', capacity: '', memory: '', cpu: '' })
const slugError = ref('')

function startEdit(which) {
  editing.value = which
  slugError.value = ''
  if (which === 'label') draft.label = props.agent.display_label || ''
  if (which === 'slug') draft.slug = props.agent.name
  if (which === 'model') { draft.model = model.value || ''; model.saveError = '' }
  if (which === 'timeout') { draft.timeout = String(timeout.minutes); timeout.inputError = ''; timeout.saveError = '' }
  if (which === 'capacity') { draft.capacity = String(capacity.stored ?? capacity.effective); capacity.inputError = ''; capacity.saveError = '' }
  // The draft holds the OVERRIDE, not the effective value: an empty choice is
  // "inherit the platform default", which clears the per-agent row.
  if (which === 'resources') { draft.memory = props.resourceLimits.memory || ''; draft.cpu = props.resourceLimits.cpu ? String(props.resourceLimits.cpu) : '' }
  nextTick(() => {
    const el = document.querySelector('[data-settings-pane] form input, [data-settings-pane] form select')
    el?.focus()
  })
}
function cancelEdit() { editing.value = null }

function saveLabel() {
  const trimmed = draft.label.trim()
  // Empty CLEARS the label (ent#181): the agent renders under its id again.
  if (trimmed !== (props.agent.display_label || '')) emit('set-label', trimmed || null)
  editing.value = null
}

function saveSlug() {
  const trimmed = draft.slug.trim()
  if (!trimmed) { slugError.value = 'Enter the new id, for example research-agent.'; return }
  if (trimmed === props.agent.name) { editing.value = null; return }
  emit('rename', trimmed)
  editing.value = null
}

// ---- description + template (the old Info tab) ----------------------------
const info = reactive({ state: 'loading', data: null, failedText: '' })
async function loadInfo() {
  if (!info.data) info.state = 'loading'
  try {
    info.data = await agentsStore.getAgentInfo(props.agent.name)
    info.state = 'loaded'
  } catch (e) {
    if (info.data) return  // keep what is on screen; the next load retries
    info.state = 'failed'
    info.failedText = props.agent.status === 'running'
      ? "Couldn't read the template from the agent."
      : 'Start the agent to read its template description.'
  }
}
const templateRef = computed(() => props.agent.template || info.data?.template_name || '')

function onInfoItemClick(payload) {
  showDetails.value = false
  emit('info-item-click', payload)
}
const showDetails = ref(false)

// ---- model ----------------------------------------------------------------
const model = reactive({ state: 'loading', value: '', saving: false, saveError: '' })
async function loadModel() {
  if (props.agent.status !== 'running') return
  model.state = 'loading'
  try {
    const r = await agentsStore.getAgentModel(props.agent.name)
    model.value = r?.model || ''
    model.state = 'loaded'
  } catch {
    model.state = 'failed'
  }
}
async function saveModel() {
  model.saving = true
  model.saveError = ''
  try {
    await agentsStore.setAgentModel(props.agent.name, draft.model || null)
    model.value = draft.model || ''
    editing.value = null
    toast(`Model set to ${draft.model || 'the runtime default'}.`)
  } catch (e) {
    model.saveError = apiErrorMessage(e, "The model wasn't changed. Try again.")
  } finally {
    model.saving = false
  }
}

// ---- funding --------------------------------------------------------------
const canChangeFunding = computed(() => props.subscriptions !== null && canEdit.value)
const fundingKind = computed(() => {
  const m = props.authStatus?.auth_mode
  return m === 'subscription' ? 'Subscription' : m === 'api_key' ? 'API key' : 'Not set'
})
const fundingVariant = computed(() => {
  const m = props.authStatus?.auth_mode
  return m === 'subscription' ? 'autonomous' : m === 'api_key' ? 'neutral' : 'danger'
})
const fundingLabel = computed(() => {
  const s = props.authStatus
  if (!s) return ''
  if (s.auth_mode === 'subscription') return s.subscription_name
  if (s.auth_mode === 'api_key') return 'Platform API key'
  return 'No credentials configured'
})

// ---- execution timeout (ent#353) -------------------------------------------
const timeout = reactive({ state: 'loading', minutes: TIMEOUT_DEFAULT_MINUTES, saving: false, inputError: '', saveError: '', schedulesConflict: false })
async function loadTimeout() {
  timeout.state = 'loading'
  try {
    const r = await agentsStore.getAgentTimeout(props.agent.name)
    timeout.minutes = secondsToMinutes(r.execution_timeout_seconds)
    timeout.state = 'loaded'
  } catch {
    timeout.state = 'failed'
  }
}
async function saveTimeout() {
  timeout.inputError = validateTimeoutMinutes(draft.timeout)
  timeout.saveError = ''
  timeout.schedulesConflict = false
  if (timeout.inputError) return
  const minutes = Number(draft.timeout)
  timeout.saving = true
  try {
    const r = await agentsStore.setAgentTimeout(props.agent.name, minutesToSeconds(minutes))
    timeout.minutes = secondsToMinutes(r?.execution_timeout_seconds ?? minutesToSeconds(minutes))
    editing.value = null
    toast(`Execution timeout set to ${timeout.minutes} min. New runs use it.`)
  } catch (e) {
    const { message, schedulesConflict } = describeTimeoutError(e)
    timeout.saveError = message
    timeout.schedulesConflict = schedulesConflict
  } finally {
    timeout.saving = false
  }
}

// ---- parallel capacity -------------------------------------------------------
const capacity = reactive({ state: 'loading', stored: null, effective: null, ceiling: 10, active: 0, saving: false, inputError: '', saveError: '' })
const capacityHint = computed(() => {
  const base = 'Jobs that may run at once. Beyond it, work queues.'
  if (capacity.state !== 'loaded') return base
  // A stored value above the ceiling is clamped by the backend; say so, or the
  // row shows a number the owner never typed with no reason given.
  const clamped = capacity.stored !== null && capacity.stored > capacity.ceiling
    ? ` Stored ${capacity.stored} is above the fleet ceiling, so ${capacity.effective} applies.`
    : ''
  return `${base} ${capacity.active} running now; fleet ceiling ${capacity.ceiling}.${clamped}`
})
async function loadCapacity() {
  if (capacity.effective === null) capacity.state = 'loading'
  try {
    const r = await agentsStore.getAgentCapacity(props.agent.name)
    capacity.stored = r?.max_parallel_tasks ?? null
    capacity.ceiling = r?.ceiling ?? 10
    capacity.effective = r?.effective_max_parallel_tasks ?? r?.max_parallel_tasks ?? null
    capacity.active = r?.active_slots ?? 0
    capacity.state = 'loaded'
  } catch {
    if (capacity.effective === null) capacity.state = 'failed'
  }
}
async function saveCapacity() {
  const n = Number(draft.capacity)
  capacity.saveError = ''
  if (!Number.isInteger(n) || n < 1 || n > capacity.ceiling) {
    capacity.inputError = `Enter a whole number from 1 to ${capacity.ceiling}, for example 3.`
    return
  }
  capacity.inputError = ''
  capacity.saving = true
  try {
    await agentsStore.setAgentCapacity(props.agent.name, n)
    await loadCapacity()
    editing.value = null
    toast(`Parallel capacity set to ${n}.`)
  } catch (e) {
    capacity.saveError = apiErrorMessage(e, "The capacity wasn't saved. Try again.")
  } finally {
    capacity.saving = false
  }
}

// ---- memory and CPU ------------------------------------------------------
const effectiveMemory = computed(() => props.resourceLimits.memory || props.resourceLimits.current_memory || '4g')
const effectiveCpu = computed(() => String(props.resourceLimits.cpu || props.resourceLimits.current_cpu || '2'))
const resourcesPending = computed(() => {
  const r = props.resourceLimits
  return !r.error && ((!!r.memory && r.memory !== r.current_memory) || (!!r.cpu && String(r.cpu) !== String(r.current_cpu)))
})
function saveResources() {
  emit('save-resources', { memory: draft.memory || null, cpu: draft.cpu || null })
  editing.value = null
}

// ---- danger zone -----------------------------------------------------------
const confirmReset = ref(false)
const resetting = ref(false)
const resetError = ref('')
async function resetToMain() {
  confirmReset.value = false
  resetting.value = true
  resetError.value = ''
  try {
    await agentsStore.resetToMainPreserveState(props.agent.name)
    toast('The workspace now matches main. Memory, credentials and schedules were kept.')
  } catch (e) {
    resetError.value = apiErrorMessage(e, "The workspace wasn't reset. Try again.")
  } finally {
    resetting.value = false
  }
}

function loadAll() {
  loadInfo()
  loadTimeout()
  loadCapacity()
  loadModel()
  loadGitConfig()
}
onMounted(loadAll)
watch(() => props.agent.name, () => {
  editing.value = null
  info.data = null
  capacity.effective = null
  loadAll()
})
watch(() => props.agent.status, (now, before) => {
  if (now === 'running' && before !== 'running') { loadModel(); loadInfo() }
})
</script>
