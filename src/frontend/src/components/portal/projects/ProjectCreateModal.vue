<!--
  New project / Make this a project (trinity-enterprise#661).

  One form for both doors. From a chat (`linkThread` set) the chat is linked on
  create and its agent is added — straight away if you own it, otherwise its
  owner gets a request. Opens pre-filled (principle 3): the chat's title seeds
  the name. Validation names the problem and gives an example (principle 17);
  a failed create keeps the form and says why next to the button (principle 18).
-->
<template>
  <BaseModal
    :model-value="modelValue"
    labelledby="project-create-title"
    :close-on-backdrop="false"
    panel-class="relative w-full max-w-lg rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <form class="space-y-4" data-testid="project-create-form" @submit.prevent="submit">
      <h2 id="project-create-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">
        {{ linkThread ? 'Make this a project' : 'New project' }}
      </h2>
      <p
        v-if="linkThread"
        class="rounded-lg border border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900 px-3 py-2 text-sm text-gray-600 dark:text-gray-300"
        data-testid="project-create-link-note"
      >
        Links this chat, "{{ linkThread.title || 'Untitled chat' }}", with {{ linkThread.agent }}.
        If you don't own {{ linkThread.agent }}, its owner is asked first.
      </p>

      <BaseInput id="project-name" v-model="form.name" label="Name" :error="errors.name" />
      <BaseTextarea
        id="project-goal"
        v-model="form.goal"
        label="Goal"
        :rows="3"
        help="One or two sentences. Every agent on the project reads this first."
        :error="errors.goal"
      />
      <div class="grid gap-4 sm:grid-cols-2">
        <BaseSelect id="project-steward" v-model="form.steward" label="Steward">
          <option value="">You</option>
          <option v-for="a in agents" :key="a" :value="`agent:${a}`">{{ a }} (agent)</option>
        </BaseSelect>
        <BaseSelect id="project-visibility" v-model="form.visibility" label="Who can see it">
          <option value="members">Members only</option>
          <option value="company">Company</option>
        </BaseSelect>
      </div>
      <p class="-mt-2 text-xs text-gray-500 dark:text-gray-400">
        {{ form.visibility === 'company'
          ? 'Everyone signed in at your company can see it. Outside clients never can.'
          : 'Only people you add can see it. You can change this later.' }}
      </p>
      <BaseInput
        id="project-tracker"
        v-model="form.tracker_url"
        label="Tracker link (optional)"
        placeholder="https://"
        :error="errors.tracker_url"
      />

      <InlineError v-if="submitError" :message="submitError" @dismiss="submitError = ''" />

      <div class="flex justify-end gap-2">
        <BaseButton variant="secondary" @click="$emit('update:modelValue', false)">Cancel</BaseButton>
        <BaseButton type="submit" :loading="saving" loading-label="Creating…" data-testid="project-create-submit">
          Create project
        </BaseButton>
      </div>
    </form>
  </BaseModal>
</template>

<script setup>
import { reactive, ref, watch } from 'vue'
import BaseModal from '@/components/base/BaseModal.vue'
import BaseInput from '@/components/base/BaseInput.vue'
import BaseTextarea from '@/components/base/BaseTextarea.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import InlineError from '@/components/InlineError.vue'
import { useProjectsStore } from '@/stores/projects'
import { projectErrorMessage, validateProjectForm } from './projectsUtils'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // { id, title, agent } when opened from a chat ("Make this a project").
  linkThread: { type: Object, default: null },
  // Agent names the person can use — offered as a steward.
  agents: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:modelValue', 'created'])

const store = useProjectsStore()
const form = reactive({ name: '', goal: '', steward: '', visibility: 'members', tracker_url: '' })
const errors = ref({})
const submitError = ref('')
const saving = ref(false)

watch(() => props.modelValue, (open) => {
  if (!open) return
  Object.assign(form, {
    name: props.linkThread?.title || '', goal: '', steward: '', visibility: 'members', tracker_url: '',
  })
  errors.value = {}
  submitError.value = ''
}, { immediate: true })

async function submit() {
  errors.value = validateProjectForm(form)
  if (Object.keys(errors.value).length) return
  const body = {
    name: form.name.trim(),
    goal: form.goal.trim(),
    visibility: form.visibility,
  }
  if (form.tracker_url.trim()) body.tracker_url = form.tracker_url.trim()
  if (form.steward.startsWith('agent:')) body.steward = { kind: 'agent', ref: form.steward.slice(6) }
  if (props.linkThread) body.link_thread_id = props.linkThread.id
  saving.value = true
  submitError.value = ''
  try {
    const project = await store.create(body)
    emit('created', project)
    emit('update:modelValue', false)
  } catch (err) {
    submitError.value = projectErrorMessage(err)
  } finally {
    saving.value = false
  }
}
</script>
