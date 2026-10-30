<template>
  <!-- Operations › Outcomes › Trackers (trinity-enterprise#941): the GitHub
       repos whose `project` epics the fleet analysis reads. An admin setting
       with the EVAL_OUTCOMES_REGISTRIES env var as the default; a save applies
       to the next weekly run. -->
  <BaseCard data-testid="outcomes-trackers">
    <div class="flex items-center gap-2 min-h-[32px]">
      <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">Trackers</h3>
      <span class="text-xs text-gray-500 dark:text-gray-400" data-testid="outcomes-trackers-source">{{ sourceText }}</span>
      <div v-if="!editing && store.trackers" class="ml-auto flex gap-2">
        <BaseButton
          v-if="store.trackers.source === 'setting'"
          variant="ghost" size="sm" :loading="store.trackersSaving && resetting"
          data-testid="outcomes-trackers-reset" @click="reset"
        >Reset to default</BaseButton>
        <BaseButton variant="secondary" size="sm" data-testid="outcomes-trackers-edit" @click="startEdit">Edit</BaseButton>
      </div>
    </div>

    <InlineError
      v-if="store.trackersError && !store.trackers"
      class="mt-2"
      :message="store.trackersError"
      retryable
      data-testid="outcomes-trackers-failed"
      @retry="store.loadTrackers()"
      @dismiss="store.loadTrackers()"
    />
    <div v-else-if="!store.trackers" class="mt-2" aria-busy="true">
      <SkeletonLoader variant="rows" :count="1" height="1.25rem" />
    </div>

    <template v-else-if="editing">
      <BaseTextarea
        v-model="draft"
        class="mt-3"
        mono
        :rows="4"
        label="One owner/repo per line"
        :error="draftError"
        help="Each repo's project-labelled epics are read with the instance's GitHub credential. Leave it empty to read no tracker."
        data-testid="outcomes-trackers-draft"
      />
      <div class="mt-3 flex items-center gap-2">
        <BaseButton variant="primary" size="sm" :loading="store.trackersSaving" :disabled="!!draftError" data-testid="outcomes-trackers-save" @click="save">Save</BaseButton>
        <BaseButton variant="ghost" size="sm" :disabled="store.trackersSaving" data-testid="outcomes-trackers-cancel" @click="editing = false">Cancel</BaseButton>
        <span class="text-xs text-gray-500 dark:text-gray-400">Applies to the next weekly run.</span>
      </div>
    </template>

    <template v-else>
      <ul v-if="store.trackers.repos.length" class="mt-2 max-h-[10rem] overflow-y-auto space-y-1" data-testid="outcomes-trackers-list">
        <li v-for="r in store.trackers.repos" :key="r" class="font-mono text-[12.5px] text-gray-700 dark:text-gray-300">{{ r }}</li>
      </ul>
      <p v-else class="mt-2 text-sm text-gray-600 dark:text-gray-300" data-testid="outcomes-trackers-empty">
        No tracker is read. Projects kept as GitHub epics count toward the fleet once you add their repo.
      </p>
    </template>

    <InlineError
      v-if="store.trackersSaveError"
      class="mt-3"
      :message="store.trackersSaveError"
      data-testid="outcomes-trackers-error"
      @dismiss="store.clearTrackersSaveError()"
    />
  </BaseCard>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import BaseButton from '../base/BaseButton.vue'
import BaseCard from '../base/BaseCard.vue'
import BaseTextarea from '../base/BaseTextarea.vue'
import InlineError from '../InlineError.vue'
import SkeletonLoader from '../SkeletonLoader.vue'
import { useOutcomesStore, parseTrackers } from '../../stores/outcomes'

const store = useOutcomesStore()
const editing = ref(false)
const resetting = ref(false)
const draft = ref('')

const draftError = computed(() => (editing.value ? parseTrackers(draft.value).error : null))

const sourceText = computed(() => {
  const t = store.trackers
  if (!t) return ''
  if (t.source === 'setting') return 'saved here'
  if (t.source === 'env') return 'from the environment default'
  return 'none configured'
})

function startEdit() {
  // Pre-filled: the admin edits the list in force, never a blank.
  draft.value = (store.trackers?.repos || []).join('\n')
  store.clearTrackersSaveError()
  editing.value = true
}

async function save() {
  const { repos, error } = parseTrackers(draft.value)
  if (error) return
  if (await store.saveTrackers(repos)) editing.value = false
}

async function reset() {
  resetting.value = true
  await store.resetTrackers()
  resetting.value = false
}

onMounted(() => {
  if (!store.trackers) store.loadTrackers()
})
</script>
