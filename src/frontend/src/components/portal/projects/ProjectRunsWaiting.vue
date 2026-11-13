<!--
  Runs waiting for a person's go (trinity-enterprise#1235).

  In Propose — or on a task held for a person's go, in any mode — the steward's
  start becomes a proposal. Each one also reaches the task's person as an
  approval ask; this card is the same decision on the page, for many at once.

  Each id is decided on its own (never all-or-nothing), so the answer is one
  outcome per id, shown beside it in words: "Started", or why it didn't. The
  server re-checks every gate at approval; this card only offers.
-->
<template>
  <BaseCard data-testid="project-runs-waiting">
    <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
      Waiting for your go <span v-if="rows.length" class="tabular-nums">· {{ rows.length }}</span>
    </h2>

    <SkeletonLoader v-if="view.state === 'loading'" :count="2" height="2.25rem" gap="0.5rem" class="mt-2" />
    <LoadFailed v-else-if="view.state === 'failed'" title="Couldn't load the runs waiting" :message="error" dense @retry="load" />
    <template v-else>
      <InlineError v-if="view.stale" :message="error" @dismiss="load" />
      <p v-if="view.state === 'empty'" class="mt-2 text-sm text-gray-600 dark:text-gray-300" data-testid="project-runs-waiting-empty">
        Nothing is waiting for your go.
      </p>
      <ul v-else class="mt-2 max-h-72 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-750">
        <li v-for="r in rows" :key="r.id" class="flex items-center gap-3 py-2 text-sm" :data-testid="`project-run-waiting-${r.id}`">
          <input
            v-if="canDecide"
            :id="`run-pick-${r.id}`"
            v-model="picked"
            type="checkbox"
            :value="r.id"
            class="h-4 w-4 rounded border-gray-300 dark:border-gray-700 text-action-primary-600 focus:ring-action-primary-500/40"
            :data-testid="`project-run-pick-${r.id}`"
          />
          <label :for="`run-pick-${r.id}`" class="min-w-0 flex-1">
            <span class="block truncate text-gray-900 dark:text-gray-100">
              <span class="font-mono text-xs text-gray-500 dark:text-gray-400">{{ r.task_id }}</span>
              on {{ r.agent_name }}
            </span>
            <span class="block text-xs text-gray-500 dark:text-gray-400">
              Proposed by {{ requester(r) }}<template v-if="r.requested_at"> · <span :title="absolute(r.requested_at)">{{ relative(r.requested_at) }}</span></template>
            </span>
          </label>
        </li>
      </ul>

      <ul v-if="outcomes.length" class="mt-2 space-y-1 text-xs" data-testid="project-runs-outcomes" aria-live="polite">
        <li v-for="o in outcomes" :key="o.id" :data-testid="`project-run-outcome-${o.id}`" class="text-gray-700 dark:text-gray-300">
          <span class="font-mono">{{ o.task || o.id }}</span>: {{ runOutcomeLabel(o.outcome) }}
        </li>
      </ul>

      <div v-if="canDecide && rows.length" class="mt-3 space-y-2">
        <BaseInput id="runs-decline-note" v-model="note" label="Note for a decline (optional)" maxlength="500" data-testid="project-runs-note" />
        <div class="flex flex-wrap gap-2">
          <BaseButton size="sm" :disabled="!picked.length" :loading="busy === 'approve'" loading-label="Approving…" data-testid="project-runs-approve" @click="decide('approve')">
            Approve selected
          </BaseButton>
          <BaseButton size="sm" variant="secondary" :disabled="!picked.length" :loading="busy === 'decline'" loading-label="Declining…" data-testid="project-runs-decline" @click="decide('decline')">
            Decline
          </BaseButton>
        </div>
      </div>
      <p v-else-if="!canDecide && rows.length" class="mt-2 text-xs text-gray-600 dark:text-gray-300">
        People who can work on this project's tasks give the go.
      </p>
      <InlineError v-if="actionError" :message="actionError" @dismiss="actionError = ''" />
    </template>
  </BaseCard>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import BaseInput from '@/components/base/BaseInput.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import { useProjectsStore } from '@/stores/projects'
import { viewState } from '@/utils/loadingState'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import { projectErrorMessage, runOutcomeLabel, sameEmail } from './projectsUtils'

const props = defineProps({
  projectId: { type: String, required: true },
  canContribute: { type: Boolean, default: false },
  archived: { type: Boolean, default: false },
  myEmail: { type: String, default: '' },
})

const store = useProjectsStore()
const rows = ref([])
const loaded = ref(false)
const error = ref('')
const picked = ref([])
const note = ref('')
const busy = ref('')
const actionError = ref('')
const outcomes = ref([])

const canDecide = computed(() => props.canContribute && !props.archived)
const view = computed(() => viewState({ hasLoaded: loaded.value, error: error.value, count: rows.value.length }))
const relative = (t) => (t ? formatRelativeTime(t) : '')
const absolute = (t) => (t ? formatLocalDateTime(t) : '')

function requester(r) {
  const by = String(r.requested_by || '')
  if (by.startsWith('agent:')) return `${by.slice(6)} (steward)`
  return sameEmail(by, props.myEmail) ? 'you' : by || 'the steward'
}

async function load() {
  try {
    rows.value = await store.fetchRunsWaiting(props.projectId)
    loaded.value = true
    error.value = ''
    // A run decided elsewhere (its ask, another tab) leaves the selection.
    picked.value = picked.value.filter((id) => rows.value.some((r) => r.id === id))
  } catch (err) {
    error.value = projectErrorMessage(err)
  }
}

async function decide(verb) {
  if (busy.value || !picked.value.length) return
  busy.value = verb
  actionError.value = ''
  const ids = [...picked.value]
  const taskOf = Object.fromEntries(rows.value.map((r) => [r.id, r.task_id]))
  try {
    const results = await store.decideRuns(props.projectId, verb, ids, verb === 'decline' ? note.value.trim() : null)
    outcomes.value = results.map((o) => ({ ...o, task: taskOf[o.id] }))
    if (verb === 'decline') note.value = ''
    picked.value = []
    await load()
  } catch (err) {
    actionError.value = projectErrorMessage(err)
  } finally {
    busy.value = ''
  }
}

watch(() => props.projectId, () => {
  rows.value = []
  loaded.value = false
  picked.value = []
  outcomes.value = []
  load()
}, { immediate: true })
</script>
