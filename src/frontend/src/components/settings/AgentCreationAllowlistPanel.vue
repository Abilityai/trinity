<template>
  <!--
    Template sources for agent-created agents (trinity-enterprise#591).

    Its own file, like TemplateRegistryPanel: Settings.vue carries a raw-colour
    baseline the ratchet only lets shrink, so it gains one import and one tag.
    Semantic tokens (`status-*`) plus gray neutrals only.

    `bounded` — never the entry count — decides the badge and the empty line: a
    damaged stored row bounds agents to nothing while listing zero entries, and
    must not read as "Any source".
  -->
  <div
    class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg"
    data-testid="agent-creation-allowlist"
  >
    <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
      <div class="flex items-center gap-2">
        <h2 class="text-lg font-medium text-gray-900 dark:text-white">
          Template sources for agent-created agents
        </h2>
        <BaseBadge
          v-if="view.state === 'ready'"
          :variant="config.stored_invalid ? 'warning' : config.bounded ? 'info' : 'neutral'"
          data-testid="allowlist-badge"
        >
          {{ badgeLabel }}
        </BaseBadge>
      </div>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Limits which templates an agent may build other agents from: durable agents created
        with the agent-management permission, and ephemeral helpers. People are never limited.
        While the list has entries, agents also cannot deploy uploaded archives.
      </p>
    </div>

    <div class="p-6 space-y-5">
      <div v-if="view.state === 'loading'" aria-busy="true">
        <span class="sr-only">Loading the template sources…</span>
        <SkeletonLoader :count="3" height="2rem" gap="0.75rem" />
      </div>

      <LoadFailed
        v-else-if="view.state === 'failed'"
        dense
        title="Couldn't load the template sources"
        :message="loadError"
        :retrying="loading"
        @retry="load"
      />

      <template v-else>
        <InlineError
          v-if="view.stale"
          :message="`Couldn't refresh the template sources — showing the last list that loaded. ${loadError}`"
          retryable
          @retry="load"
          @dismiss="loadError = ''"
        />

        <!-- A failed SAVE: persists next to the control (principle 18). The
             per-line reasons are the fix, not a technical detail, so they are
             listed in the open rather than behind the disclosure. -->
        <div v-if="saveError" class="space-y-2">
          <InlineError :message="saveError" :detail="saveErrorDetail" @dismiss="clearSaveError" />
          <ul v-if="lineErrors.length" class="space-y-1" data-testid="allowlist-line-errors">
            <li
              v-for="err in lineErrors"
              :key="err.key"
              class="text-sm text-status-danger-700 dark:text-status-danger-300 break-words"
            >
              {{ err.text }}
            </li>
          </ul>
        </div>

        <!-- A damaged stored row (only a direct DB write can make one). -->
        <div
          v-if="config.stored_invalid"
          class="rounded-md border border-status-warning-200 dark:border-status-warning-500/30 bg-status-warning-50 dark:bg-status-warning-500/10 px-4 py-3 text-sm text-status-warning-700 dark:text-status-warning-300"
          data-testid="allowlist-damaged"
        >
          The saved list couldn't be read in full. Agents are limited to the valid entries shown
          below — none, if nothing could be read. Save the list again to repair it.
        </div>

        <div>
          <BaseTextarea
            id="agent-creation-allowlist"
            v-model="draft"
            label="Allowed sources"
            mono
            :rows="5"
            :disabled="saving"
            spellcheck="false"
            :placeholder="PLACEHOLDER"
            :help="HINT"
          />
          <p
            v-if="!config.bounded && !dirty"
            class="mt-2 text-sm text-gray-600 dark:text-gray-300"
            data-testid="allowlist-empty"
          >
            Any source: agents may build from any template they can reach.
          </p>
          <p
            v-if="config.not_bundled.length && !dirty"
            class="mt-2 text-sm text-status-warning-700 dark:text-status-warning-300 break-words"
            data-testid="allowlist-not-bundled"
          >
            Not a bundled template, so agents are refused it:
            <span class="font-mono">{{ config.not_bundled.join(', ') }}</span>
          </p>
          <div class="mt-3 flex items-center gap-2">
            <BaseButton
              :disabled="saving || !dirty"
              :loading="saving"
              loading-label="Saving…"
              data-testid="allowlist-save"
              @click="save"
            >
              Save
            </BaseButton>
            <BaseButton
              variant="secondary"
              :disabled="saving || !dirty"
              data-testid="allowlist-discard"
              @click="discard"
            >
              Discard
            </BaseButton>
          </div>
        </div>

        <!-- Impact: what turning the list on (or changing it) refuses today. -->
        <div class="rounded-md border border-gray-200 dark:border-gray-700 px-4 py-3">
          <p class="text-sm font-medium text-gray-900 dark:text-gray-100">
            Templates agent-created agents use now
          </p>
          <p
            v-if="config.in_use === null"
            class="mt-1 text-sm text-gray-600 dark:text-gray-300"
            data-testid="allowlist-in-use-unknown"
          >
            Couldn't read which templates live agent-created agents use. The list above still applies.
          </p>
          <p
            v-else-if="!config.in_use.length"
            class="mt-1 text-sm text-gray-600 dark:text-gray-300"
            data-testid="allowlist-in-use-none"
          >
            No live agent was created by another agent.
          </p>
          <template v-else>
            <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400 tabular-nums">
              {{ inUseTotal }} agent{{ inUseTotal === 1 ? '' : 's' }} · checked against the saved list
            </p>
            <ul class="mt-2 max-h-48 overflow-y-auto space-y-1" data-testid="allowlist-in-use">
              <li
                v-for="row in config.in_use"
                :key="row.source"
                class="flex items-center gap-2 text-sm"
              >
                <!-- Shape and word as well as hue (principle 24). -->
                <span
                  :class="row.allowed
                    ? 'text-status-success-700 dark:text-status-success-400'
                    : 'text-status-danger-700 dark:text-status-danger-400'"
                  class="flex-none w-20"
                >
                  <span aria-hidden="true">{{ row.allowed ? '✓' : '✗' }}</span>
                  {{ row.allowed ? 'allowed' : 'refused' }}
                </span>
                <span class="font-mono text-xs text-gray-700 dark:text-gray-300 break-all min-w-0 flex-1">
                  {{ row.source }}
                </span>
                <span class="flex-none text-xs text-gray-500 dark:text-gray-400 tabular-nums">
                  {{ row.agents }} agent{{ row.agents === 1 ? '' : 's' }}
                </span>
              </li>
            </ul>
          </template>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import api from '../../api'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseTextarea from '../base/BaseTextarea.vue'
import { viewState } from '../../utils/loadingState'

const URL = '/api/settings/agent-creation-allowlist'
const PLACEHOLDER = 'local:scout\ngithub:your-org/*\ngithub:your-org/your-repo'
const HINT =
  'One per line: local:<template> (a bundled template), github:<owner>/* for every repo of an owner, ' +
  'github:<owner>/<repo> for every branch of a repo, or github:<owner>/<repo>@<branch-or-tag> for exactly one.'

const config = ref({
  entries: [],
  bounded: false,
  stored_invalid: false,
  not_bundled: [],
  in_use: [],
})
const draft = ref('')
const loading = ref(false)
const hasLoaded = ref(false)
const saving = ref(false)
const loadError = ref('')
const saveError = ref('')
const saveErrorDetail = ref('')
const lineErrors = ref([])

const view = computed(() => viewState({
  loading: loading.value,
  hasLoaded: hasLoaded.value,
  error: loadError.value || null,
}))

function linesOf(text) {
  return text.split('\n').map((l, i) => ({ text: l.trim(), line: i + 1 })).filter((l) => l.text)
}

const dirty = computed(() =>
  linesOf(draft.value).map((l) => l.text).join('\n') !== config.value.entries.join('\n'))

const badgeLabel = computed(() => {
  if (!config.value.bounded) return 'Any source'
  const n = config.value.entries.length
  return `${n} source${n === 1 ? '' : 's'}`
})

const inUseTotal = computed(() =>
  (config.value.in_use || []).reduce((sum, row) => sum + (row.agents || 0), 0))

function apply(data) {
  config.value = {
    entries: data.entries || [],
    bounded: !!data.bounded,
    stored_invalid: !!data.stored_invalid,
    not_bundled: data.not_bundled || [],
    in_use: data.in_use === undefined ? [] : data.in_use,
  }
  draft.value = config.value.entries.join('\n')
}

function clearSaveError() {
  saveError.value = ''
  saveErrorDetail.value = ''
  lineErrors.value = []
}

function failureMessage(e, fallback) {
  const detail = e?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  return fallback
}

async function load() {
  loading.value = true
  try {
    const { data } = await api.get(URL)
    apply(data)
    loadError.value = ''
    hasLoaded.value = true
  } catch (e) {
    loadError.value = failureMessage(e, 'The settings request failed. Retry, or check the backend logs.')
  } finally {
    loading.value = false
  }
}

async function save() {
  clearSaveError()
  const lines = linesOf(draft.value)
  saving.value = true
  try {
    const { data } = await api.put(URL, { entries: lines.map((l) => l.text) })
    apply(data)
  } catch (e) {
    saveError.value = failureMessage(e, "Couldn't save the template sources.")
    const errors = e?.response?.data?.detail?.errors
    if (Array.isArray(errors)) {
      // The server indexes the lines it was sent; map back to textarea lines.
      lineErrors.value = errors.map((err, i) => {
        const line = Number.isInteger(err.index) ? lines[err.index]?.line : null
        const where = line ? `Line ${line} ${err.entry ?? ''}`.trim() : 'The list'
        return { key: `${i}-${err.index}`, text: `${where} — ${err.reason}` }
      })
    } else {
      saveErrorDetail.value = e?.message || ''
    }
  } finally {
    saving.value = false
  }
}

function discard() {
  clearSaveError()
  draft.value = config.value.entries.join('\n')
}

onMounted(load)
</script>
