<!--
  A project's task list (trinity-enterprise#661 v2.1).

  The ent#673 task fields, grouped the way the steward reads them: open,
  awaiting verification, done. Members add tasks and change them; agents do
  the same through their tools. The rules are the server's — this surface only
  offers what makes sense: a done task offers "Reopen" and nothing else.
-->
<template>
  <div class="space-y-4" data-testid="project-tasks">
    <div class="flex items-center gap-2">
      <h2 class="text-base font-semibold text-gray-900 dark:text-gray-100">Tasks</h2>
      <span class="flex-1"></span>
      <BaseButton v-if="canContribute && !archived" size="sm" data-testid="project-task-new" @click="openCreate">New task</BaseButton>
    </div>

    <SkeletonLoader v-if="view.state === 'loading'" :count="4" height="3rem" gap="0.5rem" />
    <LoadFailed
      v-else-if="view.state === 'failed'"
      title="Couldn't load the tasks"
      :message="state.error"
      dense
      @retry="load"
    />
    <template v-else>
      <InlineError v-if="view.stale" :message="state.error" @dismiss="load" />
      <p v-if="view.state === 'empty'" class="rounded-lg border border-dashed border-gray-300 dark:border-gray-700 p-6 text-center text-sm text-gray-600 dark:text-gray-300" data-testid="project-tasks-empty">
        No tasks yet. Add the first one, or ask an agent on the project to break the goal into tasks.
      </p>
      <section v-for="g in groups" v-show="g.rows.length" :key="g.key" class="space-y-1.5">
        <h3 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
          {{ g.label }} <span class="tabular-nums">· {{ g.rows.length }}</span>
        </h3>
        <ul class="divide-y divide-gray-100 dark:divide-gray-750 rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800">
          <li v-for="t in g.rows" :key="t.id">
            <button
              type="button"
              class="flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-gray-50 dark:hover:bg-gray-750 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
              :data-testid="`project-task-${t.id}`"
              @click="openTask(t.id)"
            >
              <span class="font-mono text-xs text-gray-500 dark:text-gray-400 tabular-nums">{{ t.id }}</span>
              <span class="min-w-0 flex-1 truncate text-sm text-gray-900 dark:text-gray-100">{{ t.title }}</span>
              <BaseBadge v-if="t.priority === 'p1'" variant="urgent">P1</BaseBadge>
              <span v-if="t.owner || t.agent" class="hidden sm:inline max-w-[10rem] truncate text-xs text-gray-500 dark:text-gray-400">{{ t.agent || t.owner }}</span>
              <BaseBadge :variant="TASK_STATUS_BADGE[t.status] || 'neutral'">{{ taskStatusLabel(t.status) }}</BaseBadge>
            </button>
          </li>
        </ul>
      </section>
    </template>

    <!-- New task -->
    <BaseModal v-model="createOpen" labelledby="task-create-title" :close-on-backdrop="false">
      <form class="space-y-4" @submit.prevent="create">
        <h2 id="task-create-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">New task</h2>
        <BaseInput id="task-title" v-model="form.title" label="Title" :error="formError.title" placeholder="Draft the launch brief" />
        <BaseTextarea id="task-objective" v-model="form.objective" label="Objective (optional)" :rows="2" />
        <BaseTextarea id="task-dod" v-model="form.done_definition" label="Definition of done (optional)" :rows="3" mono placeholder="- [ ] Brief reviewed by two leads" />
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <BaseInput id="task-owner" v-model="form.owner" label="Owner (optional)" placeholder="name@your-company.com" />
          <BaseSelect id="task-priority" v-model="form.priority" label="Priority">
            <option value="p1">P1 — high</option>
            <option value="p2">P2 — normal</option>
            <option value="p3">P3 — low</option>
          </BaseSelect>
        </div>
        <InlineError v-if="actionError" :message="actionError" @dismiss="actionError = ''" />
        <div class="flex justify-end gap-2">
          <BaseButton variant="secondary" @click="createOpen = false">Cancel</BaseButton>
          <BaseButton type="submit" :loading="busy" loading-label="Adding…" data-testid="project-task-create">Add task</BaseButton>
        </div>
      </form>
    </BaseModal>

    <!-- One task -->
    <BaseModal
      v-model="taskOpen"
      labelledby="task-detail-title"
      panel-class="relative w-full max-w-2xl rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800 max-h-[90vh] overflow-y-auto"
    >
      <div v-if="task" class="space-y-4" data-testid="project-task-detail">
        <div class="flex flex-wrap items-center gap-2">
          <span class="font-mono text-sm text-gray-500 dark:text-gray-400">{{ task.id }}</span>
          <h2 id="task-detail-title" class="min-w-0 flex-1 text-lg font-semibold text-gray-900 dark:text-gray-100 break-words">{{ task.title }}</h2>
          <BaseBadge :variant="TASK_STATUS_BADGE[task.status] || 'neutral'">{{ taskStatusLabel(task.status) }}</BaseBadge>
        </div>
        <dl class="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
          <div><dt class="text-xs text-gray-500 dark:text-gray-400">Owner</dt><dd class="truncate text-gray-900 dark:text-gray-100">{{ task.owner || '—' }}</dd></div>
          <div><dt class="text-xs text-gray-500 dark:text-gray-400">Agent</dt><dd class="truncate text-gray-900 dark:text-gray-100">{{ task.agent || '—' }}</dd></div>
          <div><dt class="text-xs text-gray-500 dark:text-gray-400">Priority</dt><dd class="uppercase text-gray-900 dark:text-gray-100">{{ task.priority }}</dd></div>
          <div><dt class="text-xs text-gray-500 dark:text-gray-400">Updated</dt><dd class="text-gray-900 dark:text-gray-100" :title="formatLocalDateTime(task.updated_at)">{{ formatRelativeTime(task.updated_at) }}</dd></div>
        </dl>
        <div v-if="task.objective"><h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400">Objective</h3><p class="mt-1 whitespace-pre-line text-sm text-gray-800 dark:text-gray-200">{{ task.objective }}</p></div>
        <div v-if="task.done_definition"><h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400">Definition of done</h3><p class="mt-1 whitespace-pre-line font-mono text-xs text-gray-800 dark:text-gray-200">{{ task.done_definition }}</p></div>

        <form v-if="canContribute && !archived" class="space-y-2 rounded-lg border border-gray-200 dark:border-gray-750 p-3" @submit.prevent="saveTask">
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <BaseSelect id="task-status" v-model="edit.status" label="Status">
              <option v-for="s in taskStatusOptions(task)" :key="s" :value="s">{{ s === 'active' && task.status === 'done' ? 'Reopen (active)' : taskStatusLabel(s) }}</option>
            </BaseSelect>
            <BaseSelect id="task-priority-edit" v-model="edit.priority" label="Priority" :disabled="task.status === 'done'">
              <option value="p1">P1</option><option value="p2">P2</option><option value="p3">P3</option>
            </BaseSelect>
          </div>
          <BaseTextarea id="task-note" v-model="edit.note" label="Note (optional — added to the task's log)" :rows="2" />
          <InlineError v-if="actionError" :message="actionError" @dismiss="actionError = ''" />
          <div class="flex justify-end">
            <BaseButton type="submit" size="sm" :loading="busy" loading-label="Saving…" :disabled="!dirty" data-testid="project-task-save">Save</BaseButton>
          </div>
        </form>

        <div>
          <h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400">Log</h3>
          <p v-if="!task.log.length" class="mt-1 text-sm text-gray-500 dark:text-gray-400">Nothing logged yet.</p>
          <ol class="mt-2 max-h-72 space-y-2 overflow-y-auto">
            <li v-for="l in task.log" :key="l.id" class="rounded-md bg-gray-50 dark:bg-gray-900 px-3 py-2 text-sm">
              <p class="text-xs text-gray-500 dark:text-gray-400">
                <span class="font-medium text-gray-700 dark:text-gray-300">{{ taskLogKindLabel(l.kind) }}</span> ·
                {{ authorLabel(l.author, myEmail) }} · <span :title="formatLocalDateTime(l.created_at)">{{ formatRelativeTime(l.created_at) }}</span>
              </p>
              <p class="mt-0.5 whitespace-pre-line text-gray-800 dark:text-gray-200">{{ l.body }}</p>
            </li>
          </ol>
        </div>
      </div>
      <SkeletonLoader v-else :count="3" height="2.5rem" gap="0.5rem" />
    </BaseModal>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseInput from '@/components/base/BaseInput.vue'
