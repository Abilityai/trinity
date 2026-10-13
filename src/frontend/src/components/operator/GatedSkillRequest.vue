<!--
  trinity-enterprise#755 — the gated-skill approval card: which agent would run
  which skill, with what input, on whose request, by when, and how it ended.

  ONE component for the Workspace Inbox, the chat card and Operations
  (`PortalAsks`, `QueueCard`, `ResolvedCard`), rendered in place of the generic
  `QueueProposal` when the ask carries the platform's `gate` view. The input is
  the ask's frozen `proposal.input` — exactly what Approve runs. The card says
  everything the gate's question says, so its hosts leave the question out.
  Every value is requester- or agent-authored: text interpolation only, never
  v-html.
-->
<template>
  <div data-testid="gate-card">
    <dl class="rounded-lg bg-gray-50 dark:bg-gray-900/50 px-3 py-2 space-y-2 text-[12.5px]">
      <div :class="ROW">
        <dt :class="TERM">Agent</dt>
        <dd :class="VALUE" data-testid="gate-card-agent">{{ item.agent_name }}</dd>
      </div>
      <div :class="ROW">
        <dt :class="TERM">{{ skills.length === 1 ? 'Skill' : 'Skills' }}</dt>
        <dd class="min-w-0 space-y-1">
          <p v-for="skill in skills" :key="skill.name" data-testid="gate-card-skill">
            <span class="font-mono font-semibold text-gray-900 dark:text-gray-100">{{ skill.name }}</span>
            <span v-if="skill.description" class="text-gray-600 dark:text-gray-300"> — {{ skill.description }}</span>
          </p>
        </dd>
      </div>
      <div :class="ROW">
        <dt :class="TERM">Input</dt>
        <dd class="min-w-0 flex-1">
          <!-- Bounded either way: folded to a few lines, or scrolling in its own box. -->
          <pre
            class="font-mono whitespace-pre-wrap break-words text-gray-900 dark:text-gray-100 overflow-y-auto"
            :class="long && folded ? 'max-h-24' : 'max-h-80'"
            data-testid="gate-card-input"
          >{{ input }}</pre>
          <BaseButton
            v-if="long"
            size="sm"
            variant="ghost"
            class="mt-1"
            :aria-expanded="!folded"
            data-testid="gate-card-input-toggle"
            @click.stop="folded = !folded"
          >{{ folded ? 'Show the whole input' : 'Show less' }}</BaseButton>
        </dd>
      </div>
      <div v-if="gate.also_sent" :class="ROW">
        <dt :class="TERM">Also sent</dt>
        <dd :class="[VALUE, 'whitespace-pre-wrap']" data-testid="gate-card-also-sent">{{ gate.also_sent }}</dd>
      </div>
      <div :class="ROW">
        <dt :class="TERM">Requested by</dt>
        <dd :class="VALUE" data-testid="gate-card-requester">
          {{ requester.label || 'A caller' }}
          <template v-if="requester.execution_id && requester.agent">
            ·
            <router-link
              :to="runPath(requester.agent, requester.execution_id)"
              :class="LINK"
              data-testid="gate-card-requester-run"
              @click.stop
            >from this run</router-link>
          </template>
        </dd>
      </div>
      <div :class="ROW">
        <dt :class="TERM">Raised</dt>
        <dd :class="VALUE" :title="formatLocalDateTime(item.created_at)" data-testid="gate-card-raised">
          {{ formatRelativeTime(item.created_at) }}
        </dd>
      </div>
      <div v-if="item.expires_at" :class="ROW">
        <dt :class="TERM">Deadline</dt>
        <dd :class="[VALUE, 'tabular-nums']" data-testid="gate-card-deadline">
          {{ formatLocalDateTime(item.expires_at) }}
        </dd>
      </div>
      <div v-if="waiting" :class="ROW">
        <dt :class="TERM">Waiting on</dt>
        <dd :class="VALUE" data-testid="gate-card-waiting-on">{{ waiting }}</dd>
      </div>
    </dl>

    <p
      v-if="deciding"
      class="mt-2 text-[12.5px] text-gray-600 dark:text-gray-300"
      data-testid="gate-card-hint"
    >Approve runs it once, exactly as written. Reject runs nothing.</p>
    <p
      v-if="outcome"
      class="mt-2 flex flex-wrap items-center gap-2 text-[12.5px] text-gray-600 dark:text-gray-300"
      data-testid="gate-card-outcome"
      :data-state="outcome.state"
    >
      <BaseBadge :variant="outcome.variant">{{ outcome.label }}</BaseBadge>
      <span>{{ outcome.text }}</span>
      <router-link
        v-if="outcome.ran && gate.execution_id"
        :to="runPath(item.agent_name, gate.execution_id)"
        :class="LINK"
        data-testid="gate-card-run"
        @click.stop
      >Open the run</router-link>
    </p>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import { gateOutcome, gateInputLong } from '../../utils/skillGateCard'
import { formatLocalDateTime, formatRelativeTime } from '../../utils/timestamps'

const ROW = 'flex items-start gap-3'
const TERM = 'flex-shrink-0 w-24 text-gray-500 dark:text-gray-400'
const VALUE = 'min-w-0 break-words text-gray-900 dark:text-gray-100'
const LINK = 'font-medium underline underline-offset-2 text-action-primary-700 dark:text-action-primary-300 rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40'

const props = defineProps({
  // The ask: an operator-queue row or a Workspace ask, carrying `gate`.
  item: { type: Object, required: true },
})

const gate = computed(() => props.item.gate || {})
const skills = computed(() => (Array.isArray(gate.value.skills) ? gate.value.skills : []))
const requester = computed(() => gate.value.requester || {})
const input = computed(() => {
  const value = props.item.proposal?.input
  return typeof value === 'string' ? value : ''
})
const long = computed(() => gateInputLong(input.value))
const folded = ref(true)
const outcome = computed(() => gateOutcome(props.item))
// Who it is waiting on, for a viewer who cannot decide it — and only while it waits.
const waiting = computed(() => (
  !outcome.value && props.item.status === 'pending' && gate.value.can_decide !== true
    ? (gate.value.waiting_on || []).join(', ')
    : ''
))

const deciding = computed(() => (
  !outcome.value && props.item.status === 'pending' && gate.value.can_decide === true
))

const runPath = (agent, executionId) =>
  `/agents/${encodeURIComponent(agent)}/executions/${encodeURIComponent(executionId)}`
</script>
