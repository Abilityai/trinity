<template>
  <!--
    Settings › Integrations → the instance's Nevermined organization
    (abilityai/trinity-enterprise#966/#967, TD-1). Admin-only, as the tab is.
    Nevermined holds the card and the mandate; the two secrets here are
    write-only — the API never returns them and this form never shows them.
    Semantic tokens + gray neutrals only.
  -->
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg" data-testid="nvm-org-panel">
    <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
      <div class="flex items-center gap-2">
        <h2 class="text-lg font-medium text-gray-900 dark:text-white">{{ COPY.title }}</h2>
        <BaseBadge v-if="isReady" :variant="configured ? 'success' : 'neutral'" data-testid="nvm-org-status">
          <span aria-hidden="true">{{ configured ? '✓' : '○' }}</span>
          {{ configured ? COPY.configured : COPY.notConfigured }}
        </BaseBadge>
      </div>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ COPY.subtitle }}</p>
    </div>

    <div class="p-6 space-y-5">
      <div v-if="view.state === 'loading'" aria-busy="true" data-testid="nvm-org-loading">
        <span class="sr-only">{{ COPY.loading }}</span>
        <SkeletonLoader :count="4" height="2rem" gap="1rem" />
      </div>

      <LoadFailed
        v-else-if="view.state === 'failed'"
        dense
        :title="COPY.failedTitle"
        :message="COPY.failedMessage"
        :detail="error"
        :retrying="loading"
        @retry="load"
      />

      <template v-else>
        <InlineError
          v-if="view.stale"
          :message="staleMessage"
          retryable
          @retry="load"
          @dismiss="error = ''"
        />

        <dl
          v-if="configured"
          class="grid grid-cols-1 sm:grid-cols-3 gap-3 text-sm"
          data-testid="nvm-org-summary"
        >
          <div class="min-w-0">
            <dt class="font-mono text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ COPY.envLabel }}</dt>
            <dd class="mt-0.5 font-medium text-gray-900 dark:text-gray-100">{{ data.environment }}</dd>
          </div>
          <div class="min-w-0">
            <dt class="font-mono text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ COPY.mandatesLabel }}</dt>
            <dd class="mt-0.5 font-medium tabular-nums text-gray-900 dark:text-gray-100" data-testid="nvm-org-active">
              {{ data.active_delegations ?? 0 }}
            </dd>
          </div>
          <div v-if="data.updated_at" class="min-w-0">
            <dt class="font-mono text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ COPY.updatedLabel }}</dt>
            <dd class="mt-0.5 text-gray-900 dark:text-gray-100 truncate" :title="formatLocalDateTime(data.updated_at)">
              {{ formatRelativeTime(data.updated_at) }}<template v-if="data.updated_by"> · {{ data.updated_by }}</template>
            </dd>
          </div>
        </dl>

        <form class="space-y-4" data-testid="nvm-org-form" @submit.prevent="save">
          <BaseInput
            v-model="orgId"
            :label="COPY.orgIdLabel"
            autocomplete="off"
            data-testid="nvm-org-id"
          />
          <BaseSelect
            :model-value="environment"
            :label="COPY.envLabel"
            :help="COPY.envHelp"
            data-testid="nvm-org-env"
            @update:model-value="onEnvironmentChange"
          >
            <option value="sandbox">sandbox</option>
            <option value="live">live</option>
          </BaseSelect>
          <BaseInput
            v-model="widgetSecret"
            type="password"
            autocomplete="new-password"
            :label="COPY.widgetSecretLabel"
            :placeholder="configured ? COPY.rotatePlaceholder : COPY.requiredPlaceholder"
            :help="configured ? COPY.rotateHelp : COPY.widgetSecretHelp"
            data-testid="nvm-org-widget-secret"
          />
          <BaseInput
            v-model="agentApiKey"
            type="password"
            autocomplete="new-password"
            :label="COPY.agentKeyLabel"
            :placeholder="configured ? COPY.rotatePlaceholder : COPY.requiredPlaceholder"
            :help="configured ? COPY.rotateHelp : COPY.agentKeyHelp"
            data-testid="nvm-org-agent-key"
          />

          <p v-if="!configured && !secretsComplete" class="text-xs text-gray-500 dark:text-gray-400" data-testid="nvm-org-secrets-needed">
            {{ COPY.bothSecretsNeeded }}
          </p>

          <InlineError :message="saveError" data-testid="nvm-org-save-error" @dismiss="saveError = ''" />
          <p v-if="saved" role="status" class="text-sm text-status-success-700 dark:text-status-success-300" data-testid="nvm-org-saved">
            <span aria-hidden="true">✓</span> {{ COPY.saved }}
          </p>

          <div class="flex flex-wrap items-center gap-2">
            <BaseButton
              type="submit"
              variant="primary"
              :disabled="!canSave"
              :loading="saving"
              :loading-label="COPY.saving"
              data-testid="nvm-org-save"
            >
              {{ COPY.save }}
            </BaseButton>
            <BaseButton
              v-if="configured"
              variant="danger"
              :loading="removing"
              :loading-label="COPY.removing"
              data-testid="nvm-org-remove"
              @click="confirmRemove = true"
            >
              {{ COPY.remove }}
            </BaseButton>
          </div>
          <InlineError :message="removeError" data-testid="nvm-org-remove-error" @dismiss="removeError = ''" />
        </form>
      </template>
    </div>

    <ConfirmDialog
      v-model:visible="confirmEnvironment"
      :title="COPY.envConfirmTitle"
      :message="COPY.envConfirmMessage"
      :confirm-text="COPY.envConfirm"
      variant="warning"
      confirm-variant="primary"
      @confirm="keepEnvironment"
      @cancel="resetEnvironment"
    />
    <ConfirmDialog
      v-model:visible="confirmRemove"
      :title="COPY.removeTitle"
      :message="COPY.removeMessage"
      :confirm-text="COPY.remove"
      variant="danger"
      @confirm="remove"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import api from '../../api'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseSelect from '../base/BaseSelect.vue'
