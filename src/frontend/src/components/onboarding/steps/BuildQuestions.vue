<!--
  One set of Workspace Build questions inside the setup panel, asked one at a time.
  The answers are shared with the Workspace's own onboarding window.
-->
<template>
  <div class="mt-5 space-y-4">
    <template v-for="q in asked" :key="q.id">
      <p class="text-sm text-gray-900 dark:text-gray-100">{{ q.prompt }}</p>
      <!-- The question being answered, or a saved answer being changed, opens in place. -->
      <template v-if="q.id === active?.id">
        <p v-if="q.hint" class="text-[12.5px] text-gray-500 dark:text-gray-400">{{ q.hint }}</p>
        <div v-if="q.choices" class="flex flex-wrap items-center gap-2" :data-testid="`first-run-choices-${q.id}`">
          <BaseButton
            v-for="choice in q.choices"
            :key="choice.id"
            variant="secondary"
            size="sm"
            @click="answer(choice.id)"
          >{{ choice.label }}</BaseButton>
          <BaseButton v-if="editingId" variant="ghost" size="sm" data-testid="first-run-change-cancel" @click="stopChanging">Cancel</BaseButton>
        </div>
        <form v-else class="flex items-start gap-2" @submit.prevent="answer(draft)">
          <div class="flex-1 min-w-0">
            <BaseInput
              :key="q.id"
              v-model="draft"
              :placeholder="q.placeholder"
              :aria-label="q.prompt"
              data-testid="first-run-question-input"
            />
          </div>
          <BaseButton type="submit" size="sm" :disabled="!draft.trim()">Save</BaseButton>
          <BaseButton v-if="editingId" variant="ghost" size="sm" data-testid="first-run-change-cancel" @click="stopChanging">Cancel</BaseButton>
          <BaseButton v-else-if="q.optional" variant="ghost" size="sm" data-testid="first-run-no-website" @click="passed = true">We don't have one</BaseButton>
        </form>
      </template>
      <div v-else-if="answerLabel(q)" class="flex items-center gap-2">
        <p class="text-sm text-gray-600 dark:text-gray-300" :data-testid="`first-run-answer-${q.id}`">{{ answerLabel(q) }}</p>
        <BaseButton variant="ghost" size="sm" :data-testid="`first-run-change-${q.id}`" :aria-label="`Change your answer: ${q.prompt}`" @click="startChanging(q)">Change</BaseButton>
      </div>
    </template>

    <WhatIsAnAgent
      v-if="explaining"
      ref="explainer"
      class="pt-1"
      :software="intake.answers.software"
      @done="emit('update-intake', markAgentExplained(intake))"
    />
  </div>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import BaseButton from '../../base/BaseButton.vue'
import BaseInput from '../../base/BaseInput.vue'
import WhatIsAnAgent from '../WhatIsAnAgent.vue'
import { INTAKE_QUESTIONS, changeAnswer, intakeGroupDone, markAgentExplained, needsAgentExplainer } from '../../portal/builder/portalBuild'

const props = defineProps({
  group: { type: String, required: true },
  intake: { type: Object, required: true },
})
const emit = defineEmits(['update-intake', 'answered', 'all-done'])

const draft = ref('')
const passed = ref(false)
// The saved answer being changed, if any.
const editingId = ref(null)
const questions = computed(() => INTAKE_QUESTIONS.filter((q) => q.group === props.group))
const question = computed(() => questions.value.find((q) => !props.intake.answers[q.id] && !(q.optional && passed.value)) || null)
// Right after "I've never built one", what an agent is comes before the next question.
const explaining = computed(() => props.group === 'you' && needsAgentExplainer(props.intake))
// It lands under the answers they just gave, often below the panel's fold, so the panel
// scrolls to it: otherwise someone who has never built an agent may never see it.
const explainer = ref(null)
watch(explaining, async (now) => {
  if (!now) return
  await nextTick()
  const reduce = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches
  explainer.value?.$el?.scrollIntoView?.({ block: 'start', behavior: reduce ? 'auto' : 'smooth' })
})
// While a saved answer is being changed, the next unanswered question waits.
const asked = computed(() => questions.value.filter((q) => props.intake.answers[q.id] || (q === question.value && !explaining.value && !editingId.value)))
const active = computed(() => (editingId.value
  ? questions.value.find((q) => q.id === editingId.value)
  : explaining.value ? null : question.value) || null)

function answerLabel(q) {
  const value = props.intake.answers[q.id]
  return q.choices?.find((c) => c.id === value)?.label || value
}

function startChanging(q) {
  editingId.value = q.id
  draft.value = q.choices ? '' : props.intake.answers[q.id] || ''
}

function stopChanging() {
  editingId.value = null
  draft.value = ''
}

async function answer(value) {
  const changing = Boolean(editingId.value)
  emit('update-intake', changeAnswer(props.intake, active.value.id, value))
  stopChanging()
  // A changed company or website starts the research again, so the step hears again
  // that everything is answered.
  if (changing) {
    await nextTick()
    if (!question.value) emit('all-done')
  }
}

// The panel's Continue goes live once this set is answered.
watch(() => intakeGroupDone(props.intake, props.group), (done) => {
  if (done) emit('answered')
}, { immediate: true })

watch(question, (now) => {
  if (!now) emit('all-done')
}, { immediate: true })
</script>
