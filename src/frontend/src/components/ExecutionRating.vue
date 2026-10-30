<template>
  <!--
    The operator's thumb on this run (trinity-enterprise#938 C4): the human
    calibration sample the judge's agreement is measured against. Enterprise
    (eval_judge entitlement, ruled 2026-10-29); the route is person-only and
    owner-tier, and a caller it refuses never sees the control.
  -->
  <div
    v-if="store.ratingAvailable"
    class="bg-white dark:bg-gray-800 rounded-lg shadow px-4 py-3"
    data-testid="execution-rating"
  >
    <div class="flex flex-wrap items-center gap-3">
      <h2 class="text-sm font-semibold text-gray-900 dark:text-white">Your rating</h2>
      <div class="flex gap-2" role="group" aria-label="Rate this run">
        <BaseButton
          v-for="opt in OPTIONS"
          :key="opt.value"
          size="sm"
          :variant="current === opt.value ? 'primary' : 'secondary'"
          :aria-pressed="current === opt.value ? 'true' : 'false'"
          :loading="store.ratingSaving && pending === opt.value"
          :disabled="store.ratingSaving"
          :data-testid="`execution-rating-${opt.value}`"
          @click="choose(opt.value)"
        ><span aria-hidden="true">{{ opt.icon }}</span> {{ opt.label }}</BaseButton>
      </div>
      <span class="text-xs text-gray-500 dark:text-gray-400">
        Calibrates the judge — compared with its verdict on the Evaluations tab.
      </span>
    </div>

    <div v-if="current" class="mt-3 flex flex-col gap-2 max-w-xl">
      <BaseTextarea
        v-model="reason"
        :rows="2"
        label="Reason (optional)"
        :error="reasonError"
        data-testid="execution-rating-reason"
      />
      <div class="flex items-center gap-2">
        <BaseButton
          variant="ghost" size="sm"
          :disabled="store.ratingSaving || !!reasonError || reason.trim() === (store.rating?.reason || '')"
          data-testid="execution-rating-save-reason"
          @click="choose(current)"
        >Save reason</BaseButton>
      </div>
    </div>

    <InlineError
      v-if="store.ratingError"
      class="mt-3"
      :message="store.ratingError"
      data-testid="execution-rating-error"
      @dismiss="store.clearRatingError()"
    />
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseButton from './base/BaseButton.vue'
import BaseTextarea from './base/BaseTextarea.vue'
import InlineError from './InlineError.vue'
import { useEvalJudgeStore, MAX_REASON } from '../stores/evalJudge'

const props = defineProps({
  agentName: { type: String, required: true },
  executionId: { type: String, required: true },
})

// Shape and words as well as the pressed state — never hue alone.
const OPTIONS = [
  { value: 'up', icon: '👍', label: 'Good run' },
  { value: 'down', icon: '👎', label: 'Not right' },
]

const store = useEvalJudgeStore()
const reason = ref('')
const pending = ref('')

const current = computed(() => store.rating?.rating || null)
const reasonError = computed(() => (reason.value.length > MAX_REASON
  ? `Keep the reason under ${MAX_REASON} characters — say what was wrong, not the whole story.` : null))

async function choose(value) {
  if (reasonError.value) return
  pending.value = value
  await store.rate(props.agentName, props.executionId, value, reason.value)
  pending.value = ''
}

watch(() => store.rating?.reason, (r) => { reason.value = r || '' }, { immediate: true })
watch(() => [props.agentName, props.executionId], ([a, e]) => store.loadRating(a, e), { immediate: true })
</script>