import ConfirmDialog from '../ConfirmDialog.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'
import { apiErrorCode, apiErrorMessage } from '../../utils/apiError'
import { staleBannerMessage, viewState } from '../../utils/loadingState'
import { formatLocalDateTime, formatRelativeTime } from '../../utils/timestamps'

// Every string names Nevermined; none reads as Trinity holding a card or
// settling money (ent#932 — tests/unit/test_ent966_facilitator_wording.py
// scans this file).
const COPY = Object.freeze({
  title: 'Nevermined organization',
  subtitle:
    "Agents on this instance fund outside purchases through a Nevermined card mandate. Nevermined holds the card and settles each purchase; this form keeps the Nevermined organization's widget secret and agent API key, encrypted, to open Nevermined's card form and to give a funded agent its Nevermined key.",
  configured: 'Nevermined configured',
  notConfigured: 'Nevermined not configured',
  loading: 'Loading the Nevermined organization…',
  failedTitle: "Couldn't load the Nevermined organization",
  failedMessage: 'The Nevermined organization read failed. Check your connection and try again.',
  staleSubject: 'the Nevermined organization',
  orgIdLabel: 'Nevermined organization id',
  envLabel: 'Nevermined environment',
  envHelp: 'Start in the Nevermined sandbox; switch to live when real cards should be charged by Nevermined.',
  widgetSecretLabel: 'Nevermined widget secret',
  widgetSecretHelp: "Opens Nevermined's card form for an agent's owner. Never shown again after saving.",
  agentKeyLabel: 'Nevermined agent API key',
  agentKeyHelp: 'Written into a funded agent\'s credentials so it can spend through its Nevermined mandate.',
  requiredPlaceholder: 'Required for the first Nevermined setup',
  rotatePlaceholder: 'Configured with Nevermined — paste to rotate',
  rotateHelp: 'Leave blank to keep the stored Nevermined value; paste a new one to rotate it.',
  bothSecretsNeeded: 'The first Nevermined setup needs both the widget secret and the agent API key.',
  mandatesLabel: 'Agents with a Nevermined mandate',
  updatedLabel: 'Nevermined settings updated',
  save: 'Save Nevermined organization',
  saving: 'Saving the Nevermined organization…',
  saved: 'Nevermined organization saved.',
  remove: 'Remove Nevermined organization',
  removing: 'Removing the Nevermined organization…',
  removeTitle: 'Remove the Nevermined organization?',
  removeMessage:
    'Agents on this instance can no longer enroll a card with Nevermined until an admin sets the Nevermined organization up again. Nevermined refuses this while any agent holds an active Nevermined mandate.',
  envConfirmTitle: 'Switch the Nevermined environment?',
  envConfirmMessage:
    'New card mandates are created in the Nevermined environment you pick, and the Nevermined agent API key must belong to that same environment. Nothing changes until you save.',
  envConfirm: 'Switch Nevermined environment',
  activeCount: (n) => `${n} agent${n === 1 ? '' : 's'} still hold${n === 1 ? 's' : ''} an active Nevermined mandate — revoke ${n === 1 ? 'it' : 'them'} on each agent's Payments tab first.`,
})

