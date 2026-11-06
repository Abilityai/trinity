<template>
  <!--
    Settings › Guardrails › Tool access (trinity-enterprise#341, GUARD-008).

    One answer to "what may this agent do?": four named profiles and Custom.
    A profile sets the read-only layer and the tools the agent can't use
    together, so the two can no longer disagree; read-only mode is one of the
    profiles. The cards, their plain-language statements and the per-runtime
    coverage line all come from the backend, so the API and this pane say the
    same thing. The read-only part applies at once; the tool list on the next
    restart, which the one restart banner names.
  -->
  <SettingsSection
    title="Tool access"
    description="What this agent may use. One choice sets read-only mode and the tools it can't use, together."
  >
    <div
      v-if="vs.state === 'loading'"
      class="px-4 py-3 space-y-2"
      aria-busy="true"
      data-testid="tool-access-loading"
    >
      <div
        v-for="n in 5"
        :key="n"
        class="h-[68px] rounded-lg bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"
      ></div>
      <span class="sr-only">Loading tool access…</span>
    </div>

    <LoadFailed
      v-else-if="vs.state === 'failed'"
      dense
      title="Couldn't load tool access"
      message="The agent's tool access could not be read. Try again."
      :detail="loadError"
      :retrying="retrying"
      @retry="retry"
    />

    <form v-else class="px-4 py-3 space-y-3" data-testid="tool-access" @submit.prevent="save">
      <p v-if="isSystem" class="text-[12.5px] text-gray-600 dark:text-gray-400">
        The system agent's tool access is managed by the platform.
      </p>

      <div role="radiogroup" aria-label="Tool access profile" class="space-y-2">
        <div
          v-for="p in data.profiles"
          :key="p.id"
          class="rounded-lg border px-3 py-2.5"
          :class="selected === p.id
            ? 'border-action-primary-500 bg-action-primary-50 dark:border-action-primary-400 dark:bg-action-primary-500/10'
            : 'border-gray-200 dark:border-gray-750'"
          :data-testid="`tool-access-${p.id}`"
        >
          <div class="flex items-start gap-3">
            <input
              :id="`tool-access-${agentName}-${p.id}`"
              v-model="selected"
              type="radio"
              name="tool-access-profile"
              :value="p.id"
              :disabled="isSystem || saving"
              class="mt-1 h-4 w-4 flex-none accent-action-primary-600 disabled:opacity-45"
            />
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <label
                  :for="`tool-access-${agentName}-${p.id}`"
                  class="text-sm font-medium text-gray-900 dark:text-gray-100"
                  :class="isSystem ? '' : 'cursor-pointer'"
                >{{ p.label }}</label>
                <BaseBadge v-if="p.id === data.profile" variant="neutral" size="sm">Current</BaseBadge>
                <BaseBadge v-if="p.id === data.profile && !data.applied" variant="warning" size="sm">
                  {{ notAppliedLabel }}
                </BaseBadge>
              </div>
              <p class="mt-0.5 text-[12.5px] text-gray-600 dark:text-gray-400">{{ p.statement }}</p>
              <p
                v-if="p.coverage"
                class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-400"
                data-testid="tool-access-coverage"
              >{{ p.coverage }}</p>
              <div
                v-if="!isSystem && ((p.id === data.profile && !data.applied) || p.id !== 'custom')"
                class="mt-2 flex flex-wrap gap-2"
              >
                <BaseButton
                  v-if="p.id === data.profile && !data.applied"
                  size="sm"
                  :disabled="saving"
                  data-testid="tool-access-apply"
                  @click="apply(p.id)"
                >Apply</BaseButton>
                <BaseButton
                  v-if="p.id !== 'custom'"
                  variant="ghost"
                  size="sm"
                  :disabled="saving"
                  data-testid="tool-access-customize"
                  @click="customize(p)"
                >Customize</BaseButton>
              </div>
            </div>
          </div>

          <div
            v-if="p.id === 'custom' && selected === 'custom'"
            class="mt-3 ml-7 space-y-3"
            data-testid="tool-access-custom"
          >
            <label class="flex items-start gap-2 text-[13.5px] text-gray-900 dark:text-gray-100">
              <input
                v-model="draft.read_only"
                type="checkbox"
                :disabled="isSystem || saving"
                class="mt-0.5 h-4 w-4 flex-none accent-action-primary-600"
                data-testid="tool-access-custom-read-only"
              />
              <span>Read-only: block edits to its code, instructions and config files</span>
            </label>
            <BaseTextarea
              v-model="draft.tools"
              label="Tools to remove"
              mono
              :rows="5"
              :disabled="isSystem || saving"
              :error="listError"
              help="One per line. Common ones: Bash, Write, Edit, WebFetch, WebSearch. A Trinity tool is written mcp__trinity__<tool>."
            />
          </div>
        </div>
      </div>

      <InlineError :message="saveError" @dismiss="saveError = ''" />

      <div v-if="!isSystem" class="flex gap-2">
        <BaseButton
          type="submit"
          size="sm"
          :disabled="!dirty"
          :loading="saving"
          loading-label="Saving…"
          data-testid="tool-access-save"
        >Save</BaseButton>
        <BaseButton v-if="dirty" variant="ghost" size="sm" :disabled="saving" @click="reset">Cancel</BaseButton>
      </div>
    </form>
  </SettingsSection>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import { useAgentRestartStore } from '../../stores/agentRestart'
import { viewState } from '../../utils/loadingState'
import {
  CUSTOM, STRICT, parseToolList, saveMessage, toolListError, toolListText,
} from '../../utils/toolAccess'
import SettingsSection from './SettingsSection.vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseTextarea from '../base/BaseTextarea.vue'
import LoadFailed from '../LoadFailed.vue'
import InlineError from '../InlineError.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  isSystem: { type: Boolean, default: false },
  notify: { type: Function, default: null },
  // The profile the page last heard of (the header toggle changes it). When it
  // moves away from what this section shows, the section re-reads.
  currentProfile: { type: String, default: '' },
})
const emit = defineEmits(['saved'])

