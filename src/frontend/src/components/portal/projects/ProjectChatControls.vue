<!--
  A chat's project controls in the conversation header (trinity-enterprise#661).

  Linked chat: a badge naming the project (opens it) and Detach.
  Unlinked chat: ONE header button, "Project", which opens a small dialog —
  add this chat to a project the agent is on, or make a new project from it.
  One button rather than two keeps the header's single primary action and
  fits a phone (principle 11).

  Never rendered in Main, for an outside client, before a chat exists, or on a
  build without projects (`chatProjectActions`). Which project a chat is in
  comes from the server's link row for THIS chat, never from anything the
  client asserts.
-->
<template>
  <div v-if="actions.show" class="flex items-center gap-1" data-testid="project-chat-controls">
    <template v-if="actions.linked">
      <button
        type="button"
        class="inline-flex max-w-[16rem] items-center gap-1.5 rounded-full bg-action-primary-50 px-2.5 py-1 text-xs font-medium text-action-primary-700 hover:bg-action-primary-100 dark:bg-action-primary-500/16 dark:text-action-primary-300 dark:hover:bg-action-primary-500/25"
        data-testid="project-chat-badge"
        @click="$emit('open-project', project.id)"
      >
        <svg class="h-3.5 w-3.5 flex-none" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" :d="FOLDER" /></svg>
        <span class="truncate">{{ project.name }}</span>
      </button>
      <BaseButton size="sm" variant="ghost" :loading="busy" data-testid="project-chat-detach" @click="detach">Detach</BaseButton>
    </template>
    <BaseButton v-else size="sm" variant="ghost" data-testid="project-chat-open" aria-label="Add this chat to a project" @click="dialogOpen = true">
      <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" :d="FOLDER" /></svg>
      <span class="hidden sm:inline">Project</span>
    </BaseButton>
    <InlineError v-if="error && !dialogOpen" class="max-w-xs" :message="error" @dismiss="error = ''" />

    <BaseModal v-model="dialogOpen" labelledby="project-chat-title">
      <div class="space-y-4">
        <h2 id="project-chat-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">Add this chat to a project</h2>
        <form v-if="candidates.length" class="flex items-start gap-2" @submit.prevent="addToProject">
          <BaseSelect id="project-chat-pick" v-model="picked" class="flex-1" aria-label="Project">
            <option value="" disabled>Choose a project with {{ agentName }}…</option>
            <option v-for="p in candidates" :key="p.id" :value="p.id">{{ p.name }}</option>
          </BaseSelect>
          <BaseButton type="submit" :disabled="!picked" :loading="busy">Add</BaseButton>
        </form>
        <p v-else class="text-sm text-gray-600 dark:text-gray-300">
          {{ agentName }} isn't on any project you can add to yet.
        </p>
        <InlineError v-if="error" :message="error" @dismiss="error = ''" />
        <div class="border-t border-gray-200 dark:border-gray-750 pt-4">
          <BaseButton variant="secondary" data-testid="project-chat-make" @click="makeProject">Make this chat a new project</BaseButton>
        </div>
        <div class="flex justify-end">
          <BaseButton variant="ghost" @click="dialogOpen = false">Cancel</BaseButton>
        </div>
      </div>
    </BaseModal>

    <ProjectCreateModal
      v-model="createOpen"
      :link-thread="{ id: sessionId, title: sessionTitle, agent: agentName }"
      :agents="rosterAgents"
      @created="onCreated"
    />
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseModal from '@/components/base/BaseModal.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import InlineError from '@/components/InlineError.vue'
import ProjectCreateModal from './ProjectCreateModal.vue'
import { useProjectsStore } from '@/stores/projects'
import { useClientPortalStore } from '@/stores/clientPortal'
import { chatProjectActions, projectErrorMessage } from './projectsUtils'

const FOLDER = 'M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z'

const props = defineProps({
  agentName: { type: String, required: true },
  sessionId: { type: String, default: null },
  sessionTitle: { type: String, default: '' },
  isMain: { type: Boolean, default: false },
})
const emit = defineEmits(['open-project'])

const store = useProjectsStore()
const portal = useClientPortalStore()
const dialogOpen = ref(false)
const createOpen = ref(false)
const picked = ref('')
const busy = ref(false)
const error = ref('')

const project = computed(() => (props.sessionId ? store.forLink('thread', props.sessionId) : null) || null)
const actions = computed(() => chatProjectActions({
  projectsAvailable: portal.projectsAvailable,
  isPlatform: portal.isPlatformSession,
  isMain: props.isMain,
  sessionId: props.sessionId,
  project: project.value,
}))
const rosterAgents = computed(() => portal.agents.map((a) => a.name))
const candidates = computed(() => store.list.filter((p) => !p.archived_at
  && (p.agents || []).some((a) => a.agent_name === props.agentName && ['active', 'pending'].includes(a.state))))

watch(() => [props.sessionId, portal.projectsAvailable], () => {
  if (!props.sessionId || !portal.projectsAvailable || !portal.isPlatformSession || props.isMain) return
  if (store.forLink('thread', props.sessionId) === undefined) void store.fetchForLink('thread', props.sessionId)
}, { immediate: true })

watch(dialogOpen, (open) => {
  if (!open) return
  picked.value = ''
  error.value = ''
  if (!store.listLoaded) void store.fetchList()
})

async function run(fn) {
  busy.value = true
  error.value = ''
  try { await fn() } catch (err) { error.value = projectErrorMessage(err) } finally { busy.value = false }
}

const detach = () => run(() => store.unlink(project.value.id, 'thread', props.sessionId))

const addToProject = () => run(async () => {
  await store.link(picked.value, 'thread', props.sessionId)
  await store.fetchForLink('thread', props.sessionId)
  dialogOpen.value = false
})

function makeProject() {
  dialogOpen.value = false
  createOpen.value = true
}

async function onCreated() {
  await store.fetchForLink('thread', props.sessionId)
}
</script>
