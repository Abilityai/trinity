<!--
  Members & access (trinity-enterprise#661).

  The creator adds and removes people and agents and sets who can see the
  project. Everyone else sees the same lists read-only, with a line saying who
  can change them — the server enforces it either way; this only decides what
  to offer. An agent's OWNER may also take their agent off the project
  (withdrawing consent), even when they did not create it.

  Adding someone else's agent sends its owner a request; the row reads
  "Waiting for owner" until they answer. Removals confirm first, name the verb
  and restate the consequence, with Cancel focused (principle 19).
-->
<template>
  <BaseModal
    :model-value="modelValue"
    labelledby="project-members-title"
    panel-class="relative w-full max-w-lg rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800 max-h-[90vh] overflow-y-auto"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <div class="space-y-5" data-testid="project-members">
      <h2 id="project-members-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">
        Members &amp; access
      </h2>
      <p v-if="!canManage" class="rounded-lg border border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900 px-3 py-2 text-sm text-gray-600 dark:text-gray-300" data-testid="project-members-readonly">
        Only {{ creatorLabel }}, who created this project, can change members, agents and who can see it.
      </p>

      <section>
        <h3 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">People</h3>
        <ul class="mt-1 divide-y divide-gray-100 dark:divide-gray-750">
          <li v-for="m in project.members" :key="m.email" class="flex items-center gap-2 py-2 text-sm">
            <span class="min-w-0 flex-1 truncate text-gray-900 dark:text-gray-100">{{ m.email === myEmail ? 'You' : m.email }}</span>
            <BaseBadge>{{ m.role === 'creator' ? 'Creator' : 'Member' }}</BaseBadge>
            <BaseButton
              v-if="canManage && m.role !== 'creator'"
              size="sm"
              variant="ghost"
              :data-testid="`remove-member-${m.email}`"
              @click="confirmRemove('member', m.email)"
            >Remove</BaseButton>
          </li>
        </ul>
        <form v-if="canManage" class="mt-2 flex items-start gap-2" @submit.prevent="addMember">
          <BaseInput
            id="project-add-member"
            v-model="newMember"
            class="flex-1"
            type="email"
            placeholder="name@your-company.com"
            aria-label="Add a person by email"
          />
          <BaseButton type="submit" variant="secondary" :loading="busy === 'member'" :disabled="!newMember.trim()">Add</BaseButton>
        </form>
        <p v-if="canManage" class="mt-1 text-xs text-gray-500 dark:text-gray-400">People at your company only. Outside clients can't be added yet.</p>
      </section>

      <section>
        <h3 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Agents</h3>
        <p v-if="!project.agents.length" class="mt-1 text-sm text-gray-500 dark:text-gray-400">No agents yet. Add one so people can work on the project with it.</p>
        <ul class="mt-1 divide-y divide-gray-100 dark:divide-gray-750">
          <li v-for="a in project.agents" :key="a.agent_name" class="flex items-center gap-2 py-2 text-sm">
            <span class="min-w-0 flex-1 truncate text-gray-900 dark:text-gray-100">
              {{ a.agent_name }}<span v-if="owned.has(a.agent_name)" class="text-gray-500 dark:text-gray-400"> · yours</span>
            </span>
            <BaseBadge :variant="AGENT_BADGE[a.state] || 'neutral'">{{ agentStateLabel(a.state) }}</BaseBadge>
            <BaseButton
              v-if="canManage || owned.has(a.agent_name)"
              size="sm"
              variant="ghost"
              :data-testid="`remove-agent-${a.agent_name}`"
              @click="confirmRemove('agent', a.agent_name)"
            >{{ a.state === 'pending' ? 'Cancel request' : 'Remove' }}</BaseButton>
          </li>
        </ul>
        <form v-if="canManage && addableAgents.length" class="mt-2 flex items-start gap-2" @submit.prevent="addAgent">
          <BaseSelect id="project-add-agent" v-model="newAgent" class="flex-1" aria-label="Add an agent">
            <option value="" disabled>Add an agent you can use…</option>
            <option v-for="name in addableAgents" :key="name" :value="name">{{ name }}</option>
          </BaseSelect>
          <BaseButton type="submit" variant="secondary" :loading="busy === 'agent'" :disabled="!newAgent">Add</BaseButton>
        </form>
        <p v-if="canManage" class="mt-1 text-xs text-gray-500 dark:text-gray-400">
          Your own agents are added straight away. For anyone else's, its owner gets a request in their Workspace.
        </p>
      </section>

      <section v-if="canManage">
        <BaseSelect id="project-visibility-edit" :model-value="project.visibility" label="Who can see it" @update:model-value="setVisibility">
          <option value="members">Members only</option>
          <option value="company">Company</option>
        </BaseSelect>
      </section>

      <InlineError v-if="error" :message="error" @dismiss="error = ''" />

      <div class="flex justify-end">
        <BaseButton variant="secondary" @click="$emit('update:modelValue', false)">Done</BaseButton>
      </div>
    </div>

    <ConfirmDialog
      v-model:visible="confirm.open"
      :title="confirm.title"
      :message="confirm.message"
      :confirm-text="confirm.verb"
      cancel-text="Keep"
      variant="danger"
      @confirm="doRemove"
    />
  </BaseModal>
</template>

<script setup>
import { computed, ref } from 'vue'
import BaseModal from '@/components/base/BaseModal.vue'
import BaseInput from '@/components/base/BaseInput.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import InlineError from '@/components/InlineError.vue'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import { useProjectsStore } from '@/stores/projects'
import { agentStateLabel, projectErrorMessage } from './projectsUtils'

const AGENT_BADGE = { active: 'success', pending: 'warning', declined: 'neutral' }

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  project: { type: Object, required: true },
  myEmail: { type: String, default: '' },
  // Agent names on the person's roster, and the subset they own.
  rosterAgents: { type: Array, default: () => [] },
  ownedAgents: { type: Array, default: () => [] },
})
defineEmits(['update:modelValue'])