import BaseModal from '@/components/base/BaseModal.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import BaseTextarea from '@/components/base/BaseTextarea.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import { useProjectsStore } from '@/stores/projects'
import { viewState } from '@/utils/loadingState'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import {
  TASK_STATUS_BADGE, authorLabel, groupTasks, projectErrorMessage, taskLogKindLabel, taskStatusLabel, taskStatusOptions,
} from './projectsUtils'

const props = defineProps({
  projectId: { type: String, required: true },
  canContribute: { type: Boolean, default: false },
  archived: { type: Boolean, default: false },
  myEmail: { type: String, default: '' },
})

const store = useProjectsStore()
const createOpen = ref(false)
const taskOpen = ref(false)
const task = ref(null)
const busy = ref(false)
const actionError = ref('')
const form = reactive({ title: '', objective: '', done_definition: '', owner: '', priority: 'p2' })
const formError = ref({})
const edit = reactive({ status: '', priority: 'p2', note: '' })

const state = computed(() => store.tasks[props.projectId] || { rows: [], loaded: false, error: null })
const view = computed(() => viewState({ hasLoaded: state.value.loaded, error: state.value.error, count: state.value.rows.length }))
const groups = computed(() => {
  const g = groupTasks(state.value.rows)
  return [
    { key: 'open', label: 'Open', rows: g.open },
    { key: 'verifying', label: 'Awaiting verification', rows: g.verifying },
    { key: 'done', label: 'Done', rows: g.done },
  ]
})
const dirty = computed(() => task.value && (
  edit.status !== task.value.status || edit.priority !== task.value.priority || edit.note.trim() !== ''))

