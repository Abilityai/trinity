<!--
  Start a task's run from the project (trinity-enterprise#1195, ent#1136 step B).

  A member picks the agent (only those ACTIVE on the project and on their own
  list are offered; an active agent they can't use is named, with what to do),
  previews exactly the first message the agent gets — objective, definition of
  done, the context marked for agents (#1193, read by the agent per its read
  budget) and a log excerpt — and Start opens the task's chat with that agent.

  Start is idempotent end to end: the server reopens the same chat for the same
  (task, person, agent), and the first message is sent with the server's
  deterministic Idempotency-Key, so a double click or a retry runs ONE turn
  (Invariant #18). Each preview read takes a ticket, so a slower reply for an
  agent the person has since switched away from never lands (the #1194 rule).
-->
<template>
  <section class="space-y-2 rounded-lg border border-gray-200 dark:border-gray-750 p-3" data-testid="project-task-start">
    <h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400">Run this task</h3>

    <p v-if="state === 'loading'" class="text-sm text-gray-500 dark:text-gray-400">Loading…</p>
    <p v-else-if="state === 'failed'" class="text-sm text-status-danger-700 dark:text-status-danger-400" role="alert">{{ loadError }}</p>

    <template v-else-if="preview">
      <ul v-if="preview.chats.length" class="space-y-1" data-testid="project-task-chats">
        <li v-for="c in preview.chats" :key="c.id" class="flex items-center gap-2 text-sm">
          <span class="min-w-0 flex-1 truncate text-gray-800 dark:text-gray-200">Chat for this task with {{ c.agent_name }}</span>
          <BaseButton size="sm" variant="secondary" :data-testid="`project-task-chat-${c.id}`" @click="open(c.id, c.agent_name)">Open</BaseButton>
        </li>
      </ul>

      <p v-if="archived" class="text-sm text-gray-600 dark:text-gray-300">The project is archived, so no new run can start.</p>
      <p v-else-if="preview.done" class="text-sm text-gray-600 dark:text-gray-300" data-testid="project-task-start-done">This task is done. Reopen it before starting a new run.</p>
      <template v-else-if="canContribute && !preview.done">
        <div v-if="!available.length" class="text-sm text-gray-700 dark:text-gray-300" data-testid="project-task-start-none">
          <template v-if="preview.agents.not_on_your_list.length">
            You can't use {{ preview.agents.not_on_your_list.join(', ') }} yet. Ask its owner for access on the Steward card or under Work on it.
          </template>
          <template v-else>No agent is working on this project yet. Add one in Members &amp; access first.</template>
        </div>
        <template v-else>
          <BaseSelect id="task-start-agent" :model-value="agent" label="Agent" data-testid="project-task-start-agent" @update:model-value="pick">
            <option v-for="a in available" :key="a" :value="a">{{ a }}</option>
          </BaseSelect>
          <div class="flex flex-wrap gap-2">
            <BaseButton size="sm" variant="ghost" data-testid="project-task-start-preview" @click="shown = !shown">
              {{ shown ? 'Hide what it gets' : 'Preview what it gets' }}
            </BaseButton>
            <BaseButton size="sm" :loading="starting" loading-label="Starting…" :disabled="!preview.message" data-testid="project-task-start-go" @click="start">Start</BaseButton>
          </div>
          <div v-if="shown && preview.message" class="space-y-1" data-testid="project-task-start-message">
            <pre class="max-h-72 overflow-y-auto whitespace-pre-wrap rounded bg-gray-50 p-2 font-mono text-xs text-gray-800 dark:bg-gray-900 dark:text-gray-200">{{ preview.message }}</pre>
            <p v-if="preview.context.length" class="text-xs text-gray-500 dark:text-gray-400">
              The agent reads each item itself, up to {{ Math.round(preview.read_budget_bytes / 1024) }} KB per read.
            </p>
          </div>
          <p class="text-xs text-gray-500 dark:text-gray-400">{{ preview.cost_note }}</p>
        </template>
      </template>
      <p v-if="reloadError" class="text-xs text-status-danger-700 dark:text-status-danger-400" role="alert">{{ reloadError }}</p>
      <p v-if="startError" class="text-xs text-status-danger-700 dark:text-status-danger-400" role="alert">{{ startError }}</p>
    </template>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import { useProjectsStore } from '@/stores/projects'
import { useClientPortalStore } from '@/stores/clientPortal'
import { projectErrorMessage } from './projectsUtils'

const props = defineProps({
  projectId: { type: String, required: true },
  taskId: { type: String, required: true },
  canContribute: { type: Boolean, default: false },
  archived: { type: Boolean, default: false },
})
const emit = defineEmits(['open-thread'])

const preview = ref(null)
const state = ref('loading')
const loadError = ref('')
const startError = ref('')
const reloadError = ref('')
const agent = ref(null)
const shown = ref(false)
const starting = ref(false)
let ticket = 0

const available = computed(() => preview.value?.agents?.available || [])

async function load(agentName = null) {
  const mine = ++ticket
  if (!preview.value) state.value = 'loading'
  reloadError.value = ''
  try {
    const data = await useProjectsStore().startPreview(props.projectId, props.taskId, agentName)
    if (mine !== ticket) return                       // a newer read was asked for
    preview.value = data
    agent.value = data.agent
    state.value = 'ready'
  } catch (err) {
    if (mine !== ticket) return
    if (preview.value) {
      // A reload failed (say the picked agent's access was revoked): keep what
      // is on screen — the other agents and the task's chats stay reachable.
      reloadError.value = projectErrorMessage(err)
      agent.value = preview.value.agent
      return
    }
    loadError.value = projectErrorMessage(err)
    state.value = 'failed'
  }
}

function pick(name) {
  agent.value = name
  load(name)
}

function open(id, agentName) {
  emit('open-thread', { id, agent_name: agentName })
}

async function start() {
  if (starting.value || !agent.value) return          // a double click is one start
  starting.value = true
  startError.value = ''
  const name = agent.value
  try {
    const res = await useProjectsStore().startTask(props.projectId, props.taskId, name, preview.value?.message_sha)
    if (res.needs_first_message) {
      try {
        await useClientPortalStore().startPortalChat(name, res.message, res.thread_id,
          { idempotencyKey: res.idempotency_key })
      } catch (err) {
        // 409 request_in_progress: the same first message is already on its way
        // (another tab, a retry) — open the chat rather than call it a failure.
        if (err?.response?.status !== 409) throw err
      }
    }
    open(res.thread_id, name)
  } catch (err) {
    if ((err?.response?.data?.detail || {}).code === 'preview_changed') load(name)   // show the new text
    startError.value = projectErrorMessage(err)
  } finally {
    starting.value = false
  }
}

watch(() => [props.projectId, props.taskId], () => {
  preview.value = null
  shown.value = false
  load()
}, { immediate: true })
</script>