const store = useProjectsStore()
const newMember = ref('')
const newAgent = ref('')
const busy = ref('')
const error = ref('')
const confirm = ref({ open: false, kind: '', target: '', title: '', message: '', verb: '' })

const canManage = computed(() => props.project?.can?.manage_members === true)
const owned = computed(() => new Set(props.ownedAgents))
const creatorLabel = computed(() => {
  const c = (props.project.members || []).find((m) => m.role === 'creator')
  return c ? c.email : 'its creator'
})
const addableAgents = computed(() => {
  const on = new Set((props.project.agents || []).filter((a) => a.state !== 'declined').map((a) => a.agent_name))
  return props.rosterAgents.filter((n) => !on.has(n))
})

async function run(kind, fn) {
  busy.value = kind
  error.value = ''
  try { await fn() } catch (err) { error.value = projectErrorMessage(err) } finally { busy.value = '' }
}

const addMember = () => run('member', async () => {
  await store.addMember(props.project.id, newMember.value.trim())
  newMember.value = ''
})
const addAgent = () => run('agent', async () => {
  await store.addAgent(props.project.id, newAgent.value)
  newAgent.value = ''
})
const setVisibility = (v) => run('visibility', () => store.update(props.project.id, { visibility: v }))

function confirmRemove(kind, target) {
  const agent = kind === 'agent' ? props.project.agents.find((a) => a.agent_name === target) : null
  const pending = agent?.state === 'pending'
  confirm.value = kind === 'member'
    ? { open: true, kind, target, verb: 'Remove',
        title: `Remove ${target}?`,
        message: `${target} will no longer see this project. Their own chats stay theirs.` }
    : { open: true, kind, target, verb: pending ? 'Cancel request' : 'Remove',
        title: pending ? `Cancel the request for ${target}?` : `Remove ${target}?`,
        message: pending
          ? `${target}'s owner won't be asked any more.`
          : `${target} will stop getting this project's context, and nobody can start new project chats with it. Existing chats stay.` }
}

function doRemove() {
  const { kind, target } = confirm.value
  confirm.value = { ...confirm.value, open: false }
  return run(kind, () => (kind === 'member'
    ? store.removeMember(props.project.id, target)
    : store.removeAgent(props.project.id, target)))
}
</script>
