<!--
  Import a project (trinity-enterprise#661 v2.5, trinity-enterprise#1163).

  Two sources behind one switch, each a one-time copy the dialog states before
  anything happens (principle 26):
  - From an agent: the folder projects an agent holds — its own
    (`project_files/`) and the shared ones in its canon clone — the charter,
    tasks (ids kept), the log and the decisions. A folder already imported is
    shown and not offered.
  - From a GitHub epic: the epic becomes the project and each sub-issue a task
    that keeps a link to its issue. Nothing is written back to GitHub. What
    was left out (sub-issues Trinity can't read, more than 100, unlinked) is
    named before the project opens.
-->
<template>
  <BaseModal :model-value="modelValue" labelledby="project-import-title" @update:model-value="$emit('update:modelValue', $event)">
    <div class="space-y-4" data-testid="project-import">
      <h2 id="project-import-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">Import a project</h2>
      <div role="radiogroup" aria-label="Import from" class="flex gap-1" data-testid="project-import-source" @keydown="onSourceKey">
        <button
          v-for="(s, index) in SOURCES"
          :key="s.id"
          :ref="(el) => (sourceButtons[index] = el)"
          type="button"
          role="radio"
          :aria-checked="source === s.id ? 'true' : 'false'"
          :tabindex="source === s.id ? 0 : -1"
          :data-source="s.id"
          class="flex-1 min-w-0 rounded-md px-2 py-1.5 text-[12.5px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40"
          :class="source === s.id
            ? 'bg-action-primary-100 text-action-primary-700 dark:bg-action-primary-500/16 dark:text-action-primary-300'
            : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-750'"
          @click="pick(s.id)"
        >{{ s.label }}</button>
      </div>

      <template v-if="source === 'agent'">
        <p class="text-sm text-gray-600 dark:text-gray-300">
          Brings in a project the agent keeps in its files: the charter, its tasks, the log and the decisions.
          It's a one-time copy — afterwards the project lives here, and the folder isn't kept in sync.
        </p>
        <BaseSelect id="project-import-agent" v-model="agent" label="Agent">
          <option value="" disabled>Choose an agent…</option>
          <option v-for="a in agents" :key="a" :value="a">{{ a }}</option>
        </BaseSelect>

        <SkeletonLoader v-if="agent && loading" :count="3" height="2.5rem" gap="0.5rem" />
        <LoadFailed v-else-if="agent && loadError" title="Couldn't read the agent's projects" :message="loadError" dense @retry="load" />
        <template v-else-if="agent && loaded">
          <p v-if="!candidates.length" class="text-sm text-gray-600 dark:text-gray-300">
            {{ agent }} has no project folders (a folder with a project.md). It may be stopped — start it and try again.
          </p>
          <ul class="max-h-72 divide-y divide-gray-100 overflow-y-auto dark:divide-gray-750">
            <li v-for="c in candidates" :key="c.path" class="flex items-center gap-3 py-2 text-sm">
              <div class="min-w-0 flex-1">
                <p class="truncate text-gray-900 dark:text-gray-100">{{ c.name }}</p>
                <p class="truncate font-mono text-xs text-gray-500 dark:text-gray-400">{{ c.path }}</p>
              </div>
              <BaseBadge v-if="c.already_imported">Imported</BaseBadge>
              <BaseButton v-else size="sm" variant="secondary" :loading="importing === c.path" loading-label="Importing…" @click="doImport(c)">Import</BaseButton>
            </li>
          </ul>
        </template>
      </template>

      <template v-else-if="result">
        <div class="space-y-2 text-sm text-gray-600 dark:text-gray-300" data-testid="project-import-epic-result">
          <p class="text-gray-900 dark:text-gray-100">
            Imported “{{ result.name }}” with {{ result.tasks }} {{ result.tasks === 1 ? 'task' : 'tasks' }}. It isn't started yet — start it when you're ready.
          </p>
          <p v-if="result.truncated">Only the first 100 sub-issues were imported.</p>
          <p v-if="result.skipped && result.skipped.length">
            Left out, because Trinity can't read them: <span class="font-mono">{{ result.skipped.join(', ') }}</span>.
          </p>
          <p v-if="result.unlinked && result.unlinked.length">
            Imported without a link to their issue: <span class="font-mono">{{ result.unlinked.join(', ') }}</span>.
          </p>
          <p v-if="result.nested && result.nested.length">
            These have sub-issues of their own, which weren't imported: <span class="font-mono">{{ result.nested.join(', ') }}</span>.
          </p>
        </div>
      </template>

      <form v-else class="space-y-3" data-testid="project-import-epic" @submit.prevent="doImportEpic">
        <p class="text-sm text-gray-600 dark:text-gray-300">
          The epic becomes the project and each of its sub-issues a task. It's a one-time copy: the tasks keep a link
          to their issue, and nothing is written back to GitHub. Only sub-issues Trinity can read are imported.
        </p>
        <BaseInput
          id="project-import-epic-ref"
          v-model="epic"
          label="GitHub epic"
          placeholder="your-org/repo#12 or https://github.com/your-org/repo/issues/12"
          :error="epicError"
          autocomplete="off"
        />
        <div class="flex justify-end">
          <BaseButton type="submit" variant="primary" :loading="importingEpic" loading-label="Importing…">Import</BaseButton>
        </div>
      </form>

      <InlineError v-if="error" :message="error" @dismiss="error = ''" />
      <div class="flex justify-end gap-2">
        <BaseButton v-if="result" variant="primary" data-testid="project-import-open" @click="openResult">Open the project</BaseButton>
        <BaseButton variant="secondary" @click="$emit('update:modelValue', false)">Close</BaseButton>
      </div>
    </div>
  </BaseModal>
</template>

<script setup>
import { ref, watch } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseInput from '@/components/base/BaseInput.vue'
import BaseModal from '@/components/base/BaseModal.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import { useProjectsStore } from '@/stores/projects'
import { epicImportErrorMessage, projectErrorMessage, validateEpicRef } from './projectsUtils'

const SOURCES = [
  { id: 'agent', label: 'From an agent' },
  { id: 'epic', label: 'From a GitHub epic' },
]

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  agents: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:modelValue', 'imported'])

const store = useProjectsStore()
const source = ref('agent')
const sourceButtons = ref([])
const agent = ref('')
const candidates = ref([])
const loading = ref(false)
const loaded = ref(false)
const loadError = ref('')
const importing = ref('')
const error = ref('')
const epic = ref('')
const epicError = ref('')
const importingEpic = ref(false)
const result = ref(null)

watch(() => props.modelValue, (open) => {
  if (!open) return
  agent.value = props.agents.length === 1 ? props.agents[0] : ''
  error.value = ''
  epicError.value = ''
  result.value = null
})

function pick(id) {
  source.value = id
  error.value = ''
}

// Roving tabindex, the native radio contract (as ThemeChoice): arrows move AND select.
function onSourceKey(event) {
  const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key]
  if (!step) return
  event.preventDefault()
  const current = Math.max(0, SOURCES.findIndex((s) => s.id === source.value))
  const next = (current + step + SOURCES.length) % SOURCES.length
  pick(SOURCES[next].id)
  sourceButtons.value[next]?.focus()
}

async function load() {
  loading.value = true
  loaded.value = false
  loadError.value = ''
  try {
    candidates.value = await store.importCandidates(agent.value)
    loaded.value = true
  } catch (err) {
    loadError.value = projectErrorMessage(err)
  } finally {
    loading.value = false
  }
}
watch(agent, (a) => { if (a) void load() })

async function doImport(c) {
  importing.value = c.path
  error.value = ''
  try {
    const out = await store.importProject(agent.value, c.path)
    emit('imported', out)
    emit('update:modelValue', false)
  } catch (err) {
    error.value = projectErrorMessage(err)
  } finally {
    importing.value = ''
  }
}

async function doImportEpic() {
  error.value = ''
  epicError.value = validateEpicRef(epic.value)
  if (epicError.value || importingEpic.value) return
  importingEpic.value = true
  try {
    const out = await store.importEpic(epic.value)
    const leftOut = out.truncated || (out.skipped || []).length || (out.unlinked || []).length || (out.nested || []).length
    if (leftOut) {
      result.value = out          // name what was left out before the project opens (principle 15)
      return
    }
    emit('imported', out)
    emit('update:modelValue', false)
  } catch (err) {
    error.value = epicImportErrorMessage(err)
  } finally {
    importingEpic.value = false
  }
}

function openResult() {
  const out = result.value
  result.value = null
  epic.value = ''
  emit('imported', out)
  emit('update:modelValue', false)
}
</script>