function load() { return store.fetchTasks(props.projectId) }
watch(() => props.projectId, load, { immediate: true })

function openCreate() {
  Object.assign(form, { title: '', objective: '', done_definition: '', owner: '', priority: 'p2' })
  formError.value = {}
  actionError.value = ''
  createOpen.value = true
}

async function create() {
  formError.value = form.title.trim() ? {} : { title: 'Give the task a title, e.g. "Draft the launch brief".' }
  if (formError.value.title) return
  busy.value = true
  actionError.value = ''
  try {
    const body = { title: form.title.trim(), priority: form.priority }
    for (const f of ['objective', 'done_definition', 'owner']) if (form[f].trim()) body[f] = form[f].trim()
    await store.createTask(props.projectId, body)
    createOpen.value = false
  } catch (err) {
    actionError.value = projectErrorMessage(err)
  } finally {
    busy.value = false
  }
}

async function openTask(id) {
  task.value = null
  actionError.value = ''
  taskOpen.value = true
  try {
    task.value = await store.fetchTask(props.projectId, id)
    Object.assign(edit, { status: task.value.status, priority: task.value.priority, note: '' })
  } catch (err) {
    taskOpen.value = false
    actionError.value = projectErrorMessage(err)
  }
}

async function saveTask() {
  const body = {}
  if (edit.status !== task.value.status) body.status = edit.status
  if (edit.priority !== task.value.priority && task.value.status !== 'done') body.priority = edit.priority
  if (edit.note.trim()) body.note = edit.note.trim()
  busy.value = true
  actionError.value = ''
  try {
    await store.updateTask(props.projectId, task.value.id, body)
    task.value = await store.fetchTask(props.projectId, task.value.id)
    Object.assign(edit, { status: task.value.status, priority: task.value.priority, note: '' })
  } catch (err) {
    actionError.value = projectErrorMessage(err)
  } finally {
    busy.value = false
  }
}
</script>
