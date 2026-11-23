<!--
  trinity-enterprise#1234 — the steward's kickoff plan, on the project page.

  At kickoff the project's agent steward proposes the plan: milestones with
  their numbers, tasks under them, and which tasks block which. A person
  approves it ONCE. This card previews the WHOLE plan as the server froze it
  (`proposal.plan`) — every milestone with its targets, every task under its
  milestone, each blocked-by link as "after <task>" — because a 60-task plan
  does not fit the Inbox's 8 KB rows; the Inbox card links here. Nothing in the
  plan exists on the project until it is approved, so the task list stays as
  it was.

  The person it waits on (`waiting_on_people`) answers here through the same
  ask endpoint the Inbox uses: Approve once, or Decline with a reason the
  steward reads next. Anyone else reads who decides.

  With an agent steward and no plan yet, the card says so; someone who may
  change the goal can write the plan themselves (the milestone editor, then
  tasks) instead of waiting for a proposal.

  Agent-authored text is interpolated, never v-html. The preview is a bounded
  region: it scrolls inside its own box and never grows the page.
-->
<template>
  <BaseCard v-if="plan" data-testid="project-plan-proposal">
    <p class="text-[11px] font-mono font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Proposed plan</p>
    <h2 class="mt-1 text-sm font-semibold text-gray-900 dark:text-gray-100">
      {{ proposal.proposed_by }} proposed a plan for this project
    </h2>
    <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-300">
      <span class="tabular-nums">{{ plan.counts }}</span><template v-if="proposal.proposed_at"> · <span :title="absolute(proposal.proposed_at)">{{ relative(proposal.proposed_at) }}</span></template>.
      Nothing in it exists on the project until {{ waitsOnMe ? 'you approve it' : 'it is approved' }}.
    </p>
    <p v-if="proposal.note" class="mt-2 text-sm text-gray-700 dark:text-gray-300" data-testid="project-plan-note">{{ proposal.note }}</p>

    <BoundedRegion
      class="mt-3 max-h-[28rem] rounded-lg border border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900 px-3 py-2"
      label="The proposed plan"
      data-testid="project-plan-preview"
    >
      <ol class="space-y-3">
        <li v-for="m in plan.milestones" :key="m.key" :data-testid="`project-plan-milestone-${m.key}`">
          <p class="text-sm font-semibold text-gray-900 dark:text-gray-100">{{ m.name }}</p>
          <p v-if="m.reached_means" class="text-[12.5px] text-gray-600 dark:text-gray-300">{{ m.reached_means }}</p>
          <ul v-if="m.targets?.length" class="mt-1 space-y-0.5">
            <li
              v-for="(t, i) in m.targets"
              :key="`${t.agent}:${t.metric}`"
              class="text-[12.5px] text-gray-700 dark:text-gray-300"
              :data-testid="`project-plan-target-${m.key}-${i}`"
            >{{ t.metric }} <span class="text-gray-500 dark:text-gray-400">({{ t.agent }})</span> {{ word(t.direction) }} <span class="tabular-nums">{{ number(t.target) }}</span></li>
          </ul>
          <ul v-if="tasksUnder(m.key).length" class="mt-2 space-y-1.5 border-l border-gray-200 dark:border-gray-750 pl-3">
            <PlanTask v-for="t in tasksUnder(m.key)" :key="t.key" :task="t" />
          </ul>
        </li>
      </ol>
      <section v-if="unplaced.length" class="mt-3" data-testid="project-plan-unplaced">
        <p class="text-[12.5px] font-medium text-gray-600 dark:text-gray-300">{{ plan.milestones.length ? 'Not under a milestone' : 'Tasks' }}</p>
        <ul class="mt-1 space-y-1.5 border-l border-gray-200 dark:border-gray-750 pl-3">
          <PlanTask v-for="t in unplaced" :key="t.key" :task="t" />
        </ul>
      </section>
    </BoundedRegion>

    <template v-if="waitsOnMe && proposal.ask_id">
      <form v-if="declining" class="mt-3" data-testid="project-plan-decline-form" @submit.prevent="decline">
        <BaseTextarea
          v-model="reason"
          :label="`Why decline? ${proposal.proposed_by} reads this next.`"
          :error="reasonError"
          :rows="3"
        />
        <div class="mt-2 flex flex-wrap gap-2">
          <BaseButton type="submit" variant="secondary" :loading="busy" loading-label="Declining…" data-testid="project-plan-decline-send">Send decline</BaseButton>
          <BaseButton variant="ghost" :disabled="busy" @click="declining = false">Cancel</BaseButton>
        </div>
      </form>
      <div v-else class="mt-3 flex flex-wrap gap-2">
        <BaseButton :loading="busy" loading-label="Approving…" data-testid="project-plan-approve" @click="approve">Approve plan</BaseButton>
        <BaseButton variant="secondary" :disabled="busy" data-testid="project-plan-decline" @click="declining = true">Decline…</BaseButton>
      </div>
      <p class="mt-2 text-[12.5px] text-gray-600 dark:text-gray-300">
        Approve creates exactly this plan, once. Decline creates nothing.
      </p>
    </template>
    <p v-else class="mt-3 text-sm font-medium text-status-warning-700 dark:text-status-warning-400" data-testid="project-plan-waiting">
      Waiting on {{ proposal.waiting_on || `${proposal.proposed_by}'s primary contact` }}.
    </p>
    <InlineError v-if="error" class="mt-2" :message="error" @dismiss="error = ''" />
  </BaseCard>

  <BaseCard v-else-if="showEmpty" data-testid="project-plan-empty">
    <p class="text-sm font-semibold text-gray-900 dark:text-gray-100">No plan yet</p>
    <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">
      {{ steward.ref }} can propose one — milestones with their numbers and the tasks under them — for
      {{ canEdit ? 'you' : "the project's creator" }} to approve once. Nothing is created until then.
    </p>
    <BaseButton v-if="canEdit" class="mt-3" variant="secondary" data-testid="project-plan-write" @click="$emit('write-plan')">
      Write the plan yourself
    </BaseButton>
  </BaseCard>
