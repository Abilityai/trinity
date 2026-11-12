<!--
  A task's GitHub issues and PRs (trinity-enterprise#1240).

  Read-only links: Trinity reads each item's title and state and never writes
  to GitHub. A merged PR or a closed issue is evidence for a done claim — it
  never finishes the task. A ref whose latest read failed says "Couldn't read"
  in the warning tone with the time of the last good read, so a failed read
  never looks like finished work. The title is text written by whoever wrote
  the issue, so it renders as text, never as HTML; the link is an anchor only
  to an https:// url.
-->
<template>
  <div data-testid="task-refs">
    <h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400">GitHub issues and PRs</h3>
    <p v-if="!refs.length" class="mt-1 text-sm text-gray-500 dark:text-gray-400" data-testid="task-refs-empty">
      Link the GitHub issue or PR this work happens in. Trinity only reads its title and state; a merged PR or a closed issue is evidence for the done claim, not a done.
    </p>
    <ul v-else class="mt-1 divide-y divide-gray-100 dark:divide-gray-750 rounded-md border border-gray-200 dark:border-gray-750">
      <li v-for="r in refs" :key="r.id" class="flex items-center gap-2 px-3 py-2 text-sm" :data-testid="`task-ref-${r.id}`">
        <a
          v-if="refHref(r)"
          :href="refHref(r)"
          target="_blank"
          rel="noopener noreferrer"
          class="shrink-0 font-mono text-xs text-action-primary-600 hover:underline dark:text-action-primary-400"
          :data-testid="`task-ref-link-${r.id}`"
        >{{ refLabel(r) }}</a>
        <span v-else class="shrink-0 font-mono text-xs text-gray-700 dark:text-gray-300">{{ refLabel(r) }}</span>
        <span class="min-w-0 flex-1 truncate text-gray-800 dark:text-gray-200" :title="r.title || ''">{{ r.title || '' }}</span>
        <BaseBadge :variant="refBadge(r).variant">{{ refBadge(r).label }}</BaseBadge>
        <span v-if="r.read_at" class="hidden sm:inline shrink-0 text-xs text-gray-500 dark:text-gray-400" :title="formatLocalDateTime(r.read_at)">
          {{ r.readable === false ? 'last read' : 'read' }} {{ formatRelativeTime(r.read_at) }}
        </span>
        <button
          v-if="canEdit"
          type="button"
          class="shrink-0 rounded px-1 text-xs text-gray-500 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-gray-400 dark:hover:text-gray-100 disabled:opacity-50"
          :aria-label="`Remove ${refLabel(r)}`"
          :disabled="removing === r.id"
          :data-testid="`task-ref-remove-${r.id}`"
          @click="remove(r)"
        >Remove</button>
      </li>
    </ul>

    <div v-if="verifying && evidence.length" class="mt-2 rounded-md bg-gray-50 dark:bg-gray-900 px-3 py-2" data-testid="task-ref-evidence">
      <p class="text-xs font-medium text-gray-700 dark:text-gray-300">Evidence for the done claim</p>
      <ul class="mt-1 flex flex-wrap gap-x-3 gap-y-1">
        <li v-for="r in evidence" :key="r.id" class="flex items-center gap-1.5 text-xs">
          <span class="font-mono text-gray-700 dark:text-gray-300">{{ refLabel(r) }}</span>
          <BaseBadge :variant="refBadge(r).variant">{{ refBadge(r).label }}</BaseBadge>
        </li>
      </ul>
    </div>

    <form v-if="canEdit" class="mt-2 flex items-start gap-2" @submit.prevent="add">
      <div class="min-w-0 flex-1">
        <BaseInput
          id="task-ref-input"
          v-model="text"
          :maxlength="REF_TEXT_MAX"
          placeholder="owner/repo#123 or a GitHub issue or PR link"
          aria-label="GitHub issue or PR"
          :error="error"
        />
      </div>
      <BaseButton type="submit" size="sm" variant="secondary" :loading="adding" loading-label="Adding…" :disabled="!text.trim()" data-testid="task-ref-add">Add</BaseButton>
    </form>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseInput from '@/components/base/BaseInput.vue'
import { useProjectsStore } from '@/stores/projects'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import { REF_TEXT_MAX, projectErrorMessage, refBadge, refHref, refLabel } from './projectsUtils'

const props = defineProps({
  projectId: { type: String, required: true },
  taskId: { type: String, required: true },
  refs: { type: Array, default: () => [] },
  evidence: { type: Array, default: () => [] },
  // A contributor on a live project, on a task that is not done.
  canEdit: { type: Boolean, default: false },
  // The task is awaiting verification: its evidence shows beside the claim.
  verifying: { type: Boolean, default: false },
})
const emit = defineEmits(['changed'])
const store = useProjectsStore()
const text = ref('')
const error = ref('')
const adding = ref(false)
const removing = ref('')

async function add() {
  const value = text.value.trim()
  if (!value) return
  adding.value = true
  error.value = ''
  try {
    const data = await store.addTaskRefs(props.projectId, props.taskId, [value])
    text.value = ''
    emit('changed', data?.refs || [])
  } catch (err) {
    // What was typed is kept, so the fix is one edit away.
    error.value = projectErrorMessage(err)
  } finally {
    adding.value = false
  }
}

async function remove(r) {
  removing.value = r.id
  error.value = ''
  try {
    await store.removeTaskRef(props.projectId, props.taskId, r.id)
    emit('changed', props.refs.filter((x) => x.id !== r.id))
  } catch (err) {
    error.value = projectErrorMessage(err)
  } finally {
    removing.value = ''
  }
}
</script>
