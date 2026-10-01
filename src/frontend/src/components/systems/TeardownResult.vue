<template>
  <div class="space-y-4">
    <!-- Status header — switched on `status`, never the HTTP code --------- -->
    <div class="rounded-lg border p-4" :class="tone.box">
      <div class="flex items-start gap-3">
        <span class="text-lg" aria-hidden="true">{{ tone.icon }}</span>
        <div class="flex-1">
          <h3 class="font-semibold" :class="tone.title">{{ tone.heading }}</h3>
          <p class="mt-1 text-sm text-gray-600 dark:text-gray-400">{{ tone.detail }}</p>
          <p class="mt-2 text-xs text-gray-500 dark:text-gray-400 tabular-nums">
            System <span class="font-mono">{{ result.system_name }}</span> ·
            {{ removed.length }} of {{ members.length }} agent(s) removed
          </p>
        </div>
      </div>
    </div>

    <!-- Removed ----------------------------------------------------------- -->
    <section v-if="removed.length">
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">
        Removed ({{ removed.length }})
      </h4>
      <ul class="space-y-1.5">
        <li
          v-for="m in removed"
          :key="m.name"
          class="flex flex-wrap items-center gap-2"
        >
          <span class="font-mono text-sm text-gray-900 dark:text-gray-100 break-all">
            {{ m.name }}
          </span>
          <BaseBadge v-if="m.outcome === 'discarded'" variant="danger">
            discarded — not recoverable
          </BaseBadge>
          <BaseBadge v-else variant="success">
            recoverable
          </BaseBadge>
        </li>
      </ul>
    </section>

    <!-- Not removed: skipped and failed are DIFFERENT facts --------------- -->
    <section v-if="skipped.length">
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">
        Left in place ({{ skipped.length }})
      </h4>
      <ul class="space-y-2">
        <li
          v-for="m in skipped"
          :key="m.name"
          class="rounded-md border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 p-3"
        >
          <div class="flex flex-wrap items-baseline justify-between gap-2">
            <span class="font-mono text-sm text-gray-900 dark:text-gray-100 break-all">
              {{ m.name }}
            </span>
            <span v-if="m.status_code" class="text-xs text-gray-500 dark:text-gray-400 tabular-nums">
              HTTP {{ m.status_code }}
            </span>
          </div>
          <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">
            {{ skipExplanation(m) }}
          </p>
        </li>
      </ul>
    </section>

    <section v-if="failed.length">
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">
        Failed ({{ failed.length }})
      </h4>
      <ul class="space-y-2">
        <li
          v-for="m in failed"
          :key="m.name"
          class="rounded-md border border-status-danger-200 dark:border-status-danger-800 bg-white dark:bg-gray-800 p-3"
        >
          <div class="flex flex-wrap items-baseline justify-between gap-2">
            <span class="font-mono text-sm text-gray-900 dark:text-gray-100 break-all">
              {{ m.name }}
            </span>
            <span v-if="m.status_code" class="text-xs text-gray-500 dark:text-gray-400 tabular-nums">
              HTTP {{ m.status_code }}
            </span>
          </div>
          <p v-if="m.outcome === 'aborted'" class="mt-1 text-sm text-gray-600 dark:text-gray-300">
            Not attempted — the teardown stopped at an earlier failure.
          </p>
          <!-- Reasons are credential-sanitized server-side but not HTML-sanitized:
               plain text only (H-005). -->
          <p v-else-if="m.reason" class="mt-1 text-sm text-status-danger-700 dark:text-status-danger-300 break-words">
            {{ m.reason }}
          </p>
        </li>
      </ul>
    </section>

    <!-- The view + tag ---------------------------------------------------- -->
    <section v-if="result.system_views?.length">
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">System views</h4>
      <ul class="space-y-1.5 text-sm">
        <li
          v-for="v in result.system_views"
          :key="v.id"
          class="flex flex-wrap items-center gap-2 text-gray-700 dark:text-gray-300"
        >
          <span><strong>{{ v.name }}</strong></span>
          <BaseBadge :variant="v.outcome === 'removed' ? 'success' : 'warning'">
            {{ v.outcome }}
          </BaseBadge>
          <span v-if="v.reason" class="text-xs text-gray-500 dark:text-gray-400">
            {{ v.reason }}
          </span>
        </li>
      </ul>
    </section>

    <section v-if="warnings.length">
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">Notes</h4>
      <ul class="space-y-1 text-sm text-gray-600 dark:text-gray-300 list-disc list-inside">
        <li v-for="w in warnings" :key="w">{{ w }}</li>
      </ul>
    </section>

    <!-- Recovery, verbatim from the server -------------------------------- -->
    <section
      v-if="result.recovery"
      class="rounded-lg border border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900/40 p-4"
    >
      <h4 class="text-sm font-semibold text-gray-900 dark:text-white">Recovery</h4>
      <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">{{ result.recovery }}</p>
    </section>

    <!-- The natural next step (principle 27) ------------------------------ -->
    <div class="flex flex-wrap gap-2">
      <!-- `teardown-goto-fleet`, not `goto-fleet`: DeployResult.vue already owns
           that id, and on an entitled build a deploy result and a teardown
           result can be on the Library page at the same time. Same reasoning as
           the acknowledgement id. -->
      <BaseButton data-testid="teardown-goto-fleet" @click="$emit('view-fleet')">
        View the fleet
      </BaseButton>
      <BaseButton variant="secondary" @click="$emit('remove-another')">
        Remove another system
      </BaseButton>
    </div>
  </div>