const agentsStore = useAgentsStore()
const restartStore = useAgentRestartStore()

const data = ref(null)
const loadError = ref('')
const retrying = ref(false)
const selected = ref('')
const draft = reactive({ read_only: false, tools: '' })
const saving = ref(false)
const saveError = ref('')
const listError = ref('')

const vs = computed(() => viewState({ hasLoaded: !!data.value, error: loadError.value }))

const notAppliedLabel = computed(() => (
  data.value?.derived && data.value?.profile === STRICT ? 'Send block not applied' : 'Not applied'
))

const dirty = computed(() => {
  const d = data.value
  if (!d) return false
  if (selected.value !== d.profile) return true
  if (selected.value !== CUSTOM) return false
  const tools = parseToolList(draft.tools)
  return draft.read_only !== d.custom.read_only
    || tools.join('\n') !== (d.custom.disallowed_tools || []).join('\n')
})

function adopt(view) {
  data.value = view
  selected.value = view.profile
  draft.read_only = !!view.custom?.read_only
  draft.tools = toolListText(view.custom?.disallowed_tools)
  listError.value = ''
  restartStore.report(props.agentName, 'tool-access', 'Tool access', !!view.restart_required)
}

async function load() {
  const name = props.agentName
  try {
    const view = await agentsStore.getToolAccess(name)
    if (name !== props.agentName) return
    loadError.value = ''
    adopt(view)
  } catch (err) {
    if (name !== props.agentName) return
    loadError.value = err?.response?.data?.detail || err?.message || 'Request failed'
  }
}

async function retry() {
  retrying.value = true
  try { await load() } finally { retrying.value = false }
}

function reset() {
  if (data.value) adopt(data.value)
  saveError.value = ''
}

/** Open Custom pre-filled with a named profile's settings (saving makes it Custom). */
function customize(profile) {
  selected.value = CUSTOM
  draft.read_only = !!profile.read_only
  draft.tools = toolListText(profile.disallowed_tools)
  listError.value = ''
}

function apply(profileId) {
  selected.value = profileId
  return save()
}

async function save() {
  if (saving.value || props.isSystem) return
  saveError.value = ''
  listError.value = ''
  let body
  if (selected.value === CUSTOM) {
    const err = toolListError(draft.tools)
    if (err) {
      listError.value = err
      return
    }
    body = { profile: CUSTOM, custom: { read_only: draft.read_only, disallowed_tools: parseToolList(draft.tools) } }
  } else {
    body = { profile: selected.value }
  }
  const name = props.agentName
  const before = data.value
  saving.value = true
  try {
    const after = await agentsStore.setToolAccess(name, body)
    if (name !== props.agentName) return
    adopt(after)
    if (props.notify) props.notify(saveMessage(before, after), 'success')
    emit('saved', after)
  } catch (err) {
    saveError.value = err?.response?.data?.detail || err?.message || "Couldn't save tool access."
  } finally {
    saving.value = false
  }
}

onMounted(load)
watch(() => props.agentName, () => {
  data.value = null
  loadError.value = ''
  load()
})
// After a restart the backend knows whether the tool list now applies.
watch(() => restartStore.epochFor(props.agentName), () => { if (data.value) load() })
// The header's Read-only toggle changed the profile on this page.
watch(() => props.currentProfile, (profile) => {
  if (profile && data.value && !saving.value && profile !== data.value.profile) load()
})
</script>
