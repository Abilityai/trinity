<!--
  New project / Make this a project / Edit project (trinity-enterprise#661).

  One form for three doors:
    * New project (from the list);
    * Make this a project (`linkThread` set): the chat is linked on create and
      its agent is added — straight away if you own it, otherwise its owner is
      asked;
    * Edit (`project` set): the creator changes name, goal, steward, tracker
      link and who can see it. Only changed fields are sent.

  Opens pre-filled (principle 3). The steward is you, someone else at the
  company (by email), or an agent you can use. Validation names the problem
  and gives an example (principle 17); a failed save keeps the form and says
  why next to the button (principle 18).
-->
<template>
  <BaseModal
    :model-value="modelValue"
    labelledby="project-form-title"
    :close-on-backdrop="false"
    panel-class="relative w-full max-w-lg rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <form class="space-y-4" data-testid="project-create-form" @submit.prevent="submit">
      <h2 id="project-form-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">
        {{ project ? 'Edit project' : (linkThread ? 'Make this a project' : 'New project') }}
      </h2>
      <p
        v-if="linkThread && !project"
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
          <option value="me">You</option>
          <option value="person">Someone else…</option>
          <option v-for="a in agents" :key="a" :value="`agent:${a}`">{{ a }} (agent)</option>
        </BaseSelect>
        <BaseSelect id="project-visibility" v-model="form.visibility" label="Who can see it">
          <option value="members">Members only</option>
          <option value="company">Company</option>
        </BaseSelect>
      </div>
      <BaseInput
        v-if="form.steward === 'person'"
        id="project-steward-email"
        v-model="form.steward_email"
        type="email"
        label="Steward's email"
        placeholder="name@your-company.com"
        :error="errors.steward"
      />
      <p class="-mt-2 text-xs text-gray-500 dark:text-gray-400">
        {{ form.visibility === 'company'
          ? 'Everyone signed in at your company can see it. Outside clients see it only if invited.'
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
        <BaseButton
          type="submit"
          :loading="saving"
          :loading-label="project ? 'Saving…' : 'Creating…'"
          data-testid="project-create-submit"
        >
          {{ project ? 'Save' : 'Create project' }}
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
import { projectErrorMessage, stewardFormValue, stewardPayload, validateProjectForm } from './projectsUtils'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // { id, title, agent } when opened from a chat ("Make this a project").
  linkThread: { type: Object, default: null },
  // The project page's data when editing.
  project: { type: Object, default: null },
  myEmail: { type: String, default: '' },
  // Agent names the person can use — offered as a steward.
  agents: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:modelValue', 'created', 'saved'])

const store = useProjectsStore()
const form = reactive({ name: '', goal: '', steward: 'me', steward_email: '', visibility: 'members', tracker_url: '' })
const errors = ref({})
const submitError = ref('')
const saving = ref(false)
let initial = {}

watch(() => props.modelValue, (open) => {
  if (!open) return
  const p = props.project
  Object.assign(form, p
    ? {
        name: p.name, goal: p.goal, visibility: p.visibility, tracker_url: p.tracker_url || '',
        ...stewardFormValue(p.steward, props.myEmail),
      }
    : {
        name: props.linkThread?.title || '', goal: '', steward: 'me', steward_email: '',
        visibility: 'members', tracker_url: '',
      })
  initial = { ...form }
  errors.value = {}
  submitError.value = ''
}, { immediate: true })

async function submit() {
  errors.value = validateProjectForm(form)
  if (Object.keys(errors.value).length) return
  const all = {
    name: form.name.trim(),
    goal: form.goal.trim(),
    visibility: form.visibility,
    tracker_url: form.tracker_url.trim() || null,
    steward: stewardPayload(form, props.myEmail),
  }
  saving.value = true
  submitError.value = ''
  try {
    if (props.project) {
      const changed = {}
      if (all.name !== initial.name) changed.name = all.name
      if (all.goal !== initial.goal) changed.goal = all.goal
      if (all.visibility !== initial.visibility) changed.visibility = all.visibility
      if ((all.tracker_url || '') !== (initial.tracker_url || '')) changed.tracker_url = all.tracker_url
      if (form.steward !== initial.steward || form.steward_email !== initial.steward_email) changed.steward = all.steward
      if (Object.keys(changed).length) await store.update(props.project.id, changed)
      emit('saved')
    } else {
      const body = { name: all.name, goal: all.goal, visibility: all.visibility }
      if (all.tracker_url) body.tracker_url = all.tracker_url
      if (all.steward) body.steward = all.steward
      if (props.linkThread) body.link_thread_id = props.linkThread.id
      const created = await store.create(body)
      emit('created', created)
    }
    emit('update:modelValue', false)
  } catch (err) {
    submitError.value = projectErrorMessage(err)
  } finally {
    saving.value = false
  }
}
</script>
