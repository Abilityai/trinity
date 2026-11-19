<!--
  The Steward card (trinity-enterprise#896): who moves this project, and the
  state behind that — in place of the bare header label.

  Trinity wakes an agent steward (trinity-enterprise#1236) once a person starts
  it — when someone else changes a task, an ask on the project is answered, and
  daily while work is open. The card says whether it is woken, the one reason
  it is not (not started, paused, autonomy off …), the last run, what wakes it
  next, and offers Start / Pause to the creator and the agent's owner. Before
  that, an agent steward acted through its OWN scheduled sweep, so the card
  still says what the read carries besides: the last sweep (the
  agent's last digest read, stamped server-side), its schedule (`none` → "No
  sweep scheduled", `scheduled` → the next run, `hidden` → "Scheduled" without
  a time the viewer may not read, `unknown` → the read failed), and health.
  A backend that predates those fields omits the lines rather than guessing.

  A steward agent's proposal (trinity-enterprise#980) — members, agents, the
  steward, visibility, task owners — waits on the project's creator, who
  answers it once in the Inbox. The card says who it waits on, so nobody
  reads the unchanged member list as the proposal having been lost.

  An agent steward is usable only by people who hold it (trinity-enterprise#1161
  — project membership never carries agent access). A member without it reads
  that, by name, and can ask the agent's owner; the owner reads which members
  lack it and gives access here. Both are the platform's existing access request
  and share row, behind the project routes.

  An agent steward's line says whether it carries the library's
  project-management skills (trinity-enterprise#1169); a person steward's card
  says what having no steward agent means (nothing reads the digest between
  visits).

  A person steward sees what waits on them (tasks to verify, stuck tasks —
  from the roll-up the page already has) and, when it is me, the way to the
  "I steward" tab.
-->
<template>
  <BaseCard data-testid="project-steward">
    <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Steward</h2>
    <div class="mt-2 flex items-center gap-2">
      <PortalAvatar v-if="steward?.kind === 'agent'" :name="steward.ref" :size="24" />
      <p class="min-w-0 flex-1 truncate text-sm font-medium text-gray-900 dark:text-gray-100">{{ who }}</p>
      <BaseBadge v-if="health" :variant="HEALTH_BADGE[health.state] || 'neutral'" dot>{{ healthLabel(health.state) }}</BaseBadge>
    </div>
    <!-- trinity-enterprise#980: a decision waiting on someone — tinted so it
         reads as "act on this", not as one more fact in the list below. -->
    <div
      v-if="proposal"
      class="mt-3 rounded-lg border border-status-warning-200 dark:border-status-warning-900/50 bg-status-warning-50 dark:bg-status-warning-900/20 px-3 py-2.5"
      data-testid="project-steward-proposal"
    >
      <p class="text-sm font-semibold text-status-warning-700 dark:text-status-warning-400" data-testid="project-steward-proposal-heading">
        {{ waitsOnMe ? 'Waiting on you' : `Waiting on ${waitingOn}` }}
      </p>
      <p class="mt-1 text-sm text-gray-700 dark:text-gray-300">
        {{ proposal.proposed_by }} proposed changes to this project {{ '' }}<span v-if="proposal.proposed_at" :title="absolute(proposal.proposed_at)">{{ relative(proposal.proposed_at) }}</span>.
        {{ waitsOnMe ? 'Nothing changes until you approve it.' : 'Nothing changes until they approve it.' }}
      </p>
      <router-link
        v-if="waitsOnMe && inboxRoute"
        :to="inboxRoute"
        :class="INBOX_LINK_CLASS"
        class="mt-2 text-sm font-medium"
        data-testid="project-steward-proposal-review"
      >Review in Inbox</router-link>
    </div>
    <p v-if="!steward" class="mt-2 text-sm text-gray-600 dark:text-gray-300">The creator stewards it until someone is named.</p>

    <template v-else-if="steward.kind === 'agent'">
      <!-- trinity-enterprise#1161: I am on the project but can't use its steward. -->
      <div
        v-if="access && !access.has_access"
        class="mt-3 rounded-lg border border-status-warning-200 dark:border-status-warning-900/50 bg-status-warning-50 dark:bg-status-warning-900/20 px-3 py-2.5"
        data-testid="project-steward-no-access"
      >
        <p class="text-sm font-semibold text-status-warning-700 dark:text-status-warning-400">You can't use {{ access.agent }} yet</p>
        <p class="mt-1 text-sm text-gray-700 dark:text-gray-300">
          Being on the project doesn't give you the steward agent. Until its owner gives you access, you can't chat with it and its questions don't reach you.
        </p>
        <p v-if="access.request === 'pending'" class="mt-2 text-sm font-medium text-gray-700 dark:text-gray-300" data-testid="project-steward-access-pending">Access requested. Waiting on the agent's owner.</p>
        <template v-else>
          <p v-if="access.request === 'denied'" class="mt-2 text-sm text-gray-700 dark:text-gray-300" data-testid="project-steward-access-denied">The agent's owner declined your last request.</p>
          <BaseButton
            class="mt-2" size="sm" variant="secondary" :disabled="asking"
            data-testid="project-steward-access-request"
            @click="askForAccess"
          >{{ asking ? 'Asking…' : (access.request === 'denied' ? 'Ask again' : 'Ask for access') }}</BaseButton>
        </template>
        <p v-if="askError" class="mt-1 text-xs text-status-danger-700 dark:text-status-danger-400" role="alert">{{ askError }}</p>
      </div>

      <!-- trinity-enterprise#1161: I own the steward agent; members who can't use it. -->
      <div
        v-if="access?.can_grant && missing.length"
        class="mt-3 rounded-lg border border-status-warning-200 dark:border-status-warning-900/50 bg-status-warning-50 dark:bg-status-warning-900/20 px-3 py-2.5"
        data-testid="project-steward-missing-access"
      >
        <p class="text-sm font-semibold text-status-warning-700 dark:text-status-warning-400">
          {{ missing.length === 1 ? '1 member can\'t' : `${missing.length} members can't` }} use {{ access.agent }}
        </p>
        <ul class="mt-2 space-y-2">
          <li v-for="m in missing" :key="m.email" class="flex items-center gap-2" :data-testid="`project-steward-missing-${m.email}`">
            <span class="min-w-0 flex-1 truncate text-sm text-gray-800 dark:text-gray-200" :title="m.email">{{ m.label }}</span>
            <BaseBadge v-if="m.request_id" variant="warning">Asked</BaseBadge>
            <BaseButton size="sm" variant="secondary" :disabled="granting === m.email" @click="giveAccess(m.email)">
              {{ granting === m.email ? 'Giving…' : 'Give access' }}
            </BaseButton>
          </li>
        </ul>
        <p v-if="grantError" class="mt-1 text-xs text-status-danger-700 dark:text-status-danger-400" role="alert">{{ grantError }}</p>
      </div>
      <p v-if="access?.unknown" class="mt-2 text-xs text-gray-500 dark:text-gray-400" data-testid="project-steward-access-unknown">Couldn't check who can use this agent.</p>

      <ul class="mt-2 space-y-1 text-sm text-gray-700 dark:text-gray-300">
        <li v-if="'last_sweep_at' in steward" data-testid="project-steward-sweep">
          <template v-if="steward.last_sweep_at">Last sweep <span :title="absolute(steward.last_sweep_at)">{{ relative(steward.last_sweep_at) }}</span></template>
          <template v-else>No sweep recorded yet</template>
        </li>
        <li v-if="steward.schedule" data-testid="project-steward-schedule">{{ scheduleLine }}<span v-if="scheduleAt" :title="absolute(scheduleAt)"> {{ relative(scheduleAt) }}</span></li>
        <!-- ent#1169: does the agent carry the project-management skills? -->
        <li v-if="skillLine" :class="skillSet?.held ? '' : 'font-medium text-status-warning-700 dark:text-status-warning-300'" data-testid="project-steward-skills">{{ skillLine }}</li>
        <li v-if="!health">No health update yet</li>
        <li v-if="rollup.health_due" class="font-medium text-status-warning-700 dark:text-status-warning-300" data-testid="project-steward-health-due">Health update due</li>
      </ul>
      <!-- trinity-enterprise#1236: whether Trinity wakes it, the one reason it
           doesn't, its last run and what wakes it next. Off until a person
           starts it. An older backend sends no `wake`: the old line stays. -->
      <div v-if="wake" class="mt-3 border-t border-gray-200 dark:border-gray-750 pt-3" data-testid="project-steward-wake">
        <p
          class="text-sm font-medium"
          :class="wake.state === 'on' ? 'text-gray-900 dark:text-gray-100' : 'text-status-warning-700 dark:text-status-warning-300'"
          data-testid="project-steward-wake-state"
        >{{ wakeLine }}</p>
        <ul class="mt-1 space-y-1 text-sm text-gray-700 dark:text-gray-300">
          <li v-if="wakeNext" data-testid="project-steward-wake-next">{{ wakeNext.text }}<span v-if="wakeNext.at" :title="absolute(wakeNext.at)"> {{ relative(wakeNext.at) }}</span></li>
          <li v-if="wake.last_run" data-testid="project-steward-last-run">
            Last run <span :title="absolute(wake.last_run.at)">{{ relative(wake.last_run.at) }}</span><template v-if="runStatus"> · {{ runStatus }}</template>
          </li>
          <li v-if="skipLine" data-testid="project-steward-wake-skip">{{ skipLine }}</li>
        </ul>
        <p v-if="wake.reason === 'autonomy_off' && wake.is_agent_owner" class="mt-2 text-xs text-gray-600 dark:text-gray-300" data-testid="project-steward-autonomy">
          <router-link :to="agentRoute" :class="INBOX_LINK_CLASS" class="font-medium" data-testid="project-steward-autonomy-link">Turn on autonomy for {{ steward.ref }}</router-link>.
          Autonomy also lets its schedules and reminders run.
        </p>
        <BaseButton
          v-if="wake.can_change && wakeVerb"
          class="mt-2" size="sm" variant="secondary" :disabled="changingWake"
          :data-testid="`project-steward-wake-${wakeVerb}`"
          @click="changeWake"
        >{{ changingWake ? (wakeVerb === 'start' ? 'Starting…' : 'Pausing…') : (wakeVerb === 'start' ? 'Start waking the steward' : 'Pause') }}</BaseButton>
        <p v-if="wake.can_change && wakeVerb === 'start'" class="mt-1 text-xs text-gray-600 dark:text-gray-300" data-testid="project-steward-wake-start-hint">
          Changes made before you start get one catch-up turn when you do, within the hourly limit.
        </p>
        <p v-if="wakeError" class="mt-1 text-xs text-status-danger-700 dark:text-status-danger-400" role="alert">{{ wakeError }}</p>
      </div>
      <p class="mt-3 text-xs text-gray-500 dark:text-gray-400" data-testid="project-steward-how">
        <template v-if="wake">Trinity wakes it when someone else changes a task, when an ask on this project is answered, and once a day while work is open. It keeps the record and starts no task runs.</template>
        <template v-else>It reads this project's digest on its own schedule, verifies finished tasks and records health. Trinity doesn't run it.</template>
      </p>
    </template>

    <template v-else>
      <ul v-if="waiting.length" class="mt-2 space-y-1 text-sm text-gray-700 dark:text-gray-300">
        <li v-for="line in waiting" :key="line.key" :data-testid="`project-steward-${line.key}`">{{ line.text }}</li>
        <li v-if="rollup.health_due" class="font-medium text-status-warning-700 dark:text-status-warning-300" data-testid="project-steward-health-due">Health update due</li>
      </ul>
      <p v-else class="mt-2 text-sm text-gray-600 dark:text-gray-300">
        {{ rollup.health_due ? 'Health update due.' : 'Nothing is waiting on the steward.' }}
      </p>
      <p class="mt-3 text-xs text-gray-500 dark:text-gray-400">
        The steward verifies finished tasks, unblocks stuck ones and says how it's going.
      </p>
      <!-- ent#1169 (R5 on ent#1137): what having no steward AGENT means. -->
      <p class="mt-2 text-xs text-gray-500 dark:text-gray-400" data-testid="project-steward-no-agent">
        No steward agent: nothing reads this project's digest or checks done claims between visits.<template v-if="project.can?.edit"> Pick an agent as steward in Edit project to have that done.</template>
      </p>
      <BaseButton v-if="isMe" class="mt-3" size="sm" variant="secondary" data-testid="project-steward-open-stewarding" @click="$emit('open-stewarding')">Open I steward</BaseButton>
    </template>
  </BaseCard>
</template>

<script setup>
import { computed, ref } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import PortalAvatar from '@/components/portal/PortalAvatar.vue'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import { askInboxRoute } from '../portalRailAsks'
import { INBOX_LINK_CLASS } from '../portalUtils'
import {
  HEALTH_BADGE, healthLabel, attentionLines, stewardScheduleLine, projectErrorMessage,
  stewardWakeLine, stewardWakeNext, stewardWakeSkipLine, stewardRunStatus,
} from './projectsUtils'
import { useProjectsStore } from '@/stores/projects'

const props = defineProps({
  project: { type: Object, required: true },
  myEmail: { type: String, default: '' },
  // ent#1169: this session's create/edit answer for the steward's skills.
  stewardOutcome: { type: Object, default: null },
})
defineEmits(['open-stewarding'])

const steward = computed(() => props.project.steward || null)
const rollup = computed(() => props.project.rollup || { by_status: {} })
const health = computed(() => rollup.value.health || null)
const isMe = computed(() => steward.value?.kind === 'person' && Boolean(props.myEmail) && steward.value.ref === props.myEmail)
const who = computed(() => {
  const s = steward.value
  if (!s) return 'Not set'
  if (s.kind === 'agent') return `${s.ref} (agent)`
  return isMe.value ? 'You' : s.ref
})
const proposal = computed(() => props.project.proposal || null)
// trinity-enterprise#1089: the request waits on the agent's primary contact,
// who may be several people — `waiting_on_people` is the list (an older
// backend sends only `waiting_on`, one email).
const waitingOnPeople = computed(() => {
  const p = proposal.value
  if (!p) return []
  return Array.isArray(p.waiting_on_people) ? p.waiting_on_people : (p.waiting_on ? [p.waiting_on] : [])
})
const waitsOnMe = computed(() => Boolean(props.myEmail) && waitingOnPeople.value.includes(props.myEmail))
const waitingOn = computed(() => (waitsOnMe.value ? 'you' : proposal.value?.waiting_on || "the agent's primary contact"))
// The Needs you row's link: straight to this decision in the Inbox, routed in
// the app (no Workspace reload).
const inboxRoute = computed(() => {
  const p = proposal.value
  return p ? askInboxRoute({ id: p.ask_id, agent_name: p.proposed_by }) : null
})
// trinity-enterprise#1236: the read's `wake` block; absent on an older backend.
const wake = computed(() => steward.value?.wake || null)
const scheduleLine = computed(() => stewardScheduleLine(steward.value?.schedule, wake.value))
const wakeLine = computed(() => stewardWakeLine(wake.value, steward.value?.ref))
const wakeNext = computed(() => stewardWakeNext(wake.value?.next))
const skipLine = computed(() => (wake.value?.state === 'on' ? stewardWakeSkipLine(wake.value.last_skip) : ''))
const runStatus = computed(() => stewardRunStatus(wake.value?.last_run?.status))
const agentRoute = computed(() => ({ path: `/agents/${encodeURIComponent(steward.value?.ref || '')}` }))
// Start when nobody started it or a person paused it; Pause while it is started.
// No verb for a project that isn't running: Start would not change what the card says.
const wakeVerb = computed(() => {
  const w = wake.value
  if (!w || w.reason === 'project_not_running' || w.reason === 'not_agent_steward') return ''
  if (w.reason === 'not_started' || w.reason === 'paused') return 'start'
  return w.started ? 'pause' : ''
})
const changingWake = ref(false)
const wakeError = ref('')

async function changeWake() {
  const verb = wakeVerb.value
  changingWake.value = true
  wakeError.value = ''
  try {
    const store = useProjectsStore()
    await (verb === 'start' ? store.startStewardWake(props.project.id) : store.pauseStewardWake(props.project.id))
  } catch (err) {
    wakeError.value = projectErrorMessage(err)
  } finally {
    changingWake.value = false
  }
}
// ent#1169: the read's `skill_set` ({held, available}); absent on an older backend → no line.
const skillSet = computed(() => steward.value?.skill_set || null)
// The outcome names WHY nothing was assigned; it is shown only while it still
// describes this steward and the page read doesn't already say it holds the set.
const OUTCOME_REASONS = ['awaiting_owner', 'not_permitted', 'failed', 'unavailable']
const outcomeLine = computed(() => {
  const o = props.stewardOutcome
  if (!o || !OUTCOME_REASONS.includes(o.state) || !o.message) return ''
  if (steward.value?.kind !== 'agent' || steward.value.ref !== o.agent) return ''
  if (skillSet.value?.held) return ''
  return o.message
})
const skillLine = computed(() => {
  if (outcomeLine.value) return outcomeLine.value
  const s = skillSet.value
  if (!s || s.held === null || s.held === undefined) return ''
  if (s.held) return 'Has the project-management skills'
  if (s.available === false) return "The project-management skills aren't available on this Trinity yet"
  return "Doesn't have the project-management skills yet, so it can't keep the record. Its owner or an admin can give them on the agent's Skills tab."
})
const scheduleAt = computed(() => (steward.value?.schedule?.state === 'scheduled' ? steward.value.schedule.next_run_at : null))
// What waits on a person steward: the task lines only — health has its own
// line here, asks have their own card.
const waiting = computed(() => attentionLines({ ...rollup.value, open_asks: 0, health_due: false, quiet: false })
  .filter((l) => l.key === 'verify' || l.key === 'stuck'))

// trinity-enterprise#1161 — the steward agent's seat. The store is reached on
// click only, so a read-only mount needs no Pinia.
const access = computed(() => props.project.steward_access || null)
const missing = computed(() => access.value?.members_without_access || [])
const asking = ref(false)
const askError = ref('')
const granting = ref('')
const grantError = ref('')

async function askForAccess() {
  asking.value = true
  askError.value = ''
  try {
    await useProjectsStore().requestStewardAccess(props.project.id)
  } catch (err) {
    askError.value = projectErrorMessage(err)
  } finally {
    asking.value = false
  }
}

async function giveAccess(email) {
  granting.value = email
  grantError.value = ''
  try {
    await useProjectsStore().grantStewardAccess(props.project.id, email)
  } catch (err) {
    grantError.value = projectErrorMessage(err)
  } finally {
    granting.value = ''
  }
}

const relative = (t) => (t ? formatRelativeTime(t) : '')
const absolute = (t) => (t ? formatLocalDateTime(t) : '')
</script>
