<template>
  <div class="space-y-4">
    <!-- Status header ------------------------------------------------------
         Labelled "agents created", never "success": `status` describes AGENT
         CREATION only. Folder, permission, schedule, tag and start failures all
         land in `warnings[]` while `status` stays "deployed", so a fleet where
         every schedule failed and nothing started still reports "deployed". -->
    <div class="rounded-lg border p-4" :class="tone.box">
      <div class="flex items-start gap-3">
        <span class="text-lg" aria-hidden="true">{{ tone.icon }}</span>
        <div class="flex-1">
          <h3 class="font-semibold" :class="tone.title">{{ tone.heading }}</h3>
          <p class="mt-1 text-sm text-gray-600 dark:text-gray-400">{{ tone.detail }}</p>
          <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">
            System <span class="font-mono">{{ result.system_name }}</span> ·
            {{ created.length }} of {{ created.length + failed.length }} agent(s) created
          </p>
        </div>
      </div>
    </div>

    <!-- Warnings: prominent, not a footnote ------------------------------- -->
    <section
      v-if="warnings.length"
      class="rounded-lg border border-status-warning-300 dark:border-status-warning-700 bg-status-warning-50 dark:bg-status-warning-900/20 p-4"
    >
      <h4 class="font-semibold text-status-warning-900 dark:text-status-warning-100">
        {{ warnings.length }} thing{{ warnings.length === 1 ? '' : 's' }} needing attention
      </h4>
      <p class="mt-1 text-xs text-status-warning-800 dark:text-status-warning-200">
        Configuration applied after the agents were created is best-effort — these did not
        stop the deployment, but they did not take effect either.
      </p>
      <ul class="mt-2 space-y-1">
        <li
          v-for="w in warnings"
          :key="w"
          class="text-sm text-status-warning-900 dark:text-status-warning-100"
        >
          {{ w }}
        </li>
      </ul>
    </section>

    <!-- Created ----------------------------------------------------------- -->
    <section v-if="created.length">
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">
        Created ({{ created.length }})
      </h4>
      <ul class="flex flex-wrap gap-2">
        <li
          v-for="name in created"
          :key="name"
          class="rounded-full bg-status-success-100 dark:bg-status-success-900/40 px-3 py-1 font-mono text-xs text-status-success-800 dark:text-status-success-200"
        >
          {{ name }}
        </li>
      </ul>
    </section>

    <!-- Failed ------------------------------------------------------------ -->
    <section v-if="failed.length">
      <h4 class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">
        Failed ({{ failed.length }})
      </h4>
      <ul class="space-y-2">
        <li
          v-for="f in failed"
          :key="`${f.short_name}-${f.name}`"
          class="rounded-md border border-status-danger-200 dark:border-status-danger-800 bg-white dark:bg-gray-800 p-3"
        >
          <div class="flex items-baseline justify-between gap-2">
            <span class="font-mono text-sm text-gray-900 dark:text-gray-100">{{ f.name }}</span>
            <span v-if="f.status_code" class="text-xs text-gray-500 dark:text-gray-400">
              HTTP {{ f.status_code }}
            </span>
          </div>
          <p v-if="f.template" class="mt-0.5 font-mono text-xs text-gray-500 dark:text-gray-400">
            {{ f.template }}
          </p>
          <!-- Plain text only (H-005). -->
          <p class="mt-1 text-sm text-status-danger-700 dark:text-status-danger-300">{{ f.reason }}</p>
        </li>
      </ul>
    </section>

    <!-- Applied configuration -------------------------------------------- -->
    <section v-if="created.length" class="flex flex-wrap gap-2 text-xs">
      <span class="rounded bg-gray-100 dark:bg-gray-800 px-2 py-1 text-gray-700 dark:text-gray-300">
        {{ result.permissions_configured || 0 }} permission grant(s)
      </span>
      <span class="rounded bg-gray-100 dark:bg-gray-800 px-2 py-1 text-gray-700 dark:text-gray-300">
        {{ result.schedules_created || 0 }} schedule(s)
      </span>
      <span class="rounded bg-gray-100 dark:bg-gray-800 px-2 py-1 text-gray-700 dark:text-gray-300">
        {{ result.tags_configured || 0 }} tag(s)
      </span>
      <span
        v-if="result.prompt_updated"
        class="rounded bg-status-warning-100 dark:bg-status-warning-900/40 px-2 py-1 text-status-warning-800 dark:text-status-warning-200"
      >
        platform-wide prompt replaced
      </span>
    </section>

    <!-- Next action (AC #5: never a dead end) ----------------------------- -->
    <section
      v-if="created.length"
      class="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4"
    >
      <h4 class="font-semibold text-gray-900 dark:text-white">What next</h4>
      <p class="mt-1 text-sm text-gray-600 dark:text-gray-400">
        <template v-if="viewCreated">
          A system view was created for this fleet.
        </template>
        <template v-else>
          Every agent was tagged <span class="font-mono">{{ result.system_name }}</span>, so the
          dashboard can filter to just this fleet.
        </template>
        The agents still need their credentials configured before they can do real work.
      </p>
      <div class="mt-3 flex flex-wrap gap-2">
        <button
          class="rounded-lg bg-action-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-action-primary-700"
          data-testid="goto-fleet"
          @click="$emit('view-fleet')"
        >
          {{ viewCreated ? 'Open the system view' : 'View this fleet' }}
        </button>
        <button
          class="rounded-lg border border-gray-300 dark:border-gray-600 px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700"
          @click="$emit('install-another')"
        >
          Install another system
        </button>
      </div>
    </section>
  </div>
</template>

<script setup>
/**
 * Outcome of a real (non-dry-run) manifest deploy (trinity-enterprise#126, AC #3/#5).
 *
 * Switches on all FIVE `status` values, never on the HTTP status code: `partial`
 * and `invalid` both arrive as HTTP 200, and `failed` arrives as HTTP 500 with the
 * full report as the body (the store returns that as a result, not an error).
 *
 * `status` covers agent creation ONLY. Post-create configuration — folders,
 * permissions, schedules, tags, and starting the agents — is best-effort and
 * degrades into `warnings[]` with `status` still reading "deployed". That is why
 * the header says "agents created" rather than "success", and why warnings render
 * as their own prominent panel rather than a footnote.
 */
import { computed } from 'vue'

const props = defineProps({
  result: { type: Object, required: true }
})

defineEmits(['view-fleet', 'install-another'])

const created = computed(() => props.result.agents_created || [])
const failed = computed(() => props.result.failed || [])
const warnings = computed(() => props.result.warnings || [])
const viewCreated = computed(() => Boolean(props.result.system_view_created))

const TONES = {
  deployed: {
    icon: '✅',
    heading: 'All agents created',
    detail: 'Every agent in the manifest was created and started.',
    box: 'border-status-success-300 dark:border-status-success-700 bg-status-success-50 dark:bg-status-success-900/20',
    title: 'text-status-success-800 dark:text-status-success-200'
  },
  partial: {
    icon: '⚠️',
    heading: 'Some agents were created',
    detail: 'The rest failed and are listed below. Re-running this manifest would create '
      + 'duplicates of the agents that succeeded — fix the cause and create the missing '
      + 'agents individually.',
    box: 'border-status-warning-300 dark:border-status-warning-700 bg-status-warning-50 dark:bg-status-warning-900/20',
    title: 'text-status-warning-900 dark:text-status-warning-100'
  },
  failed: {
    icon: '⛔',
    heading: 'No agents were created',
    detail: 'Nothing was deployed, so there is nothing to clean up. Fix the causes below '
      + 'and try again.',
    box: 'border-status-danger-300 dark:border-status-danger-700 bg-status-danger-50 dark:bg-status-danger-900/20',
    title: 'text-status-danger-800 dark:text-status-danger-200'
  },
  // `valid`/`invalid` are dry-run statuses. They should never reach this
  // component, but rendering something honest beats rendering nothing.
  valid: {
    icon: 'ℹ️',
    heading: 'Preview only — nothing was deployed',
    detail: 'This is a dry-run result.',
    box: 'border-status-info-300 dark:border-status-info-700 bg-status-info-50 dark:bg-status-info-900/20',
    title: 'text-status-info-800 dark:text-status-info-200'
  },
  invalid: {
    icon: 'ℹ️',
    heading: 'Preview found blockers — nothing was deployed',
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
