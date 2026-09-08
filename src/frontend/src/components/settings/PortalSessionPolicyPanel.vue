<!--
  Workspace session policy (ent#375).

  How long a client stays signed in to the Workspace. The sliding session itself
  is OSS — every install renews on use — so the READ is available in every
  edition and this panel always shows the policy actually in force. Only the
  SETTER is entitled; unentitled, the values render read-only with the reason.

  Showing the numbers everywhere is the point: it is a security control that
  applies to a community install exactly as much as to an enterprise one, and an
  operator who cannot see it cannot reason about it.
-->
<template>
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
    <div class="px-6 py-5">
      <div class="flex items-center justify-between">
        <div>
          <h3 class="text-lg font-medium text-gray-900 dark:text-gray-100">Workspace sessions</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
            How long a client stays signed in to the Workspace. The session renews
            while they use it, ends after the idle window, and never outlives the cap.
          </p>
        </div>
        <span
          v-if="policy"
          class="px-2 py-0.5 text-xs rounded-full"
          :class="policy.editable
            ? 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900 dark:text-indigo-200'
            : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'"
        >{{ policy.editable ? 'Enterprise' : 'Community' }}</span>
      </div>
      <div v-if="loading" class="mt-4 text-sm text-gray-500 dark:text-gray-400">Loading…</div>

      <!-- A failure to LOAD is distinct from a failure to SAVE, and from being
           unentitled. Collapsing them would tell an operator "you can't edit
           this" when the truth is "we couldn't reach the server". -->
      <div v-else-if="loadError" class="mt-4 rounded-md bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 p-4">
        <p class="text-sm text-red-700 dark:text-red-300">{{ loadError }}</p>
        <button type="button" class="mt-2 text-sm font-medium text-red-700 dark:text-red-300 underline" @click="load">Retry</button>
      </div>

      <div v-else-if="policy" class="mt-5 space-y-4">
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div v-for="f in FIELDS" :key="f.key">
            <label :for="`psp-${f.key}`" class="block text-sm font-medium text-gray-700 dark:text-gray-300">{{ f.label }}</label>
            <div class="mt-1 flex items-center gap-2">
              <input
                :id="`psp-${f.key}`"
                v-model.number="form[f.key]"
                type="number"
                min="0"
                step="0.5"
                :disabled="!policy.editable || saving"
                :class="SETTINGS_NUMBER_INPUT_CLASS"
              />
              <span class="text-sm text-gray-500 dark:text-gray-400">days</span>
              <!-- "Configured to 7" and "defaulted to 7" are different facts.
                   Without this an operator cannot tell a deliberate setting from
                   a shipped value a future default change may move (#1638). -->
              <span
                class="text-xs px-1.5 py-0.5 rounded"
                :class="policy.sources[f.settingKey] === 'db-row'
                  ? 'bg-blue-50 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
                  : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'"
              >{{ policy.sources[f.settingKey] === 'db-row' ? 'configured' : 'default' }}</span>
            </div>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">{{ f.help }}</p>
          </div>
        </div>

        <!-- The backend's named refusals, shown where they were caused. The
             cap-shorter-than-idle one is what an operator actually hits, and it
             already says which way to move. -->
        <div v-if="saveError" class="rounded-md bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 p-3">
          <p class="text-sm text-red-700 dark:text-red-300">{{ saveError }}</p>
        </div>
        <div v-if="policy.editable" class="flex items-center gap-3 pt-2">
          <button
            type="button"
            :disabled="saving || !dirty"
            :class="SETTINGS_PRIMARY_BUTTON_CLASS"
            @click="save"
          >{{ saving ? 'Saving…' : 'Save sessions' }}</button>
          <span v-if="saved" class="text-sm text-green-600 dark:text-green-400">Saved — applied live.</span>
          <button v-if="dirty" type="button" class="text-sm text-gray-600 dark:text-gray-400 underline" @click="reset">Discard</button>
          <span class="text-xs text-gray-400">
            Idle at least {{ policy.min_idle_minutes }} min · cap at most {{ policy.max_absolute_days }} days ·
            revoking a client signs them out immediately regardless.
          </span>
        </div>

        <!-- ent#473: whether the Workspace's generated chat titles are landing.
             Every path in that generator is fail-soft for the client, which
             left the operator with only a debug line; this is the one
             Settings surface every edition renders for the Workspace, so a
             bad state is said HERE, once, with the next action. Nothing
             renders while it works. -->
        <div
          v-if="titleNotice"
          class="rounded-md border border-status-warning-200 dark:border-status-warning-500/30 bg-status-warning-50 dark:bg-status-warning-500/10 p-3"
          role="status"
          data-testid="title-generation-notice"
        >
          <p class="text-sm font-medium text-status-warning-800 dark:text-status-warning-300">{{ titleNotice.title }}</p>
          <p class="mt-1 text-xs text-status-warning-700 dark:text-status-warning-300">{{ titleNotice.body }}</p>
        </div>

        <!-- Unentitled: state the current policy as a fact, not a dead end. -->
        <div v-else class="rounded-md bg-indigo-50 dark:bg-indigo-900/30 border border-indigo-200 dark:border-indigo-800 p-4">
          <p class="text-sm text-indigo-800 dark:text-indigo-200">
            Sessions already slide on this install — clients stay signed in for
            <strong>{{ policy.idle_days }}</strong> idle days, up to
            <strong>{{ policy.absolute_days }}</strong> days total. Tuning these
            windows is an Enterprise feature.
          </p>
        </div>

      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import api from '../../api'
import { SETTINGS_NUMBER_INPUT_CLASS, SETTINGS_PRIMARY_BUTTON_CLASS } from './fieldStyles'
import { titleGenerationNotice } from '../portal/portalUtils'

const FIELDS = [
  { key: 'idle_days', settingKey: 'portal_session_idle_days', label: 'Idle window',
    help: 'No requests for this long and the session ends.' },
  { key: 'absolute_days', settingKey: 'portal_session_absolute_days', label: 'Maximum session age',
    help: 'Measured from first sign-in. Use never extends it.' },
]

const policy = ref(null)
const loading = ref(true)
const loadError = ref(null)
const saving = ref(false)
const saveError = ref(null)
const saved = ref(false)
const form = reactive({ idle_days: null, absolute_days: null })

// ent#473 — derived from the payload's `title_generation`; null while fine.
const titleNotice = computed(() => titleGenerationNotice(policy.value?.title_generation))

const dirty = computed(() =>
  !!policy.value && FIELDS.some(f => Number(form[f.key]) !== Number(policy.value[f.key]))
)

function adopt(data) {
  // MERGE, don't replace. The GET and the PUT are different endpoints (OSS read,
  // entitled setter) and a field present in one but absent from the other would
  // silently vanish here. That happened: the PUT response omitted `editable`, so
  // one successful save disabled the form and showed the community upsell. The
  // shapes are aligned now; merging means the next divergence degrades to a
  // stale field instead of a broken form.
  policy.value = { ...(policy.value || {}), ...data }
  FIELDS.forEach(f => { form[f.key] = policy.value[f.key] })
}

async function load() {
  loading.value = true
  loadError.value = null
  try {
    // The READ is the OSS endpoint, so it answers in every edition. Reading the
    // entitled one here would 404 exactly where the community message belongs.
    const { data } = await api.get('/api/settings/portal-session-policy')
    adopt(data)
  } catch (e) {
    loadError.value = e.response?.data?.detail || 'Could not load the session policy.'
  } finally {
    loading.value = false
  }
}

function reset() {
  if (policy.value) adopt(policy.value)
  saveError.value = null
  saved.value = false
}

async function save() {
  saving.value = true
  saveError.value = null
  saved.value = false
  try {
    // Send only what changed: the backend judges a partial update against the
    // value currently in force, so sending both would re-assert a window the
    // operator did not touch.
    const body = {}
    FIELDS.forEach(f => {
      if (Number(form[f.key]) !== Number(policy.value[f.key])) body[f.key] = Number(form[f.key])
    })
    const { data } = await api.put('/api/enterprise/portal-session-policy', body)
    adopt(data)
    saved.value = true
  } catch (e) {
    // The backend's refusals are already written for a human ("absolute_days (7)
    // is shorter than idle_days (30) … Raise the cap or lower the idle window"),
    // so surface them verbatim rather than replacing them with "Invalid input".
    saveError.value = e.response?.data?.detail || 'Could not save the session policy.'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>