</template>

<script setup>
import { computed, defineComponent, h, ref } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import BaseTextarea from '@/components/base/BaseTextarea.vue'
import BoundedRegion from '@/components/BoundedRegion.vue'
import InlineError from '@/components/InlineError.vue'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import { useProjectsStore } from '@/stores/projects'
import { projectErrorMessage } from './projectsUtils'

const props = defineProps({
  project: { type: Object, required: true },
  myEmail: { type: String, default: '' },
})
defineEmits(['write-plan'])

const store = useProjectsStore()

const proposal = computed(() => props.project.proposal || null)
const plan = computed(() => (proposal.value?.kind === 'plan' && proposal.value.plan) || null)
const steward = computed(() => props.project.steward || null)
const canEdit = computed(() => props.project.can?.edit === true && !props.project.archived_at)
const showEmpty = computed(() => steward.value?.kind === 'agent' && !props.project.archived_at
  && !(props.project.milestones || []).length && !proposal.value)

const waitsOnMe = computed(() => {
  const people = proposal.value?.waiting_on_people || []
  return Boolean(props.myEmail) && people.includes(props.myEmail)
})

const byKey = computed(() => Object.fromEntries((plan.value?.tasks || []).map((t) => [t.key, t])))
const existingMilestones = computed(() => Object.fromEntries((props.project.milestones || []).map((m) => [m.id, m])))
const milestoneKeys = computed(() => new Set((plan.value?.milestones || []).map((m) => m.key)))
const tasksUnder = (key) => (plan.value?.tasks || []).filter((t) => t.milestone === key)
// A task under no milestone of THIS plan: none at all, or an existing one (a
// follow-up plan), named in its own line.
const unplaced = computed(() => (plan.value?.tasks || []).filter((t) => !milestoneKeys.value.has(t.milestone)))

const refLabel = (ref) => byKey.value[ref]?.title || ref

const PlanTask = defineComponent({
  props: { task: { type: Object, required: true } },
  setup(p) {
    return () => {
      const t = p.task
      const meta = []
      if (t.for_person) meta.push(`For a person: ${t.owner}`)
      else if (t.owner) meta.push(`Owner: ${t.owner}`)
      if (t.agent) meta.push(`Agent: ${t.agent}`)
      if (t.parent) meta.push(`Subtask of ${refLabel(t.parent)}`)
      const ms = t.milestone && !milestoneKeys.value.has(t.milestone)
        ? existingMilestones.value[t.milestone]?.name || t.milestone : null
      if (ms) meta.push(`Under ${ms}`)
      return h('li', { 'data-testid': `project-plan-task-${t.key}` }, [
        h('p', { class: 'text-sm text-gray-900 dark:text-gray-100' }, t.title),
        meta.length ? h('p', { class: 'text-[12.5px] text-gray-600 dark:text-gray-300' }, meta.join(' · ')) : null,
        t.blocked_by?.length
          ? h('p', { class: 'text-[12.5px] text-gray-600 dark:text-gray-300', 'data-testid': `project-plan-edges-${t.key}` },
            'after ' + t.blocked_by.map(refLabel).join(', '))
          : null,
      ])
    }
  },
})

const word = (direction) => ({ up: '≥', down: '≤', hold: '=' }[direction] || '→')
const number = (n) => (typeof n === 'number' ? n.toLocaleString() : String(n ?? ''))
const relative = (ts) => formatRelativeTime(ts)
const absolute = (ts) => formatLocalDateTime(ts)

const busy = ref(false)
const error = ref('')
const declining = ref(false)
const reason = ref('')
const reasonError = ref('')

async function answer(response, text) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await store.answerProposal(props.project.id, proposal.value.ask_id, response, text)
    declining.value = false
  } catch (err) {
    error.value = projectErrorMessage(err)
  } finally {
    busy.value = false
  }
}

const approve = () => answer('Approve', null)

function decline() {
  const text = reason.value.trim()
  if (!text) {
    reasonError.value = `Say why, so ${proposal.value.proposed_by} can propose a better plan — e.g. “Too much for one quarter.”`
    return
  }
  reasonError.value = ''
  return answer('Decline', text)
}
</script>