const data = ref(null)
const hasLoaded = ref(false)
const loading = ref(false)
const error = ref('')
const lastLoadedAt = ref(null)

const orgId = ref('')
const environment = ref('sandbox')
const widgetSecret = ref('')
const agentApiKey = ref('')
const saving = ref(false)
const saved = ref(false)
const saveError = ref('')
const removing = ref(false)
const removeError = ref('')
const confirmRemove = ref(false)
const confirmEnvironment = ref(false)
// The select's value before a change that is waiting on the confirm (draft binding).
let pendingEnvironmentFrom = null

const view = computed(() => viewState({ hasLoaded: hasLoaded.value, error: error.value, count: 1 }))
const isReady = computed(() => !['loading', 'failed'].includes(view.value.state))
const staleMessage = computed(() => staleBannerMessage(COPY.staleSubject, lastLoadedAt.value))
const configured = computed(() => Boolean(data.value?.configured))
const secretsComplete = computed(() => Boolean(widgetSecret.value.trim() && agentApiKey.value.trim()))
const canSave = computed(() => Boolean(orgId.value.trim()) && (configured.value || secretsComplete.value) && !saving.value)

function adopt(state) {
  data.value = state
  hasLoaded.value = true
  lastLoadedAt.value = Date.now()
  error.value = ''
  orgId.value = state?.org_id || ''
  environment.value = state?.environment || 'sandbox'
  // Write-only: a saved secret is never echoed back into a field.
  widgetSecret.value = ''
  agentApiKey.value = ''
}

async function load() {
  loading.value = true
  try {
    const { data: body } = await api.get('/api/nevermined/organization')
    adopt(body)
  } catch (err) {
    error.value = apiErrorMessage(err, 'The request failed.')
  } finally {
    loading.value = false
  }
}

function onEnvironmentChange(value) {
  const from = environment.value
  environment.value = value
  saved.value = false
  // A configured organization's environment moves new mandates: ask first.
  if (configured.value && value !== (data.value?.environment || 'sandbox') && pendingEnvironmentFrom === null) {
    pendingEnvironmentFrom = from
    confirmEnvironment.value = true
  }
}
function keepEnvironment() {
  pendingEnvironmentFrom = null
}
function resetEnvironment() {
  if (pendingEnvironmentFrom !== null) environment.value = pendingEnvironmentFrom
  pendingEnvironmentFrom = null
}

function failureMessage(err) {
  const message = apiErrorMessage(err, 'The Nevermined organization could not be saved.')
  if (apiErrorCode(err) !== 'delegations_active') return message
  const n = err?.response?.data?.detail?.active_delegations
  return typeof n === 'number' ? `${message} ${COPY.activeCount(n)}` : message
}

async function save() {
  if (!canSave.value) return
  saving.value = true
  saved.value = false
  saveError.value = ''
  const body = { org_id: orgId.value.trim(), environment: environment.value }
  // Not configured: both are required. Configured: an empty field keeps the stored value.
  if (widgetSecret.value.trim()) body.widget_secret = widgetSecret.value
  if (agentApiKey.value.trim()) body.agent_api_key = agentApiKey.value
  try {
    const { data: state } = await api.put('/api/nevermined/organization', body)
    adopt(state)
    saved.value = true
  } catch (err) {
    saveError.value = failureMessage(err)
  } finally {
    saving.value = false
  }
}

async function remove() {
  removing.value = true
  removeError.value = ''
  saved.value = false
  try {
    const { data: state } = await api.delete('/api/nevermined/organization')
    adopt(state)
  } catch (err) {
    removeError.value = failureMessage(err)
  } finally {
    removing.value = false
  }
}

onMounted(load)
</script>
