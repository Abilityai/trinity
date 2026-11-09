<!--
  A project's task list (trinity-enterprise#661 v2.1).

  The ent#673 task fields, grouped the way the steward reads them: open,
  awaiting verification, done. Members add tasks and change them; agents do
  the same through their tools. The rules are the server's — this surface only
  offers what makes sense: a done task offers "Reopen" and nothing else.

  trinity-enterprise#896: the verification rule is stated where it applies —
  an agent's completion claim waits in "Awaiting verification" until a person
  (or the steward agent) confirms it — and each such row carries Confirm for
  people who may (contributors on a live project). Objectives and task-log
  bodies render as markdown through the shared renderer (#3692).

  trinity-enterprise#1133: contributors edit the title, objective and
  definition of done in the same form, with one Save and one dirty path; each
  change leaves a line in the task's log. The read-only objective / DoD blocks
  show only when those fields are not offered (a viewer, an archived project,
  or a done task), so a contributor never reads the same text twice.

  #867: contributors change a task's owner here — blank clears it.
  trinity-enterprise#1155: the owner is picked from the project's own people
  (must-pick, over `GET /projects/{id}/people?purpose=owner`); a `role:<id>`
  owner and the owner already on the task are still taken as typed. Rows and
  the detail name the owner.
-->
<template>
  <div class="space-y-4" data-testid="project-tasks">
    <div class="flex items-center gap-2">
      <h2 class="text-base font-semibold text-gray-900 dark:text-gray-100">Tasks</h2>
      <span class="flex-1"></span>
      <BaseButton v-if="canContribute && !archived" size="sm" data-testid="project-task-new" @click="openCreate">New task</BaseButton>
    </div>
    <!-- trinity-enterprise#1132: the withheld control, said where it would be. -->
    <p v-if="withheld" class="text-xs text-gray-600 dark:text-gray-300" data-testid="project-tasks-withheld">{{ withheld }}</p>

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
        <p v-if="g.key === 'verifying' && g.rows.length" class="text-xs text-gray-600 dark:text-gray-300" data-testid="project-tasks-verification-rule">
          {{ rule }} Open a task to see its evidence{{ canConfirm ? ', then Confirm' : '' }}.
        </p>
        <ul class="divide-y divide-gray-100 dark:divide-gray-750 rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800">
          <li v-for="t in g.rows" :key="t.id" class="flex items-center gap-2 pr-2">
            <button
              type="button"
              class="flex min-w-0 flex-1 items-center gap-3 px-3 py-2.5 text-left hover:bg-gray-50 dark:hover:bg-gray-750 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
              :data-testid="`project-task-${t.id}`"
              @click="openTask(t.id)"
            >
              <span class="font-mono text-xs text-gray-500 dark:text-gray-400 tabular-nums">{{ t.id }}</span>
              <span class="min-w-0 flex-1 truncate text-sm text-gray-900 dark:text-gray-100">{{ t.title }}</span>
              <BaseBadge v-if="t.priority === 'p1'" variant="urgent">P1</BaseBadge>
              <span v-if="t.owner || t.agent" class="hidden sm:inline max-w-[10rem] truncate text-xs text-gray-500 dark:text-gray-400">{{ t.agent || ownerLabel(t.owner) }}</span>
              <BaseBadge :variant="TASK_STATUS_BADGE[t.status] || 'neutral'">{{ taskStatusLabel(t.status) }}</BaseBadge>
            </button>
            <BaseButton
              v-if="g.key === 'verifying' && canConfirm"
              size="sm"
              variant="secondary"
              :loading="confirming === t.id"
              loading-label="Confirming…"
              :data-testid="`project-task-confirm-${t.id}`"
              @click="confirmDone(t)"
            >Confirm</BaseButton>
          </li>
        </ul>
      </section>
      <InlineError v-if="confirmError" :message="confirmError" @dismiss="confirmError = ''" />
    </template>

    <!-- New task -->
    <BaseModal v-model="createOpen" labelledby="task-create-title" :close-on-backdrop="false">
      <form class="space-y-4" @submit.prevent="create">
        <h2 id="task-create-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">New task</h2>
        <BaseInput id="task-title" v-model="form.title" label="Title" :error="formError.title" placeholder="Draft the launch brief" />
        <BaseTextarea id="task-objective" v-model="form.objective" label="Objective (optional)" :rows="2" :error="formError.objective" />
        <BaseTextarea id="task-dod" v-model="form.done_definition" label="Definition of done (optional)" :rows="3" mono placeholder="- [ ] Brief reviewed by two leads" :error="formError.done_definition" />
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <BasePeopleInput
            id="task-owner"
            ref="createOwnerField"
            v-model="form.owner"
            v-model:picked="createPicked"
            mode="pick"
            label="Owner (optional)"
            placeholder="Type a name or email"
            :fetch="ownerLookup"
            :no-match="noOwnerMatch"
            :accept-text="isRoleOwner"
          />
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
          <div><dt class="text-xs text-gray-500 dark:text-gray-400">Owner</dt><dd class="truncate text-gray-900 dark:text-gray-100">{{ task.owner ? ownerLabel(task.owner) : '—' }}</dd></div>
          <div><dt class="text-xs text-gray-500 dark:text-gray-400">Agent</dt><dd class="truncate text-gray-900 dark:text-gray-100">{{ task.agent || '—' }}</dd></div>
          <div><dt class="text-xs text-gray-500 dark:text-gray-400">Priority</dt><dd class="uppercase text-gray-900 dark:text-gray-100">{{ task.priority }}</dd></div>
          <div><dt class="text-xs text-gray-500 dark:text-gray-400">Updated</dt><dd class="text-gray-900 dark:text-gray-100" :title="formatLocalDateTime(task.updated_at)">{{ formatRelativeTime(task.updated_at) }}</dd></div>
        </dl>
        <div v-if="task.objective && showRead">
          <h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400">Objective</h3>
          <div class="mt-1 text-sm text-gray-800 dark:text-gray-200" data-testid="project-task-objective"><PortalMarkdown :content="task.objective" /></div>
        </div>
        <div v-if="task.done_definition && showRead"><h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400">Definition of done</h3><p class="mt-1 whitespace-pre-line font-mono text-xs text-gray-800 dark:text-gray-200">{{ task.done_definition }}</p></div>

        <!-- trinity-enterprise#1193: this task's briefs, for the agent working it. -->
        <ProjectContext
          :project-id="projectId"
          :task-id="task.id"
          :items="(task.context || []).filter((c) => c.kind === 'project-file')"
          :can-contribute="canContribute"
          :archived="archived"
          :my-email="myEmail"
          :is-creator="isCreator"
          @changed="refreshTask"
        />

        <form v-if="canContribute && !archived" class="space-y-2 rounded-lg border border-gray-200 dark:border-gray-750 p-3" @submit.prevent="saveTask">
          <BaseInput
            id="task-title-edit"
            v-model="edit.title"
            label="Title"
            :disabled="locked"
            :error="editError.title"
            :help="locked ? 'Reopen the task to change its title.' : ''"
            data-testid="project-task-title-edit"
          />
          <BaseTextarea
            id="task-objective-edit"
            v-model="edit.objective"
            label="Objective (optional)"
            :rows="4"
            :disabled="locked"
            :error="editError.objective"
            :help="locked ? 'Reopen the task to change its objective.' : ''"
            data-testid="project-task-objective-edit"
          />
          <BaseTextarea
            id="task-dod-edit"
            v-model="edit.done_definition"
            label="Definition of done (optional)"
            :rows="5"
            mono
            :disabled="locked"
            :error="editError.done_definition"
            :help="locked ? 'Reopen the task to change its definition of done.' : ''"
            data-testid="project-task-dod-edit"
          />
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <BaseSelect id="task-status" v-model="edit.status" label="Status">
              <option v-for="s in taskStatusOptions(task)" :key="s" :value="s">{{ s === 'active' && task.status === 'done' ? 'Reopen (active)' : taskStatusLabel(s) }}</option>
            </BaseSelect>
            <BaseSelect id="task-priority-edit" v-model="edit.priority" label="Priority" :disabled="task.status === 'done'">
              <option value="p1">P1</option><option value="p2">P2</option><option value="p3">P3</option>
            </BaseSelect>
          </div>
          <BasePeopleInput
            id="task-owner-edit"
            ref="editOwnerField"
            v-model="edit.owner"
            v-model:picked="editPicked"
            mode="pick"
            label="Owner"
            placeholder="Type a name or email"
            maxlength="320"
            :fetch="ownerLookup"
            :no-match="noOwnerMatch"
            :accept-text="isKeptOwner"
            :disabled="task.status === 'done'"
            :help="task.status === 'done' ? 'Reopen the task to change its owner.' : 'Leave blank for no owner.'"
            data-testid="project-task-owner"
          />
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
              <div class="mt-0.5 text-gray-800 dark:text-gray-200"><PortalMarkdown :content="l.body || ''" /></div>
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
import BasePeopleInput from '@/components/base/BasePeopleInput.vue'
import BaseModal from '@/components/base/BaseModal.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import BaseTextarea from '@/components/base/BaseTextarea.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import PortalMarkdown from '@/components/portal/PortalMarkdown.vue'
import { useProjectsStore } from '@/stores/projects'
import ProjectContext from './ProjectContext.vue'
import { viewState } from '@/utils/loadingState'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import {
  TASK_FIELD_ERROR_CODES, TASK_STATUS_BADGE, authorLabel, groupTasks, projectErrorMessage, taskLogKindLabel,
  taskStatusLabel, taskStatusOptions, validateTaskFields, verificationRule, withheldLine } from './projectsUtils'

const props = defineProps({
  // trinity-enterprise#1132: the page read, for the withheld line and the rule.
  project: { type: Object, default: null },
  projectId: { type: String, required: true },
  canContribute: { type: Boolean, default: false },
  archived: { type: Boolean, default: false },
  myEmail: { type: String, default: '' },
  // The project's members `{email, label}` — names an owner on a row.
  members: { type: Array, default: () => [] },
  // trinity-enterprise#1193: the viewer holds the creator role (may change anyone's brief).
  isCreator: { type: Boolean, default: false },
})

const store = useProjectsStore()
const createOpen = ref(false)
const taskOpen = ref(false)
const task = ref(null)
const busy = ref(false)
const actionError = ref('')
const form = reactive({ title: '', objective: '', done_definition: '', owner: '', priority: 'p2' })
const formError = ref({})
const edit = reactive({ status: '', priority: 'p2', owner: '', note: '', title: '', objective: '', done_definition: '' })
const editError = ref({})
// The three editable text fields, and the key each one sends.
const EDIT_TEXT = ['title', 'objective', 'done_definition']
const createPicked = ref(null)
const editPicked = ref(null)
const createOwnerField = ref(null)
const editOwnerField = ref(null)

const ownerLookup = (q) => store.fetchProjectPeople(props.projectId, q, 'owner')
const noOwnerMatch = (t) => `No one on this project matches “${t}”. Add them as a member first.`
const isRoleOwner = (t) => /^role:\S+$/i.test(t)
const isKeptOwner = (t) => isRoleOwner(t) || (!!task.value?.owner && t === task.value.owner)
const member = (email) => props.members.find((m) => m.email === String(email || '').toLowerCase())
const ownerLabel = (owner) => member(owner)?.label || owner
// The owner a task opens with, as a chosen person when they are on the project.
function ownerPerson(owner) {
  const m = owner && member(owner)
  return m ? { email: m.email, label: m.label || m.email } : null
}

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
const canConfirm = computed(() => props.canContribute && !props.archived)
const withheld = computed(() => withheldLine({ project: props.project, myEmail: props.myEmail, canContribute: props.canContribute, archived: props.archived }, 'tasks'))
const rule = computed(() => verificationRule(props.project))
const confirming = ref('')
const confirmError = ref('')
// A done task is frozen until it is reopened — the server refuses every other
// key with 409 `task_done`, so the form says so rather than letting a save fail.
const locked = computed(() => task.value?.status === 'done')
// The rendered objective / DoD show only when the editable fields do not.
const showRead = computed(() => !(props.canContribute && !props.archived) || locked.value)

/** The text fields this save changes, trimmed, with blank meaning "cleared". */
function changedText() {
  const out = {}
  if (!task.value || locked.value) return out
  for (const f of EDIT_TEXT) {
    const next = edit[f].trim()
    if (next !== (task.value[f] || '')) out[f] = next
  }
  return out
}

const dirty = computed(() => task.value && (
  edit.status !== task.value.status || edit.priority !== task.value.priority || edit.note.trim() !== ''
  || (task.value.status !== 'done' && edit.owner.trim() !== (task.value.owner || ''))
  || Object.keys(changedText()).length > 0))

function load() { return store.fetchTasks(props.projectId) }
watch(() => props.projectId, load, { immediate: true })

function openCreate() {
  Object.assign(form, { title: '', objective: '', done_definition: '', owner: '', priority: 'p2' })
  createPicked.value = null
  formError.value = {}
  actionError.value = ''
  createOpen.value = true
}

async function create() {
  // The same limits as an edit, from the one validator (ent#1133).
  formError.value = validateTaskFields(form)
  const ownerOk = !!createOwnerField.value?.validate()
  if (Object.keys(formError.value).length || !ownerOk) return
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

// ent#896: a person's confirmation of an agent's claim — the one transition
// the rule reserves for people (and the steward agent, through its tools).
async function confirmDone(t) {
  confirming.value = t.id
  confirmError.value = ''
  try {
    await store.updateTask(props.projectId, t.id, { status: 'done' })
  } catch (err) {
    confirmError.value = projectErrorMessage(err)
  } finally {
    confirming.value = ''
  }
}

async function refreshTask() {
  if (!task.value) return
  try {
    task.value = await store.fetchTask(props.projectId, task.value.id)
  } catch (err) {
    actionError.value = projectErrorMessage(err)
  }
}

async function openTask(id) {
  task.value = null
  actionError.value = ''
  taskOpen.value = true
  try {
    task.value = await store.fetchTask(props.projectId, id)
    resetEdit()
  } catch (err) {
    taskOpen.value = false
    actionError.value = projectErrorMessage(err)
  }
}

function resetEdit() {
  editPicked.value = ownerPerson(task.value.owner)
  editError.value = {}
  Object.assign(edit, {
    status: task.value.status, priority: task.value.priority, owner: task.value.owner || '', note: '',
    title: task.value.title || '', objective: task.value.objective || '',
    done_definition: task.value.done_definition || '',
  })
}

async function saveTask() {
  if (editOwnerField.value && !editOwnerField.value.validate()) return
  // Only the fields this save sends are checked: a legacy stored value over
  // the limit must not block a status, owner or note change.
  const text = changedText()
  editError.value = validateTaskFields(edit, Object.keys(text))
  if (Object.keys(editError.value).length) return
  const body = {}
  if (edit.status !== task.value.status) body.status = edit.status
  if (edit.priority !== task.value.priority && task.value.status !== 'done') body.priority = edit.priority
  const owner = edit.owner.trim()
  if (task.value.status !== 'done' && owner !== (task.value.owner || '')) body.owner = owner || null
  if (edit.note.trim()) body.note = edit.note.trim()
  // Blank clears a long field; a title is required, so it is never sent blank.
  for (const [f, v] of Object.entries(text)) body[f] = f === 'title' ? v : (v || null)
  busy.value = true
  actionError.value = ''
  try {
    await store.updateTask(props.projectId, task.value.id, body)
    task.value = await store.fetchTask(props.projectId, task.value.id)
    resetEdit()
  } catch (err) {
    // A refusal that names a field belongs under it; anything else is the
    // form's own error, and what was typed is kept either way.
    const field = TASK_FIELD_ERROR_CODES[err?.response?.data?.detail?.code]
    if (field) editError.value = { [field]: projectErrorMessage(err) }
    else actionError.value = projectErrorMessage(err)
  } finally {
    busy.value = false
  }
}
</script>
