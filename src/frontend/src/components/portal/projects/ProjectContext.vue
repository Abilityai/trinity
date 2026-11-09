<!--
  Context for a project's agents (trinity-enterprise#1193, #1136 step A).

  A brief a person writes or uploads (.md / .txt), on the project or on one
  task, stored by the project — no agent needed. "Agents on this project get
  this" is the context mark; turning it on says who else can read it
  (principle 26). Preview shows exactly what one agent read returns: the first
  200 KB, with the truncation named, never a silent cut.

  Upload reads the file and opens it in the editor, so the mark and its notice
  are offered before anything is saved, exactly as for a written brief.

  Items come from the read the caller already has (the page read's `context`,
  or a task's); a change emits `changed` so the caller re-reads.
-->
<template>
  <section class="space-y-2" data-testid="project-context">
    <div class="flex items-center gap-2">
      <h3 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
        {{ taskId ? 'Context for this task' : 'Context for agents' }} · <span class="tabular-nums">{{ items.length }}</span>
      </h3>
      <span class="flex-1"></span>
      <template v-if="canWrite">
        <BaseButton size="sm" variant="secondary" data-testid="context-write" @click="openWrite()">Write a brief</BaseButton>
        <BaseButton size="sm" variant="ghost" data-testid="context-upload" @click="fileInput?.click()">Upload</BaseButton>
        <input ref="fileInput" type="file" accept=".md,.markdown,.txt,text/markdown,text/plain" class="hidden" data-testid="context-upload-input" @change="upload" />
      </template>
    </div>
    <p v-if="!items.length" class="text-sm text-gray-600 dark:text-gray-300" data-testid="context-empty">
      {{ canWrite
        ? 'No brief yet. Write or upload the background an agent needs — a transcript, your view, links — and mark it for agents.'
        : 'No brief yet.' }}
    </p>
    <InlineError v-if="error" :message="error" @dismiss="error = ''" />
    <ul v-if="items.length" class="divide-y divide-gray-100 dark:divide-gray-750 rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800">
      <li v-for="c in items" :key="c.id" class="flex flex-wrap items-center gap-3 px-3 py-2 text-sm" :data-testid="`context-${c.id}`">
        <button type="button" class="min-w-0 flex-1 text-left" @click="preview(c)">
          <p class="truncate text-gray-900 dark:text-gray-100 hover:underline">{{ c.filename }}</p>
          <p class="text-xs text-gray-500 dark:text-gray-400">{{ humanSize(c.size_bytes) }} · {{ c.created_by }}</p>
        </button>
        <BaseBadge v-if="c.for_agents" variant="info">For agents</BaseBadge>
        <template v-if="canEdit(c)">
          <label class="inline-flex items-center gap-1.5 text-xs text-gray-700 dark:text-gray-300">
            <input type="checkbox" :checked="c.for_agents" :disabled="busy === c.id" :data-testid="`context-mark-${c.id}`" @change="mark(c, $event)" />
            Agents on this project get this
          </label>
          <BaseButton size="sm" variant="ghost" @click="openWrite(c)">Edit</BaseButton>
          <BaseButton size="sm" variant="ghost" :data-testid="`context-delete-${c.id}`" @click="remove(c)">Remove</BaseButton>
        </template>
      </li>
    </ul>
    <p v-if="notice" class="text-xs text-gray-600 dark:text-gray-300" role="status" data-testid="context-notice">{{ notice }}</p>

    <!-- Write / edit -->
    <BaseModal v-model="writeOpen" labelledby="context-write-title" panel-class="relative w-full max-w-3xl rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800 max-h-[90vh] overflow-y-auto">
      <form class="space-y-3" @submit.prevent="save">
        <h2 id="context-write-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">{{ editing ? 'Edit the brief' : 'Write a brief' }}</h2>
        <BaseInput id="context-filename" v-model="form.filename" label="Name" placeholder="kickoff-brief.md" />
        <BaseTextarea id="context-content" v-model="form.content" :rows="14" label="Brief (markdown)"
                      placeholder="The transcript, your view, the background an agent needs." />
        <p class="text-xs text-gray-500 dark:text-gray-400" data-testid="context-size">
          {{ humanSize(bytes) }} of {{ humanSize(fileBytes) }}<template v-if="bytes > readBytes"> · an agent reads it in parts of {{ humanSize(readBytes) }}</template>
        </p>
        <label class="flex items-start gap-2 text-sm text-gray-800 dark:text-gray-200">
          <input v-model="form.for_agents" type="checkbox" class="mt-0.5" data-testid="context-form-mark" />
          <span>Agents on this project get this<span v-if="form.for_agents" class="block text-xs text-gray-600 dark:text-gray-300">{{ CONTEXT_SHARED_NOTICE }}</span></span>
        </label>
        <InlineError v-if="formError" :message="formError" @dismiss="formError = ''" />
        <div class="flex justify-end gap-2">
          <BaseButton type="button" variant="ghost" @click="writeOpen = false">Cancel</BaseButton>
          <BaseButton type="submit" :loading="saving" data-testid="context-save">Save</BaseButton>
        </div>
      </form>
    </BaseModal>

    <!-- Preview: what an agent receives -->
    <BaseModal v-model="previewOpen" labelledby="context-preview-title" panel-class="relative w-full max-w-3xl rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800 max-h-[90vh] overflow-y-auto">
      <div class="space-y-3">
        <h2 id="context-preview-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">{{ shown?.filename }}</h2>
        <!-- An unmarked brief is never described as something an agent reads. -->
        <p v-if="shown && !shown.for_agents" class="text-sm text-gray-600 dark:text-gray-300" data-testid="context-preview-unmarked">Not marked for agents: no agent reads this.</p>
        <p v-else-if="shown?.cut?.truncated" class="rounded-lg border border-status-warning-200 dark:border-status-warning-900/50 bg-status-warning-50 dark:bg-status-warning-900/20 px-3 py-2 text-sm text-gray-800 dark:text-gray-200" data-testid="context-preview-truncated">
          An agent's first read gets the first {{ humanSize(shown.cut.shown) }} of {{ humanSize(shown.cut.total) }}, and is told the rest is there. It reads on in parts of {{ humanSize(readBytes) }}.
        </p>
        <SkeletonLoader v-if="!shown?.cut" :count="4" height="1.5rem" gap="0.5rem" />
        <pre v-else class="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded bg-gray-50 p-3 text-xs text-gray-800 dark:bg-gray-900 dark:text-gray-200" data-testid="context-preview-text">{{ shown.cut.text }}</pre>
        <div class="flex justify-end"><BaseButton variant="secondary" @click="previewOpen = false">Close</BaseButton></div>
      </div>
    </BaseModal>
  </section>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseInput from '@/components/base/BaseInput.vue'
import BaseModal from '@/components/base/BaseModal.vue'
import BaseTextarea from '@/components/base/BaseTextarea.vue'
import InlineError from '@/components/InlineError.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import { useProjectsStore } from '@/stores/projects'
import { humanSize } from '@/components/portal/portalFiles'
import { AGENT_READ_BYTES, CONTEXT_SHARED_NOTICE, agentPreview, projectErrorMessage, sameEmail } from './projectsUtils'

const props = defineProps({
  projectId: { type: String, required: true },
  items: { type: Array, default: () => [] },          // project-file context rows from the read
  taskId: { type: String, default: null },
  canContribute: { type: Boolean, default: false },
  archived: { type: Boolean, default: false },
  myEmail: { type: String, default: '' },
  // The viewer holds the project's creator role (the page read's `my_role`) — the
  // server's rule for changing someone else's brief, not an email comparison.
  isCreator: { type: Boolean, default: false },
  // The server's limits (the page read's `context_limits`); constants only as a fallback.
  limits: { type: Object, default: null },
})
const emit = defineEmits(['changed'])

const store = useProjectsStore()
const fileInput = ref(null)
const error = ref('')
const notice = ref('')
const busy = ref('')
const writeOpen = ref(false)
const editing = ref(null)
const saving = ref(false)
const formError = ref('')
const form = reactive({ filename: '', content: '', for_agents: false })
const previewOpen = ref(false)
const shown = ref(null)

const fileBytes = computed(() => props.limits?.file_bytes || 512 * 1024)
const readBytes = computed(() => props.limits?.agent_read_bytes || AGENT_READ_BYTES)
const canWrite = computed(() => props.canContribute && !props.archived)
// Whoever added a brief, or the project's creator, changes it (the server's rule).
const canEdit = (c) => canWrite.value && (props.isCreator || sameEmail(c.created_by, props.myEmail))
const bytes = computed(() => new TextEncoder().encode(form.content || '').length)

async function run(id, fn) {
  busy.value = id
  error.value = ''
  try {
    const out = await fn()
    notice.value = out?.data?.notice || ''
    emit('changed')
    return out
  } catch (err) {
    error.value = projectErrorMessage(err)
    return null
  } finally {
    busy.value = ''
  }
}

// Each open of the editor or the preview takes a ticket; a read that comes back
// for an older one (the person moved on, or already typed) is dropped, so one
// brief's text never lands under another's name.
let writeTicket = 0
let previewTicket = 0
const loadingContent = ref(false)

function openWrite(c = null) {
  const ticket = ++writeTicket
  editing.value = c
  formError.value = ''
  Object.assign(form, { filename: c?.filename || '', content: '', for_agents: c ? c.for_agents : false })
  writeOpen.value = true
  loadingContent.value = !!c
  if (!c) return
  store.readContext(props.projectId, c.id)
    .then((full) => {
      if (ticket !== writeTicket || form.content) return       // stale, or the person already typed
      form.content = full.content
    })
    .catch((err) => { if (ticket === writeTicket) formError.value = projectErrorMessage(err) })
    .finally(() => { if (ticket === writeTicket) loadingContent.value = false })
}

async function save() {
  formError.value = ''
  if (loadingContent.value) {
    formError.value = 'The brief is still loading.'
    return
  }
  if (!form.filename.trim() || !form.content.trim()) {
    formError.value = 'A brief needs a name and some text.'
    return
  }
  saving.value = true
  try {
    const body = { filename: form.filename.trim(), content: form.content, for_agents: form.for_agents }
    const out = editing.value
      ? await store.updateContext(props.projectId, editing.value.id, body)
      : await store.writeContext(props.projectId, { ...body, task_id: props.taskId || undefined })
    notice.value = out?.data?.notice || ''
    writeOpen.value = false
    emit('changed')
  } catch (err) {
    formError.value = projectErrorMessage(err)
  } finally {
    saving.value = false
  }
}

// An upload opens in the editor: the person sees it, can mark it for agents
// (with the notice), and saves it like a written brief.
async function upload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  error.value = ''
  if (!/\.(md|markdown|txt)$/i.test(file.name)) {
    error.value = projectErrorMessage({ response: { data: { detail: { code: 'unsupported_type' } } } })
    return
  }
  if (file.size > fileBytes.value) {
    error.value = projectErrorMessage({ response: { data: { detail: { code: 'context_too_large' } } } })
    return
  }
  let content
  try {
    content = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer())
  } catch {
    error.value = projectErrorMessage({ response: { data: { detail: { code: 'not_text' } } } })
    return
  }
  editing.value = null
  formError.value = ''
  Object.assign(form, { filename: file.name, content, for_agents: false })
  writeOpen.value = true
}

async function mark(c, event) {
  const on = event.target.checked
  const out = await run(c.id, () => store.updateContext(props.projectId, c.id, { for_agents: on }))
  if (!out) event.target.checked = !on       // refused: the box shows the server's state again
}
const remove = (c) => run(c.id, () => store.deleteContext(props.projectId, c.id))

async function preview(c) {
  const ticket = ++previewTicket
  shown.value = { ...c, cut: null }
  previewOpen.value = true
  try {
    const full = await store.readContext(props.projectId, c.id)
    if (ticket !== previewTicket) return
    shown.value = { ...c, cut: agentPreview(full.content, readBytes.value) }
  } catch (err) {
    if (ticket !== previewTicket) return
    previewOpen.value = false
    error.value = projectErrorMessage(err)
  }
}
</script>