</template>

<script setup>
/**
 * Outcome of a real (non-dry-run) system teardown (trinity-enterprise#454, AC #3).
 *
 * Switches on `status`, never the HTTP code — `partial` arrives as 200 and
 * `failed` arrives as **500 with the full report as the body** (the store
 * returns that as a result, not an error, because the per-member reasons are
 * the only actionable output). Same rule and same reason as DeployResult.vue.
 *
 * The three not-removed outcomes are rendered as three different things,
 * because an operator acts on them differently:
 *
 *   skipped  a decision — no permission, already gone, or not confirmed.
 *            NOT a breakage, and rendering it as one would send someone
 *            debugging a working system.
 *   failed   something broke; the reason is actionable.
 *   aborted  never attempted, because `strict` stopped the run earlier.
 *
 * Removal outcomes are likewise two facts, not one: `deleted` is recoverable
 * within the window, `discarded` (an ephemeral ghost) is gone for good. Saying
 * "removed" for both would make the recovery promise where it does not hold.
 */
import { computed } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'

const props = defineProps({
  result: { type: Object, required: true }
})

defineEmits(['view-fleet', 'remove-another'])

const members = computed(() => props.result.members || [])
const removed = computed(
  () => members.value.filter((m) => m.outcome === 'deleted' || m.outcome === 'discarded')
)
const skipped = computed(() => members.value.filter((m) => m.outcome === 'skipped'))
const failed = computed(
  () => members.value.filter((m) => m.outcome === 'failed' || m.outcome === 'aborted')
)
/**
 * Preview-time instructions do not belong on a result.
 *
 * The server's prefix warning ends *"Uncheck anything that does not belong
 * before confirming"* — an instruction for a screen that no longer exists,
 * printed underneath a report of what has already been removed. It is also
 * the opt-OUT wording the preview stopped using.
 *
 * Only that one line is dropped. Every other warning is a statement of fact
 * about what happened (agents excluded, members with no container, ephemeral
 * members with no recovery window) and still reads correctly after the event.
 */
const PREVIEW_ONLY = /matched by NAME ONLY/i

const warnings = computed(
  () => (props.result.warnings || []).filter((w) => !PREVIEW_ONLY.test(w))
)

const SKIP_COPY = {
  system_agent: 'A platform agent — never removable by a teardown.',
  not_authorized: 'You do not have permission to delete this agent. Ask its owner or an admin.',
  not_found: 'Already gone — nothing to remove.',
  not_confirmed: 'You did not select this agent, or it joined the system after the preview.',
  not_a_member: 'No longer a member of this system — it may have been renamed, re-tagged, or already removed.'
}

function skipExplanation (m) {
  return SKIP_COPY[m.reason] || m.reason || 'Left in place.'
}

const TONES = {
  torn_down: {
    icon: '✅',
    heading: 'System removed',
    detail: 'Every confirmed agent was removed and the system view was handled.',
    box: 'border-status-success-300 dark:border-status-success-700 bg-status-success-50 dark:bg-status-success-900/20',
    title: 'text-status-success-800 dark:text-status-success-200'
  },
  partial: {
    icon: '⚠️',
    heading: 'Some agents were removed',
    detail: 'The rest are listed below with what happened to each. Re-running the '
      + 'teardown is safe — agents already removed are reported as such rather than '
      + 'failing.',
    box: 'border-status-warning-300 dark:border-status-warning-700 bg-status-warning-50 dark:bg-status-warning-900/20',
    title: 'text-status-warning-900 dark:text-status-warning-100'
  },
  failed: {
    icon: '⛔',
    heading: 'Nothing was removed',
    detail: 'Every attempt failed. The system is intact — fix the causes below and '
      + 'try again.',
    box: 'border-status-danger-300 dark:border-status-danger-700 bg-status-danger-50 dark:bg-status-danger-900/20',
    title: 'text-status-danger-800 dark:text-status-danger-200'
  },
  // A dry-run status should never reach this component, but rendering something
  // honest beats rendering nothing (the DeployResult.vue precedent).
  preview: {
    icon: 'ℹ️',
    heading: 'Preview only — nothing was removed',
    detail: 'This is a dry-run result.',
    box: 'border-status-info-300 dark:border-status-info-700 bg-status-info-50 dark:bg-status-info-900/20',
    title: 'text-status-info-800 dark:text-status-info-200'
  }
}

const UNKNOWN_TONE = {
  icon: '❓',
  heading: 'Unrecognized outcome',
  detail: 'The server reported a status this version of the UI does not know. '
    + 'Check the agent list to see what actually exists.',
  box: 'border-gray-300 dark:border-gray-600 bg-gray-50 dark:bg-gray-800',
  title: 'text-gray-900 dark:text-white'
}

const tone = computed(() => TONES[props.result.status] || UNKNOWN_TONE)
</script>
