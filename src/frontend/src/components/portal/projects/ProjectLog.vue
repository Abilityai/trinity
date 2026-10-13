<!--
  A project's log (trinity-enterprise#661 v2.2): the decisions, deliverables,
  task changes, blockers and hand-offs people and agents recorded — the shared
  context every agent on the project reads first. Append-only: nothing here
  edits or deletes. Newest at the bottom, like a conversation, in a bounded
  scroll area with the count stated (principle 28).
-->
<template>
  <div class="space-y-4" data-testid="project-log">
    <h2 class="text-base font-semibold text-gray-900 dark:text-gray-100">Log</h2>

    <SkeletonLoader v-if="view.state === 'loading'" :count="4" height="3.5rem" gap="0.5rem" />
    <LoadFailed v-else-if="view.state === 'failed'" title="Couldn't load the log" :message="state.error" dense @retry="load" />
    <template v-else>
      <InlineError v-if="view.stale" :message="state.error" @dismiss="load" />
      <p v-if="view.state === 'empty'" class="rounded-lg border border-dashed border-gray-300 dark:border-gray-700 p-6 text-center text-sm text-gray-600 dark:text-gray-300" data-testid="project-log-empty">
        Nothing recorded yet. Agents on the project add an entry for each decision, deliverable or blocker; use Wrap up at the end of a chat to have one record what it did.
      </p>
      <ol v-else class="max-h-[32rem] space-y-2 overflow-y-auto pr-1">
        <li v-for="e in state.rows" :key="e.id" class="rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 px-3 py-2.5">
          <div class="flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <BaseBadge :variant="LOG_KIND_BADGE[e.kind] || 'neutral'">{{ logKindLabel(e.kind) }}</BaseBadge>
            <span v-if="e.task_id" class="font-mono">{{ e.task_id }}</span>
            <span>{{ authorLabel(e.author, myEmail) }}</span>
            <span :title="formatLocalDateTime(e.created_at)">{{ formatRelativeTime(e.created_at) }}</span>
          </div>
          <p class="mt-1 whitespace-pre-line text-sm text-gray-800 dark:text-gray-200">{{ e.body }}</p>
        </li>
      </ol>
      <p v-if="state.rows.length" class="text-xs text-gray-500 dark:text-gray-400">
        <span class="tabular-nums">{{ state.rows.length }}</span> {{ state.rows.length === 1 ? 'entry' : 'entries' }}{{ state.rows.length >= 200 ? ' · latest 200 shown' : '' }}
      </p>
    </template>

    <form v-if="canContribute && !archived" class="space-y-2 rounded-lg border border-gray-200 dark:border-gray-750 p-3" data-testid="project-log-form" @submit.prevent="add">
      <div class="flex flex-wrap items-end gap-2">
        <BaseSelect id="log-kind" v-model="kind" label="Add an entry" class="w-44">
          <option v-for="k in LOG_KINDS.filter((x) => x !== 'task')" :key="k" :value="k">{{ logKindLabel(k) }}</option>
        </BaseSelect>
      </div>
      <BaseTextarea id="log-body" v-model="body" :rows="2" placeholder="What was decided, delivered or blocked — and what it means." aria-label="Entry text" />
      <InlineError v-if="error" :message="error" @dismiss="error = ''" />
      <div class="flex justify-end">
        <BaseButton type="submit" size="sm" :loading="busy" loading-label="Adding…" :disabled="!body.trim()" data-testid="project-log-add">Add to log</BaseButton>
      </div>
    </form>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import BaseTextarea from '@/components/base/BaseTextarea.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import { useProjectsStore } from '@/stores/projects'
import { viewState } from '@/utils/loadingState'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import { LOG_KINDS, LOG_KIND_BADGE, authorLabel, logKindLabel, projectErrorMessage } from './projectsUtils'

const props = defineProps({
  projectId: { type: String, required: true },
  canContribute: { type: Boolean, default: false },
  archived: { type: Boolean, default: false },
  myEmail: { type: String, default: '' },
})

const store = useProjectsStore()
const kind = ref('decision')
const body = ref('')
const busy = ref(false)
const error = ref('')

const state = computed(() => store.logs[props.projectId] || { rows: [], loaded: false, error: null })
const view = computed(() => viewState({ hasLoaded: state.value.loaded, error: state.value.error, count: state.value.rows.length }))

function load() { return store.fetchLog(props.projectId) }
watch(() => props.projectId, load, { immediate: true })

async function add() {
  busy.value = true
  error.value = ''
  try {
    await store.addLogEntry(props.projectId, { kind: kind.value, body: body.value.trim() })
    body.value = ''
  } catch (err) {
    error.value = projectErrorMessage(err)
  } finally {
    busy.value = false
  }
}
</script>
