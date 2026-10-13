<!--
  Import a folder project (trinity-enterprise#661 v2.5).

  Lists the folder projects an agent holds — its own (`project_files/`) and the
  shared ones in its canon clone — and imports one ONCE: the charter, tasks
  (ids kept), the log and the decisions. After that the platform is the home;
  the dialog says so before anything happens (principle 26). A folder already
  imported is shown and not offered.
-->
<template>
  <BaseModal :model-value="modelValue" labelledby="project-import-title" @update:model-value="$emit('update:modelValue', $event)">
    <div class="space-y-4" data-testid="project-import">
      <h2 id="project-import-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">Import a project from an agent</h2>
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
      <InlineError v-if="error" :message="error" @dismiss="error = ''" />
      <div class="flex justify-end"><BaseButton variant="secondary" @click="$emit('update:modelValue', false)">Close</BaseButton></div>
    </div>
  </BaseModal>
</template>

<script setup>
import { ref, watch } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseModal from '@/components/base/BaseModal.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import { useProjectsStore } from '@/stores/projects'
import { projectErrorMessage } from './projectsUtils'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  agents: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:modelValue', 'imported'])

const store = useProjectsStore()
const agent = ref('')
const candidates = ref([])
const loading = ref(false)
const loaded = ref(false)
const loadError = ref('')
const importing = ref('')
const error = ref('')

watch(() => props.modelValue, (open) => {
  if (!open) return
  agent.value = props.agents.length === 1 ? props.agents[0] : ''
  error.value = ''
})

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
</script>
